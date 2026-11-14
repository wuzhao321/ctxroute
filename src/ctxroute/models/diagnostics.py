"""Per-call HTTP observations and value-limited model failure records."""

import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.error import HTTPError, URLError

from ctxroute.models.output_audit import OutputAudit

_TIMEOUT_STAGES = {
    "ConnectTimeout": "connect",
    "ReadTimeout": "read",
    "WriteTimeout": "write",
    "PoolTimeout": "pool",
}
_USAGE_ERRORS = {
    "missing_usage",
    "invalid_usage",
    "inconsistent_usage",
    "inconsistent_cache_usage",
    "Missing usage or hidden additional SDK requests",
    "Missing original DeepSeek cache usage fields",
    "SDK usage differs from stream receipt",
}
_SAFE_MESSAGES = _USAGE_ERRORS | {
    "Connection error.",
    "Request timed out.",
    "Incomplete model response",
    "Provider usage exceeded declared guard",
    "Provider price exceeded reservation",
    "Expected fixed probe or two-message context envelope",
    "Unsupported response format",
    "Stream ended without completion marker",
    "SSE event exceeded byte limit",
    "Response exceeded byte limit",
    "Invalid stream terminal metadata",
}
_BODY_VALUES = {
    "rate_limit_exceeded",
    "rate_limit_error",
    "too_many_requests",
    "invalid_request_error",
    "invalid_request",
    "invalid_api_key",
    "authentication_error",
    "insufficient_quota",
    "insufficient_balance",
    "server_error",
    "internal_error",
    "server_is_busy",
    "model_not_found",
    "Rate limit exceeded",
    "Too many requests",
}


def _safe_message(exc: BaseException) -> str | None:
    if len(exc.args) == 1 and isinstance(exc.args[0], str) and exc.args[0] in _SAFE_MESSAGES:
        return exc.args[0]
    return None


def _error_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    pending = [error]
    # Task groups can wrap the actual network/SDK exception in leaves rather
    # than __cause__. Bound traversal and retain only types/allowlisted messages.
    while pending and len(chain) < 32:
        current = pending.pop()
        if any(current is item for item in chain):
            continue
        chain.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(cast(BaseExceptionGroup[BaseException], current).exceptions))
        if isinstance(current, URLError) and isinstance(current.reason, BaseException):
            pending.append(current.reason)
        else:
            cause = current.__cause__ or current.__context__
            if cause is not None:
                pending.append(cause)
    return chain


def _safe_body(chain: list[BaseException]) -> dict[str, str]:
    for exc in chain:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            data = cast(dict[str, object], body)
            nested = data.get("error", data)
            if isinstance(nested, dict):
                fields = cast(dict[str, object], nested)
                safe = {
                    name: value
                    for name in ("code", "type", "message")
                    if isinstance(value := fields.get(name), str) and value in _BODY_VALUES
                }
                if safe:
                    return safe
    return {"omitted": "no_safe_structured_fields"}


@dataclass
class WireMetadata:
    request_body_bytes: int | None = None
    http_status: int | None = None
    provider_request_id: str | None = None
    streaming: bool = False
    response_bytes_received: int = 0
    response_chunks_received: int = 0
    response_completed: bool = False
    headers_after_ms: float | None = None
    first_byte_after_ms: float | None = None
    last_byte_after_ms: float | None = None
    sse_events: int = 0
    sse_keepalives: int = 0
    sse_done: bool = False
    stream_terminal: dict[str, Any] | None = None
    sdk_output_accepted: bool | None = None
    output_audit: OutputAudit = field(default_factory=OutputAudit, repr=False)
    _started: float = field(default_factory=time.monotonic, repr=False)

    def observe_response(self, status: int, headers: Any) -> None:
        self.http_status = status
        self.headers_after_ms = (time.monotonic() - self._started) * 1000
        if headers is not None:
            self.provider_request_id = headers.get("x-request-id") or headers.get("x-ds-trace-id")

    def observe_chunk(self, size: int) -> None:
        self.response_bytes_received += size
        self.response_chunks_received += 1
        self.last_byte_after_ms = (time.monotonic() - self._started) * 1000
        if self.first_byte_after_ms is None:
            self.first_byte_after_ms = self.last_byte_after_ms

    def observation_fields(self, key: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        request_id = self.provider_request_id
        if not self._safe_request_id(request_id, key, messages):
            request_id = None
        return {
            "http_status": self.http_status,
            "provider_request_id": request_id,
            "request_body_bytes": self.request_body_bytes,
            "streaming": self.streaming,
            "response_bytes_received": self.response_bytes_received,
            "response_chunks_received": self.response_chunks_received,
            "response_completed": self.response_completed,
            "headers_after_ms": self.headers_after_ms,
            "first_byte_after_ms": self.first_byte_after_ms,
            "last_byte_after_ms": self.last_byte_after_ms,
            "sse_events": self.sse_events,
            "sse_keepalives": self.sse_keepalives,
            "sse_done": self.sse_done,
            "stream_terminal": self.stream_terminal,
            "sdk_output_accepted": self.sdk_output_accepted,
        }

    @staticmethod
    def _safe_request_id(request_id: str | None, key: str, messages: list[dict[str, str]]) -> bool:
        if not isinstance(request_id, str) or not request_id:
            return False
        return not (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_id)
            or request_id.lower().startswith(("sk-", "bearer", "cookie", "authorization"))
            or (key and key in request_id)
            or any(
                request_id in m["content"] for m in messages if isinstance(m.get("content"), str)
            )
        )

    def failure_fields(
        self, exc: Exception, key: str, messages: list[dict[str, str]]
    ) -> dict[str, Any]:
        try:
            return self._classify(exc, key, messages)
        except Exception:
            # Some SDK exceptions expose lazy attributes. Preserve the original failure
            # and reservation even when their diagnostic information cannot be inspected.
            return {
                **self.observation_fields(key, messages),
                "failure_kind": "diagnostic_capture_failed",
                "exception_type": type(exc).__name__,
                "exception_message": None,
                "root_exception_type": None,
                "root_exception_message": None,
                "exception_chain_types": [type(exc).__name__],
                "http_status": self.http_status,
                "response_body_redacted": None,
                "provider_request_id": None,
                "request_body_bytes": self.request_body_bytes,
                "is_timeout": None,
                "timeout_stage": None,
            }

    def _classify(self, exc: Exception, key: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        chain = _error_chain(exc)
        names = [type(item).__name__ for item in chain]
        root = chain[-1]
        stage = next((_TIMEOUT_STAGES[name] for name in names if name in _TIMEOUT_STAGES), None)
        timed_out = stage is not None or any(isinstance(e, TimeoutError) for e in chain)
        timed_out |= bool({"APITimeoutError", "ModelTimeoutError", "TimeoutException"} & set(names))
        for item in chain:
            if isinstance(item, HTTPError):
                self.observe_response(item.code, item.headers)
        status = self.http_status
        request_id = self.provider_request_id or next(
            (value for e in chain if isinstance(value := getattr(e, "request_id", None), str)),
            None,
        )
        if not self._safe_request_id(request_id, key, messages):
            request_id = None
        kind = "unknown"
        if timed_out:
            kind = "timeout"
        elif status is not None and status >= 400:
            kind = "rate_limit" if status == 429 else "http_error"
        elif {
            "ConnectError",
            "ReadError",
            "WriteError",
            "CloseError",
            "APIConnectionError",
            "ConnectionRefusedError",
            "RemoteProtocolError",
        } & set(names):
            kind = "connection_error"
        elif "IncompleteStreamError" in names:
            kind = "incomplete_stream"
        elif "StreamProtocolError" in names:
            kind = "invalid_stream"
        elif terminal_kind := self._terminal_failure_kind():
            kind = terminal_kind
        elif any(_safe_message(e) in _USAGE_ERRORS for e in chain):
            kind = "invalid_usage"
        elif "UnexpectedModelBehavior" in names:
            kind = "sdk_output_rejected"
        elif isinstance(exc, (ValueError, KeyError)) or status is not None:
            kind = "validation_error" if status is None else "response_processing"
        return {
            **self.observation_fields(key, messages),
            "failure_kind": kind,
            "exception_type": names[0],
            "exception_message": _safe_message(exc),
            "root_exception_type": names[-1],
            "root_exception_message": _safe_message(root),
            "exception_chain_types": names,
            "http_status": status,
            "response_body_redacted": _safe_body(chain)
            if status is not None and status >= 400
            else None,
            "provider_request_id": request_id or None,
            "request_body_bytes": self.request_body_bytes,
            "is_timeout": timed_out if kind != "unknown" else None,
            "timeout_stage": stage,
        }

    def _terminal_failure_kind(self) -> str | None:
        # DONE establishes a protocol boundary, not successful generation. Never
        # infer a reason from event counts, HTTP status, or an SDK default stop.
        receipt = self.stream_terminal
        if not self.streaming or not self.sse_done or receipt is None:
            return None
        reason = receipt.get("finish_reason")
        if receipt.get("metadata_invalid") or reason in (None, "other", "conflicting"):
            return "invalid_stream"
        if reason == "length":
            return "output_exhausted"
        if reason != "stop":
            return "model_terminated"
        if not receipt.get("content_present"):
            return "empty_answer"
        if receipt.get("usage_status") != "valid":
            return "invalid_usage"
        return None


# Carries wire observations through optional transports and into later usage validation.
active_wire: ContextVar[WireMetadata | None] = ContextVar("ctxroute_model_wire", default=None)
