# 当前目标：代码结构重构（2026-10-29）

状态：**COMPLETE_LOCAL_SOFTWARE_VERIFIED**。用户 `/goal`：重构当前项目代码，让结构更清晰。本轮已完成任务中心与后端推理模块的职责拆分，保留现有行为、权限、幂等、审计及兼容入口；311项Node、388项后端及96项浏览器测试通过，构建、类型、lint、格式与原包体/覆盖率门槛通过。

实施计划与报告为仅本地文档：`docs/refactor/REFACTOR_PLAN-2026-10-29.md`、`docs/refactor/REFACTOR_REPORT-2026-10-29.md`。逐项记录见 `EXECUTION_PROGRESS.md` 顶部，总收据位于 `.artifacts/refactor-20261029/verification-receipt-v1.json`。保护全部既有未提交成果及HEAD/index/根`.env`；无新增依赖、数据库迁移、提交、推送、付费调用或生产部署。下方项目改进的外部验收待办保留，不因本轮本地结构验证变为通过。

---

# OpenVigil 项目改进执行目标（2026-10-23）

当前目标为完整执行 [项目改进建议](docs/project-improvement-recommendations-2026-10-23.md) 的八项改进及状态入口整理。完整条件见 [实施与验收矩阵](docs/improvement-execution-2026-10-23.md)，简短索引见 [当前状态](CURRENT_STATUS.md)；唯一活动状态为 `EXECUTION_PROGRESS.md` 顶部。不得以局部测试替代远端 CI、真实场景、现场精度或正式发布资格。

按 P0 → P1 → P2 推进：CI 冻结与 CARE/同提交远端检查；OCR 独立发布；混塔/OCR CI；异步运行健康；真实工作流评测；证据复核；并发性能；有边界的模块拆分。外部依赖未就绪时保留该项，继续可独立完成的后续工程工作，不缩减原目标。每个独立项实现、验证后及时记录证据和剩余缺口。

保护起点 HEAD `bc88d45080ef0cb0cbe446695e862755cf8e4edb`、37 个已有修改/新增文件、Git index、根 `.env`、历史收据和非本任务服务。原文件副本和摘要保存在 `.artifacts/improvements-20261023/`。现有代码与正确行为保持，数据库改动必须有迁移；不自动提交、推送、部署生产或无限调用付费模型。需要外部输入、评测预算或远端写入时，以准备好的具体结果为依据处理。

---

# 历史目标：OpenVigil 混塔风电运维智能体平台

## 当前增量阶段（2026-10-07）

兼容性收尾状态：COMPLETE_LOCAL_SOFTWARE_VERIFIED。原生v1旧键哈希与省略/显式native重放兼容、撤权重放拒绝已验证；当前八模块80项（含13专项）、实际服务与Linux204安装源码重验通过。最终收据为 `.artifacts/document-parser-20261007/software-verification-receipt-v2.json`；首轮验收及历史收据保留其原快照。

本次用户再次 `/goal` 授权继续按原技术方案开发。已有首版与重构落在 HEAD `6c5721b`；本次补齐独立 Docling/OCR、表格/页面来源与受控异步知识入库，状态为 COMPLETE_LOCAL_SOFTWARE_VERIFIED。当前源码实际服务与Linux隔离探针、相关回归及保护/清理核对通过，具体任务与验收见 本次实施任务（本地文件：`docs/design/document-parser-implementation-2026-10-07.md`）、本次软件验收（本地文件：`docs/reports/document-parser-software-acceptance-2026-10-07.md`）。现场精度、条件性科学研究与正式发布仍按实际前提验证，历史收据和发布阻断保留。当前进度以 `EXECUTION_PROGRESS.md` 顶部为准。

开始日期：2026-10-05（北京时间）。当前用户 `/goal` 授权按照本地 `docs/design/hybrid-tower-operations-agent-technical-plan.md` 开发；参考源码在 `C:\coding\reference`，只读使用。用户确认先实现软件，试点资料后续提供。

## 执行范围

在现有双运行时、权威 PostgreSQL、MinIO、Outbox、LangGraph、权限、审批与工单主干上增量实现。第一版面向陆上混塔；公开样本或合成测试不算现场数据；塔筒模态不用于估算未标定索力。

1. P0：冻结软件契约、参考源码身份、验证计划及现场资料缺口。
2. P1：构件、索束、测点、波形制品与可重放输入；独立异步计算、质量/校准/同步门禁、pyOMA2 模态对照、直接预应力测量与环境基线。
3. P2：逐段/逐页知识证据、原文定位、图投影、EvidenceCard、结构 Mission、受控复核/审批/复测工单、健康复核与待审案例；前端真实数据流及状态。
4. P3：软件挑战集、同版本 API/浏览器/数据库/对象存储验证；真实试点与影子运行等待实际资料和现场责任人，明确 UNVERIFIED。
5. P4：OpenFAST/Kratos 与跨风场/视觉研究仅在输入、标定和收益具备时推进，不将所有参考仓库加入第一版依赖。

## 完成与验证

每个独立项先实现、验证，再更新 `EXECUTION_PROGRESS.md`。覆盖跨资产访问、错误单位、坏道、缺失、过期校准、方法失配、幂等、租约/恢复、失败与无数据。算法输出保存输入 SHA-256、算法/配置/校准版本、适用域与不确定性。同步 HTTP 不执行长计算；原安全/发布门禁保持。

临时制品在 `.artifacts/hybrid-tower-20261005/`。保护原工作树、根 `.env`、历史候选服务与数据；不自动提交、推送或上线。付费模型评测单独冻结有限范围后执行，不从软件授权推导无限调用预算。

## 既有项目改进的历史边界

此前七项改进目标全文封存至 [历史执行目标](history/execution/EXECUTION_GOAL-improvements-20260927.md)。其 BLOCKED、Final Audit 0/2、安全门禁失败、正式联合和真实现场 UNVERIFIED 保留，不被新软件开发覆盖；历史状态不阻止本次独立软件实现。


## 本次首版软件验收（2026-10-06）

FIRST_RELEASE_SOFTWARE_LOCAL_VERIFIED：本文件的 P0/P1/P2 软件及 P3 软件挑战完成，最终相关回归332/332、三个当前同源码真实服务场景、最终Linux双镜像及已安装执行源码核对通过。不可变总收据为 `.artifacts/hybrid-tower-20261005/p3-final-software-verification-receipt.json`，当前状态与证据见 `EXECUTION_PROGRESS.md` 及 `docs/reports/hybrid-tower-software-acceptance-2026-10-06.md`。

用户资料后续提供，因此现场影子运行、阈值/校准/领域精度与作业许可保持UNVERIFIED；正式签名/CVE/独立双镜像联合资格及既有历史发布阻断保留。P4、OCR/SSI与标定绝对索力按实际输入和收益推进。本次未提交、推送、发布或上线，未开展付费模型领域评测。
