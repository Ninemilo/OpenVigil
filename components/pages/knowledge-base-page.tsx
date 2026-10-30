"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { MouseEvent } from "react";
import {
  ArrowRight,
  BookMarked,
  CheckCircle2,
  Database,
  FileSearch,
  Filter,
  Layers3,
  Library,
  Link2,
  LoaderCircle,
  Search,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react";
import type { LegacyColumnDef } from "@tanstack/react-table/legacy";
import { useQuery } from "@tanstack/react-query";

import { DataTable } from "@/components/data-display/data-table";
import { StatusBadge } from "@/components/data-display/status-badge";
import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api-client";
import { failureCases } from "@/lib/archive-data";
import type { SourceCitation } from "@/lib/knowledge-assistant";
import { knowledgeDocuments } from "@/lib/knowledge-data";
import { featuredMission } from "@/lib/operations-data";
import type { KnowledgeDocument, KnowledgeDocumentType } from "@/lib/types";
import { useAccessibleDialog } from "@/lib/use-accessible-dialog";
import { useDemoWorkflow } from "@/lib/use-demo-workflow";
import { knowledgeIngestionLabel } from "@/lib/knowledge-source";
import { KnowledgeCitationDrawer, KnowledgeSourceViewer } from "./knowledge-source-viewer";
import { KnowledgeParserHealth, useKnowledgeParserHealth } from "./knowledge-parser-health";

import { KnowledgeAssistantPanel } from "./knowledge-assistant-panel";
import {
  KnowledgeUploadActions,
  KnowledgeUploadFeedback,
  useKnowledgeUpload,
} from "./knowledge-upload";

import styles from "./knowledge-base-page.module.css";

interface KnowledgeDocumentsEnvelope {
  readonly data: {
    readonly documents: readonly KnowledgeDocument[];
  };
}

const typeLabel: Record<KnowledgeDocumentType, string> = {
  "equipment-manual": "设备手册",
  "maintenance-procedure": "维护规程",
  "incident-case": "事件案例",
  "failure-case": "故障案例",
  "technical-standard": "技术标准",
  "manufacturer-bulletin": "厂商通告",
  "historical-work-order": "历史工单",
  "inspection-report": "巡检报告",
  "scada-analysis-report": "SCADA 报告",
};

const availableTypes = [...new Set(knowledgeDocuments.map((document) => document.type))].sort();

const formatDate = (value: string): string =>
  !Number.isFinite(Date.parse(value))
    ? "未提供"
    : new Date(value).toLocaleDateString("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      });

function DocumentDrawer({
  document,
  citedPage,
  citedPassageId,
  runtimeMode,
  onClose,
}: {
  readonly document: KnowledgeDocument;
  readonly citedPage: number | null;
  readonly citedPassageId: string | null;
  readonly runtimeMode: "demo" | "production";
  readonly onClose: () => void;
}) {
  const dialogRef = useAccessibleDialog<HTMLElement>(onClose);
  const attributes = [
    ["版本", document.version],
    ["制造商", document.manufacturer ?? "内部资料"],
    ["页数", document.pageCount === null ? "未提供" : `${document.pageCount} 页`],
    ["语言", document.language],
    ["更新日期", formatDate(document.updatedAt)],
  ] as const;

  return (
    <>
      <button
        className={styles.backdrop}
        type="button"
        aria-label="关闭文档详情"
        onClick={onClose}
      />
      <aside
        ref={dialogRef}
        aria-label={`${document.title} 文档详情`}
        aria-modal="true"
        className={styles.drawer}
        role="dialog"
        tabIndex={-1}
      >
        <header className={styles.drawerHeader}>
          <div>
            <span>知识文档</span>
            <h2>{document.title}</h2>
            <p>{document.id}</p>
          </div>
          <Button aria-label="关闭" size="icon" variant="ghost" onClick={onClose}>
            <X size={18} />
          </Button>
        </header>

        <div className={styles.drawerBody}>
          <div className={styles.documentState}>
            <StatusBadge value={document.type} label={typeLabel[document.type]} tone="info" />
            <span className={document.vectorized ? styles.indexed : styles.pendingIndex}>
              {document.vectorized ? <CheckCircle2 size={13} /> : <LoaderCircle size={13} />}
              {runtimeMode === "production"
                ? document.vectorized
                  ? "已索引"
                  : knowledgeIngestionLabel(document.ingestionStatus)
                : document.vectorized
                  ? "已进入演示检索目录"
                  : "浏览器派生 · 待持久化索引"}
            </span>
          </div>

          {runtimeMode === "production" ? (
            <KnowledgeSourceViewer
              documentId={document.id}
              citedPassageId={citedPassageId}
              ingestionStatus={document.ingestionStatus}
              requiresNumericReview={document.requiresNumericReview}
            />
          ) : (
            <section className={styles.previewPage}>
              <span>文档预览 · 第 {citedPage ?? 1} 页</span>
              <h3>{document.equipment}</h3>
              <p>{document.summary}</p>
              <div>
                {document.tags.map((tag) => (
                  <span key={tag}>{tag}</span>
                ))}
              </div>
            </section>
          )}

          <section className={styles.drawerSection}>
            <h3>文档属性</h3>
            <dl>
              {attributes.map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          </section>

          <section className={styles.drawerSection}>
            <h3>关联对象</h3>
            <div className={styles.relatedLinks}>
              {document.relatedTurbineIds.map((turbineId) => (
                <a key={turbineId} href={`/turbines/${turbineId}`}>
                  {turbineId} <ArrowRight size={12} />
                </a>
              ))}
              {document.relatedMissionIds.map((missionId) => (
                <a key={missionId} href={`/missions/${missionId}`}>
                  {missionId} <ArrowRight size={12} />
                </a>
              ))}
            </div>
          </section>
        </div>
      </aside>
    </>
  );
}

export function KnowledgeBasePage({ runtimeMode }: { runtimeMode: "demo" | "production" }) {
  const workflow = useDemoWorkflow();
  const [query, setQuery] = useState("");
  const [type, setType] = useState<"all" | KnowledgeDocumentType>("all");
  const [selectedDocument, setSelectedDocument] = useState<KnowledgeDocument | null>(null);
  const [citedPage, setCitedPage] = useState<number | null>(null);
  const [citedPassageId, setCitedPassageId] = useState<string | null>(null);
  const [unlistedCitation, setUnlistedCitation] = useState<Pick<
    SourceCitation,
    "docId" | "title" | "passageId"
  > | null>(null);
  const parserCapabilities = useKnowledgeParserHealth(runtimeMode === "production");
  const documentQuery = useQuery({
    queryKey: ["knowledge-documents", runtimeMode],
    queryFn: ({ signal }) => apiGet<KnowledgeDocumentsEnvelope>("/api/knowledge-documents", signal),
    initialData: runtimeMode === "demo" ? { data: { documents: knowledgeDocuments } } : undefined,
    refetchInterval: (query) =>
      runtimeMode === "production" &&
      query.state.data?.data.documents.some((document) =>
        ["pending_parse", "parsing", "pending"].includes(document.ingestionStatus ?? ""),
      )
        ? 2500
        : false,
  });

  const upload = useKnowledgeUpload(parserCapabilities, documentQuery);

  const workflowDocument = useMemo<KnowledgeDocument | null>(() => {
    if (runtimeMode === "production") return null;
    if (!workflow.knowledgeCaseId) return null;
    const knowledgeEvent = [...workflow.auditTrail]
      .reverse()
      .find((event) => event.kind === "knowledge");
    return {
      id: workflow.knowledgeCaseId,
      title: "WT-023 主轴承检查、处置与复测闭环案例",
      type: "incident-case",
      equipment: "WT-023 / 主轴承",
      manufacturer: "OpenVigil D1 Workflow",
      version: "Server closed-loop v1",
      updatedAt: knowledgeEvent?.timestamp ?? "2026-08-13T10:24:00+08:00",
      vectorized: false,
      pageCount: 14,
      language: "zh-CN",
      tags: ["WT-023", "主轴承", "闭环验证", "健康度 63→78", "D1 持久化"],
      summary:
        "由 WT-023 服务器工作流派生：人工审批、五项现场任务、复测验证与知识沉淀均已完成；主轴承健康度由 63 提升至 78，审计记录持久化在 D1。",
      relatedTurbineIds: ["WT-023"],
      relatedMissionIds: [featuredMission.id],
    };
  }, [runtimeMode, workflow.auditTrail, workflow.knowledgeCaseId]);

  const documents = useMemo(() => {
    const catalog = documentQuery.data?.data.documents ?? [];
    return workflowDocument ? [workflowDocument, ...catalog] : [...catalog];
  }, [documentQuery.data?.data.documents, workflowDocument]);
  const filteredDocuments = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
    return documents.filter((document) => {
      if (type !== "all" && document.type !== type) return false;
      if (!normalizedQuery) return true;
      return [
        document.id,
        document.title,
        document.type,
        document.equipment,
        document.manufacturer ?? "",
        document.summary,
        ...document.tags,
      ]
        .join(" ")
        .toLocaleLowerCase("zh-CN")
        .includes(normalizedQuery);
    });
  }, [documents, query, type]);

  const openDocument = useCallback(
    (document: KnowledgeDocument, page: number | null = null, passage: string | null = null) => {
      setSelectedDocument(document);
      setCitedPage(page);
      setCitedPassageId(passage);
      setUnlistedCitation(null);
    },
    [],
  );
  const closeDocument = useCallback(() => {
    setSelectedDocument(null);
    setCitedPage(null);
    setCitedPassageId(null);
    setUnlistedCitation(null);
  }, []);

  useEffect(() => {
    const parameters = new URLSearchParams(window.location.search);
    const documentId = parameters.get("document");
    const passageId = parameters.get("passage");
    const turbineId = parameters.get("turbineId");
    const document = documentId
      ? documents.find((candidate) => candidate.id === documentId)
      : undefined;
    const timer = window.setTimeout(() => {
      if (document)
        openDocument(
          document,
          null,
          passageId && /^[a-f0-9-]{36}$/i.test(passageId) ? passageId : null,
        );
      else if (documentId && runtimeMode === "production" && documentQuery.isSuccess)
        setUnlistedCitation({
          docId: documentId,
          title: documentId,
          passageId: passageId ?? `${documentId}#body`,
        });
      else if (turbineId) setQuery(turbineId);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [documents, openDocument, runtimeMode, documentQuery.isSuccess]);

  const columns = useMemo<LegacyColumnDef<KnowledgeDocument, unknown>[]>(
    () => [
      {
        accessorKey: "title",
        header: "文档",
        cell: ({ row }) => (
          <button
            className={styles.documentLink}
            type="button"
            onClick={(event: MouseEvent<HTMLButtonElement>) => {
              event.stopPropagation();
              openDocument(row.original);
            }}
          >
            <span>
              {row.original.type === "incident-case" ? (
                <BookMarked size={15} />
              ) : (
                <FileSearch size={15} />
              )}
            </span>
            <span>
              <strong>{row.original.title}</strong>
              <small>{row.original.id}</small>
            </span>
          </button>
        ),
        size: 320,
      },
      {
        accessorKey: "type",
        header: "类型",
        cell: ({ row }) => <span className={styles.typeChip}>{typeLabel[row.original.type]}</span>,
      },
      {
        accessorKey: "equipment",
        header: "设备 / 范围",
      },
      {
        accessorKey: "manufacturer",
        header: "制造商",
        cell: ({ row }) => row.original.manufacturer || "—",
      },
      {
        accessorKey: "version",
        header: "版本",
        cell: ({ row }) => <span className={styles.mono}>{row.original.version}</span>,
      },
      {
        accessorKey: "pageCount",
        header: "页数",
        cell: ({ row }) =>
          row.original.pageCount === null ? "未提供" : `${row.original.pageCount} 页`,
      },
      {
        accessorKey: "updatedAt",
        header: "更新日期",
        cell: ({ row }) => formatDate(row.original.updatedAt),
      },
      {
        accessorKey: "vectorized",
        header: "检索状态",
        cell: ({ row }) => (
          <span className={row.original.vectorized ? styles.indexed : styles.pendingIndex}>
            {row.original.vectorized ? <CheckCircle2 size={12} /> : <LoaderCircle size={12} />}
            {row.original.vectorized
              ? "已编目"
              : knowledgeIngestionLabel(row.original.ingestionStatus)}
          </span>
        ),
      },
    ],
    [openDocument],
  );

  const openCitation = (citation: SourceCitation) => {
    const document = documents.find((candidate) => candidate.id === citation.docId);
    const passageId = /^[a-f0-9-]{36}$/i.test(citation.passageId) ? citation.passageId : null;
    if (document) openDocument(document, citation.page, passageId);
    else if (runtimeMode === "production") {
      setSelectedDocument(null);
      setUnlistedCitation(citation);
    }
  };
  const metrics = [
    [
      Library,
      "文档数",
      documents.length,
      runtimeMode === "production"
        ? "权威制品台账"
        : workflowDocument
          ? "+1 条工作流沉淀"
          : "精选目录",
    ],
    [
      Database,
      "可检索",
      documents.filter((item) => item.vectorized).length,
      runtimeMode === "production" ? "真实 embedding 状态" : "演示元数据",
    ],
    [Layers3, "故障案例", runtimeMode === "production" ? "—" : failureCases.length, "闭环归档"],
    [
      Link2,
      "WT-023 证据",
      runtimeMode === "production" ? "—" : featuredMission.evidenceIds.length,
      runtimeMode === "production" ? "按检索范围计算" : featuredMission.id,
    ],
  ] as const;

  return (
    <AppShell runtimeMode={runtimeMode} activePath="/knowledge">
      <PageHeader
        eyebrow="知识与数据"
        title="知识库"
        description="统一检索风机手册、维护规程、SCADA 报告和已闭环故障案例，并查看可追溯的来源引用。"
        breadcrumb={["知识与数据", "知识库"]}
        meta={
          <>
            <StatusBadge value="ready" label={`${documents.length} 份文档`} tone="success" />
            <span className="page-meta-text">
              {runtimeMode === "production"
                ? "PostgreSQL 制品台账 · pgvector + Neo4j 混合检索"
                : "确定性演示数据 · 不是生产向量库"}
            </span>
          </>
        }
        actions={
          runtimeMode === "production" ? <KnowledgeUploadActions upload={upload} /> : undefined
        }
      />

      {documentQuery.isError ? (
        <section className="controlled-entry-note" role="alert">
          <ShieldAlert size={16} />
          <span>
            <strong>生产知识目录暂时不可用</strong>
            <small>未回退到演示文档，请检查 Python 后端、对象存储与身份授权。</small>
          </span>
        </section>
      ) : null}
      {runtimeMode === "production" ? <KnowledgeParserHealth health={parserCapabilities} /> : null}
      <KnowledgeUploadFeedback upload={upload} runtimeMode={runtimeMode} />

      <section className={styles.metrics} aria-label="知识库统计">
        {metrics.map(([Icon, label, value, note]) => (
          <article key={label}>
            <span>
              <Icon size={17} />
            </span>
            <div>
              <small>{label}</small>
              <strong>{value}</strong>
            </div>
            <em>{note}</em>
          </article>
        ))}
      </section>

      <div className={styles.layout}>
        <section className={styles.libraryPanel}>
          <header className={styles.panelHeader}>
            <div>
              <span>文档目录</span>
              <h2>知识文档</h2>
              <p>
                {runtimeMode === "production"
                  ? "目录来自服务端制品台账；索引状态、模型和来源哈希均为实际持久化结果。"
                  : "搜索、筛选、排序、分页与行选择均在当前确定性数据集上真实执行。"}
              </p>
            </div>
            {workflowDocument ? (
              <span className={styles.workflowReady}>
                <Sparkles size={13} /> 闭环案例已派生
              </span>
            ) : null}
          </header>

          <div className={styles.filters}>
            <label className={styles.searchField}>
              <Search size={15} aria-hidden="true" />
              <input
                aria-label="搜索知识文档"
                placeholder="搜索标题、ID、设备、标签或摘要…"
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
              {query ? (
                <button aria-label="清除搜索" type="button" onClick={() => setQuery("")}>
                  <X size={13} />
                </button>
              ) : null}
            </label>
            <label className={styles.selectField}>
              <Filter size={14} aria-hidden="true" />
              <select
                aria-label="按文档类型筛选"
                value={type}
                onChange={(event) => setType(event.target.value as "all" | KnowledgeDocumentType)}
              >
                <option value="all">全部类型</option>
                {availableTypes.map((documentType) => (
                  <option key={documentType} value={documentType}>
                    {typeLabel[documentType]}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <DataTable
            bulkActions={[
              {
                label: "复制文档 ID",
                onActivate: async (rows) =>
                  navigator.clipboard.writeText(rows.map((row) => row.id).join(", ")),
              },
            ]}
            columns={columns}
            csvExport={{
              filename: "openvigil-knowledge-documents.csv",
              columns: [
                { label: "Document ID", value: (row) => row.id },
                { label: "Title", value: (row) => row.title },
                { label: "Type", value: (row) => row.type },
                { label: "Equipment", value: (row) => row.equipment },
                { label: "Manufacturer", value: (row) => row.manufacturer },
                { label: "Version", value: (row) => row.version },
                { label: "Updated", value: (row) => row.updatedAt },
              ],
            }}
            data={filteredDocuments}
            emptyMessage="没有符合搜索与类型筛选条件的文档"
            getRowId={(document) => document.id}
            initialSorting={[{ id: "updatedAt", desc: true }]}
            pageSize={6}
            selectedRowId={selectedDocument?.id}
            onRowActivate={(document) => openDocument(document)}
          />
        </section>

        <KnowledgeAssistantPanel runtimeMode={runtimeMode} openCitation={openCitation} />
      </div>

      {selectedDocument ? (
        <DocumentDrawer
          key={selectedDocument.id}
          document={
            documents.find((document) => document.id === selectedDocument.id) ?? selectedDocument
          }
          citedPage={citedPage}
          citedPassageId={citedPassageId}
          runtimeMode={runtimeMode}
          onClose={closeDocument}
        />
      ) : null}
      {unlistedCitation ? (
        <KnowledgeCitationDrawer
          key={unlistedCitation.passageId}
          citation={unlistedCitation}
          onClose={closeDocument}
        />
      ) : null}
    </AppShell>
  );
}
