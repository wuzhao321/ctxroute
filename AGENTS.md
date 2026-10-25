# ctxroute 工作约定

## 当前范围

最新用户指令（2026-10-24）优先：开启真实评测并完成所需准备，问题出现后反馈。用户已明确允许本机 ARM64 小规模评测，不租云主机；允许必要依赖与隔离容器环境安装、API 与固定 dev pilot。累计 10 USD、8 USD 暂停、原始划分与样本顺序不变，正式 test 仍暂缓。已有未知费用预留必须保留，可选 A2A/学习路由仍后置。

用户确认：API 与云主机合计最多 10 USD，总日历时长 3 天；本机 macOS 26.4、M5 arm64、10 核、16 GB 内存，用于开发及单并发 ARM64 dev 冒烟。不得开展模型训练。GitHub 账号 wuzhao321；仓库名称、可见性、首次发布尚未确定。

用户已确认 3 天缩减方案：本轮交付最小实现、固定 dev pilot（资源允许时）与诚实报告，正式 test 暂缓。先读 docs/DECISIONS.md 和 docs/PREREG.md。P0 本地代码质量门已通过，API 连通已验证，完整 P0 的 Docker 与 harness 验收仍待执行。

最新实测（2026-10-24）：本机 Docker/MCP 隔离验收与官方 harness 基础设施负对照已通过。首题 A0–A4 五臂已执行，均为空补丁：A0 context_limit，A1–A4 format_failure；未触发压缩，所有请求使用 strong。本轮 usage 费用 0.073165268 USD，累计 0.073168868 USD，另保留 0.10 USD 未知预留；未核验账单。报告见 results/analysis/arm64-smoke-20261024/PILOT_REPORT.md。不得覆盖首轮结果或继续原清单重跑；后续先处理动作协议可靠性，再冻结新版本计划。VM 完成后停止以释放内存，镜像保留。

## 实验与费用

2026-10-25 最新阶段：动作协议 v2 同题复测计划 2ddf897834db7a6a7e18 完成。A1–A4 的 200 个动作响应无 JSON 解析失败，A4 两次 remember 校验错误；五次压缩被接受，廉价动作请求 0。A0 非空补丁官方测试未解决，A1–A4 到 50 步仍为空补丁。累计 usage 估算 0.313874404 USD，未知预留 0.10 USD，VM 已停止。下一阶段先处理重复命令、进展/提交反馈及只读分类，不覆盖两轮结果；详见 results/analysis/arm64-json-v2-20261025/RETEST_REVIEW.md。

- 证据优先：数字来自 results/ 中的原始记录及分析脚本；未测写“未测”。不得挑选有利任务、seed 或隐藏失败。
- 任何付费工作前先确认执行环境和冻结的运行清单，打印预计 / 已花费 / 硬上限；总额包括 API、重试、压缩器、路由标签生成、云端计算、存储及网络费用。
- 8 USD 暂停询问，10 USD 为硬上限。每次请求和云资源创建前预留最坏费用；未知 usage 或未出账费用不得当作零。预算不足时停止，不通过继续降低任务数掩盖结果。
- test 结果之前必须另行提交正式 test 预注册。当前 PREREG 仅冻结本轮 pilot 选择和预算规则，不能据此启动正式 test；data/split.json 已冻结，pilot 只使用预先固定的 dev 实例。
- 仅官方 swebench harness 判分。任务 Agent 只接收允许的数据字段，不得获得 gold patch、test_patch、判分脚本或隐藏测试；判分独立容器执行。
- 密钥仅从环境变量或 .env 读取；不输出、不提交，不传入任务容器。先有 .gitignore 再创建 .env。
- 同一问题尝试两次仍未解决，停止该问题并汇报。每阶段验收后等待用户说“继续”。

## 安装与外部操作

先检测后安装。系统级软件、项目外删除、docker system prune、远端推送、超过已批预算的调用需要用户明确授权。GitHub 插件已连接不等于已经创建仓库；不得假定插件认证可供本地 Git 使用。

## 目录和 skills

- src/ctxroute/：后续实现 orchestrator、context、router、tools、sandbox、a2a、tracing、evalkit。
- configs/：模型、价格引用、实验臂和预算配置；禁止在算法里硬编码。
- data/：冻结 split 与版本化价格表；results/runs/{run_id}/：manifest、trace、预测、harness 日志；docs/：协议和证据说明。
- 按任务读取 .agents/skills/{swebench-runner,experiment-protocol,sandbox-hardening,trace-analysis}/SKILL.md。

## 质量门和命令

本地一键检查：`sh scripts/check.sh`，使用项目 .tools/uv-package/bin/uv 或 CTXROUTE_UV 指定的 uv，执行冻结依赖下的 ruff check、ruff format --check、pyright、pytest。Python 3.12、依赖通过 uv.lock 锁定。当前 CLI `ctxroute status` 只读配置，不访问网络或密钥。阶段验收还需满足对应实测项；本地质量门不等于完整 P0 通过。
