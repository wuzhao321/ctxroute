"""Summarize saved connectivity evidence without network access or secret reads."""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    runs = []
    for manifest_path in sorted((root / "results/runs").glob("api-probe-*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        events = [
            json.loads(line)
            for line in manifest_path.with_name("traces.jsonl").read_text().splitlines()
        ]
        response = next((e for e in events if e["event"] == "response"), {})
        settlement = next((e for e in events if e["event"] == "settled"), {})
        passed = any(e["event"] == "probe_passed" for e in events)
        runs.append(
            {
                "run_id": manifest["run_id"],
                "transport": manifest["transport"],
                "status": "passed" if passed else "failed_or_incomplete",
                "latency_ms": response.get("latency_ms"),
                "usage_fields": response.get("usage_fields"),
                "usage_calculated_cost_usd": settlement.get("cost_usd"),
                "invoice_verified": False,
            }
        )
    ledger = root / "results/budget/campaign.sqlite"
    conn = sqlite3.connect(f"{ledger.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT reserved, actual FROM requests").fetchall()
        hard, pause, blocked = conn.execute("SELECT hard, pause, blocked FROM config").fetchone()
    finally:
        conn.close()
    summary = {
        "kind": "connectivity_integration_not_benchmark",
        "runs": runs,
        "campaign_usage_calculated_usd": str(
            sum((Decimal(actual) for _, actual in rows if actual is not None), Decimal(0))
        ),
        "campaign_unsettled_reservation_usd": str(
            sum((Decimal(hold) for hold, actual in rows if actual is None), Decimal(0))
        ),
        "hard_limit_usd": hard,
        "pause_at_usd": pause,
        "blocked": bool(blocked),
        "invoice_verified": False,
    }
    output = root / "results/validation/runtime-setup/api-summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
