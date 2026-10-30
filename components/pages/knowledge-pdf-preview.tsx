/// <reference types="vite/client" />
"use client";

import { useEffect, useRef, useState } from "react";
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  PDFWorker,
  RenderTask,
} from "pdfjs-dist";
import PdfJsWorker from "pdfjs-dist/build/pdf.worker.min.mjs?worker";

import { Button } from "@/components/ui/primitives";
import type { NativeKnowledgePassage, SourceDownload } from "@/lib/knowledge-source";
import {
  fetchVerifiedPdf,
  KnowledgePdfError,
  pdfHighlights,
  pdfRenderScale,
  PDF_MAX_PAGES,
  PDF_MAX_PIXELS,
  PDF_TIMEOUT_MS,
  type PdfHighlight,
} from "@/lib/knowledge-pdf";
import styles from "./knowledge-pdf-preview.module.css";

export interface KnowledgePdfPreviewProps {
  readonly passage: NativeKnowledgePassage;
  readonly source: SourceDownload;
  readonly onRenewSource?: () => void;
}

export default function KnowledgePdfPreview(props: KnowledgePdfPreviewProps) {
  const [attempt, setAttempt] = useState(0);
  const identity = `${props.passage.passage_id}:${props.source.artifact_sha256}:${props.source.document_version}:${props.source.expires_at}:${props.source.download_url}`;
  return (
    <PdfSession
      key={`${identity}:${attempt}`}
      {...props}
      onRetry={() => setAttempt((value) => value + 1)}
    />
  );
}

function PdfSession({
  passage,
  source,
  onRenewSource,
  onRetry,
}: KnowledgePdfPreviewProps & { readonly onRetry: () => void }) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(passage.page_number ?? 1);
  const [zoom, setZoom] = useState(1);
  const [loading, setLoading] = useState(true);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [highlights, setHighlights] = useState<readonly PdfHighlight[]>([]);
  const [limited, setLimited] = useState(false);
  const [width, setWidth] = useState(400);
  const [renderedKey, setRenderedKey] = useState<string | null>(null);
  const renderKey = `${pageNumber}:${zoom}:${width}`;
  const canvas = useRef<HTMLCanvasElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) =>
      setWidth(Math.max(1, Math.floor(entry.contentRect.width - 24))),
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let active = true,
      disposed = false;
    let task: PDFDocumentLoadingTask | null = null;
    let worker: Worker | null = null;
    let pdfWorker: PDFWorker | null = null;
    const pending = new AbortController();
    controller.current = pending;
    const destroy = () => {
      if (disposed) return;
      disposed = true;
      void task?.destroy().catch(() => undefined); // Teardown also runs after worker termination.
      pdfWorker?.destroy();
      worker?.terminate();
    };
    const timeout = window.setTimeout(() => pending.abort("timeout"), PDF_TIMEOUT_MS);
    const expiration = window.setTimeout(
      () => {
        pending.abort("expired");
        destroy();
        if (active) {
          setDocument(null);
          setExpired(true);
          setError("原件授权已过期，请重新获取。");
          setRenderedKey(null);
        }
      },
      Math.max(0, Math.min(Date.parse(source.expires_at) - Date.now(), 2_147_483_647)),
    );
    pending.signal.addEventListener("abort", destroy, { once: true });
    void (async () => {
      try {
        const bytes = await fetchVerifiedPdf(passage, source, pending.signal);
        const pdfjs = await import("pdfjs-dist");
        pending.signal.throwIfAborted();
        worker = new PdfJsWorker({ name: "openvigil-pdf" });
        pdfWorker = pdfjs.PDFWorker.create({ port: worker });
        task = pdfjs.getDocument({
          data: bytes,
          worker: pdfWorker,
          enableXfa: false,
          stopAtErrors: true,
          useWasm: false,
          useWorkerFetch: false,
          canvasMaxAreaInBytes: PDF_MAX_PIXELS * 4,
          disableAutoFetch: true,
          disableStream: true,
        });
        // An externally supplied worker can stall before PDF.js establishes its
        // transport. Terminating that worker alone does not settle task.promise.
        const pdf = await Promise.race([
          task.promise,
          new Promise<never>((_resolve, reject) =>
            pending.signal.addEventListener(
              "abort",
              () => reject(new DOMException("PDF loading aborted", "AbortError")),
              {
                once: true,
              },
            ),
          ),
        ]);
        pending.signal.throwIfAborted();
        if (pdf.numPages < 1 || pdf.numPages > PDF_MAX_PAGES) throw new Error("page_limit");
        if ((passage.page_number ?? 1) > pdf.numPages) throw new Error("page_missing");
        if (active) setDocument(pdf);
      } catch (cause) {
        destroy();
        if (active) {
          const isExpired =
            pending.signal.reason === "expired" ||
            (cause instanceof KnowledgePdfError && cause.code === "expired");
          setExpired(isExpired);
          setError(
            isExpired
              ? "原件授权已过期，请重新获取。"
              : pending.signal.reason === "timeout"
                ? "原件读取超时，请重试。"
                : pending.signal.reason === "cancelled"
                  ? "已取消读取原件，可以重新加载。"
                  : cause instanceof KnowledgePdfError
                    ? cause.message
                    : "PDF 无法预览。请确认文件有效、未加密且不超过 200 页，再重试。",
          );
        }
      } finally {
        window.clearTimeout(timeout);
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
      window.clearTimeout(timeout);
      window.clearTimeout(expiration);
      pending.abort();
      destroy();
      if (controller.current === pending) controller.current = null;
    };
  }, [passage, source]);

  useEffect(() => {
    if (!document || !canvas.current) return;
    const element = canvas.current;
    let active = true;
    let render: RenderTask | null = null;
    let page: PDFPageProxy | null = null;
    const timeout = window.setTimeout(() => {
      render?.cancel();
      controller.current?.abort("render_timeout");
      if (active) {
        setDocument(null);
        setError("此页渲染超时，请重新加载原件。");
      }
    }, PDF_TIMEOUT_MS);
    void (async () => {
      try {
        page = await document.getPage(pageNumber);
        if (!active) return;
        const original = page.getViewport({ scale: 1 });
        const sizing = pdfRenderScale(
          original.width,
          original.height,
          width,
          zoom,
          window.devicePixelRatio || 1,
        );
        const viewport = page.getViewport({ scale: sizing.scale });
        element.width = Math.max(1, Math.floor(viewport.width * sizing.outputScale));
        element.height = Math.max(1, Math.floor(viewport.height * sizing.outputScale));
        element.style.width = `${viewport.width}px`;
        element.style.height = `${viewport.height}px`;
        const context = element.getContext("2d", { alpha: false });
        if (!context) throw new Error("canvas_unavailable");
        render = page.render({
          canvas: element,
          canvasContext: context,
          viewport,
          transform: [sizing.outputScale, 0, 0, sizing.outputScale, 0, 0],
        });
        await render.promise;
        if (!active) return;
        setHighlights(pdfHighlights(passage, page, viewport));
        setLimited(sizing.limited);
        setRenderedKey(renderKey);
        setError(null);
      } catch {
        if (active) setError("此页无法完整渲染，请重新加载原件或选择其他页。");
      } finally {
        window.clearTimeout(timeout);
        page?.cleanup();
      }
    })();
    return () => {
      active = false;
      window.clearTimeout(timeout);
      render?.cancel();
      element.width = 0;
      element.height = 0;
    };
  }, [document, pageNumber, width, zoom, passage, renderKey]);

  const ready = document !== null && renderedKey === renderKey && !error && !expired;
  const rendering = document !== null && !ready && !error && !expired;
  const table = passage.native_locator.location_precision === "table_region";
  const location =
    pageNumber !== passage.page_number
      ? `当前引用位于第 ${passage.page_number ?? "未标注"} 页。`
      : highlights.length
        ? table
          ? "高亮为表格区域，并非精确单元格。"
          : "高亮为解析定位区域，请结合原件核对。"
        : "仅定位原页，无法确认精确区域。";
  return (
    <section className={styles.preview} aria-label="PDF 原件预览">
      <div className={styles.toolbar}>
        <label className={styles.control}>
          页码{" "}
          <select
            aria-label="PDF 页码"
            value={pageNumber}
            disabled={!document || expired}
            onChange={(event) => {
              setError(null);
              setPageNumber(Number(event.target.value));
            }}
          >
            {Array.from({ length: document?.numPages ?? 1 }, (_, index) => (
              <option key={index} value={index + 1}>
                {index + 1}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.control}>
          缩放{" "}
          <select
            aria-label="PDF 缩放"
            value={zoom}
            disabled={!document || expired}
            onChange={(event) => {
              setError(null);
              setZoom(Number(event.target.value));
            }}
          >
            {[0.75, 1, 1.25, 1.5, 2, 4, 8].map((value) => (
              <option key={value} value={value}>
                {value * 100}%
              </option>
            ))}
          </select>
        </label>
        <Button
          variant="secondary"
          className={styles.action}
          disabled={!document || expired || pageNumber === passage.page_number}
          onClick={() => {
            setError(null);
            setPageNumber(passage.page_number ?? 1);
          }}
        >
          当前引用
        </Button>
      </div>
      <p className={styles.status} role="status" aria-live="polite">
        {error ??
          (loading
            ? "正在下载并核验原件…"
            : rendering
              ? `正在渲染第 ${pageNumber} 页…`
              : ready
                ? `原件字节已核验 · 第 ${pageNumber} / ${document?.numPages} 页`
                : "原件尚未就绪。")}
      </p>
      {(error || loading) && (
        <div className={styles.actions}>
          {loading ? (
            <Button
              variant="secondary"
              className={styles.action}
              onClick={() => controller.current?.abort("cancelled")}
            >
              取消读取
            </Button>
          ) : expired ? (
            onRenewSource && (
              <Button variant="secondary" className={styles.action} onClick={onRenewSource}>
                重新获取原件
              </Button>
            )
          ) : (
            <Button variant="secondary" className={styles.action} onClick={onRetry}>
              重试原件预览
            </Button>
          )}
        </div>
      )}
      <div
        ref={container}
        className={styles.viewport}
        aria-busy={loading || rendering}
        tabIndex={ready ? 0 : -1}
        role="region"
        aria-label="原 PDF 页面，可滚动查看"
      >
        <div className={styles.page} style={{ visibility: ready ? "visible" : "hidden" }}>
          <canvas
            ref={canvas}
            data-testid="knowledge-pdf-canvas"
            data-pdf-page={ready ? pageNumber : undefined}
            data-verified-source-sha256={ready ? source.artifact_sha256 : undefined}
            role="img"
            aria-label={`原 PDF 第 ${pageNumber} 页`}
          >
            当前页 PDF 图像；解析摘录可在对照区域阅读。
          </canvas>
          {ready &&
            highlights.map((region, index) => (
              <span
                key={index}
                data-testid="knowledge-pdf-highlight"
                className={styles.highlight}
                style={{
                  left: `${region.left}%`,
                  top: `${region.top}%`,
                  width: `${region.width}%`,
                  height: `${region.height}%`,
                }}
                aria-hidden="true"
              />
            ))}
        </div>
      </div>
      {ready && (
        <p className={styles.caption}>
          {location}
          {limited ? " 页面已按预览资源上限缩小。" : ""}
        </p>
      )}
    </section>
  );
}
