# 本机 ARM64 dev 环境

2026-10-24 用户已授权本机小规模评测，不租云主机。仍使用固定首题 `sympy__sympy-22840`、A0→A4、单并发；正式 test 暂缓。授权与预注册补充见 PREREG.md。

## 当前状态

首轮五臂真实推理和官方汇总已完成；均为空补丁，无成功修复。另一次基础设施负对照执行了官方测试且无基础设施错误。项目 VM 已于 18:55 停止释放内存，镜像保留；当前 Stopped 是主动结束，不是下载阻塞。详见 `results/analysis/arm64-smoke-20261024/PILOT_REPORT.md`。本轮费用及失败原因以该报告为准，以下是环境建立过程的历史说明。

最新进展：在用户授权重试后，固定 Ubuntu 镜像在独立 180 秒下载上限内成功下载且 SHA256 匹配。VM 已 Running、无宿主挂载，Docker Server 29.9.2 已可用。官方 harness 3.0.17 首题 ARM64 镜像已构建，推理镜像清理了未来 Git 引用；真实 Docker/MCP 隔离检查已通过。下面保留首次失败和恢复条件的历史记录。最新证据为 `ubuntu-image.json`、`lima-status-running.json`、`docker-info.json`、`harness-image.json` 和 `sandbox-audit.json`，均位于 results/validation/live-preflight/。

MCP 初次检查被正式 runtime 的 offline_only 门阻止，记录保留为 sandbox-audit-attempt1.*。随后用只含容器配置的临时根目录完成 MCP 实测；此检查没有加载模型或密钥。新 runtime 准备切换 live，固定首题五臂总 allowance 1 USD，模型效果仍需实际推理和官方判分。

- Lima 2.2.1 安装于 `.tools/lima`；官方发行包 SHA256 与官方 SHA256SUMS 一致。
- Docker CLI 29.6.1 安装于 `.tools/docker`。这是客户端；Docker 引擎尚未安装成功。
- 已创建专用 `ctx` 虚拟机配置：VZ 原生 aarch64、4 CPU、8 GiB 内存、120 GiB 稀疏盘上限、`mounts: []`。配置不等于已启动的 VM 或已创建的 120 GiB 磁盘。
- Linux 镜像下载失败，VM 尚未启动，未进行隔离验收、实例镜像构建、付费推理或官方判分。
- Lima 依次尝试固定日期镜像与滚动镜像地址，分别超时和连接中断。按“两次失败即停”停止重试，日志保留在 `results/validation/live-preflight/lima-start.log`。Lima 的 `--timeout=10m` 没有限制这次启动前下载，总下载阶段明显超时；后续下载必须另加外层时限，不能继续依赖该参数。

## 已安装工具入口

```sh
scripts/lima-local.sh --version
scripts/lima-local.sh list --json
scripts/docker-local.sh --version
```

两个入口只使用本项目工具与 VM 路径，不切换全局 Docker context。Docker 入口清除继承的连接配置，并明确指定本项目 Unix socket，方便后续在 MCP 的精简环境中使用。当前运行 `docker-local.sh info` 应失败，因为引擎尚未就绪。

## 恢复条件与剩余工作

先解决 Ubuntu 镜像下载网络问题，或提供从官方来源下载且匹配固定 SHA256 的本地镜像。固定源为 `https://cloud-images.ubuntu.com/releases/resolute/release-20260927/ubuntu-26.04-server-cloudimg-arm64.img`，SHA256 为 `63a93bd5a8d76e33b15ceb5daa3657bd79be804748051ab178e643b0f5da22e7`。恢复时每次下载设独立总时限与大小限制，校验通过后再将该本地镜像用于 VM 启动，不采用未校验的滚动镜像。

之后依次完成 Docker 引擎安装、无宿主挂载核验、官方 harness ARM64 实例镜像构建与内容审计、任务沙箱隔离与超时检查、运行配置和镜像摘要冻结，再执行真实推理。判分在另一批干净容器进行。只有这些前置项完成，才将 `configs/runtime.json` 的 `offline_only` 改为 `live`。

本轮新增 API/云费用为 0。历史 usage 估算累计 0.0000036 USD，未知调用仍预留 0.10 USD，未核对供应商账单。首题五臂最多预留 1 USD，累计 8 USD 暂停、10 USD 硬上限不变。

官方来源：[Lima 安装](https://lima-vm.io/docs/installation/)、[v2.2.1 发行页](https://github.com/lima-vm/lima/releases/tag/v2.2.1)、[Lima Docker 模板说明](https://lima-vm.io/docs/examples/containers/docker/)、[Docker ARM64 客户端下载目录](https://download.docker.com/mac/static/stable/aarch64/)。
