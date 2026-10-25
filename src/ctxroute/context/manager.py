"""Four-part context with transactional batch compression and calibrated estimates."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass

from ctxroute.config import ContextConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.context.memory import Memory as Memory
from ctxroute.domain import EvidenceNote, Step


class ContextLimit(RuntimeError):
    pass


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
        self.confirmed: list[dict[str, str | int]] = []
        self.progress: dict[str, str | int | float | bool] = {}

    def remember(self, notes: tuple[EvidenceNote, ...], history: list[Step]) -> None:
        """Promote exact previously visible observations, never inferred conclusions."""
        pending = list(self.confirmed)
        for note in notes:
            source = next((s for s in history if s.number == note.step), None)
            if (
                source is None
                or not source.artifact_id
                or source.action is None
                or source.action.kind != "bash"
                or not note.quote.strip()
                or len(note.quote) > self.config.fixed_quote_chars
                or note.quote not in source.observation
                or not self.artifacts.contains(source.artifact_id, note.quote)
            ):
                raise ValueError(
                    "Remember requires an exact quote from an earlier tool observation"
                )
            entry = {
                "step": note.step,
                "quote": note.quote,
                "artifact_id": source.artifact_id,
                "command": source.action.command,
                "meaning": "historical_observation_not_current_truth",
            }
            if entry not in pending:
                pending.append(entry)
        if len(pending) > self.config.fixed_max_facts:
            raise ValueError("Fixed observation capacity reached; existing evidence is retained")
        self.confirmed = pending
        if pending:
            self.fixed["confirmed_observations"] = json.dumps(
                pending, ensure_ascii=False, sort_keys=True
            )

    def append(self, step: Step) -> None:
        self.recent.append(step)
        if step.artifact_id:
            self.artifacts.pin(step.artifact_id)

    def _user_text(self, memory: Memory, recent: list[Step]) -> str:
        return (
            f"P0 TASK\n{self.task}\nP0 RUN STATUS (orchestrator observations)\n"
            f"{json.dumps(self.progress, ensure_ascii=False, sort_keys=True)}\nP1 FIXED MEMORY\n"
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
                proposed = summarizer(batch)
                # Only artifacts already in this task's visible history may be cited.
                if not proposed.references() <= self.artifacts.pinned:
                    raise ValueError("Summary cites unknown evidence")
                self._summary_cache[fingerprint] = proposed
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
