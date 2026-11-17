"""Synthetic, network-blocked regressions for replay evidence; never benchmark data."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_model_transport import offline_http

from ctxroute.domain import ModelRequest
from ctxroute.models.base import ModelFailure
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.models.output_audit import TextAudit
from ctxroute.models.pydantic_adapter import pydantic_transport
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
CONTENT = '{"open_questions":["synthetic accepted memory"]}'


@pytest.mark.parametrize("buffered", [True, False])
@pytest.mark.parametrize("finish", ["stop", "length"])
def test_nonstream_sdk_preserves_summary_and_request_without_bypassing_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, buffered: bool, finish: str
) -> None:
    httpx = pytest.importorskip("httpx")
    calls: list[dict[str, Any]] = []
    body = json.dumps(
        {
            "id": "synthetic-compression",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {
                        "role": "assistant",
                        "content": CONTENT,
                        "reasoning_content": "synthetic reasoning",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 30,
                "total_tokens": 50,
                "prompt_cache_hit_tokens": 10,
                "prompt_cache_miss_tokens": 10,
            },
        }
    ).encode()

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(body), 17):
                yield body[start : start + 17]

    def handle(request: Any) -> Any:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            **({"content": body} if buffered else {"stream": Fragments()}),
        )

    offline_http(monkeypatch, handle)
    writer = RunWriter(tmp_path, "summary-audit", {"kind": "synthetic_test"})
    ledger = Ledger(
        tmp_path / "ledger.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key="synthetic-api-key",
        prices=json.loads((ROOT / "data/prices/2026-11-17.json").read_text()),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking="disabled",
        transport=pydantic_transport,
    )
    request = ModelRequest(
        (
            {"role": "system", "content": "synthetic summary"},
            {"role": "user", "content": "history"},
        ),
        "cheap",
        "compression",
        4096,
        10,
        "summary-audit",
        8,
        Decimal("0.65"),
        "json_object",
    )
    if finish == "stop":
        assert model.complete(request).text == CONTENT
        assert ledger.snapshot().reserved == 0
    else:
        with pytest.raises(ModelFailure):
            model.complete(request)
    assert len(calls) == 1
    audit = json.loads(next((writer.path / "response_audits").glob("*.json")).read_text())
    assert audit["content"]["text"] == CONTENT and audit["content"]["capture_complete"]
    assert audit["reasoning"]["text"] == "synthetic reasoning"
    saved = json.loads(next((writer.path / "request_audits").glob("*.json")).read_text())
    assert saved["replayable"] and saved["purpose"] == "compression" and saved["step"] == 8
    for name in ["messages", "model", "max_tokens", "temperature", "thinking", "response_format"]:
        assert saved["payload"][name] == calls[0][name]
    assert saved["payload"]["stream"] is False


def test_absent_capture_is_not_reported_as_complete_empty_output() -> None:
    audit = TextAudit()
    assert not audit.record("")["capture_complete"]
    audit.add("")
    assert audit.record("")["capture_complete"]


@pytest.mark.parametrize("audit_unavailable", [False, True])
def test_request_evidence_redaction_and_capture_failure_do_not_authorize_spending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_unavailable: bool
) -> None:
    calls: list[dict[str, Any]] = []
    key = 'synthetic-"private-key'

    def transport(payload: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
        calls.append(payload)
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": CONTENT}}],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 30,
                "total_tokens": 50,
                "prompt_cache_hit_tokens": 10,
                "prompt_cache_miss_tokens": 10,
            },
        }

    writer = RunWriter(tmp_path, "request-audit", {"kind": "synthetic_test"})
    ledger = Ledger(
        tmp_path / "ledger.sqlite", hard_limit=Decimal(10), pause_at=Decimal(8), enabled=True
    )
    model = DeepSeekModel(
        key=key,
        prices=json.loads((ROOT / "data/prices/2026-11-17.json").read_text()),
        ledger=ledger,
        writer=writer,
        input_guard_tokens=100,
        thinking="disabled",
        transport=transport,
    )
    request = ModelRequest(
        ({"role": "user", "content": "synthetic prompt " + key},),
        "cheap",
        "compression",
        4096,
        10,
        "request-audit",
        8,
        Decimal("0.65"),
        "json_object",
    )
    if audit_unavailable:

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(writer, "request_snapshot", unavailable)
        with pytest.raises(ModelFailure, match="no request dispatched") as failure:
            model.complete(request)
        assert not failure.value.retryable and calls == []
        assert ledger.snapshot().spent == ledger.snapshot().reserved == 0
    else:
        assert model.complete(request).text == CONTENT
        saved = json.loads(next((writer.path / "request_audits").glob("*.json")).read_text())
        assert not saved["replayable"]
        assert saved["payload"]["messages"][0]["content"] == "synthetic prompt [REDACTED]"
        assert calls[0]["messages"][0]["content"].endswith(key)
        audit = json.loads(next((writer.path / "response_audits").glob("*.json")).read_text())
        assert audit["content"]["text"] == CONTENT and audit["content"]["capture_complete"]
