# 实验执行与复现

实验分为准备、推理、预测交接、独立判分和分析。运行前需安装锁定依赖，配置模型密钥、价格快照、预算及隔离执行环境。模型配置见 [API_SETUP](API_SETUP.md)，比较与停止规则见 [PREREG](PREREG.md)。

以下命令从项目根目录执行，路径为示例；每次配置或源码变更应使用新的计划与输出目录。

## 1. 冻结计划

```sh
.venv/bin/ctxroute experiment plan --arms A0 A1 A2 A3 A4 --count 1 --output results/plans/dev-pilot.json
.venv/bin/ctxroute experiment reproduce prepare --plan results/plans/dev-pilot.json --workflow results/workflows/dev-pilot
```

计划冻结划分、实验协议、配置、价格和源码指纹。准备阶段按 `arm`、`variant`、`seed` 分组并写入 `workflow.json`，不读取密钥或发送请求。相同内容可以重复准备，不同内容禁止覆盖。

当前计划器仅接受 `data/split.json` 中 dev 前 1–5 项、固定 seed 和 A0–A5；A2/A4 可选择 `--truncate` 生成消融计划。它不提供正式 test 或多 seed 调度。

## 2. 准备输入与镜像

任务 JSONL 每行只能包含：

```json
{"instance_id":"example__project-1","repo":"example/project","base_commit":"COMMIT_SHA","problem_statement":"Issue description"}
```

实际实例必须与计划完全匹配，不得包含 gold patch、测试补丁或判分字段。镜像映射格式由 [`evalkit/cli.py`](../src/ctxroute/evalkit/cli.py) 的注册表读取逻辑定义，镜像需固定摘要，并满足 [`sandbox/docker.py`](../src/ctxroute/sandbox/docker.py) 的隔离约束。

工具镜像与判分环境分离；构建镜像、安装依赖或下载数据由独立准备流程完成，执行入口不会自动创建云资源。

## 3. 显式执行推理

```sh
.venv/bin/ctxroute experiment reproduce infer --workflow results/workflows/dev-pilot --tasks data/private/tasks.jsonl --images configs/private/images.json --execute
```

需要 runtime 的 `execution_mode=live`、启用预算、有效价格快照和可用沙箱。请求派发前预留最坏费用，开始同一实例时检查其剩余各臂的完整额度。

恢复以任务为单位：完成任务跳过；中断任务保留证据与未结算预留，禁止自动重跑。失败、空补丁、预算终止均保留，不据结果替换实例。

## 4. 导出预测与独立判分

```sh
.venv/bin/ctxroute experiment reproduce export --workflow results/workflows/dev-pilot
```

所有计划任务必须有完成 manifest 和匹配预测。每个 `arm/variant/seed` 单独导出 `predictions/*.jsonl`；`handoff.json` 记录原始及合并预测哈希、唯一 `grading_run_id` 和官方 harness 命令参数。导出阶段不会运行判分。

在独立判分环境中，使用交接记录中的命令与预测执行官方 SWE-bench harness，保留版本、run ID 和日志。改变补丁必须使用新判分 ID。空补丁和失败任务仍属于导出集合，官方可能不会为其生成逐实例测试报告。

## 5. 导入报告并分析

将官方逐实例报告存入本地 `results/private/`，建立推理 `run_id` 到报告路径的 JSON 映射：

```json
{
  "PLAN_ID-0000": "results/private/grading/A0/example__project-1/report.json",
  "PLAN_ID-0001": "results/private/grading/A1/example__project-1/report.json"
}
```

报告格式为实例 ID 到包含布尔值 `resolved` 的对象映射。导入前核对官方 run ID、补丁与日志；文件结构和哈希不能认证报告来源。未返回的逐实例报告可以不出现在映射中，分析保留缺失项，必要时结合官方汇总说明空补丁分类。

```sh
.venv/bin/ctxroute experiment reproduce analyze --workflow results/workflows/dev-pilot --reports results/private/reports.json --output results/analysis/dev-pilot
```

分析核对交接哈希，导入 `grade.json` 并输出 `runs.csv`、`analysis.json`、`REPORT.md`、`failure_review.csv` 和 `workflow-provenance.json`。相同报告可复用，不同报告不能覆盖已导入判分；已有分析目录不能覆盖。失败分类需要根据轨迹填写，可选绘图使用 `experiment plot`。

## Makefile

```sh
make plan ARM=A4 COUNT=1
make reproduce ARM=A4 STAGE=prepare
make reproduce ARM=A4 STAGE=infer EXECUTE=--execute
```

`PLAN`、`WORKFLOW`、`TASKS`、`IMAGES`、`REPORTS`、`OUTPUT` 均可覆盖。默认 `STAGE=prepare`；推理必须显式提供执行参数。
