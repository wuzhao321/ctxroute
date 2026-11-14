import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from ctxroute.config import ArmConfig
from ctxroute.domain import ModelRequest
from ctxroute.models.base import ModelFailure
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.tracing.budget import BudgetStopped, Ledger
from ctxroute.tracing.cost import Price
from ctxroute.tracing.stage_budget import (
    StageBudget,
    StageLedger,
    budget_report,
    load_budget,
    open_budget_ledger,
)
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
PEAK = Price(D("0.044"), D("1.32"), D("3.96"))


@pytest.fixture
def policy() -> StageBudget:
    return replace(
        StageBudget.load(ROOT / "configs/budgets/stage-cny45.json"), paid_execution_enabled=True
    )


def reserve(book: StageLedger, request_id: str, usd: str, lane: str = "baseline") -> None:
    book.bind_scope(request_id, lane)
    book.reserve(
        request_id, D(usd), "model_api", scope=request_id, scope_limit=book.policy.lane_cap(lane)
    )


def request(size: int = 20) -> ModelRequest:
    return ModelRequest(
        ({"role": "user", "content": "x" * size},),
        "strong",
        "action",
        16384,
        900,
        "run",
        1,
        D("2.50"),
        "json_object",
    )


def test_stage_is_independent_and_history_is_not_adopted(
    tmp_path: Path, policy: StageBudget
) -> None:
    old_path = tmp_path / "results/budget/campaign.sqlite"
    old = Ledger(old_path, hard_limit=D(10), pause_at=D(8), enabled=True)
    old.reserve("historical", D("7.9"), "model_api")
    before = old_path.read_bytes()
    config: dict[str, Any] = {
        "stage_policy": policy,
        "paid_execution_enabled": True,
        "hard_limit": str(policy.usd_cap(policy.hard_cny)),
        "pause_at": str(policy.usd_cap(policy.pause_cny)),
    }
    new = open_budget_ledger(tmp_path, config)
    assert isinstance(new, StageLedger)
    reserve(new, "new", "0.5")  # Cross-stage occupancy exceeds the old $8 pause.
    report = budget_report(tmp_path, config)
    assert report["historical_reserved_usd"] == "7.9"
    assert D(report["cross_stage_occupied_usd"]) == D("8.4")
    assert new.snapshot().reserved == D("0.5")
    with pytest.raises(ValueError, match="cannot adopt"):
        StageLedger(old_path, policy, enabled=True)
    assert old_path.read_bytes() == before


def test_authorization_is_disabled_and_caps_round_down(tmp_path: Path) -> None:
    config = load_budget(ROOT)
    policy = config["stage_policy"]
    assert isinstance(policy, StageBudget)
    assert config["paid_execution_enabled"] is False
    assert policy.usd_cap(policy.hard_cny) * 7 <= 45
    assert policy.usd_cap(policy.pause_cny) * 7 <= 40
    assert policy.lane_cap("baseline") == policy.lane_cap("checkpoint") == D("2.5")
    assert policy.usd_cap(policy.paired_cny) == 5
    assert policy.lane_cap("validation") * 7 <= 10
    book = StageLedger(tmp_path / "disabled.sqlite", policy, enabled=True)
    with pytest.raises(BudgetStopped, match="disabled"):
        reserve(book, "no-charge", "0.1")
    assert book.snapshot().reserved == 0


def test_request_limit_and_unbound_scope_stop_before_reserving(
    tmp_path: Path, policy: StageBudget
) -> None:
    book = StageLedger(tmp_path / "stage.sqlite", policy, enabled=True)
    with pytest.raises(BudgetStopped, match="allocation"):
        book.reserve("unbound", D("0.1"), "model_api")
    reserve(book, "at-limit", "0.714285714285")
    with pytest.raises(BudgetStopped, match="per-request"):
        reserve(book, "over-limit", "0.714285714286")
    assert book.snapshot().reserved == D("0.714285714285")


def test_arm_pair_pause_and_hard_guards_include_pending(
    tmp_path: Path, policy: StageBudget
) -> None:
    book = StageLedger(tmp_path / "stage.sqlite", policy, enabled=True)
    for lane in ("baseline", "checkpoint"):
        for index in range(5):
            reserve(book, f"{lane}-{index}", "0.5", lane)
        with pytest.raises(BudgetStopped, match=lane):
            reserve(book, f"{lane}-over", "0.000001", lane)
    assert book.snapshot().reserved * 7 == 35
    reserve(book, "special", "0.5", "validation")
    with pytest.raises(BudgetStopped, match="pause threshold"):
        reserve(book, "pause", str(book.pause_at - D("5.5")), "validation")
    with pytest.raises(BudgetStopped, match="hard limit"):
        reserve(book, "hard", "1", "validation")
    assert book.snapshot().reserved * 7 == D("38.5")


def test_validation_cannot_spend_pair_allowance(tmp_path: Path, policy: StageBudget) -> None:
    book = StageLedger(tmp_path / "stage.sqlite", policy, enabled=True)
    reserve(book, "v1", "0.5", "validation")
    reserve(book, "v2", "0.5", "validation")
    reserve(book, "v3", str(policy.lane_cap("validation") - 1), "validation")
    with pytest.raises(BudgetStopped, match="validation"):
        reserve(book, "v-over", "0.000000000001", "validation")
    reserve(book, "baseline-still-free", "0.5")


def test_restart_duplicates_settlement_and_immutable_scope(
    tmp_path: Path, policy: StageBudget
) -> None:
    path = tmp_path / "stage.sqlite"
    book = StageLedger(path, policy, enabled=True)
    reserve(book, "failed", "0.5")
    book.settle("failed", None)
    book = StageLedger(path, policy, enabled=True)
    assert book.snapshot().reserved == D("0.5")
    with pytest.raises(BudgetStopped, match="already reserved"):
        reserve(book, "failed", "0.5")
    with pytest.raises(ValueError, match="cannot move"):
        book.bind_scope("failed", "validation")
    reserve(book, "known", "0.5")
    book.settle("known", D("0.1"))
    assert book.snapshot().spent == D("0.1") and book.snapshot().reserved == D("0.5")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="identity differs"):
        StageLedger(path, replace(policy, request_cny=D(4)), enabled=True)
    assert path.read_bytes() == before


def test_overrun_and_freeze_record_truth_and_block(tmp_path: Path, policy: StageBudget) -> None:
    book = StageLedger(tmp_path / "stage.sqlite", policy, enabled=True)
    reserve(book, "bad", "0.1")
    with pytest.raises(BudgetStopped, match="recorded and blocked"):
        book.settle("bad", D("0.2"))
    assert book.snapshot().spent == D("0.2")
    with pytest.raises(BudgetStopped, match="Blocked"):
        reserve(book, "next", "0.1")
    other = StageLedger(tmp_path / "uncertain.sqlite", policy, enabled=True)
    reserve(other, "unknown", "0.5")
    other.freeze("unknown", D("0.6"))
    assert other.snapshot().reserved == D("0.6") and other.snapshot().blocked


def test_concurrent_runs_share_the_arm_cap(tmp_path: Path, policy: StageBudget) -> None:
    path = tmp_path / "stage.sqlite"
    book = StageLedger(path, policy, enabled=True)
    for index in range(20):
        book.bind_scope(str(index), "baseline")

    def attempt(index: int) -> bool:
        try:
            StageLedger(path, policy, enabled=True).reserve(
                str(index), D("0.5"), "model_api", scope=str(index), scope_limit=D("2.5")
            )
            return True
        except BudgetStopped:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(20))) == 5
    assert book.snapshot().reserved == D("2.5")


def test_conservative_bound_and_candidate_configs(tmp_path: Path, policy: StageBudget) -> None:
    book = StageLedger(tmp_path / "stage.sqlite", policy)
    bound = book.reservation_bound(request(), PEAK, 32000)
    assert bound == D("0.30176256") and bound * 7 == D("2.11233792")
    assert book.reservation_bound(replace(request(), max_output_tokens=8192), PEAK, 32000) == bound
    # The full ASCII message envelope, not tokens or SSE events, defines this boundary.
    empty_size = len(json.dumps(request(0).messages, ensure_ascii=True).encode("ascii"))
    assert book.reservation_bound(request(168162 - empty_size), PEAK, 32000) * 7 <= 5
    assert book.reservation_bound(request(168163 - empty_size), PEAK, 32000) * 7 > 5
    for lane in ("baseline", "checkpoint"):
        cfg = ArmConfig.load(ROOT / f"configs/candidates/A1-{lane}-16k.json")
        assert cfg.limits.max_output_tokens == 16384 and cfg.limits.wall_seconds == 900
        assert cfg.limits.budget == policy.lane_cap(lane)
        assert cfg.context.mode == "none" and not cfg.router.enabled


@pytest.mark.parametrize("case", ["success", "invalid_usage", "transport_failure", "oversize"])
def test_gateway_reserves_full_stage_bound_before_transport(
    tmp_path: Path, policy: StageBudget, case: str
) -> None:
    book = StageLedger(tmp_path / "stage.sqlite", policy, enabled=True)
    book.bind_scope("run", "baseline")
    writer = RunWriter(tmp_path / "runs", "run", {"is_benchmark": False})
    calls = 0

    def transport(payload: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert book.snapshot().reserved == D("0.30176256")
        assert payload["max_tokens"] == 16384 and timeout == 900
        if case == "transport_failure":
            raise ConnectionError("synthetic disconnect")
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"tool":"submit"}'}}],
            "usage": {}
            if case == "invalid_usage"
            else {
                "prompt_tokens": 20,
                "completion_tokens": 8,
                "total_tokens": 28,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 20,
            },
        }

    model = DeepSeekModel(
        key="synthetic",
        prices=json.loads((ROOT / "data/prices/2026-11-07.json").read_text()),
        ledger=book,
        writer=writer,
        input_guard_tokens=32000,
        thinking="enabled",
        transport=transport,
    )
    if case == "success":
        assert model.complete(request()).text == '{"tool":"submit"}'
        assert book.snapshot().spent > 0 and book.snapshot().reserved == 0
    elif case == "oversize":
        with pytest.raises(BudgetStopped, match="per-request"):
            model.complete(request(180000))
        assert calls == 0 and book.snapshot().reserved == 0
    else:
        with pytest.raises(ModelFailure):
            model.complete(request())
        assert calls == 1 and book.snapshot().reserved == D("0.30176256")
    if case != "oversize":
        events = [
            json.loads(line) for line in (writer.path / "requests.jsonl").read_text().splitlines()
        ]
        assert events[0]["reserved_usd"] == "0.30176256"
        assert sum(event["event"] == "reserved" for event in events) == 1


def test_status_is_read_only(tmp_path: Path, policy: StageBudget) -> None:
    book = StageLedger(tmp_path / policy.ledger, policy)
    before = hashlib.sha256(book.path.read_bytes()).hexdigest()
    config = {
        "stage_policy": policy,
        "paid_execution_enabled": False,
        "hard_limit": str(book.hard_limit),
        "pause_at": str(book.pause_at),
    }
    result = budget_report(tmp_path, config)
    assert result["cross_stage_occupied_usd"] == "0"
    assert hashlib.sha256(book.path.read_bytes()).hexdigest() == before
