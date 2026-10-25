"""Offline wire test through installed SDKs; no real key, socket, or provider call."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest

from ctxroute.domain import ModelRequest
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.models.pydantic_adapter import pydantic_transport
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.writer import RunWriter


def test_sdk_preserves_output_mode_raw_text_usage_and_single_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                    "total_tokens": 28,
                    "prompt_cache_hit_tokens": 5,
                    "prompt_cache_miss_tokens": 15,
                },
            },
        )

    original_init = httpx.AsyncClient.__init__

    def offline_init(self: Any, **kwargs: Any) -> None:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        original_init(self, transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", offline_init)
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
        thinking="disabled",
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
        assert captured[i]["thinking"] == {"type": "disabled"}
        assert captured[i]["max_completion_tokens"] == 32
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
