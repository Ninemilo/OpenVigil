import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import type { NativeKnowledgePassage } from "../../lib/knowledge-source";

// Synthetic transport fixtures, with real PDF.js workers and actual canvas pixels.
// These tests provide neither real service nor field-document/OCR accuracy acceptance.
const documentId = "KB-PDF-GEOMETRY-FIXTURE";
const passageId = "12345678-1234-4234-8234-123456789014";
const rawPath = "/__e2e/geometry.pdf";
const sourceRoute = `**/api/backend/knowledge/documents/${documentId}/source*`;
const document = {
  id: documentId,
  title: "PDF 几何合成测试资料",
  type: "maintenance-procedure",
  equipment: "UI fixture only",
  manufacturer: null,
  version: "synthetic-r1",
  updatedAt: "2026-10-23T00:00:00Z",
  vectorized: true,
  pageCount: 2,
  language: "zh-CN",
  tags: ["Synthetic geometry fixture"],
  summary: "Explicit synthetic PDF geometry; not OCR or field acceptance",
  relatedTurbineIds: [],
  relatedMissionIds: [],
  ingestionStatus: "indexed",
};
const fixtureBytes = (name: string) =>
  readFileSync(new URL(`../fixtures/knowledge-pdf/${name}.pdf`, import.meta.url));

async function fixture(page: Page, name = "plain", table = false) {
  const bytes = fixtureBytes(name);
  const sha = createHash("sha256").update(bytes).digest("hex");
  const passage: NativeKnowledgePassage = {
    passage_id: passageId,
    document_id: documentId,
    document_version: document.version,
    ordinal: 0,
    page_number: 2,
    section: "Synthetic geometry",
    text: "合成定位区域 123.40 kN，仅用于浏览器验证。",
    source_sha256: sha,
    text_sha256: "b".repeat(64),
    native_locator: {
      kind: "docling_pdf_item",
      source_verified: true,
      location_precision: table ? "table_region" : "region",
      regions: [
        {
          page_number: 2,
          page_width: 300,
          page_height: 400,
          coordinate_space: "pdf_points_top_left",
          bbox: [30, 80, 120, 120],
        },
      ],
    },
  };
  const counters = { downloads: 0, grants: 0 };
  const grant = (origin: string, expires = Date.now() + 300_000) => ({
    document_id: documentId,
    document_version: document.version,
    artifact_sha256: sha,
    content_type: "application/pdf",
    passage_id: passageId,
    page_number: 2,
    native_locator: passage.native_locator,
    download_url: new URL(rawPath, origin).href,
    expires_at: new Date(expires).toISOString(),
  });
  await page.route("**/api/knowledge-documents*", (route) =>
    route.fulfill({
      json: { ok: true, data: { documents: [document] }, meta: { fixture: true }, error: null },
    }),
  );
  await page.route(`**/api/backend/knowledge/documents/${documentId}/passages*`, (route) =>
    route.fulfill({ json: { passages: [passage], next_cursor: null, fixture: true } }),
  );
  await page.route(`**/api/backend/knowledge/passages/${passageId}`, (route) =>
    route.fulfill({ json: passage }),
  );
  await page.route(sourceRoute, (route) => {
    counters.grants += 1;
    return route.fulfill({ json: grant(route.request().url()) });
  });
  await page.route(`**${rawPath}`, (route) => {
    counters.downloads += 1;
    return route.fulfill({ body: bytes, contentType: "application/pdf" });
  });
  return { bytes, sha, passage, grant, counters };
}

async function openComparison(page: Page) {
  await page.goto(`/knowledge?document=${documentId}&passage=${passageId}`);
  await page.getByRole("button", { name: "对照原件", exact: true }).click();
  return page.getByRole("region", { name: "PDF 原件预览", exact: true });
}

async function verifiedCanvas(page: Page, sha: string, pageNumber = 2) {
  const canvas = page.getByTestId("knowledge-pdf-canvas");
  await expect(canvas).toBeVisible();
  await expect(canvas).toHaveAttribute("data-verified-source-sha256", sha);
  await expect(canvas).toHaveAttribute("data-pdf-page", String(pageNumber));
  return canvas;
}

async function assertRedPixel(page: Page, x: number, y: number) {
  const rgba = await page.getByTestId("knowledge-pdf-canvas").evaluate(
    (element, point) => {
      const canvas = element as HTMLCanvasElement;
      return Array.from(
        canvas
          .getContext("2d")!
          .getImageData(
            Math.floor(canvas.width * point.x),
            Math.floor(canvas.height * point.y),
            1,
            1,
          ).data,
      );
    },
    { x, y },
  );
  expect(rgba[0]).toBeGreaterThan(190);
  expect(rgba[0]).toBeLessThan(220);
  expect(rgba[1]).toBeLessThan(40);
  expect(rgba[2]).toBeLessThan(40);
  expect(rgba[3]).toBe(255);
}

test.beforeEach(async ({ page, context }) => {
  await context.setExtraHTTPHeaders({
    "oai-authenticated-user-id": "user-manager",
    "oai-authenticated-user-email": "manager@example.com",
    "x-e2e-runtime": "production",
  });
  await page.route("**/api/backend/knowledge/parser-capabilities", (route) =>
    route.fulfill({
      json: { docling_pdf_configured: true, max_artifact_bytes: 52428800, fixture: true },
    }),
  );
});

test("on-demand bytes render page two, bounded zoom and honest table-region highlights", async ({
  page,
}) => {
  const { sha, counters } = await fixture(page, "plain", true);
  const errors: string[] = [];
  const pdfRequests: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (/\/(?:pdf-|pdf\.worker|knowledge-pdf-preview-).*\.js/.test(request.url()))
      pdfRequests.push(request.url());
  });
  await page.goto(`/knowledge?document=${documentId}&passage=${passageId}`);
  await expect(page.getByRole("button", { name: "对照原件", exact: true })).toBeVisible();
  expect(counters).toEqual({ downloads: 0, grants: 0 });
  expect(page.workers()).toHaveLength(0);
  expect(pdfRequests).toEqual([]);
  await page.getByRole("button", { name: "对照原件", exact: true }).click();
  const canvas = await verifiedCanvas(page, sha);
  await assertRedPixel(page, 0.25, 0.25);
  const region = page.getByRole("region", { name: "PDF 原件预览", exact: true });
  await expect(region).toContainText("高亮为表格区域，并非精确单元格。");
  const geometry = await page.getByTestId("knowledge-pdf-highlight").evaluate((element) => ({
    left: (element as HTMLElement).style.left,
    top: (element as HTMLElement).style.top,
    width: (element as HTMLElement).style.width,
    height: (element as HTMLElement).style.height,
  }));
  expect(geometry).toEqual({ left: "10%", top: "20%", width: "30%", height: "10%" });
  expect(page.workers()).toHaveLength(1);
  expect(pdfRequests.some((url) => /\/pdf-[^/]+\.js/.test(url))).toBe(true);
  expect(new URL(page.workers()[0].url()).origin).toBe(new URL(page.url()).origin);
  await page.getByLabel("PDF 页码", { exact: true }).focus();
  await page.getByLabel("PDF 页码", { exact: true }).press("Home");
  await page.getByLabel("PDF 页码", { exact: true }).press("Enter");
  await expect(page.getByLabel("PDF 页码", { exact: true })).toHaveValue("1");
  await verifiedCanvas(page, sha, 1);
  await expect(page.getByTestId("knowledge-pdf-highlight")).toHaveCount(0);
  const white = await canvas.evaluate((element) =>
    Array.from((element as HTMLCanvasElement).getContext("2d")!.getImageData(10, 10, 1, 1).data),
  );
  expect(white).toEqual([255, 255, 255, 255]);
  await region.getByRole("button", { name: "当前引用" }).click();
  await verifiedCanvas(page, sha);
  const originalWidth = await canvas.evaluate((element) => (element as HTMLCanvasElement).width);
  await page.getByLabel("PDF 缩放", { exact: true }).selectOption("2");
  await expect
    .poll(() => canvas.evaluate((element) => (element as HTMLCanvasElement).width))
    .toBeGreaterThan(originalWidth);
  await verifiedCanvas(page, sha);
  await assertRedPixel(page, 0.25, 0.25);
  const size = await canvas.evaluate((element) => ({
    width: (element as HTMLCanvasElement).width,
    height: (element as HTMLCanvasElement).height,
  }));
  expect(size.width * size.height).toBeLessThanOrEqual(4 * 1024 * 1024);
  expect(Math.max(size.width, size.height)).toBeLessThanOrEqual(4096);
  await page.getByLabel("PDF 缩放", { exact: true }).selectOption("1");
  await verifiedCanvas(page, sha);
  await page.setViewportSize({ width: 1440, height: 1200 });
  await region.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: ".artifacts/improvements-20261023/pdf-plain-desktop-v5.png",
    fullPage: true,
  });
  const preview = await region.evaluate((element) => {
    const snapshot = element.cloneNode(true) as HTMLElement;
    const original = element.querySelector("canvas")!;
    const image = globalThis.document.createElement("img");
    image.src = original.toDataURL("image/png");
    image.alt = "真实浏览器画布快照：合成 PDF 第 2 页";
    image.style.cssText = original.style.cssText;
    snapshot.querySelector("canvas")!.replaceWith(image);
    const css = Array.from(globalThis.document.styleSheets)
      .flatMap((sheet) => Array.from(sheet.cssRules, (rule) => rule.cssText))
      .join("\n");
    return { html: snapshot.outerHTML, css };
  });
  writeFileSync(
    ".artifacts/improvements-20261023/pdf-preview-v5.html",
    `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>合成 PDF 实际组件快照</title><style>${preview.css}</style><body><main style="max-width:560px;margin:24px auto;padding:8px"><p>合成 PDF 的实际组件与画布快照；不构成 OCR 或现场验收。</p>${preview.html}</main></body></html>`,
  );
  await page.getByRole("button", { name: "收起原件对照", exact: true }).click();
  await expect.poll(() => page.workers().length).toBe(0);
  expect(errors).toEqual([]);
});

for (const geometry of [
  { name: "rotated", ratio: 400 / 300, x: 0.75, y: 0.25 },
  { name: "cropped", ratio: 260 / 340, x: 55 / 260, y: 80 / 340 },
]) {
  test(`${geometry.name} PDF renders actual geometry and falls back to page-only`, async ({
    page,
  }) => {
    const { sha } = await fixture(page, geometry.name);
    const region = await openComparison(page);
    const canvas = await verifiedCanvas(page, sha);
    const ratio = await canvas.evaluate(
      (element) => (element as HTMLCanvasElement).width / (element as HTMLCanvasElement).height,
    );
    expect(ratio).toBeCloseTo(geometry.ratio, 2);
    await assertRedPixel(page, geometry.x, geometry.y);
    await expect(page.getByTestId("knowledge-pdf-highlight")).toHaveCount(0);
    await expect(region).toContainText("仅定位原页，无法确认精确区域。");
    await region.screenshot({
      path: `.artifacts/improvements-20261023/pdf-${geometry.name}-v5.png`,
    });
  });
}

test("checksum failure exposes no verified canvas and retry uses newly fetched bytes", async ({
  page,
}) => {
  const { sha, bytes } = await fixture(page);
  let reads = 0;
  await page.route(`**${rawPath}`, (route) =>
    route.fulfill({
      body: ++reads === 1 ? Buffer.concat([bytes, Buffer.from("tamper")]) : bytes,
      contentType: "application/pdf",
    }),
  );
  const region = await openComparison(page);
  await expect(region).toContainText("原件校验未通过");
  await expect(page.locator("canvas[data-verified-source-sha256]")).toHaveCount(0);
  expect(page.workers()).toHaveLength(0);
  await region.getByRole("button", { name: "重试原件预览" }).click();
  await verifiedCanvas(page, sha);
  expect(reads).toBe(2);
});

for (const failure of ["type", "size"] as const) {
  test(`invalid ${failure} response is rejected before worker creation`, async ({ page }) => {
    const { bytes } = await fixture(page);
    await page.route(`**${rawPath}`, (route) =>
      route.fulfill({
        body: bytes,
        headers: {
          "content-type": failure === "type" ? "text/html" : "application/pdf",
          ...(failure === "size" ? { "content-length": "52428801" } : {}),
        },
      }),
    );
    const region = await openComparison(page);
    await expect(region).toContainText(failure === "type" ? "原件不是可预览的 PDF" : "50 MiB");
    await expect(page.locator("canvas[data-verified-source-sha256]")).toHaveCount(0);
    expect(page.workers()).toHaveLength(0);
  });
}

test("expired authorization removes verified canvas and renewal fetches a new grant", async ({
  page,
}) => {
  const { sha, grant } = await fixture(page);
  let grants = 0;
  await page.route(sourceRoute, (route) =>
    route.fulfill({
      json: grant(route.request().url(), Date.now() + (++grants === 1 ? 2500 : 300_000)),
    }),
  );
  await openComparison(page);
  await verifiedCanvas(page, sha);
  await expect(page.getByRole("button", { name: "重新获取原件", exact: true })).toBeVisible({
    timeout: 8000,
  });
  await expect(page.locator("canvas[data-verified-source-sha256]")).toHaveCount(0);
  await expect.poll(() => page.workers().length).toBe(0);
  await page.getByRole("button", { name: "重新获取原件", exact: true }).click();
  await verifiedCanvas(page, sha);
  expect(grants).toBe(2);
});

test("cancel aborts an unfinished download and retry can render", async ({ page }) => {
  const { sha, bytes } = await fixture(page);
  let release: (() => void) | undefined;
  let started = false;
  await page.route(`**${rawPath}`, async (route) => {
    started = true;
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    await route.fulfill({ body: bytes, contentType: "application/pdf" });
  });
  const region = await openComparison(page);
  await expect.poll(() => started).toBe(true);
  await region.getByRole("button", { name: "取消读取" }).click();
  await expect(region).toContainText("已取消读取原件");
  expect(page.workers()).toHaveLength(0);
  await page.unroute(`**${rawPath}`);
  release?.();
  await page.route(`**${rawPath}`, (route) =>
    route.fulfill({ body: bytes, contentType: "application/pdf" }),
  );
  await region.getByRole("button", { name: "重试原件预览" }).click();
  await verifiedCanvas(page, sha);
});

test("worker load timeout terminates resources without claiming a verified page", async ({
  page,
}) => {
  await fixture(page);
  await page.clock.install();
  let release: (() => void) | undefined;
  await page.route("**/pdf.worker*.js", async (route) => {
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    await route.abort("failed");
  });
  const region = await openComparison(page);
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.clock.runFor(30_001);
  await expect(region).toContainText("原件读取超时");
  await expect(page.locator("canvas[data-verified-source-sha256]")).toHaveCount(0);
  await expect.poll(() => page.workers().length).toBe(0);
  release?.();
});

test("responsive comparison retains readable canvas and reachable controls", async ({ page }) => {
  const { sha } = await fixture(page);
  await openComparison(page);
  for (const width of [320, 375, 414, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await verifiedCanvas(page, sha);
    const overflow = await page.evaluate(
      () => globalThis.document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(overflow, `document overflow at ${width}px`).toBe(false);
    for (const label of ["PDF 页码", "PDF 缩放"]) {
      const bounds = await page.getByLabel(label, { exact: true }).boundingBox();
      expect(bounds!.height).toBeGreaterThanOrEqual(44);
      expect(bounds!.x).toBeGreaterThanOrEqual(0);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    }
    if (width === 320) {
      await page.getByTestId("knowledge-pdf-canvas").scrollIntoViewIfNeeded();
      await page.screenshot({
        path: ".artifacts/improvements-20261023/pdf-plain-mobile320-v5.png",
        fullPage: false,
      });
      const canvas = page.getByTestId("knowledge-pdf-canvas");
      const baseline = await canvas.evaluate((element) => (element as HTMLCanvasElement).width);
      const viewport = page.getByRole("region", { name: "原 PDF 页面，可滚动查看", exact: true });
      for (const zoom of ["4", "8"]) {
        await page.getByLabel("PDF 缩放", { exact: true }).selectOption(zoom);
        await verifiedCanvas(page, sha);
        await assertRedPixel(page, 0.25, 0.25);
        const dimensions = await canvas.evaluate((element) => ({
          width: (element as HTMLCanvasElement).width,
          height: (element as HTMLCanvasElement).height,
        }));
        expect(dimensions.width).toBeGreaterThan(baseline);
        expect(dimensions.width * dimensions.height).toBeLessThanOrEqual(4 * 1024 * 1024);
        expect(Math.max(dimensions.width, dimensions.height)).toBeLessThanOrEqual(4096);
        expect(
          await viewport.evaluate((element) => element.scrollWidth > element.clientWidth),
        ).toBe(true);
        await viewport.evaluate((element) => {
          element.scrollLeft = 0;
        });
        await viewport.focus();
        await viewport.press("ArrowRight");
        await expect
          .poll(() => viewport.evaluate((element) => element.scrollLeft))
          .toBeGreaterThan(0);
        expect(
          await page.evaluate(
            () => globalThis.document.documentElement.scrollWidth > window.innerWidth,
          ),
        ).toBe(false);
      }
      await viewport.evaluate((element) => {
        element.scrollLeft = element.scrollWidth * 0.2;
        element.scrollTop = element.scrollHeight * 0.15;
      });
      await page.screenshot({
        path: ".artifacts/improvements-20261023/pdf-mobile320-800percent-v5.png",
      });
      await page.getByLabel("PDF 缩放", { exact: true }).selectOption("1");
      await verifiedCanvas(page, sha);
    }
  }
});
