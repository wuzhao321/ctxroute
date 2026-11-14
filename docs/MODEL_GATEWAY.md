# 模型请求与结算

模型网关通过 [DeepSeekModel](../src/ctxroute/models/deepseek.py) 接入直接 HTTP 或 [PydanticAI 传输](../src/ctxroute/models/pydantic_adapter.py)。配置位于 runtime、价格快照与各实验臂。运行计划冻结这些文件及源码，内容改变后应生成新计划。

## 请求传输

模型标识由价格配置选择。任务请求包括 messages、max_tokens、temperature、thinking、stream，以及可选 response_format。PydanticAI 的模型 profile 将输出限制序列化为 DeepSeek Chat Completions 使用的 max_tokens 字段。JSON 输出设置与 thinking 通过 extra_body 传递。SDK 自动重试关闭，单次适配器调用只允许一个请求。执行器是否再次尝试由 runtime.model_attempts 控制。

任务上下文由一条 system 和一条 user 消息组成。user 包括任务、状态、结构化记忆及历史操作。bash 等工具在宿主侧校验后执行，模型请求未携带 API tools。当前适配器取最终 content 作为动作文本，completion_tokens 作为完整输出用量。thinking 模式的采样参数行为需按供应商接口核对。

## 发送前预留

网关使用输入护栏、最大输出与高峰费率计算预留，在派发请求之前通过共享 [Ledger](../src/ctxroute/tracing/budget.py) 检查总额度和任务 scope。显式选择阶段时，由 [StageLedger](../src/ctxroute/tracing/stage_budget.py) 在同一事务中增加阶段、分配、配对和单请求限制；预留公式及换算规则见 [BUDGET_SCOPES](BUDGET_SCOPES.md)。输出限制由服务端执行，输入护栏为本地调度约束，输入估算不是供应商 tokenizer 的保证上界。

账本占用等于已结算金额加未结算预留。下一请求将超过 scope、全局硬上限或暂停阈值时停止派发。每个 request_id 只能预留一次。模型动作、压缩和显式重试都消耗同一个额度。

## 收到 usage 后结算

响应 usage 必须满足非负整数、缓存命中量不大于输入量、缓存命中与未命中之和等于输入量等校验。明确处于非高峰区间时按非高峰价结算，其余区间用高峰价对已返回的 usage 保守结算，并释放超出结算值的预留。输入、输出或价格超出已声明护栏时先锁停。请求无 usage 或无法合法归一化时保留预留。

费用记录是基于配置价格的估算。供应商最终账单需单独核对。请求、usage、缓存及价格时段应按 request_id 关联，不能将估算和相同请求的预留重复计为支出。

[api_probe](../src/ctxroute/api_probe.py) 是独立的固定连通性探针，其结算流程不与任务网关完全相同。项目中的 [PREREG](PREREG.md) 是计划冻结输入，可能保留相应版本的协议描述。新计划应明确所采用的网关与结算实现版本。

流式请求还要求原始终结原因和用量合法，并与 SDK 结果一致。`[DONE]`、HTTP 流结束、SDK 返回输出以及动作通过校验分别记录；任一条件都不能单独授权执行。可选推理 token 数是完整输出用量的组成部分，不额外重复计费。字节、事件与总耗时边界、拒绝分类及可选恢复规则见 [REQUEST_RECOVERY](REQUEST_RECOVERY.md)。

## 停止与记录

同一次调用的预留、用量和失败事件共用本地 `request_id`。失败事件保留原有的 `model_request_failed` 类型、步骤、用途和耗时。网关在 SDK 或直接 HTTP 传输边界采集以下字段，响应返回后的本地用量校验也可以关联已收到的响应头。

| 失败事件字段 | 含义 |
|---|---|
| `failure_kind` | 限流、HTTP 错误、超时、连接异常、无效用量、本地校验等；流式路径另区分 `invalid_stream`、`output_exhausted`、`empty_answer`、`model_terminated`、`sdk_output_rejected`。规则见 [请求完成与恢复](REQUEST_RECOVERY.md)。诊断提取失败为 `diagnostic_capture_failed`。 |
| `exception_type`、`root_exception_type` 及各自的 `*_message` | 外层和异常链底层的类名。只有预定义的安全消息可以写入，其他消息为 null。 |
| `http_status`、`provider_request_id` | 实际收到的状态码和可安全保留的上游请求标识。没有响应或标识时，相应字段为 null。 |
| `response_body_redacted` | HTTP 错误仅保留已解析错误对象中的白名单值。无安全字段时保存省略原因，其他事件为 null。采集器不读取响应流或写入自由文本。 |
| `request_body_bytes` | 已序列化、交给传输层的请求字节数。未观察到请求体时为 null。它不能证明服务端已收到全部内容。 |
| `is_timeout`、`timeout_stage` | 根据异常链识别超时及可取得的 connect、read、write 或 pool 阶段。信息不足为 null。 |
| `exception_chain_types` | 遍历 cause/context 和异常组分支后得到的类名列表，最多 32 项；异常树可能没有唯一根因。 |
| `stream_terminal`、`sdk_output_accepted` | 原始 SSE 的终结原因、合法 usage、内容存在位，与 SDK 是否返回输出分开记录。内容存在位不代表合法答案，SDK 返回也不代表动作通过校验。 |

对账时区分本地标识与上游标识。请求头、提示、推理内容、任务源码和凭据不进入失败诊断。无法核实用量时，原预留继续占用预算。历史失败事件可能缺少上述字段。

执行器另累计返回的高峰价成本上界，作为任务本地停止条件。响应返回后触发该条件时，请求已经产生费用，但动作可能尚未执行。报告应分别统计已派发请求、已记录动作、已执行工具及官方判分结果。

供应商接口语义参考 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)。
