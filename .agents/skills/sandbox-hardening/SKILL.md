---
name: sandbox-hardening
description: Configure and verify ctxroute task containers for network isolation, process limits, command timeouts, output truncation, and separation from benchmark grading data.
---
# 任务沙箱

适用于 ctxroute 的模型生成命令容器。威胁模型是错误或危险命令；容器共享宿主内核，不声称零风险。

1. 每实例使用匹配架构和版本的 harness 实例镜像。参数包含 --network none、--cap-drop ALL、--security-opt no-new-privileges、显式 pids/内存/CPU 限制。
2. 不绑定宿主目录、Docker socket 或密钥；文件用 docker cp。模型 API 调用在编排器侧，容器不需要 API key。核实实际镜像内容，确保 Agent 不可读 gold/test patch、判分脚本和隐藏测试。
3. exec 默认 30 秒超时，终止整个进程组，区分超时和退出码。输出头尾截断并保留原文 artifact ID；落盘同样受总大小限制。
4. 自检覆盖外联失败、宿主不可见、子进程在超时后退出、输出截断标志正确、受引用 artifact 未被回收。进程限制用有界子进程检查，不运行无界 fork 炸弹。
5. 容器在单任务内复用，结束后销毁。判分使用另一批干净容器。异常清理必须使用本次资源 ID，不做全局清理。
6. 分别测 create 到首次 exec 就绪、exec 开销、MCP stdio 相对直接调用开销；保存每次样本、机器配置和脚本，报告 P50/P95，不预设结果。

Docker 或目标架构不可用时标记验证未执行；mock 通过不能替代真实隔离验收。
