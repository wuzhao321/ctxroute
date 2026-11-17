"""Explicit two-phase annex to a pinned, read-only stage ledger.

Use one annex and one sequential dispatcher for both phases. This is an opt-in
ledger for the budgeted model gateway, not an authorization or execution CLI.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ctxroute.domain import ModelRequest
from ctxroute.tracing.budget import BudgetStopped, Ledger
from ctxroute.tracing.cost import Price, money
from ctxroute.tracing.stage_budget import StageBudget


@dataclass(frozen=True)
class TwoPhaseBudget:
    experiment_id: str
    ledger: str
    parent: StageBudget
    parent_sha256: str
    collector_cny: Decimal
    replay_cny: Decimal
    collector_lane: str
    replay_lane: str
    paid_execution_enabled: bool = False
    independent_pool: bool = False

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.ledger:
            raise ValueError("Require an experiment identity and a dedicated ledger")
        if (
            len(self.parent_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.parent_sha256)
            or type(self.paid_execution_enabled) is not bool
            or type(self.independent_pool) is not bool
        ):
            raise ValueError("Invalid frozen parent hash or execution flag")
        for phase, cap in self.allocations().items():
            if money(cap) <= 0 or self.lane(phase) not in self.parent.allocations():
                raise ValueError("Require positive phase caps and explicit parent allocations")

    @classmethod
    def load(cls, root: Path, path: Path) -> "TwoPhaseBudget":
        raw = json.loads(path.read_text())
        if raw["schema"] != 1 or raw["kind"] != "two_phase" or raw["currency"] != "CNY":
            raise ValueError("Unsupported two-phase budget schema")
        return cls(
            experiment_id=raw["experiment_id"],
            ledger=raw["ledger"],
            parent=StageBudget.load(root / raw["parent_stage_config"]),
            parent_sha256=raw["parent_sha256"],
            collector_cny=money(str(raw["collector_cny"])),
            replay_cny=money(str(raw["replay_cny"])),
            collector_lane=raw["collector_lane"],
            replay_lane=raw["replay_lane"],
            paid_execution_enabled=raw["paid_execution_enabled"],
            independent_pool=raw.get("independent_pool", False),
        )

    def allocations(self) -> dict[str, Decimal]:
        return {"collector": self.collector_cny, "replay": self.replay_cny}

    def lane(self, phase: str) -> str:
        return {"collector": self.collector_lane, "replay": self.replay_lane}[phase]

    def phase_cap(self, phase: str) -> Decimal:
        return self.parent.usd_cap(self.allocations()[phase])

    def identity(self) -> str:
        return json.dumps(
            {
                "experiment_id": self.experiment_id,
                "ledger": self.ledger,
                "parent": json.loads(self.parent.identity()),
                "parent_sha256": self.parent_sha256,
                "caps_cny": self.allocations(),
                "lanes": {p: self.lane(p) for p in self.allocations()},
                **({"independent_pool": True} if self.independent_pool else {}),
            },
            default=str,
            sort_keys=True,
        )


class TwoPhaseLedger(Ledger):
    """No borrowing between phases; any failed/unfinished request stops both."""

    def __init__(self, root: Path, policy: TwoPhaseBudget, *, enabled: bool = False) -> None:
        self.root, self.policy = root, policy
        path = root / policy.ledger
        parent = root / policy.parent.ledger
        if path.is_symlink() or path.resolve() == parent.resolve():
            raise ValueError("Require a separate, non-symlink annex ledger")
        self._parent_state()
        if path.exists():
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='two_phase_policy'"
                ).fetchone()
                saved = (
                    conn.execute("SELECT policy FROM two_phase_policy").fetchone()
                    if exists
                    else None
                )
                if saved is None or saved[0] != policy.identity():
                    raise ValueError("Cannot adopt, reset or change an existing ledger")
        cap = policy.parent.usd_cap(policy.collector_cny + policy.replay_cny)
        super().__init__(
            path,
            hard_limit=cap,
            pause_at=cap,
            enabled=enabled and policy.paid_execution_enabled,
        )
        with self._transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS two_phase_policy (policy TEXT NOT NULL)")
            if not conn.execute("SELECT 1 FROM two_phase_policy").fetchone():
                conn.execute("INSERT INTO two_phase_policy VALUES(?)", (policy.identity(),))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS phase_scopes (scope TEXT PRIMARY KEY, phase TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS failed_requests "
                "(id TEXT PRIMARY KEY, unknown_usage INTEGER, "
                "stopped_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            # A restart cannot interpret a reservation without settlement as unsent.
            conn.execute(
                "INSERT OR IGNORE INTO failed_requests(id, unknown_usage) "
                "SELECT id, 1 FROM requests WHERE actual IS NULL"
            )
            if conn.execute("SELECT 1 FROM failed_requests LIMIT 1").fetchone():
                conn.execute("UPDATE config SET blocked=1 WHERE id=1")

    def _parent_state(self) -> tuple[Decimal, dict[str, Decimal]]:
        path = self.root / self.policy.parent.ledger
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise BudgetStopped("Parent ledger has active journal evidence; stop")
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.policy.parent_sha256:
            raise BudgetStopped("Frozen parent ledger changed; stop without rebasing")
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            saved = conn.execute("SELECT policy FROM stage_policy").fetchone()
            if saved is None or saved[0] != self.policy.parent.identity():
                raise BudgetStopped("Frozen parent stage policy differs")
            if conn.execute("SELECT blocked FROM config WHERE id=1").fetchone()[0]:
                raise BudgetStopped("Parent stage is blocked")
            rows = conn.execute(
                "SELECT s.lane, r.reserved, r.actual FROM requests r "
                "LEFT JOIN stage_scopes s ON s.scope=r.scope"
            ).fetchall()
        occupied = dict.fromkeys(self.policy.parent.allocations(), Decimal(0))
        for lane, hold, actual in rows:
            if lane not in occupied:
                raise BudgetStopped("Parent request lacks an allocation")
            occupied[lane] += money(actual if actual is not None else hold)
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.policy.parent_sha256:
            raise BudgetStopped("Parent changed during read-only accounting")
        return sum(occupied.values(), Decimal(0)), occupied

    def bind_scope(self, scope: str, phase: str) -> None:
        if not scope or phase not in self.policy.allocations():
            raise ValueError("Require a run scope and explicit collector/replay phase")
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO phase_scopes VALUES(?, ?)", (scope, phase))
            if (
                conn.execute("SELECT phase FROM phase_scopes WHERE scope=?", (scope,)).fetchone()[0]
                != phase
            ):
                raise ValueError("A scope cannot move between phases")

    def reservation_bound(
        self, request: ModelRequest, price: Price, input_guard_tokens: int
    ) -> Decimal:
        return self.policy.parent.reservation_bound(request, price, input_guard_tokens)

    def _check_allocation(
        self, conn: sqlite3.Connection, amount: Decimal, scope: str | None
    ) -> None:
        if conn.execute("SELECT 1 FROM requests WHERE actual IS NULL LIMIT 1").fetchone():
            raise BudgetStopped("Unsettled request: no retry, next sample or phase transition")
        if amount > self.policy.parent.usd_cap(self.policy.parent.request_cny):
            raise BudgetStopped("Frozen per-request limit exceeded")
        row = conn.execute("SELECT phase FROM phase_scopes WHERE scope=?", (scope,)).fetchone()
        if row is None:
            raise BudgetStopped("Request has no bound phase")
        phase = str(row[0])
        occupied = dict.fromkeys(self.policy.allocations(), Decimal(0))
        for key, hold, actual in conn.execute(
            "SELECT s.phase, r.reserved, r.actual FROM requests r "
            "JOIN phase_scopes s ON s.scope=r.scope"
        ):
            occupied[key] += money(actual if actual is not None else hold)
        occupied[phase] += amount
        if occupied[phase] > self.policy.phase_cap(phase):
            raise BudgetStopped(f"Two-phase {phase} cap exceeded including reservations")
        parent_total, parent_lanes = self._parent_state()
        if self.policy.independent_pool:
            # Explicit new-pool authorization excludes historical costs. The old
            # book is still pinned/read-only; only this pool's caps fund requests.
            return
        total = parent_total + sum(occupied.values(), Decimal(0))
        if total > self.policy.parent.usd_cap(self.policy.parent.hard_cny):
            raise BudgetStopped("Parent stage hard limit exceeded including annex")
        if total >= self.policy.parent.usd_cap(self.policy.parent.pause_cny):
            raise BudgetStopped("Parent stage pause reached including annex")
        for name, used in occupied.items():
            parent_lanes[self.policy.lane(name)] += used
        for lane, used in parent_lanes.items():
            if used > self.policy.parent.lane_cap(lane):
                raise BudgetStopped("Parent allocation exceeded including annex")
        if parent_lanes["baseline"] + parent_lanes["checkpoint"] > self.policy.parent.usd_cap(
            self.policy.parent.paired_cny
        ):
            raise BudgetStopped("Parent paired limit exceeded including annex")

    def request_failed(self, request_id: str) -> bool:
        with self._transaction() as conn:
            row = conn.execute("SELECT actual FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("Cannot stop an unreserved request")
            conn.execute(
                "INSERT OR IGNORE INTO failed_requests(id, unknown_usage) VALUES(?, ?)",
                (request_id, int(row[0] is None)),
            )
            conn.execute("UPDATE config SET blocked=1 WHERE id=1")
        return True

    def settle(self, request_id: str, actual: Decimal | None) -> None:
        if actual is None:
            self.request_failed(request_id)
            return
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM requests, config "
                "WHERE requests.id=? AND requests.actual IS NULL AND config.blocked=1",
                (request_id,),
            ).fetchone()
            if row:
                raise BudgetStopped("Stopped unknown charge requires explicit reconciliation")
        super().settle(request_id, actual)


def open_two_phase_ledger(root: Path, config: Path, *, enabled: bool = False) -> TwoPhaseLedger:
    """Pass this ledger to both collector and replay DeepSeekModel instances."""
    return TwoPhaseLedger(root, TwoPhaseBudget.load(root, config), enabled=enabled)
