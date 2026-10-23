"""Per-run content-addressed observations, bounded and reference-preserving."""

import hashlib
import json
import re
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path, *, max_bytes: int, recall_chars: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.recall_chars = recall_chars
        root.mkdir(parents=True, exist_ok=True)
        self.index = root / "index.json"
        self.pinned: set[str] = (
            set(json.loads(self.index.read_text())) if self.index.exists() else set()
        )

    def _path(self, artifact_id: str) -> Path:
        if re.fullmatch(r"[a-f0-9]{64}", artifact_id) is None:
            raise ValueError("Invalid artifact identifier")
        return self.root / f"{artifact_id}.txt"

    def put(self, text: str) -> str:
        data = text.encode()
        if len(data) > self.max_bytes:
            raise ValueError("Observation exceeds artifact size limit")
        artifact_id = hashlib.sha256(data).hexdigest()
        path = self._path(artifact_id)
        if not path.exists():
            if sum(p.stat().st_size for p in self.root.glob("*.txt")) + len(data) > self.max_bytes:
                raise ValueError("Artifact store capacity exhausted")
            path.write_bytes(data)
        return artifact_id

    def pin(self, artifact_id: str) -> None:
        if not self._path(artifact_id).is_file():
            raise ValueError("Cannot pin missing artifact")
        self.pinned.add(artifact_id)
        temporary = self.root / "index.tmp"
        temporary.write_text(json.dumps(sorted(self.pinned)))
        temporary.replace(self.index)

    def recall(self, artifact_id: str, start: int, end: int) -> str:
        if not 0 <= start < end or end - start > self.recall_chars:
            raise ValueError("Invalid or excessive recall range")
        text = self._path(artifact_id).read_text()
        if start >= len(text):
            raise ValueError("Recall start exceeds artifact length")
        return text[start:end]

    def remove(self, artifact_id: str) -> None:
        if artifact_id in self.pinned:
            raise ValueError("Referenced artifact cannot be removed")
        self._path(artifact_id).unlink()

    def contains(self, artifact_id: str, quote: str) -> bool:
        data = self._path(artifact_id).read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact_id:
            raise ValueError("Artifact content changed")
        return quote in data.decode()


def truncate(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    suffix = text[-tail:] if tail else ""
    return f"{text[:head]}\n[output truncated; full observation stored]\n{suffix}"
