---
name: swebench-runner
description: Prepare and run ctxroute SWE-bench inference and official harness evaluation, including resource checks, run manifests, resume records, and scoped cleanup.
---
# SWE-bench 运行

先读根目录 AGENTS.md、docs/PREREG.md 和预算配置。当前 M5 仅开发；正式评测需要已确认的 x86_64 环境，不自动租主机。

1. 核验已安装 harness 版本的官方 CLI、镜像架构和磁盘要求，记录来源、版本及镜像摘要。先用固定 dev 实例冒烟。任务 Agent 与判分进程分离，Agent 无法读取 test_patch、参考补丁或判分脚本。
2. 核对获批实例清单、实验臂、seed、时间限制；预算打印包含 API 和云端未结算费用。预算配置未启用时不得调用付费服务。
3. 并发不超过可用 CPU 的 75%，再按内存、磁盘和预算下调，优先单并发；不能拿本机核数决定云端并发。cache_level 按实测空间选。
4. 每次运行创建唯一 run_id，保存 commit、配置哈希、模型版本、价格版本、harness 版本、镜像摘要、起止时间、预算终止原因及允许字段的实例输入。
5. 输出 predictions.jsonl 后交给独立官方 harness。严格区分 resolved、unresolved、基础设施错误与未完成；不能把命令退出成功当作实例通过。
6. 恢复保留首次结果；重跑新建 run_id 并引用原运行，记录原因。先核对 harness 缓存规则，避免更换补丁却复用旧判分。
7. 清理仅限本运行拥有的容器和镜像；保留证据。不执行 docker system prune。

输出运行清单、原始日志路径、费用和未完成事项。未判分仅标为未测。
