"""Four-part context with transactional batch compression and calibrated estimates."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from ctxroute.config import ContextConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.domain import Step


class ContextLimit(RuntimeError):
    pass


@dataclass(frozen=True)
class Memory:
    files_read: list[str] = field(default_factory=list[str])
    commands_run: list[str] = field(default_factory=list[str])
    hypotheses: list[str] = field(default_factory=list[str])
    edits: list[str] = field(default_factory=list[str])
    open_questions: list[str] = field(default_factory=list[str])

    @classmethod
    def parse(cls, text: str) -> "Memory":
        value: Any = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Summary must be an object")
        data = cast(dict[str, Any], value)
        fields = set(cls.__dataclass_fields__)
        if set(data) != fields:
            raise ValueError("Summary schema requires exactly five memory fields")
        for items in data.values():
            if not isinstance(items, list) or any(
                not isinstance(x, str) for x in cast(list[Any], items)
            ):
                raise ValueError("Memory fields must be lists of strings")
        return cls(**data)

    def text(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class TokenEstimator:
    def __init__(self) -> None:
        self.factor = 1.0

    def count(self, text: str) -> int:
        return math.ceil(max(1, len(text.encode()) / 4) * self.factor)

    def calibrate(self, text: str, actual: int) -> None:
        if actual < 0:
            raise ValueError("Usage cannot be negative")
        self.factor = max(self.factor, actual / max(1, len(text.encode()) / 4))


@dataclass(frozen=True)
class Compression:
    triggered: bool
    accepted: bool
    before: int
    after: int
    removed_steps: int = 0


class ContextManager:
    def __init__(
        self, system: str, task: str, config: ContextConfig, artifacts: ArtifactStore
    ) -> None:
        self.system = system
        self.task = task
        self.config = config
        self.artifacts = artifacts
        self.fixed: dict[str, str] = {}
        self.memory = Memory()
        self.recent: list[Step] = []
        self.estimator = TokenEstimator()
        self._attempted: set[str] = set()
        self._summary_cache: dict[str, Memory] = {}

    def append(self, step: Step) -> None:
        self.recent.append(step)
        if step.artifact_id:
            self.artifacts.pin(step.artifact_id)

    def _user_text(self, memory: Memory, recent: list[Step]) -> str:
        return (
            f"P0 TASK\n{self.task}\nP1 FIXED MEMORY\n"
            f"{json.dumps(self.fixed, ensure_ascii=False, sort_keys=True)}\n"
            f"P2 WORKING MEMORY\n{memory.text()}\nP3 RECENT STEPS\n"
            + "\n".join(step.text() for step in recent)
        )

    def messages(self) -> tuple[dict[str, str], ...]:
        return (
            {"role": "system", "content": self.system},
            {"role": "user", "content": self._user_text(self.memory, self.recent)},
        )

    def serialized(self) -> str:
        return json.dumps(self.messages(), ensure_ascii=False)

    def tokens(self) -> int:
        return self.estimator.count(self.serialized())

    def compress(self, summarizer: Callable[[str], Memory]) -> Compression:
        before = self.tokens()
        if self.config.mode == "none":
            if before > self.config.budget_tokens:
                raise ContextLimit("Uncompressed context limit reached")
            return Compression(False, False, before, before)
        if before < self.config.budget_tokens * self.config.soft_ratio:
            return Compression(False, False, before, before)
        count = max(0, len(self.recent) - self.config.recent_steps)
        if count == 0:
            if before >= self.config.budget_tokens * self.config.hard_ratio:
                raise ContextLimit("Protected context cannot be compressed further")
            return Compression(False, False, before, before)
        batch = json.dumps(
            {"memory": asdict(self.memory), "steps": [asdict(s) for s in self.recent[:count]]},
            ensure_ascii=False,
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(batch.encode()).hexdigest()
        if fingerprint in self._attempted:
            if before >= self.config.budget_tokens * self.config.hard_ratio:
                raise ContextLimit("Repeated compression would not recover context")
            return Compression(False, False, before, before)
        self._attempted.add(fingerprint)
        if self.config.mode == "truncate":
            candidate = self.memory
        else:
            if fingerprint not in self._summary_cache:
                self._summary_cache[fingerprint] = summarizer(batch)
            candidate = self._summary_cache[fingerprint]
        remaining = self.recent[count:]
        after = self.estimator.count(
            json.dumps(
                (
                    {"role": "system", "content": self.system},
                    {"role": "user", "content": self._user_text(candidate, remaining)},
                ),
                ensure_ascii=False,
            )
        )
        if after < before:
            self.memory, self.recent = candidate, remaining
            if after >= self.config.budget_tokens:
                raise ContextLimit("Compression insufficient to fit context")
            return Compression(True, True, before, after, count)
        if before >= self.config.budget_tokens * self.config.hard_ratio:
            raise ContextLimit("Compression failed to shrink context")
        return Compression(True, False, before, before)
