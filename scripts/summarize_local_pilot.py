"""Reconcile trace costs with official empty-patch summaries without inventing test grades."""

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow_dir = root / "results/workflows/arm64-smoke-20261024"
    output = root / "results/analysis/arm64-smoke-20261024"
    receipts = json.loads((workflow_dir / "grading-receipts.json").read_text())
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
        rows.append(
            {
                "arm": manifest["arm"],
                "run_id": manifest["run_id"],
                "termination": manifest["termination"],
                "steps": manifest["steps"],
                "model_requests": len(usage),
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
                "tier_requests": dict(Counter(r["tier"] for r in usage)),
            }
        )
    control = json.loads((workflow_dir / "negative-control.json").read_text())
    control_bytes = (root / control["official_summary"]).read_bytes()
    if hashlib.sha256(control_bytes).hexdigest() != control["official_summary_sha256"]:
        raise ValueError("Negative-control summary changed")
    (summaries / "infrastructure-negative-control.json").write_bytes(control_bytes)
    total = sum((Decimal(r["usage_cost_usd"]) for r in rows), Decimal(0))
    result = {
        "kind": "first_dev_pilot_official_summary_reconciliation",
        "instance_id": "sympy__sympy-22840",
        "arms": rows,
        "new_usage_cost_usd": str(total),
        "invoice_verified": False,
        "unknown_prior_reservation_usd": "0.10",
        "new_cloud_cost_usd": "0",
        "negative_control_excluded_from_model_results": True,
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
        "# 首个真实 dev pilot：运行结果与官方汇总",
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
            "五份官方汇总均为空补丁、成功数 0、基础设施错误数 0。"
            "官方不对空补丁启动测试；不能声称五份补丁都执行了测试。",
            "独立基础设施负对照已运行官方测试，结果未解决且无基础设施错误；"
            "该人工 marker 补丁不计入模型结果。",
            "",
            "A1–A4 的原始响应出现 XML/解释性文本及额外 JSON 结束括号，"
            "连续两次无效后按冻结规则结束；A0 在下一次模型调用前达到上下文上限。",
            "所有轨迹均未触发压缩；本批不能支持压缩收益、路由收益或非劣结论。",
            "",
            "下一阶段应先解决动作协议可靠性，再建立新版本计划复测；不得覆盖这批失败结果或更换题目。",
            "通用 REPORT.md 的缺失判分表示无逐实例测试报告；"
            "本文件结合 official-summaries/ 区分了官方空补丁分类。",
        ]
    )
    (output / "PILOT_REPORT.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"new_usage_cost_usd": str(total), "rows": len(rows)}, indent=2))


if __name__ == "__main__":
    main()
