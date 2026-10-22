# A0 原生基线执行接入

本次交付补齐 A0 运行适配代码，没有安装 mini-swe-agent、启动容器、访问密钥或请求模型。真实兼容性与 A0/A1 一致性仍未验证。

## 入口和执行路径

`ctxroute experiment plan` 默认包含 A0、A1、A2、A3、A4。也可显式指定 `--arms A0`。清单生成是离线配置操作；`experiment run` 仍要求既有显式执行参数与运行配置打开，当前二者并未打开。

运行入口根据实验臂选择 `MiniSweRunner` 或 `Engine`，二者实现相同 Runner 接口。A0 的路径为：

1. 核对可选依赖的版本与关键源文件，读取上游已安装包中的默认文本配置。
2. 创建受控沙箱，用容器内 `uname` 获取供原生模板渲染的系统信息；记录该初始化调用，不算模型动作。
3. 实例化上游 `DefaultAgent`，不修改其循环、线性消息历史或格式错误处理逻辑。
4. `NativeModel` 负责把请求交给 ctxroute 的预算网关，随后调用上游 `parse_regex_actions`。解析失败仍交回原生循环处理，费用保留。
5. `NativeEnvironment` 将命令交给现有直接/MCP 沙箱，复用上游 `_check_finished` 提交检测。原生观测格式化函数继续负责输出呈现，不开放 recall。
6. 保存原生 `trajectory.json`、统一 trace、状态和补丁；无论退出原因如何，都尝试导出补丁并清理所属容器。

所有费用含重试计入同一 ledger。ctxroute 在请求前预留，补足上游循环自身“调用后检查成本”的局限。当前任务限额、最多两次请求尝试和两次格式错误等控制条件与主线一致。

## 固定的上游参考

版本 `2.4.6`，参考提交 `04d809ceab9df28f9adaed044884180159172930`，配置见 `configs/baselines/mini-swe-agent.json`。

- [DefaultAgent](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py)
- [文本动作解析与观测格式化](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/models/utils/actions_text.py)
- [默认文本配置](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/config/default.yaml)
- [Docker 提交检测](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/docker.py)

运行前对关键文件重新计算 Git blob SHA-1 并匹配清单。不匹配即停止，没有自动下载、降级或放宽版本的逻辑。此检查确认列出的源文件内容，不宣称证明整个安装包的所有文件都来自该提交；其他依赖版本仍须在后续环境阶段锁定。

上游导入时会读取全局配置目录。适配器将该目录限定到本项目 `.cache/minisweagent`，拒绝其中已有 `.env` 或已导入包指向其他目录，避免意外加载上游全局配置。实际 DeepSeek key 仍仅交给预算网关。

## 对“原版”的精确定义

保留原生循环、默认文本提示、文本动作解析器、观测模板和提交判断；模型传输与环境实现替换为预算/隔离适配器。它是**原生文本基线加受控 I/O**，不是完全未经配置改动的上游命令行。

以下差异必须在实验报告保留：

- A0 使用文本 bash 代码块和线性聊天消息；A1 使用 JSON 动作及分区上下文，提示不相同。不能用 A0/A1 差异直接归因压缩或路由，H2/H3 的主对照仍为 A1。
- A0 直接使用 HTTP 网关，避免破坏原生多条消息历史；A1–A5 默认使用 PydanticAI。传输选择记录在 manifest。
- 上游默认观测模板最多展示约 10,000 字符；A1 默认头尾各 2,000 字符。保留这一差异以维持原生 A0；不宣称两臂输出面严格一致。
- 上游默认限额被替换为本项目实验限额，命令超时/输出超限会中止任务。记录预算终止比例。
- 原生默认配置的 pager 环境变量只在容器命令中设置，不转发宿主环境；禁网和资源约束来自 ctxroute 的沙箱。
- 默认文本配置是明确选定的历史文本路线，不宣称等同于上游全部默认入口或其最新 tool-call 路线。

后续若增加统一提示/动作协议的 A0/A1 一致性实验，应创建独立变体并预先冻结；不得覆盖原生基线结果。

## 复现与产物

manifest 增加 native_profile、上游版本/参考提交、关键文件 blob 清单、baseline_policy_sha256、source_tree_sha256、model_transport 和 controls。运行计划也冻结源代码指纹；修改实现后必须生成新计划，避免代码变了但 run_id 被当作同一实验续跑。

A0 原生轨迹与统一记录都在 `results/runs/{run_id}/`。已有 `import-baseline` 入口继续用于导入在独立环境生成的规范化外部证据。未验证的 SDK 绑定和未运行的评测不能转化为效果结论。
