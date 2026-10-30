"""One offline chain contract. All task, model and grading data are test fixtures.

Only a temporary toy file is changed, through an exact command whitelist; no
shell, model API, Docker or official benchmark is executed. Temporary manifests
use the production schema solely to exercise the post-processing contract.
"""

import difflib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ctxroute.config import ArmConfig
from ctxroute.domain import ModelRequest, ModelResponse, Observation, Task
from ctxroute.evalkit.analysis import summarize_run
from ctxroute.evalkit.campaign import create_plan, run_plan
from ctxroute.evalkit.workflow import export, finalize, prepare
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.cost import Usage, money
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "def add(a, b):\n    return a - b\n"
AFTER = "def add(a, b):\n    return a + b\n"


class ToySandbox:
    """Exact dispatch, not an unrestricted local-shell sandbox."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.file = directory / "toy.py"
        self.active = False
        self.cleaned = False

    def create(self, task: Task) -> str:
        assert task.instance_id == "synthetic-chain-1"
        self.directory.mkdir()
        self.file.write_text(BEFORE)
        self.active = True
        return "toy-handle"

    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        assert self.active and handle == "toy-handle" and timeout > 0
        if command in {"cat toy.py", "cd /testbed && cat toy.py"}:
            return Observation(self.file.read_text(), 0)
        if command == "fix-toy":
            assert self.file.read_text() == BEFORE
            self.file.write_text(AFTER)
            return Observation("applied the toy edit", 0)
        if command in {"check-toy", "pytest toy.py"}:
            # Verify only known bytes; never eval arbitrary generated code.
            passed = self.file.read_text() == AFTER
            return Observation(
                "toy content check passed" if passed else "toy content check failed",
                0 if passed else 1,
            )
        raise ValueError("Toy fixture rejected a command outside its exact whitelist")

    def patch(self, handle: str) -> str:
        assert self.active and handle == "toy-handle"
        return "".join(
            difflib.unified_diff(
                BEFORE.splitlines(True),
                self.file.read_text().splitlines(True),
                fromfile="a/toy.py",
                tofile="b/toy.py",
            )
        )

    def destroy(self, handle: str) -> None:
        assert handle == "toy-handle"
        self.active, self.cleaned = False, True


def test_offline_full_chain_and_evidence_guards(tmp_path: Path) -> None:
    # The actual project, frozen split, budget and live flags are never modified.
    for name in ("configs", "docs", "src"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "data").mkdir()
    shutil.copytree(ROOT / "data/prices", tmp_path / "data/prices")
    (tmp_path / "data/split.json").write_text(
        json.dumps({"dev": ["synthetic-chain-1"], "test": []})
    )
    runtime_path = tmp_path / "configs/runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["execution_mode"] = (
        "live"  # Temporary contract fixture; factory below cannot call APIs.
    )
    runtime_path.write_text(json.dumps(runtime))
    task = Task("synthetic-chain-1", "fixture/toy", "fixture-base", "Fix the toy add operation")
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        json.dumps(
            dict(
                instance_id=task.instance_id,
                repo=task.repo,
                base_commit=task.base_commit,
                problem_statement=task.problem_statement,
            )
        )
        + "\n"
    )
    plan_path = tmp_path / "plan.json"
    plan = create_plan(tmp_path, plan_path, arms=["A1", "A4"], count=1)
    workflow = tmp_path / "workflow"
    prepare(tmp_path, plan_path, workflow)
    ledger = Ledger(
        tmp_path / "fixture-budget.sqlite",
        hard_limit=money("10"),
        pause_at=money("8"),
        enabled=True,
    )
    sandboxes: list[ToySandbox] = []
    model_calls: list[ScriptedModel] = []

    def factory(job: dict[str, Any], current: Task, run_id: str) -> Engine:
        sandbox = ToySandbox(tmp_path / run_id)
        model = ScriptedModel(
            [
                '{"tool":"bash","command":"cd /testbed && cat toy.py"}',
                '{"tool":"bash","command":"cd /testbed && cat toy.py"}',
                '{"tool":"bash","command":"cd /testbed && cat toy.py"}',
                '{"tool":"bash","command":"fix-toy",'
                '"remember":[{"step":1,"quote":"return a - b"}]}',
                '{"tool":"bash","command":"check-toy"}',
                '{"tool":"submit"}',
            ]
        )
        writer = RunWriter(
            tmp_path / "results/runs",
            run_id,
            {
                **job,
                "kind": "agent_run",
                "split": "dev",
                "evidence_origin": "unit_test_fixture_not_benchmark",
                "harness_version": None,
            },
        )
        sandboxes.append(sandbox)
        model_calls.append(model)
        return Engine(ArmConfig.load(tmp_path / job["config"]), model, sandbox, writer)

    completed = run_plan(tmp_path, plan_path, tasks, factory, ledger, execute=True)
    assert len(completed) == 2 and all(m["termination"] == "submitted" for m in completed)
    assert all(s.cleaned and not s.active and s.file.read_text() == AFTER for s in sandboxes)
    assert model_calls[1].requests[3].tier == "cheap"
    assert model_calls[1].requests[4].tier == "strong"
    assert ledger.snapshot().spent == ledger.snapshot().reserved == 0
    runs = [tmp_path / "results/runs" / m["run_id"] for m in completed]
    for run in runs:
        state = json.loads((run / "state.json").read_text())
        assert "return a - b" in state["fixed_memory"]["confirmed_observations"]
        assert (
            "+    return a + b"
            in json.loads((run / "predictions.jsonl").read_text())["model_patch"]
        )
    # A resumed completed campaign must not call either model again.
    assert run_plan(tmp_path, plan_path, tasks, factory, ledger, execute=True) == completed
    assert len(model_calls) == 2
    handoff = export(tmp_path, workflow)
    assert export(tmp_path, workflow) == handoff and not handoff["executed_harness"]
    assert len(handoff["cohorts"]) == 2
    reports: dict[str, str] = {}
    for run in runs:
        report = tmp_path / f"{run.name}-report-fixture.json"
        report.write_text(json.dumps({task.instance_id: {"resolved": True}}))
        reports[run.name] = str(report.relative_to(tmp_path))
    mapping = tmp_path / "report-fixture-map.json"
    mapping.write_text(json.dumps(reports))
    # Mismatched pre-existing grade must stop before importing another run's grade.
    bad_grade = runs[1] / "grade.json"
    bad_grade.write_text(json.dumps({"predictions_sha256": "incorrect"}))
    with pytest.raises(ValueError, match="different prediction"):
        finalize(tmp_path, workflow, mapping, tmp_path / "rejected-analysis")
    assert not (runs[0] / "grade.json").exists()
    bad_grade.unlink()  # Remove only this deliberately invalid temporary test fixture.
    output = finalize(tmp_path, workflow, mapping, tmp_path / "analysis")
    assert (output / "REPORT.md").is_file() and (output / "workflow-provenance.json").is_file()
    assert summarize_run(runs[0])["resolved"] is True
    assert summarize_run(runs[0])["cost_measured"] is False
    analysis = json.loads((output / "analysis.json").read_text())
    assert analysis["summaries"]["A4"]["graded"] == 1
    assert analysis["comparisons"][0]["population_noninferiority_claim"] is False
    prediction = runs[0] / "predictions.jsonl"
    prediction.write_text(prediction.read_text() + "\n")
    with pytest.raises(ValueError, match="changed since grading handoff"):
        finalize(tmp_path, workflow, mapping, tmp_path / "tampered-analysis")
    assert plan["purpose"] == "pilot"


def test_guidance_drives_fixture_from_reproduction_to_edit_verify_submit(tmp_path: Path) -> None:
    class GuidedFixture:
        priorities: list[str] = []

        def complete(self, request: ModelRequest) -> ModelResponse:
            # Deterministic fixture follows guidance, not a claim about a real model.
            tail = request.messages[1]["content"].split(
                "NEXT ACTION (orchestrator guidance, not tool output)\n"
            )[-1]
            priority = json.loads(tail)["priority"]
            self.priorities.append(priority)
            commands = {
                "investigate": "cat toy.py",
                "edit_needed": "fix-toy",
                "verify_patch": "pytest toy.py",
            }
            action = (
                {"tool": "submit"}
                if priority == "review_and_submit"
                else {"tool": "bash", "command": commands[priority]}
            )
            return ModelResponse(json.dumps(action), "fixture", Usage(0, 0, 0))

    sandbox = ToySandbox(tmp_path / "toy")
    model = GuidedFixture()
    writer = RunWriter(tmp_path / "runs", "guidance-fixture", {"kind": "synthetic_test"})
    task = Task("synthetic-chain-1", "fixture/toy", "fixture-base", "Fix toy add")
    result = Engine(ArmConfig.load(ROOT / "configs/arms/A1.yaml"), model, sandbox, writer).run(
        task, 1
    )
    assert result.state.termination == "submitted" and sandbox.cleaned
    assert model.priorities == [
        "investigate",
        "investigate",
        "edit_needed",
        "verify_patch",
        "review_and_submit",
    ]
    assert sandbox.file.read_text() == AFTER and "+    return a + b" in result.patch
