"""Atomic cross-process reservations. This is not a provider-side spending limit."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from ctxroute.tracing.cost import money

Kind = Literal["model_api", "cloud_compute", "cloud_storage", "network"]
KINDS = {"model_api", "cloud_compute", "cloud_storage", "network"}


class BudgetStopped(RuntimeError):
    """A request cannot be safely dispatched within the configured allowance."""


@dataclass(frozen=True)
class Snapshot:
    spent: Decimal
    reserved: Decimal
    hard_limit: Decimal
    pause_at: Decimal
    blocked: bool


class Ledger:
    """One shared file per campaign; all billable work must use this ledger.

    Persist before dispatch, settle only with known usage/billing. A crashed or
    unknown-charge request retains its full reservation across process restarts.
    """

    def __init__(
        self,
        path: Path,
        *,
        hard_limit: Decimal,
        pause_at: Decimal,
        enabled: bool = False,
    ) -> None:
        self.path = path
        self.hard_limit = money(hard_limit)
        self.pause_at = money(pause_at)
        self.enabled = enabled
        if not 0 < self.pause_at <= self.hard_limit:
            raise ValueError("Require 0 < pause threshold <= hard limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS config "
                "(id INTEGER PRIMARY KEY CHECK(id=1), hard TEXT, pause TEXT, blocked INTEGER)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO config VALUES(1, ?, ?, 0)",
                (str(self.hard_limit), str(self.pause_at)),
            )
            row = conn.execute("SELECT hard, pause FROM config WHERE id=1").fetchone()
            if row is None or (money(row[0]), money(row[1])) != (
                self.hard_limit,
                self.pause_at,
            ):
                raise ValueError("Ledger limits differ; do not reset a campaign ledger")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS requests "
                "(id TEXT PRIMARY KEY, kind TEXT NOT NULL, reserved TEXT NOT NULL, "
                "actual TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, settled_at TEXT)"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
            if "scope" not in columns:
                conn.execute("ALTER TABLE requests ADD COLUMN scope TEXT")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scopes (id TEXT PRIMARY KEY, cap TEXT NOT NULL)"
            )

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _snapshot(self, conn: sqlite3.Connection) -> Snapshot:
        spent, reserved = Decimal(0), Decimal(0)
        for hold, actual in conn.execute("SELECT reserved, actual FROM requests"):
            if actual is None:
                reserved += money(hold)
            else:
                spent += money(actual)
        row = conn.execute("SELECT blocked FROM config WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Missing ledger configuration")
        return Snapshot(spent, reserved, self.hard_limit, self.pause_at, bool(row[0]))

    def snapshot(self) -> Snapshot:
        with self._transaction() as conn:
            return self._snapshot(conn)

    def freeze(self, request_id: str, revised_upper_bound: Decimal) -> None:
        """Keep an uncertain charge reserved and block after a bound violation."""
        amount = money(revised_upper_bound)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT reserved, actual FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None or row[1] is not None:
                raise ValueError("Require an unsettled reservation")
            conn.execute(
                "UPDATE requests SET reserved=? WHERE id=?",
                (str(max(amount, money(row[0]))), request_id),
            )
            conn.execute("UPDATE config SET blocked=1 WHERE id=1")

    def reserve(
        self,
        request_id: str,
        upper_bound: Decimal,
        kind: Kind,
        *,
        scope: str | None = None,
        scope_limit: Decimal | None = None,
    ) -> None:
        amount = money(upper_bound)
        if not self.enabled:
            raise BudgetStopped("Paid execution is disabled")
        if not request_id or len(request_id) > 200 or kind not in KINDS or amount <= 0:
            raise ValueError("Require request ID, supported category and positive upper bound")
        if (scope is None) != (scope_limit is None):
            raise ValueError("Scope and limit must be supplied together")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone():
                raise BudgetStopped("Request ID already reserved; do not dispatch twice")
            state = self._snapshot(conn)
            projected = state.spent + state.reserved + amount
            if state.blocked or projected > self.hard_limit:
                raise BudgetStopped("Blocked ledger or hard limit exceeded")
            if projected >= self.pause_at:
                raise BudgetStopped("80% pause threshold reached including reservations")
            if scope is not None and scope_limit is not None:
                limit = money(scope_limit)
                conn.execute("INSERT OR IGNORE INTO scopes VALUES(?, ?)", (scope, str(limit)))
                saved = conn.execute("SELECT cap FROM scopes WHERE id=?", (scope,)).fetchone()
                if saved is None or money(saved[0]) != limit:
                    raise ValueError("Task budget cannot change within a run")
                consumed = sum(
                    (
                        money(actual if actual is not None else hold)
                        for hold, actual in conn.execute(
                            "SELECT reserved, actual FROM requests WHERE scope=?", (scope,)
                        )
                    ),
                    Decimal(0),
                )
                if consumed + amount > limit:
                    raise BudgetStopped("Task budget exceeded including unsettled requests")
            conn.execute(
                "INSERT INTO requests(id, kind, reserved, scope) VALUES(?, ?, ?, ?)",
                (request_id, kind, str(amount), scope),
            )

    def settle(self, request_id: str, actual: Decimal | None) -> None:
        # Unknown usage is deliberately not a zero-cost result.
        amount = None if actual is None else money(actual)
        overrun = False
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT reserved, actual FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Cannot settle an unreserved request")
            if row[1] is not None:
                if amount is not None and money(row[1]) == amount:
                    return
                raise ValueError("Settled request cannot be overwritten")
            if amount is None:
                return
            conn.execute(
                "UPDATE requests SET actual=?, settled_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(amount), request_id),
            )
            overrun = amount > money(row[0])
            if overrun:
                # Preserve the actual incurred charge even if the caller's bound was wrong.
                conn.execute("UPDATE config SET blocked=1 WHERE id=1")
        if overrun:
            raise BudgetStopped("Actual charge exceeded reservation; recorded and blocked")
