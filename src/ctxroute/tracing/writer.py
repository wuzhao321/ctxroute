"""Append-only event streams and exclusive per-run output directories."""

import hashlib
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
        self._secrets: list[str] = []
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

    def request_snapshot(self, request_id: str, value: dict[str, Any]) -> dict[str, Any]:
        """Persist a redacted transport payload before reservation or dispatch."""
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("Unsafe request identifier")
        return self.private_snapshot("request_audits", request_id, value)

    def protect_secret(self, secret: str) -> None:
        """Keep credentials out of new private evidence, including point captures."""
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def private_snapshot(self, category: str, name: str, value: dict[str, Any]) -> dict[str, Any]:
        if category not in {"request_audits", "compression_points"} or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,100}", name
        ):
            raise ValueError("Invalid evidence path")
        folder = self.path / category
        if folder.is_symlink():
            raise ValueError("Private evidence directory cannot be a symlink")
        folder.mkdir(mode=0o700, exist_ok=True)
        folder.chmod(0o700)
        original = json.dumps(value, default=encode, ensure_ascii=False)
        redacted = original
        for secret in self._secrets:
            redacted = redacted.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        record = json.loads(redacted)
        record["replayable"] = value.get("replayable", True) is True and original == redacted
        data = json.dumps(record, ensure_ascii=False).encode()
        used = sum(path.stat().st_size for path in folder.glob("*.json"))
        if used + len(data) > 8_388_608:
            raise ValueError("Private evidence storage limit reached")
        target = folder / f"{name}.json"
        with target.open("xb") as stream:
            target.chmod(0o600)
            stream.write(data)
        return {
            "path": str(target.relative_to(self.path)),
            "sha256": hashlib.sha256(data).hexdigest(),
            "replayable": record["replayable"],
        }

    def finish(self, termination: str, prediction: dict[str, str]) -> None:
        with (self.path / "predictions.jsonl").open("x") as output:
            output.write(json.dumps(prediction) + "\n")
        self.manifest.update(termination=termination, finished_at=datetime.now(UTC).isoformat())
        self._manifest()
