"""Bounded SSE completion tracking without storing model text in diagnostics."""

import json
import re
from dataclasses import dataclass, field
from typing import Any, cast

from ctxroute.api_probe import ProbeError, normalize_usage
from ctxroute.models.output_audit import OutputAudit

_FINISH_REASONS = {
    "stop",
    "length",
    "content_filter",
    "tool_calls",
    "insufficient_system_resource",
    "aborted",
}
_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)


class IncompleteStreamError(RuntimeError):
    """The stream ended without the protocol completion marker."""


class StreamProtocolError(RuntimeError):
    """The original stream cannot establish a valid terminal response."""


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
    _reasoning_tokens: int | None = None
    _reasoning_tokens_status: str = "not_seen"
    _content_present: bool = False
    _reasoning_present: bool = False
    _metadata_invalid: bool = False
    _skip_lf: bool = False
    output_audit: OutputAudit = field(default_factory=OutputAudit, repr=False)

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
            "reasoning_tokens": self._reasoning_tokens,
            "reasoning_tokens_status": self._reasoning_tokens_status,
            "receipt_status": self._receipt_status(),
            "content_present": self._content_present,
            "reasoning_present": self._reasoning_present,
            "metadata_invalid": self._metadata_invalid,
        }

    def validated_receipt(self) -> tuple[str, dict[str, int]]:
        """Require wire evidence before trusting SDK defaults or normalized counts.

        Non-stop reasons remain intact for the gateway's rejection and accounting.
        This receipt alone never authorizes an action or settles an SDK failure.
        """
        status = self._receipt_status()
        if status == "incomplete":
            raise IncompleteStreamError("Stream ended without completion marker")
        if status == "invalid_terminal":
            raise StreamProtocolError("Invalid stream terminal metadata")
        if status == "invalid_usage":
            raise ValueError(
                "invalid_usage" if self._usage_status != "not_seen" else "missing_usage"
            )
        assert self._finish_reason is not None and self._usage is not None
        return self._finish_reason, dict(self._usage)

    def _receipt_status(self) -> str:
        # This describes original terminal metadata and the five billing counts,
        # independently of generation success, SDK acceptance and optional detail.
        if not self.done:
            return "incomplete"
        if self._metadata_invalid or self._finish_reason not in _FINISH_REASONS:
            return "invalid_terminal"
        if self._usage_status != "valid" or self._usage is None:
            return "invalid_usage"
        return "valid"

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
        if "choices" not in record and record.get("usage") is None:
            self._metadata_invalid = True
        choices = record.get("choices", [])
        if not isinstance(choices, list):
            self._metadata_invalid = True
        else:
            if len(cast(list[object], choices)) > 1:
                self._metadata_invalid = True
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
                    if any(v is not None and not isinstance(v, str) for v in (content, reasoning)):
                        self._metadata_invalid = True
                    self._content_present |= isinstance(content, str) and bool(content)
                    self._reasoning_present |= isinstance(reasoning, str) and bool(reasoning)
                    if isinstance(content, str):
                        self.output_audit.content.add(content)
                    if isinstance(reasoning, str):
                        self.output_audit.reasoning.add(reasoning)
                else:
                    self._metadata_invalid = True
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
            self._reasoning_tokens, self._reasoning_tokens_status = None, "invalid"
            return
        if self._usage_status in {"invalid", "conflicting"}:
            return
        if self._usage is not None and counts != self._usage:
            self._usage, self._usage_status = None, "conflicting"
            self._reasoning_tokens, self._reasoning_tokens_status = None, "invalid"
        else:
            self._usage, self._usage_status = counts, "valid"
            self._observe_reasoning_usage(value, counts["completion_tokens"])

    def _observe_reasoning_usage(self, value: dict[str, Any], completion_tokens: int) -> None:
        # Optional breakdown, already included in completion_tokens. It never
        # changes pricing, SDK acceptance, or the required billing receipt.
        if self._reasoning_tokens_status in {"invalid", "conflicting"}:
            return
        details = value.get("completion_tokens_details")
        if details is None:
            return
        if not isinstance(details, dict):
            self._reasoning_tokens, self._reasoning_tokens_status = None, "invalid"
            return
        tokens = cast(dict[str, object], details).get("reasoning_tokens")
        if tokens is None:
            return
        if type(tokens) is not int or not 0 <= tokens <= completion_tokens:
            self._reasoning_tokens, self._reasoning_tokens_status = None, "invalid"
        elif self._reasoning_tokens is not None and self._reasoning_tokens != tokens:
            self._reasoning_tokens, self._reasoning_tokens_status = None, "conflicting"
        else:
            self._reasoning_tokens, self._reasoning_tokens_status = tokens, "valid"

    def feed(self, chunk: bytes) -> None:
        # OpenAI stops parsing at the first DONE. Later bytes must not alter the
        # receipt depending on how HTTPX happens to split the transport chunks.
        if self.done:
            return
        if self._skip_lf and chunk:
            chunk = chunk.removeprefix(b"\n")
            self._skip_lf = False
        self._buffer += chunk
        # SSE permits LF, CRLF and bare CR, including a CRLF split across chunks.
        while match := re.search(rb"[\r\n]", self._buffer):
            line = self._buffer[: match.start()]
            delimiter = self._buffer[match.start() : match.end()]
            self._buffer = self._buffer[match.end() :]
            if delimiter == b"\r":
                self._skip_lf = not self._buffer
                self._buffer = self._buffer.removeprefix(b"\n")
            self._event_bytes += len(line)
            if self._event_bytes > self.max_event_bytes:
                raise ValueError("SSE event exceeded byte limit")
            if not line:
                if self._data:
                    self.events += 1
                    self._observe_event(b"\n".join(self._data))
                self._data.clear()
                self._event_bytes = 0
                if self.done:
                    self._buffer = b""
                    return
            elif line.startswith(b":"):
                self.keepalives += 1
            elif line == b"data" or line.startswith(b"data:"):
                self._data.append(line.partition(b":")[2].removeprefix(b" "))
        if len(self._buffer) + self._event_bytes > self.max_event_bytes:
            raise ValueError("SSE event exceeded byte limit")
