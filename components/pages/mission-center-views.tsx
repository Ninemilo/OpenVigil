"use client";

import type { ReactNode } from "react";
import { ArrowRight, Bot, CheckCircle2, CircleDot, Clock3, Users } from "lucide-react";
import type { LegacyColumnDef } from "@tanstack/react-table/legacy";
import { DataTable } from "@/components/data-display/data-table";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Avatar, Card, Progress } from "@/components/ui/primitives";
import { agents } from "@/lib/agent-data";
import { agentDisplayName } from "@/lib/agent-control-meta";
import type { Mission } from "@/lib/types";
import { localizedMissionTitle, localizedSeverityLabel } from "@/lib/ui-localization";
import { cn } from "@/lib/utils";
import { formatUpdatedAt, severityTone, statusLabel } from "./mission-center-support";

export function MissionCard({ mission }: { mission: Mission }) {
  const lead = agents.find((agent) => agent.id === mission.leadAgentId);
  return (
    <a
      className={cn("kanban-card", mission.id === "MISSION-2026-0823" && "kanban-card--featured")}
      href={`/missions/${mission.id}`}
    >
      <div className="kanban-card__header">
        <span className="mono">{mission.id}</span>
        <StatusBadge
          value={mission.severity}
          label={localizedSeverityLabel(mission.severity)}
          tone={severityTone(mission.severity)}
          compact
        />
      </div>
      <div className="kanban-card__copy">
        <strong>{localizedMissionTitle(mission.title)}</strong>
        <span>
          {mission.turbineId} · {mission.summary}
        </span>
      </div>
      {mission.diagnosis ? (
        <div className="kanban-diagnosis">
          <Bot size={12} />
          <span>{mission.diagnosis}</span>
          {mission.confidencePercent ? <strong>{mission.confidencePercent}%</strong> : null}
        </div>
      ) : null}
      <div className="kanban-card__progress">
        <span>
          <small>进度</small>
          <strong>{mission.progressPercent}%</strong>
        </span>
        <Progress
          value={mission.progressPercent}
          tone={mission.severity === "critical" || mission.severity === "high" ? "warning" : "info"}
        />
      </div>
      <div className="kanban-card__meta">
        <span>
          <Avatar
            label={lead ? agentDisplayName(lead.id, lead.shortName) : "AI"}
            tone="teal"
            size="sm"
          />
          <span>
            <small>主导 Agent</small>
            <strong>{lead ? agentDisplayName(lead.id, lead.shortName) : "未指派"}</strong>
          </span>
        </span>
        <span>
          <Users size={12} /> {mission.agentIds.length}
        </span>
      </div>
      <div className="kanban-card__footer">
        <span>
          <Clock3 size={11} /> {formatUpdatedAt(mission.updatedAt)}
        </span>
        <ArrowRight size={13} />
      </div>
    </a>
  );
}

const missionListColumns: readonly LegacyColumnDef<Mission, unknown>[] = [
  {
    accessorKey: "title",
    header: "Mission",
    cell: ({ row }) => (
      <span className="alarm-title-cell">
        <strong>{localizedMissionTitle(row.original.title)}</strong>
        <small className="mono">{row.original.id}</small>
      </span>
    ),
    size: 280,
  },
  {
    accessorKey: "status",
    header: "状态",
    cell: ({ row }) => (
      <StatusBadge value={row.original.status} label={statusLabel(row.original.status)} compact />
    ),
  },
  {
    accessorKey: "severity",
    header: "严重度",
    cell: ({ row }) => (
      <StatusBadge
        value={row.original.severity}
        label={localizedSeverityLabel(row.original.severity)}
        tone={severityTone(row.original.severity)}
        compact
      />
    ),
  },
  {
    accessorKey: "turbineId",
    header: "机组",
    cell: ({ row }) => <span className="mono">{row.original.turbineId}</span>,
  },
  {
    accessorKey: "leadAgentId",
    header: "主导 Agent",
    cell: ({ row }) =>
      (() => {
        const agent = agents.find((candidate) => candidate.id === row.original.leadAgentId);
        return agent ? agentDisplayName(agent.id, agent.shortName) : "未指派";
      })(),
  },
  {
    accessorKey: "progressPercent",
    header: "进度",
    cell: ({ row }) => (
      <span>
        <strong>{row.original.progressPercent}%</strong>
        <Progress value={row.original.progressPercent} />
      </span>
    ),
  },
  {
    accessorKey: "updatedAt",
    header: "更新时间",
    cell: ({ row }) => <span className="mono">{formatUpdatedAt(row.original.updatedAt)}</span>,
  },
];

const missionCsvExport = {
  filename: "openvigil-missions.csv",
  columns: [
    { label: "Mission ID", value: (mission: Mission) => mission.id },
    { label: "标题", value: (mission: Mission) => localizedMissionTitle(mission.title) },
    { label: "状态", value: (mission: Mission) => statusLabel(mission.status) },
    { label: "严重度", value: (mission: Mission) => localizedSeverityLabel(mission.severity) },
    { label: "风机", value: (mission: Mission) => mission.turbineId },
    {
      label: "主导 Agent",
      value: (mission: Mission) => {
        const agent = agents.find((item) => item.id === mission.leadAgentId);
        return agent ? agentDisplayName(agent.id, agent.shortName) : mission.leadAgentId;
      },
    },
    { label: "Agent 数", value: (mission: Mission) => mission.agentIds.length },
    { label: "进度", value: (mission: Mission) => mission.progressPercent },
    { label: "更新时间", value: (mission: Mission) => mission.updatedAt },
  ],
} as const;

const missionSearchText = (mission: Mission): string =>
  `${mission.id} ${mission.title} ${mission.turbineId} ${mission.summary} ${mission.diagnosis ?? ""}`;

const missionRowId = (mission: Mission): string => mission.id;

export function MissionList({
  items,
  filterControls,
}: {
  readonly items: readonly Mission[];
  readonly filterControls: ReactNode;
}) {
  return (
    <DataTable
      bulkActions={[
        {
          label: "打开首个所选 Mission",
          onActivate: (rows) => {
            const mission = rows[0];
            if (mission) window.location.assign(`/missions/${mission.id}`);
          },
        },
      ]}
      columns={missionListColumns}
      csvExport={missionCsvExport}
      data={items}
      emptyMessage="没有符合当前筛选条件的 Mission。"
      filterControls={filterControls}
      getRowId={missionRowId}
      initialSorting={[{ id: "updatedAt", desc: true }]}
      onRowActivate={(mission) => window.location.assign(`/missions/${mission.id}`)}
      pageSize={6}
      searchPlaceholder="搜索 Mission、机组或故障…"
      searchTextForRow={missionSearchText}
      selectedRowId="MISSION-2026-0823"
    />
  );
}

export function MissionTimeline({ items }: { items: readonly Mission[] }) {
  const ordered = [...items].sort(
    (left, right) => new Date(right.updatedAt).getTime() - new Date(left.updatedAt).getTime(),
  );

  return (
    <Card className="mission-timeline-card" aria-label="Mission 更新时间线">
      <div className="mission-timeline">
        {ordered.map((mission, index) => {
          const lead = agents.find((agent) => agent.id === mission.leadAgentId);
          const completed = mission.status === "completed";
          const attention =
            mission.status === "under-review" || mission.status === "decision-pending";
          const nodeTone = completed ? "success" : attention ? "attention" : "in-progress";
          const EventIcon = completed ? CheckCircle2 : CircleDot;
          return (
            <div
              className={cn("timeline-event", index === 0 && "timeline-event--current")}
              key={mission.id}
            >
              <time dateTime={mission.updatedAt}>{formatUpdatedAt(mission.updatedAt)}</time>
              <span className={`timeline-event__node timeline-event__node--${nodeTone}`}>
                <EventIcon size={13} />
              </span>
              <div className="timeline-event__body">
                <div>
                  <span>
                    <Avatar
                      label={lead ? agentDisplayName(lead.id, lead.shortName) : "AI"}
                      tone="teal"
                      size="sm"
                    />
                    <span>
                      <strong>{lead ? agentDisplayName(lead.id, lead.shortName) : "未指派"}</strong>
                      <small>{statusLabel(mission.status)}</small>
                    </span>
                  </span>
                  <StatusBadge value={mission.status} label={statusLabel(mission.status)} compact />
                </div>
                <a href={`/missions/${mission.id}`}>
                  <h3>
                    {mission.id} · {localizedMissionTitle(mission.title)}
                  </h3>
                </a>
                <p>
                  {mission.turbineId} · {mission.summary} 下一步：{mission.nextAction}
                </p>
              </div>
            </div>
          );
        })}
        {ordered.length === 0 ? (
          <div className="command-empty">没有符合当前筛选条件的 Mission。</div>
        ) : null}
      </div>
    </Card>
  );
}
