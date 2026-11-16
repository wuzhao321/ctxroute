"""Immutable schedules and conservative whole-task resume; no evaluation on import."""

import fcntl
import hashlib
import json
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict
from itertools import combinations
from pathlib import Path
from typing import Any

from ctxroute.config import ArmConfig
from ctxroute.domain import Task
from ctxroute.evalkit.contracts import (
    check_pair,
    check_request_floor,
    check_stage_allowance,
    frozen_path,
)
from ctxroute.evalkit.provenance import code_fingerprint, controlled_variables
from ctxroute.evalkit.split import agent_input
from ctxroute.orchestrator.base import Runner
from ctxroute.tracing.budget import BudgetStopped, Ledger
from ctxroute.tracing.cost import money
from ctxroute.tracing.stage_budget import StageLedger, budget_report, load_budget


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def check_planned_controls(root: Path, jobs: list[dict[str, Any]]) -> None:
    runtime = json.loads((root / "configs/runtime.json").read_text())
    sandbox = json.loads((root / "configs/sandbox.json").read_text())
    source, price = code_fingerprint(root), digest(root / runtime["price_file"])
    profiles: list[dict[str, Any]] = []
    for job in jobs:
        cfg = ArmConfig.load(root / job["config"])
        if cfg.arm != "A0":
            expected_mode = (
                ("truncate" if job["variant"] == "truncate" else "summary")
                if cfg.arm in {"A2", "A4"}
                else "none"
            )
            if (
                cfg.engine != "ctxroute"
                or cfg.context.mode != expected_mode
                or cfg.router.enabled != (cfg.arm in {"A3", "A4"})
                or cfg.router.default_tier != ("cheap" if cfg.arm == "A5" else "strong")
            ):
                raise ValueError("Arm label does not match its declared compression/routing factor")
        profiles.append(
            {
                **job,
                "provenance": {
                    "source": source,
                    "price": price,
                    "task": job["task_sha256"],
                    "image": job["image_digest"],
                    "controls": controlled_variables(cfg, runtime, sandbox),
                },
            }
        )
    for left, right in combinations(profiles, 2):
        if left["instance_id"] == right["instance_id"]:
            check_pair(left, right)


def create_plan(
    root: Path,
    target: Path,
    *,
    arms: list[str],
    count: int = 5,
    seed: int = 20261022,
    truncate: bool = False,
    tasks_path: Path | None = None,
    images_path: Path | None = None,
    arm_configs: dict[str, str] | None = None,
    budget_lanes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Plan the frozen dev prefix only; changing test policy needs a new preregistration."""
    if not arms or len(set(arms)) != len(arms) or not set(arms) <= {f"A{i}" for i in range(6)}:
        raise ValueError("Choose unique A0-A5 arms")
    if not 1 <= count <= 5 or seed != 20261022:
        raise ValueError("Current pilot policy allows the first 1-5 dev tasks and frozen seed")
    if truncate and not set(arms) <= {"A2", "A4"}:
        raise ValueError("Truncation ablation only applies to A2/A4")
    if tasks_path is None or images_path is None:
        raise ValueError("Freeze the actual sanitized tasks and image registry when planning")
    inputs = {"tasks": frozen_path(root, tasks_path), "images": frozen_path(root, images_path)}
    tasks = load_tasks(root / inputs["tasks"])
    images = json.loads((root / inputs["images"]).read_text())
    arm_configs, budget_lanes = arm_configs or {}, budget_lanes or {}
    if (set(arm_configs) | set(budget_lanes)) - set(arms):
        raise ValueError("Config and lane mappings must refer to scheduled arms")
    split = json.loads((root / "data/split.json").read_text())
    jobs: list[dict[str, Any]] = []
    frozen: dict[str, str] = {}
    for filename in (
        "data/split.json",
        "docs/PREREG.md",
        "configs/budget.json",
        "configs/runtime.json",
        "configs/sandbox.json",
        "uv.lock",
        "pyproject.toml",
        *inputs.values(),
    ):
        frozen[filename] = digest(root / filename)
    for script in sorted((root / "scripts").glob("*")):
        if script.is_file() and script.suffix in {".py", ".sh"}:
            frozen[str(script.relative_to(root))] = digest(script)
    runtime = json.loads((root / "configs/runtime.json").read_text())
    stage_config = load_budget(root).get("active_stage_config")
    if stage_config:
        frozen[stage_config] = digest(root / stage_config)
    frozen[runtime["price_file"]] = digest(root / runtime["price_file"])
    if "A0" in arms:
        relative = "configs/baselines/mini-swe-agent.json"
        frozen[relative] = digest(root / relative)
    for instance in split["dev"][:count]:
        if (
            instance not in tasks
            or re.search(r"(?:@|^)sha256:[a-f0-9]{64}$", images.get(instance, "")) is None
        ):
            raise ValueError("Every planned task needs sanitized input and a digest-pinned image")
        for arm in arms:
            default = (
                f"configs/ablations/{arm}-truncate.yaml" if truncate else f"configs/arms/{arm}.yaml"
            )
            path = frozen_path(root, Path(arm_configs.get(arm, default)))
            cfg = ArmConfig.load(root / path)
            if cfg.arm != arm:
                raise ValueError("Arm config does not match scheduled arm")
            check_request_floor(root, cfg, budget_lanes.get(arm))
            frozen[path] = digest(root / path)
            jobs.append(
                {
                    "instance_id": instance,
                    "arm": arm,
                    "seed": seed,
                    "variant": "truncate" if truncate else "main",
                    "config": path,
                    "task_usd": cfg.limits.task_usd,
                    "budget_lane": budget_lanes.get(arm),
                    "task_sha256": canonical_hash(asdict(tasks[instance])),
                    "image_digest": images[instance],
                }
            )
    body: dict[str, Any] = {
        "schema": 3,
        "source_tree_sha256": code_fingerprint(root),
        "split": "dev",
        "purpose": "pilot",
        "jobs": jobs,
        "inputs": inputs,
        "frozen_files": frozen,
        "execution_authorized": False,
    }
    check_planned_controls(root, jobs)
    check_stage_allowance(root, jobs)
    body["plan_id"] = canonical_hash(body)[:20]
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x") as output:
        json.dump(body, output, indent=2)
        output.write("\n")
    return body


def load_plan(root: Path, path: Path) -> dict[str, Any]:
    plan: dict[str, Any] = json.loads(path.read_text())
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    if canonical_hash(body)[:20] != plan["plan_id"]:
        raise ValueError("Plan contents were changed")
    if plan.get("source_tree_sha256") != code_fingerprint(root):
        raise ValueError("Experiment implementation changed; prepare a new plan")
    if any(job["arm"] == "A0" for job in plan["jobs"]):
        if "configs/baselines/mini-swe-agent.json" not in plan["frozen_files"]:
            raise ValueError("A0 plan must freeze its upstream policy")
    for relative, expected in plan["frozen_files"].items():
        full = (root / relative).resolve()
        if not full.is_relative_to(root.resolve()) or digest(full) != expected:
            raise ValueError(f"Frozen input changed: {relative}")
    if plan.get("schema") != 3:
        raise ValueError("Execution requires a new schema-3 plan with frozen task and image inputs")
    runtime = json.loads((root / "configs/runtime.json").read_text())
    required = {
        "data/split.json",
        "docs/PREREG.md",
        "configs/budget.json",
        "configs/runtime.json",
        "configs/sandbox.json",
        "uv.lock",
        "pyproject.toml",
        runtime["price_file"],
        *(
            str(path.relative_to(root))
            for path in (root / "scripts").glob("*")
            if path.is_file() and path.suffix in {".py", ".sh"}
        ),
    }
    stage_config = load_budget(root).get("active_stage_config")
    if stage_config:
        required.add(stage_config)
    if not required <= plan["frozen_files"].keys():
        raise ValueError("Plan omits required frozen execution inputs")
    for relative in plan["inputs"].values():
        if relative not in plan["frozen_files"]:
            raise ValueError("Task and image inputs must be frozen")
    tasks = load_tasks(root / plan["inputs"]["tasks"])
    images = json.loads((root / plan["inputs"]["images"]).read_text())
    for job in plan["jobs"]:
        if job["config"] not in plan["frozen_files"]:
            raise ValueError("Job config must be frozen")
        cfg = ArmConfig.load(root / job["config"])
        if cfg.arm != job["arm"] or cfg.limits.budget != money(job["task_usd"]):
            raise ValueError("Plan and config disagree on arm or task cap")
        if (
            job["task_sha256"] != canonical_hash(asdict(tasks[job["instance_id"]]))
            or job["image_digest"] != images[job["instance_id"]]
        ):
            raise ValueError("Planned task or image differs from the frozen input")
        check_request_floor(root, cfg, job.get("budget_lane"))
    check_planned_controls(root, plan["jobs"])
    return plan


def load_tasks(path: Path) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        permitted = agent_input(raw)
        if set(raw) != set(permitted):
            raise ValueError("Runner accepts sanitized task JSONL only; remove grading fields")
        task = Task(**permitted)
        if task.instance_id in tasks:
            raise ValueError("Duplicate task ID")
        tasks[task.instance_id] = task
    return tasks


@contextmanager
def campaign_lock(root: Path) -> Generator[None]:
    path = root / "results/budget/campaign.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another campaign owns the execution lock") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


EngineFactory = Callable[[dict[str, Any], Task, str], Runner]


def run_plan(
    root: Path,
    plan_path: Path,
    tasks_path: Path,
    factory: EngineFactory,
    ledger: Ledger,
    *,
    execute: bool = False,
) -> list[dict[str, Any]]:
    runtime = json.loads((root / "configs/runtime.json").read_text())
    if not execute or runtime["execution_mode"] != "live" or not ledger.enabled:
        raise RuntimeError(
            "Campaign execution requires --execute, execution_mode=live "
            "and paid_execution_enabled=true"
        )
    plan = load_plan(root, plan_path)
    if frozen_path(root, tasks_path) != plan["inputs"]["tasks"]:
        raise ValueError("Execution must use the planned task input")
    tasks = load_tasks(tasks_path)
    if any(job["instance_id"] not in tasks for job in plan["jobs"]):
        raise ValueError("Missing a scheduled sanitized task")
    completed: list[dict[str, Any]] = []
    with campaign_lock(root):
        before = ledger.snapshot()
        if isinstance(ledger, StageLedger):
            print(json.dumps(budget_report(root, load_budget(root)), indent=2))
        print(
            json.dumps(
                {
                    "plan_id": plan["plan_id"],
                    "planned_task_allowance_usd": str(
                        sum((money(job["task_usd"]) for job in plan["jobs"]), money("0"))
                    ),
                    "spent_usd": str(before.spent),
                    "reserved_usd": str(before.reserved),
                    "pause_at_usd": str(before.pause_at),
                    "hard_limit_usd": str(before.hard_limit),
                }
            )
        )
        for index, job in enumerate(plan["jobs"]):
            run_id = f"{plan['plan_id']}-{index:04d}"
            directory = root / "results/runs" / run_id
            task = tasks[job["instance_id"]]
            task_hash = canonical_hash(
                {
                    "instance_id": task.instance_id,
                    "repo": task.repo,
                    "base_commit": task.base_commit,
                    "problem_statement": task.problem_statement,
                }
            )
            if directory.exists():
                manifest = json.loads((directory / "manifest.json").read_text())
                if manifest.get("task_sha256") != task_hash:
                    raise ValueError("Task input changed on resume")
                if (
                    not manifest.get("finished_at")
                    or not (directory / "predictions.jsonl").exists()
                ):
                    raise RuntimeError(
                        "Interrupted task retained; reconcile charges before explicit rerun"
                    )
                completed.append(manifest)
                continue
            # Keep enough allowance for all remaining arms of this instance before starting.
            pending = [
                item
                for offset, item in enumerate(plan["jobs"][index:], index)
                if item["instance_id"] == job["instance_id"]
                and not (root / "results/runs" / f"{plan['plan_id']}-{offset:04d}").exists()
            ]
            bound = sum((money(item["task_usd"]) for item in pending), money("0"))
            check_stage_allowance(root, pending)
            snapshot = ledger.snapshot()
            if snapshot.blocked or snapshot.spent + snapshot.reserved + bound >= snapshot.pause_at:
                raise BudgetStopped("Insufficient allowance for the remaining paired arm block")
            engine = factory(
                {
                    **job,
                    "plan_id": plan["plan_id"],
                    "task_sha256": task_hash,
                    "source_tree_sha256": plan["source_tree_sha256"],
                    "price_sha256": digest(root / runtime["price_file"]),
                    "controls": controlled_variables(
                        ArmConfig.load(root / job["config"]),
                        runtime,
                        json.loads((root / "configs/sandbox.json").read_text()),
                    ),
                },
                task,
                run_id,
            )
            engine.run(task, int(job["seed"]))
            completed.append(engine.writer.manifest)
            if engine.writer.manifest["termination"] in {"cleanup_failed", "model_failure"}:
                raise RuntimeError(
                    "Campaign stopped after infrastructure/model failure; evidence retained"
                )
    return completed
