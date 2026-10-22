# 本机设置密钥与 API 冒烟

## 用户自行设置

在自己的终端执行：

```sh
cd /Users/wuzhaowei/Documents/ChatGPT/ctxroute
.venv/bin/python scripts/setup_key.py
```

随后粘贴 DeepSeek key 并回车。输入隐藏；若终端无法保证隐藏输入则直接退出。脚本只创建权限 600 的 .env，拒绝覆盖已有文件，不调用 API。不要把 key 发送到聊天或写进命令参数。

设置后可运行 `.venv/bin/ctxroute key-status`，只显示是否配置，不显示值。读取优先级为 DEEPSEEK_API_KEY 环境变量、然后 .env；setup_key.py 生成的是未经 shell 展开的单行配置。如果已有同名环境变量，它优先于文件。

## 只看调用计划

`.venv/bin/ctxroute api-probe` 为离线 dry-run，不读密钥、不创建账本、不发送请求。

真实调用命令为 `.venv/bin/ctxroute api-probe --execute`，但在 configs/budget.json 的 paid_execution_enabled=false 时会拒绝。首个真实请求前由项目执行者检查、启用并提交配置；用户设置密钥本身不触发付费。

## 调用与记录规则

- 固定短提示 Reply exactly OK.，cheap 模型来自版本化价格配置；thinking disabled、temperature 0、max_tokens 32，非流式，只发一次，不自动重试。字段依据 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。
- 前置检查当天价格核验、密钥存在与本地 commit；打印预计预留/已核算/未结算预留/硬上限，事务预留 0.10 USD 后才派发。
- 0.10 USD 是从总预算扣留的余量，不是实付估计。固定输入另设 4096 token 异常护栏，它是工程保护值，不是供应商承诺的 tokenizer 上界；超过护栏会保留费用证据并锁停。
- 网络目标固定为 https://api.deepseek.com/chat/completions，不转发重定向，不使用环境代理，证书校验开启。不记录认证头、密钥、响应正文或异常原文。
- results/runs/api-probe-*/ 内保存 manifest 与 traces.jsonl，记录原始数值 usage 字段、缓存命中、耗时、配置与价格哈希、commit。响应正文仅检查是否恰为 OK。
- Ledger 在 results/budget/campaign.sqlite，跨运行共享；不能通过删除/换账本重置预算。未知 usage、连接超时仍保留 0.10 预留，不能假定没有发生计费。
- 价格取自 [DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing/)。请求完整落在明确非高峰区间时按 usage 计算成本并结算；若涉及高峰/中国节假日歧义，报告上下界并保留预留等待对账。API usage 计算值与供应商账单验证分开标注。

当前只有连通探针；尚未实现通用 Agent 模型适配器或工具调用循环。它不算 SWE-bench 实验结果。
