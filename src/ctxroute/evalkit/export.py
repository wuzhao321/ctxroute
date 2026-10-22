"""Prepare official harness inputs; never execute evaluation from this module."""

import json
from pathlib import Path
from typing import Any


def collect_predictions(run_dirs: list[Path], target: Path) -> None:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    cohort: tuple[object, ...] | None = None
    for directory in run_dirs:
        manifest: dict[str, Any] = json.loads((directory / "manifest.json").read_text())
        if manifest.get("kind") != "agent_run":
            raise ValueError("Synthetic demos cannot be submitted for benchmark evaluation")
        identity = (manifest.get("arm"), manifest.get("variant", "main"), manifest.get("seed"))
        if cohort is not None and identity != cohort:
            raise ValueError("Export exactly one arm/variant/seed per harness submission")
        cohort = identity
        for line in (directory / "predictions.jsonl").read_text().splitlines():
            row = json.loads(line)
            if set(row) != {"instance_id", "model_name_or_path", "model_patch"}:
                raise ValueError("Invalid prediction schema")
            if (
                not all(isinstance(value, str) for value in row.values())
                or row["instance_id"] in seen
            ):
                raise ValueError("Invalid or duplicate prediction")
            seen.add(row["instance_id"])
            rows.append(row)
    with target.open("x") as output:
        for row in rows:
            output.write(json.dumps(row) + "\n")


def harness_command(python: str, predictions: Path, run_id: str, instances: list[str]) -> list[str]:
    if not instances:
        raise ValueError("An explicit frozen instance list is required")
    return [
        python,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        "princeton-nlp/SWE-bench_Lite",
        "--predictions_path",
        str(predictions),
        "--instance_ids",
        *instances,
        "--run_id",
        run_id,
        "--max_workers",
        "1",
    ]
