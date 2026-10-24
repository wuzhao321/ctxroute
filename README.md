# ctxroute

面向软件工程 Agent 的结构化上下文压缩与步骤级模型路由实验框架。

当前已编写 A0 原生执行适配及 A1–A5 核心循环、压缩与路由、沙箱/MCP 适配、预算和 trace，以及批量调度、消融和统计分析代码。已完成本地运行依赖安装与 HTTP/PydanticAI API 联调。**2026-10-24 已恢复真实评测准备，执行位置待确定**；已固定兼容数据的官方 harness 3.0.17，并提取首个 dev 四字段输入。API 预算开关已开启，benchmark runtime 仍为 offline_only，没有实验效果数据。详见 [本地联调记录](docs/LOCAL_INTEGRATION.md)。

架构与实现边界见 [ARCHITECTURE](docs/ARCHITECTURE.md)，功能状态见 [IMPLEMENTATION_STATUS](docs/IMPLEMENTATION_STATUS.md)。预算保持 API 与云资源合计最多 10 USD，8 USD 暂停，不训练模型。

## 离线准备命令

使用已有项目 Python 环境，不需要密钥、Docker 或网络：

```sh
.venv/bin/ctxroute status
.venv/bin/ctxroute experiment plan --arms A0 A1 A2 A3 A4 --count 5 --output results/plans/main.json
.venv/bin/ctxroute experiment plan --arms A2 A4 --count 5 --truncate --output results/plans/truncate.json
```

清单只写入固定任务 ID、参数和哈希，不加载题目、不执行任务，也不授权付费。输出文件不可覆盖。改变参数后需要新清单。当前 pilot 的数量和 seed 限制沿用预注册；test 暂不开放。

## 本轮代码补齐

结构化记忆已升级为带路径/行范围、退出码、假设状态和证据引用的 v2；P1 支持精确引用历史工具观察，详见 [记忆接口](docs/STRUCTURED_MEMORY.md)。新的 [分阶段复现入口](docs/REPRODUCE.md) 串联准备、推理、预测交接和报告分析。`make reproduce` 默认仅准备已有清单，推理需另选阶段及显式执行；判分保持在独立环境。当前没有真实任务结果，阶段总结见 [REPORT](docs/REPORT.md)。

## 已编写的后续执行入口

以下用于说明代码接口，当前配置下不会执行。这些不是本轮操作要求。

```sh
# tasks.jsonl 必须是去除答案字段后的四字段输入；images.json 映射 instance_id 到镜像摘要。
.venv/bin/ctxroute experiment run --plan results/plans/main.json --tasks data/private/tasks.jsonl --images configs/private/images.json --execute

# 每次导出只含一个 arm、variant、seed。
.venv/bin/ctxroute experiment export --runs results/runs/RUN_ID --output results/predictions.jsonl
.venv/bin/ctxroute experiment harness-command --plan results/plans/main.json --predictions results/predictions.jsonl --run-id GRADING_RUN_ID

# harness-command 只打印 argv，官方 harness 须在独立判分环境执行。
.venv/bin/ctxroute experiment import-grade --run results/runs/RUN_ID --report PATH_TO_OFFICIAL_REPORT
.venv/bin/ctxroute experiment analyze --runs results/runs/RUN_ID --output results/analysis/report
```

`run` 支持任务级续跑；中断任务不会自动重新付费。`analyze` 生成 runs.jsonl、runs.csv、analysis.json、failure_review.csv 和 REPORT.md；配对比较需要多个臂和相同实例/seed 的有效证据。费用为含缓存的 usage 成本上界，真实账单另行对账。合成运行不会进入正式分析或预测导出。

A0 已接入统一运行入口，保留上游原生文本循环并替换模型/沙箱 I/O，详见 [A0 接入说明](docs/A0_ADAPTER.md)。真实联调和 A0/A1 一致性未验证；独立证据仍可通过 `experiment import-baseline --source DIR --run-id ID` 导入。A6/A7、学习路由、本地模型按计划后置。可选 `experiment plot --analysis DIR --output FIGURE.png` 使用 matplotlib，本轮未安装或运行。

## 代码与证据

- [预注册](docs/PREREG.md)、[冻结划分](data/split.json)：dev/test 分离与固定 pilot 顺序。
- [决策记录](docs/DECISIONS.md)：用户范围调整、实现偏差与历史记录。
- [Claims Ledger](docs/CLAIMS.md)：效果指标继续标记未测。
- `configs/arms/`：A0–A7 定义；`configs/ablations/`：A2/A4 截断消融。
- `src/ctxroute/`：完整模块划分见架构文档。
- `tests/`：已有离线测试。本轮不扩展验证矩阵、不运行模拟实验。

需要检查代码时可运行 `sh scripts/check.sh`，不会调用模型或 Docker。现有 63 项测试包含一个[离线链路用例](docs/FULL_CHAIN_CHECK.md)，使用临时玩具文件和报告格式样例，不是 SWE-bench 结果。runtime extra 已安装并锁定；重建时使用 `.tools/uv-package/bin/uv sync --frozen --extra runtime --group data`。检查命令使用 --no-sync 保留已安装的可选依赖。API 操作见 [API_SETUP](docs/API_SETUP.md)；不要把连通成功当作 benchmark 成功。
