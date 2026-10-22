"""Offline status and explicitly enabled single-request connectivity probe."""

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from ctxroute.api_probe import ProbeError, run_probe
from ctxroute.secrets import load_key
from ctxroute.tracing.budget import BudgetStopped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "key-status", "api-probe"])
    parser.add_argument("--config", type=Path, default=Path("configs/budget.json"))
    parser.add_argument("--execute", action="store_true", help="Dispatch one paid probe if enabled")
    args = parser.parse_args()
    if args.command == "key-status":
        try:
            load_key(Path.cwd())
        except (OSError, ValueError):
            print("DeepSeek key: not configured or invalid; no value displayed")
            raise SystemExit(1) from None
        print("DeepSeek key: configured; no value displayed")
        return
    if args.command == "api-probe":
        if args.config != Path("configs/budget.json"):
            parser.error("api-probe uses the campaign's configs/budget.json")
        try:
            result = run_probe(Path.cwd(), execute=args.execute)
        except (ProbeError, BudgetStopped) as exc:
            raise SystemExit(str(exc)) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit(
                "Invalid configuration or missing key; no request dispatched if preflight failed"
            ) from None
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    config: dict[str, Any] = json.loads(args.config.read_text())
    print(
        json.dumps(
            {
                "hard_limit_usd": str(Decimal(str(config["hard_limit"]))),
                "pause_at_usd": str(Decimal(str(config["pause_at"]))),
                "paid_execution_enabled": config["paid_execution_enabled"],
                "status": "P0 local preparation; benchmark not run",
                "note": "Configuration only; not a statement of account balance or billed cost",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
