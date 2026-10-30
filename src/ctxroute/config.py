"""Experiment configurations use the JSON-compatible subset of YAML 1.2."""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from ctxroute.domain import Tier
from ctxroute.tracing.cost import money


@dataclass(frozen=True)
class ContextConfig:
    budget_tokens: int
    recent_steps: int
    soft_ratio: float
    hard_ratio: float
    mode: Literal["none", "summary", "truncate"]
    fixed_max_facts: int = 16
    fixed_quote_chars: int = 500

    def __post_init__(self) -> None:
        if self.budget_tokens <= 0 or self.recent_steps < 1:
            raise ValueError("Invalid context limits")
        if not 0 < self.soft_ratio < self.hard_ratio <= 1:
            raise ValueError("Invalid compression thresholds")
        if self.mode not in {"none", "summary", "truncate"}:
            raise ValueError("Unknown compression mode")
        if min(self.fixed_max_facts, self.fixed_quote_chars) < 1:
            raise ValueError("Invalid fixed evidence limits")


@dataclass(frozen=True)
class RouterConfig:
    enabled: bool
    default_tier: Tier
    clean_steps: int
    strong_lock_steps: int
    cheap_context_tokens: int

    def __post_init__(self) -> None:
        if self.default_tier not in {"strong", "cheap", "local"}:
            raise ValueError("Invalid default model tier")
        if min(self.clean_steps, self.strong_lock_steps, self.cheap_context_tokens) < 1:
            raise ValueError("Invalid router limits")


@dataclass(frozen=True)
class Limits:
    max_steps: int
    wall_seconds: float
    command_seconds: float
    task_usd: str
    max_output_tokens: int
    output_head_chars: int
    output_tail_chars: int
    artifact_bytes: int
    recall_chars: int
    format_attempts: int

    def __post_init__(self) -> None:
        if self.max_steps < 50:
            raise ValueError("Main experiment step limit must be at least 50")
        if min(self.wall_seconds, self.command_seconds) <= 0:
            raise ValueError("Timeouts must be positive")
        if min(self.max_output_tokens, self.artifact_bytes, self.recall_chars) <= 0:
            raise ValueError("Output limits must be positive")
        if min(self.output_head_chars, self.output_tail_chars) < 0 or self.format_attempts < 1:
            raise ValueError("Invalid truncation or format retry limits")
        if money(self.task_usd) <= 0:
            raise ValueError("Task budget must be positive")

    @property
    def budget(self) -> Decimal:
        return money(self.task_usd)


@dataclass(frozen=True)
class ProgressConfig:
    edit_prompt_after: int = 6
    repeat_warn_after: int = 2
    repeat_block_after: int = 3
    max_blocked_repeats: int = 2
    final_step_warning: int = 10

    def __post_init__(self) -> None:
        values = (
            self.edit_prompt_after,
            self.repeat_warn_after,
            self.repeat_block_after,
            self.max_blocked_repeats,
            self.final_step_warning,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("Progress limits must be positive integers")
        if self.repeat_warn_after >= self.repeat_block_after:
            raise ValueError("Repeat warning must precede blocking")


@dataclass(frozen=True)
class ArmConfig:
    arm: str
    engine: str
    context: ContextConfig
    router: RouterConfig
    limits: Limits
    progress: ProgressConfig = ProgressConfig()

    @classmethod
    def load(cls, path: Path) -> "ArmConfig":
        raw: dict[str, Any] = json.loads(path.read_text())
        if set(raw) - {"progress"} != {"arm", "engine", "context", "router", "limits"}:
            raise ValueError("Unknown or missing arm configuration fields")
        if raw["arm"] not in {f"A{i}" for i in range(8)}:
            raise ValueError("Unknown arm")
        return cls(
            cast(str, raw["arm"]),
            cast(str, raw["engine"]),
            ContextConfig(**raw["context"]),
            RouterConfig(**raw["router"]),
            Limits(**raw["limits"]),
            ProgressConfig(**raw.get("progress", {})),
        )
