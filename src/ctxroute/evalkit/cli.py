"""Prepare, execute, resume and analyze campaigns through explicit separate commands."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ctxroute.domain import Task
from ctxroute.evalkit.analysis import import_harness_report, write_analysis
from ctxroute.evalkit.campaign import create_plan, load_plan, load_tasks, run_plan
from ctxroute.evalkit.export import collect_predictions, harness_command
from ctxroute.runtime import build_live_engine
from ctxroute.sandbox.base import Sandbox
from ctxroute.sandbox.docker import DockerSandbox
from ctxroute.tools.mcp_client import McpSandbox, StdioClient
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.cost import money


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Write an offline frozen pilot schedule")
    plan.add_argument("--arms", nargs="+", default=["A0", "A1", "A2", "A3", "A4"])
    plan.add_argument("--count", type=int, default=5)
    plan.add_argument("--truncate", action="store_true")
    plan.add_argument("--output", type=Path, required=True)
    run = commands.add_parser(
        "run", help="Explicit paid inference; currently disabled by configuration"
    )
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--tasks", type=Path, required=True)
    run.add_argument("--images", type=Path, required=True)
    run.add_argument("--execute", action="store_true")
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--runs", type=Path, nargs="+", required=True)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--baseline", default="A1")
    baseline = commands.add_parser("import-baseline")
    baseline.add_argument("--source", type=Path, required=True)
    baseline.add_argument("--output", type=Path, default=Path("results/runs"))
    baseline.add_argument("--run-id", required=True)
    plot = commands.add_parser("plot")
    plot.add_argument("--analysis", type=Path, required=True)
    plot.add_argument("--output", type=Path, required=True)
    grade = commands.add_parser("import-grade")
    grade.add_argument("--run", type=Path, required=True)
    grade.add_argument("--report", type=Path, required=True)
    export = commands.add_parser("export")
    export.add_argument("--runs", type=Path, nargs="+", required=True)
    export.add_argument("--output", type=Path, required=True)
    harness = commands.add_parser(
        "harness-command", help="Print argv only; never launches evaluation"
    )
    harness.add_argument("--plan", type=Path, required=True)
    harness.add_argument("--predictions", type=Path, required=True)
    harness.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "plan":
        result = create_plan(
            root, args.output, arms=args.arms, count=args.count, truncate=args.truncate
        )
        print(
            json.dumps(
                {
                    "plan_id": result["plan_id"],
                    "jobs": len(result["jobs"]),
                    "executed": False,
                    "path": str(args.output),
                },
                indent=2,
            )
        )
    elif args.command == "analyze":
        print(write_analysis(args.runs, args.output, args.baseline))
    elif args.command == "import-baseline":
        from ctxroute.evalkit.baseline import import_baseline

        print(import_baseline(args.source, args.output, args.run_id))
    elif args.command == "plot":
        from ctxroute.evalkit.plots import pareto_plot

        pareto_plot(args.analysis, args.output)
    elif args.command == "import-grade":
        print(import_harness_report(args.run, args.report))
    elif args.command == "export":
        collect_predictions(args.runs, args.output)
        print(args.output)
    elif args.command == "harness-command":
        frozen = load_plan(root, args.plan)
        ids = list(dict.fromkeys(job["instance_id"] for job in frozen["jobs"]))
        print(
            json.dumps(
                harness_command(sys.executable, args.predictions, args.run_id, ids), indent=2
            )
        )
    else:
        execute_campaign(root, args.plan, args.tasks, args.images, execute=args.execute)


def execute_campaign(root: Path, plan: Path, tasks: Path, images: Path, *, execute: bool) -> None:
    runtime = json.loads((root / "configs/runtime.json").read_text())
    budget = json.loads((root / "configs/budget.json").read_text())
    if (
        not execute
        or runtime["execution_mode"] != "live"
        or budget["paid_execution_enabled"] is not True
    ):
        raise RuntimeError("Execution disabled: this phase only implements experiment code")
    load_plan(root, plan)
    task_map = load_tasks(tasks)
    registry: dict[str, str] = json.loads(images.read_text())
    sandbox_cfg = json.loads((root / "configs/sandbox.json").read_text())
    if not set(task_map) <= set(registry):
        raise ValueError("Missing instance image digest")
    ledger = Ledger(
        root / "results/budget/campaign.sqlite",
        hard_limit=money(str(budget["hard_limit"])),
        pause_at=money(str(budget["pause_at"])),
        enabled=True,
    )
    client = None
    sandbox: Sandbox
    if runtime.get("tool_transport", "mcp") == "mcp":
        client = StdioClient(
            sys.executable,
            [
                "-m",
                "ctxroute.tools.mcp_server",
                "--root",
                str(root),
                "--tasks",
                str(tasks.resolve()),
                "--images",
                str(images.resolve()),
            ],
        )
        sandbox = McpSandbox(client, sandbox_cfg["control_timeout_seconds"])
    elif runtime["tool_transport"] == "direct":
        sandbox = DockerSandbox(registry, sandbox_cfg)
    else:
        raise ValueError("Unknown tool transport")

    def factory(job: dict[str, Any], task: Task, run_id: str) -> Any:
        return build_live_engine(
            root,
            job["arm"],
            task,
            sandbox,
            run_id=run_id,
            config_path=root / job["config"],
            metadata={
                **job,
                "split": "dev",
                "image_digest": registry[task.instance_id],
                "tool_transport": runtime.get("tool_transport", "mcp"),
            },
        )

    try:
        completed = run_plan(root, plan, tasks, factory, ledger, execute=execute)
        print(json.dumps({"completed_runs": len(completed)}, indent=2))
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
