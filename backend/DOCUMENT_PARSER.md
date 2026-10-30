# 独立 PDF/OCR 文档解析

本增量在现有知识原件、段落、pgvector、权限、读审计与工程审核上增加 Docling 路径。默认仍使用原生解析；客户端明确提交 `parser: "docling"` 时，API 仅验证原件并事务性排队，不在 HTTP 中加载 OCR 或执行布局推理。这里的运行方法与本地验收不构成正式发布或现场精度资格。

## 运行边界

- `document-parse` 使用独立 Dramatiq actor，单进程、单线程；布局/OCR 在第二个独立 Python 中计算，API 与通用 worker 不安装这些数值依赖。
- 文档与解析事件保存原件 SHA-256、文档版本、模型清单 SHA-256、七个执行模块 SHA-256 和十二个核心包版本。执行时再次观察身份，任何漂移不能发布正文。
- 预取模型的文件大小与 SHA 全量核对，解析时关闭远程服务、外部插件和隐式下载。临时 HOME/模型缓存与凭据隔离，解析进程不继承数据库、对象存储和模型供应商凭据。
- 当前上限：50 MiB、200 页、页面宽高各 2000 PDF points、400 万字符、512 段，每段 8000 字符。子进程最多 180 秒、stdout 24 MiB、stderr 64 KiB；取消、超时或超限回收本任务子进程。加密 PDF、无文本结果和无法精确归属页面的跨页元素明确失败。
- 租约与发布事务防止过期 worker 写入。最多三次实际尝试；终态失败不生成正文、段落或向量。非终态失败与到期租约由现有 relay 恢复，原始失败事件保留。

## 准备隔离依赖与模型

Python 3.12。选择项目内独立环境，保持 API 环境原依赖。固定锁包含 CPU PyTorch 和平台条件；不要将整份解析锁安装进 API 环境。

```powershell
uv venv .artifacts/document-parser/runtime --python 3.12
uv pip sync --python .artifacts/document-parser/runtime/Scripts/python.exe backend/requirements.document-parser.txt --require-hashes --torch-backend cpu
.artifacts/document-parser/runtime/Scripts/python.exe scripts/prepare-document-models.py --output .artifacts/document-parser/models
Get-FileHash .artifacts/document-parser/models/openvigil-model-manifest.json -Algorithm SHA256
```

模型准备脚本执行受限公共下载并验证固定来源/权重。布局模型来自固定 Heron commit，TableFormer 来自固定 Docling models commit，中文检测/识别/方向模型来自冻结 RapidOCR registry SHA。清单记录许可证及原始链接。已有清单的模型目录会核验，不覆盖已有权重。模型准备完成后可移除网络下载权限；运行时不需要网络推理。

## API、通用 worker 与专用 worker 的配置

使用现有配置加载方式，给 API、relay 和专用 worker 提供相同模型清单与当前源码身份。以下值是配置示例；设置前先计算实际清单 SHA，不修改现有 `.env` 的凭据。

```text
WINDOPS_DOCUMENT_PARSER_ENABLED=true
WINDOPS_DOCUMENT_PARSER_PYTHON=<isolated Python absolute path>
WINDOPS_DOCUMENT_PARSER_MODELS_PATH=<model directory absolute path>
WINDOPS_DOCUMENT_PARSER_MODEL_MANIFEST_SHA256=<actual lowercase SHA-256>
WINDOPS_DOCUMENT_PARSER_RUNTIME_DIRECTORY=<writable task scratch directory>
WINDOPS_RELEASE_COMMIT_SHA=<same full nonzero commit SHA for API and worker>
```

启动专用队列：

```powershell
backend/.venv/Scripts/python.exe -m dramatiq windops_backend.document_parse_tasks --queues document-parse --processes 1 --threads 1
```

保留现有通用 worker、Outbox relay 与读审计 worker。`GET /api/v1/knowledge/parser-capabilities` 分别返回配置声明、专用 `worker` 观测和当前用户可读文档的 `queue`。只有配置齐全、有效心跳、真实消费活动、隔离运行预检和版本全部匹配时，`worker_health_verified` 才为 true；配置齐全本身不证明能处理，运行健康也不代表 OCR 精度验收。

`worker.status` 为 `unconfigured`、`unavailable`、`offline`、`starting`、`ready`、`degraded` 或 `version_mismatch`。心跳每10秒写入 Redis，以服务器时间和45秒 TTL 判定；API读取最多32个、每个16KiB的记录，超过界限、依赖不可用或证据不完整时拒绝绿色状态，不返回连接信息或内部异常。存活与就绪分别由 `heartbeat_verified`、`runtime_ready` 表达。API与worker必须使用相同完整提交SHA、解析源码、模型清单、核心版本和完整依赖锁；API镜像只携带锁文件文本，不安装OCR依赖。旧版本worker仍存活时会阻止就绪，需排查部署版本。

预检在独立解析 Python 中校验完整安装锁、模型文件哈希并初始化实际 OCR/布局/表格 pipeline，正常每300秒重新检查，失败60秒后重试，子进程上限90秒。预检进行中或失败、证据过期、消费线程/实际broker活动异常都会撤销就绪；没有API请求内的OCR推理或隐式模型下载。关闭会回收预检子进程和自有心跳；强制杀进程后由TTL失效。

`queue` 从权威文档与 Outbox 表聚合 `waiting`、`running`、`retrying`、`expired_leases`、`overdue_running` 和 `failed_last_24h`；提供最老等待/运行年龄，等待达到300秒标记 `backlogged`。`overdue_running` 表示自领取起总历时达到180秒的预警，不等于解析子进程已超时；租约到期另行统计，旧空租约使用与领取相同的五分钟规则。无等待或运行任务时年龄为 null，不能填成已验证的零延迟。`failed_last_24h` 仅统计最近24小时内当前终态失败；`recent_failures` 最多返回10条当前仍失败的事件，包含已授权文档ID、固定错误码、次数与是否终止，并非全量失败历史。内部异常详情不向客户端透传。权限过滤在SQL聚合前执行，响应为 `Cache-Control: no-store`。读取不会重新投递或修改任务。

relay 对失败/过期且未终止的解析任务先原子预留五分钟恢复投递窗口，再联系消息队列；不改写现有领取token、租约或尝试次数。重复扫描会继续处理后续批次，发送失败或消息丢失可在窗口到期后重投，避免worker离线时反复填满队列。

知识库健康条前台每15秒刷新，后台暂停，返回页面后重新读取；读取失败、心跳到期或旧数据会撤下绿色状态及队列数字，展开详情保持。上传仍以配置声明判断能否持久排队，离线时明确等待worker恢复。2026-10-23的本地测试和浏览器fixture不替代真实PostgreSQL/Redis/worker中断恢复验收，当前独立服务条件不足仍保留未验证。

专用 Linux 镜像使用 `backend/Dockerfile.document-parser`，必须传入不可变 API 基础镜像。构建会核对 API 的基础依赖锁，安装独立 `/opt/document-parser` 环境与实际需要的 OpenCV 动态库，并重新安装本次源码的业务包。子进程使用 Python `-I` 隔离模式和显式包路径加载，不把 API 的整个 `site-packages` 加入独立解析环境。模型另以只读目录挂载到 `/models`，实际清单 SHA 由运行配置提供；UID/GID 10001，`/tmp` 使用可写、有容量限额的临时挂载。镜像不包含模型或生产凭据。既有双镜像发布报告不能自动授予 OCR 资格；新增三镜像发布流程如下，真实签名、Linux 探针和独立联合验收仍须实际运行通过。

## 三镜像发布与模型身份

发布工作流现在要求 API、结构分析、OCR 三个不同的不可变镜像，并将批准的模型清单 SHA 纳入 `openvigil.document-release.v1`。原 `openvigil.hybrid-release.v1` 身份保持不变；新文档 bundle 包含它，使历史双镜像证据仍可单独核对。

受保护的 `production-release` 环境需配置以下变量与 secret；缺失时工作流在构建前失败，不降级跳过 OCR：

| 配置                                      | 用途                                                    |
| ----------------------------------------- | ------------------------------------------------------- |
| `WINDOPS_DOCUMENT_INPUTS_URL`             | 经审核的 HTTPS ZIP 下载地址；只含批准模型和合成扫描探针 |
| `WINDOPS_DOCUMENT_INPUTS_SHA256`          | ZIP 原字节 SHA-256                                      |
| `WINDOPS_DOCUMENT_MODEL_MANIFEST_SHA256`  | 模型清单原字节 SHA-256                                  |
| `WINDOPS_DOCUMENT_INPUTS_TOKEN`（secret） | 输入制品只读访问凭据                                    |

ZIP 必须包含 `models/openvigil-model-manifest.json`、清单内的全部模型文件、`fixture/source.pdf` 与 `fixture/expectations.json`。暂存脚本拒绝路径穿越、链接、重复路径、额外文件与超限展开；解包后逐文件核对模型大小/SHA，并核对扫描件 SHA。模型总量上限2GiB；PDF上限50MiB；不在解析期间下载权重。测试输入应使用可公开的合成资料，独立报告将保存解析结果原文。

`expectations.json` 使用 `openvigil.document-smoke.v1`，必须声明实际的 `fixture_sha256`、`source_page_count`、`passage_count`、`table_cell_count`、排序去重后的 `passage_pages` 和非空 `required_text`。这些期望由制品审核冻结，不能在运行后根据输出改写。

流水线对 OCR 镜像执行构建来源证明、Cosign签名、HIGH/CRITICAL零发现扫描、SPDX清单及签名核验；实际探针以 Linux/amd64、UID/GID10001、无网络、只读根、3GiB内存/2CPU和256MiB临时空间执行。探针核对完整解析依赖锁、代码身份、模型、页码、表格及数值复核标志；API解释器不得加载Docling。

部署入口为 `backend/deploy/document-parser`。渲染器要求三镜像和模型SHA，模型PVC/PV名称为 `windops-document-models-<模型清单SHA>`，由运维预先按审核内容创建对应PV；不自动选择默认存储或改写旧模型卷。worker与挂载均只读，独立SA/Secret、单进程单线程、210秒退出宽限，网络仅可访问限定标签的DNS/PostgreSQL/Redis/MinIO。额外NetworkPolicy不能扩大该worker权限。API和relay获得相同解析配置声明；只有OCR worker实际安装数值环境并挂载模型。

独立证据包原有六份报告与 `structural_runtime.json` 继续必需，并增加 `reports/document_runtime.json`。该报告须绑定三镜像、模型SHA、双镜像及文档bundle，包含八项真实场景检查：运行身份、OCR页表格、权限及原件重验、超时重试/队列恢复、原件SHA回读、幂等并发、数值复核标记、审计/SQL/Outbox/索引一致性。每项必须引用有效且未失败/跳过的JUnit原件；还须提供 `artifacts/document/parsed-result.json`，由实际数据库持久值重建并核对来源、代码、版本及数值复核标记。现有业务证据脚本会输出 `document-parsed-result.json`，经独立审核后按上述路径收集。

最后的 `windops_backend.operations.document_release_gate` 会重新执行原11个API门禁、结构门禁、三镜像部署策略、OCR签名/SBOM/扫描及实际离线解析，要求报告处于同一验收时间窗。只有其输出 `document-release-qualification.json` 成功才表示此次三镜像软件发布候选通过；仍不表示现场OCR精度或作业许可。2026-10-23的本地策略/Windows解析测试不能替代这项正式结论。

## 页面来源与人工复核

状态依次为 `pending_parse → parsing → pending → indexed`；失败重试回到 `pending_parse`，耗尽为 `parse_failed`。重放同一原件/版本/范围/解析器不会生成第二个解析事件；换解析器必须使用新的不可变文档 ID。未知提交响应保留文件、文档 ID 和幂等键，阻止换输入，并核验同一提交。

原生路径保留旧v1请求哈希格式，旧客户端省略parser及新客户端显式native可重放同一旧键；Docling是不同输入，不能用原生旧键切换。每次重放仍先核对当前范围权限，撤权后不会返回旧收据或增加重放次数。

每段保留正文偏移、文本 SHA、实际页码与 top-left PDF points 矩形，空白页不重新编号。表格单元格保留实际行列，但页面矩形的精度是整个表格区域 `table_region`；页面区域示意不是原 PDF 的像素预览，也不宣称精确单元格框。原件链接由后端重新校验 SHA 与权限后短时签发。

“对照原件”会按需加载本地 PDF 查看器，下载后再校验原件字节 SHA，并与解析摘录并排显示。数值修正、独立复核人、服务器时间及原件版本保存在新增复核表和追加审计历史中，原文与向量不改写；支持确认、修正、批准、拒绝、撤回及修订冲突核对。页面旋转或裁剪映射无法确认时只定位到页，不猜测高亮。操作与 API 说明见 [原件与数值复核](KNOWLEDGE_REVIEW.md)，实际验证状态仍以执行进度为准。

所有 OCR 段落标记 `requires_numeric_review=true`。索引成功与原件字节一致，只证明来源和数据流；数字、正负号、小数点、单位及表格读序仍需对照原件人工核对。不能由 OCR 自动授予作业许可、填补缺失工程阈值或声明绝对索力。领域精度、真实校准/规程、责任人和现场影子运行待实际资料验证。

## 隔离验收

先准备明确的扫描测试 PDF，再运行受控业务验收；以下参数需指向本仓库 `.artifacts` 中的实际文件。该 harness 创建随机名称、仅 loopback 暴露的四个依赖与任务进程，结束后清理自己创建的服务和测试卷，并保存报告、原始失败、浏览器 trace、数据库/对象/图谱收据。身份、扫描资料、嵌入模型和 release 是声明测试资料，不是生产数据。

```powershell
backend/.venv/Scripts/python.exe backend/scripts/run_business_e2e.py --scenario document --document-python .artifacts/document-parser/runtime/Scripts/python.exe --document-models .artifacts/document-parser/models --document-fixture .artifacts/document-parser/synthetic-scan.pdf
```

Linux 本地构建与无网络/只读/非 root 的真实 OCR 探针：

```powershell
backend/.venv/Scripts/python.exe backend/scripts/verify_document_image.py --api-base <actual local API runtime> --models .artifacts/document-parser/models --fixture .artifacts/document-parser/synthetic-scan.pdf
```

此 Linux 探针专用于本次两页中文测试资料（空白第一页、十五段及九个表格单元格），不是任意文档精度评价。验收结论以不可变报告的 `passed`、`cleanup_passed` 与当前源码身份为准，不能仅凭命令启动或界面出现判断完成。

document 业务场景还要求先观测实际worker就绪，强制停止本任务自有worker并等待心跳过期，再上传、验证离线排队、重启处理，并核对终态失败与受限身份看不到相关队列记录。浏览器和数据库证据须包含全部健康断言，缺项或失败拒绝验收；该场景实现不代表已在当前环境运行通过。
