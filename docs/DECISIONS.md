# 决策记录

## 2026-10-22 用户约束

- API 与云资源合计上限 10 USD，3 天；8 USD 暂停。API 和云主机实际调用至今均为零，本轮未产生这些费用；不把 Codex 订阅费用纳入该实验预算。
- M5 arm64、16 GB 仅用于开发。遵循原提示要求，在已批准的 x86_64 云端进行正式 harness 验证。本机磁盘 884 GiB 可用，但这不改变架构选择。
- 不训练大模型。学习型路由、本地模型、多 Agent/A2A、A7 外部对照暂缓；优先规则路由及结构化上下文压缩。
- 项目级四个 skills 位于 .agents/skills；用户无需额外安装第三方 skill 市场。
- GitHub 已连通，账号 wuzhao321；仓库可见性与首次远端发布待定。用户希望 Git 问题出现时再处理。

## 官方核验

来源：[DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)，2026-10-22 读取。

- 廉价层官方名称为 deepseek-flash，对应 DeepSeek-V4.1-Flash-1017；旧 deepseek-v4-flash 请求已转到这一版本，不能声称在比较旧 V4-Flash。
- 强层 deepseek-v4-pro，对应 DeepSeek-V4-Pro-0813。
- 官方存在高峰/非高峰价，见 data/prices/2026-10-22.json；预算按高峰保守计算，实际按请求时段和账单核对。旧文档价格作废。
- 默认 thinking 开启；实际思考模式、工具调用和 schema 行为仍需在选定 SDK 后核验。模型别名可能变化，正式运行前再次核验。

来源：[官方 SWE-bench Evaluation Guide](https://www.swebench.com/SWE-bench/guides/evaluation/)，2026-10-22 读取。

- 官方入口为 swebench.harness.run_evaluation，可按 instance_ids 选择任务。
- 官方文档列有云端执行方式；本轮未选择服务商、报价或创建资源。
- harness 可缓存 run_id/instance_id 结果；改变补丁重跑时必须防止错误复用，保留首跑记录。

## 原始范围的缩减方案（用户已确认）

用户在 2026-10-22 回复“确认”。本轮交付原型、无付费单元测试，以及额度允许时的固定 dev pilot。正式 test 保持封存、标注未测；不再将原方案多臂、多 seed 全量 test 作为三天完成标准。

- 第 1 天：可用工具链、协议、预算守卫、trace 与 mock 测试。
- 第 2 天：A1 循环、沙箱、规则路由和压缩最小实现；在获得云环境后先做 1 个固定 dev 冒烟。
- 第 3 天：在事先确定的少量 dev 实例上比较 A1/A2/A3/A4；A0 保留为一致性对照，按实测费用决定可执行规模，保存未完成原因并出报告。
- 首个 API 连通探针建议最多 0.10 USD，初始 dev 冒烟建议模型费最多 0.50 USD；这些只是计划分配，不是价格估计或执行授权。云费用从同一个 10 USD 总额扣除，不能另算。
- 样本数不在此时臆定。先锁定候选 dev 顺序，再依据预先定义的成本规则决定完整配对数；不得看成功率决定挑选或扩样。
- 若云环境费用或构建时间不能满足上限，本轮交付实现和测试，harness 指标全部未测；不以 mock 成功冒充 benchmark 成功。

完整实验仍保留原 A0–A7 定义。A4 是联合主线；“test 只正式跑一次”指预注册的完整运行批次，可以含事先固定的多个 seed。多 Agent/A2A 仅主结果完成后再考虑。

## 本地工程与数据冻结

- 使用 Codex bundled Python 3.12.14、Git 2.53.0 和 Node；uv 0.13.5 安装到本项目 .tools，依赖仅在 .venv。不改 shell 配置，不安装 Docker/VS Code/gh。
- 当前运行时仅标准库；dev/data 依赖通过 uv.lock 锁定。PydanticAI、MCP、mini-swe-agent 和 swebench 在相应实现阶段核验和安装，避免提前引入未使用依赖。
- 官方 uv 安装方式参考 https://docs.astral.sh/uv/getting-started/installation/ 。首次下载因沙箱 DNS 失败，获网络执行批准后成功；没有付费调用。
- SWE-bench Lite upstream test 300 个 ID 按 SHA256(seed:instance_id) 排序，seed=20261022，前 60 为本项目 dev，余 240 封存；源 revision 与哈希在 data/split.json。Parquet 只投影 instance_id 列，提取后删除缓存。官方页面展示的 upstream dev 样例不参与本项目选样。
- 预算模块使用 SQLite 事务，先预留后结算，崩溃/未知 usage 保留预留，重复 request_id 不可重复派发；费用超过预留时记录实际并锁停。它依赖调用方提供正确上界，不能撤销已发生费用。目前没有付费适配器，不能宣称完整 API 消费拦截已接通。
- 本地 P0 质量门已通过；真实 Docker、API 和 harness 验收仍未完成，见 docs/P0_STATUS.md。

## P0 API 准备增量

用户回复“继续”后已实现固定连通探针，使用标准库 HTTPS 直接调用官方 Chat Completions 入口，避免为一次验证提前引入完整 Agent SDK；正式 Agent 适配仍按实现阶段核验。密钥只在用户终端隐藏输入，环境变量或 .env 读取。探针无自动重试，预留 0.10 USD；工程 input token 护栏不是供应商保证的计费上界，异常将保留证据并锁停。

官方 schema 要求 prompt_cache_hit_tokens / prompt_cache_miss_tokens，二者之和须匹配 prompt_tokens。没有 usage 时保留预留，不补零。无法确定峰谷时段时报告价格区间，不把上界写成实际账单。相关来源见 docs/API_SETUP.md。

低成本云环境还未确定；已记录 Modal 官方 harness 候选与 Sandbox 实际费率区别，以及传统主机关机仍计费的约束，见 docs/CLOUD_PLAN.md。没有建立资源、安装系统软件或访问密钥内容。
