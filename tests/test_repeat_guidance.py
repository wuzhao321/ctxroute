"""Repeat feedback must describe real observations and preserve progress gates."""

import difflib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from ctxroute.config import ArmConfig
from ctxroute.domain import Action, Observation, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.progress import ProgressTracker
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
PATCH = (
    "diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n"
    "@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 1\n"
)


def status(tracker: ProgressTracker, step: int) -> dict[str, str | int | float | bool]:
    return tracker.status(step=step, max_steps=50, seconds_left=200, cost_left=Decimal(".65"))


def test_feedback_distinguishes_executed_results_from_rejected_proposals() -> None:
    tracker = ProgressTracker(ArmConfig.load(ROOT / "configs/arms/A1.yaml").progress)
    action = Action("bash", command="python -c 'print(1)'")
    for number in (8, 9):
        assert not tracker.blocks(action)
        tracker.observe(action.command, Observation("1\n", 0), "", step=number)
    warning = status(tracker, 10)
    assert warning["next_priority"] == "edit_needed"
    assert "step 9 with exit code 0" in str(warning["next_action"])
    assert "NOT executed" not in str(warning["next_action"])
    assert not tracker.blocks(action)  # The third identical execution is still allowed.
    tracker.observe(action.command, Observation("1\n", 0), "", step=10)
    assert "now will be rejected" in str(status(tracker, 11)["next_action"])
    assert tracker.blocks(action)
    rejected = status(tracker, 12)
    assert "step 10 with exit code 0" in str(rejected["next_action"])
    assert "NOT executed" in str(rejected["next_action"])
    assert "1 more consecutive blocked repeat(s)" in str(rejected["next_action"])
    assert tracker.executed_commands == 3 and tracker.first_edit_step is None
    assert tracker.blocks(action)
    assert tracker.blocked_repeats == 2
    assert "limit has been reached" in tracker.repeat_feedback()


@pytest.mark.parametrize("exit_code,priority", [(0, "review_and_submit"), (1, "repair_patch")])
def test_repeat_feedback_preserves_current_patch_verification(
    exit_code: int, priority: str
) -> None:
    tracker = ProgressTracker(ArmConfig.load(ROOT / "configs/arms/A1.yaml").progress)
    tracker.observe("edit", Observation("updated", 0), PATCH, step=1)
    for step in (2, 3):
        tracker.observe(
            "pytest -q test_example.py", Observation("check result", exit_code), PATCH, step=step
        )
    current = status(tracker, 4)
    assert current["next_priority"] == priority
    assert "Do not change the diff just to reset repetition" in str(current["next_action"])
    assert tracker.first_edit_step == 1
    if exit_code:
        assert "revise the patch before retesting" in str(current["next_action"])
        assert current["local_test_status"] == "failed"
    else:
        assert "submit if ready" in str(current["next_action"])
        assert current["local_test_status"] == "passed"


def test_scripted_recovery_requires_observed_diff_and_preserves_submit_path(tmp_path: Path) -> None:
    repository = tmp_path / "fixture"
    repository.mkdir()
    source = repository / "example.py"
    original = "def value():\n    return 0\n"
    source.write_text(original)
    (repository / "test_example.py").write_text(
        "from example import value\n\ndef test_value():\n    assert value() == 1\n"
    )

    class EditingSandbox(FakeSandbox):
        def execute(self, handle: str, command: str, timeout: float) -> Observation:
            observed = super().execute(handle, command, timeout)
            if command == "edit-implementation":
                source.write_text("def value():\n    return 1\n")
            elif command == "rewrite-identical-text":
                source.write_text(source.read_text())
            elif command == "pytest -q test_example.py":
                check = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "test_example.py"],
                    cwd=repository,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                observed = Observation(check.stdout + check.stderr, check.returncode)
            diff = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    source.read_text().splitlines(keepends=True),
                    fromfile="a/example.py",
                    tofile="b/example.py",
                )
            )
            self.patch_text = "diff --git a/example.py b/example.py\n" + diff if diff else ""
            return observed

    commands = ["python -c 'print(1)'"] * 4 + [
        "rewrite-identical-text",
        "edit-implementation",
        "pytest -q test_example.py",
    ]
    model = ScriptedModel(
        [json.dumps({"tool": "bash", "command": command}) for command in commands]
        + ['{"tool":"submit"}']
    )
    sandbox = EditingSandbox({"rewrite-identical-text": Observation("patched", 0)})
    writer = RunWriter(tmp_path, "synthetic-repeat-recovery", {"kind": "synthetic_test"})
    result = Engine(ArmConfig.load(ROOT / "configs/arms/A1.yaml"), model, sandbox, writer).run(
        Task("synthetic-repeat", "fixture/repo", "fixture", "Synthetic pipeline validation"), 0
    )
    assert result.state.termination == "submitted" and result.patch == PATCH
    assert len(sandbox.commands) == 6 and sandbox.destroyed
    recovery_prompt = model.requests[4].messages[-1]["content"]
    terminal = json.loads(
        recovery_prompt.rsplit("NEXT ACTION (orchestrator guidance, not tool output)\n", 1)[1]
    )
    assert terminal["priority"] == "edit_needed"
    assert "NOT executed" in terminal["guidance"]
    assert "1 more consecutive blocked repeat(s)" in terminal["guidance"]
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    assert traces[3]["executed"] is False
    assert traces[4]["patch_present_after"] is False
    assert traces[4]["edit_progress"]["first_edit_step"] is None
    assert traces[5]["patch_present_after"] is True
    assert traces[5]["edit_progress"]["first_edit_step"] == 6
    assert traces[6]["local_test_status_after"] == "passed"
    assert traces[-1]["tool"] == "submit"
