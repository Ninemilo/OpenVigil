import type { UserGuideChapter } from "./types";

export const walkthrough: UserGuideChapter = {
  slug: "wt-023-walkthrough",
  title: "第一次练习 · WT-023",
  summary:
    "在 Demo 工作台跟踪一条主轴承振动告警，找到它对应的诊断、审批和工单。按实拍截图中的编号操作，图下说明列出输入和预期结果。整套练习可在只读演示中完成。",
  sections: [
    {
      id: "find-alarm",
      title: "从告警找到异常信号",
      steps: [
        {
          text: "先确认工作台顶部显示 Demo，再打开「智能运维 → 告警中心」。没找到目标告警时点「清除筛选」，确认状态和触发时间都为「全部」，再搜一次。",
          screenshots: [
            {
              src: "/guide/wt-023/01-demo-navigation.png",
              width: 670,
              height: 540,
              alt: "工作台顶栏的 Demo 标记和左侧的告警中心入口",
              callouts: ["确认工作台顶栏显示 Demo。", "在左侧「智能运维」分组点「告警中心」。"],
            },
            {
              src: "/guide/wt-023/01-alarm-search.png",
              width: 707,
              height: 401,
              alt: "告警中心的搜索框与 WT-023 主轴承振动告警搜索结果",
              callouts: [
                "在告警搜索框输入 MB-VIB-RMS-HI。",
                "结果应为 WT-023 的「主轴承振动 RMS 持续升高」。点告警标题打开详情。",
              ],
            },
          ],
        },
        {
          text: "在告警详情核对机组 WT-023，抄下触发时间、当前值和告警阈值。若该告警已经处理过，当前值和状态以页面为准。",
          screenshots: [
            {
              src: "/guide/wt-023/01-alarm-details.png",
              width: 437,
              height: 998,
              alt: "WT-023 振动告警详情，显示当前值、阈值和趋势及 Mission 入口",
              callouts: [
                "「告警信息」显示触发时间。原始演示读数为 4.81 mm/s，超过 4.5 mm/s 阈值。",
                "点「打开趋势」，查看 WT-023 的实时监测。",
                "详情底部的「进入 Mission」通往下一节要看的关联任务。",
              ],
            },
          ],
        },
        {
          text: "在告警详情点「打开趋势」，或用本步的直达链接。观察触发前后的曲线，读完后返回告警详情。",
          screenshots: [
            {
              src: "/guide/wt-023/01-scada-trend.png",
              width: 1185,
              height: 668,
              alt: "WT-023 的 RMS 测点搜索、主轴承振动测点和 24 小时趋势曲线",
              callouts: [
                "确认「监测资产」是 WT-023。",
                "在测点搜索框输入 RMS。",
                "选中「主轴承振动 RMS」，确认主图的单位是 mm/s。",
                "在 24H 曲线中应看到连续抬升，以及越过 4.5 mm/s 阈值虚线的点。",
              ],
            },
          ],
          link: { href: "/scada?turbineId=WT-023", label: "直达 WT-023 实时监测" },
        },
      ],
      link: { href: "/alarms", label: "打开告警中心开始练习" },
    },
    {
      id: "read-mission",
      title: "跟到 Mission，读两条证据",
      steps: [
        {
          text: "从告警详情点「进入 Mission」，也可以用本节底部的直达链接。先核对 Mission 和机组，再确认当前阶段。不同演示实例的进度可能不同。",
          screenshots: [
            {
              src: "/guide/wt-023/02-mission-evidence.png",
              width: 1186,
              height: 572,
              alt: "WT-023 的 Mission 位于审核阶段，Agent 活动时间线展示关联证据",
              callouts: [
                "核对 MISSION-2026-0823 和机组 WT-023。",
                "阶段条高亮当前环节。截图中为「审核」，进度 72%。",
                "在「Agent 活动时间线」点「证据」。",
                "查看筛选后仍显示的事件和下方的关联证据条数。",
                "准备看方案时，点「比较全部 3 个方案」。",
              ],
            },
          ],
        },
        {
          text: "回到告警中心搜索温度告警，把它与刚才的振动告警对照。记录两个信号的采集时间、比较基线和来源。",
          screenshots: [
            {
              src: "/guide/wt-023/02-temperature-search.png",
              width: 650,
              height: 160,
              alt: "告警中心输入 MB-TEMP-DEV 后显示 WT-023 的温度告警",
              callouts: [
                "在告警搜索框输入 MB-TEMP-DEV。",
                "结果应为 WT-023 的「主轴承温度偏离基线」。点标题打开详情。",
              ],
            },
            {
              src: "/guide/wt-023/02-temperature-alarm.png",
              width: 458,
              height: 478,
              alt: "WT-023 主轴承温度偏离基线告警的标题、升温和阈值",
              callouts: [
                "打开详情后核对告警代码和机组 WT-023。",
                "演示记录显示，温度较同工况下的 30 日基线升高 8.4 °C。",
                "在「告警信息」查看触发时间、当前值 76.4 °C 和阈值 75 °C。",
              ],
            },
          ],
        },
      ],
      bullets: [
        "试着用一句话交接，例如「WT-023 的主轴承振动持续抬升，温度也高于同工况基线，Mission 汇总了两类证据供进一步复核」。把页面上的诊断置信度作为分析信息一并交接，审批结果单独看。",
      ],
      link: { href: "/missions/MISSION-2026-0823", label: "打开 WT-023 演示 Mission" },
    },
    {
      id: "read-decision",
      title: "比较方案，找到人工审批",
      steps: [
        {
          text: "在 Mission 的方案卡片点「比较全部 3 个方案」，或从「AI 运营 → 决策中心」进入。比较三种方案时，先核对风险和作业条件，再对照停机时间和成本，阅读演示里的推荐依据和约束。",
          screenshots: [
            {
              src: "/guide/wt-023/03-compare-plans.png",
              width: 1185,
              height: 647,
              alt: "决策中心的 WT-023 审核队列、候选方案、方案 B 指标及作业条件",
              callouts: [
                "在审核队列选中 WT-023 的决策。",
                "点选方案 A、B、C，比较停机检查、降载后检查和继续运行并增强监测。",
                "右侧随选择更新风险、成本和停机时间。此处选中的是标有「AI 首选」的方案 B。",
                "查看作业窗口及人员、工具等资源。",
              ],
            },
          ],
        },
        {
          text: "回到 Mission 的「工程师审批」。若已批准，查找已批准的方案、审批人和时间。只读练习可直接继续查看工单。",
          screenshots: [
            {
              src: "/guide/wt-023/03-approval.png",
              width: 403,
              height: 400,
              alt: "Mission 的工程师审批卡及只读状态，审批意见框已禁用",
              callouts: [
                "找到「工程师审批」卡片。",
                "查看审核结果和运行状态。截图提示 D1 当前不可写，审批为只读。",
                "这里是审批意见框。只读练习留空即可。",
              ],
            },
          ],
        },
      ],
      link: { href: "/decisions", label: "打开决策中心比较方案" },
    },
    {
      id: "read-work-order",
      title: "查看工单和完成条件",
      steps: [
        {
          text: "进入「运维执行 → 工单中心」，用完整工单编号查找这次作业。",
          screenshots: [
            {
              src: "/guide/wt-023/04-search.png",
              width: 496,
              height: 213,
              alt: "工单表格搜索框和 WT-023 主轴承检查工单",
              callouts: [
                "在工单搜索框输入 WO-20260823-017。",
                "结果中点「WT-023 主轴承检查」，打开工单详情。",
              ],
            },
          ],
        },
        {
          text: "先读工单状态，再依次查看计划时间、班组、安全规程和任务。五项演示任务从润滑取样开始，到提交现场照片、频谱和检查结论结束。",
          screenshots: [
            {
              src: "/guide/wt-023/04-detail.png",
              width: 465,
              height: 869,
              alt: "工单详情显示草稿状态、责任班组、关联 Mission 和五项任务",
              callouts: [
                "查看工单状态。只读演示截图中为「草稿」。",
                "在「计划信息」核对班组和关联任务 MISSION-2026-0823。",
                "在「任务清单」核对完成数。截图中已完成 0 项，共 5 项。",
              ],
            },
          ],
        },
      ],
      bullets: [
        "若工单仍是草稿或显示「等待审批」，回 Mission 核对审批是否完成。若已排程，确认班组和作业窗口。若已完成，回到 Mission 或「设备健康」查看是否记录了现场证据和健康复核结果。",
      ],
      link: { href: "/work-orders", label: "打开工单中心核对任务" },
    },
    {
      id: "handoff",
      title: "用一条交接记录检验练习结果",
      paragraphs: [
        "记下机组、告警代码、Mission 编号、当前审批状态、工单状态和下一位跟进人。演示可以写成「WT-023 · MB-VIB-RMS-HI → MISSION-2026-0823 → WO-20260823-017」。状态和待办请照当前页面填写。",
      ],
      steps: [
        {
          text: "在「知识与数据 → 运维报告」查找本次演练的 AI 诊断报告。",
          screenshots: [
            {
              src: "/guide/wt-023/05-search.png",
              width: 734,
              height: 302,
              alt: "报告中心的报告编号输入、匹配目录和 AI 诊断报告预览",
              callouts: [
                "在报告搜索框输入 RPT-AI-WT023-20260813。",
                "在目录中点「AI 诊断报告」。",
                "预览应显示相同的报告编号。",
              ],
            },
          ],
          link: { href: "/reports", label: "打开报告中心查找演示报告" },
        },
        {
          text: "用报告的工作流追踪核对交接信息。若演示已推进到其他阶段，以当前报告的状态和修订号为准。",
          screenshots: [
            {
              src: "/guide/wt-023/05-preview.png",
              width: 822,
              height: 310,
              alt: "报告预览显示 WT-023 工作流追踪、修订号、四项状态及 PDF 导出入口",
              callouts: [
                "认准「WT-023 工作流追踪」，记下修订号。",
                "核对 Mission、决策、工单和现场任务进度。截图中分别是审核中、审核中、草稿和 0/5。",
                "需要带走报告时，点「导出 PDF」。",
              ],
            },
          ],
        },
      ],
      link: { href: "/guide/reports", label: "继续练习预览和导出报告" },
    },
  ],
};
