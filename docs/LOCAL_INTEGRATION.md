# 本地依赖与 API 联调记录

日期：2026-10-23。用户确认密钥已在本机设置，并要求先完成本地依赖与 API 联调；没有可用 Linux 主机或云账号。本阶段完成，不启动评测环境。

## 已完成

- 项目 `.venv` 安装并锁定 PydanticAI 2.59.0、MCP 1.30.0、mini-swe-agent 2.4.6、swebench 5.0.2；完整依赖哈希在 uv.lock，未安装系统软件。
- 上游 mini-swe-agent 关键源码核对通过；真实 MCP stdio 握手及四方法发现通过，未执行沙箱工具。官方 harness 的命令行帮助可正常加载。
- HTTP 与 PydanticAI 均成功调用 `deepseek-flash`，固定提示返回 `OK`；thinking disabled、最多 32 输出 token、每次预留 0.10 USD，无自动重试。
- PydanticAI 使用 DeepSeek provider，保留原始缓存命中/未命中 token 字段，限制一次 SDK 请求，关闭客户端重试、代理与重定向。
- 现有必要检查最终为 ruff/格式通过、pyright 0 errors、62 tests passed。仅对发现的 SDK 兼容问题追加一次离线 HTTP 合同检查；没有新增实验或测试矩阵。

## 调用与费用证据

汇总由 `scripts/summarize_probes.py` 从 `results/runs/api-probe-*/` 原始记录及只读账本生成，位于 `results/validation/runtime-setup/api-summary.json`。该脚本不调用 API、不读取密钥。

| 尝试 | 传输 | 结果 | 耗时 | 输入 / 缓存命中 / 输出 token | 按 usage 计算 USD |
|---|---|---|---|---|---|
| 003150-7ddee7ca | HTTP | 通过 | 1929.451 ms | 8 / 0 / 1 | 0.0000018 |
| 003208-2eee4dcd | PydanticAI | 失败 | 未记录 | 未知 | 未知；保留 0.10 预留 |
| 003310-1467bd89 | PydanticAI | 修复后通过 | 2259.962 ms | 8 / 0 / 1 | 0.0000018 |

第一次 SDK 尝试失败后发现已安装版本的 `result.usage` 是属性，而旧适配器使用方法调用。已修复并通过第二次实际请求；首个失败只记录通用安全错误，不能据此还原供应商 usage 或宣称其未计费。失败记录未删除、未覆盖，后续故障记录增加不含异常正文的异常类名。

两次成功请求的 usage 核算合计 **0.0000036 USD**，另有 **0.10 USD 未结算预留**。供应商账单尚未核验；预算占用为 0.1000036 USD，累计硬上限 10 USD、暂停线 8 USD。未知预留不得按零释放，不能删除账本重置预算。未创建云资源。

## 边界与后续

本次仅证明本地依赖、固定 API 请求及 usage 记账可用。尚未验证实际 Agent 多步任务、压缩/路由效果、A0/A1 一致性、Docker 隔离或官方 harness 判分；dev/test 指标全部未测。

API 预算开关为 true，实验 runtime 保持 offline_only。密钥仅存于用户本机受保护的 .env 或环境变量，不写入记录或 Git。三次尝试的 manifest 均引用各自调用前的已提交代码版本；所有改动仅本地提交，未推送远端。
