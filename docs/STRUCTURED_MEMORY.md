# 结构化记忆 v2 与 P1 观察记录

## P2 摘要

定义位于 src/ctxroute/context/memory.py，schema 版本为 2。五个顶层字段保持不变，内容改为嵌套记录：

| 字段 | 子字段 |
|---|---|
| files_read | path、start_line/end_line、facts、artifact_ids |
| commands_run | command、exit_code、conclusion、artifact_id |
| hypotheses | statement、status（pending/refuted/supported）、evidence_ids |
| edits | path、added/removed、description、artifact_ids |
| open_questions | 非空字符串列表 |

行范围和增删数量未知时使用 null，不补成零。范围必须完整且有序，禁止未知字段、类型转换、布尔值充当整数；已支持或已否定假设要求证据引用。输出须通过严格结构校验，引用须属于当前任务已保存的工具输出。假设状态和摘要事实仍是模型解释，结构校验不证明语义正确。

同一 JSON Schema 随压缩提示发送，并在宿主端校验。当前并未依赖供应商原生 constrained decoding；无效摘要以 compression_format_failure 终止并保留原上下文，不自动追加压缩费用。保留批量触发、缩小检查、重复压缩保护和原始 artifact 引用保护。

旧字符串列表摘要不能当作 v2 读取；历史结果保持原样，新运行通过源代码指纹、提示哈希、配置及 state.json 的 memory_schema_version 区分。

## P1 历史观察

A1–A5 的原有 bash/recall/submit 动作可附带可选元数据，无新增工具：

```json
{"tool":"bash","command":"cat pyproject.toml","remember":[{"step":3,"quote":"此前工具输出中的原文"}]}
```

必须引用之前已经返回的 bash 观察，quote 同时出现在可见观察和内容哈希一致的原始 artifact 中。未来步骤、虚构文本和仅存在于隐藏的输出片段均被拒绝。验证通过后写入 P1 confirmed_observations，记录步骤、命令和 artifact ID；相同引用去重，整批验证成功后才更新。

它表示“该步骤确实输出过这段文本”，不表示模型推断已经被证明，也不保证编辑之后仍然成立。提示明确标注历史性及工具输出的不可信性质。模型任意语义结论仍留在 P2，不自动提升为 P1 事实。

每臂 context 配置显式记录 fixed_max_facts=16、fixed_quote_chars=500；达到容量则拒绝新增且保留旧记录，允许模型去掉 remember 后继续操作。P1 不参加压缩；最后失败测试输出与 diff 统计继续维护。所有 A1–A5 使用相同记忆元数据能力，A0 保留原生文本接口，差异继续单列。
