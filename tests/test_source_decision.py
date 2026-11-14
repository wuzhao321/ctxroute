"""Bounded failure-to-source decisions; real local tests and recorded failures, no API."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_target_action import TargetSandbox, config

from ctxroute.config import ProgressConfig
from ctxroute.domain import Observation, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.repair_policy import RepairPolicy
from ctxroute.orchestrator.source_decision import SourceDecision, production_patch
from ctxroute.tracing.writer import RunWriter


def diff(path: str, value: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{value}\n"


def policy() -> RepairPolicy:
    return RepairPolicy(replace(config().progress, source_edit_strategy=True))


def test_test_changes_never_count_as_source_or_renew_diagnostics() -> None:
    s = SourceDecision(3)
    before = diff("tests/test_target.py", "assert False")
    after = diff("tests/test_target.py", "assert actual == expected")
    assert production_patch(before) == production_patch(after) == ""
    for step in range(1, 5):
        s.observe(
            "edit tests", before, after, step, model_action=True, had_failure=True, failures=[]
        )
    assert s.diagnostics == 4 and s.first_source_edit_step is None
    assert not s.source_edits and not s.failure_repairs
    source = diff("core.py", "return fixed")
    s.observe(
        "edit core", after, after + source, 5, model_action=True, had_failure=True, failures=[]
    )
    assert production_patch(after + source) == source
    assert s.first_source_edit_step == 5 and s.failure_repairs == [5] and s.diagnostics == 0


def test_repeated_failed_tests_cannot_reset_the_final_decision_allowance() -> None:
    p = policy()
    patch = diff("tests/test_target.py", "assert False")
    p.observe("pytest tests/test_target.py", Observation("1 failed", 1), patch, patch, 1)
    for step in range(2, 5):
        p.observe("pytest tests/test_target.py", Observation("1 failed", 1), patch, patch, step)
    assert p.status(100, 20)["source_decision"]["final_decision_due"]
    assert not p.status(100, 20)["incomplete_stop"]
    p.observe("cat core.py", Observation("source", 0), patch, patch, 5)
    assert p.status(100, 20)["incomplete_stop"] and not p.can_submit


def test_test_support_changes_and_missing_evidence_cannot_be_declared_a_source_fix() -> None:
    for path in ["tests/helper.py", "pkg/testutils/helper.py", "tests/test_new.py", "conftest.py"]:
        assert not production_patch(diff(path, "def helper(): pass"))
    p = policy()
    p.patch = diff("tests/test_target.py", "assert True")
    assert not p.can_submit
    assert p.source is not None and "Never invent a fix" in p.source.guidance("", [])


def test_hypothesis_links_to_recorded_failure_and_retest_without_new_action_fields() -> None:
    p = policy()
    before = diff("tests/test_target.py", "assert False")
    p.observe("pytest tests/test_target.py", Observation("1 failed", 1), before, before, 2)
    command = (
        "# hypothesis: None falls through the string path\n"
        "# location: core.py::normalize\n# failure_step: 2\nedit"
    )
    p.observe(command, Observation("edited", 0), before, before + diff("core.py", "fixed"), 4)
    assert p.source is not None
    assert p.source.intent["failure_link_valid"]
    assert p.source.intent["status"] == "model_hypothesis_not_verified"
    assert p.source.intent["failure_evidence"][0]["outcome"] == "failed"
    assert p.source.intent["retest_commands"] == ["pytest tests/test_target.py"]
    assert p.source.failure_repairs == [4] and p.unresolved and not p.can_submit


class SourceSandbox(TargetSandbox):
    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        if command.startswith("# hypothesis:"):
            return super().execute(handle, command.splitlines()[-1], timeout)
        observation = super().execute(handle, command, timeout)
        if command.startswith("sed -n"):
            return Observation((self.root / "core.py").read_text(), 0)
        return observation


def test_final_decision_can_edit_then_target_failure_repair_regression_submit(
    tmp_path: Path,
) -> None:
    commands = [
        "add-target",
        "cat core.py",
        "cat api.py",
        "sed -n '1,2p' core.py",
        "# hypothesis: handle None\n# location: core.py::normalize\n# failure_step: 2\nbad-source",
        "# hypothesis: retain string normalization\n# location: core.py::normalize\n"
        "# failure_step: 7\ngood-source",
    ]
    model = ScriptedModel(
        [json.dumps({"tool": "bash", "command": c}) for c in commands] + ['{"tool":"submit"}']
    )
    writer = RunWriter(tmp_path, "source-cycle", {"is_benchmark": False})
    sandbox = SourceSandbox(tmp_path / "repo")
    cfg = replace(config(), progress=replace(config().progress, source_edit_strategy=True))
    result = Engine(cfg, model, sandbox, writer).run(
        Task("toy", "toy/repo", "base", "Handle None"), 1
    )
    assert result.state.termination == "submitted"
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    state = json.loads((writer.path / "state.json").read_text())["repair_policy"]
    assert state["source_decision"]["first_production_edit_step"] == 6
    assert state["source_decision"]["failure_driven_source_edit_steps"] == [6, 8]
    assert state["source_decision"]["model_repair_hypothesis"]["failure_link_valid"]
    assert state["validation_ready_to_submit"] and not state["unresolved_failures"]
    assert [
        (t["verification_category"], t["exit_code"])
        for t in traces
        if t.get("origin") == "automatic_verification"
    ] == [("target_baseline", 1), ("target", 1), ("target", 0), ("regression", 0)]
    assert len(model.requests) == 7 and len(traces) == 11
    assert sandbox.commands.count("bad-source") == sandbox.commands.count("good-source") == 1
    assert "make_first_source_edit" in model.requests[4].messages[-1]["content"]
    assert '"final_decision_due": true' in model.requests[4].messages[-1]["content"]


def test_source_decision_requires_target_policy() -> None:
    with pytest.raises(ValueError, match="requires"):
        ProgressConfig(source_edit_strategy=True)
