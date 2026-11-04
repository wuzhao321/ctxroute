# 模型请求与结算

模型网关通过 [DeepSeekModel](../src/ctxroute/models/deepseek.py) 接入直接 HTTP 或 [PydanticAI 传输](../src/ctxroute/models/pydantic_adapter.py)。配置位于 runtime、价格快照与各实验臂。运行计划冻结这些文件及源码，内容改变后应生成新计划。

## 请求传输

模型标识由价格配置选择。任务请求包括 messages、max_tokens、temperature、thinking、stream，以及可选 response_format。PydanticAI 的模型 profile 将输出限制序列化为 DeepSeek Chat Completions 使用的 max_tokens 字段。JSON 输出设置与 thinking 通过 extra_body 传递。SDK 自动重试关闭，单次适配器调用只允许一个请求。执行器是否再次尝试由 runtime.model_attempts 控制。

任务上下文由一条 system 和一条 user 消息组成。user 包括任务、状态、结构化记忆及历史操作。bash 等工具在宿主侧校验后执行，模型请求未携带 API tools。当前适配器取最终 content 作为动作文本，completion_tokens 作为完整输出用量。thinking 模式的采样参数行为需按供应商接口核对。

## 发送前预留

网关使用输入护栏、最大输出与高峰费率计算预留，在派发请求之前通过共享 [Ledger](../src/ctxroute/tracing/budget.py) 检查总额度和任务 scope。输出限制由服务端执行，输入护栏为本地调度约束，输入估算不是供应商 tokenizer 的保证上界。

账本占用等于已结算金额加未结算预留。下一请求将超过 scope、全局硬上限或暂停阈值时停止派发。每个 request_id 只能预留一次。模型动作、压缩和显式重试都消耗同一个额度。

## 收到 usage 后结算

响应 usage 必须满足非负整数、缓存命中量不大于输入量、缓存命中与未命中之和等于输入量等校验。明确处于非高峰区间时按非高峰价结算，其余区间用高峰价对已返回的 usage 保守结算，并释放超出结算值的预留。输入、输出或价格超出已声明护栏时先锁停。请求无 usage 或无法合法归一化时保留预留。

费用记录是基于配置价格的估算。供应商最终账单需单独核对。请求、usage、缓存及价格时段应按 request_id 关联，不能将估算和相同请求的预留重复计为支出。

[api_probe](../src/ctxroute/api_probe.py) 是独立的固定连通性探针，其结算流程不与任务网关完全相同。项目中的 [PREREG](PREREG.md) 是计划冻结输入，可能保留相应版本的协议描述。新计划应明确所采用的网关与结算实现版本。

## 停止与记录

请求记录包含本地标识、步骤、用途、模型、派发时间、usage、缓存、耗时及成本上下界。当前通用异常分支只保存统一错误类型，可能无法恢复底层 HTTP 状态或错误原因。该记录能力不保证每次上游故障都可精确归因。

执行器另累计返回的高峰价成本上界，作为任务本地停止条件。响应返回后触发该条件时，请求已经产生费用，但动作可能尚未执行。报告应分别统计已派发请求、已记录动作、已执行工具及官方判分结果。

供应商接口语义参考 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)。
