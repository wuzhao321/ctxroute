# 预算范围与费用预留

[`configs/budget.json`](../configs/budget.json) 支持单账本与显式阶段两种模式。未设置 `active_stage_config` 时使用单账本；设置后，由对应阶段文件提供限额、分配、换算和预留参数。顶层开关与阶段 `paid_execution_enabled` 必须同时开启，配置额度本身不代表可执行请求。阶段限额不能用于重开、清零或迁移已有账本。

## 配置入口

| 配置 | 作用 |
|---|---|
| `active_stage_config` | 指向阶段 JSON 配置的项目相对路径 |
| `stage_id`、`ledger` | 阶段身份与持久化账本位置 |
| `hard_cny`、`pause_cny`、`request_cny` | 阶段硬上限、暂停阈值及单请求上限 |
| `baseline_cny`、`checkpoint_cny`、`paired_cny`、`validation_cny` | 各分配和配对合计上限 |
| `usd_to_cny`、`reservation` | 固定换算率与保守预留公式参数 |

配置格式由 [`StageBudget.load`](../src/ctxroute/tracing/stage_budget.py) 校验。`configs/budgets/` 保存阶段参数，`configs/candidates/` 和 `configs/variants/` 提供候选运行及策略配置；它们不包含实际消费记录或模型效果结论。

## 费用归属

每阶段只有一个账本，所有请求必须绑定不可变的运行 scope 和预算分配：`baseline`、`checkpoint` 或 `validation`。前两项共享配对额度，专项验证不能借用配对额度。多个进程或运行 ID 共用这些分配，不会因新建运行而增加额度。通用运行入口要求计划中的 `budget_lane`，禁止由 `A1` 名称猜测角色。

请求登记、阶段累计、分配累计、配对累计和单任务 scope 检查在同一个 SQLite 事务中完成。计入已结算费用及全部未结算预留，超过单请求、分配、配对或阶段硬上限时在网络请求前拦截。预计占用达到 `pause_cny` 即暂停，不能因为硬上限尚有余额而继续。

供应商费用保留原始 USD 精度，CNY 使用配置冻结的 `usd_to_cny` 换算。USD 限额向下取整到小数点后 12 位，避免循环小数扩大额度；预算表中的 CNY 数值是声明上限。

未知、缺失或无效 usage 不结算为零；失败请求保留预留。只有有效 usage 按原价格时段规则结算后，实际费用与预留的差额才自然释放。供应商实际费用或 usage 超出预留护栏时保留真实记录并锁停；本地预算不能替代供应商侧封顶，不能为满足限额而隐瞒已发生的超额。

## 模型请求上界

阶段模型网关沿用保守公式，参数从阶段配置读取：

```text
B = len(json.dumps(messages, ensure_ascii=True).encode("ascii"))
I = max(input_guard_tokens, input_floor, input_byte_multiplier * B + input_margin)
O = max(max_tokens, output_floor)
预留 USD = (I × 高峰未缓存输入价 + O × 高峰输出价) / 1,000,000
```

这里的字节数只是预留公式的输入，不是实测 token 数。Thinking 包含在输出预算中，不另加一次输出费用。请求超过预留上限时停止，不截短上下文、降低已冻结输出上限或重试。固定连通探针仍使用其独立的已验证输入/输出护栏与固定预留，阶段下记入 `validation`，不是 Thinking 动作实验。

## 审计与冻结

`ctxroute status` 只读账本和配置，不读取密钥、不联网；同时显示当前阶段和跨阶段累计已结算/预留。旧阶段费用不占新阶段额度，但跨阶段风险仍须在运行报告中展示。

`StageLedger` 拒绝接管已有旧账本或改变已建立的阶段限额、汇率与预留公式；原暂停/硬停止语义在各自阶段内执行。历史冻结脚本与账本不改写，新计划应冻结活动阶段配置并通过 `load_budget` / `open_budget_ledger` 接入，禁止复制旧脚本中的累计阈值作为新阶段守卫。

`configs/candidates/` 仅存候选参数，默认运行入口不加载它们。改变输出或墙钟上限必须新建冻结计划；离线兼容和费用检查不能证明真实模型性能提升。

通用 `experiment plan` 使用重复的 `--budget-lane ARM=LANE` 显式分配预算，使用 `--arm-config ARM=PATH` 选择候选配置。活动阶段缺少分配、单任务额度低于请求预留下界、整组额度超过阶段或分配余额时，计划生成即失败。检查包含未结算预留，且只读历史账本；执行入口仍逐请求原子预留。预留下界通过不表示实际上下文必然可负担，完整请求仍按上述字节公式检查。旧的通用任务额度可能不适合活动阶段，应选择等额的新配对配置，不能事后改写冻结计划。准备与执行边界见 [REPRODUCE](REPRODUCE.md)。

`scripts/replay_stage_budget.py` 使用旧请求及结算结果在派生目录重放，检查前后旧账本哈希，不发请求、不编辑原账本。回放不能预测新输出上限下的实际 usage 或修复成功率。

## 采集与回放账本接口

自定义流程可通过 [`open_two_phase_ledger`](../src/ctxroute/tracing/two_phase_budget.py) 为 `collector` 和 `replay` 分别配置额度、父阶段分配和运行 scope。常规 `experiment plan` 仍使用单账本或阶段模式。

两阶段账本校验父账本的固定哈希，并以只读方式查询。默认将两阶段占用计入父阶段限制。显式配置 `independent_pool` 时，单独核算两阶段占用，仍校验父账本身份及单请求预留上限。阶段各自受额度约束，失败或未结算请求会阻止继续派发。
