"use client";

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bot,
  CalendarDays,
  CheckCircle2,
  CircleDot,
  GitBranch,
  LayoutGrid,
  List,
  Plus,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/layout/page-header";
import { Button, EmptyState } from "@/components/ui/primitives";
import { QueryStateNotice, RuntimeHealthBadge } from "@/components/ui/query-state";
import { agents } from "@/lib/agent-data";
import { getFeaturedMissionNarrative } from "@/lib/demo-workflow";
import { missions } from "@/lib/operations-data";
import type { Alarm, Mission } from "@/lib/types";
import { useDemoWorkflow } from "@/lib/use-demo-workflow";
import { cn } from "@/lib/utils";
import { localizedSeverityLabel } from "@/lib/ui-localization";
import { agentDisplayName } from "@/lib/agent-control-meta";
import { apiGet } from "@/lib/api-client";
import {
  deriveQueryViewState,
  latestValidTimestamp,
  mergeRuntimeHealth,
  queryRuntimeHealth,
} from "@/lib/query-state";
import { columns, formatResolutionDuration, severityOptions } from "./mission-center-support";
import type { MissionView, SeverityFilter } from "./mission-center-support";
import { MissionCard, MissionList, MissionTimeline } from "./mission-center-views";
import { CreateMissionDrawer } from "./mission-create-drawer";

export function MissionCenterPage({ runtimeMode }: { runtimeMode: "demo" | "production" }) {
  const workflow = useDemoWorkflow();
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<"active" | "all">("active");
  const [view, setView] = useState<MissionView>("kanban");
  const [severity, setSeverity] = useState<SeverityFilter>("all");
  const [leadAgentId, setLeadAgentId] = useState("all");
  const [turbineScope, setTurbineScope] = useState<string | null>(null);
  const [diagnosisIntent, setDiagnosisIntent] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const missionQuery = useQuery({
    queryKey: ["missions", workflow.serverRevision, runtimeMode],
    queryFn: ({ signal }) => apiGet<{ readonly data: readonly Mission[] }>("/api/missions", signal),
    initialData: runtimeMode === "demo" ? { data: missions } : undefined,
    retry: false,
    staleTime: 60_000,
  });
  const alarmQuery = useQuery({
    queryKey: ["mission-create-alarms", runtimeMode],
    queryFn: ({ signal }) => apiGet<{ readonly data: readonly Alarm[] }>("/api/alarms", signal),
    initialData: runtimeMode === "demo" ? { data: [] } : undefined,
    enabled: runtimeMode === "production",
    retry: false,
    staleTime: 60_000,
  });
  const missionState = deriveQueryViewState({
    data: missionQuery.data?.data,
    dataUpdatedAt: missionQuery.dataUpdatedAt,
    error: missionQuery.error,
    isError: missionQuery.isError,
    isFetching: missionQuery.isFetching,
    isPending: missionQuery.isPending,
    isStale: missionQuery.isStale,
    isEmpty: (data) => data.length === 0,
    sourceUpdatedAt:
      runtimeMode === "production"
        ? latestValidTimestamp(missionQuery.data?.data.map((mission) => mission.updatedAt) ?? [])
        : null,
    staleAfterMs: 60_000,
  });
  const alarmState = deriveQueryViewState({
    data: alarmQuery.data?.data,
    dataUpdatedAt: alarmQuery.dataUpdatedAt,
    error: alarmQuery.error,
    isError: alarmQuery.isError,
    isFetching: alarmQuery.isFetching,
    isPending: alarmQuery.isPending,
    isStale: alarmQuery.isStale,
    isEmpty: (data) => data.length === 0,
  });
  const pageHealth = mergeRuntimeHealth(
    queryRuntimeHealth(missionState, "Mission 台账"),
    runtimeMode === "production"
      ? queryRuntimeHealth(alarmState, "告警选择器", { partial: true })
      : null,
  );
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search)
      .get("turbineId")
      ?.trim()
      .toUpperCase();
    const intent = new URLSearchParams(window.location.search).get("intent");
    if (!requested) return;
    const timer = window.setTimeout(() => {
      setTurbineScope(requested);
      setDiagnosisIntent(intent === "diagnosis");
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);
  const featuredNarrative = getFeaturedMissionNarrative(workflow);
  const displayMissions = useMemo(
    () =>
      (missionQuery.data?.data ?? []).map((mission) =>
        runtimeMode === "demo" && mission.id === "MISSION-2026-0823"
          ? {
              ...mission,
              status: workflow.missionStatus,
              progressPercent: workflow.missionProgress,
              summary: featuredNarrative.summary,
              nextAction: featuredNarrative.nextAction,
            }
          : mission,
      ),
    [
      featuredNarrative.nextAction,
      featuredNarrative.summary,
      workflow.missionProgress,
      workflow.missionStatus,
      missionQuery.data?.data,
      runtimeMode,
    ],
  );
  const leadOptions = useMemo<readonly { id: string; shortName: string }[]>(
    () =>
      runtimeMode === "production"
        ? [
            ...new Set(
              displayMissions.map((mission) => mission.leadAgentId).filter((id) => id !== ""),
            ),
          ].map((id) => ({ id, shortName: id }))
        : agents.filter((agent) =>
            displayMissions.some((mission) => mission.leadAgentId === agent.id),
          ),
    [displayMissions, runtimeMode],
  );
  const filteredByControls = useMemo(
    () =>
      displayMissions.filter(
        (mission) =>
          (scope === "all" || !["completed", "rejected"].includes(mission.status)) &&
          (turbineScope === null || mission.turbineId === turbineScope) &&
          (severity === "all" || mission.severity === severity) &&
          (leadAgentId === "all" || mission.leadAgentId === leadAgentId),
      ),
    [displayMissions, leadAgentId, scope, severity, turbineScope],
  );
  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return filteredByControls.filter((mission) =>
      `${mission.id} ${mission.title} ${mission.turbineId} ${mission.summary}`
        .toLowerCase()
        .includes(normalizedQuery),
    );
  }, [filteredByControls, query]);
  const activeCount = displayMissions.filter(
    (mission) => !["completed", "rejected"].includes(mission.status),
  ).length;
  const reviewCount = displayMissions.filter((mission) => mission.status === "under-review").length;
  const executionCount = displayMissions.filter((mission) => mission.status === "executing").length;
  const completedCount = displayMissions.filter((mission) => mission.status === "completed").length;
  // production 下统计真实 Mission 数据；demo 保持 fixture agents 的演示口径。
  const participatingAgentCount =
    runtimeMode === "production"
      ? new Set(
          displayMissions
            .filter((mission) => !["completed", "rejected"].includes(mission.status))
            .map((mission) => mission.leadAgentId)
            .filter((id) => id !== ""),
        ).size
      : agents.filter((agent) => agent.currentMissionId).length;
  const resolutionMinutes = displayMissions
    .filter((mission) => mission.status === "completed")
    .map(
      (mission) =>
        (new Date(mission.updatedAt).getTime() - new Date(mission.createdAt).getTime()) / 60_000,
    )
    .filter((minutes) => Number.isFinite(minutes) && minutes >= 0);
  const averageResolutionMinutes = resolutionMinutes.length
    ? resolutionMinutes.reduce((total, minutes) => total + minutes, 0) / resolutionMinutes.length
    : null;
  const selectView = (nextView: MissionView) => {
    if (nextView === "list") setQuery("");
    setView(nextView);
  };
  const missionFilterControls = (
    <>
      <div className="agent-filter-tabs">
        <button className={cn(scope === "active" && "active")} onClick={() => setScope("active")}>
          活跃
        </button>
        <button className={cn(scope === "all" && "active")} onClick={() => setScope("all")}>
          全部 Mission
        </button>
      </div>
      <label>
        <span className="sr-only">按严重程度筛选</span>
        <select
          className="select-field"
          value={severity}
          onChange={(event) => setSeverity(event.target.value as SeverityFilter)}
        >
          <option value="all">严重度 · 全部</option>
          {severityOptions.map((option) => (
            <option value={option} key={option}>
              严重度 · {localizedSeverityLabel(option)}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="sr-only">按主导 Agent 筛选</span>
        <select
          className="select-field"
          value={leadAgentId}
          onChange={(event) => setLeadAgentId(event.target.value)}
        >
          <option value="all">主导 Agent · 全部</option>
          {leadOptions.map((agent) => (
            <option value={agent.id} key={agent.id}>
              主导 Agent · {agentDisplayName(agent.id, agent.shortName)}
            </option>
          ))}
        </select>
      </label>
      <div className="view-switcher" aria-label="Mission 视图切换">
        <button
          className={cn(view === "kanban" && "active")}
          onClick={() => selectView("kanban")}
          aria-label="看板视图"
          aria-pressed={view === "kanban"}
        >
          <LayoutGrid size={15} />
        </button>
        <button
          className={cn(view === "list" && "active")}
          onClick={() => selectView("list")}
          aria-label="列表视图"
          aria-pressed={view === "list"}
        >
          <List size={15} />
        </button>
        <button
          className={cn(view === "timeline" && "active")}
          onClick={() => selectView("timeline")}
          aria-label="时间线视图"
          aria-pressed={view === "timeline"}
        >
          <CalendarDays size={15} />
        </button>
      </div>
      {turbineScope ? (
        <Button variant="secondary" onClick={() => setTurbineScope(null)}>
          机组 {turbineScope} <X size={12} />
        </Button>
      ) : null}
    </>
  );
  const queryBlocked =
    missionState.lifecycle === "initial-loading" || missionState.lifecycle === "error-no-data";
  const resetMissionFilters = () => {
    setQuery("");
    setScope("all");
    setSeverity("all");
    setLeadAgentId("all");
    setTurbineScope(null);
  };

  return (
    <AppShell runtimeMode={runtimeMode} activePath="/missions" pageHealth={pageHealth}>
      <PageHeader
        eyebrow="AI 运营"
        title="Mission 中心"
        description="跟踪多 Agent 从异常发现、诊断决策到运维执行的完整生命周期"
        breadcrumb={["AI 运营", "Mission 中心"]}
        meta={
          <>
            <RuntimeHealthBadge health={pageHealth} />
            <span className="page-meta-text">
              {missionState.hasData
                ? `${activeCount} 活跃 · ${reviewCount} 待审核 · ${executionCount} 执行中 · ${completedCount} 已完成`
                : "等待 Mission 权威台账"}
            </span>
          </>
        }
        actions={
          <>
            <Button
              variant={view === "timeline" ? "primary" : "secondary"}
              onClick={() => selectView("timeline")}
              aria-pressed={view === "timeline"}
            >
              <CalendarDays size={15} /> 时间线
            </Button>
            <span
              title={
                runtimeMode === "production"
                  ? "创建受控 Mission"
                  : "演示环境未连接 Mission 写入后端"
              }
            >
              <Button
                variant="primary"
                disabled={
                  runtimeMode !== "production" ||
                  missionState.health !== "ready" ||
                  alarmState.health !== "ready"
                }
                aria-describedby="create-mission-help"
                onClick={() => setCreateOpen(true)}
              >
                <Plus size={15} /> 创建 Mission
              </Button>
            </span>
            <span id="create-mission-help" className="sr-only">
              {runtimeMode === "production"
                ? "创建命令将写入权威后端并启动受控分析。"
                : "创建 Mission 在当前只读演示环境中不可用，因为尚未连接写入后端。"}
            </span>
          </>
        }
      />

      {queryBlocked ? (
        <QueryStateNotice
          state={missionState}
          target="Mission 台账"
          impact="Mission 数量、阶段、筛选结果与创建入口"
          onRetry={() => void missionQuery.refetch()}
        />
      ) : (
        <>
          <QueryStateNotice
            state={missionState}
            target="Mission 台账"
            impact="现有 Mission 内容"
            onRetry={() => void missionQuery.refetch()}
          />
          {runtimeMode === "production" ? (
            <QueryStateNotice
              state={alarmState}
              target="Mission 创建所需的告警数据"
              impact="创建入口"
              onRetry={() => void alarmQuery.refetch()}
              partial
            />
          ) : null}

          {diagnosisIntent ? (
            <section className="controlled-entry-note" role="status">
              <ShieldCheck size={16} />
              <span>
                <strong>已打开受控诊断入口</strong>
                <small>
                  资产范围：{turbineScope ?? "未指定"}。
                  {runtimeMode === "production"
                    ? "可从未关联告警创建持久化 Mission，并启动受控 Agent 分析。"
                    : "演示模式只展示现有诊断 Mission，不会假启动 Agent 流程。"}
                </small>
              </span>
              <Button
                variant="ghost"
                onClick={() => setDiagnosisIntent(false)}
                aria-label="关闭受控入口提示"
              >
                <X size={14} />
              </Button>
            </section>
          ) : null}

          <section className="mission-summary">
            <div>
              <span className="mission-summary__icon mission-summary__icon--info">
                <GitBranch size={16} />
              </span>
              <span>
                <small>活跃</small>
                <strong>{activeCount}</strong>
              </span>
            </div>
            <div>
              <span className="mission-summary__icon mission-summary__icon--warning">
                <ShieldCheck size={16} />
              </span>
              <span>
                <small>等待审核</small>
                <strong>{reviewCount}</strong>
              </span>
            </div>
            <div>
              <span className="mission-summary__icon mission-summary__icon--maintenance">
                <Bot size={16} />
              </span>
              <span>
                <small>参与 AGENT</small>
                <strong>{participatingAgentCount}</strong>
              </span>
            </div>
            <div>
              <span className="mission-summary__icon mission-summary__icon--success">
                <CheckCircle2 size={16} />
              </span>
              <span>
                <small>今日完成</small>
                <strong>{completedCount}</strong>
              </span>
            </div>
            <div className="mission-throughput">
              <span>
                <small>平均解决时长</small>
                <strong>
                  {runtimeMode === "production"
                    ? averageResolutionMinutes === null
                      ? "—"
                      : formatResolutionDuration(averageResolutionMinutes)
                    : "4h 18m"}
                </strong>
              </span>
              <em>
                {runtimeMode === "production"
                  ? averageResolutionMinutes === null
                    ? "暂无已闭环 Mission，无法计算"
                    : `基于 ${resolutionMinutes.length} 个已闭环 Mission 计算`
                  : "较 7 日均值下降 12%"}
              </em>
            </div>
          </section>

          {view === "list" ? null : (
            <section className="data-toolbar mission-toolbar">
              <div className="search-field">
                <Search size={15} />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索 Mission、机组或故障…"
                  aria-label="搜索 Mission"
                />
              </div>
              {missionFilterControls}
              <span className="toolbar-result">{filtered.length} 个 Mission</span>
            </section>
          )}

          {displayMissions.length === 0 ? (
            <EmptyState
              icon={<GitBranch size={22} />}
              title="当前范围内没有 Mission"
              description="权威 Mission 台账已成功返回空结果；这不是加载或服务错误。"
            />
          ) : filtered.length === 0 && view !== "list" ? (
            <EmptyState
              icon={<Search size={22} />}
              title="筛选结果为空"
              description="Mission 台账中存在记录，但当前搜索或筛选条件没有匹配项。"
              action={
                <Button variant="secondary" onClick={resetMissionFilters}>
                  清除筛选
                </Button>
              }
            />
          ) : view === "kanban" ? (
            <section className="mission-kanban" aria-label="Mission 状态看板">
              {columns.map((column) => {
                const columnMissions = filtered.filter(
                  (mission) => mission.status === column.status,
                );
                return (
                  <div className="kanban-column" key={column.status}>
                    <header>
                      <span>
                        <i className={`kanban-status-dot kanban-status-dot--${column.status}`} />
                        <strong>{column.label}</strong>
                        <em>{columnMissions.length}</em>
                      </span>
                      <small>{column.description}</small>
                    </header>
                    <div className="kanban-column__body">
                      {columnMissions.map((mission) => (
                        <MissionCard key={mission.id} mission={mission} />
                      ))}
                      {columnMissions.length === 0 ? (
                        <div className="kanban-empty">
                          <CircleDot size={15} />
                          <span>暂无 Mission</span>
                        </div>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </section>
          ) : view === "list" ? (
            <MissionList items={filteredByControls} filterControls={missionFilterControls} />
          ) : (
            <MissionTimeline items={filtered} />
          )}
          {createOpen ? (
            <CreateMissionDrawer
              alarms={alarmQuery.data?.data ?? []}
              preferredTurbineId={turbineScope}
              onClose={() => setCreateOpen(false)}
              onCreated={async () => {
                await Promise.all([missionQuery.refetch(), alarmQuery.refetch()]);
              }}
            />
          ) : null}
        </>
      )}
    </AppShell>
  );
}
