"""First-edit control uses observed diffs, without changing action validation."""

import difflib
import json
import os
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ctxroute.config import ArmConfig, ProgressConfig
from ctxroute.domain import ModelRequest, ModelResponse, Observation, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.progress import ProgressTracker
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
TASK = Task("first-edit-fixture", "fake/repo", "fixture", "value() should return 2")
READ = "sed -n '1,10p' module.py"
DIAGNOSE = "python -c 'from module import value; print(value())'"
EDIT = (
    'python -c \'from pathlib import Path; p=Path("module.py"); '
    'p.write_text(p.read_text().replace("return 1", "return 2"))\''
)


def configured(enabled: bool = True) -> ProgressConfig:
    return ProgressConfig(first_edit_checkpoint=enabled, edit_prompt_after=2)


def status(tracker: ProgressTracker) -> dict[str, str | int | float | bool]:
    return tracker.status(step=10, max_steps=50, seconds_left=100, cost_left=Decimal("0.1"))


@pytest.mark.parametrize(
    ("source", "diagnostic", "code", "priority"),
    [
        ("ls", DIAGNOSE, 0, "locate_source"),
        (READ, "rg value module.py", 0, "diagnose_before_edit"),
        (READ, DIAGNOSE, 1, "diagnose_before_edit"),
    ],
)
def test_checkpoint_requires_both_observable_prerequisites(
    source: str, diagnostic: str, code: int, priority: str
) -> None:
    tracker = ProgressTracker(configured())
    tracker.observe(source, Observation("source observation", 0), "", step=1)
    tracker.observe(diagnostic, Observation("diagnostic observation", code), "", step=2)
    assert tracker.edit_checkpoint_step is None and not tracker.edit_checkpoint_exhausted
    assert status(tracker)["next_priority"] == priority


def test_checkpoint_rendering_does_not_consume_allowance_or_reset_it_on_new_reads() -> None:
    tracker = ProgressTracker(configured())
    tracker.observe(READ, Observation("def value(): return 1", 0), "", step=3)
    tracker.observe(DIAGNOSE, Observation("1", 0), "", step=4)
    assert tracker.edit_checkpoint_step == 4
    first = status(tracker)
    assert first == status(tracker)  # Engine renders status twice around compression.
    assert first["next_priority"] == "make_first_edit" and tracker.no_edit_commands == 0
    tracker.observe("cat other.py", Observation("another source", 0), "", step=5)
    assert tracker.no_edit_commands == 1 and not tracker.edit_checkpoint_exhausted
    # An apparent edit which returns successfully but leaves no diff is not an edit.
    tracker.observe(EDIT, Observation("nothing changed", 0), "", step=6)
    assert tracker.edit_checkpoint_exhausted and tracker.first_edit_step is None
    assert tracker.edit_checkpoint_step == 4


def test_first_edit_uses_actual_step_and_remains_recorded_after_revert() -> None:
    tracker = ProgressTracker(configured())
    tracker.observe(READ, Observation("source", 0), "", step=2)
    tracker.observe(DIAGNOSE, Observation("1", 0), "", step=3)
    # A failed command may still change files; command success alone is not the criterion.
    tracker.observe(EDIT, Observation("failure after write", 1), "+changed\n", step=5)
    assert tracker.first_edit_step == 5 and not tracker.edit_checkpoint_exhausted
    assert status(tracker)["next_priority"] == "verify_patch"
    tracker.observe("revert", Observation("reverted", 0), "", step=6)
    assert tracker.first_edit_step == 5 and not tracker.edit_checkpoint_exhausted


def test_default_policy_keeps_original_prompt_and_does_not_enforce_checkpoint() -> None:
    tracker = ProgressTracker(configured(False))
    tracker.observe(READ, Observation("source", 0), "")
    tracker.observe(DIAGNOSE, Observation("1", 0), "")
    for i in range(4):
        tracker.observe(f"sed -n '{i + 1}p' module.py", Observation("source", 0), "")
    assert tracker.edit_checkpoint_step is None and not tracker.edit_checkpoint_exhausted
    prompt_status = status(tracker)
    assert prompt_status["next_priority"] == "edit_needed"
    assert "first_edit_step" not in prompt_status and "checkpoint_step" not in prompt_status


class FileSandbox(FakeSandbox):
    """One temporary Python file, actual file edits/diffs and a real focused pytest."""

    original = "def value():\n    return 1\n"

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory
        directory.mkdir()
        self.source = directory / "module.py"
        self.source.write_text(self.original)
        (directory / "test_module.py").write_text(
            "from module import value\n\ndef test_value():\n    assert value() == 2\n"
        )

    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        super().execute(handle, command, timeout)
        if command == EDIT:
            self.source.write_text(self.source.read_text().replace("return 1", "return 2"))
            return Observation("updated module.py", 0)
        if command == "pytest test_module.py":
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "test_module.py"],
                cwd=self.directory,
                env={"PATH": os.defpath, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return Observation(completed.stdout + completed.stderr, completed.returncode)
        if command == DIAGNOSE:
            return Observation("observed 1; expected 2", 0)
        return Observation(self.source.read_text(), 0)

    def patch(self, handle: str) -> str:
        super().patch(handle)
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.source.read_text().splitlines(keepends=True),
                fromfile="a/module.py",
                tofile="b/module.py",
            )
        )


def test_engine_reaches_actual_first_edit_then_verifies_same_diff(tmp_path: Path) -> None:
    cfg = ArmConfig.load(ROOT / "configs/variants/A1-first-edit.yaml")
    cfg = replace(cfg, progress=replace(cfg.progress, edit_prompt_after=2))
    actions = [READ, DIAGNOSE, "sed -n '1,5p' module.py", EDIT, "pytest test_module.py"]
    model = ScriptedModel(
        ["invalid action"]  # Invalid output consumes a step but never executes a tool.
        + [json.dumps({"tool": "bash", "command": c}) for c in actions]
        + ['{"tool":"submit"}']
    )
    sandbox = FileSandbox(tmp_path / "repository")
    writer = RunWriter(tmp_path, "file-edit", {"kind": "synthetic_test", "is_benchmark": False})
    result = Engine(cfg, model, sandbox, writer).run(TASK, 1)
    assert result.state.termination == "submitted" and sandbox.destroyed
    assert sandbox.commands == actions and "return 2" in sandbox.source.read_text()
    assert "+    return 2" in result.patch and "-    return 1" in result.patch
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    assert traces[0]["error_type"] == "format_error"
    edit, test = traces[4:6]
    assert edit["step"] == 5 and edit["edit_progress"]["first_edit_step"] == 5
    assert edit["patch_present_after"] and test["exit_code"] == 0
    assert edit["patch_sha256_after"] == test["patch_sha256_after"]
    assert traces[-1]["progress"]["next_priority"] == "review_and_submit"
    assert traces[-1]["progress"]["local_test_patch_sha256"] == test["patch_sha256_after"]
    assert "make_first_edit" in model.requests[3].messages[-1]["content"]
    assert writer.manifest["first_edit_step"] == 5


def test_engine_stops_different_no_diff_commands_and_keeps_all_request_costs(
    tmp_path: Path,
) -> None:
    class CostedModel(ScriptedModel):
        def complete(self, request: ModelRequest) -> ModelResponse:
            return replace(super().complete(request), cost_upper=Decimal("0.001"))

    cfg = ArmConfig.load(ROOT / "configs/variants/A1-first-edit.yaml")
    cfg = replace(cfg, progress=replace(cfg.progress, edit_prompt_after=2))
    commands = [READ, DIAGNOSE, "cat other.py", "sed -n '2p' module.py", EDIT]
    model = CostedModel([json.dumps({"tool": "bash", "command": c}) for c in commands])
    sandbox = FakeSandbox()
    writer = RunWriter(tmp_path, "no-edit", {"kind": "synthetic_test", "is_benchmark": False})
    result = Engine(cfg, model, sandbox, writer).run(TASK, 1)
    assert result.state.termination == "edit_not_reached" and not result.patch
    assert sandbox.commands == commands[:4] and len(model.requests) == 4 and sandbox.destroyed
    assert result.state.accounted_cost == Decimal("0.004")
    state = json.loads((writer.path / "state.json").read_text())
    assert state["edit_progress"]["first_edit_step"] is None
    assert state["edit_progress"]["checkpoint_exhausted"]
