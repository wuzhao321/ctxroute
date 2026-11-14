"""Explicit stage allocations; historical ledgers are read-only and never reclassified."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from ctxroute.domain import ModelRequest
from ctxroute.tracing.budget import BudgetStopped, Ledger
from ctxroute.tracing.cost import Price, Usage, money


@dataclass(frozen=True)
class StageBudget:
    stage_id: str
    ledger: str
    usd_to_cny: Decimal
    hard_cny: Decimal
    pause_cny: Decimal
    baseline_cny: Decimal
    checkpoint_cny: Decimal
    paired_cny: Decimal
    validation_cny: Decimal
    request_cny: Decimal
    input_floor: int
    input_byte_multiplier: int
    input_margin: int
    output_floor: int
    paid_execution_enabled: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.usd_to_cny,
            self.hard_cny,
            self.pause_cny,
            self.baseline_cny,
            self.checkpoint_cny,
            self.paired_cny,
            self.validation_cny,
            self.request_cny,
        ):
            if money(value) <= 0:
                raise ValueError("Stage amounts and exchange rate must be positive")
        if not self.stage_id or not self.ledger or not self.pause_cny <= self.hard_cny:
            raise ValueError("Invalid stage identity or thresholds")
        if self.baseline_cny + self.checkpoint_cny != self.paired_cny:
            raise ValueError("Paired allocation must equal the two arm allocations")
        if self.paired_cny + self.validation_cny != self.hard_cny:
            raise ValueError("Stage allocations must sum to the hard limit")
        if self.request_cny > min(self.baseline_cny, self.checkpoint_cny, self.validation_cny):
            raise ValueError("Request limit exceeds an allocation")
        if any(
            type(value) is not int or value < 1
            for value in (
                self.input_floor,
                self.input_byte_multiplier,
                self.input_margin,
                self.output_floor,
            )
        ):
            raise ValueError("Invalid conservative reservation parameters")
        if type(self.paid_execution_enabled) is not bool:
            raise ValueError("Paid execution flag must be boolean")

    @classmethod
    def load(cls, path: Path) -> "StageBudget":
        raw = json.loads(path.read_text())
        if raw["currency"] != "CNY" or raw["billing_currency"] != "USD":
            raise ValueError("Stage accounting requires CNY limits and USD billing")
        amounts = {
            name: money(str(raw[name]))
            for name in (
                "usd_to_cny",
                "hard_cny",
                "pause_cny",
                "baseline_cny",
                "checkpoint_cny",
                "paired_cny",
                "validation_cny",
                "request_cny",
            )
        }
        return cls(
            stage_id=raw["stage_id"],
            ledger=raw["ledger"],
            **amounts,
            **raw["reservation"],
            paid_execution_enabled=raw["paid_execution_enabled"],
        )

    def usd_cap(self, cny: Decimal) -> Decimal:
        # Repeating USD fractions are rounded DOWN, never granting extra CNY allowance.
        return (money(cny) / self.usd_to_cny).quantize(Decimal("0.000000000001"), ROUND_DOWN)

    def lane_cap(self, lane: str) -> Decimal:
        return self.usd_cap(self.allocations()[lane])

    def allocations(self) -> dict[str, Decimal]:
        return {
            "baseline": self.baseline_cny,
            "checkpoint": self.checkpoint_cny,
            "validation": self.validation_cny,
        }

    def identity(self) -> str:
        data = asdict(self)
        # A later explicit authorization may enable execution without changing the pool.
        data.pop("paid_execution_enabled")
        return json.dumps(data, default=str, sort_keys=True)


class StageLedger(Ledger):
    """One durable USD ledger per stage, with atomic CNY allocation enforcement."""

    def __init__(self, path: Path, policy: StageBudget, *, enabled: bool = False) -> None:
        self.policy = policy
        # Refuse to adopt an existing legacy ledger, even if its limits happen to match.
        if path.exists():
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_policy'"
                ).fetchone()
                saved = (
                    conn.execute("SELECT policy FROM stage_policy").fetchone() if exists else None
                )
                if saved is None or saved[0] != policy.identity():
                    raise ValueError("Stage identity differs; cannot adopt or reset this ledger")
        super().__init__(
            path,
            hard_limit=policy.usd_cap(policy.hard_cny),
            pause_at=policy.usd_cap(policy.pause_cny),
            enabled=enabled and policy.paid_execution_enabled,
        )
        with self._transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS stage_policy (policy TEXT NOT NULL)")
            if not conn.execute("SELECT 1 FROM stage_policy").fetchone():
                conn.execute("INSERT INTO stage_policy VALUES(?)", (policy.identity(),))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS stage_scopes "
                "(scope TEXT PRIMARY KEY, lane TEXT NOT NULL)"
            )

    def bind_scope(self, scope: str, lane: str) -> None:
        if not scope or lane not in self.policy.allocations():
            raise ValueError("Require an explicit stage allocation and run scope")
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO stage_scopes VALUES(?, ?)", (scope, lane))
            saved = conn.execute("SELECT lane FROM stage_scopes WHERE scope=?", (scope,)).fetchone()
            if saved is None or saved[0] != lane:
                raise ValueError("A run cannot move between budget allocations")

    def reservation_bound(
        self, request: ModelRequest, price: Price, input_guard_tokens: int
    ) -> Decimal:
        size = len(json.dumps(request.messages, ensure_ascii=True).encode("ascii"))
        guard = max(
            input_guard_tokens,
            self.policy.input_floor,
            self.policy.input_byte_multiplier * size + self.policy.input_margin,
        )
        return price.cost(Usage(guard, 0, max(request.max_output_tokens, self.policy.output_floor)))

    def _check_allocation(
        self, conn: sqlite3.Connection, amount: Decimal, scope: str | None
    ) -> None:
        if amount > self.policy.usd_cap(self.policy.request_cny):
            raise BudgetStopped("Stage per-request limit exceeded before dispatch")
        row = conn.execute("SELECT lane FROM stage_scopes WHERE scope=?", (scope,)).fetchone()
        if row is None:
            raise BudgetStopped("Request has no bound stage allocation")
        lane = str(row[0])
        occupied = {key: Decimal(0) for key in self.policy.allocations()}
        for key, hold, actual in conn.execute(
            "SELECT s.lane, r.reserved, r.actual FROM requests r "
            "JOIN stage_scopes s ON s.scope=r.scope"
        ):
            occupied[key] += money(actual if actual is not None else hold)
        if occupied[lane] + amount > self.policy.lane_cap(lane):
            raise BudgetStopped(f"Stage {lane} allocation exceeded including reservations")
        if lane in {"baseline", "checkpoint"} and (
            occupied["baseline"] + occupied["checkpoint"] + amount
            > self.policy.usd_cap(self.policy.paired_cny)
        ):
            raise BudgetStopped("Stage paired allocation exceeded including reservations")


def load_budget(root: Path, config_path: Path | None = None) -> dict[str, Any]:
    config = json.loads((config_path or root / "configs/budget.json").read_text())
    if "active_stage_config" not in config:
        if config.get("currency", "USD") != "USD":
            raise ValueError("Legacy ledger limits must use USD")
        return config
    policy = StageBudget.load(root / config["active_stage_config"])
    return {
        **config,
        "hard_limit": str(policy.usd_cap(policy.hard_cny)),
        "pause_at": str(policy.usd_cap(policy.pause_cny)),
        "paid_execution_enabled": config["paid_execution_enabled"] is True
        and policy.paid_execution_enabled,
        "stage_policy": policy,
    }


def open_budget_ledger(root: Path, config: dict[str, Any]) -> Ledger:
    enabled = config["paid_execution_enabled"] is True
    policy = config.get("stage_policy")
    if isinstance(policy, StageBudget):
        return StageLedger(root / policy.ledger, policy, enabled=enabled)
    return Ledger(
        root / "results/budget/campaign.sqlite",
        hard_limit=money(str(config["hard_limit"])),
        pause_at=money(str(config["pause_at"])),
        enabled=enabled,
    )


def read_ledger(path: Path) -> dict[str, Any]:
    """Read without constructing Ledger, migrating tables or releasing unknown holds."""
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT reserved, actual FROM requests").fetchall()
        hard, pause, blocked = conn.execute("SELECT hard, pause, blocked FROM config").fetchone()
    spent = sum((money(actual) for _, actual in rows if actual is not None), Decimal(0))
    pending = sum((money(hold) for hold, actual in rows if actual is None), Decimal(0))
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "requests": len(rows),
        "settled_usd": str(spent),
        "reserved_usd": str(pending),
        "occupied_usd": str(spent + pending),
        "hard_usd": hard,
        "pause_usd": pause,
        "blocked": bool(blocked),
    }


def budget_report(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    policy = config.get("stage_policy")
    active = policy.ledger if isinstance(policy, StageBudget) else "results/budget/campaign.sqlite"
    rows = {
        str(path.relative_to(root)): read_ledger(path)
        for path in sorted((root / "results/budget").glob("*.sqlite"))
    }
    current = rows.get(active)
    other = [row for path, row in rows.items() if path != active]
    historic_spent = sum((money(row["settled_usd"]) for row in other), Decimal(0))
    historic_pending = sum((money(row["reserved_usd"]) for row in other), Decimal(0))
    occupied = sum((money(row["occupied_usd"]) for row in rows.values()), Decimal(0))
    total_spent = sum((money(row["settled_usd"]) for row in rows.values()), Decimal(0))
    total_pending = sum((money(row["reserved_usd"]) for row in rows.values()), Decimal(0))
    result: dict[str, Any] = {
        "active_ledger": active,
        "active": current,
        "hard_limit_usd": str(config["hard_limit"]),
        "pause_at_usd": str(config["pause_at"]),
        "paid_execution_enabled": config["paid_execution_enabled"],
        "historical_settled_usd": str(historic_spent),
        "historical_reserved_usd": str(historic_pending),
        "cross_stage_occupied_usd": str(occupied),
        "cross_stage_settled_usd": str(total_spent),
        "cross_stage_reserved_usd": str(total_pending),
        "ledgers": rows,
        "note": "Usage estimates and unresolved reservations, not a verified provider bill.",
    }
    if isinstance(policy, StageBudget):
        result.update(
            stage_id=policy.stage_id,
            currency="CNY",
            usd_to_cny=str(policy.usd_to_cny),
            hard_limit_cny=str(policy.hard_cny),
            pause_at_cny=str(policy.pause_cny),
            allocations_cny={key: str(value) for key, value in policy.allocations().items()},
            paired_limit_cny=str(policy.paired_cny),
            request_limit_cny=str(policy.request_cny),
            cross_stage_occupied_cny=str(occupied * policy.usd_to_cny),
            historical_plus_stage_hard_cny=str(
                (historic_spent + historic_pending) * policy.usd_to_cny + policy.hard_cny
            ),
        )
    return result
