"""Offline status and explicitly enabled single-request connectivity probe."""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from ctxroute.api_probe import ProbeError, run_probe
from ctxroute.secrets import load_key
from ctxroute.tracing.budget import BudgetStopped


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "experiment":
        from ctxroute.evalkit.cli import main as experiments

        experiments(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "key-status", "api-probe", "demo"])
    parser.add_argument("--config", type=Path, default=Path("configs/budget.json"))
    parser.add_argument("--execute", action="store_true", help="Dispatch one paid probe if enabled")
    parser.add_argument("--transport", choices=["http", "pydantic_ai"], default="http")
    parser.add_argument("--arm", choices=["A1", "A2", "A3", "A4", "A5"], default="A4")
    parser.add_argument("--output", type=Path, default=Path("artifacts/demos"))
    args = parser.parse_args()
    if args.command == "demo":
        from ctxroute.runtime import demo

        result = demo(Path.cwd(), args.arm, args.output)
        print(
            json.dumps(
                {
                    "kind": "synthetic_demo",
                    "run_id": result.state.run_id,
                    "termination": result.state.termination,
                    "steps": result.state.step,
                    "output": str(args.output / result.state.run_id),
                    "is_benchmark": False,
                },
                indent=2,
            )
        )
        return
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
            result = run_probe(Path.cwd(), execute=args.execute, transport_name=args.transport)
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
                "status": "configuration_loaded",
                "note": "Configuration only; not a statement of account balance or billed cost",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
