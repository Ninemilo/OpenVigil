"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/primitives";
import { useOpenVigilIdentity } from "@/components/providers/identity-provider";
import { apiGet, OpenVigilApiError } from "@/lib/api-client";
import type { NativeKnowledgePassage } from "@/lib/knowledge-source";
import {
  correctedNumericText,
  freezeNumericCommand,
  numericReviewLabel,
  numericReviewPendingScope,
  numericSelection,
  parseNumericReview,
  postNumericCommand,
  type FrozenNumericCommand,
  type NumericCorrection,
  type NumericReviewEnvelope,
} from "@/lib/knowledge-numeric-review";
import styles from "./knowledge-numeric-review.module.css";

// Browser-memory only; closing a comparison preserves an uncertain write.
const pendingCommands = new Map<
  string,
  {
    command: FrozenNumericCommand;
    status: number | null;
    message: string | null;
    inFlight: boolean;
  }
>();
const pendingChanges = new EventTarget();

function serverTime(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

/** A separate, on-demand review layer; source OCR and retrieval are never rewritten. */
export function KnowledgeNumericReview({ passage }: { readonly passage: NativeKnowledgePassage }) {
  const { session } = useOpenVigilIdentity();
  if (!session?.subject) return <p className={styles.hint}>数值复核需要真实登录身份。</p>;
  return (
    <NumericReviewPanel
      key={numericReviewPendingScope(session.subject, passage)}
      passage={passage}
      subject={session.subject}
    />
  );
}

function NumericReviewPanel({
  passage,
  subject,
}: {
  readonly passage: NativeKnowledgePassage;
  readonly subject: string;
}) {
  const id = useId();
  const identity = numericReviewPendingScope(subject, passage);
  const [identityValid, setIdentityValid] = useState(true);
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<NumericReviewEnvelope | null>(null);
  const [reading, setReading] = useState(false);
  const [writing, setWriting] = useState(pendingCommands.get(identity)?.inFlight ?? false);
  const [readError, setReadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(pendingCommands.get(identity)?.message ?? null);
  const [failureStatus, setFailureStatus] = useState<number | null>(
    pendingCommands.get(identity)?.status ?? null,
  );
  const [command, setCommand] = useState<FrozenNumericCommand | null>(
    pendingCommands.get(identity)?.command ?? null,
  );
  const [reconciled, setReconciled] = useState(false);
  const [kind, setKind] = useState<"confirm_original" | "correct_values">("confirm_original");
  const [corrections, setCorrections] = useState<NumericCorrection[]>([]);
  const [reason, setReason] = useState("");
  const [reviewReason, setReviewReason] = useState("");
  const [selection, setSelection] = useState<ReturnType<typeof numericSelection> | null>(null);
  const busy = useRef(false);
  const active = useRef(true);
  const readController = useRef<AbortController | null>(null);
  const sourceInput = useRef<HTMLTextAreaElement>(null);
  const path = `/api/backend/knowledge/passages/${encodeURIComponent(passage.passage_id)}/numeric-review`;

  useEffect(() => {
    active.current = true;
    const synchronize = () => {
      const pending = pendingCommands.get(identity);
      setCommand(pending?.command ?? null);
      setWriting(pending?.inFlight ?? false);
      setFailureStatus(pending?.status ?? null);
      setError(pending?.message ?? null);
    };
    pendingChanges.addEventListener("change", synchronize);
    return () => {
      active.current = false;
      readController.current?.abort();
      pendingChanges.removeEventListener("change", synchronize);
    };
  }, [identity]);

  async function verifySubject(signal?: AbortSignal) {
    const actual = await apiGet<{ subject: string }>("/api/backend/session", signal);
    if (actual.subject !== subject) {
      if (active.current) setIdentityValid(false);
      throw new OpenVigilApiError(
        403,
        "REVIEW_IDENTITY_CHANGED",
        "登录身份已变化。请刷新页面，不得以另一主体核验原提交。",
      );
    }
  }

  async function read(cursor?: number) {
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    setReading(true);
    setReadError(null);
    try {
      await verifySubject(controller.signal);
      const result = parseNumericReview(
        await apiGet<unknown>(
          `${path}?limit=20${cursor === undefined ? "" : `&cursor=${cursor}`}`,
          controller.signal,
        ),
        passage,
      );
      if (!controller.signal.aborted && active.current) {
        setReconciled(true);
        setData((previous) =>
          cursor === undefined || !previous
            ? result
            : {
                ...result,
                history: [...previous.history, ...result.history].filter(
                  (event, index, events) =>
                    events.findIndex((entry) => entry.event_id === event.event_id) === index,
                ),
              },
        );
      }
    } catch (cause) {
      if (!controller.signal.aborted && active.current)
        setReadError(cause instanceof Error ? cause.message : "复核记录读取失败，请重试。");
    } finally {
      if (!controller.signal.aborted && active.current) setReading(false);
    }
  }

  function captureSelection() {
    const input = sourceInput.current;
    if (!input || input.selectionStart === input.selectionEnd) {
      setSelection(null);
      return;
    }
    try {
      setSelection(numericSelection(passage.text, input.selectionStart, input.selectionEnd));
    } catch (cause) {
      setSelection(null);
      setError(cause instanceof Error ? cause.message : "选区不可用。");
    }
  }

  function addCorrection() {
    if (!selection || corrections.length >= 40) return;
    if (!/^[0-9OoIl|+−\-.,eE]+$/.test(selection.original) || selection.original.length > 80) {
      setError("请只选择数值，单位使用单独的定位按钮。");
      return;
    }
    setCorrections([
      ...corrections,
      {
        start: selection.start,
        end: selection.end,
        original_value: selection.original,
        corrected_value: "",
        original_unit: "",
        corrected_unit: "",
        unit_start: null,
        unit_end: null,
      },
    ]);
    setError(null);
  }

  function updateCorrection(index: number, update: Partial<NumericCorrection>) {
    setCorrections((current) =>
      current.map((item, row) => (row === index ? { ...item, ...update } : item)),
    );
  }

  async function send(frozen: FrozenNumericCommand) {
    if (!identityValid || busy.current || pendingCommands.get(identity)?.inFlight) return;
    busy.current = true;
    setWriting(true);
    setCommand(frozen);
    setError(null);
    setFailureStatus(null);
    setReconciled(false);
    pendingCommands.set(identity, { command: frozen, status: null, message: null, inFlight: true });
    pendingChanges.dispatchEvent(new Event("change"));
    try {
      await verifySubject();
      // A valid receipt can be older than current state. Always read authoritative state after it.
      parseNumericReview(await postNumericCommand(frozen), passage);
      pendingCommands.delete(identity);
      pendingChanges.dispatchEvent(new Event("change"));
      if (!active.current) return;
      setCommand(null);
      setCorrections([]);
      setReason("");
      setReviewReason("");
      await read();
    } catch (cause) {
      const status = cause instanceof OpenVigilApiError ? cause.status : 0;
      const message =
        cause instanceof OpenVigilApiError && cause.code === "REVIEW_IDENTITY_CHANGED"
          ? cause.message
          : status === 409
            ? "其他提交或来源已发生变化。请刷新记录，核对差异后再编辑；不会覆盖现有记录。"
            : status === 403
              ? "当前身份无权提交。已保留原内容与提交键；权限恢复后可核验同一提交。"
              : cause instanceof Error
                ? cause.message
                : "提交结果未知，请核验同一提交。";
      pendingCommands.set(identity, { command: frozen, status, message, inFlight: false });
      pendingChanges.dispatchEvent(new Event("change"));
    } finally {
      busy.current = false;
      if (active.current) setWriting(false);
    }
  }

  function propose() {
    if (!data || command || busy.current || reading || readError || !data.permissions.can_propose)
      return;
    try {
      if (reason.trim().length < 3) throw new Error("请填写至少 3 个字符的原 PDF 对照说明。");
      if (kind === "correct_values") correctedNumericText(passage.text, corrections);
      const frozen = freezeNumericCommand(path, {
        ...data.source_binding,
        expected_revision: data.current?.revision ?? 0,
        kind,
        corrections: kind === "correct_values" ? corrections : [],
        reason: reason.trim(),
      });
      void send(frozen);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "请核对修正项。");
    }
  }

  function decide(action: "approve" | "reject" | "withdraw") {
    if (
      !data?.current ||
      command ||
      busy.current ||
      reading ||
      readError ||
      !data.permissions[`can_${action}`]
    )
      return;
    if (reviewReason.trim().length < 3) {
      setError("请填写独立核对或撤回的说明。");
      return;
    }
    void send(
      freezeNumericCommand(`${path}/decision`, {
        ...data.source_binding,
        expected_revision: data.current.revision,
        action,
        reason: reviewReason.trim(),
      }),
    );
  }

  const current = data?.current;
  const locked = !identityValid || writing || reading || Boolean(readError) || Boolean(command);
  const canEdit = Boolean(data?.permissions.can_propose) && !locked;
  let preview: string | null = null;
  let previewError: string | null = null;
  if (kind === "correct_values" && corrections.length) {
    try {
      preview = correctedNumericText(passage.text, corrections);
    } catch (cause) {
      previewError = cause instanceof Error ? cause.message : "请核对修正项。";
    }
  }

  return (
    <section className={styles.review} aria-label="本段数值复核">
      <Button
        type="button"
        className={styles.control}
        aria-expanded={open}
        aria-controls={`${id}-panel`}
        onClick={() => {
          if (!open && !data && !reading) void read();
          setOpen(!open);
        }}
      >
        数值复核
      </Button>
      {open ? (
        <div id={`${id}-panel`} className={styles.panel}>
          <header className={styles.header}>
            <h3>本段数值复核</h3>
            <span
              className={styles.status}
              data-status={readError ? "unreviewed" : (current?.status ?? "unreviewed")}
            >
              {readError
                ? "状态读取失败"
                : data
                  ? current
                    ? numericReviewLabel(current.status)
                    : "未复核"
                  : "尚未读取"}
            </span>
            <Button
              type="button"
              className={styles.control}
              loading={reading}
              disabled={writing}
              onClick={() => void read()}
            >
              刷新复核记录
            </Button>
          </header>
          <p className={styles.boundary}>
            原摘录保留。复核仅形成附注，不自动更新检索语料，不代表 OCR
            可信度、工程结论或现场作业许可。
          </p>
          {reading ? <p role="status">正在读取服务器复核记录…</p> : null}
          {readError ? (
            <p role="alert" className={styles.error}>
              {readError} 可使用“刷新复核记录”重试。
            </p>
          ) : null}
          {data && identityValid ? (
            <>
              <dl className={styles.metadata}>
                <div>
                  <dt>原件版本</dt>
                  <dd>
                    {data.source_binding.expected_document_version} · 第 {data.page_number} 页
                  </dd>
                </div>
                <div>
                  <dt>当前修订</dt>
                  <dd>{current?.revision ?? 0}</dd>
                </div>
                {current ? (
                  <>
                    <div>
                      <dt>提议人 / 服务器时间</dt>
                      <dd>
                        {current.proposed_by} ·{" "}
                        <time dateTime={current.proposed_at}>
                          {serverTime(current.proposed_at)}
                        </time>
                      </dd>
                    </div>
                    <div>
                      <dt>复核人 / 服务器时间</dt>
                      <dd>
                        {current.reviewed_by ?? "尚无"}
                        {current.reviewed_at ? (
                          <>
                            {" "}
                            ·{" "}
                            <time dateTime={current.reviewed_at}>
                              {serverTime(current.reviewed_at)}
                            </time>
                          </>
                        ) : null}
                      </dd>
                    </div>
                  </>
                ) : null}
              </dl>
              <details className={styles.details}>
                <summary>来源身份</summary>
                <dl className={styles.hashes}>
                  {Object.entries(data.source_binding).map(([key, value]) => (
                    <div key={key}>
                      <dt>{key}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              </details>
              {current ? (
                <div className={styles.record}>
                  <h4>
                    {readError
                      ? "上次读取的附注（当前状态未核验）"
                      : current.status === "approved"
                        ? "已独立复核的附注"
                        : "当前提议（未生效为已复核附注）"}
                  </h4>
                  <p>
                    {current.kind === "confirm_original" ? "确认本段原数值" : "修正数值或单位"} ·{" "}
                    {current.reason}
                  </p>
                  <pre>{current.corrected_text}</pre>
                  {current.review_reason ? <p>审核说明：{current.review_reason}</p> : null}
                </div>
              ) : null}
              <label className={styles.field} htmlFor={`${id}-source`}>
                原摘录（选择数值或单位）
                <textarea
                  id={`${id}-source`}
                  aria-label="原摘录（选择数值或单位）"
                  ref={sourceInput}
                  readOnly
                  value={passage.text}
                  rows={4}
                  onSelect={captureSelection}
                  onKeyUp={captureSelection}
                  onMouseUp={captureSelection}
                />
              </label>
              <p className={styles.hint}>
                从原摘录选择需要修正的数值；单位另行定位。也可用键盘 Shift + 方向键选择。
              </p>
              <fieldset disabled={!canEdit} className={styles.form}>
                <legend>{current ? "提出新修订" : "提交提议"}</legend>
                <div className={styles.choices}>
                  <label>
                    <input
                      type="radio"
                      name={`${id}-kind`}
                      checked={kind === "confirm_original"}
                      onChange={() => setKind("confirm_original")}
                    />
                    确认原数值
                  </label>
                  <label>
                    <input
                      type="radio"
                      name={`${id}-kind`}
                      checked={kind === "correct_values"}
                      onChange={() => setKind("correct_values")}
                    />
                    修正数值或单位
                  </label>
                </div>
                {kind === "correct_values" ? (
                  <>
                    <div className={styles.actions}>
                      <Button
                        type="button"
                        className={styles.control}
                        disabled={!canEdit || !selection || corrections.length >= 40}
                        onClick={addCorrection}
                      >
                        添加所选数值
                      </Button>
                      <span className={styles.hint}>
                        {selection
                          ? `选中“${selection.original}” · 字符 ${selection.start}–${selection.end}`
                          : "尚未选择数值"}
                      </span>
                    </div>
                    {corrections.map((item, index) => (
                      <fieldset
                        className={styles.correction}
                        key={`${item.start}-${item.end}-${index}`}
                      >
                        <legend>修正 {index + 1}</legend>
                        <div className={styles.fields}>
                          <label className={styles.field}>
                            原数值
                            <input value={item.original_value} readOnly />
                          </label>
                          <label className={styles.field}>
                            修正后数值
                            <input
                              value={item.corrected_value}
                              maxLength={80}
                              onChange={(event) =>
                                updateCorrection(index, { corrected_value: event.target.value })
                              }
                            />
                          </label>
                          <label className={styles.field}>
                            原单位
                            <input
                              value={item.original_unit}
                              maxLength={32}
                              readOnly={item.unit_start !== null}
                              onChange={(event) =>
                                updateCorrection(index, {
                                  original_unit: event.target.value,
                                  corrected_unit: event.target.value,
                                })
                              }
                            />
                          </label>
                          <label className={styles.field}>
                            修正后单位
                            <input
                              value={item.corrected_unit}
                              maxLength={32}
                              readOnly={item.unit_start === null}
                              onChange={(event) =>
                                updateCorrection(index, { corrected_unit: event.target.value })
                              }
                            />
                          </label>
                        </div>
                        <p className={styles.hint}>
                          {item.unit_start === null
                            ? "单位未定位：仅记录相同的上下文单位，不改正文；无单位留空。"
                            : `单位已定位于原摘录字符 ${item.unit_start}–${item.unit_end}。单位变更不自动换算数值。`}
                        </p>
                        <div className={styles.actions}>
                          <Button
                            type="button"
                            className={styles.control}
                            disabled={!canEdit || !selection}
                            onClick={() => {
                              if (selection)
                                updateCorrection(index, {
                                  unit_start: selection.start,
                                  unit_end: selection.end,
                                  original_unit: selection.original,
                                  corrected_unit: selection.original,
                                });
                            }}
                          >
                            用选区定位单位
                          </Button>
                          {item.unit_start !== null ? (
                            <Button
                              type="button"
                              className={styles.control}
                              onClick={() =>
                                updateCorrection(index, {
                                  unit_start: null,
                                  unit_end: null,
                                  corrected_unit: item.original_unit,
                                })
                              }
                            >
                              移除单位定位
                            </Button>
                          ) : null}
                          <Button
                            type="button"
                            className={styles.control}
                            onClick={() =>
                              setCorrections(corrections.filter((_, row) => row !== index))
                            }
                          >
                            移除修正
                          </Button>
                        </div>
                      </fieldset>
                    ))}
                    {preview ? (
                      <div className={styles.preview}>
                        <h4>待提交预览</h4>
                        <pre>{preview}</pre>
                      </div>
                    ) : null}
                    {previewError ? <p className={styles.hint}>{previewError}</p> : null}
                  </>
                ) : null}
                <label className={styles.field}>
                  提议说明
                  <textarea
                    aria-label="提议说明"
                    rows={2}
                    maxLength={2000}
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                  />
                </label>
                <Button
                  type="button"
                  className={styles.control}
                  variant="primary"
                  loading={writing && Boolean(command) && !command?.path.endsWith("/decision")}
                  disabled={
                    !canEdit || reason.trim().length < 3 || (kind === "correct_values" && !preview)
                  }
                  onClick={propose}
                >
                  {kind === "confirm_original" ? "提交原数值确认" : "提交数值修正"}
                </Button>
                {!data.permissions.can_propose ? (
                  <p className={styles.hint}>当前身份没有提议权限。权限由服务器校验。</p>
                ) : null}
              </fieldset>
              {current ? (
                <fieldset className={styles.form} disabled={locked}>
                  <legend>独立审核</legend>
                  <p className={styles.hint}>
                    提议人不能批准或拒绝自己的提议。按钮按服务器当前权限开放。
                  </p>
                  <label className={styles.field}>
                    复核说明
                    <textarea
                      aria-label="复核说明"
                      rows={2}
                      maxLength={2000}
                      value={reviewReason}
                      onChange={(event) => setReviewReason(event.target.value)}
                    />
                  </label>
                  <div className={styles.actions}>
                    <Button
                      type="button"
                      className={styles.control}
                      variant="primary"
                      disabled={
                        locked || !data.permissions.can_approve || reviewReason.trim().length < 3
                      }
                      onClick={() => decide("approve")}
                    >
                      批准数值复核
                    </Button>
                    <Button
                      type="button"
                      className={styles.control}
                      disabled={
                        locked || !data.permissions.can_reject || reviewReason.trim().length < 3
                      }
                      onClick={() => decide("reject")}
                    >
                      拒绝提议
                    </Button>
                    <Button
                      type="button"
                      className={styles.control}
                      disabled={
                        locked || !data.permissions.can_withdraw || reviewReason.trim().length < 3
                      }
                      onClick={() => decide("withdraw")}
                    >
                      撤回提议
                    </Button>
                  </div>
                </fieldset>
              ) : null}
              <details className={styles.details}>
                <summary>审计历史（已载入 {data.history.length} 条）</summary>
                {data.history.length ? (
                  <ol className={styles.history}>
                    {data.history.map((event) => (
                      <li key={event.event_id}>
                        <strong>
                          修订 {event.revision} ·{" "}
                          {
                            { propose: "提出", approve: "批准", reject: "拒绝", withdraw: "撤回" }[
                              event.action
                            ]
                          }
                        </strong>
                        <p>
                          {event.actor} ·{" "}
                          <time dateTime={event.occurred_at}>{serverTime(event.occurred_at)}</time>
                        </p>
                        <p>{event.reason}</p>
                        <details>
                          <summary>本次快照</summary>
                          <pre>{JSON.stringify(event.snapshot, null, 2)}</pre>
                          <p className={styles.hash}>快照 SHA-256：{event.snapshot_sha256}</p>
                        </details>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p>尚无复核事件。</p>
                )}
                {data.next_cursor !== null ? (
                  <Button
                    type="button"
                    className={styles.control}
                    loading={reading}
                    disabled={writing}
                    onClick={() => void read(data.next_cursor ?? undefined)}
                  >
                    加载更早历史
                  </Button>
                ) : null}
              </details>
            </>
          ) : null}
          {error ? (
            <p role="alert" className={styles.error}>
              {error}
            </p>
          ) : null}
          {command && identityValid ? (
            <div className={styles.frozen}>
              <p>
                {writing
                  ? "正在提交，已锁定内容，避免重复发送。"
                  : "原提交内容与提交键已锁定。核验不会创建另一条命令。"}
              </p>
              <details>
                <summary>查看锁定的提交</summary>
                <p className={styles.hash}>提交键：{command.key}</p>
                <pre>{command.body}</pre>
              </details>
              {failureStatus === 409 || failureStatus === 422 ? (
                <Button
                  type="button"
                  className={styles.control}
                  disabled={reading || writing || Boolean(readError) || !reconciled}
                  onClick={() => {
                    if (data) {
                      pendingCommands.delete(identity);
                      pendingChanges.dispatchEvent(new Event("change"));
                      setCommand(null);
                      setFailureStatus(null);
                      setError(null);
                    }
                  }}
                >
                  核对后重新编辑
                </Button>
              ) : (
                <Button
                  type="button"
                  className={styles.control}
                  loading={writing}
                  disabled={reading || failureStatus === null}
                  onClick={() => void send(command)}
                >
                  核验同一提交
                </Button>
              )}
              {failureStatus === 409 ? (
                <p className={styles.hint}>
                  先刷新复核记录，比较当前修订与锁定提交，再明确重新编辑。
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
