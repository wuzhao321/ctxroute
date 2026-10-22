---
name: trace-analysis
description: Analyze ctxroute run traces for cache-aware dollar cost, latency, context compression, routing behavior, paired comparisons, and reproducible evidence tables.
---
# Trace 与成本分析

先读取 manifest、对应价格版本和预注册；原始记录只读，派生表和图另存。未知字段不得自动补零。

1. 每次模型请求（包括压缩、重试和离线标签生成）记录 request_id、run_id、instance_id、arm、seed、step、purpose、tier、model_id、usage、缓存字段、起止时间、latency_ms、error_type。工具事件补 tool、exit_code、上下文前后 token、compress_event 和 router_decision。
2. 区分 prompt_tokens 是否含 cached_tokens，依据供应商语义归一化；usage 必须非负且缓存量不大于输入量。tiktoken 仅用于调度，不用于宣称实付费用。
3. 模型费用 = (缓存命中 token × 命中价 + 未命中 token × 未命中价 + 输出 token × 输出价) / 1e6。逐请求记录价格时段、币种、官方来源与采集日期。旧价不能沿用；时段或账单不可识别时保留不确定性并按保守上界预留预算。
4. 加总辅助请求与云端计算、存储、网络费用；区分 API usage 计算值、实际账单、待结算与估算。对账差异必须解释，未知账单不能写零。
5. 按层报告缓存命中率；token 下降不等于美元成本下降。延迟包含重试、工具与沙箱等待，另报基础设施开销及预算终止比例。
6. 按实例配对分析，用预注册的聚合与 bootstrap；图标明 dev/pilot/test、样本数、seed 数、价格日期和未完成数量。小样本只作探索，功效不足如实写明。

表和图链接到原始运行与分析入口；可支持结论写入 docs/CLAIMS.md，未测项继续保留未测。
