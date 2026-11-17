# OpenVigil 项目改进验收（2026-11-11）

整体状态 **BLOCKED_EXTERNAL_ACCEPTANCE**。本地候选已完成当前可执行验证，剩余验收等待外部条件。最新完整后端轮次使用候选 v17 `f1a250646f36baed23abc89ab82bd2fa441307c8`。11 月 11 日 09:19 查询时，GitHub `main` 为 `68092168dc49db2c2fbeeb6047962310fe64e424`，其最新 CI `41029126119` 已失败结束。验收标准沿用 [项目改进矩阵](docs/improvement-execution-2026-10-23.md)，分轮记录见 [执行进度](EXECUTION_PROGRESS.md)。

| 验收项                | 当前本地证据                                                                                                | 待验收                                                                     |
| --------------------- | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| IMP23-01 冻结 CI      | 独立克隆冻结安装与 15 项前置检查通过，发布命令复用安装环境                                                  | 本地候选提交后的全部 required checks，CARE Linux x64 runner 和两处真实制品 |
| IMP23-02 OCR 发布     | 冻结归档实际暂存、三镜像离线部署策略和 Windows 解析通过                                                     | 真实 Linux 镜像、签名、SBOM、漏洞扫描及同版本联合报告                      |
| IMP23-03 真实业务矩阵 | 五场景汇总负向回归通过，PostgreSQL 专项收集 35 项                                                           | 当前候选五个真实服务及浏览器场景、PostgreSQL 专项和远端汇总                |
| IMP23-04 异步健康     | 当前 Windows worker 启动预检和 89 项健康回归通过                                                            | 真实 Redis 心跳、自然过期、worker 停机及队列恢复                           |
| IMP23-05 工作流评测   | 两个软件案例到达人工审核，独立合成标签评分 1/2。未获付费授权时 CLI 退出 2                                   | 脱敏专家标注案例、有限调用预算、真实供应商与 PostgreSQL                    |
| IMP23-06 原件复核     | 15 个实际 OCR 段落定位、SQLite/API 回归及浏览器 fixture 分别核对                                            | 同一真实 PostgreSQL、MinIO、OCR 和浏览器的复核闭环                         |
| IMP23-07 并发性能     | 原 9000 行软件档 204 次请求成功。机组查询 P95/P99 为 990.40/1043.08 ms，超出参考值，保持 `measurement_only` | 四个真实多服务负载、延迟归因与容量证据                                     |
| IMP23-08 模块拆分     | CARE 及导入边界 13 项通过。已验证快照的前端源码及锁与当前一致，311 项 Node 测试与 96 项 Chromium 回归通过   | 真实 CARE 全规模与业务服务等价                                             |
| IMP23-09 状态入口     | 九项已逐项核对，历史记录保留，验证入口见执行进度                                                            | 随外部验收结果更新                                                         |

v17 独立克隆通过 15 项前置检查。完整后端回归 2099 项通过、40 项预期外部跳过、零失败，覆盖率门槛通过（全局 78.04%，改动可执行行 88.37%）。核对了全部 2139 个用例身份和 40 项跳过原因，上一轮失败的重定向和结构图用例在本轮通过。原件为 `clean-ci-v17-audit.json`。本机 Docker 服务端及 Desktop 状态查询均超时，CARE 制品变量未设置。

09:15、09:18 和 09:19 的连续复核确认，同提交远端 CI、真实 Linux 发布与隔离服务、CARE runner 和制品、专家案例与有限预算仍有缺口。提交并推送候选及创建草稿 PR 的授权正在等待回复。候选补丁和 PR 说明保存在 `.artifacts/improvements-20261111/`，逐次证据见 `external-acceptance-audits.jsonl`。

---

# 历史代码结构重构（2026-10-29）

状态：**COMPLETE_LOCAL_SOFTWARE_VERIFIED**。任务中心按查询编排、展示视图、共享支持和创建命令拆分；后端推理按schema、输出治理、确定性/真实provider及共享契约拆分，旧入口和行为保持。

- 验证：Node311项（310叶case+1父test）、后端388项、最新构建Chromium96项全部通过，零失败/跳过；构建、原包体/覆盖率门槛、类型、lint与格式检查通过。
- 保护：1084份起点文件原件与摘要已保存，无原文件删除；HEAD/index/根`.env`及旧执行记录保持。未提交、推送、改依赖/数据库或生产部署。
- 当前记录见 [执行进度](EXECUTION_PROGRESS.md) 顶部；计划/报告为仅本地文档：`docs/refactor/REFACTOR_PLAN-2026-10-29.md`、`docs/refactor/REFACTOR_REPORT-2026-10-29.md`。总收据在`.artifacts/refactor-20261029/verification-receipt-v1.json`。
- 下方保留2026-10-24项目改进快照，其外部验收待办不被本轮结构验证覆盖；其中Docker/远端运行等观察仅代表旧记录日期，本轮没有重新确认这些外部状态。

---

# OpenVigil 当前状态

更新日期：2026-10-24（北京时间）。当前活动任务为 2026-10-23 项目改进；本页是简短索引，逐项记录和历史证据以 [执行进度顶部](EXECUTION_PROGRESS.md) 为准。

## 版本与验收边界

- 起点与当前 HEAD：`bc88d45080ef0cb0cbe446695e862755cf8e4edb`。当前包含未提交改动，HEAD 不能代表全部已验证代码；各阶段收据另记录源码摘要。
- 整体状态：**BLOCKED_EXTERNAL_ACCEPTANCE**。本地工程已收口；整体目标仍待外部验收，未标记完成。本地软件、真实依赖联合运行、同提交远端 CI、正式发布及现场资格分别核对。
- 原始范围：[项目改进建议](docs/project-improvement-recommendations-2026-10-23.md)；验收条件：[实施矩阵](docs/improvement-execution-2026-10-23.md)；长期目标：[EXECUTION_GOAL.md](EXECUTION_GOAL.md)。
- 本轮未提交、推送或生产部署，未发起付费模型调用；37 份原始修改备份、HEAD、Git index 和根 `.env` 保持。

## 逐项状态

| 编号                | 本地已完成或正在验证                                             | 剩余验收                                               |
| ------------------- | ---------------------------------------------------------------- | ------------------------------------------------------ |
| IMP23-01 CI         | 冻结安装、同解释器和最新检查结果选择已验证                       | 新提交的全部 required checks；真实 CARE runner 和制品  |
| IMP23-02 OCR 发布   | 三镜像发布门禁、模型身份、策略和 Windows OCR 已验证              | Linux 离线镜像、实际签名/扫描/SBOM、同版本独立联合报告 |
| IMP23-03 持续回归   | 五场景矩阵、参数转发、聚合与失败拒绝已验证                       | 本轮五场景真实服务和新 SHA 远端 CI                     |
| IMP23-04 异步健康   | 心跳、预检、队列范围、恢复和界面已做本地验证                     | 真实 Redis/worker 中断、自然租约过期和服务恢复         |
| IMP23-05 工作流评测 | 实际编排、输入/标签分离和预算已验证；软件样例仅 1/2 符合合成标签 | 专家案例、有限真实模型预算与供应商/数据库联合验收      |
| IMP23-06 证据复核   | 原 PDF 渲染、数值复核、数据库审计及浏览器传输 fixture 已验证     | 本轮 PostgreSQL/MinIO/OCR 真实联合闭环                 |
| IMP23-07 并发性能   | 工具整合510项通过；完整进程树下204次HTTP成功，查询尾延迟仍超参考 | 四个真实多服务负载；生产瓶颈与容量验收                 |
| IMP23-08 模块拆分   | CARE/外壳/知识页等价验证通过；前端308项、92个浏览器用例最新通过  | 真实CARE全规模和业务服务场景                           |
| IMP23-09 状态入口   | DONE_LOCAL_VERIFIED：当前/历史入口、精确文档清单及候选检查完成   | 无本地工程待办                                         |

## 外部待办

1. 用户已确认 CARE Linux x64 runner、`WINDOPS_CARE_REAL_ARTIFACT_ROOT` 和 `WINDOPS_CARE_FULL_SCALE_ROOT` 尚未准备，保留待办。
2. 用户已确认脱敏专家标注案例和有限真实模型调用预算尚未准备，保留待办；不以合成标签替代专家验收。
3. 本轮 Docker 引擎启动失败，真实 PostgreSQL/Redis/MinIO/Neo4j/OCR 联合场景尚未执行；没有重置 Docker 或改动系统配置。
4. 本轮尚未产生推送后的新 SHA，也未取得真实签名发布或现场数据证据。2026-10-23 查询的远端旧运行 `37715679784` 总体失败，仅代表该时点观察。

## 使用与证据入口

- [OCR 部署与发布](backend/DOCUMENT_PARSER.md)、[工作流评测](backend/WORKFLOW_EVALUATION.md)、[证据复核](backend/KNOWLEDGE_REVIEW.md)、[并发性能评估](backend/PERFORMANCE_EVALUATION.md)。
- 本机阶段原件位于 `.artifacts/improvements-20261023/`，不纳入仓库或发布材料；失败报告保留，最新重验不会覆盖旧结果。
- 三轮旧软件报告只采到Windows Python launcher，保留HTTP结果及原件，但不能证明API资源完整。当前单次实测已覆盖5个自有进程、每个162次观测，实际解释器峰值约225MiB；查询P95/P99=1233/1278ms仍超参考，qualified=false。新旧资源方法不同，不作优化同比。
- 主要历史阶段收据：`p0-local-phase-receipt.json`、`p1-local-phase-receipt.json`、`p1-health-phase-receipt.json`、`p1-workflow-phase-receipt.json`、`p1-numeric-review-phase-receipt-v2.json`、`p2-performance-software-phase-receipt.json`。
- 最新性能收据：`p2-performance-services-phase-receipt-v1.json`、`software-tree-small-v1.receipt.json`；模块阶段总收据：`p2-module-split-phase-receipt-v1.json`，绑定21份源码/测试和41份实际证据。拆分子收据为`care-split/verification-receipt-v1.json`、`app-shell-verification-v1.json`、`knowledge-split-receipt.json`；统一前端报告为`split-frontend-v2.xml`和`split-browser-v2.xml`/`split-browser-v3-focused.xml`。
- 最终后端整合510项全部通过；Node 308含一个父test（307叶case）；浏览器全套90通过、2个测试定位失败后定向重验2/2通过，共92个唯一用例最新通过。浏览器后端传输为明确fixture，不能替代真实服务。
- 候选收据`improvement-candidate-v1-receipt.json`使用独立index，制品/文档/架构与原37份修改保护通过；这是未提交快照，随后状态文档更新不属于该tree。
- [历史技术审计](AUDIT_REPORT.md)和进度下方历史任务保持原验收时点；不将其 `ACCEPTED` 或 `DONE` 视为当前版本通过。

下一步：外部前提齐备后，按实施矩阵补真实服务、CARE、专家评测、远端CI与发布验收；用户已明确保留的资源和预算待办不重复索取。
