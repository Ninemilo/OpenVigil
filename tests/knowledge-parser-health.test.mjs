import assert from "node:assert/strict";
import test from "node:test";

import {
  parseKnowledgeParserCapabilities,
  parserDurationLabel,
  parserFailureLabel,
  parserHealthPresentation,
} from "../lib/knowledge-parser-health.ts";

const now = Date.parse("2026-10-23T10:00:00Z");
const capabilities = () => ({
  docling_pdf_configured: true,
  max_artifact_bytes: 50 * 1024 * 1024,
  worker_health_verified: true,
  worker: {
    status: "ready",
    heartbeat_verified: true,
    runtime_ready: true,
    observed_at: new Date(now).toISOString(),
    last_heartbeat_age_seconds: 5,
    heartbeat_ttl_seconds: 45,
    reason_codes: [],
  },
  queue: {
    observed_at: new Date(now).toISOString(),
    scope: "authorized_documents",
    waiting: 0,
    running: 0,
    retrying: 0,
    expired_leases: 0,
    overdue_running: 0,
    failed_last_24h: 0,
    oldest_waiting_age_seconds: null,
    oldest_running_age_seconds: null,
    backlogged: false,
    backlog_warning_seconds: 300,
    execution_timeout_seconds: 900,
    recent_failures: [],
    recent_failure_limit: 10,
  },
});
const present = (data, overrides = {}) =>
  parserHealthPresentation({
    data: parseKnowledgeParserCapabilities(data),
    now,
    dataUpdatedAt: now,
    visible: true,
    refreshAfter: 0,
    fetching: false,
    failed: false,
    ...overrides,
  });

test("configured legacy response keeps uploads available without inventing a healthy worker or zero queue", () => {
  const legacy = { docling_pdf_configured: true, max_artifact_bytes: 52428800 };
  assert.equal(parseKnowledgeParserCapabilities(legacy).docling_pdf_configured, true);
  assert.equal(present(legacy).state, "unverified");
  assert.equal(present(legacy).queue, null);
  assert.equal(present({ ...legacy, docling_pdf_configured: false }).state, "unconfigured");
});

test("only verified recent worker and scoped queue evidence can render success", () => {
  assert.equal(present(capabilities()).tone, "success");
  for (const key of ["heartbeat_verified", "runtime_ready"]) {
    const data = capabilities();
    data.worker[key] = false;
    assert.notEqual(present(data).tone, "success", key);
  }
  const data = capabilities();
  data.worker_health_verified = false;
  assert.notEqual(present(data).tone, "success");
  delete data.queue;
  assert.equal(present(data).queue, null);
  assert.notEqual(present(data).tone, "success");
});

test("prior success is withdrawn while loading, failing, hidden or waiting for a foreground refresh", () => {
  for (const observation of [
    { fetching: true },
    { failed: true },
    { visible: false },
    { refreshAfter: now },
    { dataUpdatedAt: now - 45000 },
    { dataUpdatedAt: now + 6000 },
  ]) {
    const result = present(capabilities(), observation);
    assert.notEqual(result.tone, "success", JSON.stringify(observation));
    assert.equal(result.queue, null, "stale queue numbers must not look current");
  }
});

test("stale server snapshots cannot become healthy merely by being fetched again", () => {
  for (const scope of ["worker", "queue"]) {
    for (const offset of [-45000, 6000]) {
      const data = capabilities();
      data[scope].observed_at = new Date(now + offset).toISOString();
      assert.equal(present(data).state, "stale", `${scope} ${offset}`);
      assert.equal(present(data).queue, null);
    }
  }
});

test("a nearly expired heartbeat stops showing ready before the next polling interval", () => {
  const data = capabilities();
  data.worker.last_heartbeat_age_seconds = 44;
  assert.equal(present(data).state, "ready");
  assert.equal(present(data, { now: now + 1000 }).state, "stale");
  assert.notEqual(present(data, { now: now + 1000 }).tone, "success");
  data.worker.last_heartbeat_age_seconds = null;
  assert.equal(present(data).state, "unverified");
});

test("offline, startup, runtime failure and version mismatch are distinct and never green", () => {
  for (const state of [
    "offline",
    "starting",
    "degraded",
    "version_mismatch",
    "unavailable",
    "unconfigured",
  ]) {
    const data = capabilities();
    data.worker.status = state;
    const result = present(data);
    assert.notEqual(result.tone, "success", state);
    assert.equal(
      data.docling_pdf_configured,
      true,
      "worker status does not revoke configured durable upload",
    );
  }
});

test("backlog, lease expiry, execution timeout and recent failures remain visible despite a ready worker", () => {
  for (const [field, value, expected] of [
    ["backlogged", true, "backlogged"],
    ["expired_leases", 1, "fault"],
    ["overdue_running", 1, "fault"],
    ["failed_last_24h", 1, "failures"],
    ["retrying", 1, "failures"],
  ]) {
    const data = capabilities();
    data.queue[field] = value;
    assert.equal(present(data).state, expected);
    assert.equal(present(data).queue[field], value);
    assert.notEqual(present(data).tone, "success");
  }
});

test("incomplete, out-of-scope and malformed metrics fail closed instead of coercing to zero", () => {
  const mutations = [
    (data) => delete data.queue.waiting,
    (data) => {
      data.queue.waiting = "0";
    },
    (data) => {
      data.queue.waiting = -1;
    },
    (data) => {
      data.queue.scope = "all_tenants";
    },
    (data) => {
      data.queue.oldest_waiting_age_seconds = undefined;
    },
    (data) => {
      data.worker.heartbeat_ttl_seconds = 0;
    },
    (data) => {
      data.worker.observed_at = "2026-10-23T10:00:00";
    },
    (data) => {
      data.worker.last_heartbeat_age_seconds = Number.NaN;
    },
    (data) => {
      data.worker.status = "invented";
    },
    (data) => {
      data.worker_health_verified = "true";
    },
  ];
  for (const mutate of mutations) {
    const data = capabilities();
    mutate(data);
    assert.throws(() => parseKnowledgeParserCapabilities(data));
  }
});

test("recent failures have validated shape and unknown errors do not expose internal details", () => {
  const data = capabilities();
  data.queue.recent_failures = [
    {
      document_id: "fixture-document",
      error_code: "TimeoutError",
      attempts: 3,
      age_seconds: 1,
      terminal: true,
    },
  ];
  assert.equal(present(data).queue.recent_failures[0].attempts, 3);
  assert.equal(parserFailureLabel("TimeoutError"), "解析超时");
  assert.doesNotMatch(
    parserFailureLabel("/private/server/path?token=secret"),
    /private|token|secret/,
  );
  data.queue.recent_failure_limit = 0;
  assert.throws(() => parseKnowledgeParserCapabilities(data));
});

test("missing task ages are distinct from a measured zero and long durations remain legible", () => {
  assert.equal(parserDurationLabel(null), "暂无记录");
  assert.equal(parserDurationLabel(0), "0 秒");
  assert.equal(parserDurationLabel(59.9), "59 秒");
  assert.equal(parserDurationLabel(121), "2 分钟");
  assert.equal(parserDurationLabel(3661), "1 小时 1 分钟");
});
