import { OpenVigilApiError, createIdempotencyKey } from "./api-client.ts";
import type { NativeKnowledgePassage } from "./knowledge-source.ts";

export function numericReviewPendingScope(
  subject: string,
  passage: NativeKnowledgePassage,
): string {
  if (!subject.trim()) throw new Error("数值复核需要真实登录身份。");
  return JSON.stringify([
    subject,
    passage.passage_id,
    passage.document_version,
    passage.source_sha256,
    passage.text_sha256,
  ]);
}

export interface NumericCorrection {
  readonly start: number;
  readonly end: number;
  readonly original_value: string;
  readonly corrected_value: string;
  readonly original_unit: string;
  readonly corrected_unit: string;
  readonly unit_start: number | null;
  readonly unit_end: number | null;
}

export interface NumericSourceBinding {
  readonly expected_document_version: string;
  readonly expected_source_sha256: string;
  readonly expected_text_sha256: string;
  readonly expected_parser_version: string;
  readonly expected_locator_sha256: string;
}

export type NumericReviewStatus = "pending" | "approved" | "rejected" | "withdrawn";
export interface NumericReviewRecord {
  readonly review_id: string;
  readonly revision: number;
  readonly status: NumericReviewStatus;
  readonly kind: "confirm_original" | "correct_values";
  readonly corrections: readonly NumericCorrection[];
  readonly corrected_text: string;
  readonly reason: string;
  readonly proposed_by: string;
  readonly proposed_at: string;
  readonly reviewed_by: string | null;
  readonly reviewed_at: string | null;
  readonly review_reason: string | null;
}

export interface NumericReviewEvent {
  readonly event_id: string;
  readonly revision: number;
  readonly action: "propose" | "approve" | "reject" | "withdraw";
  readonly actor: string;
  readonly occurred_at: string;
  readonly reason: string;
  readonly snapshot_sha256: string;
  readonly snapshot: {
    readonly source_binding: NumericSourceBinding;
    readonly original_text: string;
    readonly current: NumericReviewRecord;
  };
}

export interface NumericReviewEnvelope {
  readonly passage_id: string;
  readonly document_id: string;
  readonly source_binding: NumericSourceBinding;
  readonly original_text: string;
  readonly page_number: number;
  readonly current: NumericReviewRecord | null;
  readonly history: readonly NumericReviewEvent[];
  readonly next_cursor: number | null;
  readonly permissions: {
    readonly can_propose: boolean;
    readonly can_approve: boolean;
    readonly can_reject: boolean;
    readonly can_withdraw: boolean;
  };
  readonly updates_retrieval_text: false;
  readonly authorizes_work: false;
}

export function numericReviewLabel(status?: NumericReviewStatus) {
  return { pending: "待独立复核", approved: "已独立复核", rejected: "已拒绝", withdrawn: "已撤回" }[
    status ?? "pending"
  ];
}

/** Preview derives from immutable source spans, never floating-point arithmetic. */
export function correctedNumericText(
  text: string,
  corrections: readonly NumericCorrection[],
): string {
  if (!corrections.length || corrections.length > 40)
    throw new Error("请添加 1 至 40 项数值修正。");
  const points = Array.from(text);
  const replacements: { start: number; end: number; value: string }[] = [];
  for (const correction of corrections) {
    const {
      start,
      end,
      original_value,
      corrected_value,
      original_unit,
      corrected_unit,
      unit_start,
      unit_end,
    } = correction;
    if (
      !Number.isInteger(start) ||
      !Number.isInteger(end) ||
      start < 0 ||
      end <= start ||
      end > points.length ||
      points.slice(start, end).join("") !== original_value
    )
      throw new Error("数值选区与原摘录不一致，请重新选择。");
    if (
      !original_value ||
      original_value.length > 80 ||
      !/^[0-9OoIl|+−\-.,eE]+$/.test(original_value) ||
      !/[0-9OoIl|]/.test(original_value)
    )
      throw new Error("选区应只包含数值；单位请单独定位。");
    if (
      corrected_value.length > 80 ||
      !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?$/.test(corrected_value)
    )
      throw new Error("修正后数值须为十进制或科学计数字符串，例如 +1.230 或 1.23e+02。");
    if (
      [original_unit, corrected_unit].some(
        (unit) =>
          unit.length > 32 ||
          unit !== unit.trim() ||
          Array.from(unit).some((char) => char.codePointAt(0)! < 32),
      )
    )
      throw new Error("单位须明确填写，最多 32 个字符；无单位时留空。");
    if (unit_start === null && unit_end === null) {
      if (original_unit !== corrected_unit)
        throw new Error("更改单位前请在原摘录中定位单位，不进行隐式换算。");
    } else {
      if (
        !Number.isInteger(unit_start) ||
        !Number.isInteger(unit_end) ||
        unit_start === null ||
        unit_end === null ||
        unit_start < 0 ||
        unit_end <= unit_start ||
        unit_end > points.length ||
        !original_unit ||
        !corrected_unit ||
        points.slice(unit_start, unit_end).join("") !== original_unit
      )
        throw new Error("单位选区与原摘录不一致，请重新选择。");
      replacements.push({ start: unit_start, end: unit_end, value: corrected_unit });
    }
    if (original_value === corrected_value && original_unit === corrected_unit)
      throw new Error("修正项必须改变数值或已定位的单位；确认不变请使用“确认原数值”。");
    replacements.push({ start, end, value: corrected_value });
  }
  replacements.sort((a, b) => a.start - b.start);
  if (replacements.some((item, index) => index > 0 && replacements[index - 1].end > item.start))
    throw new Error("数值和单位选区不能重叠。");
  for (const replacement of replacements.toReversed())
    points.splice(replacement.start, replacement.end - replacement.start, replacement.value);
  return points.join("");
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function text(value: unknown): value is string {
  return typeof value === "string";
}
function timestamp(value: unknown): value is string {
  return text(value) && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
}
const hash = (value: unknown) => text(value) && /^[a-f0-9]{64}$/.test(value);

function validBinding(value: unknown): value is NumericSourceBinding {
  return (
    record(value) &&
    text(value.expected_document_version) &&
    text(value.expected_parser_version) &&
    hash(value.expected_source_sha256) &&
    hash(value.expected_text_sha256) &&
    hash(value.expected_locator_sha256)
  );
}
function validCurrent(value: unknown, original: string): value is NumericReviewRecord {
  if (
    !record(value) ||
    !text(value.review_id) ||
    !Number.isInteger(value.revision) ||
    Number(value.revision) < 1 ||
    !["pending", "approved", "rejected", "withdrawn"].includes(String(value.status)) ||
    !["confirm_original", "correct_values"].includes(String(value.kind)) ||
    !Array.isArray(value.corrections) ||
    !text(value.corrected_text) ||
    !text(value.reason) ||
    !text(value.proposed_by) ||
    !timestamp(value.proposed_at) ||
    !(value.reviewed_by === null || text(value.reviewed_by)) ||
    !(value.reviewed_at === null || timestamp(value.reviewed_at)) ||
    !(value.review_reason === null || text(value.review_reason))
  )
    return false;
  if (
    value.status === "approved" &&
    (!value.reviewed_by || value.reviewed_by === value.proposed_by || !value.reviewed_at)
  )
    return false;
  try {
    return value.kind === "confirm_original"
      ? value.corrections.length === 0 && value.corrected_text === original
      : correctedNumericText(original, value.corrections as NumericCorrection[]) ===
          value.corrected_text;
  } catch {
    return false;
  }
}

/** Fail closed on stale identity or malformed state; never show a guessed approval. */
export function parseNumericReview(
  value: unknown,
  passage: NativeKnowledgePassage,
): NumericReviewEnvelope {
  const permissions = record(value) ? value.permissions : null;
  if (
    !record(value) ||
    value.passage_id !== passage.passage_id ||
    value.document_id !== passage.document_id ||
    value.original_text !== passage.text ||
    value.page_number !== passage.page_number ||
    !validBinding(value.source_binding) ||
    value.source_binding.expected_document_version !== passage.document_version ||
    value.source_binding.expected_source_sha256 !== passage.source_sha256 ||
    value.source_binding.expected_text_sha256 !== passage.text_sha256 ||
    value.updates_retrieval_text !== false ||
    value.authorizes_work !== false ||
    value.fixtureFallback !== false ||
    !(value.current === null || validCurrent(value.current, passage.text)) ||
    !record(permissions) ||
    !["can_propose", "can_approve", "can_reject", "can_withdraw"].every(
      (key) => typeof permissions[key] === "boolean",
    ) ||
    !Array.isArray(value.history) ||
    value.history.length > 50 ||
    !(
      value.next_cursor === null ||
      (Number.isInteger(value.next_cursor) && Number(value.next_cursor) > 0)
    )
  )
    throw new Error("复核响应或来源身份不匹配。请重新读取当前段落。");
  for (const event of value.history) {
    if (
      !record(event) ||
      !text(event.event_id) ||
      !Number.isInteger(event.revision) ||
      Number(event.revision) < 1 ||
      !["propose", "approve", "reject", "withdraw"].includes(String(event.action)) ||
      !text(event.actor) ||
      !timestamp(event.occurred_at) ||
      !text(event.reason) ||
      !hash(event.snapshot_sha256) ||
      !record(event.snapshot) ||
      !validBinding(event.snapshot.source_binding) ||
      event.snapshot.original_text !== passage.text ||
      !validCurrent(event.snapshot.current, passage.text) ||
      event.snapshot.current.revision !== event.revision
    )
      throw new Error("审计历史响应不完整，请重新读取。");
  }
  return value as unknown as NumericReviewEnvelope;
}

/** API offsets count Unicode code points. DOM selection offsets count UTF-16 units. */
export function numericSelection(text: string, start: number, end: number) {
  if (
    !Number.isInteger(start) ||
    !Number.isInteger(end) ||
    start < 0 ||
    start >= end ||
    end > text.length
  ) {
    throw new Error("请选择原摘录中的完整数值。");
  }
  const points = Array.from(text);
  const boundaries = new Map<number, number>([[0, 0]]);
  let utf16 = 0;
  points.forEach((point, index) => {
    utf16 += point.length;
    boundaries.set(utf16, index + 1);
  });
  const codepointStart = boundaries.get(start);
  const codepointEnd = boundaries.get(end);
  if (codepointStart === undefined || codepointEnd === undefined) {
    throw new Error("选区不能截断 Unicode 字符，请重新选择。");
  }
  return {
    start: codepointStart,
    end: codepointEnd,
    original: points.slice(codepointStart, codepointEnd).join(""),
  };
}

export interface FrozenNumericCommand {
  readonly path: string;
  readonly key: string;
  /** Serialized once so mutation of a form cannot change a retry. */
  readonly body: string;
}

export function freezeNumericCommand(path: string, payload: unknown): FrozenNumericCommand {
  return Object.freeze({
    path,
    key: createIdempotencyKey("numeric-review"),
    body: JSON.stringify(payload),
  });
}

/** Intentionally one attempt. An unknown result can only be retried explicitly with this object. */
export async function postNumericCommand(command: FrozenNumericCommand): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(command.path, {
      method: "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        "idempotency-key": command.key,
      },
      body: command.body,
    });
  } catch {
    throw new OpenVigilApiError(
      0,
      "COMMAND_RESULT_UNKNOWN",
      "提交结果未知。已保留完整提交内容，请核验同一提交。",
      null,
      command.key,
    );
  }
  let decoded: unknown;
  try {
    decoded = await response.json();
  } catch {
    throw new OpenVigilApiError(
      response.ok ? 0 : response.status,
      response.ok ? "COMMAND_RESULT_UNKNOWN" : "INVALID_API_RESPONSE",
      "服务响应无法读取。已保留原提交，请核验同一提交。",
      null,
      command.key,
    );
  }
  if (!response.ok) {
    const error =
      typeof decoded === "object" && decoded !== null && "error" in decoded ? decoded.error : null;
    const details =
      typeof error === "object" && error !== null ? (error as Record<string, unknown>) : {};
    throw new OpenVigilApiError(
      response.status,
      typeof details.code === "string" ? details.code : "API_ERROR",
      typeof details.message === "string"
        ? details.message
        : `服务拒绝提交（${response.status}）。`,
      null,
      command.key,
    );
  }
  return decoded;
}
