"""Observed invocation failures and strict format recovery, without network calls."""

import json
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_source_decision import SourceSandbox
from test_target_action import config, selection

from ctxroute.config import ArmConfig, ProgressConfig
from ctxroute.domain import Action, Observation, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.failure_recovery import FailureRecovery
from ctxroute.orchestrator.repair_policy import RepairPolicy
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.writer import RunWriter

SCRIPT = "def combine(a, b, c, d): return a + b + c + d\nprint(combine(1, 2, 3, 4, 5))"
BAD_CALL = shlex.join(["python", "-c", SCRIPT])
GOOD_CALL = shlex.join(["python", "-c", SCRIPT.replace("1, 2, 3, 4, 5", "1, 2, 3, 4")])
TRACEBACK = (
    'Traceback (most recent call last):\n  File "<string>", line 2, in <module>\n'
    "TypeError: combine() takes 4 positional arguments but 5 were given\n"
)
TASK = Task("synthetic-recovery", "toy/repo", "base", "Handle None, retain normalized strings")


def recovery_config() -> ArmConfig:
    cfg = config()
    return replace(
        cfg,
        progress=replace(cfg.progress, source_edit_strategy=True, failure_recovery_strategy=True),
    )


class DiagnosticSandbox(SourceSandbox):
    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        if command in {BAD_CALL, GOOD_CALL}:
            self.commands.append(command)
            result = subprocess.run(
                [sys.executable, *shlex.split(command)[1:]],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return Observation(result.stdout + result.stderr, result.returncode)
        return super().execute(handle, command, timeout)


def test_failed_call_replay_blocked_then_unexecuted_correction_and_full_repair(
    tmp_path: Path,
) -> None:
    corrected = json.dumps(Action("bash", command=GOOD_CALL).payload())
    model = ScriptedModel(
        [
            json.dumps(Action("bash", command=c).payload())
            for c in ["add-target", BAD_CALL, BAD_CALL]
        ]
        + [corrected + "]", corrected]
        + [json.dumps(Action("bash", command="good-source").payload()), '{"tool":"submit"}']
    )
    sandbox = DiagnosticSandbox(tmp_path / "repo")
    writer = RunWriter(tmp_path, "recovery-cycle", {"is_benchmark": False})
    cfg = recovery_config()
    result = Engine(cfg, model, sandbox, writer, model_attempts=1).run(TASK, 1)
    assert result.state.termination == "submitted"
    assert sandbox.commands.count(BAD_CALL) == sandbox.commands.count(GOOD_CALL) == 1
    assert sandbox.commands.count("good-source") == 1
    traces = [json.loads(x) for x in (writer.path / "traces.jsonl").read_text().splitlines()]
    blocked = next(t for t in traces if t.get("error_type") == "invocation_replay_blocked")
    assert blocked["executed"] is False and blocked["exit_code"] is None
    assert blocked["failure_recovery"]["invocation_failure"]["exit_code"] == 1
    invalid = next(t for t in traces if t.get("error_type") == "format_error")
    assert invalid["tool"] is None and invalid["executed"] is False
    assert invalid["failure_recovery"]["format_error"]["unexecuted_candidate"] == {
        "tool": "bash",
        "command": GOOD_CALL,
    }
    feedback = model.requests[4].messages[-1]["content"]
    assert feedback.count("REJECTED RESPONSE DATA") == 1
    assert json.dumps(GOOD_CALL) in feedback and "FORMAT CORRECTION FIRST" in feedback
    assert "REJECTED ACTION step=4" in feedback
    assert all(r.max_output_tokens == cfg.limits.max_output_tokens for r in model.requests)
    assert len(model.requests) == 7
    state = json.loads((writer.path / "state.json").read_text())
    assert state["repair_policy"]["validation_ready_to_submit"]
    assert state["repair_policy"]["unresolved_failures"] == []
    assert [
        (t["verification_category"], t["exit_code"])
        for t in traces
        if t.get("origin") == "automatic_verification"
    ] == [("target_baseline", 1), ("target", 0), ("regression", 0)]


def test_two_invalid_responses_stop_without_executing_a_candidate(tmp_path: Path) -> None:
    invalid = json.dumps(Action("bash", command="touch forbidden").payload()) + "]"
    model = ScriptedModel([invalid, invalid])
    sandbox = FakeSandbox()
    result = Engine(recovery_config(), model, sandbox, RunWriter(tmp_path, "invalid", {})).run(
        TASK, 1
    )
    assert result.state.termination == "format_failure"
    assert not sandbox.commands and len(model.requests) == 2
    assert result.state.history[0].action is None
    assert "touch forbidden" in model.requests[1].messages[-1]["content"]


def test_format_correction_cannot_switch_to_an_executed_action(tmp_path: Path) -> None:
    first = json.dumps(Action("bash", command="cat core.py").payload())
    candidate = json.dumps(Action("bash", command="echo pending").payload()) + "]"
    model = ScriptedModel([first, candidate, first])
    sandbox = FakeSandbox()
    result = Engine(
        recovery_config(), model, sandbox, RunWriter(tmp_path, "different-action", {})
    ).run(TASK, 1)
    assert result.state.termination == "format_failure" and sandbox.commands == ["cat core.py"]
    assert "preserve the unexecuted candidate" in result.state.history[-1].observation


def test_non_diagnostic_text_and_changed_state_do_not_retain_invocation_bans() -> None:
    recovery = FailureRecovery(4000)
    for command, observation, after in [
        ("cat traceback.txt", Observation(TRACEBACK, 1), "patch"),
        (BAD_CALL, Observation(TRACEBACK, 0), "patch"),
        (BAD_CALL, Observation(TRACEBACK, 1, output_limited=True), "patch"),
        (BAD_CALL, Observation("TypeError: invalid input", 1), "patch"),
        (BAD_CALL, Observation(TRACEBACK, 1), "changed-patch"),
    ]:
        recovery.observe(command, observation, "patch", after, 1, model_action=True)
        assert recovery.invocation is None
    recovery.observe(BAD_CALL, Observation(TRACEBACK, 1), "patch", "patch", 2, model_action=True)
    recovery.observe(
        "cat core.py", Observation("source", 0), "patch", "patch", 3, model_action=True
    )
    assert recovery.blocks(Action("bash", command=BAD_CALL), "patch")
    assert not recovery.blocks(Action("bash", command=BAD_CALL), "edited")
    # An executed opaque action could change untracked files/environment even with the same diff.
    recovery.observe(
        "install-dependency", Observation("done", 0), "patch", "patch", 4, model_action=True
    )
    assert not recovery.blocks(Action("bash", command=BAD_CALL), "patch")


def test_rejected_proposal_consumes_diagnostics_without_test_evidence() -> None:
    policy = RepairPolicy(recovery_config().progress)
    policy.install_plan(selection(), "test-only")
    job = policy.queue.pop(0)
    policy.observe(
        job.command,
        Observation("E AssertionError\n1 failed", 1),
        "test-only",
        "test-only",
        1,
        job=job,
    )
    before = {k: v.record() for k, v in policy.unresolved.items()}
    for step in range(2, 6):
        policy.reject_action(BAD_CALL, step)
    assert policy.status(100, 10)["incomplete_stop"] and not policy.can_submit
    assert {k: v.record() for k, v in policy.unresolved.items()} == before
    assert policy.source is not None and policy.source.last_diagnostic["executed"] is False


def test_unrecognized_suffix_and_long_response_never_become_executable_candidates() -> None:
    for raw in (
        '{"tool":"submit"}{"tool":"submit"}',
        '[{"tool":"submit"}]',
        '{"tool":"bash","command":"x","extra":true}]',
        json.dumps(Action("bash", command="x" * 5000).payload()) + "]",
    ):
        recovery = FailureRecovery(1000)
        recovery.reject_format(raw, "invalid", recall_enabled=False)
        assert recovery.candidate is None and recovery.format_error is not None
        assert len(recovery.format_error["rejected_response_excerpt"]) <= 1000

    recovery = FailureRecovery(1000)
    original = Action("bash", command="echo original")
    recovery.reject_format(json.dumps(original.payload()) + "]", "invalid", recall_enabled=False)
    recovery.reject_format('{"tool":"submit"}]', "different", recall_enabled=False)
    assert recovery.candidate == original
    with pytest.raises(ValueError, match="preserve"):
        recovery.validate_correction(Action("submit"))


def test_recovery_is_opt_in_and_requires_source_policy() -> None:
    assert not ProgressConfig().failure_recovery_strategy
    with pytest.raises(ValueError, match="requires source"):
        ProgressConfig(failure_recovery_strategy=True)
    with pytest.raises(ValueError, match="must be boolean"):
        ProgressConfig(failure_recovery_strategy=1)  # type: ignore[arg-type]
