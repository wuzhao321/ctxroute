"""Offline budget status; no network calls and no key access."""

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status"])
    parser.add_argument("--config", type=Path, default=Path("configs/budget.json"))
    args = parser.parse_args()
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
