"""Offline wire test through installed SDKs; no real key, socket, or provider call."""

import json
import socket
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from email.message import Message
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError

import pytest

from ctxroute.api_probe import http_transport
from ctxroute.domain import ModelRequest
from ctxroute.models.base import ModelFailure
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.models.diagnostics import WireMetadata, active_wire
from ctxroute.models.pydantic_adapter import pydantic_transport
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.writer import RunWriter


def offline_http(monkeypatch: pytest.MonkeyPatch, handle: Callable[[Any], Any]) -> None:
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("openai")
    original_init = httpx.AsyncClient.__init__

    def offline_init(self: Any, **kwargs: Any) -> None:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        original_init(self, transport=httpx.MockTransport(handle), **kwargs)

    def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("The SDK attempted to access the network")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", offline_init)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)


@pytest.mark.parametrize("thinking", ["disabled", "enabled"])
def test_sdk_preserves_output_mode_raw_text_usage_and_single_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thinking: str
) -> None:
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("openai")
    captured: list[dict[str, Any]] = []
    # The SDK must return malformed text unchanged so Engine owns the one correction.
    outputs = ['{"tool":"submit"}}', "native baseline text"]

    def handle(request: Any) -> Any:
        captured.append(json.loads(request.content))
        assert request.url.host == "api.deepseek.com"
        return httpx.Response(
            200,
            json={
                "id": "offline-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": outputs[len(captured) - 1]},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 8,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                    "total_tokens": 28,
                    "prompt_cache_hit_tokens": 5,
                    "prompt_cache_miss_tokens": 15,
                },
            },
        )

    offline_http(monkeypatch, handle)
    root = Path(__file__).resolve().parents[1]
    writer = RunWriter(tmp_path, "sdk-fixture", {"kind": "synthetic_test", "is_benchmark": False})
    ledger = Ledger(
        tmp_path / "ledger.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key="offline-fixture-key",
        prices=json.loads((root / "data/prices/2026-10-24.json").read_text()),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking=thinking,
        transport=pydantic_transport,
    )
    modes: tuple[Literal["json_object", "text"], ...] = ("json_object", "text")
    for i, mode in enumerate(modes):
        result = model.complete(
            ModelRequest(
                (
                    {"role": "system", "content": "Return JSON."},
                    {"role": "user", "content": "Fixture"},
                ),
                "strong",
                "action",
                32,
                10,
                "sdk-fixture",
                i + 1,
                Decimal("0.20"),
                response_format=mode,
            )
        )
        assert len(captured) == i + 1
        assert result.text == outputs[i]
        assert result.usage.input_tokens == 20 and result.usage.cached_tokens == 5
        assert result.usage.output_tokens == 8
        assert captured[i]["thinking"] == {"type": thinking}
        assert captured[i]["max_tokens"] == 32
        assert "max_completion_tokens" not in captured[i]
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in captured[1]
    records = [
        json.loads(line) for line in (writer.path / "requests.jsonl").read_text().splitlines()
    ]
    assert [r["response_format"] for r in records if r["event"] == "reserved"] == [
        "json_object",
        "text",
    ]
    assert sum(r["event"] == "usage" for r in records) == 2
    assert not any(r["event"] == "failed" for r in records)


@pytest.mark.parametrize(
    ("case", "kind", "status", "timeout_stage"),
    [
        ("bad_request", "http_error", 400, None),
        ("rate_limit", "rate_limit", 429, None),
        ("unavailable", "http_error", 503, None),
        ("read_timeout", "timeout", None, "read"),
        ("connect_error", "connection_error", None, None),
        ("missing_usage", "invalid_usage", 200, None),
        ("inconsistent_usage", "invalid_usage", 200, None),
        ("local_validation", "validation_error", None, None),
    ],
)
def test_failed_request_records_safe_diagnostics_and_keeps_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    kind: str,
    status: int | None,
    timeout_stage: str | None,
) -> None:
    httpx = pytest.importorskip("httpx")
    key = "sk-do-not-log-this-fixture"
    prompt = "private_prompt_and_source"
    provider_id = "req-offline-0123456789"
    sensitive = f"Authorization: Bearer {key} Cookie: private-cookie {prompt}"
    bodies: list[bytes] = []

    def handle(request: Any) -> Any:
        bodies.append(request.content)
        if case == "read_timeout":
            raise httpx.ReadTimeout(sensitive, request=request)
        if case == "connect_error":
            raise httpx.ConnectError(sensitive, request=request)
        headers = {"x-request-id": provider_id, "Set-Cookie": sensitive}
        if status == 503:
            return httpx.Response(status, headers={"x-request-id": key}, text=sensitive)
        if status in (400, 429):
            code = "rate_limit_exceeded" if status == 429 else "invalid_request_error"
            return httpx.Response(
                status,
                headers=headers,
                json={"error": {"code": code, "type": prompt, "message": sensitive}},
            )
        data: dict[str, Any] = {
            "id": "offline-completion",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": prompt},
                }
            ],
        }
        if case == "inconsistent_usage":
            data["usage"] = {
                "prompt_tokens": 20,
                "completion_tokens": 1,
                "total_tokens": 21,
                "prompt_cache_hit_tokens": 5,
                "prompt_cache_miss_tokens": 16,
            }
        return httpx.Response(200, headers=headers, json=data)

    offline_http(monkeypatch, handle)
    writer = RunWriter(tmp_path, "failed-wire", {"kind": "synthetic_test"})
    ledger = Ledger(
        tmp_path / "ledger.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key=key,
        prices=json.loads(
            (Path(__file__).resolve().parents[1] / "data/prices/2026-10-24.json").read_text()
        ),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking="enabled",
        transport=pydantic_transport,
    )
    role = "assistant" if case == "local_validation" else "user"
    with pytest.raises(ModelFailure):
        model.complete(
            ModelRequest(
                ({"role": role, "content": prompt},),
                "strong",
                "action",
                32,
                10,
                "failed-wire",
                1,
                Decimal("0.20"),
                response_format="json_object",
            )
        )
    raw = (writer.path / "requests.jsonl").read_text()
    reserved, failed = map(json.loads, raw.splitlines())
    assert failed["event"] == "failed" and failed["error_type"] == "model_request_failed"
    assert failed["step"] == 1 and failed["request_id"] == reserved["request_id"]
    assert failed["latency_ms"] >= 0 and failed["ended_at"]
    assert ledger.snapshot().spent == 0 and ledger.snapshot().reserved == Decimal(
        reserved["reserved_usd"]
    )
    assert failed["failure_kind"] == kind and failed["http_status"] == status
    assert failed["exception_type"] and failed["root_exception_type"]
    assert "exception_message" in failed and "root_exception_message" in failed
    assert failed["is_timeout"] is (timeout_stage is not None)
    assert failed["timeout_stage"] == timeout_stage
    assert failed["provider_request_id"] == (provider_id if status in (200, 400, 429) else None)
    assert len(bodies) == (0 if case == "local_validation" else 1)
    assert failed["request_body_bytes"] == (len(bodies[0]) if bodies else None)
    if status in (400, 429):
        assert failed["response_body_redacted"] == {
            "code": "rate_limit_exceeded" if status == 429 else "invalid_request_error"
        }
    elif status == 503:
        assert failed["response_body_redacted"] == {"omitted": "no_safe_structured_fields"}
    else:
        assert failed["response_body_redacted"] is None
    for secret in (key, prompt, "Authorization", "private-cookie", "Set-Cookie"):
        assert secret not in raw


def test_direct_transport_records_status_without_consuming_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"model": "fixture", "messages": [{"role": "user", "content": "private prompt"}]}
    headers = Message()
    headers["X-Request-ID"] = "req-urllib-123"

    def reject(self: Any, request: urllib.request.Request, timeout: int) -> Any:
        assert request.data == json.dumps(payload).encode()
        raise HTTPError(request.full_url, 429, "private error detail", headers, None)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", reject)
    wire = WireMetadata()
    token = active_wire.set(wire)
    try:
        with pytest.raises(HTTPError) as raised:
            http_transport(payload, "fake", 1)
    finally:
        active_wire.reset(token)
    fields = wire.failure_fields(raised.value, "fake", [])
    assert (fields["http_status"], fields["provider_request_id"]) == (429, "req-urllib-123")
    assert fields["request_body_bytes"] == len(json.dumps(payload).encode())
    assert fields["response_body_redacted"] == {"omitted": "no_safe_structured_fields"}
    assert "private" not in json.dumps(fields)


def test_unavailable_exception_metadata_still_returns_safe_fields() -> None:
    class Uninspectable(RuntimeError):
        @property
        def body(self) -> str:
            raise RuntimeError("private detail")

    fields = WireMetadata(http_status=503).failure_fields(Uninspectable(), "fake", [])
    assert fields["failure_kind"] == "diagnostic_capture_failed"
    assert fields["http_status"] == 503 and fields["exception_type"] == "Uninspectable"
    assert fields["is_timeout"] is None and fields["provider_request_id"] is None
    assert "private" not in json.dumps(fields)
