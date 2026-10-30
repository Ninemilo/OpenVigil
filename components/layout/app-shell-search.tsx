"use client";

import { useMemo, useState } from "react";
import {
  Activity,
  TriangleAlert as AlarmTriangle,
  Bot,
  BrainCircuit,
  ChevronRight,
  CircleGauge,
  ClipboardCheck,
  FileBarChart,
  GitBranch,
  HeartPulse,
  Library,
  Monitor,
  PackageSearch,
  Search,
  Wrench,
} from "lucide-react";
import { WindFarmIcon, WindTurbineIcon } from "@/components/ui/wind-icons";
import { useOpenVigilIdentity } from "@/components/providers/identity-provider";
import type { OpenVigilCapability } from "@/lib/identity-session";
import { agents } from "@/lib/agent-data";
import { turbines } from "@/lib/farm-data";
import { alarms, missions, workOrders } from "@/lib/operations-data";
import { useAccessibleDialog } from "@/lib/use-accessible-dialog";
import { cn } from "@/lib/utils";
import {
  activeAgentCount,
  visibleAlarmCount,
  navigation,
  type RuntimeMode,
} from "./app-shell-navigation";

const primaryCommands = [
  {
    label: "打开 Mission 中心",
    description: "查看全部 Mission，并支持机组范围筛选",
    href: "/missions",
    icon: GitBranch,
  },
  {
    label: "创建工单 · 只读受控入口",
    description: "打开 WT-023 工单工作台并说明写入边界；此命令不会创建工单",
    href: "/work-orders?turbineId=WT-023&intent=create",
    icon: ClipboardCheck,
  },
  {
    label: "启动诊断 · 只读诊断入口",
    description: "仅打开 WT-023 现有 Mission 诊断视图；不会启动新 Agent 或写入诊断",
    href: "/missions?turbineId=WT-023&intent=diagnosis",
    icon: BrainCircuit,
  },
  {
    label: "打开 WT-023 机组详情",
    description: "健康度 68 · 主轴承预警",
    href: "/turbines/WT-023",
    icon: WindTurbineIcon,
  },
  {
    label: "查看 MISSION-2026-0823",
    description: "主轴承异常 · 可重放完整闭环",
    href: "/missions/MISSION-2026-0823",
    icon: GitBranch,
  },
  {
    label: "打开设备健康矩阵",
    description: "64 台机组 · 健康 · 异常 · 告警",
    href: "/health",
    icon: HeartPulse,
  },
  {
    label: "打开预测性维护",
    description: "64 台机组 · 状态证据 · 维护优先级",
    href: "/predictive-maintenance",
    icon: CircleGauge,
  },
  {
    label: "打开智能诊断",
    description: "差异诊断 · 证据支持与反证 · Agent 协作",
    href: "/diagnosis?turbineId=WT-023",
    icon: BrainCircuit,
  },
  {
    label: "查询运维资源",
    description: "备件 · 班组 · 船舶 · 工具 · 天气窗",
    href: "/resources",
    icon: PackageSearch,
  },
  {
    label: "打开维护计划",
    description: "工单 · 天气窗口 · 班组与资源冲突",
    href: "/maintenance",
    icon: Wrench,
  },
  {
    label: "打开运维报告",
    description: "六类报告 · 预览 · PDF · DOCX",
    href: "/reports",
    icon: FileBarChart,
  },
  {
    label: "打开知识库",
    description: "文档检索 · WT-023 引用式问答",
    href: "/knowledge",
    icon: Library,
  },
  {
    label: "打开数字孪生",
    description: "风场拓扑 · 机组状态 · 运维闭环映射",
    href: "/digital-twin",
    icon: Monitor,
  },
  { label: "查看实时 SCADA", description: "17 个在线测点", href: "/scada", icon: Activity },
  {
    label: "打开告警中心",
    description: `${visibleAlarmCount} 个待处理告警`,
    href: "/alarms",
    icon: AlarmTriangle,
  },
  {
    label: "打开检修工单",
    description: "查看 AI 决策关联的可审计工单",
    href: "/work-orders?workOrder=WO-20260823-017",
    icon: ClipboardCheck,
  },
  {
    label: "选择风场",
    description: "当前可用：华东海上风电场",
    href: "/wind-farms",
    icon: WindFarmIcon,
  },
  {
    label: "查看 Agent 控制中心",
    description: `${activeAgentCount} 个 Agent 正在工作`,
    href: "/agents",
    icon: Bot,
  },
];

const domainCommands = [
  ...turbines.map((turbine) => ({
    label: turbine.id,
    description: `${turbine.model} · 健康度 ${turbine.healthScore} · ${turbine.status}`,
    href: `/turbines/${turbine.id}`,
    icon: WindTurbineIcon,
  })),
  ...alarms.map((alarm) => ({
    label: alarm.id,
    description: `${alarm.turbineId} · ${alarm.code} · ${alarm.title}`,
    href: `/alarms?alarm=${alarm.id}`,
    icon: AlarmTriangle,
  })),
  ...missions.map((mission) => ({
    label: mission.id,
    description: `${mission.turbineId} · ${mission.title}`,
    href: `/missions/${mission.id}`,
    icon: GitBranch,
  })),
  ...workOrders.map((workOrder) => ({
    label: workOrder.id,
    description: `${workOrder.turbineId} · ${workOrder.issue}`,
    href: `/work-orders?workOrder=${workOrder.id}`,
    icon: ClipboardCheck,
  })),
  ...agents.map((agent) => ({
    label: agent.shortName,
    description: `${agent.name} · ${agent.role}`,
    href: `/agents?agent=${agent.id}`,
    icon: Bot,
  })),
];

const commands = [...primaryCommands, ...domainCommands];

const productionCommands = navigation.flatMap((group) =>
  group.items
    .filter((item) => !item.disabled)
    .map((item) => ({
      label: `打开${item.label}`,
      description: `进入${group.label}的${item.label}权威数据视图`,
      href: item.href === "/turbines/WT-023" ? "/wind-farms" : item.href,
      icon: item.icon,
      capability: item.capability,
    })),
);

export function commandResults(
  query: string,
  runtimeMode: RuntimeMode,
  can: (capability: OpenVigilCapability) => boolean,
) {
  const normalized = query.trim().toLowerCase();
  const permittedProductionCommands = productionCommands.filter(
    (command) => !command.capability || can(command.capability),
  );
  const catalog = runtimeMode === "production" ? permittedProductionCommands : commands;
  const defaults = runtimeMode === "production" ? permittedProductionCommands : primaryCommands;
  return normalized
    ? catalog.filter((command) =>
        `${command.label} ${command.description}`.toLowerCase().includes(normalized),
      )
    : defaults;
}

export function CommandPalette({
  open,
  onClose,
  runtimeMode,
}: {
  open: boolean;
  onClose: () => void;
  runtimeMode: RuntimeMode;
}) {
  const { can } = useOpenVigilIdentity();
  const dialogRef = useAccessibleDialog<HTMLDivElement>(onClose, open);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const results = useMemo(() => {
    return commandResults(query, runtimeMode, can);
  }, [can, query, runtimeMode]);

  if (!open) return null;

  return (
    <div className="dialog-backdrop">
      <button
        type="button"
        className="dialog-backdrop__dismiss"
        onClick={onClose}
        aria-label="关闭快捷命令"
      />
      <div
        ref={dialogRef}
        className="command-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="OpenVigil 快捷命令"
        tabIndex={-1}
      >
        <div className="command-dialog__search">
          <Search size={18} aria-hidden="true" />
          <input
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveIndex((value) => Math.min(results.length - 1, value + 1));
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveIndex((value) => Math.max(0, value - 1));
              }
              if (event.key === "Enter" && results[activeIndex]) {
                event.preventDefault();
                window.location.assign(results[activeIndex].href);
              }
            }}
            placeholder="搜索机组、告警、Mission 或工单…"
            aria-label="搜索快捷命令"
          />
          <kbd>ESC</kbd>
        </div>
        <div className="command-dialog__body">
          <span className="command-dialog__label">快捷操作</span>
          {results.length ? (
            results.map((command, index) => {
              const Icon = command.icon;
              return (
                <a
                  className={cn("command-item", index === activeIndex && "command-item--active")}
                  href={command.href}
                  key={`${command.label}-${command.href}`}
                  onMouseEnter={() => setActiveIndex(index)}
                >
                  <span className="command-item__icon">
                    <Icon size={17} />
                  </span>
                  <span>
                    <strong>{command.label}</strong>
                    <small>{command.description}</small>
                  </span>
                  <ChevronRight size={15} />
                </a>
              );
            })
          ) : (
            <div className="command-empty">没有找到相关对象</div>
          )}
        </div>
        <div className="command-dialog__footer">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd> 导航
          </span>
          <span>
            <kbd>↵</kbd> 打开
          </span>
        </div>
      </div>
    </div>
  );
}
