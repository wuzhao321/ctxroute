# 决策记录

## 2026-10-25 同题真实复测完成

用户在代码验收后回复“继续”，执行新计划 2ddf897834db7a6a7e18，同一首题、seed、A0→A4，预算和环境不变；原三天结束后只续接此固定任务块，未扩样。235 次请求，本轮 usage 估算 0.240705536 USD，累计 0.313874404 USD，历史未知预留 0.10 USD，账单未核验。A0 context_limit，非空补丁经官方测试未解决；A1–A4 均 step_limit、空补丁。JSON 语法失败 0，A4 记忆引用错误 2；A2/A4 共接受 5 次压缩，廉价动作请求 0。重复命令和保守分类导致的持续 strong_lock 成为下一代码阶段重点。判分目录按 plan_id 隔离，首轮记录不改写；VM 已停止。详见 results/analysis/arm64-json-v2-20261025/RETEST_REVIEW.md。

## 2026-10-25 动作协议可靠性修复

用户要求进入下一阶段并继续未完成任务。补齐显式 JSON 输出模式，A1–A5 动作及压缩请求统一使用 json_object，A0 保持原生文本协议；PydanticAI 透传参数并保留原始文本，本地校验与一次纠错限制不变。新增控制变量 json_action_v2，未来必须生成新清单，保留首轮不利结果。离线请求拦截验证真实 SDK 序列化与 usage 回传，65 项检查通过；本阶段无 API、容器或评测支出。实际模型可靠性及 A0 上下文问题仍待后续验证。详见 ACTION_PROTOCOL.md。

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

## 2026-10-24 简单链路测试

用户要求继续完善项目并简单测试完整任务链路。本轮增加一个离线集成用例，临时工作区使用 synthetic-chain-1 玩具任务、A1/A4 两个配置、固定模型响应和精确命令白名单，实际改写临时 toy.py 并通过 difflib 生成补丁。调用现有清单/续跑、Engine、P1、规则路由、预测交接、报告导入和分析代码；报告使用测试格式样例，未运行官方 harness。生产结构的 manifest 仅在 pytest 临时目录生成，并带 unit_test_fixture_not_benchmark 来源标记；不存入项目 results/runs，也不纳入声明台账的效果结论。

修复批量报告导入的检查顺序：全部已有 grade 的预测和报告哈希通过后，才导入尚未导入的报告；检查失败不再先写入其他任务的 grade。修复成本字段的惰性回退，避免已有 cost_upper_usd 时仍提前访问缺失的 reserved_usd。分析报告同时展示缺失判分、成功/已启动及已判分子集比例，避免隐去未判分分母。

必要检查最终 63 passed（原 62 项加 1 个链路用例），ruff/format 与 pyright 通过。本轮无 API、云资源、Docker、官方判分；真实环境的完整端到端验证仍待执行。

## 2026-10-24 恢复真实评测准备

用户授权开启真实评测并开展所需工作，覆盖此前暂停安装/API/评测的要求，预算与 dev/test 边界不变。检查确认本机没有 Docker/Colima/Lima/Homebrew，未配置 Linux 主机或云账号；已有 key 可用。此前 M5 仅开发的约定仍需明确是否允许改为本机 ARM64 单并发冒烟，已发起执行位置选择，尚未创建资源。

发现原已安装 swebench 5.0.2 与冻结数据格式不兼容：仅查看 Parquet 列名确认缺 image、eval_script、log_parser、eval_type。为保留原始数据而固定官方 swebench 3.0.17；其源码从旧字段构建测试规范，官方 CLI --help 已通过。PyPI 项目元数据指向 SWE-bench 官方仓库，依赖精确版本和包哈希更新至 uv.lock。没有运行 gold patch 验证或判分。

新增 prepare_dev_tasks.py，仅投影四个允许字段，按固定 dev 前缀导出，原始 Parquet SHA-256 与冻结值一致。首个实例 sympy__sympy-22840 的 Agent 输入已保存到忽略目录，下载缓存已删除；未解码参考补丁或隐藏测试列。官方价格 2026-10-24 重新核验，与前一日相同，保存新快照。benchmark runtime 继续 offline_only，待隔离环境与镜像审计通过后才开启。准备状态见 results/validation/live-preflight/。

## 2026-10-24 本机 ARM64 执行获批

用户回复“允许本机 ARM64 小规模评测（不租云主机）”，覆盖之前仅开发的架构约束。选择项目级 Lima 原生 VZ Linux 虚拟机承载 Docker；不挂载宿主目录，不租云。目标资源 4 vCPU、8 GiB RAM、120 GiB 稀疏磁盘，任务仍单并发、2 CPU/4 GiB。安装与环境验收尚在进行，真实推理与判分尚未启动。累计费用不重置，保留未知调用的 0.10 USD 预留。

安装结果：Lima 2.2.1 官方包校验通过，Docker CLI 29.6.1 可运行。Ubuntu 固定日期镜像下载超时，Lima 自动回退的滚动镜像连接中断；按两次失败停止约定未作第三次尝试。`--timeout=10m` 未约束启动前镜像下载，后续必须单独设置下载墙钟上限。VM 只读检查为 Stopped，无 Docker socket、无宿主挂载；不将客户端安装或配置创建称作隔离验收通过。运行模式继续 offline_only。具体记录见 LOCAL_ARM64.md 与 live-preflight 证据。

用户随后授权“重试，如果不行，返回上一级尝试其他办法”。使用 curl 独立 180 秒时限重试成功，945530880 字节镜像 SHA256 与 Lima 固定官方值一致。已检测 Alpine 与 Debian 官方备用地址可达，无需实际切换。切换本地镜像的首个检查脚本误将 mounts:null 判为失败，短暂启动了重复下载；已终止该进程并按无挂载语义修正，再用本地镜像启动成功。原始失败日志保留。

本机引擎已就绪：VM Running、VZ/aarch64、4 CPU、8 GiB、mounts=[]；Docker Server 29.9.2、Ubuntu 26.04.1 LTS。客户端为 29.6.1。新增官方 harness 首题镜像准备脚本，只有安装元数据列可进入 Python，日志和构建上下文位于 results/private/harness。新增真实容器审计脚本覆盖资源限制、网络/文件边界、超时与输出上限、MCP 开销；审计通过之前继续禁用付费推理。

官方首题镜像构建及 Git 历史清理已完成。真实 Docker 审计通过；首轮 MCP 被 offline_only 门阻止，保留失败记录，改用仅含沙箱配置的临时根目录后 MCP 检查通过，无 API 调用。每种 exec 路线仅 5 个样本，直接调用中位数约 70.1 ms、MCP 约 72.0 ms，只是本机热环境小样本，不作总体性能结论。开始冻结首题五臂计划，runtime 切换 live；模型费合计上限 1 USD，既有累计费用与未知预留不重置。正式 test 仍未授权。

首轮真实 pilot 清单 93d5e0763da19ee86a9c 已完成：A0 因上下文上限结束，A1–A4 因连续两次动作格式无效结束，五份预测均为空补丁。共 76 次模型请求，全部 strong、off_peak，未触发压缩。本轮 usage 费用 0.073165268 USD；累计 0.073168868 USD，未知预留 0.10 USD 保留，未核验供应商账单。官方 make_run_report 将五臂各列为 1 个空补丁、0 成功、0 基础设施错误，不把未执行的测试写成通过/失败。独立人工 marker 负对照调用原版 run_instance，实际执行官方测试，未解决、无基础设施错误；此结果不混入模型统计。所有容器已清理，项目 VM 停止但保留磁盘缓存。

切换 live 后发现两个原有单元测试依赖生产配置，现改为临时 offline 配置及配置化 Docker 路径断言；最终必要检查仍为 63 项通过。错误测试仅构造 Runner，未调用 Engine.run 或 API，其不完整 synthetic manifest 移至 validation/test-sideeffects 并附说明。首轮格式失败不在当前批次中修补或重跑；下一阶段应改进协议约束，并另建新版本清单，保留首次不利结果。
