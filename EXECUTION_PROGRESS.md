# 当前活动任务：代码结构重构（2026-10-29）

状态：**COMPLETE_LOCAL_SOFTWARE_VERIFIED**。本轮按职责拆分任务中心与推理模块；计划和报告为仅本地文档：`docs/refactor/REFACTOR_PLAN-2026-10-29.md`、`docs/refactor/REFACTOR_REPORT-2026-10-29.md`。下方既有项目改进保持原外部验收状态。

| 编号    | 工作                     | 状态 | 最终证据                                                                                                            |
| ------- | ------------------------ | ---- | ------------------------------------------------------------------------------------------------------------------- |
| RF29-01 | 起点调查、保护与基线     | DONE | 1084份原件；前端旧9/9；后端153通过/2个既有断言失败，原件保留                                                        |
| RF29-02 | 任务中心职责拆分         | DONE | 1000→522行；17定义/导入绑定等价、116条旧断言保持；相关Node12/12                                                     |
| RF29-03 | 推理模块职责拆分         | DONE | 835→105行；12定义/88全局绑定等价、旧35名称兼容；相关179/179                                                         |
| RF29-04 | 冻结源码整合验证         | DONE | Node311/311（310叶case+1父test）、后端388/388、Chromium96/96；均零失败/跳过；build/原包体/覆盖率/全仓类型与lint通过 |
| RF29-05 | 独立复核、保护核对与报告 | DONE | 双向独立复核无发现；原文件无删除、HEAD/index/.env及历史后缀保持；测试PID20156退出、4179端口释放                     |

- 结构：页面保留查询/状态/筛选/组合，视图/支持/创建抽屉各归owner；推理旧入口保留兼容导出与工厂，schema/校验/两种provider分开。没有新增依赖或改变业务行为。
- 静态与预算：255源严格mypy、全量Ruff、488文件格式；TypeScript/ESLint/Prettier通过。覆盖率89.43%/52.63%/33.35%达到原89/49/32，原应用2764141≤2764800字节。架构事实、当前index制品策略和diff检查通过。
- 失败处理：基线2个审核用例按已有知识上下文契约加强断言，保留其余evidence/完整快照/SHA/存储原件严格检查；前端首轮310通过/1文档链接失败，按仅本地文档约定修正后全套311通过。早期等价验证器、unused imports与新浏览器定位失败均已修正且原件保留，不改写成首轮通过。
- 证据：`.artifacts/refactor-20261029/verification-receipt-v1.json`绑定源码与原失败/重验材料。后端扩大回归为26文件388项，浏览器为最新构建单次96项；SQLite/确定性provider及HTTP传输fixture明确，不作为真实模型/CARE、真实依赖、远端CI、生产发布或现场验收。未提交、推送或部署。

---

# 当前活动任务：项目改进（2026-10-23）

状态：**BLOCKED_EXTERNAL_ACCEPTANCE**。本地工程与阶段验证已收口，整体目标未完成；剩余真实服务、CARE、远端CI、专家评测与正式发布取决于外部前提。[简短当前状态](CURRENT_STATUS.md)；任务原文：[项目改进建议](docs/project-improvement-recommendations-2026-10-23.md)；条件：[验收矩阵](docs/improvement-execution-2026-10-23.md)。下方旧任务及其结论均为历史快照，不被本轮状态覆盖。

| 编号 | 优先级 / 范围 | 当前状态 | 下一项验收 |
| --- | --- | --- | --- |
| IMP23-01 | P0 CI 冻结、CARE runner、同提交 required checks | LOCAL_VERIFIED / EXTERNAL_PENDING | 真实 runner、制品和新 SHA 远端成功 |
| IMP23-02 | P0 OCR 独立镜像与正式发布链 | LOCAL_VERIFIED / EXTERNAL_PENDING | Linux镜像与实际签名/CVE/SBOM/部署/同版本联合验收 |
| IMP23-03 | P1 混塔与 OCR 持续回归 | LOCAL_VERIFIED / EXTERNAL_PENDING | 五场景真实依赖联合运行及新 SHA CI |
| IMP23-04 | P1 异步任务健康 | LOCAL_VERIFIED / EXTERNAL_PENDING | 真实Redis/worker中断、自然过期与恢复 |
| IMP23-05 | P1 真实工作流评测 | LOCAL_VERIFIED / EXTERNAL_PENDING | 专家标注案例、有限调用预算及供应商/PG验收 |
| IMP23-06 | P1 原件与数值复核 | LOCAL_VERIFIED / EXTERNAL_PENDING | 真实PG/MinIO/OCR浏览器联合场景 |
| IMP23-07 | P2 规模和并发性能 | LOCAL_TOOL_VERIFIED / MEASUREMENT_ONLY | 四场景真实多服务负载；生产规模和延迟资格未获 |
| IMP23-08 | P2 模块拆分 | LOCAL_SOFTWARE_VERIFIED / EXTERNAL_PENDING | 真实CARE全规模和业务服务场景保持待验收 |
| IMP23-09 | 当前状态入口 | DONE_LOCAL_VERIFIED | 当前/历史入口、候选制品与文档链接已核对 |

当前工程阶段（更新至2026-10-24）：本轮本地工程已收口。最终性能工具/生命周期/边界整合510/510、25源静态检查通过；前端308项Node（307叶case与1父test）、原覆盖率和包体门槛、构建/类型/lint通过；浏览器92个唯一用例最新结果通过（全套90通过与2个新增定位失败，修正测试后2/2定向通过，未将重验相加成94）。CARE、外壳、知识页拆分已验证。修正Windows launcher漏采后的单次SQLite/HTTP实测204/204成功，5个自有进程每个162次资源观测，原机组查询P95/P99参考仍未达标；真实Docker多服务运行未执行。外部条件已连续多个目标轮次确认未就绪，当前无可独立继续的必要工程项；用户已明确保留CARE和专家/预算为外部待办，无需重复索取。下方按阶段积累的“当前/正在”描述保留当时快照，以本段及表格为准。

- 最终性能阶段：`p2-performance-services-phase-receipt-v1.json`绑定510项无失败/错误/跳过测试、25源mypy/Ruff/格式/Bandit零发现、当前源码和保护摘要。`software-tree-small-v1.receipt.json`记录唯一新实测，实际解释器RSS峰值236335104字节、CPU增量43.453125秒；查询P95/P99=1233.29/1278.03ms，历史=973.13/1188.28ms，CLI3、measurement_only、qualified=false。自有树正常退出、5份原进程身份消失、端口释放；未将新资源方法与旧三轮作同条件优化同比，未因超标放宽预算。
- 最终模块拆分：`p2-module-split-phase-receipt-v1.json`绑定21份源码/测试与41份实际证据，确认CARE/外壳/知识页拆分及静态数据去重等价、原包体与覆盖率门槛通过；记录最终构建/TypeScript/ESLint、307个Node叶case及92个浏览器唯一用例最新结果。浏览器完整轮次90通过/2失败与修正后2项定向通过分别保留，不声称一次92项全绿。4179端口与登记的两个测试服务PID均已释放，真实服务和CARE全规模验收继续待外部条件。
- 最终候选与状态入口：`improvement-candidate-v1-receipt.json`的独立index候选通过制品策略、链接和架构检查，1085个blob，37原件备份/HEAD/用户index/.env保持。候选tree是未提交快照；随后仅更新当前状态/执行进度/性能说明，不将后续文档伪称属于该tree。所有原失败和历史阶段收据保留。没有提交、推送、付费模型调用或生产部署。

- IMP23-08 CARE子项 LOCAL_SOFTWARE_VERIFIED：原评估模块2745→1194行，输入、计算、证据各归属独立模块。35个定义AST/签名/旧入口对象身份、261项全局绑定、精确合成数值及旧制品重放一致；69个不同测试最新结果通过、零跳过，5源Ruff/严格mypy通过。共享首轮50/51的失败是services实际入口尚未登记，修复入口登记后边界6/6，旧失败原件保留。收据 `care-split/verification-receipt-v1.json`；原37文件备份及HEAD/index/.env保持。真实Linux runner、真实制品、PostgreSQL全规模与发布仍UNVERIFIED。
- IMP23-08 应用外壳局部验证：1129→447行，导航、侧栏、搜索、通知分离。28项Node、20项AST等价、TypeScript/ESLint/格式/架构检查通过，收据 `app-shell-verification-v1.json`。新增2项浏览器交互测试已登记但尚未运行；待知识页冻结后统一build、包体门禁、完整Node和浏览器验证。
- IMP23-07 services整合首轮：`services-integrated-v1.xml` 476项中473通过、3失败，均在资源采样测试，保留原日志调查整套运行下的进程RSS和采样失败原因；不能用此前18/18专项覆盖此次失败。20源严格mypy复验通过（`services-integrated-mypy-v2.log`），修正ROOT从定义模块显式导入；未运行Docker或真实多服务负载。
- IMP23-07 软件基线证据更正（2026-10-24）：复核三次旧报告发现API登记的是Windows虚拟环境Python launcher PID，RSS恒约5.4MB且CPU增量为0，实际解释器子进程未纳入。旧HTTP延迟/成功数及原报告保留，但旧“资源覆盖完整”仅成立于登记PID，不能证明API总RSS/CPU覆盖或定位瓶颈。正在修复实际执行PID/进程树观测并补回归；不改写旧阶段收据。现有日志未记录SQL/审计阶段耗时，不能断言查询尾延迟主要由审计造成。
- IMP23-08 前端整合首轮：统一build、全仓TypeScript及ESLint通过；`split-bundle-v1.log`显示原应用JS为2766363字节，超过2764800预算1563字节。保持原预算与资源计量，正在寻找等价去重；完整Node与浏览器尚未执行。
- IMP23-07 资源测试定位完成：原测试采样pytest自身，施加有界320MiB诊断分配后实测602–613MB，复现原3项同样失败；改用显式自有stdlib子进程后，相同压力下3/3通过，资源专项19/19。生产资源门禁及512MiB/1GiB预算未改，新增64MiB门槛真实对照验证显式纳入高RSS父进程仍拒绝。收据 `resources-isolation-receipt-v1.json`，原整合/压力失败均保留，下一步完整整合重验。
- IMP23-08 知识页局部验证：970→580行，上传流程231行、问答展示232行。45项AST等价、10份受保护源码/旧测试原SHA保持；46项相关Node（含5项实际上传命令配置/未知结果同键恢复）通过，类型/lint/格式通过。`knowledge-split-receipt.json`区分hook调度/传输fixture与真实命令实现；统一前端和真实业务验收仍待执行。
- IMP23-09 文档入口子项已验证：两任务文档按精确路径开放，其他docs规则保持；八语言README新增历史快照说明和当前入口、性能手册链接；架构登记新模块职责，原生成facts和历史正文保持。4项文档/架构检查及格式通过，`imp23-09-docs-receipt.json`保存13份文件摘要；根 `CURRENT_STATUS.md` 和最终候选全体核对仍在收口。
- IMP23-08 包体修复已验证：仅将 `lib/operations-data.ts` 的10个重复静态字符串提为私有具名常量；129次常量展开AST、8数据导出、269对象共享关系及7函数的2352次输入/返回/引用关系等价。`operations-data-dedup/final-receipt.json`保留dirty原件与逐项证据；build v2原应用2764138≤2764800字节，节约2225字节、余662字节，原门槛/依赖/资源分类保持。最新完整Node 308/308（含父test）、零失败/跳过，覆盖率89.38%/52.60%/33.34%达到原89/49/32门槛（`split-frontend-v2.xml/log`）。
- IMP23-08 浏览器正在执行：首个CLI因PowerShell未引用reporter逗号参数而未启动，`split-browser-v1.log`保留；修正启动后92项完整套件运行中。新增2项测试在桌面用默认可见role查找隐藏移动导航按钮，已通过失败截图/既有CSS定位；改为显式包含hidden的同一enabled水合断言，生产UI未改，待定向复验，不能把本轮浏览器写为通过。
- IMP23-07 最终状态竞态已修复：真实事件循环回归复现末跳取消后已有failure_codes但status仍measurement_only；序列化前按失败账本最终降级，整文件22/22通过，red原件保留（`services-integration-review-receipt-v1.json`）。全backend现冻结，正在顺序整合load工具、新software树和旧公共边界。

- IMP23-07 services组件进展：批量文档HTTP与积压控制`load-documents-backlog-v3.xml` 72/72，六路数据/只读保护`services-dataset-v2.xml` 16/16，SQL只读claim/恢复及共享collector/result身份`services-evidence-v4.xml` 49/49。Windows真实角色与命令整树`load-services-process-command-v7.xml` 21/21；stack集成`load-services-stack-v4.xml` 54/54，其中Docker输出为明确fixture，命令timeout/cancel/orphan使用真实自有子孙socket进程。资源`load-services-resources-v6.xml` 18/18捕获实际Python launcher和解释器子PID内存增长；容器统计为loopback fixture。各组件收据保留旧失败版本，不相加为无重叠总数，也不代表自然300秒租约、Docker或OCR联合实测。
- IMP23-07 总入口局部验证：`root-services-v1.xml` 59/59覆盖签名/输入/私有TLS bootstrap、实际TLS观测HTTP与入口失败/取消/清理保存；`load-services-api-v2.xml` 12/12使用真实ASGI/SQLite与实际services身份映射，验证4主体×6GET、JWT篡改、拒绝域、审计和完整测试头绕过的正反对照。修正服务scope漏mission等权限，保留tenant+farm隔离；外部graph/storage/rate/audit适配器明确fixture。总入口后续改动仍需最终整合重验。
- IMP23-07 独立复核修复：固定Docker本机transport，取消期间完整执行资源清理，命令执行改为内核Job/进程组整树包含；此前真实探针证实单杀父进程会残留孙进程，失败证据`load-stack-review-v1.json`保留。新增资源窗口探针发现首采样等待会使尾部漏采，现等待计入测量总预算；backlog最小180/600秒注册与实际剩余截止分离；完整bypass控制头用于有鉴别力的拒绝检查。正在补超时被下层转换为部分结果后不得启动下一工作负载的回归，尚不宣布services完整通过。

- IMP23-07 services局部工程验证（尚未集成完成）：`load-bootstrap-v1.xml` 79/79覆盖有界GET/新逐请求JWT、四主体两域、隔离配置、TLS材料、固定角色bootstrap和Compose拥有控制；其中Compose使用明确CLI输出fixture，命令时限/取消使用真实本地子进程，不代表Docker运行。API直接以DEVELOPMENT选实际MinIO/Redis分支，worker以TEST选离线确定性embedding；没有验证后改成production或填造镜像digest，私有配置SHA绑定且不继承provider凭据。文档只读SQL模块另有`load-document-evidence-final.xml` 21/21，真实SQLite业务事务/结果hash/权限范围/尝试证据，存储与parser输出明确fixture，索引可重试失败计unfinished。此前软件冻结阶段收据为`p2-performance-software-phase-receipt.json`；局部新测试含旧用例，不与154简单相加。整体与真实多服务/生产规模资格保持未完成。

- IMP23-07 工具局部验证：严格 profile/统计/可比性契约63/63（`load-contracts-v2.xml`），有界进程资源采样21/21（`load-resources-final.xml`）；Windows实际自有进程RSS/CPU采样成功，Linux分支尚未实机验证。真实HTTP排程器与软件生命周期回归仍在收尾，不能据此宣布全项完成。
- IMP23-07 软件工具 LOCAL_VERIFIED：最新真实HTTP排程41/41（`load-runner-v4.xml`）、真实SQLite/API/子进程生命周期19/19（`performance-e2e-final-v4.xml`）及10项架构/公开表面/模型/schema边界通过，连同上述契约和资源共154个独立测试。修复Windows低精度时钟、资源异常丢失账本、cleanup阶段取消可能漏停服，以及有效连接池配置未进入可比身份；正常/资源故障/停服取消三次各204请求真实成功，故障报告明确failed，均exit0释放端口，报告严格回读一致。早期代码同时修改导致源身份门禁失败、tuple/list回读差异及旧报告保留。当前正运行冻结源码下三次software-small测量；真实services四场景（尤其批量OCR/积压）尚需实现及外部运行，不将Docker不可用当作省略工程的理由。
- IMP23-07 冻结软件基线完成（测量资格仍false）：`load-software-baseline-v1.json` 保存三次独立新库/同profile/依赖/有效配置/环境/数据/缓存身份对照。每轮100机组+100历史请求、4预热和另10次权限控制；测量600/600成功、错误率0，机组P95=1045.58/1124.23/1071.02ms，P99=1162.22/1252.14/1157.98ms，持续超过500/1000ms参考；历史P95=918.94/953.34/938.93ms，P99=1016.94/1063.06/1018.85ms。资源实际302/302、312/312、308/308覆盖，三个自有API均正常退出/释放端口，业务表与保护摘要保持。三次CLI均exit3 measurement_only，未声称优化/生产达标。候选独立index/制品/链接/架构通过（`load-candidate-v1-receipt.json`），37原件/HEAD/index/.env保持；未提交推送。后续services实施方案在 `load-services-implementation-plan.md`，尚未实现，真实Linux/PG/Redis/MinIO/OCR与生产规模仍未验收。
- IMP23-07 首次软件测量已留原件：新建SQLite含9000条合成历史，独立Uvicorn/四真实鉴权主体、两授权域与拒绝域；200测量+4预热均成功，业务表不变、读取审计新增、原HEAD/index/.env与源码前后保持，自有服务退出/端口释放。机组列表P95/P99约1063/1109ms，超过预登记500/1000ms参考；历史序列约953/1079ms。报告 `.artifacts/performance-e2e/eeec62b8cbc740de948f82ee8c019050/report.json` 保留 `measurement_only`、qualified=false，不改阈值，不作为生产容量。该早期报告顶层仍用measurement_complete；后续工具改为更明确measurement_only及exit3，并补强异常清理与部分账本保留；最终同源码基线尚未运行。

- 起点保护：已保存 37 个已有修改或新增文件原件，记录 HEAD/index/.env 摘要；`baseline.json` 及 `original/` 位于 `.artifacts/improvements-20261023/`。未修改或复制根 `.env` 内容。
- 当前外部观察：2026-10-23 GitHub API 复核旧运行 `37715679784` 为五项 failure、一项 success、CARE cancelled，非正在排队；`remote-initial.json` 保存状态。本次尚未证明新提交成功或 runner 已配置。Docker 启动实际失败：backend 在创建 `dockerInference` socket 时报告系统无法访问文件，日志确认所有引擎已停；证据 `docker-start-failure.log`。只终止了本任务挂起的 `docker info` 查询，未重置 Docker、改系统配置或处理其他项目资源。Linux 容器验收待恢复运行环境。
- IMP23-01 冻结环境子项 LOCAL_VERIFIED：真实跨层 CI 改为 `uv sync --project backend --frozen --extra test`，把同一环境放入 GITHUB_PATH，迁移/seed/server 使用相同解释器，取消启动时隐式 resync。新增契约并通过 47/47；项目内新建冻结 `ci-env` 实际安装142包、pip check、该环境47/47、唯一迁移头0032与离线迁移SQL通过。证据：`p0-01-frozen-install.log`、`p0-01-frozen-contracts.xml`、`p0-01-migrations.sql`。这不是 Linux 或远端通过，整项保持 IN_PROGRESS。
- 远端刷新：公开 Actions API 确认最新运行仍是 `37715679784` / HEAD `bc88d450...`，总体 failure，详见 `remote-recent-runs.json`。Git HTTPS 查询遇443连接失败，不能冒充同步成功；连接器可读仓库与旧运行。专用 runner/真实制品路径待用户输入，未更改远端。
- 用户已确认（2026-10-23）：Linux x64 CARE runner 及 `WINDOPS_CARE_REAL_ARTIFACT_ROOT`、`WINDOPS_CARE_FULL_SCALE_ROOT` 尚未准备，按要求保留为外部待办。上条“待用户输入”为此前记录；当前无需重复索取，真实 CARE 与同 SHA 全部检查保持 UNVERIFIED，继续独立工程工作。
- 用户另已确认（2026-10-23）：脱敏专家标注案例和有限真实模型调用预算尚未准备，保留为外部待办。无需重复索取，不进行付费模型调用；IMP23-05 的真实供应商、专家样本和现场资格仍为 UNVERIFIED。
- IMP23-06 已开始实施：沿用原件授权与不可变摘录，新增按需本地 PDF 渲染及独立数值复核头/追加历史、五项来源身份绑定、独立复核与同键重放。首批真实 SQLite/API 证据门禁测试6/6（`review-evidence-v1.xml`），验证错误修正被拒绝→确认原值→独立批准四修订、收据/历史回读与篡改拒绝；浏览器观测在该单测中为明确fixture，不能据此声称真实浏览器/PG/MinIO闭环。完整迁移/权限/并发/浏览器验证仍在进行。
- IMP23-06 后端子项 LOCAL_VERIFIED：新增0033迁移、复核API与权威头/追加事件，相关扩大回归177/177（`review-integrated-v1.xml`），包含来源身份漂移、当前范围重验、独立审核、重放/并发、原文恒等、事务回滚、审计哈希和迁移约束；Ruff/格式、6源严格mypy和新增源Bandit零发现通过。已同步迁移头声明与实际业务场景证据脚本，真实PG/MinIO/OCR浏览器联合场景尚未执行。
- IMP23-06 网关子项 LOCAL_VERIFIED：补齐数值复核GET/POST与decision仅POST的生产路径；实际构建worker验证委托主体、方法、含查询参数目标、UTF-8请求体SHA、幂等key/重放响应头，30/30（`review-gateway-v5.log`）。初始路径拒绝及测试在vinext首次请求安装fetch包装器前注入传输导致的失败保留；先初始化再安装fixture后重验，不把传输fixture当作远端后端。
- IMP23-06 前端子项 LOCAL_FIXTURE_VERIFIED：本地PDF.js worker实际渲染合成PDF字节，SHA/页码/真实像素/区域/旋转裁剪降级、取消/超时/到期与800%移动端内部滚动通过；数值复核验证Unicode选区、原文保留、独立审批、未知请求跨组件卸载同键重试及主体隔离。最终默认浏览器90/90、0失败/错误/跳过（`review-browser-v5-default-port.xml`，4.3分钟），全前端295/295且原覆盖门槛通过（lines91.91%、branches52.24%、functions33.10%）；全仓ESLint/TypeScript和本阶段格式检查通过。默认套件仍不含真实外部服务场景。
- IMP23-06 包体和失败追溯：新增PDF依赖使旧全量总预算真实失败，原件保留；按需复核UI/PDF核心/worker分别设50/450/1200KiB上限，manifest动态引用、同源worker与所有静态JS计量门禁32项通过，未知资源仍计原应用。知识统计/属性及通知重复DOM等价去重后，v5原应用2764776≤2764800字节，复核UI36730、核心431097、worker1172645；全部113个JS共4405248字节，不把新增资源隐藏为零成本。Vite worker file URL及卡住时超时不结束均已修复并真实浏览器负向验证；首轮完整套件使用4197遇旧测试硬编码4179，失败/中断原件保留，确认默认端口空闲后重验90/90，未修改视觉基线或弱化断言。
- IMP23-06 交付边界：使用说明 [KNOWLEDGE_REVIEW.md](backend/KNOWLEDGE_REVIEW.md)，本地阶段收据 `p1-numeric-review-phase-receipt-v2.json` 保存具体源码/报告、独立候选清单与原37份修改及HEAD/index/.env保护；v1原件保留，v2仅澄清Node的296项含一个父test，JUnit有295个叶case。SQLite/API、真实PDF渲染的传输fixture和构建网关验证分别成立；已扩展的真实document场景尚未运行，不宣称PG/Redis/MinIO/OCR联合通过。旋转/裁剪未证实的框不绘制，未知命令只保存在当前页面内存；原摘录/检索语料不变，复核不授予作业许可。未付费调用模型、提交、推送或发布。
- IMP23-06 候选验证补强：独立索引首轮遇Windows subprocess默认GBK解码失败；UTF-8重验发现三份新PDF仍被全局忽略。已仅开放三个精确fixture路径并标记binary，其他PDF保持忽略；新增Git选择/属性回归通过，最终全前端296/296且覆盖率仍达原门槛（`review-frontend-v2.xml`）。候选v3的1012个blob通过制品策略/Markdown链接与三PDF字节恒等核对，原index/HEAD/.env及37份备份完整（`review-candidate-v3-receipt.json`）；各旧索引/失败日志保留。4179/4197/4198本任务测试服务监听均已释放（`review-browser-v5-services.json`）。
- IMP23-05 当前推进：发现生产知识检索此前仅传通用摘要，已让有界原段落、来源/版本/内容摘要进入诊断、方案与审核并持久化相同快照。真实 SQLite/PDF API→生产图测试先失败后通过，相关56/56；旧结构环境缺 pyOMA_2/scipy 的5项失败保留，改用既有完整冻结环境复验51/51（`knowledge-context-structural-v2.xml`），未修改依赖或削弱断言。请求前预算预留模块与生产适配器注入回归95/95（`workflow-budget-final.xml`），不代表付费模型调用。正在接通隔离数据库、源数据/答案分离、生产outbox执行和事后评分，未取得专家样本或真实供应商工作流资格。
- IMP23-05 来源与隔离子项 LOCAL_VERIFIED：独立输入/标签文件、原字节与案例双SHA、完整案例集合、来源文件大小/hash、显式范围和无预制结果/向量已接通；真实SQLite验证发现SAVEPOINT提前提交并修复，24/24（`workflow-eval-inputs-v4.xml`）。新数据库helper复现并修复可变目标名、提前清理成功、未创建却声明fresh和URL路由覆盖问题；SQLite实库与PG连接/迁移边界注入合计68/68（`workflow-eval-database-final.xml`），真实PostgreSQL仍UNVERIFIED。
- IMP23-05 生产执行子项：专用子进程冻结输入后，调用实际任务创建/outbox/七节点并保留失败与费用；拒答仍由生产0.5置信度门槛拦截，候选仅用于事后评分。runner真实SQLite+生产LiteLLM适配器传输fixture 10/10（`workflow-runner-v5.xml`），验证三次模型调用使用同一检索正文、失败回滚后费用/节点保留、无审批工单及SQLite不含隐藏响应标记。首轮3例usage fixture形状错误、后续1例仅给2方案被生产精确3方案门禁拒绝均已保留，修正fixture而未弱化门禁。真实CLI软件两例到达人工审核不等于标签质量通过。
- IMP23-05 评分/预算子项：独立score复验来源映射、拒答门槛、方案/五维审核和费用，保留全部分母；联合141/141（`workflow-scoring-v4.xml`），未知费用保持null、embedding按批内逐条完整容量预留、工作截止与清理宽限分离。当前发现安装版LiteLLM会把embedding的max_retries=0回退到默认2，正在修复专用进程的SDK内部重试控制并补真实SDK边界测试，不能宣称供应商预算闭环已完整通过。使用说明为本地文件 `backend/WORKFLOW_EVALUATION.md`；专家样本与付费预算尚未获本轮证据。
- IMP23-05 SDK与最终整合 LOCAL_VERIFIED：专用子进程导入前使用本地SDK元数据并固定默认重试为0，校验实际OpenAI客户端零重试，关闭fallback/Router/Responses分流并在退出恢复覆盖配置。离线真实SDK A/B/A观察2→0→2，chat/embedding最终HTTP方法截断各一次，首次导入在禁用socket连接/DNS的子进程中零网络尝试；SDK/预算61/61（`workflow-eval-transport-final-v2.xml`）。当前18模块整合 **295/295**、0失败/错误/跳过（`workflow-integrated-v3.xml`，120.23秒）。v1的1个新模块未登记、v2的1个新CLI误用旧帮助参数断言均保留；现按两个真实CLI分别检查参数，保持原27个console入口与无孤立模块门禁。
- IMP23-05 最终软件CLI与质量边界：`workflow-software-v3/run.json` 两例真实SQLite/outbox/七节点均停在人工审核、零审批/工单；独立 `score.json` **1/2符合合成标签**，拒答例仍给出诊断，classification/expected_stop失败且CLI exit1。该结果证明评分能揭示问题，不能把50%合成样例符合度称作真实模型准确率；未改变确定性实现或标签来制造全绿。原始供应商错误在outbox前安全转换，实际DB字节中不保存测试私有trace，已计费用量仍保留。专家数据/有限真实调用预算已询问但本轮尚未提供，真实PG/供应商/专家/现场资格保持UNVERIFIED。
- IMP23-05 静态与保护：10源strict mypy、18文件Ruff/格式、架构与10项前端文档/导入检查通过。Bandit原报告保留2项Low（B404/B603），人工核对为当前Python可执行文件+固定模块+独立argv+无shell+超时的子进程通用告警，未关闭规则，不能称零发现。精确源码/报告/评分/37份原件与HEAD/index/.env保护见 `p1-workflow-phase-receipt.json`；前阶段收据和失败原件保持。整体目标仍IN_PROGRESS，下一独立项为IMP23-06，已保存只读源码准备 `imp23-06-source-plan.md`，尚未实现或验收原件复核工作台。
- IMP23-02 发现并复现旧安全锁升级未同步运行身份：`pypdf==6.19.0` 与代码预期6.17.0不一致，新增全12核心包与hash锁一致性测试先失败（`p0-02-lock-red.xml`），同步代码预期后解析契约/进程/结果身份25/25通过（`p0-02-identity-green.xml`）。新隔离解析环境按当前hash锁实际安装87包，原环境和模型保持。
- IMP23-02 真实 Windows OCR 局部验收：新解析环境、既有固定只读模型和两页中文扫描件实际运行6/6通过（`p0-02-actual-ocr.xml`，137.41秒）。验证加密/损坏/空白/超大页面/错误模型拒绝，以及实际OCR生成15段、9表格单元格、正确页码/数值文字、SQLite段落入库/索引与原件API；API对象存储为测试内存适配器，不能声称PostgreSQL/MinIO/容器/现场已验收。
- IMP23-02 镜像策略与离线探针子项：新增独立digest/API父层/UID/命令/隔离环境检查及模型/扫描件/代码/完整锁身份校验。首轮55通过1失败（测试预期异常类型与Pydantic拒绝层不符）保留；修正异常分类后56/56通过（`p0-02-artifact-contracts-v2.xml`），两新增模块strict mypy/Ruff通过。这是策略测试，真实Linux探针尚未执行，正式发布链仍在实现。
- IMP23-02 部署策略子项 LOCAL_VERIFIED：新增三镜像+模型内容身份（保持旧双镜像v1身份）、OCR overlay、独立SA/Secret、单进程队列、3Gi资源限制、版本化只读模型PVC与受限依赖网络；拒绝额外网络授权、错镜像/模型、写模型卷、提权及配置漂移。原策略与新增负向测试90/90、四模块strict mypy通过（首轮类型问题已修正），实际 `kubectl kustomize` → renderer → policy CLI通过28资源/10工作负载。证据 `p0-02-deployment-v1.xml`、`p0-02-rendered-policy-synthetic.json`；使用明确合成digest，仅证明渲染/策略行为，不是实际集群部署或签名发布。
- IMP23-02 发布链代码已接入：OCR exact API父镜像构建、来源证明、Cosign、HIGH/CRITICAL扫描、SPDX、批准模型/探针ZIP、实际离线探针、三镜像部署、独立document_runtime报告与最后document_release_gate。最终门禁重新执行原11个API及结构门禁，不信任已有qualified摘要；独立报告必须绑定三镜像/模型/时间窗，逐项引用未失败/跳过JUnit，并包含持久化解析结果。报告收集脚本增加真实JSONB重建结果导出。尚未推送/实际签名/运行正式发布，整项保持IN_PROGRESS。
- IMP23-02 配置及输入验证：补齐API/relay等所有工作负载必需的解析路径声明，以实际Settings加载验证；更新后的渲染CLI证据为 `p0-02-rendered-policy-synthetic-v2.json`。归档首轮19通过1失败保留，定位Windows ZIP原始文件名规范化差异，补查orig_filename并用真实恶意ZIP字节复测20/20（`p0-02-inputs-v2.xml`）；未放宽路径/哈希/大小门禁。
- IMP23-02 验收脚本硬化：Bandit发现原脚本27条assert在Python -O下会消失；已全部改为明确RuntimeError拒绝，转换时保存27条件AST等价证据（`document-evidence-guard-equivalence.json`），一处随后按lint将not-is等价改为is-not。真实SQLite错误状态在独立-O进程中3/3被拒绝（`p0-02-guards.xml`）；17文件Ruff/格式、11源strict mypy、最终Bandit零发现（`p0-final-bandit.json`）。单脚本mypy首轮缺源码搜索路径的错误已通过显式MYPYPATH=src重验解决，未忽略类型错误。
- IMP23-02 扩大回归首轮349通过1失败（`p0-final-v2.xml`），唯一失败为旧测试硬编码两份扫描；现按预期三镜像更新为精确三份路径集合，并增加文档最终门禁位于结构门禁之后的断言。当前完整相关重验350/350通过、0失败/错误/跳过、exit0（`p0-final-v3.xml`，86.06秒），覆盖文档解析/任务/身份/归档/部署/发布、旧API/结构门禁、模块边界与业务runner。旧失败原件保留；不是全仓库回归或真实容器/远端资格。阶段摘要与源码哈希在 `p0-local-phase-receipt.json`。
- 当前保护核验：37份原件备份哈希全部保持、无原文件缺失，HEAD/index/.env未变；原修改中仅CI/两个相关测试/目标与进度有本任务增量，见 `protection-p0.json`。架构检查与10项前端文档/导入契约通过；原P1/P2范围和外部验收仍待推进。
- 前一轮 10 项前端、51 项后端定向通过保持其检查快照，不算本轮改动通过。
- IMP23-03 五场景 CI 工程已接入：保留 required check `business-cross-layer-e2e`，新增 maintenance / structural / structural-modal / structural-scope / document 独立矩阵，结构额外依赖与隔离 OCR 哈希锁按需安装；固定合成扫描件与模型清单随源码纳入。Node 入口转发全部场景参数和真实退出码，所有场景输出非跳过浏览器 JUnit；聚合要求同一 SHA / workflow / attempt 的五份报告、数据库/对象回读及成功清理，只上传脱敏 verdict。当前配置、聚合负向、扫描件和 runner 相关 124/124 通过（`p1-ci-v3.xml`），尚未执行本轮五类真实服务或远端 CI，整项仍 IN_PROGRESS。
- IMP23-01/03 发布检查重跑缺陷：旧实现接受同名同 SHA 历史任一成功，复现 4 失败 / 1 通过（`p1-latest-red.xml`）。现获取所有分页尝试，拒绝未结束尝试，按有时区的实际开始时间选择最新完成结果，时间缺失或歧义拒绝，不推测 ID 大小或页序；失败、取消、跳过、等待和旧结果均有回归。第一轮合并测试 123 通过 / 1 测试常量拼写错误（`p1-ci-v2.xml`），修正后上述 124/124 全通过；原失败证据保留。
- IMP23-03 候选与入口核验：实际执行 `pnpm test:e2e:business --scenario document --help`，先完整构建再由真实 Python 入口接收参数（`p1-launcher-build-help.log`）；这是入口/构建验证，不是场景运行。7 文件 Ruff/格式、4 脚本 strict mypy、12 项前端契约、Prettier 与架构检查通过。使用独立临时 index 生成候选树/ZIP，950 个 blob 通过制品策略，PDF/模型清单归档字节一致、无 `.env`/scratch；37 份原件、HEAD、用户 index、`.env` 保持，见 `p1-candidate-receipt.json`。未提交或推送。
- IMP23-04 队列观测子项 LOCAL_VERIFIED：能力接口增量返回 SQL 权限过滤后的等待/运行/重试、过期租约、超出解析预算、近24小时终止失败、最老等待/运行年龄及最多10条脱敏失败。租户/文档实体/DataScope 在聚合前过滤，禁止共享缓存；读接口不重投任务。真实 SQLite/API、越权/匿名拒绝、异常脱敏、过期领取恢复及旧知识/模块/公开表面/Schema 回归62/62通过（`p1-health-v2.xml`）；补充跨租户隔离后当前健康模块12/12通过（`p1-health-final.xml`），两新增/改动源 strict mypy、Ruff/格式通过。恢复测试实际执行生产领取/发布逻辑，但解析输出为明确的协议 fixture，不是实际 OCR/PG/Redis 联合验收。
- 当前前端完整测试238/238、0失败/跳过，覆盖率 lines91.53%、branches51.61%、functions32.91%达到原门槛；已构建制品通过包体预算，证据 `p1-frontend-coverage.log`。增量发布/聚合/健康代码Bandit零发现（`p1-bandit.json`），架构与文档链接通过。阶段代码与证据摘要在 `p1-local-phase-receipt.json`，原失败报告保持。
- IMP23-04 恢复投递修复 LOCAL_VERIFIED：复现失败/过期任务每两秒重复投递、最早50条占用扫描窗口的问题（`p1-recovery-red.xml`）；新增事务性五分钟投递预留，仅更新 dispatched_at，不覆盖状态、尝试次数、claim token 或租约。连续 relay 50→1→0、SQLite并发预留无重叠、发送失败后间隔重投和新领取保护验证通过；相关39/39（`p1-recovery-final.xml`），中间时区测试断言失败原件保留。这不代表 PostgreSQL 行锁或真实 Redis 验收。
- IMP23-04 能力 API 与验收边界：API 已接入专用 worker 观测，只有配置齐全、身份匹配、有效心跳和真实运行预检同时成立才允许 worker_health_verified；健康/范围API、停启控制器、证据失败拒绝、旧业务runner相关57/57（`p1-health-root-v1.xml`）。旧空租约按实际领取规则满五分钟才视为到期。真实 document 场景新增自有worker强制停止→租约过期→离线排队→重启解析→终态失败与范围隔离断言；尚未运行依赖服务，不能据脚本实现标记真实场景通过。
- IMP23-04 前端局部验证：健康条已接入配置/就绪/积压/预警/离线/版本不符/过期，后台暂停、前台15秒刷新和读取失败撤下旧健康；失败/未知队列不填零，配置允许时仍可排队。当前构建、包体预算、全仓类型检查及相关ESLint通过，前端测试覆盖率 lines91.64%、branches51.78%、functions32.94%达到原门槛（`p1-health-frontend-coverage.log`）。浏览器 fixture 与最终 worker 回归仍在进行，整项保持 IN_PROGRESS。
- IMP23-04 心跳与就绪软件 LOCAL_VERIFIED：Redis服务器时间/45秒TTL、最多32条有界观测、完整提交/解析源码/模型/核心版本/完整锁比较、实际broker调用与消费者活性、独立预检已接通。复核发现的完整锁身份缺口、缓存迭代误算Redis成功、关闭异常跳过清理三处均修复并负向测试。最终worker相关63/63（`worker-health-v3.xml`）；扩大健康/恢复/任务/隔离/公开契约/发布回归224/224、0失败/错误/跳过（`p1-health-integrated-v1.xml`），Ruff/格式、严格mypy与生产源Bandit零发现（`p1-health-source-bandit.json`）。报告有重叠，不累加为唯一测试数。
- IMP23-04 真实 Windows 预检 LOCAL_VERIFIED：当前三文件源码、固定完整依赖锁与只读模型，实际初始化Docling/OCR/布局/表格pipeline，10.61秒成功；模型前后全量哈希、子进程退出/临时目录清理通过。证据 `worker-health-runtime-preflight-v1.json`。这不含真实Redis Lua或Linux镜像；SQLite租约及Dramatiq传输fixture不冒称真实消息服务。
- IMP23-04 UI 验证 LOCAL_FIXTURE_VERIFIED：最终浏览器13/13（`ocr-health-browser-v4.xml`）、Node专项20/20、全部前端248/248与原覆盖率门禁通过；320/375/414/768/1440布局及八状态预览已实际查看，类型/lint/格式通过，自有4196服务监听已释放。证据 `ocr-health-ui-receipt.json`；首轮命令引用错误和预览测试路径失败分别保留v1/v3日志。浏览器使用明确传输fixture，当前真实API/Redis/worker中断恢复仍待独立服务。
- IMP23-03 证据读取补强：新增真实UTF-16/LE/BE JUnit表明原字节DOCTYPE检查可被多字节编码绕过，3个失败保留于 `p1-health-xml-red.xml`；现仅接受Playwright UTF-8并在解析前检查空字节/声明，原字节摘要保持，当前矩阵/汇总52/52（`p1-health-matrix-final.xml`）。扩大脚本Bandit原件 `p1-health-bandit.json` 保留；通用subprocess和显式测试凭据字面量提示按边界复核，XML声明拒绝已用上述真实编码反例验证，不将全脚本扫描声称零发现。
- IMP23-03 结构范围证据硬化 LOCAL_VERIFIED：14个生产assert改为显式RuntimeError，全部条件与其他AST保持（`structural-scope-guard-equivalence.json`）。真实SQLite在独立普通/-O/-OO解释器中均拒绝错误实体计数，相关11/11（`structural-scope-guards-v2.xml`），严格mypy、Ruff/格式与Bandit B101检查通过；首轮因测试未声明test环境而被Settings拒绝的原报告保留，没有放宽生产配置校验。
- 当前子项：健康阶段源码及证据汇总至 `p1-health-phase-receipt.json` 后，推进真实工作流评测的生产内容连接、输入/标签隔离和预算门禁。CARE 外部待办无需再次索取；Docker/Linux、真实队列故障与正式发布仍未验证，专家案例/有限付费预算、数值复核、性能及模块拆分范围保留。

---

# 历史任务：GitHub Actions 修复（2026-10-08）

状态：**LOCAL_FIX_VERIFIED / CARE_RUNNER_NOT_CONFIGURED / REMOTE_RECHECK_PENDING**。本次起点 `bc88d45080ef0cb0cbe446695e862755cf8e4edb`，工作树干净。远端运行 `37715679784`：真实跨层 E2E 成功，frontend/backend/browser/business/postgres 五项失败，CARE 等待专用 runner；用户确认尚未配置 runner。分支保护读取被 GitHub App 的 administration 权限限制拒绝；仓库发布脚本明确要求七项成功，不把它误称为已读出的分支保护设置。

- 已定位并修复本地文件掩盖的 CI 输入缺口：Git ignore 与制品策略仅开放七份必要运维文档的精确路径，其他 docs 保持本地忽略/禁止提交，大文件与缓存限制不变；目标/进度中的本地制品改为明确的本地文件引用。Markdown 检查同时要求目标在仓库文件清单中，避免 ignored 文件掩盖干净克隆断链。最新前端构建、包体和覆盖率测试 236/236，0 fail/skip；覆盖率 lines 91.55%、branches 51.65%、functions 32.91%，原门槛保持。
- CARE 官方源码、许可证的已冻结 v6 哈希来自 CRLF 表示；官方固定 commit 的 Git blob 为 LF。校验仅统一这两种换行，保留既有协议/制品身份以及 commit/tree/gitlink 门禁；新增 LF/CRLF 与源码篡改回归。CARE/运维/发布契约首轮 57/57 通过。Windows 所有权校验支持路径大小写/分隔符差异，并保留 PID/创建时间/命令/可执行文件与目录边界核对；本机实际 sentinel、JSON 重放、错误身份拒绝、幂等停止、占用端口和兄弟目录拒绝通过，GitHub runner 仍需新运行验证。
- PostgreSQL 五组契约远端已经通过，后续 migration 声明验证因缺少 ignored 发布手册而失败；所需文档已纳入本次候选文件。
- 业务 E2E 远端在 Compose up 阶段失败。registry 探针证明 Docker Hub 的 frozen MinIO 镜像不可拉取，Quay 同版本不可访问。固定官方 minio/mc commit、源码归档 SHA-256、Go/Alpine 基础镜像 digest 的源码构建与真实重验已通过，见下述收据；旧缓存版成功仅保留为历史比较。
- 证据：`.artifacts/ci-repair-20261008/`，原远端业务报告、失败日志和本地回归保留。尚未 commit/push/发布；CARE 真制品、专用 runner、当前提交七项远端成功与正式发布保持 UNVERIFIED。

### 后续验证与安全修复

- COMPLETE_LOCAL_BROWSER_VERIFIED：完整浏览器首轮 66 pass / 1 fail，暴露移动导航在水合前接受点击却不开抽屉；修复初始化完成前的 disabled 状态，增加阻塞 JS 加载的实际回归。专项 4/4、最终完整浏览器 68/68（0 fail/skip），最新前端 236/236 和覆盖率门槛、ESLint/类型/格式均通过，未更改视觉基线或延长断言超时。
- COMPLETE_SOURCE_BUILT_MINIO_BUSINESS_VERIFIED：固定官方 MinIO/mc 源码及归档 SHA、基础镜像 digest 的两镜像已实际构建。首轮 Go 模块下载 EOF 留证，缓存构建重验成功。新 MinIO 的维护业务真实闭环通过；新的冻结 Python 环境下再次运行也通过，5 个 MinIO 对象回读、业务数据库/异步 worker、cleanup、原 .env/index 保护均通过。证据为 `20261008023744-dd5d06` 与 `20261008024606-190a5f` 的 report.json；不是旧 registry 缓存通过。
- COMPLETE_LOCAL_SECURITY_VERIFIED：Bandit 的生产 assert 改为明确的超时选择分支，保留普通/委员会推理的超时与审计行为。pip-audit 初次发现 5 包 14 项漏洞；仅升级 langgraph-sdk 0.4.6、multidict 6.9.1、PyJWT 2.15.1、pypdf 6.19.0、urllib3 2.8.0，并冻结最低安全约束及两个运行容器导出锁。OCR 独立锁仅同步 pypdf 6.19.0，CPU/其他包版本保持；本轮不复述旧 OCR 模型精度验收。独立 `.artifacts/ci-repair-20261008/backend-verified-env` 安装冻结全 extras/groups，原 backend/.venv 未同步或覆盖；该环境 pip-audit 无已知漏洞、Bandit exit0、204 源 strict mypy、382 文件 Ruff/格式和锁导出检查通过。
- COMPLETE_CLEAN_CHECKOUT_VERIFIED：在项目内部的独立 Git 索引与 clean-checkout 验证候选文件，原索引保持；LF checkout 的 Markdown 文件清单检查、唯一迁移头、CARE 等价门禁、57 项发布/运维契约通过，不依赖 ignored docs/制品。最新入口/路由/schema/工单契约 13/13 通过；候选文件纳入索引后暴露 docs 前缀禁令，已使用现有精确路径例外限定七份 CI 必需文档。最终 37 文件候选的制品策略（926 blobs）、架构与 Markdown 核对通过；`candidate-final.json` 与总收据记录最终 tree/源码 SHA，旧轮次保留各自快照。
- 原环境完整后端：996 项收集，951 passed / 5 failed / 0 errors / 40 既有外部门禁 skipped，4254.574 秒，原始 XML/log 保留。两项 CLI closure 失败经独立 `-x` 复现为 PATH 未加入 Scripts，CI 原工作流已有 GITHUB_PATH 设置；冻结新环境补齐 PATH 后三项 closure 通过。另三项是下述未同步的契约，修正复测通过，不将该原全量改写为零失败。额外 branch 覆盖率首轮主动中断后改用 CI 原参数，中断记录保留。CARE runner/真实制品与七项新 SHA 的远端成功仍待外部前提，审批请求仅针对修复分支/PR，尚未据此执行远端写入。
- COMPLETE_LOCKED_RELATED_REGRESSION：冻结新环境及正确 Scripts PATH 的 18 模块最终回归 179/179，0 fail/error/skip，包含 CARE 依赖/参考、运维/发布、broker/本地配置、推理超时与思考开关、JWT/权限、安全、知识解析/段落/异步解析/结果身份/子进程和 WT-023 完整审计工作流；不与旧轮次重叠相加。`locked-regression-final.xml`、`local-verification-receipt.json` 保存范围和源码 SHA。CI 固定 uv 0.12.1 的 `lock --check` 通过；标准 pnpm start 的 Demo 与无效生产配置失败关闭 smoke 通过。源码构建的 MinIO/mc 两服务真实 bucket/version/lifecycle 初始化退出0及新项目清理通过，收据 `minio-init-receipt.json`；没有操作原本地依赖栈。
- COMPLETE_FROZEN_CONTRACT_REGRESSION：全量回归收集到三处既有 Docling 增量未同步的契约，已按真实入口/路由/schema 补齐：登记解析计算与独立 actor 两个进程引用；严格冻结新增 GET parser-capabilities 路由，保留原 95 条路由摘要；严格冻结可选 parser 字段及 native 默认值，投影该单一扩展后保留原 58 个 legacy schema 摘要。冻结新环境 8/8 通过、相关 Ruff/格式通过；原全量中的旧断言失败保持，不能据此改写为一次零失败全量。
- COMPLETE_LOCAL_COVERAGE_BUDGET：原全量全局 76.10% 达到 68% 门槛，但原工单关键模块 86.73% 低于 88%，原失败保留。新增真实数据库/已批准工单测试，证明前序未完成及不合格测量被拒绝、五任务状态不变、零现场证据写入。冻结新环境最终修复模块 16/16（0 fail/error/skip）通过，包含原五个失败 case 和新工单门禁；原全量数据与该轮当前同源码数据累计的覆盖率全局 76.16%、工单模块 88.50%、修改行 100%，全部原预算通过。证据为 `backend-remediation-final.xml`、`backend-final-coverage.json` 与 `backend-coverage-budget-final.log`；累计覆盖率不能冒称一次完整零失败运行。

---

# 文档维护记录：八语言 README 同步（2026-10-08）

状态：**DONE_DOCUMENTATION_VERIFIED**。按当前 HEAD `11847e5`、代码/配置及最新软件验收核对英语、简体中文、德语、西班牙语、法语、日语、韩语、意大利语八份 README；本轮只更新 README 与此进度记录。

- 内容：统一陆上混塔阶段与本地软件/现场/正式发布边界；补齐六份译文缺失的混塔章节；新增独立 Docling PDF/中文 OCR、表格/页面来源、数值人工复核、异步状态/租约/三次尝试、原生 v1 重放兼容与权限重验说明；同步结构队列手动启动、托管启动的结构 worker、解析部署入口、工作区/API/架构/技术栈与新增文档镜像的发布资格要求。源码 AST 确认后端目录 18 工具，纠正旧译文的 11 工具；Demo 保持 16 工具。
- 验证：八语言 32 个二/三级章节、每份 17 个可执行命令/配置块、配置标识符及外部 URL 一致；208 处本地文件/图片/锚点与双向语言导航通过。八份 Prettier、架构事实、仓库制品策略、`git diff --check` 通过；发布流程文档契约测试 46/46、0失败/错误/跳过，唯一迁移头 `0032_structural_workflow` 通过。证据：`.artifacts/readme-update-20261008/documentation-validation.json`、`release-pipeline.xml`。
- 边界：本轮验证仅覆盖文档及相关契约，没有重跑应用构建、业务服务、OCR/模型或现场/正式发布验收；2026-10-07 解析和此前混塔验收保留各自源码快照，不把历史软件验证改写为生产资格。未提交、推送或发布；未修改业务代码、依赖、数据库、凭据、服务与历史收据。

---

# 当前活动任务：文档解析增量（2026-10-07）

**兼容性收尾 COMPLETE_LOCAL_SOFTWARE_VERIFIED（2026-10-07）**：保留原生v1请求哈希，省略/显式native都可用旧键重放；换parser拒绝旧键，原生/Docling均在读取收据或原件前复核当前权限，撤权不增加replay_count。专项13/13（包含在当前八模块80/80中，不重复相加）；208源码strict mypy、相关Ruff通过。最终实际服务20261007104442-70218e与Linuximage-20261007104429-756032重新验证当前源码，204安装源/结果SHA/对象/图/审计、cleanup与.env/index/HEAD/五个参考源SHA全部核对。最终收据：`.artifacts/document-parser-20261007/software-verification-receipt-v2.json`；下述v1收据保留为首轮快照。

状态：**COMPLETE_LOCAL_SOFTWARE_VERIFIED**。按原混塔技术方案在既有平台上补齐独立 PDF/OCR 软件链路。起点 HEAD `6c5721b`，工作树干净；未重做历史首版闭环或重构。任务：本次实施任务（本地文件：`docs/design/document-parser-implementation-2026-10-07.md`）；运行说明：[DOCUMENT_PARSER.md](backend/DOCUMENT_PARSER.md)；验收：本次软件验收（本地文件：`docs/reports/document-parser-software-acceptance-2026-10-07.md`）。

- D07-01：DONE_LOCAL_VERIFIED。已读取规则/目标/当前验收/技术方案/源码与本地 Docling 参考；当前知识段落/入库、结构子进程、结构工作流与真实 FDD 模态闭环五模块42项回归正常 exit0；收集数量与输出/进程退出对应，首轮未请求 JUnit，不冒充已有 XML。`d07-01-receipt.json` 保存范围/退出状态及 `.env`/index/HEAD 保护核对。
- D07-02：DONE_WINDOWS_LINUX_VERIFIED。通用CPU哈希锁/Windows87包、pip check、预取模型来源/许可证/commit与字节清单核对。七执行模块/12核心包在子进程实际观察；actual-runtime-v5.xml六项真实离线转换全通过：两页中文、正负数/单位、15段/9表格单元格、空白第一页及坏PDF/空白/加密/超尺寸/错模型拒绝。Linux当前镜像无网络/只读/UID10001探针通过，204已安装源码逐字节一致；主环境不安装OCR依赖。SQLite/内存对象接口夹具与实际服务分别说明；领域OCR精度UNVERIFIED。
- D07-03：DONE_LOCAL_SOFTWARE_VERIFIED。显式入口、独立actor/relay、租约恢复、三次尝试终态、原件前后重验、解析/段落/索引事件事务发布接通。十项声明协议状态挑战及真实服务终态通过；当前知识/权限/图/解析/哈希八模块77项、运行器/结果/隔离broker21项通过（不同轮次与重叠不累加）。待解析/失败正文不产生向量或#body引用；旧租约不能成功或失败写入。
- D07-04：DONE_LOCAL_SOFTWARE_VERIFIED。知识工作台接通真实状态/页码/区域、配置读取重试与数值复核提示；7项浏览器fixture全通过，236项前端单测、构建/包体/类型/ESLint/Prettier通过。未知响应冻结文件/文档ID/幂等键，503→403→202只一次PUT；真实服务最终保留三次上传授权超时，原键核验后取得真实200/202并完成索引。表格框明确table_region，不伪造精确单元格框。
- D07-05：DONE_CURRENT_SOURCE_LOCAL_VERIFIED。最终真实服务20261007102500-1a5b57的browser1/1、PG/Redis/MinIO/Neo4j/专用worker、两原件SHA、15段/9单元格/1536维向量、JSONB回读结果SHA、持久读审计、三次坏PDF失败及越权拒绝全部核对；最终Linuximage-20261007102459-8e6ac0通过。服务与镜像源码运行期间不变，自有服务/registry/volume/进程清理；208源码strict mypy、26文件Ruff lint/format与架构检查通过。总收据：`.artifacts/document-parser-20261007/software-verification-receipt.json`。
- 失败与修复：原始坐标/CPU索引/HOME/调用参数/浏览器定位与所有服务超时失败保留。Linux发现Windows锁未带平台条件、API site-packages污染独立NumPy、OpenCV缺实际动态库，分别由通用锁、-I显式包加载与专用镜像动态库修复，并重新验证。四旧文件仅统一混合换行，归一化字节与AST一致；最终服务/镜像在格式后的精确源码重跑。旧首版/重构/D07早轮收据保留为原快照，不冒充当前源码。
- 保护：原 `.env`、Git index/HEAD、历史收据与其他项目服务保持；独立临时依赖/模型/证据使用 `.artifacts/document-parser-20261007/`。不提交、推送、发布或上线。真实试点、领域精度和正式发布资格继续 UNVERIFIED。

---

# 历史活动任务：代码重构（2026-10-06）

状态：**COMPLETE_LOCAL_VERIFIED**。授权：用户要求重构当前项目，并选择按现状确定重点；基线 1183102af943fdb922d10eb1c07a344f9b940221，起点工作树干净。计划与完整记录：docs/refactor/REFACTOR_PLAN-2026-10-06.md、docs/refactor/REFACTOR_REPORT-2026-10-06.md；不可覆盖总收据：.artifacts/refactor-20261006/verification-receipt.json。

- R26-01 / R26-02：DONE_LOCAL_VERIFIED。Agent工具协议/目录/参数校验与五类处理器、共享辅助函数拆分，执行主文件1934→201行；旧导出同对象、序列/100条内存历史由一处维护。82/82原声明token等价，233前端单测、完整构建/包体、覆盖门槛（91.52%/51.56%/32.90%）、typecheck/ESLint/Prettier与63/63浏览器通过。
- R26-03：DONE_LOCAL_VERIFIED。图预算/SQL分页/构建/稳定序列化独立，主文件1361→866行；共享Outbox写入、推理schema、知识制品常量与窄配置Protocol解除三组旧SCC。52/52函数/类AST等价，无环/可达性/27 CLI、权限/图/审核/知识/事务/结构FDD及闭环回归完成；196源码strict mypy、Ruff、318 Python文件格式通过。
- R26-04：DONE_LOCAL_VERIFIED。结构页精确路径哈希和实际Docker/子进程/CI/验证入口补齐；架构/0032唯一迁移头一致。模拟browser上传origin配置修复，真实后端仍需显式配置；八语言README Demo工具数纠正为实际16。旧告警替身改用真实Alarm、WT-023后端18工具精确集合同步，原断言保留。
- 后端原始全量：913 passed / 2旧测试failed / 40外部门禁skipped；核心原始轮次787 passed / 相同2旧测试failed / 126 deselected。两轮在修正前已收集旧fixture。当前两个模块14/14复测通过，覆盖并解决这2项；去重915个非外部case有通过证据，不冒充一次零失败全量运行。所有原始XML/失败/trace保留，40外部条件保持UNVERIFIED。
- 失败修正证据：旧SimpleNamespace缺已有Alarm.code，create_mission函数AST与HEAD一致；HEAD已包含query_structural_context而旧断言仅17项，保留精确18项集合。CARE进程PATH复测3/3、模拟上传10/10与完整63/63通过。首轮构建/类型基线、漏页/SCC/可达性、重叠源码的失效模态轮次、PATH/旧fixture/旧目录及harness失败均保留。
- 保护与边界：513源/config最终快照一致；三处import格式、Dramatiq精确类型及两处旧缩进delta明确，后两者整文件AST一致，结构计算版本文件在最终回归期间保持。原.env/HEAD/index意图与历史进度后缀保留，无迁移、依赖升级、付费模型请求、提交、推送或上线。正式联合、镜像/安全发布和现场资格仍UNVERIFIED；以下首版验收属于其原源码快照。

---

# EXECUTION_PROGRESS.md

# Long-running Execution Progress

## 当前活动目标：混塔风电运维智能体平台（2026-10-05）

### 当前验收结论（2026-10-06）

状态：**FIRST_RELEASE_SOFTWARE_LOCAL_VERIFIED**。用户“先实现软件，试点资料后续提供”的首版软件范围已实现并验证；技术方案的现场影子运行、正式发布和条件性 P4 不在此软件结论中宣称通过。

- P0/P1：软件契约/11个参考身份、构件/索束/校准/原件/质量门禁、独立异步 pyOMA2 FDD、直接索力与环境基线完成。
- P2：原生段落与原文、工程证据/独立声明审核、结构 Mission、审批/复测/健康/案例、工作台实际 API 和受权图投影完成。
- P3 软件：当前同源码三场景（索力、30窗模态、范围/依赖故障/队列恢复）真实服务 browser 各1/1，分别2/2/32个队列结果、3/7/34个原件、9/17/18表与持久审计/Neo4j核对。207源码/构建SHA一致，每场声明fixture bundle独立，不冒充正式同镜像evidence_set。
- 最终回归：332/332、0失败/错误/跳过，正常exit0；架构/0032迁移头、12产品源和5harness源strict mypy、21文件Ruff/格式、新增结构Bandit通过，重叠测试不累加。
- 最终镜像：images-20261005192608-c43290 API/结构实际Linux构建/两探针通过；exact API父层、专用命令、线程限制核对。234构建源保持、两个已安装包各191源与当前代码逐字节一致；FDD0.5Hz/flat-line拒绝、非root/只读/无网络、完整131锁中130适用包及pip check通过。
- 真实缺陷修复：Neo4j投影交错读取有界版本/数量/逐行检查；新结构结果JSONB数值等价哈希，保持精度/类型与旧幂等规则；每次分析冻结声明部署及实际执行版本。所有旧失败/trace与不可变阶段收据保留。
- 总收据：p3-final-software-verification-receipt.json（本地文件：`.artifacts/hybrid-tower-20261005/p3-final-software-verification-receipt.json`）；可读记录：软件验收（本地文件：`docs/reports/hybrid-tower-software-acceptance-2026-10-06.md`）；当前语义：软件契约（本地文件：`docs/design/hybrid-tower-software-contract.md`）。
- 保护：原.env/HEAD/Git index、历史进度后缀保持；验证服务/registry/volume/进程已清理，保留本地镜像；无提交/推送/发布/上线或付费模型评测。
- **UNVERIFIED**：真实试点输入/校准/规程/阈值/独立测量/责任人、影子运行和领域精度；正式签名/CVE/独立双镜像联合资格及旧发布阻断保留。Docling/OCR/bbox、SSI/阻尼资格、标定绝对索力和OpenFAST/Kratos按实际前提另行推进。

### 阶段执行记录（以下为当时快照，当前状态以上述验收结论为准）



- P3 最终相关回归：CURRENT_SOURCE_REGRESSION_LOCAL_VERIFIED。provenance-final-regression-v1.xml 332/332、0fail/error/skip，测试进程正常exit0；结构/科学/结果哈希/基线/知识工程审核/图一致性/工作流/配置/API/旧发布/双镜像治理26模块。先前139/1失败与3/3专项保留，没有弱化断言。架构、0032唯一迁移头、12源码严格mypy、21文件Ruff/格式、Bandit/diff、README CN/EN格式通过。Linux最终源码双镜像v5构建中，整体Goal仍IN_PROGRESS；正式与field UNVERIFIED。
- P3 当前源码三场景：CURRENT_SOURCE_THREE_SERVICE_SCENARIOS_VERIFIED，整体IN_PROGRESS。scope 20261005185320-ba2fe6 / force 20261005185707-3f8d5d / modal 20261005190140-6d5db9均完整passed/cleanup/env/index=true，每场browser1/1，207源码/构建同一哈希快照。分别2/2/32个真实队列计算，全部算法/执行代码/声明部署与PG回读数值结果SHA核对；3/7/34个原件SHA，9/17/18表精确计数与实际持久读审计、Neo4j核对。modal真实256秒墙钟窗/30窗24train6holdout/健康/案例闭环通过，305审计、图126/196；force64审计图42/55、scope79审计图30/35。每场release/bundle是独立synthetic fixture，不冒充同一个正式evidence_set。收据p3-current-closures-verification-receipt.json不可覆盖。最终相关回归运行中，最后Linux当前镜像待执行；正式签名/CVE/独立联合与field UNVERIFIED。
- P3 当前版本索力闭环：CURRENT_PROVENANCE_FORCE_SERVICE_LOCAL_VERIFIED。20261005185707-3f8d5d完整passed/cleanup/env/index=true，browser1/1；实际FDD与flat-line共2结果的声明部署/执行代码/算法/PG回读新SHA核对，17表精确计数、7原件SHA、64读审计、Neo4j完整闭环/撤回排除验证。源码与scope v4同一快照，未更改算法/工单门禁。当前30窗modal验证进行中，广回归与Linux当前镜像待完成，Goal IN_PROGRESS，正式/field UNVERIFIED。
- P3 版本归属真实范围/恢复：RUN_ATTRIBUTION_SCOPE_REAL_SOFTWARE_VERIFIED，整体IN_PROGRESS。scope v4 20261005185320-ba2fe6完整passed/cleanup/env/index=true，浏览器1/1；2个真实队列FDD结果在PG回读后逐条部署声明/执行代码/库身份与新数值JSON结果SHA全部一致，3个MinIO原件SHA、9表精确计数、79持久读审计、Neo4j30/35以及越权/三依赖故障/停止重启恢复核对。207源码/构建SHA保持；收据p3-provenance-service-verification-receipt.json不可覆盖。前三次真实失败及cleanup保留，已修复投影交错读取和JSONB数字哈希差异；当前force/modal、更广回归与最后Linux镜像仍待验证。正式release/field保持UNVERIFIED。
- P3 每次分析版本归属：VERSION_BINDING_IN_PROGRESS。可信双digest/bundle与执行代码/哈希规则SHA冻结进分析身份，参与去重并核对；错版本零观察终态，legacy明确unbound/null。旧Python序列化结果SHA被真实PG JSONB规范化负零/数字写法改变，已实际复现并实现openvigil.structural-result-json.v1，仅新结构结果使用，保留精度/JSON类型并拒绝NaN/Inf；jsonb-20261005185158-b2ce6a真实PG/FDD回读PASS且自有服务清理。新30项、12源码strictmypy、21文件Ruff/格式/diff通过。首轮service v1/v2因真实Neo4j重建交错读悬空关系失败，通过before/after版本+数量+逐行版本与最多3次重读修复；7一致性/诊断测试与3并发专项通过。v3浏览器1/1故障/worker恢复PASS但SQL结果SHA校验失败，整体FAILED/cleanup/env/index=true，所有失败保留。旧广回归139pass/1barrier超时未忽略，待独立完整重跑；格式失败修复有AST一致证据。当前service v4重新验证全链路，最终Linux镜像/完整当前源码闭环仍待完成。整体IN_PROGRESS；正式联合/现场UNVERIFIED，无提交/推送/上线。

- P3 软件服务挑战首场景：FIRST_REAL_SERVICE_SCENARIO_LOCAL_VERIFIED，整体目标仍 IN_PROGRESS。第 10 次 `.artifacts/business-e2e/20261005154715-8e6f84` 浏览器 1/1、0 fail/error/skip，真实独立结构队列 FDD 与 flat-line 拒绝、TLS API/通用 worker/relay/读审计、实际上传/索力复测/独立审核/工单/案例/Neo4j 工作台/原件篡改拒绝和恢复/撤回清空已贯通。SQL 精确 17 表计数验证（1 completed Mission、1 completed order、2 tasks）；MinIO 7 原件 SHA 回读、2 analyses 的适配代码/数值版本及处理事件、66 条持久读审计、Neo4j 42 nodes / 55 relationships（1 复测测量与 1 闭环复核、撤回声明排除）核对。195 个源码/构建文件哈希运行期间不变，原 .env / index 未改、隔离服务已清理。第 1–9 次失败及各自 cleanup 证据保留；250 ms 审计接纳超时仍 503 无业务数据，严格有界恢复后要求真实终态，本次记录 1 次拒绝。实际图谱 GET 读取预算 15 s（后端证明 10 s）、水合前控件禁用及刷新期间旧结论清空修复，浏览器回归 2/2、前端 30 项、broker/隔离 4 项、构建/包体/类型/ESLint/mypy/Ruff 通过。收据 `p3-first-service-verification-receipt.json`。30-window 实际服务模态闭环、构件/测点/混合通道范围与服务故障恢复、结构镜像/多镜像发布仍待完成；合成信号/身份/release 和确定性推理不构成现场或正式发布资格，无提交/推送/上线。
- P3 实际服务模态场景：MODAL_REAL_SERVICE_SOFTWARE_LOCAL_VERIFIED（北京时间 2026-10-06），整体目标仍 IN_PROGRESS。第 2 次 .artifacts/business-e2e/20261005160439-030f0f 浏览器 1/1、0 fail/error/skip，真实 30-window 基线与 2 个独立分析窗（32 队列 FDD 全部 succeeded、attempts/event=1、适配/数值版本一致）通过。实际发布 24 train / 6 holdout 基线、漂移复核/独立审核/浏览器审批，先过早交接 422，再按未修改服务器墙钟等待完整 256 秒窗末；复测/独立健康审核/独立案例批准完成。精确 18 表 SQL（1 completed Mission/order、2 tasks、1 baseline）、34 MinIO 原件 SHA、实际审计和 Neo4j 基线验证/闭环投影核对。窗口 2026-10-05T16:09:55.688Z–16:14:11.688Z，维持损伤概率 null、未知不确定性、现场资格 unverified 与无作业授权。197 源码/构建哈希运行中不变；.env/index 未改、隔离服务已清理。第 1 次错误上传路由的失败/cleanup/trace 保留，网关门禁未扩大。类型/ESLint/Ruff/格式/严格 mypy 与 runner/broker 4/4 通过；收据 p3-modal-service-verification-receipt.json。首个索力场景收据保持历史快照；下一步授权粒度/服务故障及结构镜像/多镜像，旧正式发布与现场仍未验证。
- P3 结构镜像：LOCAL_IMAGES_AND_LINUX_FDD_VERIFIED（2026-10-06），整体 IN_PROGRESS。第三轮 images-20261005170745-ad9251 当前 API 与 exact API RepoDigest 派生结构镜像完成，实际 Linux/amd64 非 root UID/GID 10001、无网络/只读/tmpfs/全部 capability drop 数值探针 FDD 0.5 Hz、flat-line 拒绝、阻尼空值/不确定性未知/绝对索力 unavailable 通过。完整 131 个固定锁条目、Linux 适用的 130 包版本和 pip check 核对；pyOMA_2 1.4.3 / numpy 2.5.2 / scipy 1.18.1 与适配 SHA 一致，不包含 pytest，专用单进程单线程 structural-analysis 命令核对。全部构建源 SHA 运行中不变，.env/index 未改、自有 loopback registry/volume 清理通过。收据 p3-local-images-verification-receipt.json；两个失败构建/索引与哈希下载诊断保留。revision 明确 worktree-sha256 / local-only，未获取签名/CVE/独立多镜像或现场资格，没有提交/推送/上线。
- P3 实体范围与故障挑战：SCOPE_RECOVERY_REAL_SERVICE_SOFTWARE_LOCAL_VERIFIED，整体 IN_PROGRESS。第 6 次 20261005173840-ceafac 浏览器 1/1、0 fail/error/skip，实际构件/测点与混合原件 22 次越权拒绝、窄范围 SQL/API/UI/Neo4j 一致。暂停 MinIO 10.017 s / Neo4j 网关 15.033 s / Redis 审计 0.291 s 均 503 无业务数据，恢复后来源修订/声明身份不变；停止结构 worker 时 pending 与同键同 run 重放保持，重启后实际 FDD0.5Hz、attempts/event=1 与投影成功。9 表精确计数（2 components/3 sensors/3 records/2 runs/2 modal/1 Mission/1 claim+review/0 order）、3 原件 SHA 回读、81 持久读审计、Neo4j30/35核对。204 源码/构建哈希运行中不变，.env/index/cleanup=true，收据 p3-scope-recovery-service-verification-receipt.json。第1–5次失败/cleanup保留；v5 Windows Dramatiq CLI重启WinError6，v6本地测试用实际单进程单线程Worker API/生产actor，Linux镜像专用CLI不改。核心24/24、旧权限21/21、锁16/16、harness9/9、类型/ESLint/Ruff通过；新bootstrap上游无类型边界的严格类型检查待完成，多镜像治理待实现，不构成正式发布或现场验收。
- P3 双镜像发布软件：HYBRID_GOVERNANCE_LOCAL_VERIFIED，整体 IN_PROGRESS。exact API父层/UID/queue/完整锁/Linux FDD容器校验、双digest/bundle增量overlay、独立服务账号/配置/资源/网络和额外策略扩大访问拒绝已接通。CI保留完整11项API门禁，新增结构独立provenance/签名/CVE/SBOM/探针/双镜像清单和最后联合资格；受保护独立archive必须附同bundle结构报告，核对原件SHA/非跳过JUnit/时窗、fresh Cosign+SPDX内容身份。扩大102、完整治理165、新增43与安全delta35各自0fail/error/skip（重叠不累加）；8源码strictmypy、Ruff/格式、真实kubectl+策略CLI通过。CARE CI旧0030改读取当前0032常量，actual DB head比较保留，两轮失败留证。Bandit新assert改显式错误，受限pod /tmp精确说明，v2零finding；相关35项复测通过。第四轮images-20261005180427-230e80实际API/结构镜像与打包后Linux探针PASS，231源码构建期保持、两探针结果完全一致、.env/index/owned registry-volume cleanup=true；镜像是worktree未签名历史快照，之后仅deployment-policy安全helper修正有源差异，收据记录旧/新SHA，不伪称当前同镜像或正式资格。收据p3-hybrid-governance-verification-receipt.json。下一步每个AnalysisRun/result绑定声明部署与实际计算版本并复验，正式发布与现场仍UNVERIFIED，无提交/推送/上线。
- P2 结构图谱与声明保护：STRUCTURAL_GRAPH_LOCAL_VERIFIED，整体目标仍 IN_PROGRESS。增加构件/索束/测点、原始记录/分析/观察、基线、工程声明、初次及追加复测/独立健康复核的业务摘要与关系，不投影波形样本。声明独立标识为 reviewed_engineering_statement，保留修订/冻结 SHA/适用域/用途与 authorizesWork=false；待审/拒绝/撤回/过期/漂移均不可消费。图读取核对当前 SQL/来源权限、原件 SHA/MIME，并在对象 I/O 后以新身份映射重新读取当前声明与来源；共享原连接且不提交/回滚调用方事务。读取限时 10 秒，基础设施失败返回 503，不返回部分证明或沿用缓存；内部未验证读取不能消费声明。结构业务事件同事务生成图 Outbox，重放/回滚保护、现有顺序 fencing 与资源预算保留；重试耗尽也发布终态。
- P2 图谱验证：最新 graph-regression-v2.xml 49/49、legacy-policy-v2.xml 11/11、postgres-graph-v2.xml 4/4、graph-modal-closure-v1.xml 1/1、graph-browser-v1.xml 1/1、graph-frontend.xml 34/34，全部 0 fail/error/skip，重叠用例不累加。PostgreSQL 验证实际并发撤回/内容/测点变化与调用方事务保持；原件校验仍为内存实现，图存储仍为测试内存实现。模态用例实际 32 次 FDD，图中保留 30 窗基线/域/留出验证与复测关系。发现并修复旧 0007 迁移来源策略的分列/JSON 兼容问题，采用实际名称/类型/启用列补充缺项，保留并验证全部既有 JSON 约束；禁止以宽松默认策略绕过。初始失败日志/XML 保留且临时数据库凭据已脱敏。122 API/旧 95 签名、旧模型/外观契约、固定八节点与唯一迁移头 0032 保持；18 个源码严格 mypy、相关 Ruff/格式、前端类型/构建/包体/ESLint 与架构校验通过。收据为 p2-graph-verification-receipt.json；两个隔离 PostgreSQL 项目均已移除，浏览器进程已回收，无提交/推送/上线。实际 MinIO/Redis/Neo4j、同版本服务联合、结构镜像/多镜像发布安全和现场资格仍待验证。

- 用户授权：按照 `docs/design/hybrid-tower-operations-agent-technical-plan.md` 开发当前平台；用户确认先实现软件，试点资料后续提供。
- 状态：IN_PROGRESS。已读取全部 893 行技术方案、仓库规则、历史目标/审核与活动进度，核对主干代码及本地参考目录；本轮起点仅进度文件有未提交修改，原文件已封存至 `.artifacts/hybrid-tower-20261005/EXECUTION_PROGRESS.baseline.md`。
- P0 软件契约与参考身份：DONE_LOCAL_VERIFIED。`docs/design/hybrid-tower-software-contract.md` 明确已实现/待实现、质量/适用域/不确定性和现场边界；`backend/structural-reference-manifest.json` 冻结 11 个源码下载身份及所选文件 SHA-256。Git 元数据已删除，commit 仅引用此前收据；已安装 pyOMA_2 1.4.3 的 FDD 文件在统一换行后与本地参考一致。
- P1 首批后端：CORE_BACKEND_LOCAL_VERIFIED，整体目标仍 IN_PROGRESS。八个结构业务表、静态迁移 `0029_hybrid_tower_structural` 和 13 条增量 API 已接通权限/读审计/幂等/独立 Outbox 队列。实现原始波形与校准/同步/单位门禁、真实 pyOMA2 FDD、1P/3P 标识、直接 N/kN 测量及按测点/校准/来源分组的本页趋势、健康确认和环境 OLS 基线；保持未量化不确定性、无数据和失败状态，未实现或宣称 SSI/阻尼资格、频率法绝对索力或损伤概率。
- 计算与基线验证：`p1-final.xml` 55/55、0 fail/error/skip，包含真实 30 窗 FDD → 基线发布、UTC、谐波、过期/失配、恢复/fencing/重试耗尽、子进程超时/取消回收与数值回归。排队时保存适配代码 SHA-256 和固定数值库版本并参与去重；worker 版本失配保留 `StructuralAlgorithmVersionMismatch`，不提交观察。新增趋势及角色门禁另以 `prestress-final.xml`（5/5）和 `role-prestress.xml`（2/2）通过，重复用例不累加为唯一数量。
- 权限与兼容回归：`security-final.xml` 84/84、0 fail/error/skip，SAWarning 视为失败；修复实体权限在相关子查询内造成别名冗余笛卡尔积的问题，新增同资产不同构件的别名授权/拒绝验证。结构事件按独立 `structural` 域过滤，`structural-permissions-final.xml` 2/2；原有 95 条 API 签名保留。网关路径/HTTP 方法回归 24/24，TypeScript、修改前端文件 ESLint、12 个新增源码严格 mypy、对应 Ruff lint/格式、冻结依赖/唯一迁移头与架构校验通过。
- PostgreSQL 最新版本：`postgres-identity-final.xml` 4/4、0 fail/error/skip，覆盖空库/历史升级/ORM 对齐、组合外键跨资产拒绝、并发版本唯一性、降回 0028 后旧资产保留及重新升级。测试仅使用新建隔离项目，已停止并移除。最初版本失败 XML 和修正后的结果全部保留在 `.artifacts/hybrid-tower-20261005/`；2 次字段/错误类型失败属于本轮代码问题，未删测试或弱化断言。
- P2 原生段落证据后端：BACKEND_LOCAL_VERIFIED，整体 P2 仍 IN_PROGRESS。新增不可变 `KnowledgePassage`、静态迁移 `0030_knowledge_passages` 和三条权限/读审计保护的段落与源文件读取 API；TXT/Markdown 行号、PDF 实际页码、DOCX 段落/表格单元格、正文偏移和原文/段落哈希独立保存。逐段 embedding 接入现有受控批处理、pgvector 和业务图投影；历史正文保留且不虚构页码，旧 `#body` 链接兼容。修复知识实体授权大小写与全局实体过滤，并保护失去租约的 worker 成功和失败两条写入路径。Docling/OCR/bbox 未实现，扫描 PDF 明确拒绝无文本索引。
- P2 段落验证：最新 `passages-ui-contract.xml` 62/62、0 fail/error/skip，SAWarning 视为失败，覆盖新定位、SHA 漂移、跨租户/文档/实体读取、向量版本、坏文档、审计背压、fencing 和原有知识/图谱/外观契约；`postgres-passages-v2.xml` 5/5、0 fail/error/skip，包含原升级/ORM 对齐、真实 pgvector 段落权限检索、唯一约束、保留旧文档的降级/重新升级。隔离 PostgreSQL 测试项目已停止并移除；最初失败 XML/日志保留（旧链接兼容、测试 HTTP 状态和 nullable 索引时间契约已修正）。当前 111 条 API 中旧 95 条签名及 57 个旧业务模型源码哈希保持；TypeScript/ESLint、12 个相关源码严格 mypy、Ruff 与唯一迁移头通过。
- P2 原文界面：NATIVE_READER_FIXTURE_UI_LOCAL_VERIFIED。生产阅读抽屉通过真实段落 API 展示正文、原生位置、版本与哈希，获取校验后的短时源文件链接；未知页码保留空值。PDF 总页数由服务端解析覆盖客户端元数据；前端 37/37（含网关 26 项）和浏览器 `knowledge-browser.xml` 3/3、0 fail/error/skip，覆盖引用定位、打开原件、失败重试与 ordinal=0 分页。构建/包体/TypeScript/ESLint 通过，截图和 `p2-passages-verification-receipt.json` 已保存。浏览器使用明确的界面测试资料与传输 fixture，浏览器 → 实际 API/MinIO 联合链路仍 UNVERIFIED；没有将它计作现场或完整业务验收。
- P2 工程证据与结论后端：ENGINEERING_CLAIMS_CORE_BACKEND_LOCAL_VERIFIED。新增 `EngineeringClaim` / 独立审核历史、静态迁移 `0031_engineering_claims` 和三条增量 API；服务端从实际模态/分析、直接预应力、基线或知识原文生成 EvidenceCard，保存租户/风场/构件版本、窗口、原始哈希、方法/校准、单位、质量与不确定性。结论冻结支持/反证/缺失/适用域与期限，默认待审；作者不能自审，审核重新校验源字节，采用修订比较与数据库锁/原子条件，过期/内容或来源漂移不能用于推理。缺失或不可用数据只可供明确的复测建议审核，保留缺失项且不授权作业。
- P2 结论验证：最新 `claims-verified-v2.xml` 43/43、0 fail/error/skip（含知识/图谱/权限与旧外观契约回归），`postgres-claims-v2.xml` 6/6、0 fail/error/skip（实际 PostgreSQL 迁移/ORM/pgvector、两名并发审核人只落审一次、跨资产外键、审核修订唯一性、降级保留原文和 Mission）；后者源字节验证器为内存实现，不作为实际 MinIO 联合验收。当前 114 条 API 中旧 95 条签名和旧 57 模型哈希保留，模型外观导出的业务表共 68（不代表整个数据库表总数）；前端 38/38、构建/包体/类型/网关 ESLint、四个新增源码严格 mypy、对应 Ruff/格式/唯一迁移头通过。失败报告保留（测试任务未初始化、错误测试配置键、通道失败标签和直接制品字段契约已修正），未跳过或弱化断言。收据为 `p2-claims-verification-receipt.json`，两个隔离 PostgreSQL 项目均已停止并移除。
- P2 结构复测闭环后端：STRUCTURAL_WORKFLOW_CORE_BACKEND_LOCAL_VERIFIED，整体 P2 仍 IN_PROGRESS。静态迁移 `0032_structural_workflow` 新增四个侧表和八条 API；以实际模态/直接索力、可选已确认基线或责任人审核的规程范围冻结结构 Mission，继续复用固定八节点与受控模型/审批/资源门禁。结构来源不制造 SCADA 或默认健康分；未量化概率、费用/能量保持空值。确切工程结论必须独立审核并重新核验原始字节才可审批；失败审核不能被人工批准静默覆盖。陆上复测后保留待健康复核，独立责任人核对实际新增测量、作业后窗口、方法/校准/质量/适用域；异常结果保持未闭环，后续实测通过不可变侧表追加，原现场证据与复核历史保留。
- P2 健康复核与案例：实际复核关单同步更新工单、Mission 和告警，保留原风机健康分/状态及无恢复运行权限。新案例默认待审，独立案例审核前不进入 SQL 检索或图投影。目录 2026.10.1 共 18 工具（旧 17 保留），新增只读结构上下文；目录升级保留操作员停用和自定义治理发布。当前 122 API 的旧 95 签名、57 旧 ORM 模型源码与两个旧模板 SHA 保持；模型外观导出 72 个业务表，不代表全数据库；70 schema 导出项中 63 个模型，五个有依据的契约调整、其余 58 个 JSON schema 哈希冻结。
- P2 当前验证：`structural-workflow-regression-v12.xml` 39/ 39、`structural-legacy-regression-v5.xml` 30/30、`postgres-workflow-v3.xml` 7/7，全为 0 fail/error/skip；用例重叠不累加唯一数量。覆盖新来源/权限/冻结范围/撤权重放、实际复测及异常追加、审核责任人与案例门禁；隔离 PostgreSQL 覆盖迁移/ORM/pgvector、并发健康与案例审核只落审一次、跨资产外键、降级保留原工单/证据后重新升级。PostgreSQL 的源字节验证器为内存实现；所有该阶段隔离测试项目已停止并移除，原开发栈与候选保留。失败 XML/日志保留，已修复结论适用域类型、候选数量、不可变证据唯一约束处理和过时测试契约，没有 skip 或弱化断言。
- P2 结构审核界面：STRUCTURAL_REVIEW_FIXTURE_UI_LOCAL_VERIFIED。`/structural` 连接资产/拓扑/实际观测、冻结 Mission、逐条 EvidenceCards/原文定位、工程结论审核、独立健康复核和案例审核 API；生产 Mission 详情也接入。工单新增“待健康复核”，未知金额/置信度保留“未评估/未量化”。前端 `frontend-workflow-final.xml` 32/32，Chromium `structural-browser-v4.xml` 3/3，均 0 fail/error/skip；后者为明确的 UI 资料与传输 fixture，含错误重试、修订变化和窄屏布局。构建/包体/TypeScript/修改前端 ESLint/Prettier、八个相关源码严格 mypy、76 个修改 Python 的 Ruff lint/格式、唯一迁移头与架构校验通过；图片已检查。收据 `p2-workflow-verification-receipt.json` 保存哈希与验收边界。
- P2 追加复测操作：STRUCTURAL_FOLLOWUP_FIXTURE_UI_LOCAL_VERIFIED。专用表单读取实际已登记观测，按最新异常复核提交；短时 presign → 原文件 PUT → SHA-256 与 Mission 修订绑定的幂等提交，展示不可变追加历史。上传失败不执行交付，响应丢失/截断/成功响应字段不完整时保留原制品、观测、修订和幂等键，冻结切换机组/任务与健康复核直到核验；授权过期可更新尚未使用的上传授权。GET 工单响应增量返回 handoff 历史，跨资产读取仍拒绝；保留首次任务证据唯一约束。
- P2 模态完整软件闭环：MODAL_RETEST_SYNTHETIC_SOFTWARE_LOCAL_VERIFIED。实际数值进程计算 30 个独立健康窗与异常/复测两个窗口，共 32 个 FDD 记录；确认环境基线、原始异常复核、固定八节点、结论独立审核、受控审批、新窗口交付、独立健康复核、待审案例独立批准全部接通。显式测试时钟只推进采集窗口完成时间，未结束窗口仍被 422 拒绝；方法/校准/MAC/适用域门禁不修改。基线内容与风机原健康分/状态保持，损伤概率缺失且无恢复运行许可。合成信号及内存制品验证器不代表现场或实际 MinIO 验收。
- 本阶段证据：`retest-workflow-api-v2.xml` 9/9、`modal-closure-v1.xml` 1/1、`retest-compatibility.xml` 12/12、`retest-frontend-final.xml` 39/39、`structural-retest-browser-v4.xml` 4/4，全部 0 fail/error/skip，重叠不累加。最新构建/包体/TypeScript/修改前端 ESLint/Prettier、相关三个 Python Ruff lint/格式、增量 API 严格 mypy、唯一迁移头/架构校验通过。最初测试 SHA 期望错误改为独立 Node crypto 字节摘要校验；浏览器 CSP 失败通过既有显式上传来源白名单配置修正，测试未关闭 CSP；测试变量命名遮蔽 document 的类型错误已修正。失败 XML/trace 保留，窄屏图片检查通过。收据 `p2-retest-verification-receipt.json` 保存范围、报告与源码身份。
- P2 结构采集操作界面：STRUCTURAL_ACQUISITION_FIXTURE_UI_LOCAL_VERIFIED。生产工作台接通构件/索束/测点校准版本建档、原始波形及直接索力登记、独立后台分析和健康基线发布。原文件短时 presign → PUT → SHA-256 绑定登记，来源分类明确；微秒时间和原字节保留，可选环境值缺失不填零，已提供的真实零保留。分析展示真实排队/运行/失败、质量原因与模态 ID；基线要求 30–256 个独立引用、健康确认责任和所选环境特征，服务端既有 MAC/适用域/校准/留出验证门禁保持。
- P2 采集重放与分页：未知命令结果锁定原输入和资产范围，以同幂等键核验；不同输入不能生成新命令绕过未核验结果。机组/任务、三类拓扑、分析与索力各自提供首页/上页/下页；选择保留同机组已读取的实际 ID，换机组清空关联选择与采集草稿。角色按建档负责人/采集人员/工程复核人限制入口，后台权限继续作为权威。桌面两列表单及 390px 窄屏图片已检查。
- 采集阶段证据：`acquisition-frontend-final.xml` 48/48、`acquisition-browser-v3.xml` 10/10，0 fail/error/skip，重叠不累加。构建/包体/TypeScript/修改前端 ESLint/Prettier、架构/唯一迁移头及 diff 检查通过；没有新增 API/schema/migration 或修改算法门禁。初次 BigInt 字面量与编译目标冲突已改为标准构造；浏览器原生 option 的禁用断言改为直接检查 disabled 属性，首次格式失败已修正，失败报告保留。新收据 `p2-acquisition-verification-receipt.json` 保存当次源码身份；旧阶段收据为历史快照，不冒充当前哈希。
- P2 其余结构软件与 P3：IN_PROGRESS。结构图投影已本地验证；实际 Redis/MinIO/Neo4j 队列联合与同版本浏览器验收进行中。独立结构镜像冻结锁/构建文件及六进程管理已添加，实际镜像构建与多镜像发布治理待验证；后续交接为 `.artifacts/hybrid-tower-20261005/next-structural-software.md`。没有启动历史已停止栈，没有提交/推送/上线。
- 真实 SHM、混塔结构/索束参数、校准、健康基线、授权规程与现场负责人：后续提供，现场与工程资格 UNVERIFIED。研究仿真、自动损伤定位及跨风场泛化不计入第一版已实现能力。
- 旧七项改进目标全文封存至 `history/execution/EXECUTION_GOAL-improvements-20260927.md`；其 BLOCKED、正式/现场 UNVERIFIED、Final Audit 0/2 与暂不上线边界继续保留。

## 参考项目 Git 元数据删除：2026-10-05

- 用户范围：删除 `C:\coding\reference` 下的 `.git` 文件夹。
- 当前状态：DONE_GIT_METADATA_REMOVED。已删除 24 个 `.git` 目录，复查剩余 0 个；删除前核实目标绝对路径均位于授权目录内，未发现重解析点或该目录的活动 Git 进程。
- 保留验证：其余 59,858 个文件的路径、大小和修改时间均与删除前一致，源码目录无缺失；此前下载的参考项目现为普通源码目录。前后清单、删除目标及完成收据保存在 `.artifacts/reference-git-cleanup-20261005/`。

## 参考开源项目下载：2026-10-05

- 用户范围：列出混塔技术方案中提到的全部仓库和逐行 clone 命令，检查 `C:\coding\reference`，仅下载缺少项目的远端默认分支。
- 当前状态：DONE_REFERENCE_REPOSITORIES_VERIFIED。目标目录已存在，原有 7 个目录与 1 个 ZIP 保留；初次检查时方案中的 11 个仓库均缺少，现已全部补齐并验证。已逐一通过 `git ls-remote --symref <url> HEAD` 核实默认分支，9 个为 main，Kratos/OpenSees 为 master。
- 下载策略：`--depth 1 --single-branch --no-tags --branch <默认分支>`；只下载当前默认分支的浅历史，不初始化子模块、不执行项目安装或代码。
- 已完成并验证 11/11：EnergyFaultDetector、LangGraph、OpenFAST、LightRAG、GraphRAG、Ragas、OpenTelemetry Python、Docling、pyOMA2（main）和 Kratos、OpenSees（master）。本次新建 9 个浅克隆；检测到其他进程已开始下载 Docling/pyOMA2，待其结束后只读复用验证，保留两者默认分支完整历史，没有覆盖目录或历史。Docling 的重复 clone 被 Git 拒绝，随后通过既有目录验证解决，未删除或重建该目录。
- 验证：11 个 origin、默认分支、单分支 fetch 配置、无额外分支/标签、工作树零改动和 Git 对象连通性检查通过；9 个本次浅克隆均只含 1 个提交。来源仓库与 11 行命令一一对应；技术方案 SHA-256 未变，既有进度正文和原目录项目保留。逐项收据位于 `.artifacts/reference-clone-20261005/`，本项仅下载源代码。
- 命令、默认分支及目录初始清单保存在 `.artifacts/reference-clone-20261005/`；完成后核验 origin、当前分支、远端引用、shallow 状态与工作树完整性。

## 混塔运维智能体技术方案：2026-10-05

- 用户范围：了解当前平台现状，搜索 GitHub 项目和论文，借鉴写作智能体参考文档的方案结构，保存独立 Markdown 技术方案；不实施组件集成或恢复历史改进目标。
- 当前状态：DONE_DOCUMENT_VERIFIED（方案文档完成并验证，不表示建设实施或项目验收完成）。已核查当前源码、任务/审核/进度文件，并只读检查现有候选；9 容器运行，其中 TLS 代理因证书过期 unhealthy，底层 API health 200。开发栈 3180/8180 未监听；数据库为 1 篇已索引文档/201 字符、3 案例、6 Mission（3 completed）、3 工单（3 completed）。
- 输出：`docs/design/hybrid-tower-operations-agent-technical-plan.md`，包含 18 个主体章节、10 组核心 GitHub 项目选型及 12 篇论文依据；区分已有实现、新组件候选、研究对照、拟议数据/API、MVP、现场资源和验收门槛。现有 `/docs/` 本地保存策略保持。
- 文档验证：Prettier、33 处本地链接、UTF-8/20 组围栏及原有进度正文保留检查通过，27 个不同外部来源 URL 已列入文档；未进行全部链接的 HTTP 复测，未重跑历史软件或现场验收。收据为 `.artifacts/technical-plan-20261005/document-validation.json`。
- 核查快照与源码哈希保存在 `.artifacts/technical-plan-20261005/`；原有未提交进度内容已逐字节封存，不改写历史验收结果。本项不调用付费模型、不产生业务写入、不修复/启动服务、不提交或推送；七项改进 BLOCKED、正式验收 UNVERIFIED 和暂不上线边界保持。

## 停止本次本地开发栈：2026-10-04

- 用户授权：停止刚部署的本地服务。当前状态：`STOPPED`；`scripts/Stop-Local.ps1` 退出 0。
- 已按仓库路径、PID、创建时间和命令行核验并停止 frontend、API、Dramatiq、outbox relay、read-audit 及其子进程；五个原进程均已退出，管理状态清空。隔离项目 `openvigil-dev-e3beaa2d44` 的依赖容器均停止，3180、8180、25432、26379、29000、29001、27474、27687 八个端口已释放。
- 四个原开发数据卷及容器、凭据保留。既有候选九服务继续运行；本次停止范围为本轮启动的开发栈。验证报告和停止输出位于 `.artifacts/local-stack/stopped-20261004.json`、`stop-20261004.log`。

## 本地开发栈部署：2026-10-04

- 用户授权：部署当前项目到本地。部署完成时状态：`LOCAL_SERVICES_VALIDATED`；随后已按用户指令停止，当前状态见上方停止记录。
- 使用 `scripts/Start-Local.ps1` 重新构建当前工作树并启动保存的隔离项目 `openvigil-dev-e3beaa2d44`；启动及 `scripts/Status-Local.ps1` 均退出 0。工作台为 `http://127.0.0.1:3180/`，登录页为 `/login`，独立 Python API 文档为 `http://127.0.0.1:8180/docs`。
- 五个应用进程（frontend、API、Dramatiq、outbox relay、read-audit）归属核验通过，PostgreSQL、Redis、MinIO、Neo4j 四个依赖健康。当前迁移头、API 鉴权及未鉴权 401、五个 MinIO 桶、Neo4j 连接和新请求的读审计落库验证通过；登录页、API health 和 API 文档 HTTP 200。浏览器已从演示入口进入运营指挥中心，并保留工作台标签页。
- 本隔离栈前端为 `demo`，运行时明确返回 `productionReady=false`、`BACKEND_NOT_CONFIGURED`；独立 Python 为 `development / deterministic / static_tokens`。本次证明本地开发服务可用，不代表生产网关、真实模型或正式发布验收。
- 原四个开发数据卷、根 `.env` 和已有工作树改动保留，既有候选九服务继续运行。部署报告、状态输出和工作台截图位于忽略目录 `.artifacts/local-stack/` 的 `deployment-20261004.json`、`status-20261004.log`、`dashboard-20261004.jpg`；项目改进的 BLOCKED 和正式发布边界继续沿用下方记录。

## 仓库清理：2026-10-04

- 用户授权：按 `docs/reports/repository-clutter-audit-2026-10-04.md` 执行清理和归档。当前状态：DONE_LOCAL_VALIDATED（本地清理完成），操作明细及边界见本地 `docs/reports/repository-cleanup-result-2026-10-04.md`。
- 已完成缓存清理：26 个白名单路径、805,086,215 字节；PPT 工具目录的仓外 junction 仅移除链接，共享运行时保留。此前目录扫描计入了链接目标及重复遍历，实际按不遍历 reparse point 的清单计量。
- 已完成重复数据库处理：三份 Trivy 数据库 SHA-256 相同，保留三个原路径并共享一份 NTFS 硬链接数据，去除 2,915,106,816 字节重复占用；元数据、工具、镜像及扫描报告保持。
- 已完成素材归档：13 组路径、157 个文件已搬迁并逐文件核对大小及 SHA-256；旧登录图、架构图和 PPT 制作/参考资料在本地 `docs/history/repository-cleanup-2026-10-04/`，旧重构任务在 `history/refactor/REFACTOR_PROMPT.md`。
- 进度历史：[阅读版](history/execution/EXECUTION_PROGRESS-through-2026-10-03.md) 保留完整记录，仅将旧 Markdown 尾随双空格改为等价换行；[原始 ZIP](history/execution/EXECUTION_PROGRESS-through-2026-10-03.original.zip) 内的原文件保留 532,215 字节与原 SHA-256。根文件保留当前状态、近期独立完成记录和归档索引。
- 保留与验证：17,704 个受保护文件的大小及 SHA-256 均保持；当前候选九服务及六项 health、全部原容器身份/挂载/端口、Git 实际索引和 CARE 子模块未变。主依赖环境、环境配置、验收证据与 Docker 卷保留；旧构建 393 文件已封存，新构建产物保留供本地启动使用。
- 回归结果：前端定向测试 10/10、后端边界/CARE 供应链测试 8/8、Chromium 登录页测试 5/5，失败和跳过均为 0；生产构建、包体预算、TypeScript、架构及制品门禁通过。相关文档的本地链接及格式、归档 157 文件和原始 ZIP 复核通过。浏览器测试使用测试身份，不代表真实外部提供商或上线验收。
- 本项不改变七项改进 BLOCKED、整体未完成或暂不上线状态，不调用付费模型、不清理容器或卷、不提交或推送。

## README 韩语与意大利语版本：2026-10-03

- 用户授权：新增完整韩语、意大利语 README，沿用现有语言文件结构，并将全部八种语言的导航与相关校验同步更新。
- 当前状态：DONE（文档本地验证）。新增 `README-KO.md`、`README-IT.md` 完整译文，包含翻译后的图示与说明；保留原文的 29 个章节、16 个命令/配置代码块、技术标识符和外部来源 URL。原有六份 README 仅更新语言导航，全部八种语言均可互相切换。
- 防漂移：迁移声明校验及既有发布说明参数化测试已覆盖八份 README，保留测试数量来源、跳过不算发布通过、指定环境跳过即失败的说明。
- 已验证：八份 README 的 Prettier 检查、184 处本地链接/图片/章节锚点、技术内容一致性、唯一迁移头 `0028_read_audit_pipeline`、发布流程测试 46/46（失败/错误/跳过均为 0）、修改 Python 文件的 Ruff lint/格式及 `git diff --check` 均通过。测试报告位于忽略目录 `.artifacts/readme-add-ko-it-20261003.xml`。未运行应用构建、服务启动或现场/生产验收。
- 本项仅涉及文档及一致性校验，既有项目改进和生产/现场验收状态继续沿用原记录。

## README 多语言版本：2026-10-03

- 用户授权：为当前项目增加德语、西班牙语、法语、日语 README，沿用根目录独立语言文件并在六个版本之间提供导航。
- 当前状态：DONE（文档本地验证）。新增 `README-DE.md`、`README-ES.md`、`README-FR.md`、`README-JA.md` 完整译文；中英文原正文保持，六个版本顶部均可切换语言。四份译文保留原文的 29 个章节、16 个命令/配置代码块、技术标识符和外部来源 URL，图示及说明文字已翻译。
- 防漂移：`backend/scripts/verify_migration_head.py` 和既有 `backend/tests/test_release_pipeline.py` 参数化测试覆盖全部六份 README，保留测试数量、跳过不算发布通过、指定环境跳过即失败的说明。
- 已验证：六份 README 的 Prettier 检查、126 处本地链接/图片/章节锚点、技术内容一致性、当前唯一迁移头 `0028_read_audit_pipeline`、发布流程测试 44/44、修改 Python 文件的 Ruff lint/格式及 `git diff --check` 均通过。未运行应用构建、服务启动或现场/生产验收；本项仅修改文档与相关校验范围。
- 验收范围仅为文档及其一致性校验；既有项目改进、生产发布门禁及现场验收状态沿用下方原记录。

## 文档仅保留本地：2026-09-29

- 用户授权：从 GitHub 当前 `main` 版本撤下 `docs/`，保留本地目录与全部原文件；本项不重写历史提交。
- 已实施：取消 12 个文档文件的 Git 跟踪；整目录忽略且无放行规则。制品策略迁至 `scripts/repository-artifact-policy.json`，保留既有大小、缓存路径与来源门禁，增加禁止跟踪 `docs/`；格式命令不依赖本地文档目录。
- 引用处理：根目录中的 33 个本地文档链接改为明确标注的本地资料路径，保留原始报告内容、结论和未完成状态。
- 本地实现与验证：DONE。65 个本地文件的路径、字节数及 SHA-256 与操作前完全一致；不含 `docs/` 的干净导出通过 4 项链接/架构测试、架构检查、制品门禁及完整格式检查。隔离索引负向验证确认 CI 拒绝强制跟踪 `docs/`，未改变实际索引。
- Git 交付边界：本项使用独立提交正常推送 `main`，不重写历史；远端交付结果以推送后 Git 引用核对为准。

## 当前活动运行：2026-09-27 项目改进

- 2026-10-01 当前终态：目标已由工具正式更新为BLOCKED，整体未完成/Final Audit0/2，原七项不缩减。A1/A2及当前A3连续三回合no progress，同一阻塞条件未变：可支持的补丁镜像组件及当前同身份正式联合/真实现场证据缺失；本次blocked审核已验证三次分类/计数/条件和全部七项。实际rc16最终CLI再次退出1、清单/摘要/qualification缺失；rc14正式配置/活动部署0保持，本机九服务与CA验证API health/登录页200。285后端/229前端/393冻结产物、原制品与四卷/角色/八表/main/index/.env保持；官方稳定acl仍vulnerable，原候选0Critical/47High/8CVE及backports20候选零保留，本轮未重扫/查询索引或改代码/装包。前轮相关进程清单为空，本轮25499终态退出0，无待完成核查；没有新增测试、模型、业务写入、激活或上线，累计119/保守0.9274252元保持。缺口需要可用修复版本或正式/现场资源发生变化，重复确认不能代替证据；既有自动确认授权保留。主证据remaining-acceptance-audit-20261001-a3/{remaining-gate-audit-3,blocked-audit-ready,goal-status-update}.json；报告docs/reports/remaining-acceptance-audit-2026-10-01-a3.md。较早阶段见历史归档，不代表当前状态。

详细实施、失败和复核记录见上方完整历史归档；当前清理结果见本地 `docs/reports/repository-cleanup-result-2026-10-04.md`。
