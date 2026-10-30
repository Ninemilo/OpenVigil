import type { Mission, MissionStatus } from "@/lib/types";

export const columns: { status: MissionStatus; label: string; description: string }[] = [
  { status: "detected", label: "已发现", description: "新发现事件" },
  { status: "investigating", label: "调查中", description: "收集与分析证据" },
  { status: "diagnosed", label: "已诊断", description: "已形成故障判断" },
  { status: "decision-pending", label: "等待决策", description: "生成候选方案" },
  { status: "under-review", label: "审核中", description: "专业审核中" },
  { status: "approved", label: "已批准", description: "等待执行资源" },
  { status: "executing", label: "执行中", description: "现场任务进行中" },
  { status: "rejected", label: "已拒绝", description: "人工审核已拒绝" },
  { status: "completed", label: "已完成", description: "已完成闭环" },
];

export const severityOptions: readonly Mission["severity"][] = [
  "critical",
  "high",
  "medium",
  "low",
];

export type MissionView = "kanban" | "list" | "timeline";
export type SeverityFilter = "all" | Mission["severity"];

export function severityTone(severity: Mission["severity"]) {
  if (severity === "critical") return "critical";
  if (severity === "high") return "warning";
  if (severity === "medium") return "info";
  return "neutral";
}

export function statusLabel(status: MissionStatus) {
  return columns.find((column) => column.status === status)?.label ?? status;
}

export function formatUpdatedAt(timestamp: string) {
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function formatResolutionDuration(minutes: number) {
  const rounded = Math.round(minutes);
  if (rounded < 60) return `${rounded}m`;
  return `${Math.floor(rounded / 60)}h ${rounded % 60}m`;
}
