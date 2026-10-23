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

## 当前用户范围覆盖：优先实现，暂停实验和搭建

用户明确要求先完成架构与实验代码，随后要求减少测试和验证、重点实现功能。本轮不再依赖云服务器、密钥或 Docker 问题，不进行真实评测、API 冒烟或 SDK 环境安装。此前“每阶段等待继续”不阻止完成本轮代码工作。

已编写 A1–A5、结构化压缩、规则路由、MCP/Docker 边界、预算网关、运行清单/任务级续跑、消融配置、预测/判分导入和配对分析。核心使用 dataclass；PydanticAI 和 MCP 采用延迟加载适配器。JSONL/CSV 为默认产物，暂不引入 DuckDB/Parquet 的运行时依赖。

A0 仅实现原版证据导入与配置，原版付费执行接入及 A0/A1 一致性仍待完成。A6/A7、学习路由、本地模型保持后置。所有真实效果指标未测。详细边界以 ARCHITECTURE.md 与 IMPLEMENTATION_STATUS.md 为准；早先 P0 文档描述的是历史状态。

## A0 执行适配增量

用户继续授权代码实现，评测与环境搭建仍暂停。新增 MiniSweRunner，调用固定参考提交的原生 DefaultAgent、文本动作解析、观测模板及提交检测；模型与环境 I/O 接入既有预算和沙箱。保留 native-text profile，记录它与 A1 的提示、动作格式、截断和传输差异，不宣称 A0/A1 已等价。主优化比较仍使用 A1。

参考版本 2.4.6 / commit 04d809ceab9df28f9adaed044884180159172930，关键源文件 blob SHA 固定在 configs/baselines/mini-swe-agent.json。没有安装该包、下载镜像或运行原生循环。计划开始冻结实现源代码指纹，代码改变后需生成新清单。详见 A0_ADAPTER.md。

## 2026-10-23 恢复运行阶段

用户明确授权继续安装依赖、调用 API 或运行评测，覆盖此前暂停要求。预算仍为累计 10 USD、8 USD 暂停，三天范围不自动重置。先安装项目级依赖、核验 SDK 接口和完成固定 API 探针，再按冻结 dev 顺序推进。无需为已授权的每一步重复确认；密钥与外部评测环境等缺失信息仍需用户提供。

初始只读检测：DeepSeek key 缺失或格式无效；Docker/Colima/Lima/Homebrew 未发现；运行 SDK 与 swebench 未安装；无本项目预算 ledger。正式评测环境尚未确定，不创建未知计费资源。

用户已在本机设置密钥，并明确没有 Linux 主机/云账号，先完成本地依赖与 API 联调。允许开启 API 预算开关；benchmark runtime 继续 offline_only，不租主机、不安装 Docker。先执行廉价层固定 32 输出 token 探针，单请求预留最多 0.10 USD，无自动重试。

本地联调结果：HTTP 成功；第一次 PydanticAI 尝试失败，检查已安装 SDK 确认 usage 为属性，修复旧方法调用后第二次成功。最初失败日志只保存通用错误，不能用修复推断其精确账单。原始记录全部保留；成功调用按 usage 合计 0.0000036 USD，未知调用保留 0.10 USD，供应商账单未核验。没有继续调用模型或执行评测。详见 LOCAL_INTEGRATION.md。

## 2026-10-23 代码补齐：记忆与复现

用户要求重心继续放在代码实现。本轮不调用 API、不安装依赖、不搭建环境或运行评测。摘要改为 v2 嵌套 schema；P1 仅接受有原始工具输出支持的精确引用，并明确历史性，模型推断留在 P2。A1–A5 统一增加可选 remember 动作元数据，bash 工具面不变；A0 原生协议不变。新的上下文开销属于新的实现版本，不沿用旧计划。

复现新增分阶段工作流。默认准备只写清单；推理沿用预算与隔离门；预测交接为每臂生成官方 harness argv，判分由独立环境执行；报告导入与分析在后处理进程进行。正式 test 与多 seed 调度仍不开放，未因编排入口新增而扩大实验范围。JSONL/CSV 保持默认，不引入新的分析依赖。
