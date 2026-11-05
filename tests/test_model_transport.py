"""Offline wire test through installed SDKs; no real key, socket, or provider call."""

import asyncio
import gzip
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
from ctxroute.config import ArmConfig
from ctxroute.domain import ModelRequest, Task
from ctxroute.models.base import ModelFailure
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.models.diagnostics import WireMetadata, active_wire
from ctxroute.models.pydantic_adapter import pydantic_transport
from ctxroute.models.streaming import StreamTracker
from ctxroute.orchestrator.engine import Engine
from ctxroute.sandbox.fake import FakeSandbox
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


def test_failed_diagnostic_capture_never_authorizes_retry_for_http_503(tmp_path: Path) -> None:
    class Uninspectable(RuntimeError):
        @property
        def body(self) -> str:
            raise RuntimeError("private detail")

    def transport(payload: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
        wire = active_wire.get()
        assert wire is not None
        wire.observe_response(503, {})
        raise Uninspectable()

    writer = RunWriter(tmp_path, "diagnostic-stop", {"kind": "synthetic_test"})
    ledger = Ledger(
        tmp_path / "ledger.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key="fake",
        prices=json.loads(
            (Path(__file__).resolve().parents[1] / "data/prices/2026-10-24.json").read_text()
        ),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking="enabled",
        transport=transport,
    )
    with pytest.raises(ModelFailure) as raised:
        model.complete(
            ModelRequest(
                ({"role": "user", "content": "fixture"},),
                "strong",
                "action",
                32,
                1,
                "diagnostic-stop",
                1,
                Decimal("0.20"),
            )
        )
    assert raised.value.retryable is False
    failed = json.loads((writer.path / "requests.jsonl").read_text().splitlines()[-1])
    assert failed["failure_kind"] == "diagnostic_capture_failed"
    assert failed["http_status"] == 503 and ledger.snapshot().reserved > 0


def sse_body(
    text: str, *, done: bool = True, measured: bool = True, finish_reason: str = "stop"
) -> bytes:
    def event(delta: dict[str, Any], finish: str | None = None) -> bytes:
        obj: dict[str, Any] = {
            "id": "offline-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if finish and measured:
            obj["usage"] = {
                "prompt_tokens": 20,
                "completion_tokens": 8,
                "total_tokens": 28,
                "prompt_cache_hit_tokens": 5,
                "prompt_cache_miss_tokens": 15,
            }
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\r\n\r\n"

    return (
        b": keep-alive\r\n\r\n"
        + event({"role": "assistant", "reasoning_content": "private-reasoning-fixture"})
        + event({"content": text[:5]})
        + event({"content": text[5:]})
        + event({}, finish_reason)
        + (b"data: [DONE]\r\n\r\n" if done else b"")
    )


@pytest.mark.parametrize(
    "case", ["complete", "gzip", "disconnect", "missing_done", "missing_usage", "empty_stream"]
)
def test_stream_requires_completion_and_usage_and_preserves_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    httpx = pytest.importorskip("httpx")
    captured: list[dict[str, Any]] = []
    action = '{"tool":"bash","command":"printf café"}'
    body = sse_body(action, done=case != "missing_done", measured=case != "missing_usage")
    if case == "empty_stream":
        body = b": keep-alive\r\n\r\n"
    if case == "gzip":
        body = gzip.compress(body)

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(body), 7):
                if case == "disconnect" and start > 80:
                    raise httpx.RemoteProtocolError("private-reasoning-fixture incomplete body")
                yield body[start : start + 7]

    def handle(request: Any) -> Any:
        captured.append(json.loads(request.content))
        headers = {"content-type": "text/event-stream", "x-request-id": "req-stream-test"}
        if case == "gzip":
            headers["content-encoding"] = "gzip"
        return httpx.Response(200, headers=headers, stream=Fragments())

    offline_http(monkeypatch, handle)
    writer = RunWriter(tmp_path, "stream-test", {"kind": "synthetic_test"})
    ledger = Ledger(
        tmp_path / "budget.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key="fake-stream-key",
        prices=json.loads(
            (Path(__file__).resolve().parents[1] / "data/prices/2026-10-24.json").read_text()
        ),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking="enabled",
        transport=pydantic_transport,
        stream=True,
    )
    request = ModelRequest(
        ({"role": "user", "content": "Synthetic JSON action"},),
        "strong",
        "action",
        32,
        10,
        "stream-test",
        1,
        Decimal("0.20"),
        response_format="json_object",
    )
    if case in ("complete", "gzip"):
        assert model.complete(request).text == action
        assert ledger.snapshot().spent > 0 and ledger.snapshot().reserved == 0
    else:
        with pytest.raises(ModelFailure) as raised:
            model.complete(request)
        assert raised.value.retryable is (case in ("disconnect", "missing_done", "empty_stream"))
        assert ledger.snapshot().spent == 0 and ledger.snapshot().reserved > 0
    raw = (writer.path / "requests.jsonl").read_text()
    events = [json.loads(line) for line in raw.splitlines()]
    last = events[-1]
    assert len(captured) == 1 and captured[0]["stream"] is True
    assert captured[0]["stream_options"] == {"include_usage": True}
    assert captured[0]["max_tokens"] == 32 and captured[0]["thinking"] == {"type": "enabled"}
    assert last["http_status"] == 200 and last["provider_request_id"] == "req-stream-test"
    assert last["streaming"] is True and last["response_bytes_received"] > 0
    assert last["response_chunks_received"] > 0 and last["first_byte_after_ms"] is not None
    assert last["sse_keepalives"] == 1
    assert last["sse_done"] is (case in ("complete", "gzip", "missing_usage"))
    for secret in ("fake-stream-key", "private-reasoning-fixture", "Synthetic JSON action"):
        assert secret not in raw


@pytest.mark.parametrize("guard_tokens", [100, 100_000])
def test_interrupted_stream_retries_once_with_separate_retained_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_tokens: int
) -> None:
    httpx = pytest.importorskip("httpx")
    calls = 0

    def handle(request: Any) -> Any:
        nonlocal calls
        calls += 1
        # A plausible complete action without DONE must never be executed.
        text = '{"tool":"bash","command":"touch partial"}' if calls == 1 else '{"tool":"submit"}'
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": f"req-{calls}"},
            content=sse_body(text, done=calls != 1),
        )

    offline_http(monkeypatch, handle)
    root = Path(__file__).resolve().parents[1]
    writer = RunWriter(tmp_path, "stream-retry", {"kind": "synthetic_test"})
    ledger = Ledger(
        tmp_path / "budget.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key="fake-key",
        prices=json.loads((root / "data/prices/2026-10-24.json").read_text()),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=guard_tokens,
        thinking="enabled",
        transport=pydantic_transport,
        stream=True,
    )
    sandbox = FakeSandbox()
    result = Engine(
        ArmConfig.load(root / "configs/arms/A1.yaml"),
        model,
        sandbox,
        writer,
        model_attempts=2,
        retry_transient_only=True,
        sleep=lambda _: None,
    ).run(Task("synthetic-stream", "fake/repo", "fixture", "Return JSON"), 1)
    assert sandbox.commands == [] and sandbox.destroyed
    records = [
        json.loads(line) for line in (writer.path / "requests.jsonl").read_text().splitlines()
    ]
    if guard_tokens == 100:
        assert result.state.termination == "submitted" and calls == 2
        assert [r["event"] for r in records] == ["reserved", "failed", "reserved", "usage"]
        assert records[0]["request_id"] != records[2]["request_id"]
        assert ledger.snapshot().spent > 0
    else:
        assert result.state.termination == "budget_limit" and calls == 1
        assert [r["event"] for r in records] == ["reserved", "failed"]
        assert ledger.snapshot().spent == 0
    assert ledger.snapshot().reserved == Decimal(records[0]["reserved_usd"])


def test_stream_keepalive_cannot_extend_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx = pytest.importorskip("httpx")

    class KeepAlive(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": keep-alive\n\n"
                await asyncio.sleep(0.01)

    offline_http(
        monkeypatch,
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=KeepAlive()
        ),
    )
    wire = WireMetadata()
    token = active_wire.set(wire)
    try:
        with pytest.raises(TimeoutError):
            pydantic_transport(
                {
                    "model": "deepseek-v4-pro",
                    "thinking": {"type": "enabled"},
                    "messages": [{"role": "user", "content": "fixture"}],
                    "max_tokens": 32,
                    "temperature": 0,
                    "stream": True,
                },
                "fake-key",
                1,
            )
    finally:
        active_wire.reset(token)
    assert wire.sse_keepalives > 1 and wire.sse_done is False


@pytest.mark.parametrize("finish_reason", ["length", "stop"])
def test_thinking_only_sdk_failure_keeps_terminal_receipt_and_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish_reason: str
) -> None:
    httpx = pytest.importorskip("httpx")
    body = sse_body("", finish_reason=finish_reason)
    calls: list[bytes] = []

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(body), 7):
                yield body[start : start + 7]

    def handle(request: Any) -> Any:
        calls.append(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "req-thinking-only"},
            stream=Fragments(),
        )

    offline_http(monkeypatch, handle)
    writer = RunWriter(tmp_path, "thinking-only", {"kind": "synthetic_test"})
    ledger = Ledger(
        tmp_path / "ledger.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    root = Path(__file__).resolve().parents[1]
    model = DeepSeekModel(
        key="synthetic-key",
        prices=json.loads((root / "data/prices/2026-10-24.json").read_text()),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking="enabled",
        transport=pydantic_transport,
        stream=True,
    )
    sandbox = FakeSandbox()
    result = Engine(
        ArmConfig.load(root / "configs/arms/A1.yaml"),
        model,
        sandbox,
        writer,
        model_attempts=2,
        retry_transient_only=True,
        sleep=lambda _: pytest.fail("Definitive SDK failure must not retry"),
    ).run(Task("synthetic-thinking", "fake/repo", "fixture", "Return JSON"), 1)
    assert result.state.termination == "model_failure"
    assert sandbox.commands == [] and sandbox.destroyed and len(calls) == 1
    raw = (writer.path / "requests.jsonl").read_text()
    reserved, failed = map(json.loads, raw.splitlines())
    assert failed["exception_type"] == "UnexpectedModelBehavior"
    assert failed["failure_kind"] == "response_processing"
    assert failed["http_status"] == 200 and failed["provider_request_id"] == "req-thinking-only"
    assert failed["sse_done"] is True
    assert failed["stream_terminal"] == {
        "finish_reason": finish_reason,
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "total_tokens": 28,
            "prompt_cache_hit_tokens": 5,
            "prompt_cache_miss_tokens": 15,
        },
        "usage_status": "valid",
        "content_present": False,
        "reasoning_present": True,
        "metadata_invalid": False,
    }
    assert ledger.snapshot().spent == 0
    assert ledger.snapshot().reserved == Decimal(reserved["reserved_usd"])
    assert all(value not in raw for value in ("synthetic-key", "private-reasoning-fixture"))


@pytest.mark.parametrize(
    "case", ["invalid_usage", "conflicting_usage", "untrusted_finish", "bad_json"]
)
def test_stream_receipt_filters_untrusted_fields_and_preserves_validation_errors(case: str) -> None:
    secret = "private-text-not-a-diagnostic"
    tracker = StreamTracker()
    tracker.feed(sse_body(secret, done=False))
    event: dict[str, Any] = {"untrusted": secret}
    if case in ("invalid_usage", "conflicting_usage"):
        event["usage"] = {
            "prompt_tokens": 20,
            "completion_tokens": 9,
            "total_tokens": 29,
            "prompt_cache_hit_tokens": 5,
            "prompt_cache_miss_tokens": 15,
            "untrusted": secret,
        }
        if case == "invalid_usage":
            event["usage"]["completion_tokens"] = secret
    elif case == "untrusted_finish":
        event["choices"] = [{"index": 0, "finish_reason": secret}]
    body = secret.encode() if case == "bad_json" else json.dumps(event).encode()
    tracker.feed(b"data: " + body + b"\n\ndata: [DONE]\n\n")
    fields = tracker.terminal_fields()
    assert tracker.done and secret not in json.dumps(fields)
    if case.endswith("usage"):
        assert fields["usage"] is None
        assert fields["usage_status"] == ("invalid" if case == "invalid_usage" else "conflicting")
    else:
        assert fields["metadata_invalid"]
