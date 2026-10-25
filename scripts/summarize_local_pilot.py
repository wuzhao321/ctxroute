"""Reconcile trace costs with official empty-patch summaries without inventing test grades."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prior-plan-id")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    workflow_dir = root / args.workflow
    output = root / args.output
    output.mkdir(parents=True, exist_ok=True)
    if (output / "pilot-summary.json").exists() or (output / "PILOT_REPORT.md").exists():
        raise FileExistsError("Pilot summary already exists; choose a new output directory")
    workflow = json.loads((workflow_dir / "workflow.json").read_text())
    receipts = json.loads((workflow_dir / "grading-receipts.json").read_text())
    expected = {job["run_id"] for jobs in workflow["cohorts"].values() for job in jobs}
    if len(receipts) != len(expected) or {r["inference_run_id"] for r in receipts} != expected:
        raise ValueError("Incomplete or duplicate grading receipts")
    summaries = output / "official-summaries"
    summaries.mkdir(exist_ok=True)
    rows = []
    for receipt in receipts:
        run = root / "results/runs" / receipt["inference_run_id"]
        manifest = json.loads((run / "manifest.json").read_text())
        content = (root / receipt["official_summary"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != receipt["official_summary_sha256"]:
            raise ValueError("Official summary changed")
        official = json.loads(content)
        (summaries / f"{manifest['arm']}.json").write_bytes(content)
        requests = [json.loads(line) for line in (run / "requests.jsonl").read_text().splitlines()]
        usage = [r for r in requests if r["event"] == "usage"]
        if any(r["price_period"] != "off_peak" for r in usage):
            raise ValueError("This report needs explicit handling of uncertain pricing periods")
        traces = [json.loads(line) for line in (run / "traces.jsonl").read_text().splitlines()]
        json_failures = None
        if manifest["arm"] != "A0":
            json_failures = 0
            for event in traces:
                try:
                    if not isinstance(json.loads(event["model_response"]), dict):
                        json_failures += 1
                except (json.JSONDecodeError, TypeError):
                    json_failures += 1
        rows.append(
            {
                "arm": manifest["arm"],
                "run_id": manifest["run_id"],
                "termination": manifest["termination"],
                "steps": manifest["steps"],
                "model_requests": sum(r["event"] == "reserved" for r in requests),
                "requests_with_usage": len(usage),
                "usage_cost_usd": str(
                    sum((Decimal(r["cost_lower_usd"]) for r in usage), Decimal(0))
                ),
                "peak_upper_usd": str(
                    sum((Decimal(r["cost_upper_usd"]) for r in usage), Decimal(0))
                ),
                "wall_seconds": (
                    datetime.fromisoformat(manifest["finished_at"])
                    - datetime.fromisoformat(manifest["started_at"])
                ).total_seconds(),
                "empty_patches": official["empty_patch_instances"],
                "resolved": official["resolved_instances"],
                "test_completed": official["completed_instances"],
                "infrastructure_errors": official["error_instances"],
                "compressions": sum(
                    bool(r.get("compress_event", {}).get("triggered")) for r in traces
                ),
                "accepted_compressions": sum(
                    bool(r.get("compress_event", {}).get("accepted")) for r in traces
                ),
                "format_errors": sum(r.get("error_type") == "format_error" for r in traces),
                "json_object_failures": json_failures,
                "validation_errors": dict(
                    Counter(r["validation_error"] for r in traces if r.get("validation_error"))
                ),
                "tier_requests": dict(Counter(r["tier"] for r in usage)),
            }
        )
    control_path = workflow_dir / "negative-control.json"
    if control_path.exists():
        control = json.loads(control_path.read_text())
        control_bytes = (root / control["official_summary"]).read_bytes()
        if hashlib.sha256(control_bytes).hexdigest() != control["official_summary_sha256"]:
            raise ValueError("Negative-control summary changed")
        (summaries / "infrastructure-negative-control.json").write_bytes(control_bytes)
    total = sum((Decimal(r["usage_cost_usd"]) for r in rows), Decimal(0))
    result = {
        "kind": "dev_pilot_official_summary_reconciliation",
        "plan_id": workflow["plan_id"],
        "prior_plan_id": args.prior_plan_id,
        "instance_id": "sympy__sympy-22840",
        "arms": rows,
        "new_usage_cost_usd": str(total),
        "invoice_verified": False,
        "unknown_prior_reservation_usd": "0.10",
        "new_cloud_cost_usd": "0",
        "negative_control_excluded_from_model_results": True,
        "negative_control_run_in_this_batch": control_path.exists(),
        "note": (
            "Empty patches have official aggregate classifications but no test reports. "
            "Generic REPORT.md labels those absent reports missing; "
            "they are not infrastructure errors."
        ),
    }
    (output / "pilot-summary.json").write_text(json.dumps(result, indent=2) + "\n")
    with (output / "pilot-summary.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# 固定首题 dev pilot：运行结果与官方汇总",
        "",
        "固定实例 sympy__sympy-22840；ARM64；每臂 1 次，顺序 A0→A4。正式 test 未测。",
        "",
        "| 臂 | 终止原因 | 步数 | API 请求 | usage 费用 USD | 用时秒 | 官方空补丁 | 成功 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['arm']} | {row['termination']} | {row['steps']} | "
            f"{row['model_requests']} | {row['usage_cost_usd']} | {row['wall_seconds']:.1f} | "
            f"{row['empty_patches']} | {row['resolved']} |"
        )
    lines.extend(
        [
            "",
            f"本轮含缓存 usage 费用合计 {total} USD，全部请求记录为 off_peak；未核验供应商账单。",
            "历史未知调用的 0.10 USD 预留继续保留，云费用为 0。",
            "",
            f"官方汇总：空补丁 {sum(r['empty_patches'] for r in rows)}，"
            f"完成测试 {sum(r['test_completed'] for r in rows)}，"
            f"成功 {sum(r['resolved'] for r in rows)}，"
            f"基础设施错误 {sum(r['infrastructure_errors'] for r in rows)}。"
            "官方不对空补丁启动测试。",
            "",
            f"动作格式错误 {sum(r['format_errors'] for r in rows)} 次；"
            f"压缩触发 {sum(r['compressions'] for r in rows)} 次，"
            f"接受 {sum(r['accepted_compressions'] for r in rows)} 次。"
            "逐臂模型层请求数见 pilot-summary.json。",
            "仅同一实例的开发诊断，不能支持总体收益或非劣结论。"
            "复测不能与首轮合并为独立样本；原始失败结果继续保留。",
            "",
            f"本轮计划 {workflow['plan_id']}；前序计划 {args.prior_plan_id or '无'}。",
            "通用 REPORT.md 的缺失判分表示无逐实例测试报告；"
            "本文件结合 official-summaries/ 区分了官方空补丁分类。",
        ]
    )
    (output / "PILOT_REPORT.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"new_usage_cost_usd": str(total), "rows": len(rows)}, indent=2))


if __name__ == "__main__":
    main()
