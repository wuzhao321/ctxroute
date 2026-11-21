# 开发与测试

项目使用 Python 3.12，依赖定义在 pyproject.toml，并由 uv.lock 锁定。在项目根目录使用 uv 创建环境和安装开发依赖。

```sh
uv sync --frozen --group dev
```

需要处理数据或使用真实执行适配器时，按需加入 data 依赖组和 runtime 扩展。

```sh
uv sync --frozen --group dev --group data --extra runtime
```

模型配置见 [API_SETUP](API_SETUP.md)，执行接口见 [REPRODUCE](REPRODUCE.md)。环境安装与模型请求是独立步骤。

## 本地质量门

```sh
sh scripts/check.sh
```

检查脚本依次运行 ruff check、ruff format --check、pyright 和 pytest，使用冻结依赖及离线模式。它优先寻找项目工具目录内的 uv，也支持 CTXROUTE_UV 指定可执行文件；若 Node 未在 PATH 中，可通过 CTXROUTE_NODE_DIR 指定其目录。

## 验证范围

tests/ 覆盖成本计算与阶段分配、配置与数据边界、上下文和路由、执行器、沙箱接口、模型传输和离线工作流。固定响应模型和临时工作区用于验证控制流程。

| 检查范围 | 相关测试 |
|---|---|
| 请求字段、流式完成条件、SDK 与原始回执一致性、未知用量预留 | `test_model_transport.py`、`test_stream_receipt.py`、`test_stream_acceptance.py`、`test_reasoning_usage.py` |
| 阶段身份、并发分配、硬上限、暂停阈值及两阶段账本 | `test_budget.py`、`test_stage_budget.py`、`test_two_phase_budget.py` |
| 压缩观测、快照校验与离线回放输入 | `test_compression_audit.py`、`test_compression_replay.py` |
| 检查退出码归因、diff 绑定、失败保留及目标和回归范围 | `test_verification.py`、`test_repair_evidence.py`、`test_repair_policy.py`、`test_target_action.py` |
| 可选编辑检查点、源码决策、动作历史及信息组织 | `test_first_edit.py`、`test_post_edit.py`、`test_source_decision.py`、`test_action_context.py`、`test_efficiency.py` |
| 重复动作指引与可选动作恢复 | `test_repeat_guidance.py`、`test_failure_recovery.py` |

模型传输测试使用合成凭据、HTTP 拦截器和内存流，不发送真实模型请求。无效用量用例可能触发 SDK 的用量提取警告，应结合对应断言判断，不能将缺失或非法用量当作零费用。

这些检查验证实现行为。任务修复以独立官方 SWE-bench harness 的 resolved 结果判定；完整流程还需单独准备匹配镜像、审计隔离条件及输入字段。基准数据的 gold patch、隐藏测试与判分脚本只进入独立判分环境。

运行记录与开发说明按 .gitignore 保留在本地。提交前检查暂存区，避免纳入环境文件、密钥、账本、预测、原始轨迹或个人资料。
