# 动作协议 v2：代码阶段记录

日期：2026-10-25。范围：修复首轮 dev pilot 暴露的动作格式问题，完成必要离线验收。没有新增 API 请求、容器运行或 benchmark 结果。

## 原因与实现

首轮 A1–A4 在连续两次无效动作后终止，原始响应包含 XML、解释文字或多余 JSON 结束括号。原请求仅靠提示要求 JSON，没有发送供应商的 JSON 输出参数。这是已确认的请求约束缺口；补上参数能否消除实际格式失败，仍需真实复测。

- ModelRequest.response_format 明确区分 text 与 json_object。ctxroute Engine 的动作和压缩请求均选 JSON；A0 原生文本适配器保持默认文本请求。
- DeepSeek 请求发送 response_format={"type":"json_object"}。PydanticAI 经 extra_body 透传，保留文本输出而不启用 SDK schema 校验或自动纠错，预算层先记录 usage，再由 Engine 校验动作。
- 提示明确单对象、JSON 字符串转义和内部历史格式；格式反馈附具体校验原因与允许动作示例。trace 保存 validation_error，便于区别语法、动作字段与记忆证据问题。
- 本地严格校验继续拒绝多余字段、不可用工具和不合规记忆引用；不自动剪裁 XML、Markdown 或多余括号。连续两次格式失败仍停止，纠错步骤计费规则不变。
- 新计划的控制变量记录 json_action_v2 和输出模式，源代码与提示指纹随之变化。首轮计划和结果不可覆盖。

依据：[DeepSeek JSON Output 官方说明](https://api-docs.deepseek.com/guides/json_mode/)，2026-10-25 核验。JSON 模式约束语法，不保证动作 schema 正确；官方也提示可能返回空内容或受输出长度截断。不能把配置成功写成模型可靠性已验证。

## 验收与边界

执行 sh scripts/check.sh：ruff/format 通过，pyright 0 errors，65 tests passed。日志见 [checks.log](../results/validation/action-protocol-v2/checks.log)。本轮只新增两个针对性回归用例，并补充既有断言：

1. 实际已安装的 PydanticAI/OpenAI SDK 接收虚构密钥，通过 HTTP MockTransport 拦截请求，不访问网络。确认 JSON 参数出现在 HTTP 请求体，文本模式无该参数，每次调用只产生一次请求，原始无效文本不被修补，缓存 usage 正常回传并记录。固定 SDK 将输出上限映射为 max_completion_tokens。
2. 合法 JSON 但非法动作字段不会触发沙箱命令；一次纠错后可提交。首轮出现的 XML/额外括号形式仍被拒绝，原有连续两次错误停止测试通过。

这是本地协议与计费链路验证，不是供应商实测。SDK 用例在未安装可选依赖时会跳过；本机本次 65 项全部通过，无跳过。旧的压缩和路由离线用例仍通过，真实压缩/廉价路由效果仍未测。A0 的上下文上限问题本阶段未改动。

本阶段新增实验费用为 0。最近已记录累计 usage 估算 0.073168868 USD，未知预留 0.10 USD，未核验账单；不重置预算或历史记录。

## 下一阶段

代码验收后按项目约定等待“继续”。真实复测前重新核验当天价格与执行时限、启动已有 ARM64 VM、固定新的源码版本和运行清单。使用同一首题、seed、A0→A4 顺序与每臂 0.20 USD / 50 步 / 600 秒限额，引用首轮计划 93d5e0763da19ee86a9c；这是开发修复后的复测，不与首轮合并成独立样本。

新清单尚未生成、未启动。正式 test 继续暂缓。A0 上下文终止及压缩/路由是否实际触发，均需在后续阶段单独记录，不能因本次协议修复宣称已解决。
