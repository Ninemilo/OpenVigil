# 完整工作流评测

入口：`python -m windops_backend.operations.workflow_eval`。它执行生产的源数据导入、任务创建、outbox 领取、SQL 工具、LangGraph、诊断、方案和五维审核，最后停在人工审批之前。它不创建审批或工单，不改变生产置信度门槛。原 `reasoning_eval` 的单提示词评测继续保留，但不能替代本入口。

## 输入与标签

- [输入样例](tests/fixtures/workflow-eval/inputs.json)只包含拓扑、接收记录、测量、告警、知识正文、资源、天气、任务请求及调用者范围。两个样例均明确为合成数据，不代表现场或专家结论。
- [标签样例](tests/fixtures/workflow-eval/labels.json)与输入分文件保存。标签同时绑定输入文件原始字节 SHA-256 和每个案例的规范化 SHA-256；改变输入后必须重新审核标签，不能自动更新哈希来掩盖变化。
- `run` 不接收或读取标签。`score` 是执行完成后的另一条命令，加载标签并要求完整案例集合。失效、未执行和错误案例不能从分母中删除。
- 专家案例须提供脱敏声明、原始资料相对路径、大小与摘要，以及标注人的身份、时间、审核资料摘要。导入器实际核对原始文件；标签中的专家声明本身不授予专家资格。专家资质、语义上的引用支撑关系和现场适用性仍须独立核验。
- 知识正文不预置向量；生产工具实际生成段落、索引并检索。示例正文采用历史文本导入路径，`source_verification=unverified`，不冒充已验证 PDF 原件。输入中的任意正文仍是不可信参考资料。
- 历史 `as_of` 保持原样，报告另记运行时间。七个审批前节点读取提供的事实快照；本工具不改系统时间、不重新证明历史天气仍有效，也不授予当前作业权限。

## 本地软件验证

从 `backend` 目录，在项目已冻结的验证环境中执行。以下目录必须尚不存在：

```powershell
$env:PYTHONPATH = 'src'
python -m windops_backend.operations.workflow_eval run `
  --inputs tests/fixtures/workflow-eval/inputs.json `
  --artifact-root tests/fixtures/workflow-eval `
  --mode software `
  --output ../.artifacts/workflow-eval-software

python -m windops_backend.operations.workflow_eval score `
  --inputs ../.artifacts/workflow-eval-software/inputs.json `
  --labels tests/fixtures/workflow-eval/labels.json `
  --run ../.artifacts/workflow-eval-software/run.json `
  --output ../.artifacts/workflow-eval-software/score.json
```

软件模式使用真实 SQLite、SQL 工具和生产编排，推理与 embedding 使用项目已有的确定性实现。流程可能全部跑到人工审核，但标签评分仍失败，例如确定性实现对应该拒答的合成案例给出诊断。**执行成功与质量评分通过是两个不同结果**；本工具保留两者，不能用确定性流程成功宣称真实模型精度。

## 受控供应商执行

只有在明确批准数据发送范围与有限预算、核对模型价格和完整输入容量后，才运行 `--mode provider --allow-paid-provider`。该模式要求：

1. 已准备并审核的源数据与独立标签。
2. 显式环境变量 `WINDOPS_EVAL_ADMIN_DATABASE_URL`，只接受 loopback `postgresql+asyncpg` 管理连接且拒绝 URL 查询参数。数据库必须支持当前 Alembic 迁移和扩展。评测只新建随机 `windops_eval_*` 库，迁移并执行后删除自己确认创建的库，不复用业务库，不终止其他连接。
3. 使用项目现有 `WINDOPS_LLM_PROVIDER`、对应供应商模型/凭据或 `WINDOPS_LITELLM_MODEL`，以及 `WINDOPS_EMBEDDING_MODEL`、embedding 配置。不隐式加载 `.env`；只读取明确传入的进程环境。凭据不要写入案例、预算、价格文件或命令行。
4. 与两个实际模型精确匹配的价格文件，遵循 `FrozenRateCard`。每项包含请求种类、请求模型、允许的响应模型、每百万输入/输出 token 价格、模型完整输入容量、容量出处与证据 SHA-256。价格文件包含币种、出处和有时区的核对时间。实际供应商账单仍需对账。
5. `WorkflowBudgetLimits` 文件，明确请求总数、每请求/总输入字节、每请求/总输出 token、费用总额、单请求/总时长，全部有限。价格与预算文件在新输出目录冻结后才传入执行子进程。

调用形式：

```powershell
python -m windops_backend.operations.workflow_eval run `
  --inputs $ReviewedInputs `
  --artifact-root $ReviewedArtifactRoot `
  --mode provider --allow-paid-provider `
  --rate-card $VerifiedRateCard `
  --budget $ApprovedBudget `
  --output $NewEvidenceDirectory
```

这些变量必须指向已经审核的真实文件；仓库没有默认真实价格、专家案例或付费批准。随后使用相同 `score` 命令对冻结输入与独立标签评分。

预算在每次 completion/embedding 请求之前预留。completion 输入费用按所声明的模型完整容量预留，embedding 按批内每条输入分别预留完整容量；输出按明确 token 上限预留，不用“字节数等于 token 数”的估算。生产重试中的每次实际 embedding 请求均计入。未知用量、错模型、超时或取消保留费用预留并停止新请求，不能把费用写成零。响应已经收费但被 schema/业务门槛拒绝，也保留用量与费用。费用是冻结价格下的估算，不保证供应商账单绝对一致。

安装版 LiteLLM 1.96.2 的 embedding 路径会把显式 `max_retries=0` 回退到默认值 2，因此只给顶层函数传零不足以关闭隐藏重试。专用子进程在导入前设置 `DEFAULT_MAX_RETRIES=0`、使用本地模型元数据，并固定/核验运行时重试、fallback、alias、cache、callback 和参数修复开关；实际 OpenAI 客户端也必须报告零重试。当前仅支持冻结的 LiteLLM 1.96.2 / OpenAI 2.54.0 的直接 OpenAI-compatible chat 与 embedding 路径；Responses、Router、其他供应商路由和未经验证的 SDK 版本均明确拒绝。退出时恢复所有覆盖的 SDK 全局、客户端检查、transport 和环境变量。实际 SDK 的离线 HTTP 边界测试不等于真实供应商或账单验证。

每案例生产 outbox 段最多 240 秒，保持在现有五分钟领取租约内；准备阶段至公开结果审计同时受总工作截止约束。结果读取另限10秒，最后清理有独立30秒宽限，父进程也有终止上限。总工作预算不是包含最后清理的绝对 wall-clock 上界，前面案例的清理时间仍保守减少后续工作余量。取消不伪造生产终态；清理和迁移的失败单独保留。数据库创建确认丢失时不猜测归属或删除，报告生成的身份供人工核查。

## 证据与评分边界

输出目录保留冻结输入、各案例 JSON、最终 `run.json`；SQLite 软件模式额外保留实际数据库。报告包含实际源码文件摘要、运行包版本、每节点公开产物、工具调用、用量、耗时、数据库归属/清理结果和预算收据。不要公开含敏感事实的案例或本地数据库。

供应商原始响应、隐藏推理、完整异常文本及凭据不保存。仅保留严格公开 schema 下的候选产物；拒绝的候选用于事后评分，不写回任务诊断。异常在 outbox 持久化前转换为固定安全代码，保留生产节点及费用审计。

评分检查分类、正确拒答、引用 ID 与来源映射、必需资料召回、禁止来源/短语、方案绑定、五维审核、无审批/工单、耗时和费用证据。引用 ID 存在并不等于语义支持成立；禁止短语检查也不能穷尽所有不安全建议。报告分别保留自动评分、软件边界、专家与现场资格，不将有限规则冒充专家评审。

`run` 在任何案例未到达人工审核时返回非零，包括正常被诊断门槛拦下的拒答；这不妨碍随后 `score` 将有完整证据的正确拒答计为通过。`score` 对标签不匹配、缺失案例或自动评分失败返回非零。外部资格始终单独记录，实际 PostgreSQL、供应商调用和专家样本必须有各自验收证据。
