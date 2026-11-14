"""Repair packets retain strict actions, truthful evidence and the complete repair gate."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_source_decision import SourceSandbox
from test_target_action import config, selection

from ctxroute.config import ProgressConfig
from ctxroute.domain import ModelRequest, ModelResponse, Observation, Task
from ctxroute.models.base import ModelFailure
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.public_behavior import TARGET_PATH
from ctxroute.orchestrator.repair_evidence import RepairEvidence
from ctxroute.orchestrator.repair_policy import RepairPolicy
from ctxroute.tracing.writer import RunWriter


def test_packet_links_exact_public_assertion_error_retest_and_diff_without_diagnosis() -> None:
    evidence = RepairEvidence(4000, public_scaffold=True)
    selector = TARGET_PATH + "::test_public_import_side_case"
    command = "python -m pytest -vv --showlocals --tb=short " + selector
    output = (
        "E   assert 2 == 0\n"
        "   'message': \"No name 'b' in module 'a'\",\n"
        "   'path': 'r.py',\nFAILED " + selector + " - AssertionError\n1 failed"
    )
    evidence.observe(
        command, Observation(output, 1), "patch", 10, unchanged_model_action=False, failed=True
    )
    failures = [
        {
            "step": 10,
            "targets": [selector],
            "command": command,
            "exit_code": 1,
            "patch_sha256_after": hashlib.sha256(b"patch").hexdigest(),
        }
    ]
    packet = evidence.packet("patch", failures, {})
    assert packet["focus"]["same_diff"] and packet["focus"]["exit_code"] == 1
    assert "No name 'b'" in packet["focus"]["error_excerpt"]
    assert 'run_pylint(tmp_path, "-E", "r", "a")' in json.dumps(packet).replace('\\"', '"')
    assert "assert code == 0" in packet["focus"]["public_contract"][0]["calls_and_assertions"]
    assert packet["focus"]["retest"] == command
    assert not evidence.packet("changed", failures, {})["focus"]["same_diff"]
    assert packet["meaning"] == "public_observations_not_verified_root_cause"
    diagnostic = {
        **failures[0],
        "step": 12,
        "targets": [],
        "exit_code": 0,
        "attribution": "reported_child_exit",
    }
    combined = evidence.packet("patch", failures + [diagnostic], {})
    assert combined["focus"]["step"] == 10 and combined["focus"]["public_contract"]
    assert combined["later_diagnostic"]["step"] == 12
    evidence.observe(
        "reproduce",
        Observation('returncode: 2\n  "message": "import failed"', 0),
        "patch",
        12,
        unchanged_model_action=True,
        failed=False,
    )
    quoted = evidence.packet("patch", failures, {})
    assert quoted["focus"]["exit_code"] == 1 and quoted["open_failure_steps"] == [10]
    assert quoted["later_diagnostic"] is None


def test_source_locations_are_observed_not_guessed_and_test_reads_do_not_count() -> None:
    evidence = RepairEvidence(4000)
    for step, path, size, unchanged in [
        (1, "core.py", 6000, True),
        (2, "tests/test_target.py", 200, True),
        (3, "core.py", 300, False),
        (4, "core.py", 300, True),
    ]:
        evidence.observe(
            f"# question: inspect path\nsed -n '1,30p' {path}",
            Observation("x" * size, 0),
            "patch",
            step,
            unchanged_model_action=unchanged,
            failed=False,
        )
    assert [r["step"] for r in evidence.reads] == [1, 4]
    assert [r["fully_visible"] for r in evidence.reads] == [False, True]


def test_final_decision_packet_keeps_original_limit_and_never_forces_an_edit() -> None:
    p = RepairPolicy(
        replace(config().progress, source_edit_strategy=True, evidence_action_strategy=True)
    )
    p.install_plan(selection(), "test-only")
    job = p.queue.pop(0)
    p.observe(
        job.command,
        Observation("E AssertionError\n1 failed", 1),
        "test-only",
        "test-only",
        2,
        job=job,
    )
    for step in range(3, 6):
        p.observe(
            "# question: inspect specific branch\nsed -n '1,20p' core.py",
            Observation("code", 0),
            "test-only",
            "test-only",
            step,
        )
    status = p.status(100, 10)
    assert "FINAL DECISION" in status["next_action"] and "# question:" in status["next_action"]
    assert "Never fabricate" in status["next_action"] and not status["incomplete_stop"]
    p.observe("cat tests/test_target.py", Observation("assertions", 0), "test-only", "test-only", 6)
    assert p.status(100, 10)["incomplete_stop"] and not p.can_submit


@pytest.mark.parametrize("invalid_first", [False, True])
def test_red_edit_failed_fix_repair_tests_submit_and_no_duplicate_execution(
    tmp_path: Path, invalid_first: bool
) -> None:
    actions = [
        json.dumps({"tool": "bash", "command": c})
        for c in ["add-target", "bad-source", "good-source"]
    ]
    if invalid_first:
        actions.insert(0, '{"tool":"bash","command":"add-target"}]')
    model = ScriptedModel(actions + ['{"tool":"submit"}'])
    cfg = replace(
        config(),
        progress=replace(
            config().progress, source_edit_strategy=True, evidence_action_strategy=True
        ),
    )
    writer = RunWriter(tmp_path, "evidence-cycle", {"is_benchmark": False})
    sandbox = SourceSandbox(tmp_path / "repo")
    result = Engine(cfg, model, sandbox, writer, model_attempts=1).run(
        Task("toy", "toy/repo", "base", "Handle None"), 1
    )
    assert result.state.termination == "submitted"
    assert len(model.requests) == 4 + invalid_first
    assert all(sandbox.commands.count(c) == 1 for c in ["add-target", "bad-source", "good-source"])
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    assert [
        (t["verification_category"], t["exit_code"])
        for t in traces
        if t.get("origin") == "automatic_verification"
    ] == [("target_baseline", 1), ("target", 1), ("target", 0), ("regression", 0)]
    repairs = [r for r in model.requests if "PUBLIC EVIDENCE PACKET" in r.messages[-1]["content"]]
    assert repairs and all(
        r.messages[-1]["content"].count("PUBLIC EVIDENCE PACKET") == 1 for r in repairs
    )
    assert "PUBLIC EVIDENCE PACKET" not in model.requests[-1].messages[-1]["content"]


def test_model_exhaustion_does_not_execute_or_retry_an_unreceived_action(tmp_path: Path) -> None:
    class Exhausted:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            raise ModelFailure("output_exhausted")

    cfg = replace(
        config(),
        progress=replace(
            config().progress, source_edit_strategy=True, evidence_action_strategy=True
        ),
    )
    model = Exhausted()
    sandbox = SourceSandbox(tmp_path / "repo")
    result = Engine(
        cfg, model, sandbox, RunWriter(tmp_path, "exhausted", {}), model_attempts=1
    ).run(Task("toy", "toy/repo", "base", "issue"), 1)
    assert result.state.termination == "model_failure" and model.calls == 1
    assert not sandbox.commands and not result.patch


def test_evidence_policy_is_opt_in_and_requires_source_policy() -> None:
    assert not ProgressConfig().evidence_action_strategy
    with pytest.raises(ValueError, match="requires"):
        ProgressConfig(evidence_action_strategy=True)
