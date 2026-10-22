"""Budgeted production boundary, never invoked by the offline test/demo entrypoints."""

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
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.cost import Usage
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
    ) -> None:
        if thinking not in {"enabled", "disabled"} or input_guard_tokens < 1:
            raise ValueError("Invalid model settings")
        self.key, self.prices, self.ledger = key, prices, ledger
        self.writer, self.input_guard, self.thinking = writer, input_guard_tokens, thinking
        self.transport = transport

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.timeout_seconds <= 0:
            raise ModelFailure("No model time allowance remains")
        model = self.prices["models"][request.tier]
        peak = price_from(model["peak"])
        bound = peak.cost(Usage(self.input_guard, 0, request.max_output_tokens))
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
            "stream": False,
        }
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
            # Peak/holiday ambiguity deliberately remains reserved, including in task scope.
            text = raw["choices"][0]["message"]["content"]
            if not isinstance(text, str) or raw["choices"][0]["finish_reason"] != "stop":
                raise ModelFailure("Incomplete model response")
            return ModelResponse(
                text.replace(self.key, "[REDACTED]"), model["model_id"], usage, elapsed, upper
            )
        except Exception:
            self.writer.append(
                "requests",
                {
                    "request_id": request_id,
                    "step": request.step,
                    "purpose": request.purpose,
                    "event": "failed",
                    "error_type": "model_request_failed",
                    "latency_ms": (time.monotonic() - timer) * 1000,
                    "ended_at": datetime.now(UTC).isoformat(),
                },
            )
            raise ModelFailure(
                "Model request failed; inspect numeric usage and reservations"
            ) from None
