# 动作与输出协议

A1–A5 使用单个 JSON 对象表达动作。动作定义与严格解析位于 [`domain.py`](../src/ctxroute/domain.py)，执行与纠错位于 [`orchestrator/engine.py`](../src/ctxroute/orchestrator/engine.py)。A0 使用上游文本协议，见 [A0_ADAPTER](A0_ADAPTER.md)。

## 动作

```json
{"tool":"bash","command":"cat pyproject.toml"}
```

```json
{"tool":"recall","id":"ARTIFACT_ID","start":0,"end":100}
```

```json
{"tool":"submit"}
```

`bash` 要求非空字符串命令且不含空字符。`recall` 仅在配置启用摘要或截断时可用，使用字符偏移，要求 `0 <= start < end`。`submit` 结束动作循环，由编排器导出当前补丁，不表示补丁已经正确。

每个动作可以附加 `remember` 数组，引用较早步骤的可见工具输出。引用格式与校验见 [STRUCTURED_MEMORY](STRUCTURED_MEMORY.md)。除此以外，缺失字段、额外字段、不可用工具及错误类型均拒绝接受，不做隐式类型转换。

## 模型输出模式

`ModelRequest.response_format` 区分 `text` 与 `json_object`。ctxroute 的动作和压缩请求选择 JSON 模式，直接 HTTP 请求发送：

```json
{"response_format":{"type":"json_object"}}
```

PydanticAI 通过 `extra_body` 传递同一参数，保留原始文本输出，由 ctxroute 执行 schema 校验。SDK 自动重试和自动格式纠错关闭，每次适配器调用只允许一个请求，所有额外请求由预算层显式管理。

JSON 模式约束输出语法，不保证动作 schema、引用或语义正确。宿主端仍拒绝 Markdown、XML 包装、解释文本和多余括号，不自动修补响应。相关供应商接口见 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。

## 纠错与记录

预算层先记录 usage 与请求费用，再进行动作校验。无效动作不会调用沙箱；编排器把具体错误与合法示例加入下一步上下文，并在 trace 保存 `validation_error`。格式纠错消耗步骤、时间与模型费用。

连续失败次数达到实验臂 `limits.format_attempts` 时，以 `format_failure` 结束；合法动作会清零连续格式失败计数。压缩摘要使用独立 schema，无效摘要以 `compression_format_failure` 结束，不自动追加压缩请求。

输出模式、协议版本、提示与源码指纹进入运行记录。变更协议或停止规则后必须重新生成计划，避免将不同执行行为当作同一实验续跑。
