"""SDK/wire parity regressions with sockets disabled and synthetic SSE only."""

import json
import socket
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ctxroute.config import ArmConfig
from ctxroute.domain import ModelRequest, Task
from ctxroute.models.base import ModelFailure
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.models.diagnostics import WireMetadata
from ctxroute.models.pydantic_adapter import pydantic_transport
from ctxroute.models.streaming import StreamTracker
from ctxroute.orchestrator.engine import Engine
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.writer import RunWriter

_ACTION = '{"tool":"bash","command":"printf fixture"}'
_USAGE = {
    "prompt_tokens": 20,
    "completion_tokens": 8,
    "total_tokens": 28,
    "prompt_cache_hit_tokens": 5,
    "prompt_cache_miss_tokens": 15,
}


def _event(delta: dict[str, Any], *, finish: str | None = None, usage: bool = False) -> bytes:
    value: dict[str, Any] = {
        "id": "offline-receipt",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        value["usage"] = dict(_USAGE)
    return b"data: " + json.dumps(value).encode() + b"\n\n"


def _valid_body() -> bytes:
    return (
        _event({"role": "assistant", "content": _ACTION})
        + _event({}, finish="stop", usage=True)
        + b"data: [DONE]\n\n"
    )


def _wire_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes, *, fragment_size: int = 7
) -> tuple[DeepSeekModel, ModelRequest, Ledger, RunWriter, list[dict[str, Any]]]:
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("openai")
    calls: list[dict[str, Any]] = []

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for offset in range(0, len(body), fragment_size):
                yield body[offset : offset + fragment_size]

    def handle(request: Any) -> Any:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragments()
        )

    original_init = httpx.AsyncClient.__init__

    def offline_init(self: Any, **kwargs: Any) -> None:
        original_init(self, transport=httpx.MockTransport(handle), **kwargs)

    def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Synthetic SDK test attempted network access")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", offline_init)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    root = Path(__file__).resolve().parents[1]
    writer = RunWriter(tmp_path, "receipt", {"kind": "synthetic_test", "is_benchmark": False})
    ledger = Ledger(
        tmp_path / "ledger.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key="synthetic-receipt-key",
        prices=json.loads((root / "data/prices/2026-10-24.json").read_text()),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking="enabled",
        transport=pydantic_transport,
        stream=True,
    )
    request = ModelRequest(
        ({"role": "user", "content": "Synthetic fixture"},),
        "strong",
        "action",
        32,
        10,
        "receipt",
        1,
        Decimal("0.20"),
        response_format="json_object",
    )
    return model, request, ledger, writer, calls


def test_duplicate_usage_cannot_double_settle_or_deliver_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _valid_body().replace(b"data: [DONE]", _event({}, usage=True) + b"data: [DONE]")
    model, request, ledger, writer, calls = _wire_model(tmp_path, monkeypatch, body)
    with pytest.raises(ModelFailure) as raised:
        model.complete(request)
    assert not raised.value.retryable and len(calls) == 1
    reserved, failed = map(json.loads, (writer.path / "requests.jsonl").read_text().splitlines())
    assert failed["failure_kind"] == "invalid_usage"
    assert failed["stream_terminal"]["usage"] == _USAGE
    assert ledger.snapshot().spent == 0
    assert ledger.snapshot().reserved == Decimal(reserved["reserved_usd"])


def test_sdk_rejecting_completed_valid_text_never_executes_or_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = pytest.importorskip("pydantic_ai")
    results = pytest.importorskip("pydantic_ai.result")
    model, _, ledger, writer, calls = _wire_model(tmp_path, monkeypatch, _valid_body())
    original_get_output = results.StreamedRunResult.get_output

    async def reject_completed_output(self: Any) -> str:
        await original_get_output(self)
        raise sdk.UnexpectedModelBehavior("private-rejection-fixture")

    monkeypatch.setattr(results.StreamedRunResult, "get_output", reject_completed_output)
    sandbox = FakeSandbox()
    root = Path(__file__).resolve().parents[1]
    outcome = Engine(
        ArmConfig.load(root / "configs/arms/A1.yaml"),
        model,
        sandbox,
        writer,
        model_attempts=2,
        retry_transient_only=True,
        sleep=lambda _: pytest.fail("SDK output rejection must not retry"),
    ).run(Task("synthetic-rejection", "fake/repo", "fixture", "Return JSON"), 1)
    assert outcome.state.termination == "model_failure"
    assert sandbox.commands == [] and sandbox.destroyed and len(calls) == 1
    raw = (writer.path / "requests.jsonl").read_text()
    reserved, failed = map(json.loads, raw.splitlines())
    assert failed["failure_kind"] == "sdk_output_rejected"
    assert failed["sse_done"] and failed["stream_terminal"]["finish_reason"] == "stop"
    assert failed["stream_terminal"]["content_present"]
    assert ledger.snapshot().spent == 0
    assert ledger.snapshot().reserved == Decimal(reserved["reserved_usd"])
    assert "private-rejection-fixture" not in raw and "synthetic-receipt-key" not in raw


@pytest.mark.parametrize(
    "exception_name,kind", [("ReadError", "connection_error"), ("ReadTimeout", "timeout")]
)
def test_exception_group_preserves_nested_network_failure(exception_name: str, kind: str) -> None:
    httpx = pytest.importorskip("httpx")
    error = getattr(httpx, exception_name)("private-network-fixture")
    group = ExceptionGroup(
        "private-group-fixture",
        [ValueError("private-unrelated-fixture"), ExceptionGroup("nested", [error])],
    )
    fields = WireMetadata(http_status=200, streaming=True).failure_fields(group, "fake", [])
    assert fields["failure_kind"] == kind
    assert fields["is_timeout"] is (kind == "timeout")
    assert "private" not in json.dumps(fields)


@pytest.mark.parametrize("fragment_size", [1, 7, 100_000])
def test_cr_only_sse_matches_sdk_and_preserves_valid_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fragment_size: int
) -> None:
    body = _valid_body().replace(b"\n", b"\r")
    model, request, ledger, writer, calls = _wire_model(
        tmp_path, monkeypatch, body, fragment_size=fragment_size
    )
    assert model.complete(request).text == _ACTION
    assert len(calls) == 1 and ledger.snapshot().spent > 0 and ledger.snapshot().reserved == 0
    record = json.loads((writer.path / "requests.jsonl").read_text().splitlines()[-1])
    assert record["sse_done"] and record["stream_terminal"]["finish_reason"] == "stop"
    assert record["stream_terminal"]["usage"] == _USAGE


@pytest.mark.parametrize("fragment_size", [1, 7, 100_000])
def test_first_done_freezes_receipt_independently_of_chunk_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fragment_size: int
) -> None:
    # OpenAI stops decoding at the first DONE. An observer must not include a trailer
    # merely because HTTPX happened to deliver it in the same byte chunk.
    body = _valid_body() + _event({"content": "private-trailer"}, finish="length", usage=True)
    tracker = StreamTracker()
    for offset in range(0, len(body), fragment_size):
        tracker.feed(body[offset : offset + fragment_size])
    assert tracker.done and tracker.events == 3
    assert tracker.terminal_fields()["finish_reason"] == "stop"
    assert not tracker.terminal_fields()["metadata_invalid"]
    model, request, ledger, _, calls = _wire_model(
        tmp_path, monkeypatch, body, fragment_size=fragment_size
    )
    assert model.complete(request).text == _ACTION
    assert len(calls) == 1 and ledger.snapshot().spent > 0 and ledger.snapshot().reserved == 0
