"""Public call-graph selection and failure-aware completion, without API calls."""

import difflib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from ctxroute.config import ArmConfig, ProgressConfig
from ctxroute.context.manager import Compression, ContextManager, Memory
from ctxroute.domain import Observation, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.public_tests import select_tests
from ctxroute.orchestrator.repair_policy import RepairPolicy
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = {
    "core.py": "def normalize(value):\n    return value.strip()\n",
    "api.py": "from core import normalize\ndef endpoint(value):\n    return normalize(value)\n",
    "tests/test_api.py": (
        "from api import endpoint\ndef test_strings():\n    assert endpoint(' x ') == 'x'\n"
    ),
    "tests/test_other.py": "def test_other():\n    assert True\n",
}
TARGET = "from core import normalize\ndef test_none():\n    assert normalize(None) == ''\n"
GOOD = "def normalize(value):\n    return '' if value is None else value.strip()\n"
BAD = "def normalize(value):\n    return ''\n"


def plan() -> dict[str, object]:
    return select_tests(
        ORIGINAL,
        {"core.py": GOOD, "tests/test_target.py": TARGET},
        ["core.py", "tests/test_target.py"],
    )


def test_selection_follows_callers_and_separates_target_from_public_regressions() -> None:
    chosen = plan()
    assert chosen["selection_complete"] is True
    assert chosen["regression"] == ["tests/test_api.py::test_strings"]
    assert chosen["target"] == ["tests/test_target.py::test_none"]
    assert chosen["reasons"] == {
        "tests/test_api.py::test_strings": [
            "core.normalize",
            "api.endpoint",
            "tests.test_api.test_strings",
        ]
    }


def test_constructing_a_class_does_not_invoke_all_its_methods() -> None:
    original = {
        **ORIGINAL,
        "unrelated.py": (
            "from core import normalize\nclass C:\n"
            "    def later(self):\n        return normalize('x')\n"
        ),
        "tests/test_construct.py": "from unrelated import C\ndef test_make():\n    C()\n",
    }
    chosen = select_tests(original, {"core.py": GOOD}, ["core.py"])
    assert "tests/test_construct.py::test_make" not in chosen["regression"]


def test_existing_test_weakened_or_removed_prevents_completion() -> None:
    chosen = select_tests(
        ORIGINAL,
        {"core.py": GOOD, "tests/test_api.py": "def test_strings():\n    assert True\n"},
        ["core.py", "tests/test_api.py"],
    )
    assert not chosen["selection_complete"]
    assert chosen["modified_existing_test_functions"] == ["tests.test_api.test_strings"]


def test_unknown_or_unbounded_selection_never_claims_coverage() -> None:
    result = select_tests({"core.py": ORIGINAL["core.py"]}, {"core.py": GOOD}, ["core.py"])
    assert result["selection_complete"] is False and result["regression"] == []


def test_new_diff_and_unrelated_pass_cannot_hide_failed_regression() -> None:
    policy = RepairPolicy(ProgressConfig(regression_strategy=True))
    policy.install_plan(json.dumps(plan()), "+bad")
    failed = policy.queue.pop(0)
    policy.observe(failed.command, Observation("1 failed", 1), "+bad", "+bad", 2, job=failed)
    assert policy.unresolved and not policy.can_submit and not policy.queue
    policy.observe("pytest tests/test_other.py", Observation("1 passed", 0), "+bad", "+bad", 3)
    assert policy.unresolved and not policy.can_submit
    policy.observe("edit", Observation("edited", 0), "+bad", "+good", 4)
    policy.install_plan(json.dumps(plan()), "+good")
    while policy.queue:
        job = policy.queue.pop(0)
        policy.observe(job.command, Observation("1 passed", 0), "+good", "+good", 5, job=job)
    assert policy.can_submit and not policy.unresolved and policy.failure_repairs == 1


def test_failed_compound_output_is_a_diagnostic_and_blocks_submit() -> None:
    policy = RepairPolicy(ProgressConfig(regression_strategy=True))
    policy.install_plan(json.dumps(plan()), "+fix")
    policy.observe(
        "python -m pylint sample.py; exit 0",
        Observation("sample.py:1:0: E0611: missing import", 0),
        "+fix",
        "+fix",
        2,
    )
    assert policy.unresolved and not policy.can_submit
    for step in range(3, 6):
        policy.observe("cat core.py", Observation("source", 0), "+fix", "+fix", step)
    status = policy.status(10, 1)
    assert status["incomplete_stop"] and status["next_priority"] == "repair_specific_failure"
    assert "Resources are low" in status["next_action"]


@pytest.mark.parametrize("output", ["no tests ran", "1 skipped", "1 passed"])
def test_incomplete_observations_never_allow_submit(output: str) -> None:
    policy = RepairPolicy(ProgressConfig(regression_strategy=True))
    policy.install_plan(json.dumps(plan()), "+fix")
    job = policy.queue.pop(0)
    policy.observe(
        job.command,
        Observation(output, 0, timed_out=output == "1 passed"),
        "+fix",
        "+fix",
        1,
        job=job,
    )
    assert not policy.can_submit


def test_selector_cannot_turn_repository_text_into_shell_code() -> None:
    policy = RepairPolicy(ProgressConfig())
    value = plan()
    value["regression"] = ["tests/a.py; touch /tmp/oops"]
    with pytest.raises(ValueError, match="Unsafe"):
        policy.install_plan(json.dumps(value), "+fix")


def test_reported_child_failure_blocks_after_all_selected_tests_pass() -> None:
    policy = RepairPolicy(ProgressConfig(regression_strategy=True))
    policy.install_plan(json.dumps(plan()), "+fix")
    while policy.queue:
        job = policy.queue.pop(0)
        policy.observe(job.command, Observation("1 passed", 0), "+fix", "+fix", 1, job=job)
    assert policy.can_submit
    policy.observe(
        "python - <<'PY'\nprint(proc.returncode)\nPY",
        Observation("exit 1\n", 0),
        "+fix",
        "+fix",
        2,
    )
    assert not policy.can_submit and policy.unresolved


class PublicSandbox(FakeSandbox):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        for name, content in ORIGINAL.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        super().execute(handle, command, timeout)
        if command in {"edit-bad", "edit-good"}:
            (self.root / "core.py").write_text(BAD if command == "edit-bad" else GOOD)
            (self.root / "tests/test_target.py").write_text(TARGET)
            return Observation("edited", 0)
        if "CTXROUTE_PUBLIC_SELECTION" in command:
            current = {
                str(p.relative_to(self.root)): p.read_text() for p in self.root.rglob("*.py")
            }
            return Observation(
                json.dumps(select_tests(ORIGINAL, current, ["core.py", "tests/test_target.py"])), 0
            )
        if command.startswith("python -m pytest"):
            completed = subprocess.run(
                [sys.executable, *shlex.split(command)[1:]],
                cwd=self.root,
                env={
                    "PATH": os.defpath,
                    "PYTHONPATH": str(self.root),
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                },
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return Observation(completed.stdout + completed.stderr, completed.returncode)
        return Observation("source", 0)

    def patch(self, handle: str) -> str:
        result = ""
        for name in ["core.py", "tests/test_target.py"]:
            path = self.root / name
            result += "".join(
                difflib.unified_diff(
                    ORIGINAL.get(name, "").splitlines(keepends=True),
                    (path.read_text() if path.exists() else "").splitlines(keepends=True),
                    fromfile="a/" + name,
                    tofile="b/" + name,
                )
            )
        return result


def run_fixture(
    tmp_path: Path, commands: list[str], *, enabled: bool = True
) -> tuple[object, Path, ScriptedModel]:
    cfg = ArmConfig.load(ROOT / "configs/candidates/A1-checkpoint-16k.json")
    cfg = replace(cfg, progress=replace(cfg.progress, regression_strategy=enabled))
    model = ScriptedModel(
        [json.dumps({"tool": "bash", "command": c}) for c in commands] + ['{"tool":"submit"}']
    )
    writer = RunWriter(tmp_path, "policy-test", {"is_benchmark": False})
    result = Engine(cfg, model, PublicSandbox(tmp_path / "repo"), writer).run(
        Task("toy", "toy/repo", "base", "Handle None and retain string behavior"), 1
    )
    return result, writer.path, model


def test_actual_regression_failure_repair_retest_target_and_submit(tmp_path: Path) -> None:
    _, run, model = run_fixture(tmp_path, ["edit-bad", "edit-good"])
    manifest = json.loads((run / "manifest.json").read_text())
    state = json.loads((run / "state.json").read_text())
    traces = [json.loads(line) for line in (run / "traces.jsonl").read_text().splitlines()]
    assert manifest["termination"] == "submitted"
    assert len(model.requests) == 3 and manifest["steps"] == 6
    assert [t["exit_code"] for t in traces] == [0, 1, 0, 0, 0, None]
    assert state["repair_policy"]["validation_ready_to_submit"]
    assert state["repair_policy"]["failure_repair_edits"] == 1
    assert [t.get("verification_category") for t in traces] == [
        "diagnostic",
        "regression",
        "diagnostic",
        "regression",
        "target",
        None,
    ]
    assert "repair_specific_failure" in model.requests[1].messages[-1]["content"]


def test_compression_event_is_retained_before_automatic_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inject a compression outcome to isolate the automatic-step recording path.
    original = ContextManager.compress

    def compress(manager: ContextManager, callback: Callable[[str], Memory]) -> Compression:
        if len(manager.recent) == 1:
            return Compression(True, True, 100, 50, 1)
        return original(manager, callback)

    monkeypatch.setattr(ContextManager, "compress", compress)
    _, run, _ = run_fixture(tmp_path, ["edit-bad", "edit-good"])
    lifecycle = [json.loads(line) for line in (run / "lifecycle.jsonl").read_text().splitlines()]
    traces = [json.loads(line) for line in (run / "traces.jsonl").read_text().splitlines()]
    compressions = [item for item in lifecycle if item["event"] == "compression"]
    assert len(compressions) == 1 and compressions[0]["step"] == 2
    assert traces[1]["origin"] == "automatic_verification"


def test_submit_with_known_failure_becomes_explicit_incomplete(tmp_path: Path) -> None:
    _, run, _ = run_fixture(tmp_path, ["edit-bad"])
    assert json.loads((run / "manifest.json").read_text())["termination"] == "incomplete_validation"


def test_repeated_diagnosis_stops_without_forced_submission(tmp_path: Path) -> None:
    _, run, model = run_fixture(tmp_path, ["edit-bad", "read-1", "read-2", "read-3"])
    assert json.loads((run / "manifest.json").read_text())["termination"] == "repair_stalled"
    assert len(model.requests) == 4


def test_existing_checkpoint_does_not_gain_automatic_tests_or_submit_gate(tmp_path: Path) -> None:
    _, run, model = run_fixture(tmp_path, ["edit-bad"], enabled=False)
    state = json.loads((run / "state.json").read_text())
    assert state["state"]["termination"] == "submitted" and state["repair_policy"] is None
    assert len(model.requests) == 2
