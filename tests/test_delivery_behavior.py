"""Bounded delivery evidence, suspended clocks and public behavior contracts."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from test_stream_receipt import _event, _wire_model  # pyright: ignore[reportPrivateUsage]

from ctxroute.config import ContextConfig, ProgressConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.context.manager import ContextManager
from ctxroute.domain import Action, Observation, Step
from ctxroute.models.base import ModelFailure
from ctxroute.models.deadline import suspension_aware_clock, wall_timeout
from ctxroute.models.output_audit import TextAudit
from ctxroute.orchestrator.public_behavior import PUBLIC_TARGET, TARGET_PATH, contract_sha
from ctxroute.orchestrator.public_tests import select_tests
from ctxroute.orchestrator.repair_policy import RepairPolicy
from ctxroute.orchestrator.target_contract import TargetContract


def test_output_audit_detects_whitespace_repetition_bounds_and_redacts_key() -> None:
    a = TextAudit()
    a.add("secret-")
    a.add("key" + " " * 70000)
    record = a.record("secret-key")
    assert record["chars"] == 70010 and record["whitespace_chars"] == 70000
    assert record["sha256"] == hashlib.sha256(("secret-key" + " " * 70000).encode()).hexdigest()
    assert not record["capture_complete"] and record["captured_chars"] == 65536
    assert "secret-key" not in record["text"]
    assert record["most_repeated_64char_block_count"] > 900


def test_length_response_is_preserved_but_never_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = '{"tool":"bash","command":"unfinished'
    body = (
        _event({"reasoning_content": "short reasoning"})
        + _event({"content": content})
        + _event({}, finish="length", usage=True)
        + b"data: [DONE]\n\n"
    )
    model, request, ledger, writer, calls = _wire_model(tmp_path, monkeypatch, body)
    with pytest.raises(ModelFailure):
        model.complete(request)
    assert len(calls) == 1 and ledger.snapshot().spent > 0 and ledger.snapshot().reserved == 0
    saved = json.loads(next((writer.path / "response_audits").glob("*.json")).read_text())
    assert saved["content"]["text"] == content and saved["content"]["capture_complete"]
    assert saved["reasoning"]["chars"] == 15


def test_partial_response_without_receipt_keeps_unknown_charge_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _event({"reasoning_content": "partial thinking only"})
    model, request, ledger, writer, calls = _wire_model(tmp_path, monkeypatch, body)
    with pytest.raises(ModelFailure):
        model.complete(request)
    assert len(calls) == 1 and ledger.snapshot().spent == 0 and ledger.snapshot().reserved > 0
    saved = json.loads(next((writer.path / "response_audits").glob("*.json")).read_text())
    assert saved["reasoning"]["chars"] == 21 and saved["content"]["chars"] == 0


def test_suspension_counts_toward_deadline_and_backward_clock_does_not_grant_time() -> None:
    mono, wall = [10.0], [100.0]
    clock = suspension_aware_clock(lambda: mono[0], lambda: wall[0])
    mono[0] += 2
    wall[0] += 1000
    assert clock() == 1010
    wall[0] = 50
    assert clock() == 1010


def test_wall_timeout_cancels_wait_after_simulated_host_suspend() -> None:
    async def scenario() -> None:
        now = [0.0]
        with pytest.raises(TimeoutError):
            async with wall_timeout(100, lambda: now[0]):
                now[0] = 1001
                await asyncio.sleep(10)

    asyncio.run(scenario())


def test_public_contract_accepts_passing_controls_but_requires_real_failure() -> None:
    p = TargetContract(allow_controls=True)
    p.select(
        {
            "target": ["target", "control"],
            "target_contract": {
                "test_only_valid": True,
                "implementation_changed": False,
                "file_sha256": {"fixture": "fixed"},
            },
        }
    )
    p.observe(("control",), Observation("2 passed", 0), "tests", "tests", 1)
    assert not p.red_proven
    p.observe(("target",), Observation("AssertionError\n1 failed", 1), "tests", "tests", 2)
    assert p.red_proven


def test_efficient_prompt_retains_every_action_and_observation(tmp_path: Path) -> None:
    c = ContextManager(
        "system",
        "public task",
        ContextConfig(32000, 6, 0.7, 0.9, "none"),
        ArtifactStore(tmp_path, max_bytes=10000, recall_chars=1000),
    )
    c.action_records = True
    c.progress = {
        "local_test_evidence": "x" * 5000,
        "validation_policy": json.dumps(
            {
                "validation_ready_to_submit": False,
                "unresolved_failures": [{"step": 1, "duplicated_output": "x" * 3000}],
                "target_contract": {"baseline_assertion_proven": True},
            }
        ),
    }
    for i in range(1, 4):
        c.append(Step(i, "strong", Action("bash", command=f"echo action{i}"), f"observed{i}", 0))
    old = c.serialized()
    c.efficient_actions = True
    new = c.serialized()
    assert len(new) < len(old)
    for i in range(1, 4):
        assert f"action{i}" in new and f"observed{i}" in new
    assert "open_failure_steps" in new and "baseline_proven" in new


def test_public_fixture_is_external_and_contract_cannot_be_weakened() -> None:
    assert "expand_modules" not in PUBLIC_TARGET and "module_names" not in PUBLIC_TARGET
    assert '"-E", "r", "a"' in PUBLIC_TARGET and "undefined-variable" in PUBLIC_TARGET
    cfg = ProgressConfig(
        regression_strategy=True, target_action_strategy=True, delivery_behavior_strategy=True
    )
    p = RepairPolicy(cfg)
    plan = select_tests({}, {TARGET_PATH: PUBLIC_TARGET}, [TARGET_PATH], target_contract=True)
    assert plan["target_contract"]["file_sha256"] == {TARGET_PATH: contract_sha()}
    p.install_plan(json.dumps(plan), "test-only")
    assert len(p.queue) == 4 and all(j.category == "target_baseline" for j in p.queue)
    plan["target_contract"]["file_sha256"][TARGET_PATH] = "tampered"
    p.install_plan(json.dumps(plan), "weakened")
    assert not p.queue and not p.can_submit
