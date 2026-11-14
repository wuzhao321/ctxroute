"""Efficiency changes preserve evidence, strict actions and real repair completion."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_source_decision import SourceSandbox
from test_target_action import config, selection

from ctxroute.config import ProgressConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.context.manager import ContextManager
from ctxroute.domain import Action, Observation, Step, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.efficiency import EfficiencyTracker
from ctxroute.orchestrator.engine import Engine
from ctxroute.orchestrator.repair_policy import RepairPolicy
from ctxroute.tracing.writer import RunWriter


def test_guidance_deduplicates_only_derived_text_not_actions_or_observations(
    tmp_path: Path,
) -> None:
    cfg = config()
    context = ContextManager(
        "system", "issue", cfg.context, ArtifactStore(tmp_path, max_bytes=10000, recall_chars=1000)
    )
    context.action_records = context.efficient_actions = True
    context.progress = {"next_action": "unique next instruction", "next_priority": "repair"}
    context.append(Step(1, "strong", Action("bash", command="cat core.py"), "source evidence", 0))
    old = context.messages()[-1]["content"]
    context.execution_efficiency = True
    new = context.messages()[-1]["content"]
    assert old.count("unique next instruction") == 2 and new.count("unique next instruction") == 1
    assert '"source evidence"' in new and '"command": "cat core.py"' in new
    assert len(new) < len(old) and len(context.recent) == 1


def test_read_index_marks_truncated_and_stale_windows_without_caching_execution() -> None:
    index = EfficiencyTracker(4000)
    index.observe(
        "# hypothesis: inspect\nsed -n '1,180p' core.py",
        Observation("x" * 6022, 0),
        "patch",
        7,
        model_action=True,
        failed=False,
    )
    index.observe(
        "sed -n '55,150p' core.py",
        Observation("x" * 3806, 0),
        "patch",
        8,
        model_action=True,
        failed=False,
    )
    rows = index.status("patch")["read_windows"]
    assert [r["fully_visible"] for r in rows] == [False, True]
    assert all(r["same_diff"] for r in rows)
    assert not any(r["same_diff"] for r in index.status("new patch")["read_windows"])
    assert "do not append" in index.guidance("new patch", unresolved=False).lower()
    assert len(index.reads) == 2


def test_failure_focus_quotes_observations_and_never_clears_failures() -> None:
    p = RepairPolicy(
        replace(config().progress, source_edit_strategy=True, efficiency_strategy=True)
    )
    p.install_plan(selection(), "test patch")
    job = p.queue.pop(0)
    assert "--showlocals" in job.command and "-vv" in job.command
    p.observe(
        job.command,
        Observation("E AssertionError: expected public behavior\n1 failed", 1),
        "test patch",
        "test patch",
        2,
        job=job,
    )
    assert p.efficiency is not None
    focus = p.efficiency.status("test patch")["last_failure"]
    assert focus["observed_error_lines"] == "E AssertionError: expected public behavior"
    assert p.unresolved and not p.can_submit
    assert "E AssertionError" in p.status(100, 10)["next_action"]


def test_same_selected_tests_and_baseline_gate_with_different_diagnostic_verbosity() -> None:
    old = RepairPolicy(replace(config().progress, source_edit_strategy=True))
    new = RepairPolicy(replace(old.config, efficiency_strategy=True))
    for p in [old, new]:
        p.install_plan(selection(), "test-only")
    assert [(j.category, j.selectors) for j in old.queue] == [
        (j.category, j.selectors) for j in new.queue
    ]
    assert "--showlocals" not in old.queue[0].command and "--showlocals" in new.queue[0].command
    for p in [old, new]:
        job = p.queue.pop(0)
        p.observe(
            job.command,
            Observation("ImportError\n1 error", 2),
            "test-only",
            "test-only",
            2,
            job=job,
        )
        assert not p.can_submit and p.target_contract and not p.target_contract.red_proven


def test_real_red_repair_target_regression_submit_without_added_model_calls(tmp_path: Path) -> None:
    commands = ["add-target", "bad-source", "good-source"]
    model = ScriptedModel(
        [json.dumps({"tool": "bash", "command": c}) for c in commands] + ['{"tool":"submit"}']
    )
    writer = RunWriter(tmp_path, "efficient-cycle", {"is_benchmark": False})
    cfg = replace(
        config(),
        progress=replace(config().progress, source_edit_strategy=True, efficiency_strategy=True),
    )
    sandbox = SourceSandbox(tmp_path / "repo")
    result = Engine(cfg, model, sandbox, writer).run(
        Task("toy", "toy/repo", "base", "Handle None"), 1
    )
    assert result.state.termination == "submitted" and len(model.requests) == 4
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    tests = [t for t in traces if t.get("origin") == "automatic_verification"]
    assert [(t["verification_category"], t["exit_code"]) for t in tests] == [
        ("target_baseline", 1),
        ("target", 1),
        ("target", 0),
        ("regression", 0),
    ]
    assert all("--showlocals" in t["command"] for t in tests)
    assert sandbox.commands.count("bad-source") == sandbox.commands.count("good-source") == 1
    assert "EXECUTION EFFICIENCY" not in model.requests[-1].messages[-1]["content"]
    state = json.loads((writer.path / "state.json").read_text())["repair_policy"]
    assert state["validation_ready_to_submit"] and not state["unresolved_failures"]


def test_efficiency_requires_existing_source_policy() -> None:
    with pytest.raises(ValueError, match="requires"):
        ProgressConfig(efficiency_strategy=True)


def test_read_and_edit_in_one_command_is_not_indexed_as_a_current_source_read() -> None:
    p = RepairPolicy(
        replace(config().progress, source_edit_strategy=True, efficiency_strategy=True)
    )
    p.observe("sed -n '1,20p' core.py && edit", Observation("old source", 0), "before", "after", 1)
    assert p.efficiency is not None and not p.efficiency.reads
