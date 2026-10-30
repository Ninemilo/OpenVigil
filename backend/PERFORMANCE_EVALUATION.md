# 有界性能测量

当前新增工具可运行独立的 **software** 场景：真实 Uvicorn/HTTP、四个不同静态鉴权主体、两个互相隔离的授权范围、一个拒绝范围，以及新建 SQLite 数据库。测试 Header 绕过关闭，业务读取实际写入数据库审计。输入是明确的合成数据。

入口另提供 **services** 场景，使用本任务新建的 PostgreSQL/TimescaleDB、Redis、MinIO、Neo4j，以及隔离的 API、通用 worker、relay、读取审计 worker 和按需文档 worker。实际服务运行与组件测试分开记录；本轮 Docker 不可用，尚未执行这组真实多服务负载。服务失败会保存报告，不降级为软件测量。当前工具不代表生产容量或正式发布验收。

## 运行

从仓库根目录使用项目冻结的 Python 环境（需 `test` extra 中的 aiosqlite），并设置 `PYTHONPATH=backend/src`：

```powershell
$env:PYTHONPATH = 'backend/src'
python backend/scripts/run_performance_e2e.py --profile backend/tests/fixtures/performance/software-small.json
```

固定样例预先声明 9000 条历史记录、并发 4、两条路径各 100 次测量及总共 4 次预热；每条路径覆盖全部四个主体。每次创建新的 `.artifacts/performance-e2e/<run-id>/`，数据库和 JSON 报告保留为证据，不使用现有本地栈。监听地址为随机本机端口，凭据仅通过本次子进程专用环境变量传递，子入口读取后立即移除，不进入命令参数、报告或文件。stdin 只传停止指令。结束后关闭且等待本次创建的完整进程树，检查监听释放。

负载前另有固定 10 次权限控制请求，单独记录在 `authorization_controls`，不计入 204 项负载计划或延迟分位数。资源采样也不覆盖这组启动控制检查。

进程返回码：

| 返回码 | 含义                                                                          |
| ------ | ----------------------------------------------------------------------------- |
| 0      | 此次软件 profile 的全部已登记预算及完整性检查合格；仍不代表真实服务或生产容量 |
| 1      | 请求、预算、证据完整性、保护或清理失败                                        |
| 2      | 输入无效，或当前入口不支持该 profile；未运行负载                              |
| 3      | 完成软件测量或合成 services 测量；`qualified=false`，不授予生产资格           |

样例两项预算均为 `measurement_only`，因此不会返回资格通过。500/1000 毫秒查询参考值和历史序列 2000/5000 毫秒参考值是实验开始前登记的比较值，不是已确认的生产 SLO；超出值照常记录，不能根据本次结果放宽门槛。报告顶层状态和每条路径的 `reason_codes` 必须一起检查。

## 统计与证据

- `samples` 保留全部预热和测量计划项，以及失败、超时、丢弃、取消、未执行；失败耗时参与全尝试延迟统计。成功请求另列分位数。无自动重试。
- 分位数使用 nearest-rank；P99 至少需要 100 个实际延迟样本才可作门禁。每个主体的 25 次子样本可以展示数值，但不能据此独立认证 P99。
- HTTP 成功还必须通过语义核验：机组列表精确符合当前主体的资产域；历史聚合逐序列核对机组、来源、变量、单位、时间、均值、质量和迟到标记。不能将 200 空内容视为成功。
- `closed_loop` 在响应变慢时会降低到达率；`fixed_interval` 使用固定间隔、有界并发，满载时明确记录 dropped。请求计划、开始/完成时间和排程等待都保留，不能只汇报最快成功请求。
- 资源采样覆盖负载器和已验证的自有 API 进程树，包括 guardian、虚拟环境 launcher、实际解释器及其他自有成员。ready 记录的实际解释器 PID/开始身份必须属于私有树；每250ms检查成员变化，新增、消失或身份不符会取消测量并将资源证据标为不可用。Windows 使用只读原生 API，Linux 使用 `/proc`；无法读取是 unknown，不填零。当前实际验证平台为 Windows，Linux 实机仍未验证。
- 运行身份包含 `resource_accounting=load_client_and_owned_api_tree_v1`。早期仅登记 Windows launcher 的报告不覆盖实际 API 的 RSS/CPU，保留作历史原件，不能与当前完整树计量直接作优化同比。
- 资源窗口是预热与测量阶段，**不包含**建库、准备数据、启动和最后对账。RSS 为定期观测峰值，不能代表两次采样间的绝对峰值；总内存预算使用各进程观测峰值之和这一保守上界。CPU 为实际观测区间内的进程累计时间差。
- 资源守卫按同一预登记 RSS 限额停止新增请求；请求数、上传声明、历史行数、并发、耗时和响应字节数均有硬限。原始部分结果、清理结果和失败原因保留。
- 运行前后核对源码、HEAD、原 Git index 和根 `.env` 摘要；报告仅含摘要，不含 `.env` 内容。数据库除读取审计外的全部表按有序行内容核对；数据变动使运行失败。
- 比较要求 profile、数据、运行依赖及有效配置、环境和缓存策略身份一致；源码身份允许变化，以支持受控优化比较。不同 SQLite/Timescale、网络、网关、审计适配器或资源条件不能混为一组。

当前软件模式使用单个 Uvicorn worker、直接 loopback HTTP、SQLite 数据库审计和关闭的限流配置；没有网关、TLS、Redis 审计流、解析 worker 或付费模型调用。报告保留这些边界。正式多服务负载需独立运行身份、真实队列终态对账及完整资源覆盖，不能复用软件结果填充。

## Services 场景与输入

四个预登记场景均有配对的 `.inputs.json`。文件位于 `backend/tests/fixtures/performance/`：

| Profile                         | 实际工作计划                                                     | 测量时限 |
| ------------------------------- | ---------------------------------------------------------------- | -------- |
| `services-reads.json`           | 4主体、4条列表路线和2条历史路线，每条100请求；9000条合成历史     | 180秒    |
| `services-documents.json`       | 两授权域各上传一份固定两页中文扫描PDF，等待解析和索引            | 600秒    |
| `services-backlog-offline.json` | 停止自有文档worker，等待真实45秒TTL失效，排队后恢复              | 300秒    |
| `services-backlog-claim.json`   | 观察实际活动claim/started事件，停止worker，等待原300秒租约后恢复 | 720秒    |

例如，在 Docker 服务可用且输入条件满足时：

```powershell
$env:PYTHONPATH = 'backend/src'
python backend/scripts/run_performance_e2e.py --profile backend/tests/fixtures/performance/services-reads.json --services-inputs backend/tests/fixtures/performance/services-reads.inputs.json
```

文档场景的输入清单指向项目内已核对的合成PDF、独立解析Python和只读模型目录；其默认路径反映本次本地准备位置。其他机器应复制清单至项目内私有目录，填写实际已安装的解析环境和模型路径，并核对清单SHA。不得填占位模型、替换parser输出或把合成PDF称作现场资料。输入在创建服务前检查路径、文件字节、页数、页面尺寸及模型清单，运行后再次核对；文档ID每次独立生成。每域使用相同扫描件是明确的重复负载，不代表不同真实报告的统计多样性。

服务配置从显式参数构造，不读取根`.env`或继承供应商凭据。API使用DEVELOPMENT选择真实MinIO/Redis路径、请求绑定的JWT、服务端scope和原限流值；worker使用TEST选择确定性离线embedding。读取原件/索引/审计仍走实际服务；确定性embedding不能用作RAG语义质量结论。TLS证书只面向本次127.0.0.1，HTTP客户端校验证书和主机名。

## Services 账本与清理

- 仅创建随机UUID对应的Compose项目、具名卷和随机loopback端口。先检查本机Docker端点并固定transport；检查所有权、CID、运行实例身份和源manifest后才采样或删除。未知服务、挂载、替换后的容器或旧项目拒绝使用。
- 迁移、seed与建桶在有进程树包含边界的独立命令中执行。真实Alembic唯一head、空库seed、空对象存储是前提；不会借用其他栈的数据。API及四类worker由Windows Job/Linux进程组管理，超时和取消会清理包含的子孙进程。Linux运行分支尚未实机验证。
- `maximum_requests`约束测量GET、文档HTTP及测量期健康观察的合计。每条GET路线保留预热与全部计划项；backlog预留最多512次健康HTTP，剩余文档额度按登记数量分配。启动控制另有256次上限，独立报告且不计业务吞吐；MinIO最终回读、Neo4j/SQL对账属于证据阶段。
- 首次资源采样等待消耗测量总时限。资源每10秒观察动态自有进程成员及4个固定容器，统计回调8秒上限，取消收尾另有10秒；缺样、身份变化或未知指标明确失败。进程RSS与Docker working set使用独立预算，不相加；Docker CLI数值具有显示舍入精度，报告保留这个限制。短命子进程可能落在两个采样点之间，不能声称完整生命周期覆盖。
- 文档账本分别保留presign、上传、ingest、poll及accepted/unknown；POST超时不会换ID重投。SQL只读证据核对实际源版本、解析结果hash、passages、index与claim/attempt，明确区分未完成和终止失败。最终还需MinIO源字节SHA回读、限定owned passages的Neo4j投影和四主体持久化读取审计通过。
- 积压恢复不回写时间、不缩短TTL/租约。恢复需不同worker generation、原job更高attempt、旧claim无效、唯一结果及索引；fixture控制测试不证明自然等待或实际消息交付。
- 清理不依赖后续证据是否成功；重复取消仍等待自有资源清理。总清理预算默认180秒，构成操作各有时限；为避免遗留自有进程，超过总预算时继续完成这些有界清理并将超预算记为失败。未知资源不会被强制删除。
- JSON报告保存在 `.artifacts/performance-e2e/<run-id>/report.json`，包括未执行项和失败。私有配置、命令日志、签名材料位于同目录的`private/`，不得上传。服务进程停止后保留本地证据文件，报告不包含凭据、签名URL或文档原文。

services成功也仅为`measurement_only`。文档上限32份，成功样本不足100时P95/P99显示为空并记录`insufficient_samples`；没有从少量文件推断尾延迟资格。正式预算未通过时状态为`budget_unqualified`；真实服务完整性失败时为`failed`。应同时阅读逐路线预算理由、HTTP/文档账本、资源覆盖、源身份和清理结果。
