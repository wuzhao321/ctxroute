# ctxroute

研究结构化上下文压缩与步骤级模型路由对软件工程 Agent 成功率、含缓存成本和延迟的影响。

当前状态：P0 本地准备通过；已有成本核算、预算 ledger、数据隔离与冻结划分，尚无 Agent 实验结果。预算最多 10 USD（含 API 和云），3 天；8 USD 暂停。正式 test 暂缓，仅在资源允许时执行固定 dev pilot。

## 本地运行

当前机器已配置项目目录内 uv 和 Python 3.12 虚拟环境：

```sh
sh scripts/check.sh
UV_CACHE_DIR=.cache/uv .tools/uv-package/bin/uv run --frozen --offline ctxroute status
```

质量门执行 ruff、pyright 和 pytest，不调用 API 或读取密钥。status 只显示配置，不代表供应商余额或账单。

新机器需要 Python 3.12、uv 和 Node（供 pyright 使用），然后 `uv sync --frozen`，再运行检查。自定义 uv 路径使用 CTXROUTE_UV。当前脚本在无独立 Node 时可使用 Codex bundled Node，其他机器需自行提供。

## 数据与证据

- [预注册](docs/PREREG.md)：固定 pilot 候选、预算及统计规则，不授权正式 test。
- [冻结划分](data/split.json)：官方 upstream test 300 个 ID 分成 dev 60 / test 240，不向 Agent 提供答案字段。
- [决策记录](docs/DECISIONS.md)、[环境记录](docs/ENV.lock.md)、[P0 验收](docs/P0_STATUS.md)。
- [Claims Ledger](docs/CLAIMS.md)：实验效果全部未测。
- [本地检查日志](results/validation/p0/checks.log)。

重建脚本 scripts/freeze_split.py 只读取 instance_id 列，拒绝覆盖冻结文件；源 revision、哈希和 seed 在 split.json。仅在独立新工作目录重建，需要 `uv sync --frozen --group data`。

## 密钥与付费

密钥设置与单次 API 探针见 [API_SETUP](docs/API_SETUP.md)。运行 `.venv/bin/python scripts/setup_key.py` 可隐藏输入并以权限 600 保存；不要发送到聊天。.env 已被忽略。用 `.venv/bin/ctxroute api-probe` 查看离线计划。

预算使用 Decimal 和 SQLite 事务，未知费用保持预留，超过预留时记录并锁停。已接入固定连通探针，尚无通用 Agent 或云适配器；paid_execution_enabled 保持 false。详见 [API 准备验收](docs/P0_API_STATUS.md) 和 [云端候选](docs/CLOUD_PLAN.md)。

## 项目约定

见 [AGENTS.md](AGENTS.md)。四个项目 skills 在 .agents/skills/。本机 M5 仅开发，正式评测需 x86_64 环境。当前没有 benchmark 一键复现入口，不得用离线测试通过代替 harness 判分。
