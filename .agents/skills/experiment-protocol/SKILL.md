---
name: experiment-protocol
description: Prepare ctxroute preregistration, freeze dev and test selections, specify paired analysis, and maintain evidence-backed claims before interpreting benchmark results.
---
# 实验协议

先读 docs/PREREG.md、docs/DECISIONS.md、docs/CLAIMS.md 和根目录 AGENTS.md。

1. 预注册写清 H1–H3、主比较、实验臂、实例集合、seed、运行顺序、步数和墙钟预算、δ=0.05、重试规则、缺失处理、停止规则及统计方法。草稿不得宣称已冻结。
2. 读取正式 test 结果前提交协议和 split，保存数据集 revision、选样算法、随机种子与清单哈希。缩减样本只按事先固定的预算规则或独立随机方法选择，不按难度或成功结果挑选。
3. dev 用于调试；pilot 与 test 分开报告。当前 10 USD/3 天约束下，test 可能完全不执行，必须报告未测。不得把少量 dev 结果包装成完整基准。
4. A1 是 A2/A3/A4 的内部对照，A0 是外部基线。先定义 A0/A1 一致性标准，无法证明一致时分别报告，不把差异归因于压缩或路由。
5. 单 seed 二元配对成功使用精确 McNemar；多 seed 差值按实例聚合再做实例级配对 bootstrap，不把重复 seed 当成独立实例。McNemar 的二元输入另行预先定义，不能输入 seed 平均值。非劣需成功率差 CI 下界至少为 -0.05；不显著不等于非劣。
6. 成本含所有层、缓存及辅助调用，配对比值的零分母处理要预先确定。H1 长轨迹相关性不能解释为因果退化；其余分析标注探索性。
7. Claims Ledger 每项链接到 manifest、原始 trace 和分析产物；缺证据写未测。基础设施失败、重跑和预算终止不得删除。

每阶段按完成项、验收命令与摘要、偏差、费用、下一步、待决事项汇报并等待“继续”。
