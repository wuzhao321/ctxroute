# ctxroute 工作约定

## 当前范围

最新用户指令（2026-10-24）优先：开启真实评测并完成所需准备，问题出现后反馈。用户已明确允许本机 ARM64 小规模评测，不租云主机；允许必要依赖与隔离容器环境安装、API 与固定 dev pilot。累计 10 USD、8 USD 暂停、原始划分与样本顺序不变，正式 test 仍暂缓。已有未知费用预留必须保留，可选 A2A/学习路由仍后置。

用户确认：API 与云主机合计最多 10 USD，总日历时长 3 天；本机 macOS 26.4、M5 arm64、10 核、16 GB 内存，用于开发及单并发 ARM64 dev 冒烟。不得开展模型训练。GitHub 账号 wuzhao321；仓库名称、可见性、首次发布尚未确定。

用户已确认 3 天缩减方案：本轮交付最小实现、固定 dev pilot（资源允许时）与诚实报告，正式 test 暂缓。先读 docs/DECISIONS.md 和 docs/PREREG.md。P0 本地代码质量门已通过，API 连通已验证，完整 P0 的 Docker 与 harness 验收仍待执行。

## 实验与费用

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
