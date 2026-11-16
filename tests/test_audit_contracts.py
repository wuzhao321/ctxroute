import hashlib
import json
import shutil
import subprocess
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ctxroute.config import ArmConfig
from ctxroute.domain import Action, Task
from ctxroute.evalkit.analysis import (
    import_harness_report,
    paired_comparison,
    summarize_run,
    verify_grading_receipt,
)
from ctxroute.evalkit.campaign import canonical_hash, create_plan, load_plan
from ctxroute.evalkit.provenance import controlled_variables
from ctxroute.orchestrator.engine import action_prompt
from ctxroute.router.rules import classify
from ctxroute.sandbox.docker import DockerSandbox, ProcessResult
from ctxroute.tracing.budget import BudgetStopped
from ctxroute.tracing.stage_budget import StageBudget, StageLedger

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "configs", tmp_path / "configs")
    shutil.copyfile(ROOT / "uv.lock", tmp_path / "uv.lock")
    shutil.copyfile(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    for directory in ("docs", "data/prices", "src/ctxroute", "scripts"):
        (tmp_path / directory).mkdir(parents=True)
    shutil.copyfile(ROOT / "docs/PREREG.md", tmp_path / "docs/PREREG.md")
    (tmp_path / "scripts/docker-local.sh").write_text("# offline fixture\n")
    (tmp_path / "src/ctxroute/fixture.py").write_text("# offline fixture\n")
    runtime = json.loads((tmp_path / "configs/runtime.json").read_text())
    shutil.copyfile(ROOT / runtime["price_file"], tmp_path / runtime["price_file"])
    (tmp_path / "data/split.json").write_text(json.dumps({"dev": ["fixture"], "test": []}))
    (tmp_path / "tasks.jsonl").write_text(
        json.dumps(
            {
                "instance_id": "fixture",
                "repo": "fixture/repo",
                "base_commit": "a" * 40,
                "problem_statement": "Synthetic task; never run a model",
            }
        )
        + "\n"
    )
    (tmp_path / "images.json").write_text(json.dumps({"fixture": "sha256:" + "a" * 64}))
    budget = json.loads((tmp_path / "configs/budget.json").read_text())
    budget.pop("active_stage_config", None)
    (tmp_path / "configs/budget.json").write_text(json.dumps(budget))
    return tmp_path


def plan(root: Path, **kwargs: Any) -> dict[str, Any]:
    return create_plan(
        root,
        root / "plan.json",
        arms=["A1", "A2"],
        count=1,
        tasks_path=root / "tasks.jsonl",
        images_path=root / "images.json",
        **kwargs,
    )


@pytest.mark.parametrize(
    "changed", ["tasks.jsonl", "images.json", "uv.lock", "scripts/docker-local.sh"]
)
def test_plan_freezes_real_inputs(project: Path, changed: str) -> None:
    frozen = plan(project)
    assert frozen["schema"] == 3 and not frozen["execution_authorized"]
    assert changed in frozen["frozen_files"]
    path = project / changed
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="Frozen input changed"):
        load_plan(project, project / "plan.json")


def test_valid_hash_does_not_excuse_inconsistent_task_cap(project: Path) -> None:
    frozen = plan(project)
    frozen["jobs"][0]["task_usd"] = "0.01"
    frozen["plan_id"] = canonical_hash({k: v for k, v in frozen.items() if k != "plan_id"})[:20]
    (project / "plan.json").write_text(json.dumps(frozen))
    with pytest.raises(ValueError, match="task cap"):
        load_plan(project, project / "plan.json")


@pytest.mark.parametrize("field", ["recall_enabled", "max_steps", "mode"])
def test_plan_rejects_uncontrolled_or_mislabeled_arms(project: Path, field: str) -> None:
    path = project / "configs/arms/A2.yaml"
    cfg = json.loads(path.read_text())
    if field == "max_steps":
        cfg["limits"][field] += 1
    else:
        cfg["context"][field] = False if field == "recall_enabled" else "none"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="Uncontrolled|Arm label"):
        plan(project)
    assert not (project / "plan.json").exists()


def test_rehashed_plan_cannot_omit_required_inputs(project: Path) -> None:
    frozen = plan(project)
    del frozen["frozen_files"]["uv.lock"]
    frozen["plan_id"] = canonical_hash({k: v for k, v in frozen.items() if k != "plan_id"})[:20]
    (project / "plan.json").write_text(json.dumps(frozen))
    with pytest.raises(ValueError, match="required frozen"):
        load_plan(project, project / "plan.json")


def stage(root: Path) -> StageBudget:
    path = root / "configs/budget.json"
    budget = json.loads(path.read_text())
    budget["active_stage_config"] = "configs/budgets/stage-cny45.json"
    path.write_text(json.dumps(budget))
    return StageBudget.load(root / budget["active_stage_config"])


def test_stage_rejects_missing_lane_and_impossible_request_floor(project: Path) -> None:
    stage(project)
    with pytest.raises(ValueError, match="budget_lane"):
        plan(project)
    with pytest.raises(ValueError, match="reservation floor"):
        plan(project, budget_lanes={"A1": "baseline", "A2": "checkpoint"})
    assert not (project / "plan.json").exists()


def test_whole_plan_checks_lane_headroom_without_mutating_history(project: Path) -> None:
    policy = replace(stage(project), paid_execution_enabled=True)
    ledger_path = project / policy.ledger
    ledger = StageLedger(ledger_path, policy, enabled=True)
    ledger.bind_scope("prior", "baseline")
    for number in range(4):
        ledger.reserve(
            str(number), Decimal("0.5"), "model_api", scope="prior", scope_limit=Decimal("2.5")
        )
    before = ledger_path.read_bytes()
    for arm in ("A1", "A2"):
        path = project / f"configs/arms/{arm}.yaml"
        cfg = json.loads(path.read_text())
        cfg["limits"]["task_usd"] = "0.65"
        path.write_text(json.dumps(cfg))
    with pytest.raises(BudgetStopped, match="baseline allocation"):
        plan(project, budget_lanes={"A1": "baseline", "A2": "checkpoint"})
    assert ledger_path.read_bytes() == before
    assert not (project / "plan.json").exists()


def pair() -> list[dict[str, Any]]:
    runtime = json.loads((ROOT / "configs/runtime.json").read_text())
    sandbox = json.loads((ROOT / "configs/sandbox.json").read_text())
    return [
        dict(
            arm=arm,
            variant="main",
            instance_id="fixture",
            seed=1,
            resolved=False,
            cost_measured=True,
            cost_upper_usd=1,
            wall_ms=10,
            provenance=dict(
                source="source",
                price="price",
                task="task",
                image="image",
                controls=controlled_variables(
                    ArmConfig.load(ROOT / f"configs/arms/{arm}.yaml"), runtime, sandbox
                ),
            ),
        )
        for arm in ("A1", "A2")
    ]


@pytest.mark.parametrize("field", ["source", "price", "task", "image"])
def test_pair_rejects_drift_and_labels_explicit_historical_mode(field: str) -> None:
    rows = pair()
    rows[1]["provenance"][field] = "different"
    with pytest.raises(ValueError, match="Uncontrolled"):
        paired_comparison(rows, "A1", "A2")
    result = paired_comparison(rows, "A1", "A2", allow_unverified=True)
    assert result["comparability"] == "unverified_historical"
    assert result["unverified_conditions"]


def test_only_declared_mechanisms_may_differ() -> None:
    rows = pair()
    assert paired_comparison(rows, "A1", "A2")["comparability"] == "verified"
    rows[1]["provenance"]["controls"]["limits"]["max_steps"] += 1
    with pytest.raises(ValueError, match="max_steps"):
        paired_comparison(rows, "A1", "A2")


def test_recall_is_independent_of_summary_and_present_in_evidence_prompt() -> None:
    cfg = ArmConfig.load(ROOT / "configs/arms/A2.yaml")
    cfg = replace(
        cfg,
        progress=replace(
            cfg.progress,
            regression_strategy=True,
            target_action_strategy=True,
            source_edit_strategy=True,
            evidence_action_strategy=True,
        ),
    )
    cfg = replace(cfg, context=replace(cfg.context, recall_enabled=True, mode="none"))
    assert "recall" in action_prompt(cfg)
    assert (
        Action.parse(
            '{"tool":"recall","id":"x","start":0,"end":1}', recall_enabled=cfg.context.can_recall
        ).kind
        == "recall"
    )
    disabled = replace(cfg, context=replace(cfg.context, recall_enabled=False, mode="summary"))
    assert "recall" not in action_prompt(disabled)


def test_read_pipelines_do_not_include_shell_side_effects() -> None:
    assert classify('grep -n "def example" source.py | head -20') == "read"
    assert classify("cd /testbed && cat file | head -20 | wc -l") == "read"
    for command in [
        "cat file | sh",
        "cat file | head -20 > other",
        "cat file | head -20; touch x",
        "rg --pre sh x | head -20",
    ]:
        assert classify(command) == "edit_or_unknown"


def test_compression_before_automatic_step_is_counted_once(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            dict(kind="agent_run", run_id="fixture", instance_id="fixture", arm="A2", seed=1)
        )
    )
    event = dict(event="compression", step=2, triggered=True, accepted=True, before=30, after=10)
    (tmp_path / "lifecycle.jsonl").write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n")
    (tmp_path / "traces.jsonl").write_text(
        json.dumps(
            {
                "step": 2,
                "tier": "strong",
                "origin": "automatic_verification",
                "command": "cat file",
                "compress_event": event,
            }
        )
        + "\n"
    )
    result = summarize_run(tmp_path)
    assert result["compression_count"] == 2 and result["compression_tokens_saved"] == 40


def test_costs_and_routing_use_all_request_receipts(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "agent_run",
                "run_id": "fixture",
                "instance_id": "fixture",
                "arm": "A4",
                "seed": 1,
                "accounted_cost_upper_usd": "0.02",
            }
        )
    )
    receipts = [
        dict(
            request_id="action",
            event="reserved",
            purpose="action",
            tier="strong",
            reserved_usd="0.3",
        ),
        dict(
            request_id="action",
            event="completed",
            cost_upper_usd="0.02",
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=2,
        ),
        dict(
            request_id="summary",
            event="reserved",
            purpose="compression",
            tier="cheap",
            reserved_usd="0.1",
        ),
        dict(request_id="summary", event="failed"),
    ]
    (tmp_path / "requests.jsonl").write_text("".join(json.dumps(item) + "\n" for item in receipts))
    result = summarize_run(tmp_path)
    assert result["cost_upper_usd"] == pytest.approx(0.12)
    assert result["cost_measured"] is False and result["unmeasured_requests"] == 1
    assert result["failed_requests"] == 1
    assert result["action_requests_by_tier"] == {"strong": 1}
    assert result["compression_requests_by_tier"] == {"cheap": 1}


def test_patch_survives_commit_and_timeout_export_only_restart(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    git("init", "-q")
    (tmp_path / "file.py").write_text("value = 1\n")
    git("add", "file.py")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    (tmp_path / "file.py").write_text("value = 2\n")
    git("add", "file.py")
    git("commit", "-qm", "task commit")
    calls: list[list[str]] = []

    def executor(argv: list[str], timeout: float, max_bytes: int) -> ProcessResult:
        calls.append(argv)
        if argv[1] == "exec":
            if argv[-1] == "simulate-timeout":
                return ProcessResult("timeout", 124)
            result = subprocess.run(
                argv[-1], shell=True, cwd=tmp_path, capture_output=True, text=True
            )
            return ProcessResult(result.stdout, result.returncode)
        return ProcessResult("ok", 0)

    config = json.loads((ROOT / "configs/sandbox.json").read_text())
    sandbox = DockerSandbox({"fixture": "sha256:" + "a" * 64}, config, executor)
    handle = sandbox.create(Task("fixture", "repo", base, "issue"))
    assert "+value = 2" in sandbox.patch(handle)
    assert sandbox.execute(handle, "simulate-timeout", 1).timed_out
    assert "+value = 2" in sandbox.patch(handle)
    assert sum(c[1] == "start" for c in calls) == 2
    with pytest.raises(ValueError, match="Terminated"):
        sandbox.execute(handle, "touch forbidden", 1)
    sandbox.destroy(handle)


def test_report_requires_prediction_and_grading_identity(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            dict(
                kind="agent_run",
                run_id="fixture",
                finished_at="synthetic",
                instance_id="fixture",
                harness_version="test",
            )
        )
    )
    prediction = tmp_path / "predictions.jsonl"
    prediction.write_text('{"model_patch":"A"}\n')
    report = tmp_path / "report.json"
    report.write_text('{"fixture":{"resolved":false}}')
    receipt = dict(
        inference_run_id="fixture",
        instance_id="fixture",
        harness_version="test",
        grading_run_id="grading-fixture",
        predictions_sha256=hashlib.sha256(prediction.read_bytes()).hexdigest(),
        report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
    )
    bad = deepcopy(receipt)
    bad["predictions_sha256"] = "different"
    with pytest.raises(ValueError, match="receipt"):
        import_harness_report(tmp_path, report, bad)
    assert not (tmp_path / "grade.json").exists()
    with pytest.raises(ValueError, match="receipt"):
        verify_grading_receipt(tmp_path, report, receipt, "another-grading-run")
    assert import_harness_report(tmp_path, report, receipt).exists()
