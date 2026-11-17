"""Offline prospective captures: synthetic tasks, sandbox, responses, and ledger only."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ctxroute.config import ArmConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.context.manager import ContextManager, Memory
from ctxroute.domain import Action, Observation, RunState, Step, Task
from ctxroute.evalkit.compression_replay import (
    build_forks,
    capture_point,
    classify_sample,
    judge_h1,
    normalized_action,
    verified_snapshot,
)
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]


def synthetic_collector(directory: Path) -> tuple[Path, dict[str, Any]]:
    cfg = ArmConfig.load(ROOT / "configs/arms/A2.yaml")
    cfg = replace(
        cfg,
        context=replace(cfg.context, budget_tokens=5000),
        limits=replace(cfg.limits, output_head_chars=1000, output_tail_chars=1000),
    )
    commands = [f"cat file{i} | head -20" for i in range(15)]
    actions = iter(
        [json.dumps({"tool": "bash", "command": c}) for c in commands] + ['{"tool":"submit"}']
    )

    def transport(payload: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
        content = (
            Memory(open_questions=["Synthetic accepted summary"]).text()
            if payload["model"] == "deepseek-flash"
            else next(actions)
        )
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
            },
        }

    writer = RunWriter(
        directory,
        "synthetic-collector",
        {
            "kind": "synthetic_test",
            "arm": "A2",
            "source_tree_sha256": "synthetic-source",
        },
    )
    ledger = Ledger(
        directory / "synthetic-ledger.sqlite",
        hard_limit=Decimal(10),
        pause_at=Decimal(8),
        enabled=True,
    )
    model = DeepSeekModel(
        key="synthetic-secret",
        prices=json.loads((ROOT / "data/prices/2026-11-17.json").read_text()),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=32000,
        thinking="disabled",
        transport=transport,
    )
    sandbox = FakeSandbox({c: Observation("x" * 1500, 0) for c in commands})
    task = Task("synthetic-fork", "fake/repo", "fixture", "Synthetic task only")
    writer.snapshot("task", asdict(task))
    writer.snapshot("config", asdict(cfg))
    result = Engine(cfg, model, sandbox, writer, model_attempts=1).run(task, 20261022)
    assert result.state.termination == "submitted"
    assert ledger.snapshot().spent == ledger.snapshot().reserved == 0
    controls = {
        "manifest": {"source_tree_sha256": "synthetic-source", "arm": "A2"},
        "payload": {
            "model": "deepseek-v4-pro",
            "max_tokens": cfg.limits.max_output_tokens,
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "stream": False,
            "response_format": {"type": "json_object"},
        },
        "input_guard_tokens": 32000,
        "summary_model": "deepseek-flash",
        "input_hashes": {
            name: hashlib.sha256((writer.path / name).read_bytes()).hexdigest()
            for name in ("task.json", "config.json")
        },
    }
    return writer.path, controls


def test_prospective_capture_builds_six_context_only_forks(tmp_path: Path) -> None:
    run, controls = synthetic_collector(tmp_path)
    with pytest.raises(ValueError, match="explicitly synthetic"):
        build_forks(run, controls)
    forks = build_forks(run, controls, allow_synthetic=True)
    assert len(forks) == 6
    assert len({row["point"] for row in forks}) == 3
    for full, compressed in zip(forks[::2], forks[1::2], strict=True):
        left, right = deepcopy(full["payload"]), deepcopy(compressed["payload"])
        a, b = left.pop("messages"), right.pop("messages")
        assert left == right and a[0] == b[0] and a[1] != b[1]
        assert full["timeout_seconds"] == compressed["timeout_seconds"]
        assert full["previous_executed_actions"] == compressed["previous_executed_actions"]
        assert not full["execution_authorized"] and full["kind"] == "synthetic_test"


def test_full_history_retains_common_evidence_without_mutating_context(tmp_path: Path) -> None:
    cfg = ArmConfig.load(ROOT / "configs/arms/A2.yaml")
    context = ContextManager(
        "system",
        "task",
        cfg.context,
        ArtifactStore(
            tmp_path / "artifacts",
            max_bytes=cfg.limits.artifact_bytes,
            recall_chars=cfg.limits.recall_chars,
        ),
    )
    context.action_records = True
    context.fixed = {"confirmed_observations": "same fixed observation"}
    context.progress = {"next_action": "edit source", "seconds_left": 123.0}
    context.memory = Memory(open_questions=["accepted compressed memory"])
    history = [
        Step(1, "strong", Action("bash", command="cat a"), "old output", 0),
        Step(
            2,
            "strong",
            Action("bash", command="cat a"),
            "rejected",
            error="repeated_action_blocked",
        ),
        Step(3, "strong", Action("bash", command="test a"), "failed but executed", 1),
    ]
    context.recent = history[-1:]
    before = context.messages()
    state = RunState(
        "fixture", Task("fixture", "fake/repo", "fixture", "task"), "A2", 1, step=4, history=history
    )
    record = capture_point(context, state)
    assert context.messages() == before and context.recent == history[-1:]
    full, compressed = record["full_history_messages"][1]["content"], before[1]["content"]
    for common in ("same fixed observation", "edit source", "123.0"):
        assert common in full and common in compressed
    assert "old output" in full and "old output" not in compressed
    assert "accepted compressed memory" not in full and "accepted compressed memory" in compressed
    assert [a["step"] for a in record["previous_executed_actions"]] == [1, 3]


@pytest.mark.parametrize(
    "change",
    [
        "point_hash",
        "missing_point",
        "summary_truncated",
        "summary_redacted",
        "payload_control",
        "unknown_usage",
        "fewer_points",
        "summary_model",
    ],
)
def test_incomplete_or_changed_collector_evidence_blocks_all_forks(
    tmp_path: Path, change: str
) -> None:
    run, controls = synthetic_collector(tmp_path)
    life = [json.loads(x) for x in (run / "lifecycle.jsonl").read_text().splitlines()]
    point = next(e for e in life if e["event"] == "compression_point")
    if change == "point_hash":
        (run / point["snapshot"]["path"]).write_text("{}")
    elif change == "missing_point":
        life.remove(point)
    elif change in {"summary_truncated", "summary_redacted"}:
        requests = [json.loads(x) for x in (run / "requests.jsonl").read_text().splitlines()]
        req = next(
            e for e in requests if e["purpose"] == "compression" and e["step"] == point["step"]
        )
        event = next(
            e
            for e in life
            if e["event"] == "response_audit" and e["request_id"] == req["request_id"]
        )
        path = run / "response_audits" / (req["request_id"] + ".json")
        value = json.loads(path.read_text())
        value["content"]["capture_complete" if change == "summary_truncated" else "redacted"] = (
            change == "summary_redacted"
        )
        path.write_text(json.dumps(value))
        event["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif change == "payload_control":
        controls["payload"]["temperature"] = 0.5
    elif change == "summary_model":
        controls["summary_model"] = "different-model"
    elif change == "unknown_usage":
        path = run / "requests.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines()]
        rows = [e for e in rows if e.get("event") != "usage" or e["step"] != 1]
        path.write_text("".join(json.dumps(e) + "\n" for e in rows))
    else:
        life = [e for e in life if e["event"] != "compression" or not e.get("accepted")]
    (run / "lifecycle.jsonl").write_text("".join(json.dumps(e) + "\n" for e in life))
    with pytest.raises(ValueError):
        build_forks(run, controls, allow_synthetic=True)


def test_private_point_redaction_permissions_and_path_rejection(tmp_path: Path) -> None:
    writer = RunWriter(tmp_path, "private", {"kind": "synthetic_test"})
    writer.protect_secret("synthetic-secret")
    ref = writer.private_snapshot(
        "compression_points", "step-0008", {"messages": "synthetic-secret"}
    )
    path = writer.path / ref["path"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert "synthetic-secret" not in path.read_text() and not ref["replayable"]
    with pytest.raises(ValueError, match="redacted"):
        verified_snapshot(writer.path, ref, "compression_points")
    with pytest.raises(ValueError, match="reference"):
        verified_snapshot(writer.path, {"path": "../outside.json"}, "compression_points")


def test_repeat_identity_and_missing_samples_follow_frozen_h1_rule() -> None:
    previous = [{"step": 1, "normalized": normalized_action(Action("bash", command="cat a"))}]
    assert classify_sample('{"tool":"bash","command":"cat a"}', previous)["repeated"]
    assert not classify_sample('{"tool":"bash","command":"cat  a"}', previous)["repeated"]
    assert not classify_sample('{"tool":"bash","command":"cat a"}}', previous)["valid"]
    samples = [
        {"point": p, "condition": c, "valid": True, "repeated": c == "compressed_history" and p < 3}
        for p in (1, 2, 3)
        for c in ("full_history", "compressed_history")
    ]
    assert judge_h1(samples, {1: True, 2: True}) == "支持"
    assert judge_h1(samples, {1: True, 2: None}) == "无法判断"
    assert judge_h1(samples[:-1], {1: True, 2: True}) == "无法判断"
    for row in samples:
        row["repeated"] = False
    assert judge_h1(samples, {}) == "证伪"


def test_private_evidence_cannot_be_redirected_outside_the_run(tmp_path: Path) -> None:
    writer = RunWriter(tmp_path, "private-symlink", {"kind": "synthetic_test"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (writer.path / "compression_points").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        writer.private_snapshot("compression_points", "step-0008", {"value": "fixture"})
    assert list(outside.iterdir()) == []
