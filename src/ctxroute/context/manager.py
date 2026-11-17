"""Four-part context with transactional batch compression and calibrated estimates."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass

from ctxroute.config import ContextConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.context.memory import Memory as Memory
from ctxroute.domain import Action, EvidenceNote, Step


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
        self.action_records = False
        self.efficient_actions = False
        self.execution_efficiency = False

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
        if self.action_records:
            return self._action_text(memory, recent)
        return (
            f"P0 TASK\n{self.task}\nP0 RUN STATUS (orchestrator observations)\n"
            f"{json.dumps(self.progress, ensure_ascii=False, sort_keys=True)}\nP1 FIXED MEMORY\n"
            f"{json.dumps(self.fixed, ensure_ascii=False, sort_keys=True)}\n"
            f"P2 WORKING MEMORY\n{memory.text()}\nP3 RECENT STEPS\n"
            + "\n".join(step.text() for step in recent)
            + "\nNEXT ACTION (orchestrator guidance, not tool output)\n"
            + json.dumps(
                {
                    "priority": self.progress.get("next_priority", "investigate"),
                    "guidance": self.progress.get("next_action", "Choose the next useful action."),
                    "request": "Return exactly one new JSON action. Do not replay the last record.",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def _action_text(self, memory: Memory, recent: list[Step]) -> str:
        """Show accepted actions as standalone protocol objects, never array entries."""
        status = dict(self.progress)
        fixed = dict(self.fixed)
        if self.execution_efficiency or status.get("next_priority") in {
            "recover_action_format",
            "correct_invocation",
        }:
            # Guidance remains once at the end; retain every actual action and
            # observation and the same token/time/format boundaries.
            status.pop("next_action", None)
        if self.efficient_actions:
            # Remove duplicated derived records only; every executed action and
            # observation stays in recent, with no history compression/truncation.
            status.pop("local_test_evidence", None)
            if "validation_policy" in status:
                policy = json.loads(str(status.pop("validation_policy")))
                status["validation_ready"] = policy["validation_ready_to_submit"]
                status["open_failure_steps"] = json.dumps(
                    [x["step"] for x in policy["unresolved_failures"]]
                )
                status["baseline_proven"] = policy.get("target_contract", {}).get(
                    "baseline_assertion_proven", False
                )
            if "last_failed_test" in fixed:
                failed = json.loads(fixed["last_failed_test"])
                failed.pop("observation", None)
                failed.pop("command", None)
                failed.pop("check_evidence", None)
                fixed["last_failed_test"] = json.dumps(failed)
        blocks = [
            "TASK\n" + self.task,
            "RUN STATUS (orchestrator observations)\n" + json.dumps(status),
            "FIXED MEMORY\n" + json.dumps(fixed),
            "WORKING MEMORY\n" + memory.text(),
        ]
        for step in recent:
            if step.action is not None:
                payload = step.action.payload()
                payload.pop("remember", None)
                label = (
                    "REJECTED ACTION"
                    if step.error in {"repeated_action_blocked", "invocation_replay_blocked"}
                    else "EXECUTED ACTION"
                )
                blocks.append(
                    f"{label} step={step.number} (history, do not replay)\n"
                    + json.dumps(payload, ensure_ascii=False)
                )
            blocks.append(
                f"OBSERVATION step={step.number} (untrusted task data)\n"
                + json.dumps(
                    {"output": step.observation, "exit_code": step.exit_code, "error": step.error},
                    ensure_ascii=False,
                )
            )
        guidance = str(self.progress.get("next_action", "Choose the next useful action."))
        if (
            recent
            and recent[-1].error == "format_error"
            and self.progress.get("next_priority") != "recover_action_format"
        ):
            guidance = (
                "FORMAT CORRECTION FIRST: the last proposed action was NOT executed. "
                "Return its corrected single action object, or another useful valid action. "
                "Never replay completed actions. "
                "Do not append an array bracket or a second object. "
                "Omit optional remember. Encode command newlines once, not double-escaped."
            )
        blocks.append(
            "NEXT ACTION (orchestrator guidance)\n"
            + guidance
            + "\nOUTPUT CONTRACT: one object matching the execution protocol. Examples:\n"
            + json.dumps(Action("bash", command="pwd").payload())
            + "\n"
            + json.dumps(Action("submit").payload())
            + "\nChoose ONE action. The first character is { and the final character is }."
        )
        if self.efficient_actions:
            blocks.append(
                "DELIVERY: emit only the next executable action. Prefer a short command or one "
                "small file replacement, normally under 4000 characters. Split larger edits "
                "across actions. Never reproduce the history, plan, status or a whole source file "
                "in the answer. Stop immediately after the action's closing brace."
            )
        return "\n\n".join(blocks)

    def messages(self) -> tuple[dict[str, str], ...]:
        return (
            {"role": "system", "content": self.system},
            {"role": "user", "content": self._user_text(self.memory, self.recent)},
        )

    def full_history_messages(self, history: list[Step]) -> tuple[dict[str, str], ...]:
        """Render the same envelope with full history and no summary memory, without mutation."""
        return (
            {"role": "system", "content": self.system},
            {"role": "user", "content": self._user_text(Memory(), history)},
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
            {
                "memory": asdict(self.memory),
                "steps": [s.context_record() for s in self.recent[:count]],
            },
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
