"""Versioned, strict JSON schema for summaries; no provider SDK dependency."""

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypedDict, cast


class FileRead(TypedDict):
    path: str
    start_line: int | None
    end_line: int | None
    facts: list[str]
    artifact_ids: list[str]


class CommandRun(TypedDict):
    command: str
    exit_code: int | None
    conclusion: str
    artifact_id: str | None


class Hypothesis(TypedDict):
    statement: str
    status: Literal["pending", "refuted", "supported"]
    evidence_ids: list[str]


class Edit(TypedDict):
    path: str
    added: int | None
    removed: int | None
    description: str
    artifact_ids: list[str]


def record(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


TEXT = {"type": "string", "minLength": 1}
ARTIFACT = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
LINE = {"type": ["integer", "null"], "minimum": 1}
COUNT = {"type": ["integer", "null"], "minimum": 0}
MEMORY_SCHEMA = record(
    {
        "files_read": array(
            record(
                {
                    "path": TEXT,
                    "start_line": LINE,
                    "end_line": LINE,
                    "facts": array(TEXT),
                    "artifact_ids": array(ARTIFACT),
                }
            )
        ),
        "commands_run": array(
            record(
                {
                    "command": TEXT,
                    "exit_code": {"type": ["integer", "null"]},
                    "conclusion": TEXT,
                    "artifact_id": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"},
                }
            )
        ),
        "hypotheses": array(
            record(
                {
                    "statement": TEXT,
                    "status": {"type": "string", "enum": ["pending", "refuted", "supported"]},
                    "evidence_ids": array(ARTIFACT),
                }
            )
        ),
        "edits": array(
            record(
                {
                    "path": TEXT,
                    "added": COUNT,
                    "removed": COUNT,
                    "description": TEXT,
                    "artifact_ids": array(ARTIFACT),
                }
            )
        ),
        "open_questions": array(TEXT),
    }
)
MEMORY_VERSION = 2


def validate(value: Any, schema: dict[str, Any]) -> None:
    """Validate the subset used by MEMORY_SCHEMA without coercing model output."""
    import re

    types = schema["type"]
    allowed = cast(list[str], types) if isinstance(types, list) else [str(types)]
    actual = {
        "dict": "object",
        "list": "array",
        "str": "string",
        "int": "integer",
        "NoneType": "null",
    }
    if actual.get(type(value).__name__) not in allowed:
        raise ValueError("Invalid memory field type")
    if value is None:
        return
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Invalid hypothesis status")
    if isinstance(value, dict):
        data = cast(dict[str, Any], value)
        if set(data) != set(schema["required"]):
            raise ValueError("Unknown or missing memory fields")
        for key, item in data.items():
            validate(item, schema["properties"][key])
    elif isinstance(value, list):
        for item in cast(list[Any], value):
            validate(item, schema["items"])
    elif isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0):
            raise ValueError("Empty memory text")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError("Invalid artifact reference")
    elif type(value) is int and "minimum" in schema and value < schema["minimum"]:
        raise ValueError("Invalid line or diff count")


@dataclass(frozen=True)
class Memory:
    files_read: list[FileRead] = field(default_factory=list[FileRead])
    commands_run: list[CommandRun] = field(default_factory=list[CommandRun])
    hypotheses: list[Hypothesis] = field(default_factory=list[Hypothesis])
    edits: list[Edit] = field(default_factory=list[Edit])
    open_questions: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        validate(asdict(self), MEMORY_SCHEMA)
        for item in self.files_read:
            start, end = item["start_line"], item["end_line"]
            if (start is None) != (end is None) or (
                start is not None and end is not None and start > end
            ):
                raise ValueError("Line range must be ordered or wholly unknown")
        for hypothesis in self.hypotheses:
            if hypothesis["status"] != "pending" and not hypothesis["evidence_ids"]:
                raise ValueError("Supported/refuted hypotheses require evidence references")

    @classmethod
    def parse(cls, text: str) -> "Memory":
        value: Any = json.loads(text)
        validate(value, MEMORY_SCHEMA)
        return cls(**value)

    def references(self) -> set[str]:
        refs = {ref for item in self.files_read for ref in item["artifact_ids"]}
        refs.update(item["artifact_id"] for item in self.commands_run if item["artifact_id"])
        refs.update(ref for item in self.hypotheses for ref in item["evidence_ids"])
        refs.update(ref for item in self.edits for ref in item["artifact_ids"])
        return refs

    def text(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
