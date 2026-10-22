"""Append-only event streams and exclusive per-run output directories."""

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def encode(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Unsupported trace type: {type(value).__name__}")


class RunWriter:
    def __init__(self, root: Path, run_id: str, manifest: dict[str, Any]) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", run_id):
            raise ValueError("Unsafe run identifier")
        self.path = root / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.manifest = {**manifest, "run_id": run_id, "started_at": datetime.now(UTC).isoformat()}
        self._manifest()

    def _manifest(self) -> None:
        target = self.path / "manifest.json"
        temporary = self.path / "manifest.tmp"
        temporary.write_text(json.dumps(self.manifest, default=encode, indent=2) + "\n")
        temporary.replace(target)

    def append(self, stream: str, event: dict[str, Any]) -> None:
        if stream not in {"traces", "requests", "lifecycle"}:
            raise ValueError("Unknown trace stream")
        record = {"run_id": self.manifest["run_id"], **event}
        with (self.path / f"{stream}.jsonl").open("a") as output:
            output.write(json.dumps(record, default=encode, ensure_ascii=False) + "\n")
            output.flush()

    def snapshot(self, name: str, value: dict[str, Any]) -> None:
        if name not in {"state", "config", "task"}:
            raise ValueError("Unknown snapshot")
        target = self.path / f"{name}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, default=encode, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(target)

    def finish(self, termination: str, prediction: dict[str, str]) -> None:
        with (self.path / "predictions.jsonl").open("x") as output:
            output.write(json.dumps(prediction) + "\n")
        self.manifest.update(termination=termination, finished_at=datetime.now(UTC).isoformat())
        self._manifest()
