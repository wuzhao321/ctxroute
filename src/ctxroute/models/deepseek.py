"""Budgeted production boundary, never invoked by the offline test/demo entrypoints."""

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ctxroute.api_probe import (
    Transport,
    definitely_off_peak,
    http_transport,
    normalize_usage,
    price_from,
)
from ctxroute.domain import ModelRequest, ModelResponse
from ctxroute.models.base import ModelFailure
from ctxroute.models.diagnostics import WireMetadata, active_wire
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.writer import RunWriter


class DeepSeekModel:
    def __init__(
        self,
        *,
        key: str,
        prices: dict[str, Any],
        ledger: Ledger,
        writer: RunWriter,
        input_guard_tokens: int,
        thinking: str,
        transport: Transport = http_transport,
        stream: bool = False,
    ) -> None:
        if thinking not in {"enabled", "disabled"} or input_guard_tokens < 1:
            raise ValueError("Invalid model settings")
        self.key, self.prices, self.ledger = key, prices, ledger
        self.writer, self.input_guard, self.thinking = writer, input_guard_tokens, thinking
        if type(stream) is not bool or (stream and transport is http_transport):
            raise ValueError("Streaming requires a streaming-capable transport")
        self.transport, self.stream = transport, stream

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.timeout_seconds <= 0:
            raise ModelFailure("No model time allowance remains")
        model = self.prices["models"][request.tier]
        peak = price_from(model["peak"])
        bound = self.ledger.reservation_bound(request, peak, self.input_guard)
        request_id = uuid4().hex
        self.ledger.reserve(
            request_id,
            bound,
            "model_api",
            scope=request.run_id,
            scope_limit=request.scope_limit,
        )
        started = datetime.now(UTC)
        timer = time.monotonic()
        self.writer.append(
            "requests",
            {
                "request_id": request_id,
                "step": request.step,
                "purpose": request.purpose,
                "response_format": request.response_format,
                "tier": request.tier,
                "model_id": model["model_id"],
                "event": "reserved",
                "reserved_usd": str(bound),
                "started_at": started.isoformat(),
                "price_date": self.prices["retrieved_on"],
                "price_source": self.prices["source"],
                "currency": "USD",
            },
        )
        payload = {
            "model": model["model_id"],
            "messages": list(request.messages),
            "max_tokens": request.max_output_tokens,
            "temperature": 0,
            "thinking": {"type": self.thinking},
            "stream": self.stream,
        }
        if request.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        wire = WireMetadata()
        token = active_wire.set(wire)
        try:
            raw = self.transport(payload, self.key, max(1, int(request.timeout_seconds)))
            usage = normalize_usage(raw)
            upper = peak.cost(usage)
            ended = datetime.now(UTC)
            elapsed = (time.monotonic() - timer) * 1000
            self.writer.append(
                "requests",
                {
                    "request_id": request_id,
                    "step": request.step,
                    "purpose": request.purpose,
                    "tier": request.tier,
                    "model_id": model["model_id"],
                    "event": "usage",
                    **wire.observation_fields(self.key, list(request.messages)),
                    "prompt_tokens": usage.input_tokens,
                    "cached_tokens": usage.cached_tokens,
                    "completion_tokens": usage.output_tokens,
                    "latency_ms": elapsed,
                    "cost_upper_usd": str(upper),
                    "cost_lower_usd": str(price_from(model["off_peak"]).cost(usage)),
                    "ended_at": ended.isoformat(),
                    "price_period": "off_peak"
                    if definitely_off_peak(started, ended, self.prices["peak_hours_utc"])
                    else "uncertain",
                },
            )
            if (
                usage.input_tokens > self.input_guard
                or usage.output_tokens > request.max_output_tokens
            ):
                self.ledger.freeze(request_id, upper)
                raise ModelFailure("Provider usage exceeded declared guard")
            if upper > bound:
                self.ledger.freeze(request_id, upper)
                raise ModelFailure("Provider price exceeded reservation")
            if definitely_off_peak(started, ended, self.prices["peak_hours_utc"]):
                self.ledger.settle(request_id, price_from(model["off_peak"]).cost(usage))
            else:
                # Verified usage has a finite peak-price cost even when the period is unknown.
                self.ledger.settle(request_id, upper)
            text = raw["choices"][0]["message"]["content"]
            if not isinstance(text, str) or raw["choices"][0]["finish_reason"] != "stop":
                raise ModelFailure("Incomplete model response")
            return ModelResponse(
                text.replace(self.key, "[REDACTED]"), model["model_id"], usage, elapsed, upper
            )
        except Exception as exc:
            diagnostics = wire.failure_fields(exc, self.key, list(request.messages))
            self.writer.append(
                "requests",
                {
                    "request_id": request_id,
                    "step": request.step,
                    "purpose": request.purpose,
                    "event": "failed",
                    "error_type": "model_request_failed",
                    **diagnostics,
                    "latency_ms": (time.monotonic() - timer) * 1000,
                    "ended_at": datetime.now(UTC).isoformat(),
                },
            )
            raise ModelFailure(
                "Model request failed; inspect numeric usage and reservations",
                retryable=diagnostics["failure_kind"] != "diagnostic_capture_failed"
                and (
                    diagnostics["failure_kind"]
                    in {"connection_error", "timeout", "incomplete_stream", "rate_limit"}
                    or diagnostics["http_status"] in {500, 502, 503, 504}
                ),
            ) from None
        finally:
            try:
                # Preserve partial/length outputs even when no ModelResponse is delivered.
                # Diagnostic evidence cannot bypass the finish/usage/schema gates.
                folder = self.writer.path / "response_audits"
                folder.mkdir(exist_ok=True)
                data = json.dumps(wire.output_audit.record(self.key), ensure_ascii=False).encode()
                used = sum(p.stat().st_size for p in folder.glob("*.json"))
                saved = used + len(data) <= 8_388_608
                if saved:
                    target = folder / (request_id + ".json")
                    with target.open("xb") as output:
                        output.write(data)
                    target.chmod(0o600)
                self.writer.append(
                    "lifecycle",
                    {
                        "event": "response_audit",
                        "step": request.step,
                        "request_id": request_id,
                        "saved": saved,
                        "sha256": hashlib.sha256(data).hexdigest() if saved else None,
                        "capture_limit_bytes_per_run": 8_388_608,
                    },
                )
            finally:
                active_wire.reset(token)
