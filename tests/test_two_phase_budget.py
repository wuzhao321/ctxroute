"""Two-phase accounting uses local transports and temporary ledgers only."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest

from ctxroute.domain import ModelRequest
from ctxroute.models.base import ModelFailure
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.tracing.budget import BudgetStopped, Ledger
from ctxroute.tracing.cost import Price
from ctxroute.tracing.stage_budget import StageBudget, StageLedger
from ctxroute.tracing.two_phase_budget import (
    TwoPhaseBudget,
    TwoPhaseLedger,
    open_two_phase_ledger,
)
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
PEAK = Price(D("0.044"), D("1.32"), D("3.96"))


@pytest.fixture
def policy(tmp_path: Path) -> TwoPhaseBudget:
    parent = replace(
        StageBudget.load(ROOT / "configs/budgets/stage-cny45.json"),
        ledger="parent.sqlite",
        paid_execution_enabled=True,
    )
    old = StageLedger(tmp_path / parent.ledger, parent, enabled=True)
    old.bind_scope("old", "baseline")
    old.reserve("known", D(".1"), "model_api", scope="old", scope_limit=D(2))
    old.settle("known", D(".05"))
    old.reserve("unknown", D(".03"), "model_api", scope="old", scope_limit=D(2))
    return TwoPhaseBudget(
        "synthetic-test",
        "annex.sqlite",
        parent,
        hashlib.sha256(old.path.read_bytes()).hexdigest(),
        D("4.55"),
        D("2.50"),
        "checkpoint",
        "validation",
        True,
    )


def reserve(book: TwoPhaseLedger, name: str, amount: D, phase: str = "collector") -> None:
    book.bind_scope(name, phase)
    book.reserve(name, amount, "model_api", scope=name, scope_limit=book.policy.phase_cap(phase))


def request(scope: str = "run", content: str = "Synthetic task only") -> ModelRequest:
    return ModelRequest(
        ({"role": "user", "content": content},),
        "strong",
        "action",
        8192,
        30,
        scope,
        8,
        D(".65"),
        "json_object",
    )


def test_caps_are_shared_across_scopes_without_phase_borrowing(
    tmp_path: Path, policy: TwoPhaseBudget
) -> None:
    book = TwoPhaseLedger(tmp_path, policy, enabled=True)
    reserve(book, "collector", D(".65"))
    book.settle("collector", D(".65"))
    with pytest.raises(BudgetStopped, match="collector cap"):
        reserve(book, "another-collector", D(".000000000001"))
    reserve(book, "replay", D(".35"), "replay")
    book.settle("replay", D(".35"))
    with pytest.raises(BudgetStopped):
        reserve(book, "another-replay", D(".01"), "replay")
    with pytest.raises(ValueError, match="move"):
        book.bind_scope("collector", "replay")
    assert book.snapshot().spent == D(1)
    assert book.snapshot().reserved == 0


def test_replay_cap_and_per_request_cap_do_not_use_collector_headroom(
    tmp_path: Path, policy: TwoPhaseBudget
) -> None:
    book = TwoPhaseLedger(tmp_path, policy, enabled=True)
    with pytest.raises(BudgetStopped, match="per-request"):
        reserve(book, "big", D(".8"))
    with pytest.raises(BudgetStopped, match="replay cap"):
        reserve(book, "replay-over", D(".36"), "replay")
    reserve(book, "replay-at", policy.phase_cap("replay"), "replay")
    book.settle("replay-at", policy.phase_cap("replay"))
    with pytest.raises(BudgetStopped, match="replay cap"):
        reserve(book, "new-run", D(".000000000001"), "replay")
    reserve(book, "collector-free", D(".3"))


def test_identity_history_and_reservation_formula_are_preserved(
    tmp_path: Path, policy: TwoPhaseBudget
) -> None:
    old_path = tmp_path / policy.parent.ledger
    before = old_path.read_bytes()
    book = TwoPhaseLedger(tmp_path, policy, enabled=True)
    for text in ("short", "历史" * 10000):
        req = request(content=text)
        assert book.reservation_bound(req, PEAK, 32000) == policy.parent.reservation_bound(
            req, PEAK, 32000
        )
    assert book.reservation_bound(request(), PEAK, 32000) == D(".30176256")
    reserve(book, "action", D(".3"))
    book.settle("action", D(".01"))
    assert old_path.read_bytes() == before
    new_before = book.path.read_bytes()
    with pytest.raises(ValueError, match="change"):
        TwoPhaseLedger(tmp_path, replace(policy, replay_cny=D(3)), enabled=True)
    with pytest.raises(ValueError, match="separate"):
        TwoPhaseLedger(tmp_path, replace(policy, ledger=policy.parent.ledger))
    legacy = Ledger(tmp_path / "legacy.sqlite", hard_limit=D(2), pause_at=D(2))
    legacy_before = legacy.path.read_bytes()
    with pytest.raises(ValueError, match="adopt"):
        TwoPhaseLedger(tmp_path, replace(policy, ledger="legacy.sqlite"))
    assert legacy.path.read_bytes() == legacy_before
    assert book.path.read_bytes() == new_before
    assert old_path.read_bytes() == before


@pytest.mark.parametrize(
    "mode", ["missing_usage", "timeout", "network", "invalid_usage", "interrupt", "known_failure"]
)
def test_failure_keeps_charge_blocks_both_phases_and_restart(
    tmp_path: Path, policy: TwoPhaseBudget, mode: str
) -> None:
    book = TwoPhaseLedger(tmp_path, policy, enabled=True)
    book.bind_scope("run", "collector")
    writer = RunWriter(tmp_path / "runs", "run", {"kind": "synthetic_test"})
    calls = 0

    def transport(payload: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic-secret")
        if mode == "network":
            raise httpx.ConnectError("synthetic-secret")
        if mode == "interrupt":
            raise KeyboardInterrupt
        raw: dict[str, Any] = {
            "choices": [{"message": {"content": '{"tool":"submit"}'}, "finish_reason": "stop"}]
        }
        if mode in {"invalid_usage", "known_failure"}:
            raw["usage"] = {
                "prompt_tokens": -1 if mode == "invalid_usage" else 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 1,
            }
        if mode == "known_failure":
            raw["choices"][0]["finish_reason"] = "length"
        return raw

    model = DeepSeekModel(
        key="synthetic-secret",
        prices=json.loads((ROOT / "data/prices/2026-11-17.json").read_text()),
        ledger=book,
        writer=writer,
        input_guard_tokens=32000,
        thinking="disabled",
        transport=transport,
    )
    with pytest.raises(KeyboardInterrupt if mode == "interrupt" else ModelFailure) as error:
        model.complete(request())
    if isinstance(error.value, ModelFailure):
        assert not error.value.retryable
    state = book.snapshot()
    assert state.blocked
    if mode == "known_failure":
        assert state.spent > 0 and state.reserved == 0
    else:
        assert state.spent == 0 and state.reserved == D(".30176256")
    reopened = TwoPhaseLedger(tmp_path, policy, enabled=True)
    for phase in ("collector", "replay"):
        reopened.bind_scope(phase, phase)
        with pytest.raises(BudgetStopped):
            model.complete(replace(request(), run_id=phase))
    assert calls == 1
    assert reopened.snapshot() == state
    with sqlite3.connect(book.path) as conn:
        failed = conn.execute("SELECT id, unknown_usage FROM failed_requests").fetchone()
    assert failed is not None and bool(failed[1]) == (mode != "known_failure")
    if mode != "known_failure":
        with pytest.raises(BudgetStopped, match="reconciliation"):
            reopened.settle(failed[0], D(0))
    for path in writer.path.rglob("*.json*"):
        assert "synthetic-secret" not in path.read_text()
    assert list((writer.path / "request_audits").glob("*.json"))
    assert list((writer.path / "response_audits").glob("*.json"))


@pytest.mark.parametrize("explicit_unknown", [False, True])
def test_crash_and_explicit_unknown_never_release_or_retry(
    tmp_path: Path, policy: TwoPhaseBudget, explicit_unknown: bool
) -> None:
    book = TwoPhaseLedger(tmp_path, policy, enabled=True)
    reserve(book, "interrupted", D(".3"))
    if explicit_unknown:
        book.settle("interrupted", None)
    else:
        with pytest.raises(BudgetStopped, match="Unsettled"):
            reserve(book, "overlap", D(".01"), "replay")
    restarted = TwoPhaseLedger(tmp_path, policy, enabled=True)
    assert restarted.snapshot().blocked and restarted.snapshot().reserved == D(".3")
    with pytest.raises(BudgetStopped):
        reserve(restarted, "retry", D(".01"), "replay")
    with pytest.raises(BudgetStopped, match="reconciliation"):
        restarted.settle("interrupted", D(0))


def test_parallel_dispatch_is_atomic(tmp_path: Path, policy: TwoPhaseBudget) -> None:
    books = [TwoPhaseLedger(tmp_path, policy, enabled=True) for _ in range(2)]

    def attempt(index: int) -> bool:
        try:
            reserve(books[index], f"sample-{index}", D(".3"), "replay")
            return True
        except BudgetStopped:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, (0, 1))) == 1
    assert books[0].snapshot().reserved == D(".3")


@pytest.mark.parametrize("guard", ["lane", "pause", "changed_parent"])
def test_parent_guards_are_read_only_and_fail_closed(
    tmp_path: Path, policy: TwoPhaseBudget, guard: str
) -> None:
    old = StageLedger(tmp_path / policy.parent.ledger, policy.parent, enabled=True)
    if guard != "changed_parent":
        targets = {"checkpoint": D("2.5")}
        if guard == "pause":
            targets.update(baseline=D("2.42"), validation=D(".5"))
        for lane, remaining in targets.items():
            old.bind_scope(lane, lane)
            i = 0
            while remaining:
                amount = min(remaining, D(".5"))
                old.reserve(f"{lane}-{i}", amount, "model_api", scope=lane, scope_limit=D(3))
                remaining -= amount
                i += 1
        policy = replace(policy, parent_sha256=hashlib.sha256(old.path.read_bytes()).hexdigest())
    book = TwoPhaseLedger(tmp_path, policy, enabled=True)
    if guard == "changed_parent":
        old.settle("unknown", D(".01"))
    before = old.path.read_bytes()
    with pytest.raises(
        BudgetStopped,
        match={"lane": "allocation", "pause": "pause", "changed_parent": "changed"}[guard],
    ):
        reserve(book, "new", D(".30176256"), "replay" if guard == "pause" else "collector")
    assert old.path.read_bytes() == before
    assert book.snapshot().spent == book.snapshot().reserved == 0


def test_explicit_loader_requires_both_execution_flags(
    tmp_path: Path, policy: TwoPhaseBudget
) -> None:
    source = json.loads((ROOT / "configs/budgets/stage-cny45.json").read_text())
    source.update(ledger="parent.sqlite", paid_execution_enabled=True)
    (tmp_path / "parent.json").write_text(json.dumps(source))
    config = {
        "schema": 1,
        "kind": "two_phase",
        "currency": "CNY",
        "experiment_id": policy.experiment_id,
        "ledger": policy.ledger,
        "parent_stage_config": "parent.json",
        "parent_sha256": policy.parent_sha256,
        "collector_cny": "4.55",
        "replay_cny": "2.50",
        "collector_lane": "checkpoint",
        "replay_lane": "validation",
        "paid_execution_enabled": False,
    }
    path = tmp_path / "two-phase.json"
    path.write_text(json.dumps(config))
    book = open_two_phase_ledger(tmp_path, path, enabled=True)
    with pytest.raises(BudgetStopped, match="disabled"):
        reserve(book, "off", D(".3"))
    config["paid_execution_enabled"] = True
    path.write_text(json.dumps(config))
    book = open_two_phase_ledger(tmp_path, path)
    with pytest.raises(BudgetStopped, match="disabled"):
        reserve(book, "still-off", D(".3"))
    book = open_two_phase_ledger(tmp_path, path, enabled=True)
    reserve(book, "explicit-local-transport-test", D(".3"))
