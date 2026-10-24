# 简单完整链路检查（离线）

日期：2026-10-24。结果：新增一个集成用例通过；全项目必要检查为 63 passed、ruff/format 通过、pyright 0 errors。原始输出见 results/validation/full-chain/checks.log，检查边界见同目录 scope.json。

## 实际走过的代码路径

临时配置与任务 → 创建冻结清单 → 工作流准备 → run_plan → Engine → 固定模型动作 → 临时玩具文件读写 → P1 原文引用与 A4 规则路由 → 实际 diff → 清理 → 任务级续跑 → 分臂预测导出 → 报告格式样例导入 → analysis.json / REPORT.md。

任务是修正 toy.py 中的加法实现。A1/A4 都使用固定动作；工具适配器仅接受三个精确命令名，并用 Python 文件操作实现，拒绝其他输入，不运行任意 shell。补丁由实际修改前后的文件文本生成。续跑确认不创建新模型实例；A4 确认只读步骤后切换 cheap，编辑之后锁回 strong；P1 引用必须能对应先前观察。

报告使用带 resolved 字段的格式样例，验证的是导入和分析接口。所有样例 manifest、预测、判分和报告只存在 pytest 临时目录，标注 unit_test_fixture_not_benchmark，不写入项目真实 results/runs。测试代码见 tests/test_full_workflow.py。

## 修复与保护检查

- 判分导入先校验全部已有 grade 的预测/报告哈希；后续任务不匹配时，前面的新 grade 也不会被提前写入。
- 预测交接支持相同内容重复执行；修改任何原始预测会在分析前被拒绝。
- 没有 usage 时成本保留未知，固定响应的零 token 不作为付费测量。
- 成本取值在缺少上界时才访问预留字段，避免默认值表达式被提前求值。
- 报告同时列出缺失判分、成功/已启动和已判分子集 pass@1。

## 未覆盖

没有使用真实模型、Docker、MCP 工具调用或官方 harness；没有加载真实基准题目、gold patch 或隐藏测试。未验证模型修复能力、真实压缩收益、容器隔离或线上资源开销。本轮新增 API 和云费用为零，既有未知费用预留未修改。

单独复查这个用例：`.venv/bin/python -m pytest tests/test_full_workflow.py -q`。完整离线检查：`sh scripts/check.sh`。两者均不安装依赖或调用付费服务。
