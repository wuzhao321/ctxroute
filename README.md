# ctxroute

## 项目目的

ctxroute 是面向软件工程 Agent 的实验框架，用于研究结构化上下文压缩与步骤级模型路由。它将代码修复任务的执行、上下文管理、模型选择和评测记录组织在统一流程中，便于比较不同策略对任务完成情况、模型调用成本和执行延迟的影响。

## 项目内容

- **Agent 执行与对照策略**：适配 mini-swe-agent 原生执行循环，并提供 A1–A5 配置，覆盖固定模型、结构化压缩、规则路由及组合策略。
- **上下文与记忆管理**：划分系统任务、固定证据、结构化摘要和近期步骤，支持历史输出存储、证据引用与按需召回。
- **步骤级模型路由**：依据命令类型、执行结果和连续步骤状态选择模型层级，通过统一接口接入模型服务。
- **工具与隔离执行**：提供 Docker 沙箱与 MCP 工具接口，管理命令执行、资源限制、补丁提取和容器生命周期；动作协议与进展控制负责校验输出和处理重复动作。
- **实验编排与分析**：提供实验清单冻结、任务级续跑、费用预留与结算、请求和步骤轨迹、预测导出、SWE-bench 官方判分对接、消融配置和配对统计分析。

## 项目结构与导航

| 路径 | 内容 |
|---|---|
| [src/ctxroute/orchestrator/](src/ctxroute/orchestrator/) | Agent 主循环、终止规则与进展控制 |
| [src/ctxroute/context/](src/ctxroute/context/) | 上下文分区、结构化记忆、压缩与历史输出召回 |
| [src/ctxroute/router/](src/ctxroute/router/) | 模型层级选择规则 |
| [src/ctxroute/models/](src/ctxroute/models/) | 模型请求协议、DeepSeek 与 PydanticAI 传输适配 |
| [src/ctxroute/baselines/](src/ctxroute/baselines/) | mini-swe-agent 基线适配 |
| [src/ctxroute/sandbox/](src/ctxroute/sandbox/) / [tools/](src/ctxroute/tools/) | Docker 沙箱与 MCP 客户端、服务端 |
| [src/ctxroute/tracing/](src/ctxroute/tracing/) | 预算账本、调用成本与运行轨迹 |
| [src/ctxroute/evalkit/](src/ctxroute/evalkit/) | 实验编排、复现流程、判分交接与统计分析 |
| [src/ctxroute/cli.py](src/ctxroute/cli.py) | 命令行入口 |
| [configs/](configs/) | 实验臂、消融、模型运行、预算与沙箱配置 |
| [data/](data/) | 数据划分与版本化模型价格表 |
| [scripts/](scripts/) | 数据准备、环境工具与评测辅助脚本 |
| [tests/](tests/) | 核心模块和工作流测试 |
| [pyproject.toml](pyproject.toml) / [uv.lock](uv.lock) | 项目依赖、工具配置与依赖锁定 |

运行时生成的清单、轨迹、预测和分析报告保存在本地 `results/`，不纳入版本控制。

| 文档 | 查看内容 |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | 整体架构、模块职责与数据流 |
| [API_SETUP](docs/API_SETUP.md) | 模型服务配置与密钥设置 |
| [REPRODUCE](docs/REPRODUCE.md) | 准备、推理、导出和分析流程 |
| [PREREG](docs/PREREG.md) | 实验设计与清单冻结规则 |
| [A0_ADAPTER](docs/A0_ADAPTER.md) | 原生基线的接入方式与接口边界 |
| [STRUCTURED_MEMORY](docs/STRUCTURED_MEMORY.md) | 记忆结构、证据引用与校验规则 |
| [ACTION_PROTOCOL](docs/ACTION_PROTOCOL.md) | 动作格式、解析与纠错机制 |
| [PROGRESS_CONTROL](docs/PROGRESS_CONTROL.md) | 进展反馈、重复动作检测与停止规则 |
