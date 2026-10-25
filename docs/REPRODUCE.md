# 分阶段复现入口

入口统一准备、推理、预测交接与分析。2026-10-24 首题 ARM64 pilot 已运行，实际清单位于 results/plans/arm64-smoke-20261024.json。当前 runtime 为 live，VM 已停止；下面是接口模板，不能覆盖首轮结果。后续实现修改后需新建计划。

## 1. 固定清单与准备

```sh
.venv/bin/ctxroute experiment plan --arms A0 A1 A2 A3 A4 --count 5 --output results/plans/pilot-v2.json
.venv/bin/ctxroute experiment reproduce prepare --plan results/plans/pilot-v2.json --workflow results/workflows/pilot-v2
```

准备阶段只读已冻结的计划并写 workflow.json，不读取密钥或题目，不创建预算账本。按 arm、variant、seed 分组，所有计划任务都保留。相同内容可重复准备，已有不同内容禁止覆盖。代码或运行配置改变后必须生成新计划及新 workflow，包括从 offline_only 切换到 live 的情况。

## 2. 推理（显式付费执行）

```sh
.venv/bin/ctxroute experiment reproduce infer --workflow results/workflows/pilot-v2 --tasks data/private/tasks.jsonl --images configs/private/images.json --execute
```

需要 live 配置、启用预算、可用的隔离环境、完整任务白名单和镜像摘要。沿用既有任务级续跑及费用预留，不自动重试中断任务，不放宽预算。此命令不判分；Agent 进程不读取判分报告。

## 3. 预测交接

```sh
.venv/bin/ctxroute experiment reproduce export --workflow results/workflows/pilot-v2
```

全部计划任务必须有完成 manifest 和对应的预测；运行身份必须匹配计划。空补丁、预算终止及失败任务均保留，未开始/中断任务阻止交接，不悄悄缩减集合。

每个 arm/variant/seed 单独导出 predictions/*.jsonl；handoff.json 记录每个原始预测与导出文件的 SHA-256、唯一 grading_run_id，以及官方 harness 的 argv。该阶段不执行 argv。独立判分环境从项目根目录执行这些命令，并保留官方日志与版本；预测文件须按交接中的相对路径复制，不能把旧 grading_run_id 用于不同补丁。环境安装、镜像构建及资源费用不由此命令自动处理。

## 4. 官方报告导入与分析

将官方每实例 report.json 放入本项目的 results/private/，创建 JSON 映射：

```json
{
  "计划ID-0000": "results/private/grading/A0/实例ID/report.json",
  "计划ID-0001": "results/private/grading/A1/实例ID/report.json"
}
```

键必须为计划对应的推理 run_id；报告内容须为官方的实例 ID → resolved 布尔值格式。操作者仍需核对报告的 grading_run_id、补丁与官方日志；结构和哈希校验不等于认证报告来源。

```sh
.venv/bin/ctxroute experiment reproduce analyze --workflow results/workflows/pilot-v2 --reports results/private/reports.json --output results/analysis/pilot-v2
```

导入前检查预测没有偏离交接哈希。已导入的相同报告可以复用，变化的报告不能覆盖原 grade；已有分析目录不能覆盖。映射可缺少未返回的判分，缺失项保留为未测。输出 runs.csv、analysis.json、REPORT.md、failure_review.csv 及 workflow-provenance.json。失败分类仍需人工填写；绘图继续使用可选 plot 命令，不自动安装绘图库。

Makefile 对应入口：`make reproduce ARM=A4 STAGE=prepare`，默认只准备已有 PLAN；推理须显式使用 `STAGE=infer EXECUTE=--execute`。PLAN、WORKFLOW、TASKS、IMAGES、REPORTS、OUTPUT 均可覆盖。当前支持固定 dev pilot，未开放正式 test 或多 seed 调度。

## 2026-10-25 同题复测记录

新计划 results/plans/arm64-json-v2-20261025.json，workflow 同名目录。grade_local_pilot.py 现将私有报告写入 results/private/harness/{plan_id}/reports.json，避免覆盖前次映射。汇总脚本现要求显式路径：

```sh
.venv/bin/python scripts/summarize_local_pilot.py --workflow results/workflows/arm64-json-v2-20261025 --output results/analysis/new-summary-directory --prior-plan-id 93d5e0763da19ee86a9c
```

输出目录内已有 pilot-summary.json 或 PILOT_REPORT.md 时拒绝覆盖。真实结果和下一阶段重点见 results/analysis/arm64-json-v2-20261025/RETEST_REVIEW.md。
