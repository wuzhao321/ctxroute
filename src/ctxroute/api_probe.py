"""One fixed, non-streaming DeepSeek connectivity request with durable accounting.

No automatic retry, no response text or authentication headers in artifacts.
This is a connectivity probe, not the experiment's general model adapter.
"""

import hashlib
import json
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from ctxroute.secrets import load_key
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.cost import Price, Usage, money

Json = dict[str, Any]
Transport = Callable[[Json, str, int], Json]


class ProbeError(RuntimeError):
    """Safe, value-free message for CLI diagnostics."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def http_transport(payload: Json, key: str, timeout: int) -> Json:
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    # Neither redirects nor environment proxy settings may forward the credential.
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ProbeError("response_too_large")
    result: object = json.loads(raw)
    if not isinstance(result, dict):
        raise ProbeError("invalid_response")
    return cast(Json, result)


def integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ProbeError("invalid_usage")
    return value


def normalize_usage(response: Json) -> Usage:
    value = response.get("usage")
    if not isinstance(value, dict):
        raise ProbeError("missing_usage")
    raw = cast(Json, value)
    prompt = integer(raw.get("prompt_tokens"))
    output = integer(raw.get("completion_tokens"))
    cached = integer(raw.get("prompt_cache_hit_tokens"))
    missed = integer(raw.get("prompt_cache_miss_tokens"))
    total = integer(raw.get("total_tokens"))
    if cached + missed != prompt or total != prompt + output:
        raise ProbeError("inconsistent_usage")
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict) and "cached_tokens" in details:
        if integer(cast(Json, details)["cached_tokens"]) != cached:
            raise ProbeError("inconsistent_cache_usage")
    return Usage(prompt, cached, output)


def definitely_off_peak(start: datetime, end: datetime, peak_hours: list[list[str]]) -> bool:
    """Outside weekday peak intervals is off-peak even on a public holiday.

    Inside a peak interval the holiday status is unknown; retain the reservation
    and report both prices instead of claiming a precise billed amount.
    """
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("Require an ordered timezone-aware interval")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if day.weekday() < 5:
            for first, last in peak_hours:
                first_hour, first_minute = (int(x) for x in first.split(":"))
                last_hour, last_minute = (int(x) for x in last.split(":"))
                interval_start = day.replace(hour=first_hour, minute=first_minute)
                interval_end = day.replace(hour=last_hour, minute=last_minute)
                if interval_start >= interval_end:
                    raise ValueError("Invalid peak interval")
                if start < interval_end and end >= interval_start:
                    return False
        day += timedelta(days=1)
    return True


def price_from(raw: Json) -> Price:
    return Price(*(money(str(raw[k])) for k in ("cached_input", "uncached_input", "output")))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_commit(root: Path) -> str:
    """Read this project's local Git commit without depending on system Git."""
    head = (root / ".git/HEAD").read_text().strip()
    if head.startswith("ref: "):
        ref = head[5:]
        path = root / ".git" / ref
        if path.exists():
            head = path.read_text().strip()
        else:
            head = next(
                (
                    line.split()[0]
                    for line in (root / ".git/packed-refs").read_text().splitlines()
                    if line.endswith(" " + ref)
                ),
                "",
            )
    if len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
        raise ProbeError("commit_required_before_execution")
    return head


def run_probe(
    root: Path,
    *,
    execute: bool = False,
    transport: Transport = http_transport,
    transport_name: str = "http",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Json:
    if transport_name not in {"http", "pydantic_ai"}:
        raise ProbeError("invalid_probe_transport")
    if transport_name == "pydantic_ai":
        from ctxroute.models.pydantic_adapter import pydantic_transport

        transport = pydantic_transport
    settings: Json = json.loads((root / "configs/api_probe.json").read_text())
    budget: Json = json.loads((root / "configs/budget.json").read_text())
    price_path = root / settings["price_file"]
    prices: Json = json.loads(price_path.read_text())
    model: Json = prices["models"][settings["model_tier"]]
    reservation = money(settings["reservation_usd"])
    if reservation > Decimal("0.10") or reservation <= 0 or settings["max_attempts"] != 1:
        raise ProbeError("invalid_probe_allowance")
    if settings["prompt"] != "Reply exactly OK." or settings["thinking"] != "disabled":
        raise ProbeError("probe_must_use_fixed_prompt_and_disabled_thinking")
    output_limit = integer(settings["max_output_tokens"])
    if not 1 <= output_limit <= 32 or not 1 <= settings["timeout_seconds"] <= 30:
        raise ProbeError("invalid_probe_limits")
    input_guard = integer(settings["input_guard_tokens"])
    guard_cost = price_from(model["peak"]).cost(Usage(input_guard, 0, output_limit))
    if input_guard < 1 or guard_cost > reservation:
        raise ProbeError("configured_guard_exceeds_probe_allowance")
    plan = {
        "mode": "execute" if execute else "dry_run",
        "transport": transport_name,
        "model_id": model["model_id"],
        "reservation_usd": str(reservation),
        "guard_cost_usd": str(guard_cost),
        "hard_limit_usd": str(budget["hard_limit"]),
        "max_output_tokens": output_limit,
        "paid_execution_enabled": budget["paid_execution_enabled"],
    }
    if not execute:
        return plan
    if budget["paid_execution_enabled"] is not True:
        raise ProbeError("paid_execution_disabled")
    start = clock()
    if prices["retrieved_on"] != start.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ProbeError("price_snapshot_needs_daily_verification")
    commit = source_commit(root)
    key = load_key(root)
    book = Ledger(
        root / "results/budget/campaign.sqlite",
        hard_limit=money(str(budget["hard_limit"])),
        pause_at=money(str(budget["pause_at"])),
        enabled=True,
    )
    state = book.snapshot()
    print(
        f"预计预留 ${reservation} / 已核算 ${state.spent} / 未结算预留 ${state.reserved}"
        f" / 硬上限 ${state.hard_limit}",
        flush=True,
    )
    run_id = f"api-probe-{start.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    folder = root / "results/runs" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {
        **plan,
        "run_id": run_id,
        "commit": commit,
        "purpose": "connectivity_probe_not_benchmark",
        "started_at": start.isoformat(),
        "probe_config_sha256": digest(root / "configs/api_probe.json"),
        "budget_config_sha256": digest(root / "configs/budget.json"),
        "price_sha256": digest(price_path),
        "price_source": prices["source"],
        "documented_model_version": model["documented_version"],
        "provider_retries": 0,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    trace = folder / "traces.jsonl"

    def log(event: Json) -> None:
        encoded = json.dumps({"run_id": run_id, **event}, ensure_ascii=False)
        with trace.open("a") as output:
            output.write(encoded.replace(key, "[REDACTED]") + "\n")
            output.flush()

    book.reserve(run_id, reservation, "model_api")
    log({"event": "reserved", "reservation_usd": str(reservation)})
    payload: Json = {
        "model": model["model_id"],
        "messages": [{"role": "user", "content": settings["prompt"]}],
        "max_tokens": output_limit,
        "temperature": 0,
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    timer = time.monotonic()
    try:
        response = transport(payload, key, settings["timeout_seconds"])
        usage = normalize_usage(response)
        end = clock()
        lower = price_from(model["off_peak"]).cost(usage)
        upper = price_from(model["peak"]).cost(usage)
        event: Json = {
            "event": "response",
            "finished_at": end.isoformat(),
            "latency_ms": round((time.monotonic() - timer) * 1000, 3),
            "prompt_tokens": usage.input_tokens,
            "cached_tokens": usage.cached_tokens,
            "completion_tokens": usage.output_tokens,
            "usage_fields": {
                k: integer(response["usage"][k])
                for k in (
                    "prompt_tokens",
                    "completion_tokens",
                    "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens",
                    "total_tokens",
                )
            },
            "cost_lower_usd": str(lower),
            "cost_upper_usd": str(upper),
            "invoice_verified": False,
        }
        # Log observed usage before any validation/settlement that could raise.
        log(event)
        if (
            usage.input_tokens > settings["input_guard_tokens"]
            or usage.output_tokens > output_limit
        ):
            book.freeze(run_id, upper)
            raise ProbeError("provider_usage_exceeds_probe_guard")
        if upper > reservation:
            book.freeze(run_id, upper)
            raise ProbeError("provider_cost_exceeds_reservation")
        if definitely_off_peak(start, end, prices["peak_hours_utc"]):
            book.settle(run_id, lower)
            log({"event": "settled", "cost_usd": str(lower), "basis": "usage_off_peak"})
            event["usage_calculated_cost_usd"] = str(lower)
        else:
            log({"event": "unsettled", "reason": "peak_or_holiday_rate_needs_reconciliation"})
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProbeError("missing_completion")
        first = cast(list[Any], choices)[0]
        if not isinstance(first, dict):
            raise ProbeError("invalid_completion")
        first = cast(Json, first)
        message = first.get("message")
        if not isinstance(message, dict):
            raise ProbeError("invalid_completion")
        content = cast(Json, message).get("content")
        if (
            first.get("finish_reason") != "stop"
            or not isinstance(content, str)
            or content.strip() != "OK"
        ):
            raise ProbeError("unexpected_completion")
        log({"event": "probe_passed", "response_matches_ok": True})
        return {**event, "run_id": run_id, "status": "passed"}
    except Exception as exc:
        # Exception messages and provider bodies can contain credentials; do not serialize them.
        code = (
            str(exc).replace(key, "[REDACTED]")
            if isinstance(exc, ProbeError)
            else "transport_or_response_error"
        )
        log({"event": "probe_failed", "error_type": code, "exception_class": type(exc).__name__})
        raise ProbeError(code) from None
