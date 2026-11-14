"""Replay historical charges in a separate stage ledger; never invoke a model or tool."""

import argparse
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

from ctxroute.api_probe import price_from
from ctxroute.domain import ModelRequest
from ctxroute.tracing.budget import Kind
from ctxroute.tracing.stage_budget import (
    StageLedger,
    budget_report,
    load_budget,
    read_ledger,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-plan", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    config = load_budget(root)
    policy = config["stage_policy"]
    before = budget_report(root, config)
    plan = json.loads(args.prior_plan.read_text())
    source = root / plan["budget"]["new_ledger"]
    source_before = read_ledger(source)
    lanes = {item["run_id"]: item["variant"] for item in plan["runs"]}
    args.output.mkdir(parents=True, exist_ok=False)
    book = StageLedger(
        args.output / "synthetic-replay.sqlite",
        replace(policy, paid_execution_enabled=True),
        enabled=True,
    )
    records = []
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT id, kind, reserved, actual, scope FROM requests ORDER BY rowid")
        for request_id, kind, held, actual, scope in rows:
            lane = lanes[scope]
            book.bind_scope(scope, lane)
            book.reserve(
                request_id,
                Decimal(held),
                cast(Kind, kind),
                scope=scope,
                scope_limit=policy.lane_cap(lane),
            )
            book.settle(request_id, None if actual is None else Decimal(actual))
            state = book.snapshot()
            records.append(
                {
                    "request_id": request_id,
                    "lane": lane,
                    "reserved_usd": held,
                    "settled_usd": actual,
                    "occupied_cny_after": str((state.spent + state.reserved) * policy.usd_to_cny),
                }
            )
    peak = price_from(json.loads(args.prices.read_text())["models"]["strong"]["peak"])
    request = ModelRequest(
        ({"role": "user", "content": "synthetic"},),
        "strong",
        "action",
        16384,
        900,
        "candidate",
        1,
        policy.lane_cap("baseline"),
        "json_object",
    )
    bound = book.reservation_bound(request, peak, 32000)
    max_bytes = int(
        (
            (policy.usd_cap(policy.request_cny) * 1_000_000 - policy.output_floor * peak.output)
            / peak.uncached_input
            - policy.input_margin
        )
        / policy.input_byte_multiplier
    )
    after = budget_report(root, config)
    if before != after or source_before != read_ledger(source):
        raise RuntimeError("Original ledger changed during offline replay")
    state = book.snapshot()
    result = {
        "synthetic_budget_replay_only": True,
        "paid_model_requests": 0,
        "historical_ledger_count": len(before["ledgers"]) - int(before["active"] is not None),
        "historical_hashes_unchanged": True,
        "before": before,
        "source_ledger": str(source.relative_to(root)),
        "source_sha256": source_before["sha256"],
        "replayed_requests": len(records),
        "requests": records,
        "replayed_settled_usd": str(state.spent),
        "replayed_pending_usd": str(state.reserved),
        "replayed_occupied_cny": str((state.spent + state.reserved) * policy.usd_to_cny),
        "largest_historical_reservation_cny": str(
            max(Decimal(row["reserved_usd"]) for row in records) * policy.usd_to_cny
        ),
        "candidate_minimum_bound_usd": str(bound),
        "candidate_minimum_bound_cny": str(bound * policy.usd_to_cny),
        "message_ascii_bytes_ceiling_at_request_cap": max_bytes,
        "caveat": "Recorded charges only; does not predict usage or task success at 16384.",
    }
    (args.output / "replay.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"before", "requests"}},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
