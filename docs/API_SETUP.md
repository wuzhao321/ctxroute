# 模型 API 配置

ctxroute 使用 DeepSeek 模型接口，支持直接 HTTP 和 PydanticAI 两种传输。模型层级、价格快照、输出限制及预算分别由 [`configs/`](../configs/) 管理。

## 设置密钥

在项目根目录、已安装依赖的环境中运行：

```sh
.venv/bin/python scripts/setup_key.py
.venv/bin/ctxroute key-status
```

设置脚本隐藏输入，创建权限为 `600` 的 `.env`，拒绝覆盖已有文件。`key-status` 只报告是否已配置。读取顺序为 `DEEPSEEK_API_KEY` 环境变量、项目 `.env`；文件值按字面量读取，不执行 shell 展开。

密钥不应写入命令参数、版本控制或任务容器。设置密钥不会触发 API 请求。

## 连通性探针

```sh
# 查看计划；不读取密钥或发送请求
.venv/bin/ctxroute api-probe

# 显式发送固定短请求
.venv/bin/ctxroute api-probe --execute

# 使用 PydanticAI 传输发送同一探针
.venv/bin/ctxroute api-probe --transport pydantic_ai --execute
```

探针使用固定提示 `Reply exactly OK.`、`temperature=0`、关闭 thinking、非流式输出，最多生成 32 token；每次命令只发送一次请求，不自动重试。配置位于 [`configs/api_probe.json`](../configs/api_probe.json)。探针检查连接、响应与 usage 字段，不产生基准任务结果。

执行前需启用 `configs/budget.json` 的 `paid_execution_enabled`，并核验探针所引用价格快照的日期。请求前检查本地提交与预算，先预留配置额度，再派发请求。预留额度与最终 usage 成本分别记录。

## 记录与计费

- 请求目标固定为 `https://api.deepseek.com/chat/completions`，开启证书校验，禁用重定向和环境代理。
- `results/runs/api-probe-*/` 保存 manifest 与 trace，包括 usage、缓存命中、耗时、配置和价格哈希；不记录认证头、密钥或响应正文。
- 所有运行共享 `results/budget/campaign.sqlite`。超时或缺失 usage 的请求保留未结算预留，不能按零费用处理，也不能通过更换账本重置累计费用。
- 输入护栏与最大输出限制用于请求前的保守费用预留；输入估算不是供应商 tokenizer 上界。
- API usage 估算、价格上下界和供应商账单是不同依据。只有明确满足已配置费率区间时才按该区间结算，歧义保留待核对状态。

通用模型执行还要求 runtime 为 `live`，并受冻结运行计划、任务限额和沙箱检查约束，详见 [REPRODUCE](REPRODUCE.md)。供应商接口参考：[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[价格说明](https://api-docs.deepseek.com/quick_start/pricing/)。
