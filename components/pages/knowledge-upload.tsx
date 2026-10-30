"use client";

import { useRef, useState } from "react";
import { ShieldAlert, Upload } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { StructuralArtifactCommand as KnowledgeArtifactCommand } from "@/lib/structural-artifact-command";
import type { useKnowledgeParserHealth } from "./knowledge-parser-health";
import styles from "./knowledge-base-page.module.css";

type ParserCapabilities = Pick<ReturnType<typeof useKnowledgeParserHealth>, "data">;

export function useKnowledgeUpload(
  parserCapabilities: ParserCapabilities,
  documentQuery: {
    readonly refetch: () => Promise<unknown>;
  },
) {
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadUnknown, setUploadUnknown] = useState(false);
  const artifactCommand = useRef(new KnowledgeArtifactCommand());
  const uploadFlight = useRef(false);
  const frozenUpload = useRef<{
    readonly file: File;
    readonly documentId: string;
    readonly contentType: string;
    readonly parser: "docling" | "native";
  } | null>(null);
  const [parseScannedPdf, setParseScannedPdf] = useState(false);
  const uploadKnowledgeDocument = async (file: File) => {
    if (uploadFlight.current) return;
    const extension = file.name.split(".").pop()?.toLowerCase();
    const contentType =
      file.type ||
      (extension === "md"
        ? "text/markdown"
        : extension === "txt"
          ? "text/plain"
          : extension === "pdf"
            ? "application/pdf"
            : extension === "docx"
              ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              : "");
    const supported = new Set([
      "text/plain",
      "text/markdown",
      "application/pdf",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]);
    if (!supported.has(contentType)) {
      setUploadError("仅支持 UTF-8 TXT/Markdown、PDF 和 DOCX 文档。");
      return;
    }
    if (
      !artifactCommand.current.resultUnknown &&
      file.size > (parserCapabilities.data?.max_artifact_bytes ?? 50 * 1024 * 1024)
    ) {
      setUploadError("知识制品不能超过 50 MiB。");
      return;
    }
    const selectedParser =
      contentType === "application/pdf" && parseScannedPdf ? "docling" : "native";
    if (
      !artifactCommand.current.resultUnknown &&
      selectedParser === "docling" &&
      parserCapabilities.data?.docling_pdf_configured !== true
    ) {
      setUploadError("扫描件解析尚未配置，请先确认后台解析服务。");
      return;
    }
    setUploading(true);
    uploadFlight.current = true;
    setUploadError(null);
    try {
      if (!artifactCommand.current.resultUnknown) {
        frozenUpload.current = {
          file,
          documentId: `KD-${crypto.randomUUID()}`,
          contentType,
          parser: selectedParser,
        };
      }
      const frozen = frozenUpload.current;
      if (!frozen) throw new Error("没有可核验的知识提交。");
      const title = frozen.file.name.replace(/\.[^.]+$/, "");
      await artifactCommand.current.run(
        {
          file: frozen.file,
          presignPath: "/api/backend/knowledge/documents/uploads/presign",
          presignBody: { document_id: frozen.documentId, content_type: frozen.contentType },
          commandPath: "/api/backend/knowledge/documents",
          commandBody: {
            document_id: frozen.documentId,
            title,
            document_type: "maintenance-procedure",
            document_version: "1",
            content_type: frozen.contentType,
            parser: frozen.parser,
            metadata: { summary: title, language: "zh-CN", tags: ["uploaded"] },
          },
        },
        (value): value is { document_id: string; ingestion_status: string } =>
          value !== null &&
          typeof value === "object" &&
          (value as Record<string, unknown>).document_id === frozen.documentId &&
          typeof (value as Record<string, unknown>).ingestion_status === "string",
      );
      frozenUpload.current = null;
      await documentQuery.refetch();
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "知识制品上传失败。");
    } finally {
      setUploading(false);
      uploadFlight.current = false;
      setUploadUnknown(artifactCommand.current.resultUnknown);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  };

  return {
    uploadInputRef,
    uploading,
    uploadError,
    uploadUnknown,
    frozenUpload,
    parseScannedPdf,
    setParseScannedPdf,
    uploadKnowledgeDocument,
    parserCapabilities,
  };
}

export function KnowledgeUploadActions({
  upload,
}: {
  readonly upload: ReturnType<typeof useKnowledgeUpload>;
}) {
  const {
    uploadInputRef,
    uploading,
    uploadUnknown,
    parseScannedPdf,
    setParseScannedPdf,
    uploadKnowledgeDocument,
    parserCapabilities,
  } = upload;
  return (
    <>
      <label>
        <input
          type="checkbox"
          checked={parseScannedPdf}
          disabled={
            uploading || uploadUnknown || parserCapabilities.data?.docling_pdf_configured !== true
          }
          onChange={(event) => setParseScannedPdf(event.target.checked)}
        />{" "}
        PDF 扫描件解析
      </label>
      <input
        ref={uploadInputRef}
        hidden
        type="file"
        accept=".txt,.md,.pdf,.docx,text/plain,text/markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void uploadKnowledgeDocument(file);
        }}
      />
      <Button
        variant="primary"
        disabled={uploading || uploadUnknown}
        onClick={() => uploadInputRef.current?.click()}
      >
        <Upload size={15} /> {uploading ? "校验并上传中…" : "上传知识制品"}
      </Button>
    </>
  );
}

export function KnowledgeUploadFeedback({
  upload,
  runtimeMode,
}: {
  readonly upload: ReturnType<typeof useKnowledgeUpload>;
  readonly runtimeMode: "demo" | "production";
}) {
  const {
    uploading,
    uploadError,
    uploadUnknown,
    frozenUpload,
    parseScannedPdf,
    uploadKnowledgeDocument,
  } = upload;
  return (
    <>
      {runtimeMode === "production" && parseScannedPdf ? (
        <p className={styles.sourceNote}>
          PDF 将在后台解析。OCR 文字和表格中的数字、正负号、小数点与单位需对照原件复核。
        </p>
      ) : null}
      {uploadError ? (
        <section className="controlled-entry-note" role="alert">
          <ShieldAlert size={16} />
          <span>
            <strong>{uploadUnknown ? "知识提交响应未确认" : "知识制品未写入"}</strong>
            <small>{uploadError}</small>
          </span>
        </section>
      ) : null}
      {uploadUnknown ? (
        <section className="controlled-entry-note" role="alert">
          <span>
            <strong>提交结果尚待核验</strong>
            <small>已保留原文件、文档身份与幂等键。核验同一提交期间不能更换输入。</small>
          </span>
          <Button
            disabled={uploading}
            onClick={() => {
              if (frozenUpload.current) void uploadKnowledgeDocument(frozenUpload.current.file);
            }}
          >
            核验同一提交
          </Button>
        </section>
      ) : null}
    </>
  );
}
