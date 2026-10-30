"use client";

import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { StatusBadge } from "@/components/data-display/status-badge";
import { Button } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api-client";
import {
  PARSER_HEALTH_REFRESH_MS,
  parseKnowledgeParserCapabilities,
  parserDurationLabel,
  parserFailureLabel,
  parserHealthPresentation,
} from "@/lib/knowledge-parser-health";

import styles from "./knowledge-parser-health.module.css";

export function useKnowledgeParserHealth(enabled: boolean) {
  return useQuery({
    queryKey: ["knowledge-parser-capabilities", enabled ? "production" : "demo"],
    enabled,
    queryFn: async ({ signal }) =>
      parseKnowledgeParserCapabilities(
        await apiGet<unknown>(
          "/api/backend/knowledge/parser-capabilities",
          AbortSignal.any([signal, AbortSignal.timeout(10_000)]),
        ),
      ),
    refetchInterval: enabled ? PARSER_HEALTH_REFRESH_MS : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: "always",
    staleTime: 0,
  });
}

function useHealthPresentation(query: ReturnType<typeof useKnowledgeParserHealth>) {
  const [observation, setObservation] = useState({ now: 0, visible: false, refreshAfter: 0 });
  const { refetch } = query;
  useEffect(() => {
    const tick = () =>
      setObservation((previous) => ({
        ...previous,
        now: Date.now(),
        visible: document.visibilityState === "visible",
      }));
    const onVisibility = () => {
      const now = Date.now();
      const visible = document.visibilityState === "visible";
      setObservation({ now, visible, refreshAfter: now });
      if (visible)
        void refetch({ cancelRefetch: false }).then((result) => {
          if (result.isError || document.visibilityState !== "visible") return;
          setObservation((previous) =>
            previous.refreshAfter === now
              ? { ...previous, now: Date.now(), refreshAfter: 0 }
              : previous,
          );
        });
    };
    const initialTick = window.setTimeout(tick, 0);
    const timer = window.setInterval(tick, 1_000);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearTimeout(initialTick);
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refetch]);
  return parserHealthPresentation({
    ...observation,
    data: query.data,
    dataUpdatedAt: query.dataUpdatedAt,
    fetching: query.isFetching,
    failed: query.isError,
  });
}

export function KnowledgeParserHealth({
  health,
}: {
  readonly health: ReturnType<typeof useKnowledgeParserHealth>;
}) {
  const presentation = useHealthPresentation(health);
  const { isFetching, isError, refetch } = health;
  const { queue } = presentation;
  const metrics = queue
    ? ([
        ["等待解析", queue.waiting],
        ["正在解析", queue.running],
        ["重试任务", queue.retrying],
        ["租约过期", queue.expired_leases],
        ["运行时长预警", queue.overdue_running],
        ["近 24 小时终态失败", queue.failed_last_24h],
      ] as const)
    : [];
  return (
    <section
      className={styles.health}
      aria-label="扫描件解析运行状态"
      data-health-state={presentation.state}
    >
      <div className={styles.header}>
        <div className={styles.title}>
          <h2>扫描件解析</h2>
          <span role="status" aria-live="polite" aria-atomic="true">
            <StatusBadge
              value={presentation.state}
              label={presentation.label}
              tone={presentation.tone}
            />
          </span>
        </div>
        <Button
          className={styles.refresh}
          variant="secondary"
          loading={isFetching}
          onClick={() => void refetch({ cancelRefetch: false })}
        >
          <RefreshCw size={14} aria-hidden="true" />
          {isError ? "重试解析配置" : "刷新解析状态"}
        </Button>
      </div>
      <p className={styles.description}>{presentation.description}</p>
      {health.data?.queue ? (
        <details className={styles.details}>
          <summary>队列详情 · 仅当前可访问文档</summary>
          {queue ? (
            <>
              <dl className={styles.metrics}>
                {metrics.map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
              <p className={styles.timing}>
                最长等待 {parserDurationLabel(queue.oldest_waiting_age_seconds)} · 最长运行{" "}
                {parserDurationLabel(queue.oldest_running_age_seconds)}
                <br />
                积压阈值 {parserDurationLabel(queue.backlog_warning_seconds)} · 运行时长阈值{" "}
                {parserDurationLabel(queue.execution_timeout_seconds)}
              </p>
              {queue.recent_failures.length > 0 ? (
                <ul className={styles.failures} aria-label="最近 24 小时仍未恢复的解析失败">
                  {queue.recent_failures.map((failure) => (
                    <li key={failure.document_id}>
                      <a href={`/knowledge?document=${encodeURIComponent(failure.document_id)}`}>
                        {failure.document_id}
                      </a>
                      <span>
                        {parserFailureLabel(failure.error_code)} ·{" "}
                        {failure.terminal ? "已终止" : "等待重试"} · 已尝试 {failure.attempts} 次
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}
              <p className={styles.observed}>
                观测于{" "}
                <time dateTime={queue.observed_at}>
                  {new Date(queue.observed_at).toLocaleTimeString("zh-CN", { hour12: false })}
                </time>{" "}
                · 前台每 15 秒刷新
              </p>
            </>
          ) : (
            <p className={styles.observed}>队列数量尚未确认</p>
          )}
        </details>
      ) : (
        <p className={styles.observed}>队列数量尚未确认</p>
      )}
    </section>
  );
}
