# OpenVigil 项目改进建议

当前实施状态与验证入口见 [CURRENT_STATUS.md](../CURRENT_STATUS.md)；下文保留原始检查快照。

- 检查与保存日期：2026-10-23（北京时间）。
- 检查基线：`HEAD bc88d45080` 与检查时的本地未提交修改；不能仅以 HEAD 代表所检查代码。
- 检查范围：项目规则、执行记录、代码结构、关键实现、CI 与发布配置，以及定向契约测试。
- 文档性质：改进建议，不表示相关改进已经实施，也不替代历史审计或正式验收。

当前最值得投入的是：**补齐发布链、让混塔/OCR 持续接受回归验证、提高 Agent 的实际业务质量。** 项目的权限、异步任务、审计和业务闭环已有较完整基础，建议按下面的优先级推进。

## 改进清单

| 优先级              | 改进点                               | 当前依据与具体建议                                                                                                                                                                                                                                                                                                                                                        |
| ------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **P0：发布前**      | **完成 CI 修复的交付闭环**           | 执行记录仍为“本地修复已验证、CARE runner 未配置、远端待复验”。另外，真实跨层 CI 仍使用未冻结的 `pip install`，其他任务使用冻结锁。应统一依赖安装方式，配置 CARE 执行条件，并取得同一提交的全部 required checks 成功证据。[执行记录](../EXECUTION_PROGRESS.md)、[CI 配置](../.github/workflows/ci.yml)                                                                     |
| **P0：启用 OCR 前** | **将文档解析纳入正式发布体系**       | 当前发布工作流覆盖 API、结构分析两类镜像；独立 OCR worker 尚未纳入完整发布资格。需要补齐解析镜像的签名、漏洞扫描、SBOM、模型清单、部署策略和同版本联合验收。[解析部署说明](../backend/DOCUMENT_PARSER.md)、[发布工作流](../.github/workflows/release.yml)                                                                                                                 |
| **P1**              | **把混塔与 OCR 专项接入 CI**         | 已有结构、模态、权限恢复、文档解析等真实服务测试，但默认 Playwright 排除它们，CI 业务入口只运行默认运维场景。应建立分层测试矩阵，并让启动脚本转发场景参数，避免新增能力仅依赖一次本地验收。[测试配置](../playwright.config.ts)、[启动脚本](../scripts/run-business-e2e.mjs)、[场景运行器](../backend/scripts/run_business_e2e.py)                                         |
| **P1**              | **补齐异步任务的运行健康状态**       | OCR 能力接口只判断配置，`worker_health_verified` 固定为 `false`。建议增加 worker 心跳、队列最老任务年龄、执行超时及失败原因，并在界面区分“已配置、可处理、积压、故障”。[能力接口](../backend/src/windops_backend/api/knowledge.py)                                                                                                                                        |
| **P1**              | **从模型单点评测走向完整工作流评测** | 已有真实供应商评测，但内置六个合成案例使用独立评测提示词，不能代表生产工作流质量。应增加脱敏、专家标注案例，沿实际检索 → 诊断 → 方案 → 审核链路评价引用正确性、拒答、越权建议、耗时和成本。[评测边界](../README-CN.md#真实模型回归评测)、[评测实现](../backend/src/windops_backend/operations/reasoning_eval.py)                                                          |
| **P1**              | **改善证据复核体验**                 | 当前引用区域是页面示意图，OCR 数值复核主要依靠提示。可增加原 PDF 与解析结果并排查看、定位高亮，以及数值修正、复核人、时间和原件版本的审计记录，让人工复核形成可追踪结果。[来源查看器](../components/pages/knowledge-source-viewer.tsx)                                                                                                                                    |
| **P2**              | **验证规模与并发性能**               | 已有性能测量脚本，但主要是本机、无网络限速、并发为 1 的实验。下一步应覆盖多用户查询、长时间序列、批量解析和队列积压，测量 P95/P99、错误率与资源消耗，再决定优化位置。[性能测量](../scripts/measure-performance.mjs)                                                                                                                                                       |
| **P2**              | **继续做有边界的模块拆分**           | 当前 CARE 全量评估模块约 2745 行，应用外壳约 1150 行，知识库页面约 992 行。可分别拆分评估计算与证据校验、导航与搜索通知、上传流程与问答展示；保留已有 API、权限和幂等行为。[CARE 评估](../backend/src/windops_backend/benchmarks/care/fullscale_evaluation.py)、[应用外壳](../components/layout/app-shell.tsx)、[知识库页面](../components/pages/knowledge-base-page.tsx) |

## 当前状态入口

另一个成本较低的改进是**整理当前状态入口**：[旧审计报告](../AUDIT_REPORT.md)仍指向 9 月活动区，而实际进度顶部已经是 10 月 CI 修复。建议保留历史证据，单独维护简短的“当前版本、未完成项、阻塞条件、验证入口”索引。

上述 CI 状态来自检查时的仓库执行记录，本次没有重新查询远端，不能将历史记录直接视为远端当前状态。既有本地软件通过、真实现场未验证和正式发布资格应分别记录。

## 本次验证与边界

本次分析未修改业务代码；架构一致性检查和 **10 项前端契约、51 项后端发布及模块边界测试通过**。

执行命令：

```text
node scripts/check-architecture-doc.mjs
node --experimental-strip-types --test tests/architecture-doc.test.mjs tests/markdown-local-links.test.mjs tests/import-boundaries.test.mjs
```

后端在 `backend` 目录，使用项目内已有的冻结验证环境执行：

```powershell
New-Item -ItemType Directory -Path '../.artifacts/project-review-20261023' -Force | Out-Null
& '../.artifacts/ci-repair-20261008/backend-verified-env/Scripts/python.exe' -m pytest tests/test_import_boundaries.py tests/test_release_pipeline.py -o addopts= -q -p no:cacheprovider --basetemp '../.artifacts/project-review-20261023/pytest-tmp-recheck' --junitxml '../.artifacts/project-review-20261023/contracts-recheck.xml'
```

- 架构检查输出：`Architecture facts match source.`
- 前端契约结果：10 passed，0 failed，0 skipped。
- 后端首轮因本次临时目录父目录未创建，7 项测试在 fixture 设置阶段报错；这不是业务代码失败。修正目录设置后复跑，结果为 51 passed。
- 首轮报告保留于本地文件 `.artifacts/project-review-20261023/contracts.xml`，复跑报告为 `.artifacts/project-review-20261023/contracts-recheck.xml`。
- 本次属于源码检查和定向验证，未重新验证远端 CI、完整业务运行或生产环境，不能据此宣称全量回归或生产验收通过。
- 本文记录的是 2026-10-23 检查快照；后续实施前应重新核对工作树、配置和相关外部条件。
