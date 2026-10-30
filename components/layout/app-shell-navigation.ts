import {
  Activity,
  TriangleAlert as AlarmTriangle,
  Archive,
  Bot,
  Boxes,
  BrainCircuit,
  CircleGauge,
  ClipboardCheck,
  Database,
  FileBarChart,
  GitBranch,
  HeartPulse,
  LayoutDashboard,
  Library,
  Monitor,
  PackageSearch,
  Settings2,
  ShieldCheck,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { WindFarmIcon, WindTurbineIcon } from "@/components/ui/wind-icons";
import { agents } from "@/lib/agent-data";
import { windFarm } from "@/lib/farm-data";
import { alarms } from "@/lib/operations-data";
import type { OpenVigilCapability } from "@/lib/identity-session";

type NavigationItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  badge?: string;
  disabled?: boolean;
  capability?: OpenVigilCapability;
};

export type RuntimeMode = "demo" | "production";

type NavigationGroup = {
  label: string;
  items: NavigationItem[];
};

export const activeAgentCount = agents.filter((agent) =>
  ["thinking", "working", "reviewing"].includes(agent.status),
).length;
export const visibleAlarmCount = alarms.filter(
  (alarm) => alarm.status !== "resolved" && alarm.status !== "suppressed",
).length;
export const navigation: NavigationGroup[] = [
  {
    label: "概览",
    items: [
      {
        label: "运营指挥中心",
        href: "/",
        icon: LayoutDashboard,
        capability: "view.dashboard",
      },
    ],
  },
  {
    label: "资产与监测",
    items: [
      { label: "风场", href: "/wind-farms", icon: WindFarmIcon, capability: "view.assets" },
      {
        label: "风机",
        href: "/turbines/WT-023",
        icon: WindTurbineIcon,
        capability: "view.assets",
      },
      { label: "实时监测", href: "/scada", icon: Activity, capability: "view.telemetry" },
      { label: "设备健康", href: "/health", icon: HeartPulse, capability: "view.assets" },
      { label: "混塔结构", href: "/structural", icon: GitBranch, capability: "view.assets" },
    ],
  },
  {
    label: "智能运维",
    items: [
      {
        label: "告警中心",
        href: "/alarms",
        icon: AlarmTriangle,
        badge: String(visibleAlarmCount),
        capability: "view.alarms",
      },
      {
        label: "智能诊断",
        href: "/diagnosis",
        icon: BrainCircuit,
        capability: "view.missions",
      },
      {
        label: "预测性维护",
        href: "/predictive-maintenance",
        icon: CircleGauge,
        capability: "view.models",
      },
    ],
  },
  {
    label: "AI 运营",
    items: [
      {
        label: "Agent 控制中心",
        href: "/agents",
        icon: Bot,
        badge: String(activeAgentCount),
        capability: "view.agents",
      },
      {
        label: "Mission 中心",
        href: "/missions",
        icon: GitBranch,
        badge: String(windFarm.activeMissionCount),
        capability: "view.missions",
      },
      {
        label: "决策中心",
        href: "/decisions",
        icon: ShieldCheck,
        badge: "2",
        capability: "view.decisions",
      },
    ],
  },
  {
    label: "运维执行",
    items: [
      {
        label: "工单中心",
        href: "/work-orders",
        icon: ClipboardCheck,
        capability: "view.work_orders",
      },
      {
        label: "维护计划",
        href: "/maintenance",
        icon: Wrench,
        capability: "view.work_orders",
      },
      {
        label: "运维资源",
        href: "/resources",
        icon: PackageSearch,
        capability: "view.resources",
      },
    ],
  },
  {
    label: "知识与数据",
    items: [
      { label: "知识库", href: "/knowledge", icon: Library, capability: "view.knowledge" },
      { label: "数字孪生", href: "/digital-twin", icon: Monitor, capability: "view.assets" },
      {
        label: "故障知识图谱",
        href: "/knowledge-graph",
        icon: Boxes,
        capability: "view.knowledge",
      },
      { label: "数据中心", href: "/data", icon: Database, capability: "view.platform" },
      { label: "运维报告", href: "/reports", icon: FileBarChart, capability: "view.reports" },
    ],
  },
  {
    label: "系统",
    items: [
      { label: "模型管理", href: "/models", icon: Archive, capability: "view.models" },
      { label: "系统设置", href: "/settings", icon: Settings2, capability: "view.platform" },
    ],
  },
];

export function visibleNavigationFor(
  runtimeMode: RuntimeMode,
  can: (capability: OpenVigilCapability) => boolean,
) {
  const isProduction = runtimeMode === "production";
  return navigation
    .map((group) => ({
      ...group,
      items: group.items.filter(
        (item) => !isProduction || !item.capability || can(item.capability),
      ),
    }))
    .filter((group) => group.items.length > 0);
}
