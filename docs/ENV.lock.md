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
