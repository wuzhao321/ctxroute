"""Optional reasoning counts remain evidence, never additional billable output."""

import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ctxroute.models.diagnostics import WireMetadata, active_wire
from ctxroute.models.pydantic_adapter import pydantic_transport
from ctxroute.models.streaming import StreamTracker

_USAGE = {
    "prompt_tokens": 20,
    "completion_tokens": 8,
    "total_tokens": 28,
    "prompt_cache_hit_tokens": 5,
    "prompt_cache_miss_tokens": 15,
}


def _event(value: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(value).encode() + b"\n\n"


def _usage_event(details: Any = None, *, usage: dict[str, int] | None = None) -> bytes:
    counts: dict[str, Any] = dict(_USAGE if usage is None else usage)
    if details is not None:
        counts["completion_tokens_details"] = details
    return _event({"choices": [], "usage": counts})


def _terminal(finish: str | None = "stop") -> bytes:
    return _event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})


@pytest.mark.parametrize(
    "details,tokens,status",
    [
        (None, None, "not_seen"),
        ({}, None, "not_seen"),
        ({"reasoning_tokens": None}, None, "not_seen"),
        ({"reasoning_tokens": 0}, 0, "valid"),
        ({"reasoning_tokens": 5}, 5, "valid"),
        ({"reasoning_tokens": 8}, 8, "valid"),
        ({"reasoning_tokens": True}, None, "invalid"),
        ({"reasoning_tokens": -1}, None, "invalid"),
        ({"reasoning_tokens": 9}, None, "invalid"),
        ({"reasoning_tokens": 5.0}, None, "invalid"),
        ({"reasoning_tokens": "private-detail"}, None, "invalid"),
        ("private-detail", None, "invalid"),
    ],
)
def test_optional_reasoning_detail_does_not_change_billing_receipt(
    details: Any, tokens: int | None, status: str
) -> None:
    tracker = StreamTracker()
    tracker.feed(_terminal() + _usage_event(details) + b"data: [DONE]\n\n")
    fields = tracker.terminal_fields()
    assert fields["reasoning_tokens"] == tokens
    assert fields["reasoning_tokens_status"] == status
    assert fields["usage"] == _USAGE and fields["usage_status"] == "valid"
    assert fields["receipt_status"] == "valid"
    assert tracker.validated_receipt() == ("stop", _USAGE)
    assert "private-detail" not in json.dumps(fields)


@pytest.mark.parametrize("second,status", [(5, "valid"), (3, "conflicting"), (True, "invalid")])
def test_repeated_reasoning_detail_is_never_added_or_repaired(second: Any, status: str) -> None:
    tracker = StreamTracker()
    tracker.feed(
        _terminal()
        + _usage_event({"reasoning_tokens": 5})
        + _usage_event({"reasoning_tokens": second})
        + _usage_event({"reasoning_tokens": 5})
        + b"data: [DONE]\n\n"
    )
    fields = tracker.terminal_fields()
    assert fields["reasoning_tokens_status"] == status
    assert fields["reasoning_tokens"] == (5 if status == "valid" else None)
    assert tracker.validated_receipt() == ("stop", _USAGE)


@pytest.mark.parametrize("main_status", ["invalid", "conflicting"])
def test_unusable_main_usage_removes_prior_reasoning_count(main_status: str) -> None:
    tracker = StreamTracker()
    other = {**_USAGE, "completion_tokens": 7, "total_tokens": 27}
    if main_status == "invalid":
        other["total_tokens"] = 99
    tracker.feed(
        _terminal()
        + _usage_event({"reasoning_tokens": 5})
        + _usage_event({"reasoning_tokens": 5}, usage=other)
        + _usage_event({"reasoning_tokens": 5})
        + b"data: [DONE]\n\n"
    )
    fields = tracker.terminal_fields()
    assert fields["usage_status"] == main_status and fields["usage"] is None
    assert fields["reasoning_tokens"] is None and fields["reasoning_tokens_status"] == "invalid"
    assert fields["receipt_status"] == "invalid_usage"
    with pytest.raises(ValueError, match="invalid_usage"):
        tracker.validated_receipt()


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing_done", "incomplete"),
        ("missing_finish", "invalid_terminal"),
        ("missing_usage", "invalid_usage"),
        ("length", "valid"),
        ("stop", "valid"),
    ],
)
def test_receipt_status_describes_evidence_not_model_success(case: str, status: str) -> None:
    tracker = StreamTracker()
    finish = None if case == "missing_finish" else "length" if case == "length" else "stop"
    body = _terminal(finish)
    if case != "missing_usage":
        body += _usage_event({"reasoning_tokens": 8})
    if case != "missing_done":
        body += b"data: [DONE]\n\n"
    tracker.feed(body)
    assert tracker.terminal_fields()["receipt_status"] == status


@pytest.mark.parametrize("case", ["accepted", "thinking_stop", "thinking_length"])
def test_reasoning_receipt_survives_actual_sdk_acceptance_and_rejection(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    httpx = pytest.importorskip("httpx")
    sdk = pytest.importorskip("pydantic_ai")
    pytest.importorskip("openai")
    finish = "length" if case == "thinking_length" else "stop"
    delta = {"role": "assistant", "reasoning_content": "private-reasoning-fixture"}
    if case == "accepted":
        delta["content"] = '{"tool":"submit"}'

    def chunk(fields: dict[str, Any]) -> bytes:
        return _event(
            {
                "id": "offline-reasoning",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-v4-pro",
                **fields,
            }
        )

    body = (
        chunk({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        + chunk(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {**_USAGE, "completion_tokens_details": {"reasoning_tokens": 5}},
            }
        )
        + b"data: [DONE]\n\n"
    )
    calls: list[bytes] = []

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for offset in range(0, len(body), 7):
                yield body[offset : offset + 7]

    def handle(request: Any) -> Any:
        calls.append(request.content)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragments()
        )

    original_init = httpx.AsyncClient.__init__

    def offline_init(self: Any, **kwargs: Any) -> None:
        original_init(self, transport=httpx.MockTransport(handle), **kwargs)

    def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Synthetic reasoning test attempted network access")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", offline_init)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    wire = WireMetadata()
    token = active_wire.set(wire)
    payload = {
        "messages": [{"role": "user", "content": "Return JSON"}],
        "thinking": {"type": "enabled"},
        "model": "deepseek-v4-pro",
        "max_tokens": 4096,
        "temperature": 0,
        "stream": True,
        "response_format": {"type": "json_object"},
    }
    try:
        if case == "accepted":
            response = pydantic_transport(payload, "fake-key", 10)
            assert response["usage"] == _USAGE
            assert response["choices"][0]["message"]["content"] == '{"tool":"submit"}'
        else:
            with pytest.raises(sdk.UnexpectedModelBehavior) as raised:
                pydantic_transport(payload, "fake-key", 10)
            fields = wire.failure_fields(raised.value, "fake-key", [])
            assert fields["failure_kind"] == (
                "output_exhausted" if finish == "length" else "empty_answer"
            )
            assert fields["stream_terminal"]["reasoning_tokens"] == 5
        assert len(calls) == 1
        assert wire.sdk_output_accepted is (case == "accepted")
        assert wire.stream_terminal is not None
        assert wire.stream_terminal["receipt_status"] == "valid"
        assert wire.stream_terminal["reasoning_tokens"] == 5
        assert wire.stream_terminal["reasoning_tokens_status"] == "valid"
        assert "private-reasoning-fixture" not in json.dumps(
            wire.observation_fields("fake-key", [])
        )
    finally:
        active_wire.reset(token)
