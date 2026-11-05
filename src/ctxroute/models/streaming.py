"""Bounded SSE completion tracking without storing model text in diagnostics."""

import json
from dataclasses import dataclass, field
from typing import Any, cast

from ctxroute.api_probe import ProbeError, normalize_usage

_FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource"}
_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)


class IncompleteStreamError(RuntimeError):
    """The stream ended without the protocol completion marker."""


@dataclass
class StreamTracker:
    events: int = 0
    keepalives: int = 0
    done: bool = False
    max_event_bytes: int = 1_048_576
    _buffer: bytes = b""
    _data: list[bytes] = field(default_factory=list[bytes])
    _event_bytes: int = 0
    _finish_reason: str | None = None
    _usage: dict[str, int] | None = None
    _usage_status: str = "not_seen"
    _content_present: bool = False
    _reasoning_present: bool = False
    _metadata_invalid: bool = False

    def terminal_fields(self) -> dict[str, Any]:
        """Only enum values, validated counts and booleans; never response text.

        An SDK can reject a thinking-only response before returning its usage.
        Keep the wire receipt available for diagnosis without using it to bypass
        SDK validation, execute an action, or settle an uncertain reservation.
        """
        return {
            "finish_reason": self._finish_reason,
            "usage": dict(self._usage) if self._usage is not None else None,
            "usage_status": self._usage_status,
            "content_present": self._content_present,
            "reasoning_present": self._reasoning_present,
            "metadata_invalid": self._metadata_invalid,
        }

    def _observe_event(self, data: bytes) -> None:
        if data.strip() == b"[DONE]":
            self.done = True
            return
        try:
            value: Any = json.loads(data)
        except (ValueError, RecursionError):
            self._metadata_invalid = True
            return
        if not isinstance(value, dict):
            self._metadata_invalid = True
            return
        record = cast(dict[str, Any], value)
        choices = record.get("choices", [])
        if not isinstance(choices, list):
            self._metadata_invalid = True
        else:
            for item in cast(list[object], choices):
                if not isinstance(item, dict):
                    self._metadata_invalid = True
                    continue
                choice = cast(dict[str, Any], item)
                if type(choice.get("index")) is not int or choice["index"] != 0:
                    self._metadata_invalid = True
                    continue
                reason = choice.get("finish_reason")
                if reason is not None:
                    safe = (
                        reason if isinstance(reason, str) and reason in _FINISH_REASONS else "other"
                    )
                    if self._finish_reason is not None and safe != self._finish_reason:
                        self._metadata_invalid = True
                        safe = "conflicting"
                    self._finish_reason = safe
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    fields = cast(dict[str, object], delta)
                    content = fields.get("content")
                    reasoning = fields.get("reasoning_content")
                    self._content_present |= isinstance(content, str) and bool(content)
                    self._reasoning_present |= isinstance(reasoning, str) and bool(reasoning)
        if record.get("usage") is not None:
            self._observe_usage(record["usage"])

    def _observe_usage(self, value: Any) -> None:
        try:
            normalize_usage({"usage": value})
            # Also bound numeric diagnostics: JSON may contain arbitrary-size integers.
            if any(not 0 <= value[name] <= 2**63 - 1 for name in _USAGE_FIELDS):
                raise ValueError("Invalid numeric diagnostic")
            counts = {name: value[name] for name in _USAGE_FIELDS}
        except (ProbeError, TypeError, ValueError):
            self._usage, self._usage_status = None, "invalid"
            return
        if self._usage_status in {"invalid", "conflicting"}:
            return
        if self._usage is not None and counts != self._usage:
            self._usage, self._usage_status = None, "conflicting"
        else:
            self._usage, self._usage_status = counts, "valid"

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            self._event_bytes += len(line)
            if self._event_bytes > self.max_event_bytes:
                raise ValueError("SSE event exceeded byte limit")
            if not line:
                if self._data:
                    self.events += 1
                    self._observe_event(b"\n".join(self._data))
                self._data.clear()
                self._event_bytes = 0
            elif line.startswith(b":"):
                self.keepalives += 1
            elif line == b"data" or line.startswith(b"data:"):
                self._data.append(line.partition(b":")[2].removeprefix(b" "))
        if len(self._buffer) + self._event_bytes > self.max_event_bytes:
            raise ValueError("SSE event exceeded byte limit")
