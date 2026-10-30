export const PARSER_HEALTH_REFRESH_MS = 15_000;
export const PARSER_HEALTH_MAX_AGE_MS = 45_000;

export interface ParserWorkerHealth {
  readonly status:
    | "unconfigured"
    | "unavailable"
    | "offline"
    | "starting"
    | "ready"
    | "degraded"
    | "version_mismatch";
  readonly heartbeat_verified: boolean;
  readonly runtime_ready: boolean;
  readonly observed_at: string;
  readonly last_heartbeat_age_seconds: number | null;
  readonly heartbeat_ttl_seconds: number;
  readonly reason_codes: readonly string[];
}

export interface ParserQueueHealth {
  readonly observed_at: string;
  readonly scope: "authorized_documents";
  readonly waiting: number;
  readonly running: number;
  readonly retrying: number;
  readonly expired_leases: number;
  readonly overdue_running: number;
  readonly failed_last_24h: number;
  readonly oldest_waiting_age_seconds: number | null;
  readonly oldest_running_age_seconds: number | null;
  readonly backlogged: boolean;
  readonly backlog_warning_seconds: number;
  readonly execution_timeout_seconds: number;
  readonly recent_failures: readonly {
    readonly document_id: string;
    readonly error_code: string;
    readonly attempts: number;
    readonly age_seconds: number | null;
    readonly terminal: boolean;
  }[];
  readonly recent_failure_limit: number;
}

export interface KnowledgeParserCapabilities {
  readonly docling_pdf_configured: boolean;
  readonly max_artifact_bytes: number;
  readonly worker_health_verified?: boolean;
  readonly worker?: ParserWorkerHealth;
  readonly queue?: ParserQueueHealth;
}

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const count = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const duration = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;
const age = (value: unknown): value is number | null => value === null || duration(value);
const timestamp = (value: unknown): value is string =>
  typeof value === "string" &&
  /(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
  Number.isFinite(Date.parse(value));

// Accept the older configuration-only response, but never infer worker health from it.
export function parseKnowledgeParserCapabilities(value: unknown): KnowledgeParserCapabilities {
  if (
    !object(value) ||
    typeof value.docling_pdf_configured !== "boolean" ||
    !count(value.max_artifact_bytes) ||
    value.max_artifact_bytes === 0 ||
    (value.worker_health_verified !== undefined &&
      typeof value.worker_health_verified !== "boolean")
  )
    throw new Error("解析配置响应不完整，请重新获取。");
  const worker = value.worker;
  if (
    worker !== undefined &&
    (!object(worker) ||
      ![
        "unconfigured",
        "unavailable",
        "offline",
        "starting",
        "ready",
        "degraded",
        "version_mismatch",
      ].includes(String(worker.status)) ||
      typeof worker.heartbeat_verified !== "boolean" ||
      typeof worker.runtime_ready !== "boolean" ||
      !timestamp(worker.observed_at) ||
      !age(worker.last_heartbeat_age_seconds) ||
      !duration(worker.heartbeat_ttl_seconds) ||
      worker.heartbeat_ttl_seconds === 0 ||
      !Array.isArray(worker.reason_codes) ||
      !worker.reason_codes.every((code) => typeof code === "string"))
  )
    throw new Error("解析运行状态响应不完整，请重新获取。");
  const queue = value.queue;
  if (
    queue !== undefined &&
    (!object(queue) ||
      !timestamp(queue.observed_at) ||
      queue.scope !== "authorized_documents" ||
      ![
        "waiting",
        "running",
        "retrying",
        "expired_leases",
        "overdue_running",
        "failed_last_24h",
        "recent_failure_limit",
      ].every((key) => count(queue[key])) ||
      !age(queue.oldest_waiting_age_seconds) ||
      !age(queue.oldest_running_age_seconds) ||
      typeof queue.backlogged !== "boolean" ||
      !duration(queue.backlog_warning_seconds) ||
      !duration(queue.execution_timeout_seconds) ||
      !Array.isArray(queue.recent_failures) ||
      queue.recent_failures.length > Number(queue.recent_failure_limit) ||
      !queue.recent_failures.every(
        (failure) =>
          object(failure) &&
          typeof failure.document_id === "string" &&
          failure.document_id.length > 0 &&
          typeof failure.error_code === "string" &&
          count(failure.attempts) &&
          age(failure.age_seconds) &&
          typeof failure.terminal === "boolean",
      ))
  )
    throw new Error("解析队列响应不完整，请重新获取。");
  return value as unknown as KnowledgeParserCapabilities;
}

interface HealthObservation {
  readonly data?: KnowledgeParserCapabilities;
  readonly dataUpdatedAt: number;
  readonly now: number;
  readonly visible: boolean;
  readonly refreshAfter: number;
  readonly fetching: boolean;
  readonly failed: boolean;
}

export interface ParserHealthPresentation {
  readonly state: string;
  readonly label: string;
  readonly tone: "neutral" | "info" | "success" | "warning" | "critical";
  readonly description: string;
  readonly queue: ParserQueueHealth | null;
}

function fresh(observedAt: string, now: number): boolean {
  const elapsed = now - Date.parse(observedAt);
  return elapsed >= -5_000 && elapsed < PARSER_HEALTH_MAX_AGE_MS;
}

export function parserHealthPresentation(observation: HealthObservation): ParserHealthPresentation {
  const { data, now, visible, refreshAfter, dataUpdatedAt, fetching, failed } = observation;
  const result = (
    state: string,
    label: string,
    tone: ParserHealthPresentation["tone"],
    description: string,
    queue: ParserQueueHealth | null = null,
  ): ParserHealthPresentation => ({ state, label, tone, description, queue });
  if (!visible)
    return result("paused", "后台已暂停更新", "neutral", "返回页面后重新读取运行状态。");
  if (fetching)
    return result(
      data ? "refreshing" : "loading",
      data ? "正在刷新" : "正在读取",
      "neutral",
      "正在核对解析配置、worker 心跳与文档队列。",
    );
  if (failed)
    return result(
      "unavailable",
      "状态读取失败",
      "warning",
      "连接恢复后请重试。普通文字文档仍可上传。",
    );
  if (!data) return result("loading", "正在读取", "neutral", "尚未取得解析配置和运行状态。");
  if (
    dataUpdatedAt <= refreshAfter ||
    now - dataUpdatedAt >= PARSER_HEALTH_MAX_AGE_MS ||
    now < dataUpdatedAt - 5_000
  )
    return result("stale", "状态已过期", "warning", "当前运行情况尚未确认，请刷新后查看。");
  const queue = data.queue && fresh(data.queue.observed_at, now) ? data.queue : null;
  if (!data.docling_pdf_configured)
    return result(
      "unconfigured",
      "扫描件解析未配置",
      "neutral",
      "普通文字文档仍可上传；启用扫描件解析需要配置后台服务。",
      queue,
    );
  const wait = "已配置扫描件上传；任务将等待可用的后台解析服务。";
  const worker = data.worker;
  if (!worker) return result("unverified", "worker 尚未验证", "warning", wait, queue);
  if (!fresh(worker.observed_at, now) || (data.queue && !queue))
    return result("stale", "状态已过期", "warning", "当前运行情况尚未确认，请刷新后查看。");
  if (worker.status === "version_mismatch")
    return result(
      "version_mismatch",
      "解析版本不匹配",
      "critical",
      "后台解析版本与当前配置不一致，需要管理员核对部署。",
      queue,
    );
  if (worker.status === "degraded")
    return result(
      "degraded",
      "解析运行检查未通过",
      "critical",
      "后台模型或解析运行检查未通过，请联系管理员。已提交的任务保留在队列中。",
      queue,
    );
  if (worker.status === "offline") return result("offline", "worker 离线", "warning", wait, queue);
  if (worker.status === "starting")
    return result(
      "starting",
      "worker 正在准备或复检",
      "info",
      "后台正在准备或定期复检解析环境，已提交的任务等待就绪。",
      queue,
    );
  if (
    worker.status !== "ready" ||
    !data.worker_health_verified ||
    !worker.heartbeat_verified ||
    !worker.runtime_ready ||
    worker.last_heartbeat_age_seconds === null
  )
    return result("unverified", "worker 尚未验证", "warning", wait, queue);
  const heartbeatAge =
    worker.last_heartbeat_age_seconds + Math.max(0, now - Date.parse(worker.observed_at)) / 1000;
  if (heartbeatAge >= worker.heartbeat_ttl_seconds)
    return result(
      "stale",
      "心跳已过期",
      "warning",
      "最近一次心跳已超过有效期，请刷新确认 worker 状态。",
      queue,
    );
  if (!queue)
    return result(
      "unverified",
      "队列状态尚未验证",
      "warning",
      "worker 已通过检查，但尚未取得当前可访问文档的队列状态。",
    );
  if (queue.expired_leases > 0 || queue.overdue_running > 0)
    return result(
      "fault",
      "任务运行需检查",
      "warning",
      "有任务领取后的总历时超过阈值，或租约已过期；请核对处理进度，后台会检查恢复条件。",
      queue,
    );
  if (queue.backlogged)
    return result(
      "backlogged",
      "解析队列积压",
      "warning",
      "worker 可处理任务，但部分文档等待时间已超过阈值。",
      queue,
    );
  if (queue.failed_last_24h > 0 || queue.retrying > 0)
    return result(
      "failures",
      "队列有失败或重试",
      "warning",
      "worker 可处理任务，请检查失败文档及其重试状态。",
      queue,
    );
  return result(
    "ready",
    "可处理扫描件",
    "success",
    "worker 心跳与解析环境已验证，队列未发现积压或运行时长预警。",
    queue,
  );
}

export function parserDurationLabel(seconds: number | null): string {
  if (seconds === null) return "暂无记录";
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟`;
}

export function parserFailureLabel(code: string): string {
  const labels: Record<string, string> = {
    TimeoutError: "解析超时",
    DocumentParseFailure: "解析失败",
    DocumentParserIdentityMismatch: "解析版本不匹配",
    DocumentParseAttemptsExhausted: "已达到最大尝试次数",
    ValueError: "原文或解析结果未通过校验",
  };
  return labels[code] ?? "解析未完成，请查看文档状态";
}
