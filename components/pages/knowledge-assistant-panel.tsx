"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  ArrowRight,
  Bot,
  FileCheck2,
  LoaderCircle,
  Send,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Button } from "@/components/ui/primitives";
import { apiPost } from "@/lib/api-client";
import {
  DEFAULT_KNOWLEDGE_QUESTION,
  type KnowledgeAssistantAnswer,
  type SourceCitation,
} from "@/lib/knowledge-assistant";
import { knowledgeSourceLocation } from "@/lib/knowledge-source";
import { featuredMission } from "@/lib/operations-data";
import styles from "./knowledge-base-page.module.css";

interface AssistantSuccessEnvelope {
  readonly ok: true;
  readonly data: { readonly answer: KnowledgeAssistantAnswer };
  readonly error: null;
  readonly meta: {
    readonly deterministic: boolean;
    readonly retrievalMode:
      | "deterministic-d1-passage-retrieval"
      | "fixture-passage-fallback"
      | "production-pgvector-neo4j-hybrid";
    readonly realEmbedding: boolean;
  };
}

function isAssistantSuccess(value: unknown): value is AssistantSuccessEnvelope {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const envelope = value as Partial<AssistantSuccessEnvelope>;
  return (
    envelope.ok === true &&
    envelope.error === null &&
    typeof envelope.data?.answer?.answer?.summary === "string" &&
    Array.isArray(envelope.data.answer.citations) &&
    typeof envelope.meta?.deterministic === "boolean" &&
    typeof envelope.meta.realEmbedding === "boolean"
  );
}

function CitationButton({
  citation,
  onOpen,
}: {
  readonly citation: SourceCitation;
  readonly onOpen: (citation: SourceCitation) => void;
}) {
  return (
    <button className={styles.citation} type="button" onClick={() => onOpen(citation)}>
      <span>
        <FileCheck2 size={14} /> {citation.docId} ·{" "}
        {knowledgeSourceLocation({
          page_number: citation.page,
          native_locator: citation.nativeLocator ?? {},
        })}
      </span>
      <strong>{citation.title}</strong>
      <small>{citation.summary}</small>
      <em>
        打开来源 <ArrowRight size={12} />
      </em>
    </button>
  );
}

export function KnowledgeAssistantPanel({
  runtimeMode,
  openCitation,
}: {
  readonly runtimeMode: "demo" | "production";
  readonly openCitation: (citation: SourceCitation) => void;
}) {
  const [question, setQuestion] = useState(
    runtimeMode === "production"
      ? "当前知识库有哪些已验证的维护证据？"
      : DEFAULT_KNOWLEDGE_QUESTION,
  );
  const [answer, setAnswer] = useState<KnowledgeAssistantAnswer | null>(null);
  const [assistantError, setAssistantError] = useState<string | null>(null);
  const [assistantLoading, setAssistantLoading] = useState(false);
  const didAskDefault = useRef(false);
  const askQuestion = useCallback(
    async (value: string) => {
      const normalized = value.trim();
      if (!normalized) {
        setAssistantError("请输入问题后再检索。");
        return;
      }
      setAssistantLoading(true);
      setAssistantError(null);
      try {
        const payload: unknown = await apiPost("/api/knowledge-assistant", {
          question: normalized,
          turbineId: runtimeMode === "demo" ? featuredMission.turbineId : "",
          missionId: runtimeMode === "demo" ? featuredMission.id : "",
        });
        if (!isAssistantSuccess(payload)) throw new Error("知识助手返回了无效响应。");
        setAnswer(payload.data.answer);
      } catch (error) {
        setAssistantError(error instanceof Error ? error.message : "知识检索失败，请稍后重试。");
      } finally {
        setAssistantLoading(false);
      }
    },
    [runtimeMode],
  );

  useEffect(() => {
    if (didAskDefault.current) return;
    didAskDefault.current = true;
    void askQuestion(question);
  }, [askQuestion, question]);

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void askQuestion(question);
  };
  return (
    <aside className={styles.assistantPanel} aria-label="知识助手">
      <header className={styles.assistantHeader}>
        <span>
          <Bot size={18} />
        </span>
        <div>
          <small>知识助手</small>
          <h2>带来源的回答</h2>
        </div>
        <StatusBadge value="demo" label="确定性检索" tone="info" />
      </header>

      <form className={styles.questionForm} onSubmit={onSubmit}>
        <label htmlFor="knowledge-question">向 WT-023 知识域提问</label>
        <div>
          <textarea
            id="knowledge-question"
            maxLength={500}
            rows={3}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
          />
          <Button
            aria-label="发送问题"
            loading={assistantLoading}
            size="icon"
            type="submit"
            variant="primary"
          >
            <Send size={15} />
          </Button>
        </div>
      </form>

      {assistantError ? (
        <div className={styles.assistantError} role="alert">
          <ShieldAlert size={16} />
          <span>
            <strong>检索失败</strong>
            <small>{assistantError}</small>
          </span>
        </div>
      ) : null}

      {assistantLoading && !answer ? (
        <div className={styles.loadingAnswer} aria-live="polite">
          <LoaderCircle className="spin" size={18} />
          正在对固定知识目录执行关键词打分…
        </div>
      ) : null}

      {answer ? (
        <div className={styles.answer} aria-live="polite">
          <section className={styles.answerSummary}>
            <span>
              <Sparkles size={14} /> 结构化回答
            </span>
            <h3>{answer.answer.headline}</h3>
            <p>{answer.answer.summary}</p>
            <div>
              <strong>{answer.answer.confidencePercent}%</strong>
              <small>Mission 诊断置信度</small>
            </div>
          </section>

          <div className={styles.findings}>
            {answer.answer.findings.map((finding) => (
              <article key={finding.label}>
                <span>{finding.label}</span>
                <strong>{finding.value}</strong>
                <p>{finding.interpretation}</p>
                <small>
                  {[...finding.evidenceIds, ...finding.passageIds].join(" · ") || "无可追溯段落"}
                </small>
              </article>
            ))}
          </div>

          <section className={styles.conclusion}>
            <strong>结论与边界</strong>
            <p>{answer.answer.conclusion}</p>
          </section>

          <section className={styles.sources}>
            <div>
              <span>来源引用</span>
              <small>{answer.citations.length} 个可追溯来源</small>
            </div>
            {answer.citations.map((citation) => (
              <CitationButton key={citation.passageId} citation={citation} onOpen={openCitation} />
            ))}
          </section>

          <div className={styles.disclaimer}>
            <ShieldAlert size={14} />
            <p>{answer.disclaimer}</p>
          </div>
        </div>
      ) : null}
    </aside>
  );
}
