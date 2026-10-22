# P0 本地准备验收

日期：2026-10-22。结论：本地准备通过，完整 P0 尚未通过；未启动 P1 实验。

## 已完成

- Python 3.12 工程与 uv.lock；工具和依赖只安装在项目目录。
- 四个项目 skills 通过 skill-creator/quick_validate.py。
- Decimal 缓存成本核算；持久化预算预留/结算，默认禁用付费。
- Agent 输入字段白名单；冻结 60/240 划分、候选顺序与源 revision。
- 用户确认的三天 pilot 协议及停止条件。

## 质量门

命令：`sh scripts/check.sh`。原始输出：results/validation/p0/checks.log。
ruff check、ruff format --check、pyright 全通过，pytest 18 项通过。
覆盖费用核算、非法 usage、80%/硬上限、并发预留、重启后未知账单、重复派发、实际费用超预留锁停、数据隔离及冻结划分复现。

首轮 lint 误扫依赖缓存，已显式排除 .cache/.tools/.venv；pyright 发现的 contextmanager 返回注解已修正。未修改第三方代码。

## 未完成与边界

- Docker hello-world 和官方 harness 单实例未运行：本机 arm64，未配置 x86_64 云环境。
- DeepSeek 最小 API 请求未运行：用户密钥尚未设置，付费适配器未实现。
- SQLite 预算组件通过离线测试，不能代替供应商硬限额；尚未接入真实调用。
- 多余系统软件、UI 扩展和可选框架暂缓。
- 因真实验收未完成，不将本地通过写成完整 P0 通过或基准结果。

## 费用与下一步

本轮 API 调用 0 次、云资源创建 0 次，对应实验费用 0 USD，剩余 10 USD；这不是读取供应商历史账单所得的账户余额。

下一步完成 API 适配和最小调用，再落实预算内的 x86_64 环境。云端不可行则依已确认的降级方案继续实现及离线验证，实测指标保持未测。进入下一阶段前等待用户“继续”。
