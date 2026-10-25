# A0：mini-swe-agent 原生基线适配

A0 将固定版本的 mini-swe-agent 文本循环接入 ctxroute 的模型网关、沙箱与记录接口，用作外部基线。实现位于 [`baselines/mini.py`](../src/ctxroute/baselines/mini.py)，上游加载与内容校验位于 [`baselines/upstream.py`](../src/ctxroute/baselines/upstream.py)。

## 执行路径

1. 校验已安装的 mini-swe-agent 版本及关键源文件，读取其默认文本配置。
2. 创建任务沙箱，获取模板所需的系统信息。
3. 实例化上游 `DefaultAgent`，保留任务循环、线性消息历史、格式错误处理和提交判断。
4. `NativeModel` 将请求交给 ctxroute 的预算网关，再调用上游 `parse_regex_actions` 解析文本动作。
5. `NativeEnvironment` 将命令交给直接调用或 MCP 沙箱，使用上游观测模板返回输出。
6. 保存原生 `trajectory.json`、统一 trace、状态和预测，导出补丁并清理所属容器。

请求、重试及失败解析的费用都进入同一账本。预算预留发生在请求派发之前，任务步数、墙钟和工具限制由实验配置控制。A0 不提供 ctxroute 的结构化记忆、`recall` 或重复动作拦截。

## 上游固定策略

[`configs/baselines/mini-swe-agent.json`](../configs/baselines/mini-swe-agent.json) 固定 mini-swe-agent `2.4.6`、参考提交 `04d809ceab9df28f9adaed044884180159172930`、`native-text` 配置和动作正则，以及关键源文件的 Git blob SHA-1。

适配器在导入上游包前重新校验版本与列出的文件，不匹配即停止，不自动下载或降级。校验范围为清单中的文件，完整依赖版本由 [`uv.lock`](../uv.lock) 固定。

上游配置目录限定为项目内 `.cache/minisweagent`，该目录不得存在 `.env`。如果上游包已从其他全局配置目录加载，适配器拒绝继续。模型密钥只由宿主侧预算网关读取。

## 对照边界

A0 保留原生循环和文本接口，模型传输与执行环境替换为 ctxroute 适配器；它不是未经配置修改的上游 CLI。

| 项目 | A0 | A1 |
|---|---|---|
| 动作协议 | 原生文本命令块 | 严格 JSON 对象 |
| 历史组织 | 线性消息 | P0–P3 分区上下文 |
| 模型传输 | 直接 HTTP | 由 runtime 配置选择 |
| 输出呈现 | 上游观测模板 | 头尾截断与 artifact 引用 |
| 固定观察记忆 | 无 | `remember` 元数据 |
| 重复动作控制 | 上游行为 | ctxroute 进展控制 |

这些接口差异随运行配置记录。A0 与 A1 的差异用于描述整体系统表现；压缩与路由的主要配对分别为 A2–A1、A3–A1，联合配置为 A4–A1。统一接口的基线实验应定义独立变体，不覆盖原生基线。

## 产物与复现

运行计划冻结源码、实验配置和上游策略哈希。manifest 记录上游版本、参考提交、关键文件清单、模型传输和控制参数；A0 轨迹与统一记录写入 `results/runs/{run_id}/`。实现或策略发生变化时，应生成新计划。

也可通过 `ctxroute experiment import-baseline` 导入独立运行产生的规范化外部证据。通用流程见 [REPRODUCE](REPRODUCE.md)，实验比较方法见 [PREREG](PREREG.md)。
