"""Post-edit guidance keeps observed test outcomes separate from coverage claims."""

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
from ctxroute.domain import Action, ModelRequest, ModelResponse, Observation, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.progress import ProgressTracker
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]


def status(tracker: ProgressTracker) -> dict[str, str | int | float | bool]:
    return tracker.status(step=10, max_steps=50, seconds_left=100, cost_left=Decimal("0.1"))


def test_passed_command_is_a_scope_decision_and_new_diff_invalidates_its_evidence() -> None:
    tracker = ProgressTracker(ProgressConfig(post_edit_checkpoint=True))
    tracker.observe("edit", Observation("edited", 0), "+fix\n", step=1)
    assert status(tracker)["next_priority"] == "verify_regressions"
    tracker.observe("pytest tests/test_target.py", Observation("1 passed", 0), "+fix\n", step=2)
    current = status(tracker)
    assert current == status(tracker)
    assert current["next_priority"] == "submit_decision"
    assert current["local_test_command"] == "pytest tests/test_target.py"
    assert current["local_test_step"] == 2
    assert current["test_coverage"] == "not_established_by_exit_code"
    assert "preserved behavior" in str(current["next_action"])
    # Even a test command returning zero cannot verify a diff it changes itself.
    tracker.observe("pytest tests/test_target.py", Observation("1 passed", 0), "+new fix\n")
    current = status(tracker)
    assert current["next_priority"] == "verify_regressions"
    assert not current["local_test_command"] and not current["local_test_patch_sha256"]
    assert current["local_test_step"] == 0
    tracker.observe("python -c 'print(1)'", Observation("1", 0), "+new fix\n")
    assert status(tracker)["next_priority"] == "verify_regressions"


def test_pre_edit_guidance_adds_preservation_without_changing_original_checkpoint() -> None:
    cfg = ProgressConfig(first_edit_checkpoint=True, edit_prompt_after=2)
    original, candidate = (
        ProgressTracker(cfg),
        ProgressTracker(replace(cfg, post_edit_checkpoint=True)),
    )
    for tracker in (original, candidate):
        tracker.observe("cat sample.py", Observation("source", 0), "")
        tracker.observe("python -c 'print(1)'", Observation("1", 0), "")
        assert status(tracker)["next_priority"] == "make_first_edit"
        assert tracker.edit_checkpoint_step == 2 and tracker.no_edit_commands == 0
    assert "callers must retain" in str(status(candidate)["next_action"])
    assert "callers must retain" not in str(status(original)["next_action"])
    assert "local_test_command" not in status(original)


def test_collection_error_is_not_guidance_to_blindly_change_the_patch() -> None:
    tracker = ProgressTracker(ProgressConfig(post_edit_checkpoint=True))
    tracker.observe("edit", Observation("edited", 0), "+fix\n")
    tracker.observe(
        "pytest tests", Observation("ERROR collecting: missing dependency", 2), "+fix\n"
    )
    current = status(tracker)
    assert current["local_test_status"] == "not_run" and not current["local_test_patch_sha256"]
    assert current["next_priority"] == "verify_patch"
    assert "collection stopped" in str(current["next_action"])
    assert "pytest tests" in str(current["next_action"])
    check = tracker.last_check
    assert check and check.attribution == "collection_error"
    assert check.exit_code == 2 and check.test_exit_code is None


class RegressionSandbox(FakeSandbox):
    """Execute real local tests for a synthetic bug and a public caller contract."""

    original = "def normalize(value):\n    return value.strip()\n"

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory
        directory.mkdir()
        self.source = directory / "sample.py"
        self.source.write_text(self.original)
        (directory / "test_target.py").write_text(
            "from sample import normalize\n\ndef test_none():\n    assert normalize(None) == ''\n"
        )
        (directory / "test_callers.py").write_text(
            "from sample import normalize\n\ndef test_string():\n"
            "    assert normalize(' x ') == 'x'\n"
        )

    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        super().execute(handle, command, timeout)
        if command == "write-broad-fix":
            self.source.write_text("def normalize(value):\n    return ''\n")
            return Observation("edited", 0)
        if command == "write-narrow-fix":
            self.source.write_text(
                "def normalize(value):\n    return '' if value is None else value.strip()\n"
            )
            return Observation("edited", 0)
        assert command.startswith("pytest ")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *command.split()[1:]],
            cwd=self.directory,
            env={"PATH": os.defpath, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return Observation(completed.stdout + completed.stderr, completed.returncode)

    def patch(self, handle: str) -> str:
        super().patch(handle)
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.source.read_text().splitlines(keepends=True),
                fromfile="a/sample.py",
                tofile="b/sample.py",
            )
        )


def test_engine_target_pass_regression_failure_repair_and_submit(tmp_path: Path) -> None:
    cfg = ArmConfig.load(ROOT / "configs/variants/A1-edit-verify.yaml")
    assert cfg.limits.max_output_tokens == 8192
    assert cfg.progress.first_edit_checkpoint and cfg.progress.post_edit_checkpoint
    commands = [
        "write-broad-fix",
        "pytest test_target.py",
        "pytest test_callers.py",
        "write-narrow-fix",
        "pytest test_target.py test_callers.py",
    ]
    model = ScriptedModel(
        [json.dumps({"tool": "bash", "command": command}) for command in commands]
        + ['{"tool":"submit"}']
    )
    sandbox = RegressionSandbox(tmp_path / "repo")
    writer = RunWriter(tmp_path, "regression", {"is_benchmark": False})
    task = Task("regression-fixture", "fake/repo", "fixture", "normalize(None) should return ''.")
    result = Engine(cfg, model, sandbox, writer).run(task, 1)
    assert result.state.termination == "submitted" and sandbox.destroyed
    assert sandbox.commands == commands and len(model.requests) == 6
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    assert [trace["exit_code"] for trace in traces] == [0, 0, 1, 0, 0, None]
    assert traces[2]["progress"]["next_priority"] == "submit_decision"
    assert traces[3]["progress"]["next_priority"] == "repair_patch"
    assert traces[4]["progress"]["next_priority"] == "verify_regressions"
    assert traces[4]["progress"]["local_test_command"] == ""
    assert traces[-1]["progress"]["local_test_command"] == commands[-1]
    assert traces[-1]["progress"]["local_test_patch_sha256"] == traces[-2]["patch_sha256_after"]
    assert traces[1]["patch_sha256_after"] != traces[4]["patch_sha256_after"]
    assert result.state.accounted_cost == 0
    assert "submit_decision" in model.requests[-1].messages[-1]["content"]


MASKED_PROGRAM = (
    "from pathlib import Path\nimport subprocess, sys\n"
    'Path("sample.py").write_text("def normalize(value):\\n    return \'\'\\n")\n'
    'proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_callers.py"], '
    "capture_output=True, text=True)\n"
    "print(proc.stdout)\nprint('exit', proc.returncode)\n"
)
MASKED_EDIT = "python - <<'PY'\n" + MASKED_PROGRAM + "PY"


def test_reported_failure_invalidates_pass_and_survives_reads_until_new_evidence() -> None:
    tracker = ProgressTracker(ProgressConfig(post_edit_checkpoint=True))
    tracker.observe("edit", Observation("edited", 0), "+patch\n")
    tracker.observe("pytest test_target.py", Observation("1 passed", 0), "+patch\n")
    tracker.observe(MASKED_EDIT, Observation("assertion failure\nexit 1\n", 0), "+patch\n", step=3)
    current = status(tracker)
    assert current["reported_child_exit_code"] == 1 and current["reported_check_step"] == 3
    assert current["next_priority"] == "repair_patch" and current["local_test_status"] == "not_run"
    assert not current["local_test_patch_sha256"]
    tracker.observe("cat sample.py", Observation("source", 0), "+patch\n")
    assert status(tracker)["reported_check_step"] == 3
    tracker.observe("edit", Observation("repaired", 0), "+repaired\n")
    assert "reported_child_exit_code" not in status(tracker)
    assert status(tracker)["local_test_status"] == "not_run"
    tracker.observe(MASKED_EDIT, Observation("exit 0\n", 0), "+repaired\n")
    assert status(tracker)["local_test_status"] == "not_run"  # Not a coverage/pass claim.
    tracker.observe("pytest tests", Observation("2 passed", 0), "+repaired\n")
    assert status(tracker)["next_priority"] == "submit_decision"


@pytest.mark.parametrize(
    ("command", "observation"),
    [
        ("cat sample.py", Observation("print(proc.returncode)\nexit 1\n", 0)),
        (MASKED_EDIT, Observation("exit 1\n", 0, timed_out=True)),
        (MASKED_EDIT, Observation("exit 1\n", 0, output_limited=True)),
    ],
)
def test_reported_exit_is_not_inferred_from_source_reads_or_incomplete_output(
    command: str,
    observation: Observation,
) -> None:
    tracker = ProgressTracker(ProgressConfig(post_edit_checkpoint=True))
    tracker.observe("edit", Observation("edited", 0), "+patch\n")
    tracker.observe(command, observation, "+patch\n")
    assert "reported_child_exit_code" not in status(tracker)
    assert status(tracker)["local_test_status"] == "not_run"


class MaskedReproductionSandbox(RegressionSandbox):
    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        if command == MASKED_EDIT:
            FakeSandbox.execute(self, handle, command, timeout)
            completed = subprocess.run(
                [sys.executable, "-c", MASKED_PROGRAM],
                cwd=self.directory,
                env={"PATH": os.defpath, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return Observation(completed.stdout + completed.stderr, completed.returncode)
        if command == "cat sample.py":
            FakeSandbox.execute(self, handle, command, timeout)
            return Observation(self.source.read_text(), 0)
        return super().execute(handle, command, timeout)


@pytest.mark.parametrize(
    "malformed",
    [
        '{"tool":"bash","command":"must-not-execute"}]',
        '{"tool":"bash","command":"must-not-execute","start":0}',
    ],
)
def test_edit_failure_format_correction_repair_test_and_submit(
    tmp_path: Path, malformed: str
) -> None:
    class CostedModel(ScriptedModel):
        def complete(self, request: ModelRequest) -> ModelResponse:
            history = (
                request.messages[-1]["content"]
                .split("P3 RECENT STEPS\n")[1]
                .split("\nNEXT ACTION")[0]
            )
            for line in history.splitlines():
                action = json.loads(line)["action"]
                if action is not None:
                    Action.parse(json.dumps(action), recall_enabled=False)
            return replace(super().complete(request), cost_upper=Decimal("0.001"))

    cfg = ArmConfig.load(ROOT / "configs/variants/A1-edit-verify.yaml")
    commands = [
        MASKED_EDIT,
        "pytest test_callers.py",
        "cat sample.py",
        "write-narrow-fix",
        "pytest test_target.py test_callers.py",
    ]
    actions = [json.dumps({"tool": "bash", "command": command}) for command in commands]
    actions.insert(1, malformed)
    model = CostedModel(actions + ['{"tool":"submit"}'])
    sandbox = MaskedReproductionSandbox(tmp_path / "repo")
    writer = RunWriter(tmp_path, "continue-after-edit", {"is_benchmark": False})
    result = Engine(cfg, model, sandbox, writer, recover_action_metadata=True).run(
        Task("continuation-fixture", "fake/repo", "fixture", "Handle None, preserve strings."), 1
    )
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    assert result.state.termination == "submitted" and sandbox.destroyed
    assert sandbox.commands == commands and len(model.requests) == 7
    assert result.state.accounted_cost == Decimal("0.007")  # Includes rejected response.
    assert [t["exit_code"] for t in traces] == [0, None, 1, 0, 0, 0, None]
    assert traces[1]["progress"]["reported_child_exit_code"] == 1
    assert traces[1]["progress"]["reported_check_changed_patch"] is True
    assert traces[1]["progress"]["next_priority"] == "repair_patch"
    assert traces[1]["error_type"] == "format_error"
    correction = model.requests[2].messages[-1]["content"]
    assert "Action not executed" in correction
    assert (
        "Unexpected trailing content"
        if malformed.endswith("]")
        else "Unsupported fields for bash: start"
    ) in correction
    assert traces[3]["progress"]["local_test_status"] == "failed"
    assert traces[4]["progress"]["next_priority"] == "repair_patch"  # Read did not erase failure.
    assert traces[5]["progress"]["local_test_status"] == "not_run"  # New diff needs a test.
    assert (
        "historical_failure_may_be_stale_after_edits" in model.requests[5].messages[-1]["content"]
    )
    assert traces[6]["progress"]["local_test_status"] == "passed"
    assert traces[6]["progress"]["local_test_patch_sha256"] == traces[5]["patch_sha256_after"]
    state = json.loads((writer.path / "state.json").read_text())
    last_failure = json.loads(state["fixed_memory"]["last_failed_test"])
    assert last_failure["step"] == 3 and last_failure["exit_code"] == 1
    assert last_failure["meaning"] == "historical_failure_may_be_stale_after_edits"
    assert last_failure["patch_sha256_after"] != traces[5]["patch_sha256_after"]
    assert state["state"]["history"][0]["action"]["kind"] == "bash"  # Raw audit shape unchanged.
