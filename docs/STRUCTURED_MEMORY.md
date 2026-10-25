# 结构化记忆

[`context/manager.py`](../src/ctxroute/context/manager.py) 将上下文组织为四个分区：

| 分区 | 内容 |
|---|---|
| P0 | 系统提示、任务说明和编排器运行状态 |
| P1 | 固定的历史观察、最后失败测试输出和补丁统计 |
| P2 | 结构化工作记忆 |
| P3 | 最近步骤与工具观察 |

## P2 摘要 schema

[`context/memory.py`](../src/ctxroute/context/memory.py) 定义 schema 版本 2：

| 字段 | 记录内容 |
|---|---|
| `files_read` | `path`、`start_line`、`end_line`、`facts`、`artifact_ids` |
| `commands_run` | `command`、`exit_code`、`conclusion`、`artifact_id` |
| `hypotheses` | `statement`、`status`、`evidence_ids` |
| `edits` | `path`、`added`、`removed`、`description`、`artifact_ids` |
| `open_questions` | 非空字符串列表 |

未知行范围、增删数量和退出码使用 `null`；行范围必须完整且有序。校验拒绝未知字段、类型转换和布尔值充当整数。假设状态为 `pending`、`refuted` 或 `supported`，后两者必须有证据引用。

压缩提示包含同一 JSON Schema，宿主端进行严格校验；所有引用必须属于该任务保存并固定的 artifact。JSON 输出模式不保证 schema 或语义正确，模型写入的事实与假设仍是解释性摘要。

## 压缩流程

实验臂的 `context` 配置控制上下文预算、软硬阈值和保留步骤数。`summary` 模式将旧摘要与较早步骤交给廉价模型，保留最近 K 步；`truncate` 模式只移除较早步骤，用作消融；`none` 模式不压缩。

候选摘要只有在结构、引用合法且上下文缩小时才接受。无效摘要触发 `compression_format_failure`，不替换原上下文；相同批次的重复压缩受保护。接近硬阈值仍无法缩小时触发上下文限制终止。artifact 全文独立存储，不因摘要替换而改写。

## P1 历史观察

A1–A5 的动作可附带 `remember` 元数据：

```json
{"tool":"bash","command":"cat pyproject.toml","remember":[{"step":3,"quote":"此前工具输出中的原文"}]}
```

引用必须来自之前已返回的 `bash` 观察，原文同时出现在可见观察和内容哈希一致的原始 artifact 中。未来步骤、虚构文本和仅在截断隐藏片段中的文本不允许固定。整批验证通过后写入 `confirmed_observations`，保存步骤、命令和 artifact ID，并去重。

固定记录表示某一步确实输出过该文本，不表示编辑后仍然成立，也不将模型推断提升为事实。P1 不参加压缩，工具文本始终视为不可信数据。

容量由 `fixed_max_facts`、`fixed_quote_chars` 控制；达到容量时拒绝新增并保留旧记录。动作可以去除无效 `remember` 后纠正重试。A0 保留原生文本接口，不具备该元数据能力。

## Artifact 与 recall

[`context/artifacts.py`](../src/ctxroute/context/artifacts.py) 按内容哈希保存完整工具输出。模型观察采用头尾截断，并附 artifact ID；启用压缩或截断模式的实验臂可通过 `recall` 按字符范围读取片段。大小、读取范围和总容量均受配置限制。

schema 版本、提示和源码指纹写入运行记录。不同版本的摘要不可直接混用，历史产物应由对应版本实现读取。
