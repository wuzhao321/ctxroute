# Claims Ledger

2026-10-25 同题真实复测已完成：[复测结论](../results/analysis/arm64-json-v2-20261025/RETEST_REVIEW.md)。A1–A4 的 200 个动作响应均可解析为 JSON 对象，A4 有两次记忆引用校验错误；实际接受 5 次压缩，廉价动作请求为 0。A0 非空补丁经官方测试未解决，其余四臂为空补丁。只支持协议及压缩链路已运行的工程结论，仍不支持成功率、节费或非劣结论。之前“真实复测未运行”段落为代码验收时的历史状态。

2026-10-25 动作协议 v2 已实现并通过离线 SDK 请求拦截及本地质量门（65 项）。真实复测未运行，不能据此宣称格式失败已经消除。实现与边界见 [ACTION_PROTOCOL.md](ACTION_PROTOCOL.md)，证据见 [checks.log](../results/validation/action-protocol-v2/checks.log)。下述首轮实测结论保持不变。

| 拟写结论 | 状态 | 缺少证据 |
| --- | --- | --- |
| 首个 dev 基线运行 | 已测但无修复：A0–A4 均空补丁；正式 test 未测 | 见下方实测证据，不支持总体成功率结论 |
| 压缩降低含缓存成本 | 未测 | A1/A2 配对 trace、成本脚本与置信区间 |
| pass@1 非劣 | 未测 | 已冻结协议、足够样本、配对 CI |
| 路由降低成本或延迟 | 未测 | A1/A3/A4 配对结果 |
| 沙箱启动及 exec 开销 | 本机有限工程采样已测 | 5 个 exec 样本/路线，仅热环境诊断，不外推 |
| A2A 协议实现 | 未实现 | 代码与协议验证 |

当前已实现离线成本核算、预算预留和数据边界，并通过本地质量门（results/validation/p0/）；这些是工程验证，不是模型或 benchmark 结果，不得据此填写实验效果简历条目。

2026-10-24 实测证据：[首个 dev 报告](../results/analysis/arm64-smoke-20261024/PILOT_REPORT.md)、[逐臂汇总](../results/analysis/arm64-smoke-20261024/pilot-summary.json)、[官方聚合报告](../results/analysis/arm64-smoke-20261024/official-summaries/)、[沙箱原始采样](../results/validation/live-preflight/sandbox-audit.json)。五臂均未触发压缩、所有请求均用 strong，不能据此声称压缩或路由节省费用。官方对空补丁不启动测试；另一次人工 marker 负对照执行了测试但不计模型成绩。
