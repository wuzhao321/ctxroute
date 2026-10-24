"""Staged reproduction: offline preparation, prediction handoff, report import and analysis.

This module never starts models, containers, or the grading harness. Inference is
an explicit CLI stage; grading commands are handed to an independent environment.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from ctxroute.evalkit.analysis import import_harness_report, write_analysis
from ctxroute.evalkit.campaign import canonical_hash, digest, load_plan
from ctxroute.evalkit.export import collect_predictions, harness_command


def local(root: Path, path: Path) -> Path:
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Workflow paths must stay within the project")
    return resolved


def relative(root: Path, path: Path) -> str:
    return str(local(root, path).relative_to(root.resolve()))


def immutable_json(path: Path, value: dict[str, Any]) -> None:
    """Repeat identical offline stages, but never replace different evidence."""
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError(f"Existing evidence differs: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def prepare(root: Path, plan_path: Path, directory: Path) -> dict[str, Any]:
    root = root.resolve()
    plan_path, directory = local(root, plan_path), local(root, directory)
    plan = load_plan(root, plan_path)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, job in enumerate(plan["jobs"]):
        cohort = f"{job['arm']}-{job['variant']}-{job['seed']}"
        grouped[cohort].append({**job, "run_id": f"{plan['plan_id']}-{index:04d}"})
    body: dict[str, Any] = {
        "schema": 1,
        "kind": "staged_dev_reproduction",
        "plan_path": relative(root, plan_path),
        "plan_sha256": digest(plan_path),
        "plan_id": plan["plan_id"],
        "cohorts": dict(grouped),
        "grading": "external_official_harness_only",
        "execution_authorized": False,
    }
    body["workflow_id"] = canonical_hash(body)
    immutable_json(directory / "workflow.json", body)
    return body


def load(root: Path, directory: Path) -> dict[str, Any]:
    directory = local(root, directory)
    value: dict[str, Any] = json.loads((directory / "workflow.json").read_text())
    if (
        canonical_hash({k: v for k, v in value.items() if k != "workflow_id"})
        != value["workflow_id"]
    ):
        raise ValueError("Workflow contents changed")
    plan_path = local(root, Path(value["plan_path"]))
    if digest(plan_path) != value["plan_sha256"]:
        raise ValueError("Workflow plan changed")
    # Reconstruct membership from the immutable plan, not edited run directories.
    plan = json.loads(plan_path.read_text())
    expected: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, job in enumerate(plan["jobs"]):
        cohort = f"{job['arm']}-{job['variant']}-{job['seed']}"
        expected[cohort].append({**job, "run_id": f"{plan['plan_id']}-{index:04d}"})
    if value["plan_id"] != plan["plan_id"] or value["cohorts"] != dict(expected):
        raise ValueError("Workflow membership differs from its plan")
    return value


def completed_runs(root: Path, workflow: dict[str, Any]) -> list[Path]:
    directories: list[Path] = []
    for jobs in workflow["cohorts"].values():
        for job in jobs:
            directory = local(root, Path("results/runs") / job["run_id"])
            path = directory / "manifest.json"
            if not path.exists():
                raise ValueError(f"Scheduled run not started: {job['run_id']}")
            manifest = json.loads(path.read_text())
            if (
                manifest.get("kind") != "agent_run"
                or not manifest.get("finished_at")
                or manifest.get("plan_id") != workflow["plan_id"]
                or any(
                    manifest.get(k) != job[k]
                    for k in ("run_id", "instance_id", "arm", "variant", "seed")
                )
            ):
                raise ValueError(f"Incomplete or mismatched scheduled run: {job['run_id']}")
            rows = [
                json.loads(line)
                for line in (directory / "predictions.jsonl").read_text().splitlines()
                if line.strip()
            ]
            if len(rows) != 1 or rows[0].get("instance_id") != job["instance_id"]:
                raise ValueError("Prediction does not match the scheduled instance")
            directories.append(directory)
    return directories


def export(root: Path, directory: Path) -> dict[str, Any]:
    directory = local(root, directory)
    workflow = load(root, directory)
    runs = completed_runs(root, workflow)
    run_hashes = {path.name: digest(path / "predictions.jsonl") for path in runs}
    cohorts: dict[str, Any] = {}
    for name, jobs in workflow["cohorts"].items():
        selected = [local(root, Path("results/runs") / job["run_id"]) for job in jobs]
        target = directory / "predictions" / f"{name}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            wanted = [json.loads((path / "predictions.jsonl").read_text()) for path in selected]
            found = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
            if found != wanted:
                raise ValueError("Export exists with different predictions; use a new workflow")
        else:
            collect_predictions(selected, target)
        grading_id = f"ctxroute-{workflow['plan_id']}-{name}"
        cohorts[name] = {
            "predictions": relative(root, target),
            "sha256": digest(target),
            "grading_run_id": grading_id,
            "argv": harness_command(
                "python",
                Path(relative(root, target)),
                grading_id,
                [job["instance_id"] for job in jobs],
            ),
        }
    receipt = {
        "workflow_id": workflow["workflow_id"],
        "run_prediction_hashes": run_hashes,
        "cohorts": cohorts,
        "executed_harness": False,
        "instructions": "Transfer frozen predictions to the independent grading environment; "
        "run each argv from the project root there. Return official per-instance reports "
        "using a JSON mapping of inference run_id to report path. Do not reuse grading IDs "
        "with changed patches. Empty patches and failed runs remain in each cohort.",
    }
    immutable_json(directory / "handoff.json", receipt)
    return receipt


def finalize(root: Path, directory: Path, reports_path: Path, output: Path) -> Path:
    directory, output = local(root, directory), local(root, output)
    if output.exists():
        raise ValueError("Analysis output already exists; choose a new evidence directory")
    workflow = load(root, directory)
    runs = completed_runs(root, workflow)
    receipt = json.loads((directory / "handoff.json").read_text())
    if receipt["workflow_id"] != workflow["workflow_id"]:
        raise ValueError("Handoff belongs to a different workflow")
    if receipt["run_prediction_hashes"] != {p.name: digest(p / "predictions.jsonl") for p in runs}:
        raise ValueError("Predictions changed since grading handoff")
    for cohort in receipt["cohorts"].values():
        if digest(local(root, Path(cohort["predictions"]))) != cohort["sha256"]:
            raise ValueError("Grading export changed")
    supplied: Any = json.loads(local(root, reports_path).read_text())
    if not isinstance(supplied, dict):
        raise ValueError("Reports must be an object")
    reports = cast(dict[str, Any], supplied)
    if not set(reports) <= {p.name for p in runs}:
        raise ValueError("Reports must map scheduled run IDs to official report paths")
    # Validate the complete supplied mapping before importing any grades.
    resolved: dict[str, Path] = {}
    for run in runs:
        if run.name not in reports:
            continue  # Missing grades remain explicit in the analysis.
        item = reports[run.name]
        if not isinstance(item, str):
            raise ValueError("Report path must be a string")
        report = local(root, Path(item))
        raw: Any = json.loads(report.read_text())
        instance = json.loads((run / "manifest.json").read_text())["instance_id"]
        if not isinstance(raw, dict):
            raise ValueError("Official report must be an object")
        item = cast(dict[str, Any], raw).get(instance)
        if (
            not isinstance(item, dict)
            or type(cast(dict[str, Any], item).get("resolved")) is not bool
        ):
            raise ValueError("Expected official per-instance report with resolved boolean")
        resolved[run.name] = report
    for run in runs:
        grade_path = run / "grade.json"
        report = resolved.get(run.name)
        if grade_path.exists():
            grade = json.loads(grade_path.read_text())
            if grade["predictions_sha256"] != digest(run / "predictions.jsonl"):
                raise ValueError("Existing grade refers to a different prediction")
            if report is not None and grade["report_sha256"] != digest(report):
                raise ValueError("Existing grade refers to a different report")
    # Preflight every existing grade before mutating any run. A later mismatch
    # must not leave an earlier run newly graded after a failed batch import.
    for run in runs:
        report = resolved.get(run.name)
        if not (run / "grade.json").exists() and report is not None:
            import_harness_report(run, report)
    result = write_analysis(runs, output)
    immutable_json(
        result / "workflow-provenance.json",
        {
            "workflow_id": workflow["workflow_id"],
            "handoff_sha256": digest(directory / "handoff.json"),
            "report_mapping_sha256": digest(local(root, reports_path)),
            "reports": {run_id: digest(path) for run_id, path in resolved.items()},
            "note": "Operator-supplied official reports; "
            "correspondence must be checked in grading logs.",
        },
    )
    return result


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "infer", "export", "analyze"])
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.stage == "prepare":
        if args.plan is None:
            parser.error("prepare requires --plan")
        print(json.dumps(prepare(root, args.plan, args.workflow), indent=2))
    elif args.stage == "export":
        print(json.dumps(export(root, args.workflow), indent=2))
    elif args.stage == "analyze":
        if args.reports is None or args.output is None:
            parser.error("analyze requires --reports and --output")
        print(finalize(root, args.workflow, args.reports, args.output))
    else:
        if not args.execute or args.tasks is None or args.images is None:
            parser.error("infer requires --execute, --tasks and --images")
        from ctxroute.evalkit.cli import execute_campaign

        workflow = load(root, args.workflow)
        execute_campaign(
            root,
            local(root, Path(workflow["plan_path"])),
            local(root, args.tasks),
            local(root, args.images),
            execute=True,
        )


if __name__ == "__main__":
    main(sys.argv[1:])
