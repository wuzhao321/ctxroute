"""Test result attribution must not depend on the routing classifier."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ctxroute.config import ArmConfig, ProgressConfig
from ctxroute.domain import Observation, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.progress import ProgressTracker
from ctxroute.orchestrator.verification import observe_check
from ctxroute.router.rules import classify
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.writer import RunWriter


def test_read_then_test_is_recorded_without_changing_routing() -> None:
    tracker = ProgressTracker(ProgressConfig())
    tracker.observe("edit", Observation("", 0), "+fix")
    command = "git diff -- sample.py && python -m pytest tests/test_sample.py -q"
    assert classify(command) == "edit_or_unknown"
    tracker.observe(command, Observation("5 passed in 0.01s", 0), "+fix", step=2)
    assert tracker.verification == "passed"
    check = tracker.last_check
    assert check and check.step == 2 and check.targets == ("tests/test_sample.py",)
    assert check.test_exit_code == 0 and check.patch_sha256_before == check.patch_sha256_after


def test_failure_remains_until_same_scope_or_explicit_selector_superset_passes() -> None:
    tracker = ProgressTracker(ProgressConfig(post_edit_checkpoint=True))
    tracker.observe("edit", Observation("", 0), "+fix")
    tracker.observe("pytest tests/a.py", Observation("1 failed", 1), "+fix", step=2)
    tracker.observe("pytest tests/b.py", Observation("1 passed", 0), "+fix", step=3)
    assert tracker.verification == "failed" and tracker.test_step == 2
    tracker.observe("cat tests/a.py", Observation("source", 0), "+fix")
    assert tracker.verification == "failed"
    tracker.observe("pytest tests/a.py tests/b.py", Observation("2 passed", 0), "+fix")
    assert tracker.verification == "passed"


@pytest.mark.parametrize(
    "command", ["pytest tests/a.py -k other", "cd /other && pytest tests/a.py"]
)
def test_changed_selection_or_directory_does_not_clear_failure(command: str) -> None:
    tracker = ProgressTracker(ProgressConfig())
    tracker.observe("edit", Observation("", 0), "+fix")
    tracker.observe("pytest tests/a.py", Observation("1 failed", 1), "+fix")
    tracker.observe(command, Observation("1 passed", 0), "+fix")
    assert tracker.verification == "failed"


def test_compound_reproduction_failure_does_not_become_a_pytest_pass() -> None:
    tracker = ProgressTracker(ProgressConfig())
    tracker.observe("edit", Observation("", 0), "+fix")
    command = 'out=$(python -m pylint -E r.py a 2>&1); status=$?; echo "$out"; exit $status'
    tracker.observe(command, Observation("r.py:1:0: E0611: No name 'b' in module 'a'", 1), "+fix")
    check = tracker.last_check
    assert check and check.test_exit_code is None and check.exit_code == 1
    assert check.attribution == "diagnostic_in_compound_command"
    tracker.observe("pytest tests/a.py", Observation("5 passed", 0), "+fix")
    assert tracker.verification == "failed"
    tracker.observe("edit", Observation("", 0), "+new fix")
    assert tracker.verification == "not_run" and not tracker.verified_patch


@pytest.mark.parametrize(
    ("command", "output", "code"),
    [
        ("false && pytest tests/a.py", "", 1),
        ("cat missing.py && pytest tests/a.py", "no such file", 1),
        ("pytest tests/a.py || true", "1 failed", 0),
        ("pytest tests/a.py | cat", "1 failed", 0),
        ("pytest tests/a.py; echo ok", "1 failed", 0),
        ("pytest tests/a.py; echo ok", "1 passed", 0),
        ("pytest --collect-only tests/a.py", "2 tests collected", 0),
    ],
)
def test_skipped_or_masked_test_never_earns_pass(command: str, output: str, code: int) -> None:
    evidence = observe_check(command, Observation(output, code), "+fix", "+fix", 1)
    assert evidence is None or evidence.outcome != "passed"
    if "1 failed" in output:
        assert evidence and evidence.outcome == "failed" and evidence.test_exit_code is None


def test_incomplete_or_changed_diff_cannot_verify_new_patch() -> None:
    tracker = ProgressTracker(ProgressConfig())
    tracker.observe("edit", Observation("", 0), "+fix")
    tracker.observe("pytest tests", Observation("1 passed", 0, output_limited=True), "+fix")
    assert tracker.verification == "not_run"
    tracker.observe("pytest tests", Observation("1 passed", 0), "+different")
    assert tracker.verification == "not_run" and tracker.last_check
    assert tracker.last_check.patch_sha256_before != tracker.last_check.patch_sha256_after


def test_engine_compound_failure_is_persisted_and_partial_pass_does_not_erase_it(
    tmp_path: Path,
) -> None:
    class Sandbox(FakeSandbox):
        def execute(self, handle: str, command: str, timeout: float) -> Observation:
            super().execute(handle, command, timeout)
            return (
                Observation("1 failed", 1)
                if "tests/a.py" in command
                else Observation("1 passed", 0)
            )

        def patch(self, handle: str) -> str:
            return "+fix"

    cfg = ArmConfig.load(
        Path(__file__).resolve().parents[1] / "configs/candidates/A1-baseline-16k.json"
    )
    commands = ["edit", "git diff && pytest tests/a.py", "pytest tests/b.py"]
    model = ScriptedModel(
        [json.dumps({"tool": "bash", "command": c}) for c in commands] + ['{"tool":"submit"}']
    )
    writer = RunWriter(tmp_path, "compound-test", {"is_benchmark": False})
    result = Engine(cfg, model, Sandbox(), writer).run(Task("toy", "repo", "base", "fix"), 1)
    assert result.state.termination == "submitted" and result.state.accounted_cost == Decimal(0)
    saved = json.loads((writer.path / "state.json").read_text())
    failure = json.loads(saved["fixed_memory"]["last_failed_test"])
    assert failure["step"] == 2 and failure["exit_code"] == 1
    assert failure["check_evidence"]["targets"] == ["tests/a.py"]
    assert saved["verification"]["local_test_status"] == "failed"
