"""Per-call HTTP observations and value-limited model failure records."""

import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast
from urllib.error import HTTPError, URLError

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
}
_SAFE_MESSAGES = _USAGE_ERRORS | {
    "Connection error.",
    "Request timed out.",
    "Incomplete model response",
    "Provider usage exceeded declared guard",
    "Provider price exceeded reservation",
    "Expected fixed probe or two-message context envelope",
    "Unsupported response format",
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
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        if isinstance(current, URLError) and isinstance(current.reason, BaseException):
            current = current.reason
        else:
            current = current.__cause__ or current.__context__
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

    def observe_response(self, status: int, headers: Any) -> None:
        self.http_status = status
        if headers is not None:
            self.provider_request_id = headers.get("x-request-id") or headers.get("x-ds-trace-id")

    def failure_fields(
        self, exc: Exception, key: str, messages: list[dict[str, str]]
    ) -> dict[str, Any]:
        try:
            return self._classify(exc, key, messages)
        except Exception:
            # Some SDK exceptions expose lazy attributes. Preserve the original failure
            # and reservation even when their diagnostic information cannot be inspected.
            return {
                "failure_kind": "diagnostic_capture_failed",
                "exception_type": type(exc).__name__,
                "exception_message": None,
                "root_exception_type": None,
                "root_exception_message": None,
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
        if request_id and (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_id)
            or request_id.lower().startswith(("sk-", "bearer", "cookie", "authorization"))
            or (key and key in request_id)
            or any(
                request_id in m["content"] for m in messages if isinstance(m.get("content"), str)
            )
        ):
            request_id = None
        kind = "unknown"
        if timed_out:
            kind = "timeout"
        elif status is not None and status >= 400:
            kind = "rate_limit" if status == 429 else "http_error"
        elif any(_safe_message(e) in _USAGE_ERRORS for e in chain):
            kind = "invalid_usage"
        elif {"ConnectError", "APIConnectionError", "ConnectionRefusedError"} & set(names):
            kind = "connection_error"
        elif isinstance(exc, (ValueError, KeyError)) or status is not None:
            kind = "validation_error" if status is None else "response_processing"
        return {
            "failure_kind": kind,
            "exception_type": names[0],
            "exception_message": _safe_message(exc),
            "root_exception_type": names[-1],
            "root_exception_message": _safe_message(root),
            "http_status": status,
            "response_body_redacted": _safe_body(chain)
            if status is not None and status >= 400
            else None,
            "provider_request_id": request_id or None,
            "request_body_bytes": self.request_body_bytes,
            "is_timeout": timed_out if kind != "unknown" else None,
            "timeout_stage": stage,
        }


# Carries wire observations through optional transports and into later usage validation.
active_wire: ContextVar[WireMetadata | None] = ContextVar("ctxroute_model_wire", default=None)
