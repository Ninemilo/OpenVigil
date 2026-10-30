"use client";

import { useState } from "react";
import type { FormEvent } from "react";
import { Plus, ShieldCheck, X } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { apiPostCommand } from "@/lib/api-client";
import type { Alarm } from "@/lib/types";
import { useAccessibleDialog } from "@/lib/use-accessible-dialog";

export function CreateMissionDrawer({
  alarms,
  preferredTurbineId,
  onClose,
  onCreated,
}: {
  readonly alarms: readonly Alarm[];
  readonly preferredTurbineId: string | null;
  readonly onClose: () => void;
  readonly onCreated: () => Promise<void>;
}) {
  const eligibleAlarms = alarms.filter((alarm) => alarm.missionId === null);
  const preferred =
    eligibleAlarms.find((alarm) => alarm.turbineId === preferredTurbineId) ?? eligibleAlarms[0];
  const [alarmId, setAlarmId] = useState(preferred?.id ?? "");
  const [title, setTitle] = useState(
    preferred ? `${preferred.turbineId} ${preferred.title} Investigation` : "",
  );
  const [component, setComponent] = useState(preferred?.subsystem.replaceAll("-", "_") ?? "");
  const [primaryVariable, setPrimaryVariable] = useState(
    preferred
      ? (preferred.sourceVariable ?? `${preferred.subsystem.replaceAll("-", "_")}_condition_signal`)
      : "",
  );
  const [knowledgeQuery, setKnowledgeQuery] = useState(
    preferred ? `${preferred.subsystem.replaceAll("-", " ")} inspection failure diagnosis` : "",
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useAccessibleDialog<HTMLElement>(onClose);

  function selectAlarm(nextAlarmId: string) {
    setAlarmId(nextAlarmId);
    const alarm = eligibleAlarms.find((item) => item.id === nextAlarmId);
    if (!alarm) return;
    const nextComponent = alarm.subsystem.replaceAll("-", "_");
    setTitle(`${alarm.turbineId} ${alarm.title} Investigation`);
    setComponent(nextComponent);
    setPrimaryVariable(alarm.sourceVariable ?? `${nextComponent}_condition_signal`);
    setKnowledgeQuery(`${nextComponent.replaceAll("_", " ")} inspection failure diagnosis`);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await apiPostCommand(
        "/api/missions",
        {
          alarm_id: alarmId,
          title,
          analysis_profile: {
            component,
            primary_variable: primaryVariable,
            related_variables: [],
            knowledge_query: knowledgeQuery,
          },
        },
        `mission-create-${crypto.randomUUID()}`,
      );
      await onCreated();
      onClose();
    } catch (submissionError) {
      setError(
        submissionError instanceof Error ? submissionError.message : "Mission 创建请求失败。",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <button className="drawer-backdrop" onClick={onClose} aria-label="关闭 Mission 创建面板" />
      <aside
        ref={dialogRef}
        className="detail-drawer mission-create-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-mission-title"
        tabIndex={-1}
      >
        <header className="detail-drawer__header">
          <div>
            <span className="eyebrow">受控业务命令</span>
            <h2 id="create-mission-title">创建 Mission</h2>
            <p>任务与分析事件将在同一事务中持久化，并支持幂等重试。</p>
          </div>
          <Button variant="ghost" onClick={onClose} aria-label="关闭">
            <X size={15} />
          </Button>
        </header>
        {eligibleAlarms.length ? (
          <form className="mission-create-form" onSubmit={(event) => void submit(event)}>
            <section className="drawer-section">
              <h3>任务来源</h3>
              <label>
                <span>未关联告警</span>
                <select value={alarmId} onChange={(event) => selectAlarm(event.target.value)}>
                  {eligibleAlarms.map((alarm) => (
                    <option value={alarm.id} key={alarm.id}>
                      {alarm.id} · {alarm.turbineId} · {alarm.title}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Mission 标题</span>
                <input
                  required
                  minLength={3}
                  maxLength={240}
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                />
              </label>
            </section>
            <section className="drawer-section">
              <h3>可审计分析配置</h3>
              <label>
                <span>部件标识</span>
                <input
                  required
                  minLength={2}
                  maxLength={80}
                  value={component}
                  onChange={(event) => setComponent(event.target.value)}
                />
              </label>
              <label>
                <span>主信号变量</span>
                <input
                  required
                  minLength={2}
                  maxLength={96}
                  value={primaryVariable}
                  onChange={(event) => setPrimaryVariable(event.target.value)}
                />
              </label>
              <label>
                <span>知识检索查询</span>
                <textarea
                  required
                  minLength={3}
                  maxLength={400}
                  rows={4}
                  value={knowledgeQuery}
                  onChange={(event) => setKnowledgeQuery(event.target.value)}
                />
              </label>
            </section>
            {error ? (
              <div className="drawer-alert" role="alert">
                <span className="drawer-alert__icon">
                  <ShieldCheck size={15} />
                </span>
                <span>
                  <strong>创建失败</strong>
                  <small>{error}</small>
                </span>
              </div>
            ) : null}
            <footer className="detail-drawer__footer">
              <Button type="button" variant="secondary" onClick={onClose} disabled={submitting}>
                取消
              </Button>
              <Button type="submit" variant="primary" disabled={submitting || !alarmId}>
                <Plus size={15} /> {submitting ? "正在提交…" : "创建并启动分析"}
              </Button>
            </footer>
          </form>
        ) : (
          <section className="drawer-section">
            <h3>没有可用告警</h3>
            <p className="muted">所有当前告警都已关联 Mission，或告警列表仍在加载。</p>
          </section>
        )}
      </aside>
    </>
  );
}
