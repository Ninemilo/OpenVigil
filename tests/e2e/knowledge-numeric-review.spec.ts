import { expect, test, type Page } from "@playwright/test";
import type {
  NumericReviewEnvelope,
  NumericReviewRecord,
} from "../../lib/knowledge-numeric-review";

// Explicit browser transport fixtures. Real database/object-store evidence is a separate suite.
const documentId = "KB-NUMERIC-UI-FIXTURE";
const passageId = "12345678-1234-4234-8234-123456789013";
const passage = {
  passage_id: passageId,
  document_id: documentId,
  document_version: "synthetic-v1",
  ordinal: 0,
  page_number: 2,
  section: "Synthetic force",
  text: "📐拉力 +123.40 kN",
  source_sha256: "a".repeat(64),
  text_sha256: "b".repeat(64),
  native_locator: { kind: "pdf_page", source_verified: true },
};
const document = {
  id: documentId,
  title: "数值复核界面测试资料",
  type: "maintenance-procedure",
  equipment: "UI fixture only",
  manufacturer: null,
  version: "synthetic-v1",
  updatedAt: "2026-10-23T00:00:00Z",
  vectorized: true,
  pageCount: 2,
  language: "zh-CN",
  tags: ["UI fixture"],
  summary: "Explicit synthetic browser source",
  relatedTurbineIds: [],
  relatedMissionIds: [],
  ingestionStatus: "indexed",
};
const endpoint = new RegExp(
  `/api/backend/knowledge/passages/${passageId}/numeric-review(?:/decision)?(?:\\?.*)?$`,
);
function snapshot(
  current: NumericReviewRecord | null = null,
): NumericReviewEnvelope & { fixtureFallback: false; fixture: true } {
  return {
    passage_id: passageId,
    document_id: documentId,
    source_binding: {
      expected_document_version: passage.document_version,
      expected_source_sha256: passage.source_sha256,
      expected_text_sha256: passage.text_sha256,
      expected_parser_version: "synthetic-parser",
      expected_locator_sha256: "c".repeat(64),
    },
    original_text: passage.text,
    page_number: 2,
    current,
    history: [],
    next_cursor: null,
    permissions: {
      can_propose: true,
      can_approve: Boolean(current?.status === "pending"),
      can_reject: Boolean(current?.status === "pending"),
      can_withdraw: Boolean(current && ["pending", "approved"].includes(current.status)),
    },
    updates_retrieval_text: false,
    authorizes_work: false,
    fixtureFallback: false,
    fixture: true,
  };
}
function pending(revision = 1): NumericReviewRecord {
  return {
    review_id: "review-fixture",
    revision,
    status: "pending",
    kind: "confirm_original",
    corrections: [],
    corrected_text: passage.text,
    reason: "synthetic source comparison",
    proposed_by: "fixture-author",
    proposed_at: "2026-10-23T01:00:00Z",
    reviewed_by: null,
    reviewed_at: null,
    review_reason: null,
  };
}
async function openPanel(page: Page) {
  await page.goto(`/knowledge?document=${documentId}&passage=${passageId}`);
  await page.getByRole("button", { name: "对照原件", exact: true }).click();
  await page.getByRole("button", { name: "数值复核", exact: true }).click();
  const panel = page.getByRole("region", { name: "本段数值复核", exact: true });
  await expect(panel.getByText("原件版本", { exact: true })).toBeVisible();
  return panel;
}
async function selectSource(page: Page, literal: string) {
  const source = page.getByLabel("原摘录（选择数值或单位）", { exact: true });
  await source.focus();
  // Chromium's headless Windows readonly caret does not move with Arrow keys,
  // including on a plain textarea without React. Set the browser's real range,
  // then deliver a real keyup event so the product reads its UTF-16 offsets.
  await source.evaluate((element, text) => {
    const input = element as HTMLTextAreaElement;
    const start = input.value.indexOf(text);
    input.setSelectionRange(start, start + text.length);
  }, literal);
  await source.press("Shift");
  expect(
    await source.evaluate((element) => {
      const input = element as HTMLTextAreaElement;
      return input.value.slice(input.selectionStart, input.selectionEnd);
    }),
  ).toBe(literal);
}
test.beforeEach(async ({ context, page }) => {
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
  // Numeric UI tests do not manufacture a verified PDF. Renderer byte tests run separately.
  await page.route(`**/api/backend/knowledge/documents/${documentId}/source*`, (route) =>
    route.fulfill({
      status: 422,
      json: { error: { code: "UI_FIXTURE", message: "此交互测试不包含原 PDF" } },
    }),
  );
});

test("selected emoji-prefixed numeric and unit spans submit strings once then read server state", async ({
  page,
}) => {
  let state = snapshot();
  let reads = 0;
  const writes: { body: Record<string, unknown>; key: string }[] = [];
  await page.route(endpoint, async (route) => {
    if (route.request().method() === "GET") {
      reads++;
      return route.fulfill({ json: state });
    }
    const body = route.request().postDataJSON();
    writes.push({ body, key: route.request().headers()["idempotency-key"] });
    state = snapshot({
      ...pending(),
      kind: body.kind,
      corrections: body.corrections,
      reason: body.reason,
      corrected_text: "📐拉力 +123.400 MN",
    });
    return route.fulfill({ json: state });
  });
  await page.goto(`/knowledge?document=${documentId}`);
  await expect(page.getByRole("button", { name: "对照原件", exact: true })).toBeVisible();
  expect(reads).toBe(0);
  await page.getByRole("button", { name: "对照原件", exact: true }).click();
  await page.getByRole("button", { name: "数值复核", exact: true }).click();
  await page.getByRole("radio", { name: "修正数值或单位", exact: true }).check();
  await selectSource(page, "+123.40");
  await page.getByRole("button", { name: "添加所选数值" }).click();
  await page.getByLabel("修正后数值", { exact: true }).fill("+123.400");
  await selectSource(page, "kN");
  await page.getByRole("button", { name: "用选区定位单位" }).click();
  await page.getByLabel("修正后单位", { exact: true }).fill("MN");
  await page.getByLabel("提议说明", { exact: true }).fill("明确的合成界面测试修正，非真实原件复核");
  await page.getByRole("button", { name: "提交数值修正", exact: true }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(page.getByText("待独立复核", { exact: true })).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].body.corrections).toEqual([
    {
      start: 4,
      end: 11,
      original_value: "+123.40",
      corrected_value: "+123.400",
      original_unit: "kN",
      corrected_unit: "MN",
      unit_start: 12,
      unit_end: 14,
    },
  ]);
  expect(writes[0].body.expected_locator_sha256).toBe("c".repeat(64));
  expect(reads).toBeGreaterThanOrEqual(2);
  await expect(page.getByLabel("原摘录（选择数值或单位）", { exact: true })).toHaveValue(
    passage.text,
  );
});

test("unknown write survives unmount and explicit retry preserves complete body and key", async ({
  page,
}) => {
  let state = snapshot();
  const writes: { body: string | null; key: string }[] = [];
  await page.route(endpoint, async (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: state });
    writes.push({
      body: route.request().postData(),
      key: route.request().headers()["idempotency-key"],
    });
    if (writes.length === 1) return route.abort("failed");
    state = snapshot(pending());
    return route.fulfill({ json: state });
  });
  await openPanel(page);
  await page.getByLabel("提议说明", { exact: true }).fill("synthetic original confirmation");
  await page.getByRole("button", { name: "提交原数值确认", exact: true }).click();
  await expect(page.getByRole("button", { name: "核验同一提交", exact: true })).toBeEnabled();
  expect(writes).toHaveLength(1);
  await expect(page.getByLabel("提议说明", { exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "收起原件对照", exact: true }).click();
  await page.getByRole("button", { name: "对照原件", exact: true }).click();
  await page.getByRole("button", { name: "数值复核", exact: true }).click();
  await expect(page.getByRole("button", { name: "核验同一提交", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "核验同一提交", exact: true }).click();
  await expect(page.getByText("待独立复核", { exact: true })).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual(writes[0]);
});

test("403 never auto-submits under a new key and 409 requires a successful read before re-edit", async ({
  page,
}) => {
  let status = 403;
  let state = snapshot();
  const writes: { body: string | null; key: string }[] = [];
  await page.route(endpoint, (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: state });
    writes.push({
      body: route.request().postData(),
      key: route.request().headers()["idempotency-key"],
    });
    return route.fulfill({
      status,
      json: { error: { code: "SYNTHETIC_REFUSAL", message: "explicit test refusal" } },
    });
  });
  await openPanel(page);
  await page.getByLabel("提议说明", { exact: true }).fill("synthetic original confirmation");
  await page.getByRole("button", { name: "提交原数值确认", exact: true }).click();
  await expect(page.getByText(/当前身份无权提交/)).toBeVisible();
  expect(writes).toHaveLength(1);
  status = 409;
  await page.getByRole("button", { name: "核验同一提交", exact: true }).click();
  await expect(page.getByRole("button", { name: "核对后重新编辑", exact: true })).toBeDisabled();
  expect(writes[1]).toEqual(writes[0]);
  state = snapshot({
    ...pending(2),
    status: "rejected",
    reviewed_by: "independent-reviewer",
    reviewed_at: "2026-10-23T01:01:00Z",
    review_reason: "synthetic rejection",
  });
  await page.getByRole("button", { name: "刷新复核记录", exact: true }).click();
  await expect(page.getByText("已拒绝", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "核对后重新编辑", exact: true }).click();
  await expect(page.getByLabel("提议说明", { exact: true })).toBeEnabled();
  expect(writes).toHaveLength(2);
});

test("independent reviewer can approve then withdraw and stale permissions never appear approved", async ({
  page,
}) => {
  let state = snapshot(pending());
  let failRead = false;
  await page.route(endpoint, (route) => {
    if (route.request().method() === "GET")
      return failRead
        ? route.fulfill({
            status: 503,
            json: { error: { code: "UNAVAILABLE", message: "synthetic audit unavailable" } },
          })
        : route.fulfill({ json: state });
    const action = route.request().postDataJSON().action;
    state = snapshot({
      ...pending(action === "approve" ? 2 : 3),
      status: action === "approve" ? "approved" : "withdrawn",
      reviewed_by: "independent-reviewer",
      reviewed_at: "2026-10-23T01:01:00Z",
      review_reason: "synthetic independent check",
    });
    state = {
      ...state,
      history: [
        {
          event_id: `event-${state.current!.revision}`,
          revision: state.current!.revision,
          action,
          actor: "independent-reviewer",
          occurred_at: "2026-10-23T01:01:00Z",
          reason: "synthetic independent check",
          snapshot_sha256: "d".repeat(64),
          snapshot: {
            source_binding: state.source_binding,
            original_text: passage.text,
            current: state.current!,
          },
        },
      ],
    };
    return route.fulfill({ json: state });
  });
  const panel = await openPanel(page);
  await page.getByLabel("复核说明", { exact: true }).fill("synthetic independent check");
  await page.getByRole("button", { name: "批准数值复核", exact: true }).click();
  await expect(panel.getByText("已独立复核", { exact: true })).toBeVisible();
  await expect(
    panel.getByRole("definition").filter({ hasText: "independent-reviewer" }),
  ).toBeVisible();
  await panel.getByText("审计历史（已载入 1 条）", { exact: true }).click();
  await expect(panel.getByText("修订 2 · 批准", { exact: true })).toBeVisible();
  failRead = true;
  await page.getByRole("button", { name: "刷新复核记录", exact: true }).click();
  await expect(panel.getByText("状态读取失败", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "批准数值复核", exact: true })).toBeDisabled();
  failRead = false;
  await page.getByRole("button", { name: "刷新复核记录", exact: true }).click();
  await page.getByLabel("复核说明", { exact: true }).fill("synthetic withdrawal reason");
  await page.getByRole("button", { name: "撤回提议", exact: true }).click();
  await expect(panel.getByText("已撤回", { exact: true })).toBeVisible();
});

test("mobile layout and keyboard controls remain usable with long source identity", async ({
  page,
}) => {
  await page.route(endpoint, (route) => route.fulfill({ json: snapshot() }));
  const panel = await openPanel(page);
  for (const width of [320, 375, 414, 768]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(panel).toBeVisible();
    expect(await panel.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(
      true,
    );
    const button = panel.getByRole("button", { name: "刷新复核记录", exact: true });
    await button.focus();
    await expect(button).toBeFocused();
    await button.press("Enter");
    await expect(button).toBeEnabled();
  }
  await page.screenshot({
    path: ".artifacts/improvements-20261023/numeric-review-mobile.png",
    fullPage: true,
  });
});

test("a live identity switch cannot expose or replay another subject's uncertain command", async ({
  page,
}) => {
  let writes = 0;
  await page.route(endpoint, (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: snapshot() });
    writes++;
    return route.abort("failed");
  });
  await openPanel(page);
  await page.getByLabel("提议说明", { exact: true }).fill("synthetic account isolation check");
  await page.getByRole("button", { name: "提交原数值确认", exact: true }).click();
  await expect(page.getByRole("button", { name: "核验同一提交", exact: true })).toBeEnabled();
  await page.route("**/api/backend/session", (route) =>
    route.fulfill({ json: { subject: "another-authenticated-subject" } }),
  );
  await page.getByRole("button", { name: "核验同一提交", exact: true }).click();
  await expect(page.getByRole("button", { name: "核验同一提交", exact: true })).toHaveCount(0);
  await expect(page.getByText("查看锁定的提交", { exact: true })).toHaveCount(0);
  expect(writes).toBe(1);
});
