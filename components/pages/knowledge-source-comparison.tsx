"use client";

import { lazy, Suspense, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api-client";
import {
  knowledgeSourceLocation,
  verifiedSourceHref,
  type NativeKnowledgePassage,
  type SourceDownload,
} from "@/lib/knowledge-source";
import { verifyPassageSource } from "@/lib/knowledge-source-grant";

import styles from "./knowledge-source-comparison.module.css";

const PdfPreview = lazy(() => import("./knowledge-pdf-preview"));
const NumericReview = lazy(() =>
  import("./knowledge-numeric-review").then((module) => ({
    default: module.KnowledgeNumericReview,
  })),
);

/** Mounted only after an explicit comparison request; the parent keys source identity. */
export function KnowledgeSourceComparison({
  passage,
}: {
  readonly passage: NativeKnowledgePassage;
}) {
  const [source, setSource] = useState<SourceDownload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const region = useRef<HTMLElement>(null);

  function renewSource() {
    setLoading(true);
    setSource(null);
    setError(null);
    setAttempt((value) => value + 1);
  }

  useEffect(() => {
    region.current?.focus();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let expiryTimer: ReturnType<typeof setTimeout> | undefined;
    void apiGet<SourceDownload>(
      `/api/backend/knowledge/documents/${encodeURIComponent(passage.document_id)}/source?passage_id=${encodeURIComponent(passage.passage_id)}`,
      controller.signal,
    )
      .then((grant) => {
        verifyPassageSource(grant, passage);
        if (controller.signal.aborted) return;
        setSource(grant);
        expiryTimer = setTimeout(
          () => {
            setSource(null);
            setError("原文链接已过期，请重新获取。");
          },
          Math.min(Date.parse(grant.expires_at) - Date.now(), 300_000),
        );
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : "原件授权读取失败。");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      clearTimeout(expiryTimer);
    };
  }, [passage, attempt]);

  return (
    <section
      ref={region}
      className={styles.comparison}
      data-source-comparison
      tabIndex={-1}
      aria-label="原件与解析结果对照"
    >
      <header className={styles.header}>
        <h4>原件与解析结果对照</h4>
        <p>
          {knowledgeSourceLocation(passage)} · 版本 {passage.document_version}
        </p>
      </header>
      <div className={styles.columns}>
        <div className={styles.original}>
          {loading ? <p role="status">正在核验原件授权…</p> : null}
          {error ? (
            <div role="alert">
              <p>{error}</p>
              <Button size="sm" onClick={renewSource}>
                重新获取原件
              </Button>
            </div>
          ) : null}
          {source?.content_type === "application/pdf" ? (
            <Suspense fallback={<p role="status">正在加载 PDF 查看器…</p>}>
              <PdfPreview passage={passage} source={source} onRenewSource={renewSource} />
            </Suspense>
          ) : source ? (
            <div>
              <p>此原件不是 PDF，请打开原文件核对。</p>
              <a
                href={verifiedSourceHref(source.download_url, source.page_number)}
                target="_blank"
                rel="noopener noreferrer"
              >
                打开已授权原文件
              </a>
            </div>
          ) : null}
        </div>
        <div className={styles.excerpt}>
          <h4>解析原文</h4>
          <p className={styles.text}>{passage.text}</p>
          <p className={styles.note}>复核与修正单独记录；原始摘录和检索索引保持原内容。</p>
          <Suspense fallback={<p role="status">正在加载数值复核…</p>}>
            <NumericReview passage={passage} />
          </Suspense>
        </div>
      </div>
    </section>
  );
}
