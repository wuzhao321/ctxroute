"""Validated domain records, independent of provider and execution environment."""

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Literal, cast

from ctxroute.tracing.cost import Usage

Tier = Literal["strong", "cheap", "local"]


@dataclass(frozen=True)
class Task:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str

    def __post_init__(self) -> None:
        if not all((self.instance_id, self.repo, self.base_commit, self.problem_statement)):
            raise ValueError("Task fields must be nonempty")


@dataclass(frozen=True)
class Action:
    kind: Literal["bash", "recall", "submit"]
    command: str = ""
    artifact_id: str = ""
    start: int = 0
    end: int = 0

    @classmethod
    def parse(cls, text: str, *, recall_enabled: bool) -> "Action":
        try:
            raw: Any = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("Return exactly one JSON action object") from None
        if not isinstance(raw, dict):
            raise ValueError("Action must be an object")
        data = cast(dict[str, Any], raw)
        kind = data.get("tool")
        if kind == "submit" and set(data) == {"tool"}:
            return cls("submit")
        if kind == "bash" and set(data) == {"tool", "command"}:
            command = data["command"]
            if isinstance(command, str) and command.strip() and "\x00" not in command:
                return cls("bash", command=command)
        if kind == "recall" and recall_enabled and set(data) == {"tool", "id", "start", "end"}:
            start, end, artifact = data["start"], data["end"], data["id"]
            if type(start) is int and type(end) is int and 0 <= start < end:
                if isinstance(artifact, str) and artifact:
                    return cls("recall", artifact_id=artifact, start=start, end=end)
        raise ValueError("Invalid action schema or unavailable tool")


@dataclass(frozen=True)
class Observation:
    output: str
    exit_code: int
    timed_out: bool = False
    output_limited: bool = False
    rpc_ms: float = 0.0


@dataclass(frozen=True)
class Step:
    number: int
    tier: Tier
    action: Action | None
    observation: str
    exit_code: int | None = None
    error: str | None = None
    artifact_id: str | None = None

    def text(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass
class RunState:
    run_id: str
    task: Task
    arm: str
    seed: int
    step: int = 0
    history: list[Step] = field(default_factory=list[Step])
    tier_history: list[Tier] = field(default_factory=list[Tier])
    accounted_cost: Decimal = Decimal(0)
    termination: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict[str, str], ...]
    tier: Tier
    purpose: str
    max_output_tokens: int
    timeout_seconds: float
    run_id: str
    step: int
    scope_limit: Decimal


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model_id: str
    usage: Usage
    latency_ms: float = 0.0
    cost_upper: Decimal = Decimal(0)
