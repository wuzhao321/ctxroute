"""Offline experiment contracts shared by planning, execution and analysis."""

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from ctxroute.api_probe import price_from
from ctxroute.config import ArmConfig
from ctxroute.tracing.budget import BudgetStopped
from ctxroute.tracing.cost import Usage, money
from ctxroute.tracing.stage_budget import StageBudget, load_budget


def frozen_path(root: Path, path: Path) -> str:
    full = (root / path).resolve()
    if not full.is_relative_to(root.resolve()) or not full.is_file():
        raise ValueError("Frozen inputs must be existing project files")
    return str(full.relative_to(root.resolve()))


def check_stage_allowance(root: Path, jobs: list[dict[str, Any]]) -> None:
    """Read-only worst-case task allocation check; requests still reserve atomically."""
    budget = load_budget(root)
    policy = budget.get("stage_policy")
    if not isinstance(policy, StageBudget):
        return
    occupied: dict[str, Decimal] = defaultdict(Decimal)
    total = Decimal(0)
    path = root / policy.ledger
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT blocked FROM config WHERE id=1").fetchone()
            if row is None or row[0]:
                raise BudgetStopped("Stage ledger is blocked or invalid")
            for reserved, actual, lane in conn.execute(
                "SELECT r.reserved,r.actual,s.lane FROM requests r "
                "LEFT JOIN stage_scopes s ON r.scope=s.scope"
            ):
                amount = money(actual if actual is not None else reserved)
                if lane not in policy.allocations():
                    raise BudgetStopped("Existing stage charge has no allocation")
                occupied[lane] += amount
                total += amount
    requested: dict[str, Decimal] = defaultdict(Decimal)
    for job in jobs:
        lane = job.get("budget_lane")
        if lane not in policy.allocations():
            raise ValueError("Every stage job requires an explicit budget_lane")
        requested[lane] += money(job["task_usd"])
    projected = total + sum(requested.values(), Decimal(0))
    if projected >= policy.usd_cap(policy.pause_cny):
        raise BudgetStopped("Whole plan reaches the stage pause threshold")
    if projected > policy.usd_cap(policy.hard_cny):
        raise BudgetStopped("Whole plan exceeds the stage hard limit")
    for lane, amount in requested.items():
        if occupied[lane] + amount > policy.lane_cap(lane):
            raise BudgetStopped(f"Whole plan exceeds the {lane} allocation")
    if sum(
        occupied[lane] + requested[lane] for lane in ("baseline", "checkpoint")
    ) > policy.usd_cap(policy.paired_cny):
        raise BudgetStopped("Whole plan exceeds the paired allocation")


def check_request_floor(root: Path, cfg: ArmConfig, lane: str | None) -> None:
    """Reject impossible task caps; actual message-dependent bounds remain in the gateway."""
    runtime = json.loads((root / "configs/runtime.json").read_text())
    prices = json.loads((root / runtime["price_file"]).read_text())
    policy = load_budget(root).get("stage_policy")
    tiers = {cfg.router.default_tier}
    if cfg.router.enabled:
        tiers.update(("strong", "cheap"))
    if cfg.context.mode == "summary":
        tiers.add("cheap")
    inputs, outputs = runtime["input_guard_tokens"], cfg.limits.max_output_tokens
    if isinstance(policy, StageBudget):
        if lane not in policy.allocations():
            raise ValueError("Every stage job requires an explicit budget_lane")
        inputs = max(inputs, policy.input_floor, policy.input_margin)
        outputs = max(outputs, policy.output_floor)
    for tier in tiers:
        if tier not in prices["models"]:
            raise ValueError(f"Missing frozen price for required tier: {tier}")
        minimum = price_from(prices["models"][tier]["peak"]).cost(Usage(inputs, 0, outputs))
        if minimum > cfg.limits.budget:
            raise ValueError(f"Task cap is below the {tier} request reservation floor: {minimum}")
        if isinstance(policy, StageBudget) and minimum > policy.usd_cap(policy.request_cny):
            raise ValueError(f"Request floor exceeds the stage request cap: {tier}")


def differences(left: Any, right: Any, prefix: str = "") -> set[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        a, b = cast(dict[str, Any], left), cast(dict[str, Any], right)
        changed: set[str] = set()
        for key in a.keys() | b.keys():
            changed.update(differences(a.get(key), b.get(key), f"{prefix}.{key}".strip(".")))
        return changed
    return {prefix} if left != right else set()


def check_pair(left: dict[str, Any], right: dict[str, Any]) -> None:
    """Only declared internal research factors may differ; A0 is an external reference."""
    if "A0" in {left["arm"], right["arm"]}:
        return
    a, b = left.get("provenance"), right.get("provenance")
    required = {"source", "price", "task", "image", "controls"}
    if not isinstance(a, dict) or not isinstance(b, dict):
        raise ValueError("Missing comparison provenance")
    a, b = cast(dict[str, Any], a), cast(dict[str, Any], b)
    if any(not item.get(key) for item in (a, b) for key in required):
        raise ValueError(
            "Missing comparison provenance; use explicit unverified historical analysis"
        )
    allowed: set[str] = set()
    arms = {left["arm"], right["arm"]}
    if arms & {"A2", "A4"}:
        allowed.add("controls.context.mode")
    if arms & {"A3", "A4"}:
        allowed.add("controls.router.enabled")
    if "A5" in arms:
        allowed.add("controls.router.default_tier")
    unexpected = differences(a, b) - allowed
    if unexpected:
        raise ValueError("Uncontrolled comparison differences: " + ", ".join(sorted(unexpected)))
