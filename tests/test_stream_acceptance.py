"""Acceptance regressions through the real SDK stack with sockets disabled."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_model_transport import offline_http

from ctxroute.config import ArmConfig
from ctxroute.domain import Task
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.models.pydantic_adapter import pydantic_transport
from ctxroute.orchestrator.engine import Engine, RunResult
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
ACTION = '{"tool":"bash","command":"record-fixture"}'
USAGE = {
    "prompt_tokens": 20,
    "completion_tokens": 8,
    "total_tokens": 28,
    "prompt_cache_hit_tokens": 5,
    "prompt_cache_miss_tokens": 15,
}


def event(
    delta: dict[str, Any], finish: str | None = None, usage: dict[str, Any] | None = None
) -> bytes:
    value: dict[str, Any] = {
        "id": "offline-acceptance",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        value["usage"] = usage
    return b"data: " + json.dumps(value).encode() + b"\n\n"


def body(case: str, text: str) -> bytes:
    usage = dict(USAGE)
    if case == "invalid_total":
        usage["total_tokens"] += 1
    elif case == "invalid_cache":
        usage["prompt_cache_miss_tokens"] += 1
    elif case == "negative_usage":
        usage["completion_tokens"] = -1
        usage["total_tokens"] = 19
    elif case == "bool_usage":
        usage["prompt_cache_hit_tokens"] = True
    finish = None if case == "missing_finish" else "stop"
    if case in {"partial_length", "full_action_length", "thinking_length"}:
        finish = "length"
    if case == "partial_length":
        text = '{"tool":"bash","command":"record-fixture'
    elif case in {"thinking_stop", "thinking_length"}:
        text = ""
    elif case == "malformed_action":
        text = '{"tool":"bash","command":7}'
    data = event({"role": "assistant", "reasoning_content": "private-thinking-fixture"})
    data += event({"content": text})
    if case == "conflicting_finish":
        data += event({}, "length")
    data += event({}, finish, None if case in {"missing_usage", "separate_usage"} else usage)
    if case == "separate_usage":
        data += b"data: " + json.dumps({"choices": [], "usage": usage}).encode() + b"\n\n"
    elif case == "duplicate_usage":
        data += event({}, usage=usage)
    if case == "conflicting_usage":
        changed = {**USAGE, "completion_tokens": 9, "total_tokens": 29}
        data += event({}, usage=changed)
    return data + (b"" if case == "missing_done" else b"data: [DONE]\n\n")


def run_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> tuple[RunResult, FakeSandbox, Ledger, RunWriter, list[dict[str, Any]]]:
    httpx = pytest.importorskip("httpx")
    calls: list[dict[str, Any]] = []

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = body(case, ACTION if len(calls) == 1 else '{"tool":"submit"}')
            for start in range(0, len(data), 13):
                if case == "disconnect" and start > 80:
                    raise httpx.RemoteProtocolError("private-network-detail")
                yield data[start : start + 13]

    def handle(request: Any) -> Any:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "req-acceptance"},
            stream=Fragments(),
        )

    offline_http(monkeypatch, handle)
    writer = RunWriter(tmp_path, "acceptance", {"kind": "synthetic_test"})
    ledger = Ledger(
        tmp_path / "ledger.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key="private-key-fixture",
        prices=json.loads((ROOT / "data/prices/2026-10-24.json").read_text()),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking="enabled",
        transport=pydantic_transport,
        stream=True,
    )
    sandbox = FakeSandbox()
    result = Engine(
        ArmConfig.load(ROOT / "configs/arms/A1.yaml"),
        model,
        sandbox,
        writer,
        model_attempts=1,
        retry_transient_only=True,
        sleep=lambda _: pytest.fail("Fixture must not retry"),
    ).run(Task("acceptance", "fake/repo", "fixture", "private-prompt-fixture"), 1)
    return result, sandbox, ledger, writer, calls


def records(writer: RunWriter, name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (writer.path / f"{name}.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("case", ["normal", "separate_usage"])
def test_normal_stream_delivers_exactly_one_validated_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    result, sandbox, ledger, writer, calls = run_fixture(tmp_path, monkeypatch, case)
    assert result.state.termination == "submitted"
    assert sandbox.commands == ["record-fixture"] and sandbox.destroyed
    assert len(calls) == 2 and all(call["stream"] is True for call in calls)
    assert [item["event"] for item in records(writer, "requests")] == [
        "reserved",
        "usage",
        "reserved",
        "usage",
    ]
    assert ledger.snapshot().spent > 0 and ledger.snapshot().reserved == 0


@pytest.mark.parametrize(
    ("case", "kind", "reserved"),
    [
        ("thinking_length", "output_exhausted", True),
        ("thinking_stop", "empty_answer", True),
        ("partial_length", "output_exhausted", False),
        ("full_action_length", "output_exhausted", False),
        ("missing_finish", "invalid_stream", True),
        ("conflicting_finish", "invalid_stream", True),
        ("missing_usage", "invalid_usage", True),
        ("invalid_total", "invalid_usage", True),
        ("invalid_cache", "invalid_usage", True),
        ("negative_usage", "invalid_usage", True),
        ("bool_usage", "invalid_usage", True),
        ("conflicting_usage", "invalid_usage", True),
        ("duplicate_usage", "invalid_usage", True),
        ("missing_done", "incomplete_stream", True),
        ("disconnect", "connection_error", True),
    ],
)
def test_rejected_stream_never_delivers_an_action_or_loses_unknown_charge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    kind: str,
    reserved: bool,
) -> None:
    result, sandbox, ledger, writer, calls = run_fixture(tmp_path, monkeypatch, case)
    assert result.state.termination == "model_failure"
    assert sandbox.commands == [] and sandbox.destroyed
    assert len(calls) == 1
    requests = records(writer, "requests")
    failed = requests[-1]
    assert failed["event"] == "failed" and failed["failure_kind"] == kind
    assert failed["http_status"] == 200 and failed["is_timeout"] is False
    assert failed["sse_done"] is (case not in {"missing_done", "disconnect"})
    if reserved:
        assert ledger.snapshot().spent == 0
        assert ledger.snapshot().reserved == Decimal(requests[0]["reserved_usd"])
        assert not any(item["event"] == "usage" for item in requests)
    else:
        assert ledger.snapshot().spent > 0 and ledger.snapshot().reserved == 0
        assert sum(item["event"] == "usage" for item in requests) == 1
    request_log = (writer.path / "requests.jsonl").read_text()
    for secret in (
        "private-key-fixture",
        "private-thinking-fixture",
        "private-prompt-fixture",
        "private-network-detail",
        "record-fixture",
    ):
        assert secret not in request_log


def test_completed_malformed_action_is_engine_format_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, sandbox, ledger, writer, calls = run_fixture(tmp_path, monkeypatch, "malformed_action")
    assert result.state.termination == "format_failure"
    assert sandbox.commands == [] and sandbox.destroyed and len(calls) == 2
    assert not any(item["event"] == "failed" for item in records(writer, "requests"))
    assert all(item["error_type"] == "format_error" for item in records(writer, "traces"))
    assert ledger.snapshot().spent > 0 and ledger.snapshot().reserved == 0
