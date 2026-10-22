"""Import independently generated A0 evidence without replacing the native baseline loop."""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from ctxroute.tracing.writer import RunWriter


def import_baseline(source: Path, output: Path, run_id: str) -> Path:
    """Source must be a normalized export alongside the unmodified native trajectory.

    No baseline execution is hidden here. The upstream entrypoint and package/commit
    must be fixed separately before generating the external evidence.
    """
    manifest: dict[str, Any] = json.loads((source / "manifest.json").read_text())
    required = {
        "instance_id",
        "seed",
        "upstream_commit",
        "upstream_version",
        "config_sha256",
        "price_sha256",
        "model_ids",
        "image_digest",
        "harness_version",
        "started_at",
        "finished_at",
        "termination",
        "wall_ms",
        "steps",
    }
    if not required <= set(manifest) or manifest.get("engine") != "mini-swe-agent":
        raise ValueError("A0 requires complete mini-swe-agent provenance")
    if manifest.get("kind") != "agent_run" or manifest.get("is_benchmark") is False:
        raise ValueError("Only real externally generated baseline evidence is accepted")
    predictions = [
        json.loads(line) for line in (source / "predictions.jsonl").read_text().splitlines() if line
    ]
    if len(predictions) != 1 or predictions[0]["instance_id"] != manifest["instance_id"]:
        raise ValueError("Expected exactly one matching baseline prediction")
    if set(predictions[0]) != {"instance_id", "model_name_or_path", "model_patch"}:
        raise ValueError("Invalid baseline prediction schema")
    trajectory = source / "trajectory.json"
    json.loads(trajectory.read_text())
    writer = RunWriter(
        output,
        run_id,
        {
            **manifest,
            "arm": "A0",
            "variant": "main",
            "import_source": str(source.resolve()),
            "trajectory_sha256": hashlib.sha256(trajectory.read_bytes()).hexdigest(),
        },
    )
    writer.manifest["started_at"] = manifest["started_at"]
    shutil.copyfile(trajectory, writer.path / "trajectory.json")
    for name in ("requests", "traces"):
        file = source / f"{name}.jsonl"
        if file.exists():
            for line in file.read_text().splitlines():
                if line:
                    event: dict[str, Any] = json.loads(line)
                    event.pop("run_id", None)
                    writer.append(name, event)
    writer.finish(manifest["termination"], predictions[0])
    writer.manifest["finished_at"] = manifest["finished_at"]
    # Persist provenance timestamps rather than import duration.
    (writer.path / "manifest.json").write_text(json.dumps(writer.manifest, indent=2) + "\n")
    return writer.path
