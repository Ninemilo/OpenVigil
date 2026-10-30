import { expect, test, type Page } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

// Explicit transport fixtures verify browser behavior only, not worker or service acceptance.
async function healthySnapshot(page: Page) {
  const observedAt = await page.evaluate(() => new Date().toISOString());
  return {
    fixture: true,
    docling_pdf_configured: true,
    max_artifact_bytes: 52428800,
    worker_health_verified: true,
    worker: {
      status: "ready",
      heartbeat_verified: true,
      runtime_ready: true,
      observed_at: observedAt,
      last_heartbeat_age_seconds: 2,
      heartbeat_ttl_seconds: 45,
      reason_codes: [] as string[],
    },
    queue: {
      observed_at: observedAt,
      scope: "authorized_documents",
      waiting: 2,
      running: 1,
      retrying: 0,
      expired_leases: 0,
      overdue_running: 0,
      failed_last_24h: 0,
      oldest_waiting_age_seconds: 12,
      oldest_running_age_seconds: 5,
      backlogged: false,
      backlog_warning_seconds: 300,
      execution_timeout_seconds: 900,
      recent_failures: [] as {
        document_id: string;
        error_code: string;
        attempts: number;
        age_seconds: number;
        terminal: boolean;
      }[],
      recent_failure_limit: 10,
    },
  };
}

test.beforeEach(async ({ context, page }) => {
  await context.setExtraHTTPHeaders({
    "oai-authenticated-user-id": "user-manager",
    "oai-authenticated-user-email": "manager@example.com",
    "x-e2e-runtime": "production",
  });
  await page.route("**/api/knowledge-documents*", (route) =>
    route.fulfill({
      json: { ok: true, data: { documents: [] }, error: null, meta: { fixture: true } },
    }),
  );
});

test("configuration loading, legacy and offline states never invent readiness or block configured uploads", async ({
  page,
}) => {
  let releaseRead: (() => void) | undefined;
  const reading = new Promise<void>((resolve) => {
    releaseRead = resolve;
  });
  let state = "legacy";
  await page.route("**/api/backend/knowledge/parser-capabilities", async (route) => {
    await reading;
    const snapshot = await healthySnapshot(page);
    if (state === "legacy")
      return route.fulfill({
        json: { docling_pdf_configured: true, max_artifact_bytes: 52428800, fixture: true },
      });
    snapshot.worker.status = state;
    snapshot.worker_health_verified = false;
    snapshot.worker.heartbeat_verified = false;
    return route.fulfill({ json: snapshot });
  });
  await page.goto("/knowledge");
  const health = page.getByRole("region", { name: "扫描件解析运行状态" });
  const checkbox = page.getByRole("checkbox", { name: "PDF 扫描件解析" });
  await expect(health.getByText("正在读取", { exact: true })).toBeVisible();
  await expect(checkbox).toBeDisabled();
  await expect(health.locator("dd")).toHaveCount(0);
  releaseRead?.();
  await expect(health.getByText("worker 尚未验证", { exact: true })).toBeVisible();
  await expect(checkbox).toBeEnabled();
  await expect(health.getByText("队列数量尚未确认", { exact: true })).toBeVisible();
  state = "offline";
  await health.getByRole("button", { name: "刷新解析状态" }).click();
  await expect(health.getByText("worker 离线", { exact: true })).toBeVisible();
  await checkbox.check();
  await expect(page.getByRole("button", { name: "上传知识制品" })).toBeEnabled();
  await expect(health.locator(".status-badge--success")).toHaveCount(0);
});

test("periodic failure discards old green status and queue numbers, then recovers through the existing retry action", async ({
  page,
}) => {
  let fail = false;
  let reads = 0;
  await page.clock.install();
  await page.route("**/api/backend/knowledge/parser-capabilities", async (route) => {
    reads++;
    return fail
      ? route.fulfill({
          status: 503,
          json: {
            error: { code: "DECLARED_HEALTH_FIXTURE", message: "Declared health read failure" },
          },
        })
      : route.fulfill({ json: await healthySnapshot(page) });
  });
  await page.goto("/knowledge");
  const health = page.getByRole("region", { name: "扫描件解析运行状态" });
  await expect(health.getByText("可处理扫描件", { exact: true })).toBeVisible();
  await health.locator("summary").click();
  await expect(health.locator("dd").first()).toHaveText("2");
  const initialReads = reads;
  fail = true;
  await page.clock.fastForward(15_000);
  await expect.poll(() => reads).toBeGreaterThan(initialReads);
  await page.clock.fastForward(2_000);
  await expect(health.getByText("状态读取失败", { exact: true })).toBeVisible();
  await expect(health.locator(".status-badge--success")).toHaveCount(0);
  await expect(health.locator("dd")).toHaveCount(0);
  await expect(health.locator("details")).toHaveAttribute("open", "");
  fail = false;
  await health.getByRole("button", { name: "重试解析配置" }).click();
  await expect(health.getByText("可处理扫描件", { exact: true })).toBeVisible();
  await expect(health.locator("dd").first()).toHaveText("2");
  await expect(health.locator("details")).toHaveAttribute("open", "");
});

test("hidden page withdraws success and returning to the foreground requires a new completed read", async ({
  page,
}) => {
  let releaseRead: (() => void) | undefined;
  let hold = false;
  const pending = new Promise<void>((resolve) => {
    releaseRead = resolve;
  });
  await page.route("**/api/backend/knowledge/parser-capabilities", async (route) => {
    if (hold) await pending;
    return route.fulfill({ json: await healthySnapshot(page) });
  });
  await page.goto("/knowledge");
  const health = page.getByRole("region", { name: "扫描件解析运行状态" });
  await expect(health.getByText("可处理扫描件", { exact: true })).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(health.getByText("后台已暂停更新", { exact: true })).toBeVisible();
  await expect(health.locator(".status-badge--success")).toHaveCount(0);
  hold = true;
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(health.getByText("正在刷新", { exact: true })).toBeVisible();
  await expect(health.locator(".status-badge--success")).toHaveCount(0);
  await expect(health.getByRole("button", { name: "刷新解析状态" })).toBeDisabled();
  releaseRead?.();
  await expect(health.getByText("可处理扫描件", { exact: true })).toBeVisible();
});

test("old observations and heartbeat expiry cannot render success even after a successful HTTP response", async ({
  page,
}) => {
  let staleSnapshot = true;
  await page.clock.install();
  await page.route("**/api/backend/knowledge/parser-capabilities", async (route) => {
    const snapshot = await healthySnapshot(page);
    if (staleSnapshot)
      snapshot.worker.observed_at = new Date(
        Date.parse(snapshot.worker.observed_at) - 50_000,
      ).toISOString();
    else snapshot.worker.last_heartbeat_age_seconds = 44.5;
    return route.fulfill({ json: snapshot });
  });
  await page.goto("/knowledge");
  const health = page.getByRole("region", { name: "扫描件解析运行状态" });
  await expect(health.getByText("状态已过期", { exact: true })).toBeVisible();
  await expect(health.locator("dd")).toHaveCount(0);
  staleSnapshot = false;
  await health.getByRole("button", { name: "刷新解析状态" }).click();
  await expect(health.getByText("可处理扫描件", { exact: true })).toBeVisible();
  await page.clock.fastForward(1_100);
  await expect(health.getByText("心跳已过期", { exact: true })).toBeVisible();
  await expect(health.locator(".status-badge--success")).toHaveCount(0);
});

test("scoped queue details expose backlog and safe failure reasons with usable narrow-screen controls", async ({
  page,
}) => {
  let expired = false;
  await page.setViewportSize({ width: 375, height: 812 });
  await page.route("**/api/backend/knowledge/parser-capabilities", async (route) => {
    const snapshot = await healthySnapshot(page);
    snapshot.queue.backlogged = true;
    snapshot.queue.oldest_waiting_age_seconds = 480;
    snapshot.queue.failed_last_24h = 1;
    snapshot.queue.overdue_running = expired ? 1 : 0;
    snapshot.queue.recent_failures = [
      {
        document_id: "KB-SYNTHETIC-FAILURE",
        error_code: "TimeoutError",
        attempts: 3,
        age_seconds: 12,
        terminal: true,
      },
    ];
    return route.fulfill({ json: snapshot });
  });
  await page.goto("/knowledge");
  const health = page.getByRole("region", { name: "扫描件解析运行状态" });
  await expect(health.getByText("解析队列积压", { exact: true })).toBeVisible();
  await health.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(health.getByText("解析超时 · 已终止 · 已尝试 3 次", { exact: true })).toBeVisible();
  await expect(health.getByRole("link", { name: "KB-SYNTHETIC-FAILURE" })).toHaveAttribute(
    "href",
    "/knowledge?document=KB-SYNTHETIC-FAILURE",
  );
  await expect(health.getByText(/最长等待 8 分钟/)).toBeVisible();
  const refresh = health.getByRole("button", { name: "刷新解析状态" });
  const box = await refresh.boundingBox();
  expect(box?.height).toBeGreaterThanOrEqual(44);
  expect(await health.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  expired = true;
  await refresh.click();
  await expect(health.getByText("任务运行需检查", { exact: true })).toBeVisible();
  await expect(health.locator(".status-badge--success")).toHaveCount(0);
  for (const width of [320, 375, 414, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await health.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(
      true,
    );
    const bounds = await refresh.boundingBox();
    expect(bounds?.height).toBeGreaterThanOrEqual(44);
    expect(bounds?.x).toBeGreaterThanOrEqual(0);
    expect((bounds?.x ?? 0) + (bounds?.width ?? 0)).toBeLessThanOrEqual(width);
    await health.screenshot({ path: `.artifacts/improvements-20261023/ocr-health-${width}.png` });
  }
});

test("configuration withdrawal and worker checks remain distinct, with a standalone state preview", async ({
  page,
}) => {
  const states = [
    "unconfigured",
    "unverified",
    "starting",
    "ready",
    "backlogged",
    "degraded",
    "version_mismatch",
    "stale",
  ];
  const labels = [
    "扫描件解析未配置",
    "worker 尚未验证",
    "worker 正在准备或复检",
    "可处理扫描件",
    "解析队列积压",
    "解析运行检查未通过",
    "解析版本不匹配",
    "状态已过期",
  ];
  let index = 0;
  await page.route("**/api/backend/knowledge/parser-capabilities", async (route) => {
    const snapshot = await healthySnapshot(page);
    const state = states[index];
    snapshot.docling_pdf_configured = state !== "unconfigured";
    if (["unconfigured", "starting", "degraded", "version_mismatch"].includes(state))
      snapshot.worker.status = state;
    if (state === "unverified") snapshot.worker_health_verified = false;
    if (state === "backlogged") {
      snapshot.queue.backlogged = true;
      snapshot.queue.oldest_waiting_age_seconds = 600;
    }
    if (state === "stale")
      snapshot.worker.observed_at = new Date(
        Date.parse(snapshot.worker.observed_at) - 50_000,
      ).toISOString();
    return route.fulfill({ json: snapshot });
  });
  await page.goto("/knowledge");
  const health = page.getByRole("region", { name: "扫描件解析运行状态" });
  const checkbox = page.getByRole("checkbox", { name: "PDF 扫描件解析" });
  const previews: string[] = [];
  for (index = 0; index < states.length; index++) {
    if (index > 0) await health.getByRole("button", { name: "刷新解析状态" }).click();
    await expect(health.getByText(labels[index], { exact: true })).toBeVisible();
    if (states[index] === "unconfigured") await expect(checkbox).toBeDisabled();
    else await expect(checkbox).toBeEnabled();
    await expect(health.locator(".status-badge--success")).toHaveCount(
      states[index] === "ready" ? 1 : 0,
    );
    previews.push(await health.evaluate((element) => element.outerHTML));
  }
  index = 0;
  await health.getByRole("button", { name: "刷新解析状态" }).click();
  await expect(checkbox).toBeDisabled();
  const styles = await page.evaluate(() =>
    Array.from(document.styleSheets, (sheet) =>
      Array.from(sheet.cssRules, (rule) => rule.cssText).join("\n"),
    ).join("\n"),
  );
  const previewPath = resolve(
    ".artifacts/improvements-20261023/KnowledgeParserHealth.preview.html",
  );
  await mkdir(resolve(".artifacts/improvements-20261023"), { recursive: true });
  await writeFile(
    previewPath,
    `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OCR健康状态组件预览</title><style>${styles}</style><body><main style="max-width:960px;margin:24px auto;padding:16px"><h1>扫描件解析运行状态</h1><p>明确的浏览器夹具：8种实际渲染状态的静态预览。按钮不连接服务，此文件不是产品代码。</p>${previews.join("\n")}</main></body></html>`,
    "utf8",
  );
  await page.goto(pathToFileURL(previewPath).href);
  await expect(page.getByRole("region", { name: "扫描件解析运行状态" })).toHaveCount(8);
  await page.screenshot({
    path: ".artifacts/improvements-20261023/ocr-health-preview.png",
    fullPage: true,
  });
});
