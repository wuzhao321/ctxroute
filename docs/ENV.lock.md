# 环境检查记录

检查日期：2026-10-22（Asia/Shanghai）。这是检测记录，不是已完成安装的锁文件。

| 项目 | 证据 / 状态 |
| --- | --- |
| OS | sw_vers：macOS 26.4，build 25E246 |
| 架构 | uname -m：arm64 |
| CPU | system_profiler：Apple M5，10 核（4 Performance + 6 Efficiency） |
| 内存 | system_profiler：16 GB |
| GPU | SPDisplaysDataType：Apple M5，10 核；未发现 NVIDIA GPU；统一内存不等于独立显存 |
| 项目卷可用空间 | df -h：884 GiB，检查时快照 |
| Git / Python / make | /usr/bin 下命令存在，但执行因未安装 Apple Command Line Tools 失败；不算可用 |
| jq | jq-1.7.1-apple |
| Docker / code / uv / node / pnpm / gh / just | command -v 未找到；常见 /Applications 路径也未找到 Docker 和 VS Code |
| GitHub 插件 | 已连接；get_user_login 返回 wuzhao321；不代表本地 Git 认证已配置 |
| API key | 用户表示已有；尚未读取、设置或验证 |

sysctl 的硬件查询被沙箱拒绝，随后用 system_profiler 完成检测；未保存设备序列号和 UUID。

Codex bundled runtime 已验证 Git 2.53.0、Python 3.12.14 可运行。初次准备时缺少 PyYAML，官方 skill validator 未运行成功；后续在项目 .venv 安装锁定依赖，四个 skills 均已通过官方 quick_validate.py，日志在 results/validation/p0/skills.log。没有安装系统级软件。

## 本轮本地依赖锁与验收

uv 0.13.5 在 .tools/uv-package；.venv 使用 Python 3.12.14。uv.lock 锁定完整版本与包哈希：ruff 0.17.1、pyright 1.1.414、pytest 9.1.1、PyYAML 6.0.3、PyArrow 26.0.0。Node 复用 Codex runtime 供 pyright 使用。数据依赖仅用于提取 ID；正式 Agent/SDK 依赖尚未安装。

`sh scripts/check.sh` 已通过 lint、format、typecheck、18 项测试，详见 results/validation/p0/checks.log 与 manifest.json。CLI 配置状态也已离线验证。

仍未运行：docker hello-world、harness 单实例构建与判分、DeepSeek 最小请求。完整 P0 尚未通过。

## 2026-10-23 本地运行依赖增量

按用户授权安装项目级 runtime extra，精确版本与哈希见 uv.lock：pydantic-ai-slim[openai] 2.59.0、MCP 1.30.0、mini-swe-agent 2.4.6、swebench 5.0.2、OpenAI SDK 3.37.0。未安装系统软件或 Docker 引擎。

已确认 SDK 导入、上游 mini-swe-agent 关键源码哈希、真实 MCP stdio initialize/list_tools 四方法、官方 harness --help。没有调用沙箱工具或运行判分。记录见 results/validation/runtime-setup/。本轮必要检查修正了依赖运行配置的旧测试夹具，最终 62 passed、ruff 与 pyright 通过；未增加测试矩阵。

API 密钥已由用户本机设置，状态检查只报告 configured。API 联调结果后续单独记录；不能据 SDK 导入推断调用成功。

## 2026-10-24 真实评测兼容性准备

官方 swebench 固定为 3.0.17，替换与冻结旧数据 schema 不兼容的 5.0.2；其余运行 SDK 未改变。新的 uv.lock 记录版本/哈希，CLI 帮助和必要离线检查通过，见 results/validation/live-preflight/。Docker/本地虚拟机仍未安装，无 Linux 主机或云账号，执行位置待用户确定。首个固定 dev 四字段输入已准备，官方判分仍未运行。

## 2026-10-24 本机 ARM64 安装尝试

用户已批准本机 ARM64 小规模评测。Lima 2.2.1 与 Docker CLI 29.6.1 已安装到项目 `.tools`，版本命令通过；Lima 官方发行包 SHA256 校验通过。VM 配置为 VZ/aarch64、4 CPU、8 GiB、120 GiB 稀疏盘上限、无宿主挂载。两个 Ubuntu 启动镜像地址均下载失败，按约定停止重试，VM 状态为 Stopped、Docker socket 不存在；Docker 引擎、容器隔离及 harness 实测仍未完成。安装哈希和失败日志位于 results/validation/live-preflight/，恢复说明见 LOCAL_ARM64.md。未新增 API 或云费用。

后续重试成功，Ubuntu 镜像 945530880 字节且 SHA256 匹配；VM 启动后 Docker Server 29.9.2、Ubuntu 26.04.1 LTS，官方 harness 的任务镜像内部为其默认 Ubuntu/Python 环境。Docker/MCP 隔离检查通过，官方首题镜像与推理用清理历史镜像摘要见 harness-image.json。首题五臂推理已完成但均无补丁；官方负对照执行了实际测试、无基础设施错误。完成后于 2026-10-24 18:55（Asia/Shanghai）停止项目 VM，保留磁盘和构建缓存。运行前最后一次必要代码检查 63 passed。
