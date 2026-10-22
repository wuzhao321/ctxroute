"""Read-only evidence analysis. Missing grades/usage remain explicit, never fabricated."""

import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, cast

from ctxroute.evalkit.statistics import bootstrap, mcnemar, percentile
from ctxroute.router.rules import classify


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if path.exists()
        else []
    )


def import_harness_report(run_dir: Path, report: Path) -> Path:
    """Import one official per-instance report; no inference code reads grading data."""
    manifest = json.loads((run_dir / "manifest.json").read_text())
    if manifest.get("kind") != "agent_run" or not manifest.get("finished_at"):
        raise ValueError("Only completed real runs accept grades")
    instance = manifest["instance_id"]
    raw = json.loads(report.read_text())
    item = raw.get(instance)
    if not isinstance(item, dict):
        raise ValueError("Missing official per-instance result")
    item = cast(dict[str, Any], item)
    if type(item.get("resolved")) is not bool:
        raise ValueError(
            "Expected official per-instance report keyed by instance_id with resolved bool"
        )
    target = run_dir / "grade.json"
    record = {
        "instance_id": instance,
        "resolved": item["resolved"],
        "report_path": str(report.resolve()),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "predictions_sha256": hashlib.sha256(
            (run_dir / "predictions.jsonl").read_bytes()
        ).hexdigest(),
        "harness_version": manifest.get("harness_version"),
        "provenance": "operator-supplied official harness report",
    }
    with target.open("x") as output:
        json.dump(record, output, indent=2)
        output.write("\n")
    return target


def summarize_run(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("kind") != "agent_run":
        raise ValueError("Synthetic runs are excluded from experimental analysis")
    events = read_jsonl(directory / "traces.jsonl")
    requests = read_jsonl(directory / "requests.jsonl")
    by_request: dict[str, dict[str, Any]] = {}
    for event in requests:
        by_request.setdefault(event["request_id"], {}).update(event)
    usage = [event for event in by_request.values() if "prompt_tokens" in event]
    unmeasured = len(by_request) - len(usage)
    tier_tokens: dict[str, dict[str, int | float | None]] = {}
    for tier in ("strong", "cheap", "local"):
        selected = [item for item in usage if item["tier"] == tier]
        input_tokens = sum(item["prompt_tokens"] for item in selected)
        cached = sum(item["cached_tokens"] for item in selected)
        tier_tokens[tier] = {
            "input": input_tokens,
            "cached": cached,
            "output": sum(item["completion_tokens"] for item in selected),
            "cache_hit_rate": cached / input_tokens if input_tokens else None,
        }
    commands = [event["command"] for event in events if event.get("command")]
    read_commands = [command for command in commands if classify(command) == "read"]
    seen: set[str] = set()
    repeated_after, reads_after, compressed = 0, 0, False
    for event in events:
        compressed |= bool(event.get("compress_event", {}).get("accepted"))
        command = event.get("command")
        if command and classify(command) == "read":
            if compressed:
                reads_after += 1
                repeated_after += command in seen
            seen.add(command)
    grade_path = directory / "grade.json"
    resolved = None
    if grade_path.exists():
        grade = json.loads(grade_path.read_text())
        actual = hashlib.sha256((directory / "predictions.jsonl").read_bytes()).hexdigest()
        if actual != grade["predictions_sha256"] or grade["instance_id"] != manifest["instance_id"]:
            raise ValueError("Grade no longer matches predictions")
        resolved = grade["resolved"]
    costs = [
        float(event.get("cost_upper_usd", event["reserved_usd"])) for event in by_request.values()
    ]
    cost_known = bool(by_request) and not unmeasured
    return {
        "run_id": manifest["run_id"],
        "instance_id": manifest["instance_id"],
        "arm": manifest["arm"],
        "variant": manifest.get("variant", "main"),
        "seed": manifest["seed"],
        "split": manifest.get("split", "dev"),
        "resolved": resolved,
        "termination": manifest.get("termination", "interrupted"),
        "wall_ms": manifest.get("wall_ms"),
        "steps": manifest.get("steps", len(events)),
        "cost_upper_usd": sum(costs) if by_request else None,
        "cost_measured": cost_known,
        "billed_usd": None,
        "requests": len(by_request),
        "unmeasured_requests": unmeasured,
        "compression_requests": sum(
            item.get("purpose") == "compression" for item in by_request.values()
        ),
        "failed_requests": sum(item.get("event") == "failed" for item in by_request.values()),
        "compression_count": sum(
            bool(event.get("compress_event", {}).get("accepted")) for event in events
        ),
        "compression_tokens_saved": sum(
            event["ctx_tokens_before"] - event["ctx_tokens_after"]
            for event in events
            if event.get("compress_event", {}).get("accepted")
        ),
        "format_errors": sum(event.get("error_type") == "format_error" for event in events),
        "tool_errors": sum(
            event.get("error_type") in {"tool_error", "command_timeout", "output_limit"}
            for event in events
        ),
        "repeat_command_rate": (1 - len(set(commands)) / len(commands)) if commands else None,
        "repeat_read_command_rate": (1 - len(set(read_commands)) / len(read_commands))
        if read_commands
        else None,
        "post_compression_repeat_read_rate": repeated_after / reads_after if reads_after else None,
        "tier_tokens": tier_tokens,
        "tier_steps": dict(Counter(event["tier"] for event in events)),
        "max_context_tokens": max((event["ctx_tokens_before"] for event in events), default=0),
        "evidence_path": str(directory.resolve()),
    }


def paired_comparison(rows: list[dict[str, Any]], baseline: str, candidate: str) -> dict[str, Any]:
    grouped: dict[str, dict[str, dict[int, dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for row in rows:
        label = row["arm"] + (":" + row["variant"] if row["variant"] != "main" else "")
        group = grouped[label][row["instance_id"]]
        if row["seed"] in group:
            raise ValueError("Multiple runs per arm/instance/seed; choose an explicit rerun policy")
        group[row["seed"]] = row
    left, right = grouped[baseline], grouped[candidate]
    differences: list[float] = []
    cost_ratios: list[float] = []
    latency_ratios: list[float] = []
    cost_differences: list[float] = []
    binary: list[tuple[bool, bool]] = []
    excluded: dict[str, str] = {}
    zero_cost: list[str] = []
    cost_excluded: list[str] = []
    latency_excluded: list[str] = []
    single_seed = True
    for instance in sorted(set(left) | set(right)):
        a, b = left.get(instance, {}), right.get(instance, {})
        if not a or set(a) != set(b):
            excluded[instance] = "missing arm or mismatched seeds"
            continue
        if any(item["resolved"] is None for item in [*a.values(), *b.values()]):
            excluded[instance] = "missing official grade"
            continue
        av, bv = list(a.values()), list(b.values())
        differences.append(
            statistics.mean(float(item["resolved"]) for item in bv)
            - statistics.mean(float(item["resolved"]) for item in av)
        )
        single_seed &= len(a) == 1
        if len(a) == 1:
            binary.append((av[0]["resolved"], bv[0]["resolved"]))
        if all(item["cost_measured"] for item in av + bv):
            ac = statistics.mean(float(item["cost_upper_usd"]) for item in av)
            bc = statistics.mean(float(item["cost_upper_usd"]) for item in bv)
            cost_differences.append(bc - ac)
            if ac > 0:
                cost_ratios.append(bc / ac)
            else:
                zero_cost.append(instance)
        else:
            cost_excluded.append(instance)
        if all(item["wall_ms"] is not None for item in av + bv):
            al = statistics.mean(float(item["wall_ms"]) for item in av)
            bl = statistics.mean(float(item["wall_ms"]) for item in bv)
            if al > 0:
                latency_ratios.append(bl / al)
            else:
                latency_excluded.append(instance)
        else:
            latency_excluded.append(instance)
    success = bootstrap(differences)
    return {
        "baseline": baseline,
        "candidate": candidate,
        "analysis": "dev exploratory",
        "success_difference": success,
        "excluded_pairs": excluded,
        "mcnemar": mcnemar(binary) if single_seed and binary else None,
        "noninferiority_margin": 0.05,
        "ci_clears_margin": success["ci95"] is not None and success["ci95"][0] >= -0.05,
        "population_noninferiority_claim": False,
        "cost_upper_ratio_median": bootstrap(cost_ratios, statistics.median),
        "cost_upper_difference_median": bootstrap(cost_differences, statistics.median),
        "cost_pairs_unmeasured": cost_excluded,
        "zero_cost_denominators": zero_cost,
        "latency_ratio_median": bootstrap(latency_ratios, statistics.median),
        "latency_pairs_excluded": latency_excluded,
    }


def write_analysis(run_dirs: list[Path], target: Path, baseline: str = "A1") -> Path:
    rows = [summarize_run(directory) for directory in run_dirs]
    if not rows:
        raise ValueError("No experimental evidence supplied")
    if any(row["split"] != "dev" for row in rows):
        raise ValueError("Current analysis protocol is dev-only; register test analysis separately")
    labels = sorted(
        {row["arm"] + (":" + row["variant"] if row["variant"] != "main" else "") for row in rows}
    )
    summaries: dict[str, Any] = {}
    for label in labels:
        selected = [
            row
            for row in rows
            if row["arm"] + (":" + row["variant"] if row["variant"] != "main" else "") == label
        ]
        measured = [float(row["wall_ms"]) for row in selected if row["wall_ms"] is not None]
        grades = [row["resolved"] for row in selected if row["resolved"] is not None]
        summaries[label] = {
            "started": len(selected),
            "graded": len(grades),
            "missing_grades": len(selected) - len(grades),
            "successes_over_started": sum(grades) / len(selected),
            "graded_pass_at_1": sum(grades) / len(grades) if grades else None,
            "wall_p50_ms": percentile(measured, 0.5),
            "wall_p95_ms": percentile(measured, 0.95),
            "wall_sample_count": len(measured),
            "termination_counts": dict(Counter(row["termination"] for row in selected)),
            "budget_termination_rate": sum(
                row["termination"]
                in {"budget_limit", "wall_timeout", "step_limit", "context_limit"}
                for row in selected
            )
            / len(selected),
        }
    comparisons = [
        paired_comparison(rows, baseline, candidate)
        for candidate in labels
        if candidate != baseline
    ]
    target.mkdir(parents=True, exist_ok=False)
    (target / "runs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (target / "analysis.json").write_text(
        json.dumps(
            {
                "summaries": summaries,
                "comparisons": comparisons,
                "h1_observational_bins": [
                    {
                        "arm": label,
                        "step_interval": [low, high],
                        "started": len(selected),
                        "graded": sum(row["resolved"] is not None for row in selected),
                        "resolved": sum(row["resolved"] is True for row in selected),
                    }
                    for label in labels
                    for low, high in [(0, 10), (11, 25), (26, 50), (51, 100000)]
                    for selected in [
                        [
                            row
                            for row in rows
                            if row["arm"]
                            + (":" + row["variant"] if row["variant"] != "main" else "")
                            == label
                            and low <= row["steps"] <= high
                        ]
                    ]
                ],
                "cloud_billed_usd": None,
                "actual_api_billed_usd": None,
                "note": (
                    "Costs are cache-aware usage upper bounds, not bills; missing data is retained."
                ),
            },
            indent=2,
        )
        + "\n"
    )
    scalar = [key for key, value in rows[0].items() if not isinstance(value, dict)]
    with (target / "runs.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=scalar, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    failures = sorted(
        (row for row in rows if row["resolved"] is not True), key=lambda row: row["run_id"]
    )[:30]
    with (target / "failure_review.csv").open("w", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "run_id",
                "instance_id",
                "termination",
                "category",
                "notes",
                "evidence_path",
            ],
        )
        writer.writeheader()
        for row in failures:
            writer.writerow(
                {
                    **{
                        key: row[key]
                        for key in ("run_id", "instance_id", "termination", "evidence_path")
                    },
                    "category": "",
                    "notes": "",
                }
            )
    report = [
        "# ctxroute dev 探索性分析",
        "",
        "费用为含缓存 usage 的高峰价格上界，不代表账单。缺失判分单列；不得据 pilot 声称总体非劣。",
        "",
        "| 臂 | 已启动 | 已判分 | 已判分 pass@1 | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, row in summaries.items():
        report.append(
            f"| {label} | {row['started']} | {row['graded']} | "
            f"{row['graded_pass_at_1']} | {row['wall_p50_ms']} | {row['wall_p95_ms']} |"
        )
    report.extend(
        [
            "",
            "逐实例证据见 runs.csv；配对 CI、缺失原因和统计检验见 analysis.json；"
            "人工失败分类见 failure_review.csv。",
            "长轨迹与成功的关系仅可作为观察性分析；recall 工具差异、缓存时段、"
            "执行顺序及不完整配对均是效度限制。",
        ]
    )
    (target / "REPORT.md").write_text("\n".join(report) + "\n")
    return target
