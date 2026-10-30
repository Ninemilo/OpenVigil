"use client";

import Link from "next/link";
import { TriangleAlert as AlarmTriangle, Bell, Bot, GitBranch, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import type { DemoWorkflowState } from "@/lib/demo-workflow";
import type { RuntimeMode } from "./app-shell-navigation";

type NotificationWorkflow = Pick<
  DemoWorkflowState,
  "missionStatus" | "decisionStatus" | "workOrderStatus" | "missionProgress"
>;

export function notificationItems(isProduction: boolean, workflow: NotificationWorkflow) {
  return isProduction
    ? ([
        [Link, "/alarms", AlarmTriangle, "critical", "打开告警中心", "查看当前权威告警与处置状态"],
        [
          Link,
          "/missions",
          GitBranch,
          "info",
          "打开 Mission 中心",
          "查看持久化事件与 Agent 执行进度",
        ],
      ] as const)
    : ([
        [
          Link,
          "/missions/MISSION-2026-0823",
          ShieldCheck,
          "warning",
          workflow.missionStatus === "completed"
            ? "WT-023 闭环已完成"
            : workflow.decisionStatus === "under-review"
              ? "WT-023 等待人工审批"
              : workflow.workOrderStatus === "in-progress"
                ? "WT-023 现场任务执行中"
                : "作业资源等待最终确认",
          `${workflow.missionStatus.replaceAll("-", " ")} · ${workflow.missionProgress}%`,
        ],
        ["a", "/alarms", AlarmTriangle, "critical", "主轴承振动告警升级", "ALARM-0031 · 9 分钟前"],
        ["a", "/agents", Bot, "info", "诊断 Agent 已完成分析", "置信度 87% · 14 分钟前"],
      ] as const);
}

export function ShellNotifications({
  open,
  onToggle,
  runtimeMode,
  workflow,
}: {
  open: boolean;
  onToggle: () => void;
  runtimeMode: RuntimeMode;
  workflow: NotificationWorkflow;
}) {
  const isProduction = runtimeMode === "production";
  const notifications = notificationItems(isProduction, workflow);
  return (
    <div className="popover-anchor">
      <Button size="icon" variant="ghost" onClick={onToggle} aria-label="通知">
        <Bell size={17} />
        {!isProduction ? <span className="notification-dot" /> : null}
      </Button>
      {open ? (
        <div className="notification-popover">
          <div className="notification-popover__header">
            <strong>最新动态</strong>
            <span>{isProduction ? "权威事件入口" : "3 条未读"}</span>
          </div>
          {notifications.map(([Tag, href, Icon, tone, title, detail]) => (
            <Tag href={href} key={href}>
              <span className={`notification-icon notification-icon--${tone}`}>
                <Icon size={15} />
              </span>
              <span>
                <strong>{title}</strong>
                <small>{detail}</small>
              </span>
            </Tag>
          ))}
        </div>
      ) : null}
    </div>
  );
}
