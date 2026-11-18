# 实验执行与复现

实验分为准备、推理、预测交接、独立判分和分析。运行前需安装锁定依赖，配置模型密钥、价格快照、预算及隔离执行环境。模型配置见 [API_SETUP](API_SETUP.md)，比较与停止规则见 [PREREG](PREREG.md)。

以下命令从项目根目录执行，路径为示例；每次配置或源码变更应使用新的计划与输出目录。先准备下文所述的脱敏任务文件和固定摘要镜像表，再生成计划。候选臂配置应使用相同任务额度，并符合所选阶段的请求预留下界及余额；示例路径需替换为实际文件。

## 1. 冻结计划

```sh
.venv/bin/ctxroute experiment plan --arms A1 A2 --count 1 \
  --tasks data/private/tasks.jsonl --images configs/private/images.json \
  --arm-config A1=configs/private/A1-pilot.json --arm-config A2=configs/private/A2-pilot.json \
  --budget-lane A1=baseline --budget-lane A2=checkpoint --output results/plans/dev-pilot.json
.venv/bin/ctxroute experiment reproduce prepare --plan results/plans/dev-pilot.json --workflow results/workflows/dev-pilot
```

schema-3 计划冻结实际任务文件、镜像表、划分、实验协议、配置、价格、锁文件、执行脚本和源码指纹；逐任务记录输入哈希、镜像摘要、任务额度和预算分配。准备阶段按 `arm`、`variant`、`seed` 分组并写入 `workflow.json`，不读取密钥或发送请求。相同内容可以重复准备，不同内容禁止覆盖。旧计划保留为历史记录，新执行需重新生成计划。

当前计划器仅接受 `data/split.json` 中 dev 前 1–5 项、固定 seed 和 A0–A5；A2/A4 可选择 `--truncate` 生成消融计划。它不提供正式 test 或多 seed 调度。

**阶段模式边界**：选择活动阶段时，每个臂必须在计划生成时通过 `--budget-lane` 绑定 `baseline`、`checkpoint` 或 `validation`。生成时只读核对整组任务额度、分配余额及最低请求预留，同时拒绝内部臂间未声明的控制变量差异。预算分配不决定实验角色，不能从臂名推断分配，也不能事后修改已冻结计划。预算模式与分配规则见 [BUDGET_SCOPES](BUDGET_SCOPES.md)。

## 2. 准备输入与镜像

任务 JSONL 每行只能包含：

```json
{"instance_id":"example__project-1","repo":"example/project","base_commit":"0123456789abcdef0123456789abcdef01234567","problem_statement":"Issue description"}
```

实际实例必须与计划完全匹配，不得包含 gold patch、测试补丁或判分字段；`base_commit` 必须是实际的 40 位 Git SHA。镜像表为实例 ID 到 `sha256:...` 或 `image@sha256:...` 的 JSON 映射。推理必须使用计划冻结的两个文件，镜像满足 [`sandbox/docker.py`](../src/ctxroute/sandbox/docker.py) 的隔离约束。

工具镜像与判分环境分离；构建镜像、安装依赖或下载数据由独立准备流程完成，执行入口不会自动创建云资源。源码身份和构建失败记录要求见 [BUILD_PREFLIGHT](BUILD_PREFLIGHT.md)。

SymPy 的 Python 3.9 上游环境配方使用仓库的 `bin/test`，不保证安装 pytest。
若任务允许 `python -m pytest`，准备阶段须先补齐测试工具，再冻结新镜像摘要。
[固定依赖](../configs/environments/sympy-py39-pytest.lock)与
[派生镜像配方](../configs/environments/sympy-py39-pytest.Dockerfile)提供该环境。
用 `pip download --only-binary=:all: --no-deps --require-hashes -r <lock> -d <context>/wheels`
准备 wheelhouse，将锁文件复制为构建目录的 `requirements.lock`，再使用该 Dockerfile，
以不可变且已清理判分数据的任务镜像 ID 作为 `BASE_IMAGE`，在 `--network none` 下构建。
构建目录只包含配方、锁文件与 wheel；不得发送整个项目或密钥。
在隔离容器中实际运行所需公开测试，记录原始退出码、收集/运行数量及新镜像 ID。
保留原镜像与旧计划；新环境必须进入新计划，不能替换历史冻结值或官方判分镜像。

缺少测试运行器、收集失败、无测试执行与断言失败分别报告。测试无法启动不能获得
通过证据，也不能据此指示 Agent 修改实现；只有对应范围的实际成功验证才能解除阻碍。

## 3. 显式执行推理

```sh
.venv/bin/ctxroute experiment reproduce infer --workflow results/workflows/dev-pilot --tasks data/private/tasks.jsonl --images configs/private/images.json --execute
```

需要 runtime 的 `execution_mode=live`、启用所选预算模式的执行开关、有效价格快照和可用沙箱。阶段模式还要求逐运行的有效 `budget_lane`。请求派发前预留最坏费用，开始同一实例时检查其剩余各臂的完整额度。

恢复以任务为单位：完成任务跳过；中断任务保留证据与未结算预留，禁止自动重跑。失败、空补丁、预算终止均保留，不据结果替换实例。

## 4. 导出预测与独立判分

```sh
.venv/bin/ctxroute experiment reproduce export --workflow results/workflows/dev-pilot
```

所有计划任务必须有完成 manifest 和匹配预测。每个 `arm/variant/seed` 单独导出 `predictions/*.jsonl`；`handoff.json` 记录原始及合并预测哈希、唯一 `grading_run_id` 和官方 harness 命令参数。导出阶段不会运行判分。

在独立判分环境中，使用交接记录中的命令与预测执行官方 SWE-bench harness，保留版本、run ID 和日志。改变补丁必须使用新判分 ID。空补丁和失败任务仍属于导出集合，官方可能不会为其生成逐实例测试报告。

## 5. 导入报告并分析

将官方逐实例报告存入本地 `results/private/`，由独立判分侧建立推理 `run_id` 到报告及回执的 JSON 映射：

```json
{
  "PLAN_ID-0000": {
    "report": "results/private/grading/A1/example__project-1/report.json",
    "receipt": {
      "inference_run_id": "PLAN_ID-0000",
      "instance_id": "example__project-1",
      "grading_run_id": "ID_FROM_HANDOFF",
      "predictions_sha256": "SHA256_OF_RUN_PREDICTIONS",
      "report_sha256": "SHA256_OF_REPORT",
      "harness_version": "VERSION_FROM_RUN_MANIFEST"
    }
  }
}
```

报告格式为实例 ID 到包含布尔值 `resolved` 的对象映射。导入会验证实例、推理及判分 ID、原始预测文件哈希、报告哈希与 harness 版本，并核对冻结 handoff；不能只按同题实例匹配。回执仍是独立判分操作者提供的证据，文件结构和哈希不能认证报告来源。未返回的逐实例报告可以不出现在映射中，分析保留缺失项，必要时结合官方汇总说明空补丁分类。单份 `experiment import-grade` 同样要求 `--receipt`。

```sh
.venv/bin/ctxroute experiment reproduce analyze --workflow results/workflows/dev-pilot --reports results/private/reports.json --output results/analysis/dev-pilot
```

分析核对交接哈希，导入 `grade.json` 并输出 `runs.csv`、`analysis.json`、`REPORT.md`、`failure_review.csv` 和 `workflow-provenance.json`。相同报告可复用，不同报告不能覆盖已导入判分；已有分析目录不能覆盖。失败分类需要根据轨迹填写，可选绘图使用 `experiment plot`。

普通 `experiment analyze` 默认严格校验内部配对条件。历史证据缺失或不一致时，可显式指定 `--allow-unverified` 输出标记为 `unverified_historical` 的描述性分析；该选项不能用于工作流的正式验收，也不能证明压缩或路由的因果效果。A0 始终标记为外部参考。

## Makefile

```sh
make plan ARM=A4 COUNT=1 PLAN_OPTIONS="--arm-config A4=configs/private/A4-pilot.json --budget-lane A4=checkpoint"
make reproduce ARM=A4 STAGE=prepare
make reproduce ARM=A4 STAGE=infer EXECUTE=--execute
```

`PLAN`、`WORKFLOW`、`TASKS`、`IMAGES`、`REPORTS`、`OUTPUT`、`PLAN_OPTIONS` 均可覆盖。默认 `STAGE=prepare`；推理必须显式提供执行参数。
