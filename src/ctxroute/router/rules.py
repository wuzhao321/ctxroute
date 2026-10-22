"""Conservative command classification is a routing heuristic, not a security boundary."""

import re
import shlex
from dataclasses import dataclass

from ctxroute.config import RouterConfig
from ctxroute.domain import RunState, Tier


def classify(command: str) -> str:
    if any(symbol in command for symbol in (";", "|", ">", "<", "`", "$", "\n", "&")):
        return "edit_or_unknown"
    try:
        parts = shlex.split(command)
    except ValueError:
        return "edit_or_unknown"
    if not parts:
        return "edit_or_unknown"
    if parts[0] in {"pytest", "tox"} or parts[:3] in (
        ["python", "-m", "pytest"],
        ["python", "-m", "unittest"],
    ):
        return "test"
    if parts[0] in {"cat", "grep", "rg", "ls", "head", "tail", "pwd", "wc"}:
        if parts[0] == "rg" and any(p.startswith("--pre") for p in parts):
            return "edit_or_unknown"
        return "read"
    if parts[0] == "find" and not any(
        p.startswith(("-exec", "-delete", "-ok", "-fprint", "-fls")) for p in parts
    ):
        return "read"
    if len(parts) >= 3 and parts[:2] == ["sed", "-n"] and re.fullmatch(r"\d+(,\d+)?p", parts[2]):
        return "read"
    return "edit_or_unknown"


@dataclass(frozen=True)
class Decision:
    tier: Tier
    reason: str
    locked_steps_remaining: int


class RuleRouter:
    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self.remaining = 0
        self.last_event = -1
        self.last_step = -1
        self.last_decision: Decision | None = None

    def decide(self, state: RunState, context_tokens: int) -> Decision:
        if self.last_step == state.step and self.last_decision is not None:
            return self.last_decision
        cfg = self.config
        reason, tier = "fixed_tier", cfg.default_tier
        if cfg.enabled:
            tier, reason = "strong", "default_strong"
            recent = state.history[-cfg.clean_steps :]
            previous = recent[-1] if recent else None
            if previous and previous.number != self.last_event:
                self.last_event = previous.number
                category = classify(previous.action.command) if previous.action else "invalid"
                consecutive_errors = len(recent) >= 2 and all(
                    s.error is not None or s.exit_code not in (None, 0) for s in recent[-2:]
                )
                if (
                    category == "edit_or_unknown"
                    or (category == "test" and previous.exit_code != 0)
                    or consecutive_errors
                    or (previous.tier == "cheap" and previous.error == "format_error")
                ):
                    self.remaining = cfg.strong_lock_steps
            if self.remaining:
                self.remaining -= 1
                reason = "strong_lock"
            elif (
                previous
                and previous.action
                and classify(previous.action.command) == "read"
                and len(recent) >= cfg.clean_steps
                and all(s.error is None and s.exit_code in (None, 0) for s in recent)
                and context_tokens < cfg.cheap_context_tokens
            ):
                tier, reason = "cheap", "clean_read_history"
        decision = Decision(tier, reason, self.remaining)
        self.last_step, self.last_decision = state.step, decision
        return decision
