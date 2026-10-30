import { randomUUID } from "node:crypto";
import {
  expect,
  type APIRequestContext,
  type BrowserContext,
  type Page,
  type Response,
} from "@playwright/test";
import { fixtureIdentity } from "./structural-real-support";

interface ReviewPassage {
  passage_id: string;
  document_id: string;
  text: string;
  source_sha256: string;
}

interface ReviewState {
  source_binding: Record<string, string>;
  current: {
    review_id: string;
    revision: number;
    status: string;
    kind: string;
    corrected_text: string;
    proposed_by: string;
    reviewed_by: string | null;
  } | null;
  history: { revision: number; action: string; snapshot_sha256: string }[];
  original_text: string;
}

async function captureCommand(
  page: Page,
  path: string,
  action: () => Promise<unknown>,
): Promise<Response> {
  const response = page.waitForResponse(
    (value) => value.request().method() === "POST" && new URL(value.url()).pathname === path,
  );
  await action();
  const result = await response;
  expect(result.status(), await result.text()).toBe(200);
  return result;
}

async function openReview(page: Page, passage: ReviewPassage) {
  await page.goto(
    `/knowledge?document=${encodeURIComponent(passage.document_id)}&passage=${encodeURIComponent(passage.passage_id)}`,
  );
  const drawer = page.getByRole("dialog");
  await drawer.getByRole("button", { name: "对照原件", exact: true }).first().click();
  const comparison = drawer.getByRole("region", { name: "原件与解析结果对照" });
  const canvas = comparison.getByTestId("knowledge-pdf-canvas");
  await expect(canvas).toHaveAttribute("data-verified-source-sha256", passage.source_sha256);
  await expect(canvas).toHaveAttribute("data-pdf-page", "2");
  await expect(comparison.getByTestId("knowledge-pdf-highlight").first()).toBeVisible();
  await comparison.getByRole("button", { name: "数值复核", exact: true }).click();
  return comparison;
}

/** Real gateway/API/database path. The deliberate wrong proposal must be rejected. */
export async function documentNumericReview(
  page: Page,
  context: BrowserContext,
  request: APIRequestContext,
  passage: ReviewPassage,
) {
  const apiPath = `/api/backend/knowledge/passages/${passage.passage_id}/numeric-review`;
  let comparison = await openReview(page, passage);
  await comparison.getByRole("radio", { name: "修正数值或单位" }).check();
  const start = passage.text.indexOf("+123.45");
  expect(start).toBeGreaterThanOrEqual(0);
  const excerpt = comparison.getByLabel("原摘录（选择数值或单位）", { exact: true });
  await excerpt.focus();
  await excerpt.evaluate((element, position) => {
    (element as HTMLTextAreaElement).setSelectionRange(position, position + 7);
  }, start);
  await excerpt.press("Shift");
  const addValue = comparison.getByRole("button", { name: "添加所选数值", exact: true });
  await expect(addValue).toBeEnabled();
  await addValue.click();
  await comparison.getByLabel("修正后数值", { exact: true }).fill("+123.40");
  await comparison.getByLabel("原单位", { exact: true }).fill("kN");
  await comparison.getByLabel("修正后单位", { exact: true }).fill("kN");
  await comparison
    .getByLabel("提议说明", { exact: true })
    .fill("合成验收错误提议：用于验证独立复核会拒绝与原件不同的数值。");
  const proposal = await captureCommand(page, apiPath, () =>
    comparison.getByRole("button", { name: "提交数值修正", exact: true }).click(),
  );
  const proposed = (await proposal.json()) as ReviewState;
  expect(proposed.current?.revision).toBe(1);
  expect(proposed.current?.status).toBe("pending");
  expect(proposed.current?.proposed_by).toBe("business-manager");
  const proposalPayload = proposal.request().postDataJSON();
  const proposalKey = proposal.request().headers()["idempotency-key"];
  expect(proposalKey).toBeTruthy();

  const self = await request.post(apiPath + "/decision", {
    data: {
      ...proposed.source_binding,
      expected_revision: 1,
      action: "approve",
      reason: "Synthetic self approval must be rejected",
    },
    headers: { ...fixtureIdentity(), "Idempotency-Key": randomUUID() },
  });
  expect(self.status(), await self.text()).toBe(422);

  const reviewer = await context.browser()!.newContext({
    baseURL: new URL(page.url()).origin,
    extraHTTPHeaders: fixtureIdentity("business-reviewer"),
  });
  try {
    const reviewerPage = await reviewer.newPage();
    const review = await openReview(reviewerPage, passage);
    await review
      .getByLabel("复核说明", { exact: true })
      .fill("对照合成PDF原件为+123.45，拒绝+123.40的错误修正；不作现场资格结论。");
    const rejected = await captureCommand(reviewerPage, apiPath + "/decision", () =>
      review.getByRole("button", { name: "拒绝提议", exact: true }).click(),
    );
    const rejection = (await rejected.json()) as ReviewState;
    expect(rejection.current?.revision).toBe(2);
    expect(rejection.current?.status).toBe("rejected");

    comparison = await openReview(page, passage);
    await comparison.getByRole("radio", { name: "确认原数值" }).check();
    await comparison
      .getByLabel("提议说明", { exact: true })
      .fill("对照合成PDF确认原数值+123.45；仅验证此段，不代表现场验收。");
    const confirmation = await captureCommand(page, apiPath, () =>
      comparison.getByRole("button", { name: "提交原数值确认", exact: true }).click(),
    );
    expect(((await confirmation.json()) as ReviewState).current?.revision).toBe(3);

    const finalReview = await openReview(reviewerPage, passage);
    await finalReview
      .getByLabel("复核说明", { exact: true })
      .fill("独立核对同一合成PDF原数值、正负号和单位；仅为软件测试原件。");
    const approval = await captureCommand(reviewerPage, apiPath + "/decision", () =>
      finalReview.getByRole("button", { name: "批准数值复核", exact: true }).click(),
    );
    const approved = (await approval.json()) as ReviewState;
    expect(approved.current?.revision).toBe(4);
    expect(approved.current?.status).toBe("approved");
    expect(approved.current?.kind).toBe("confirm_original");
    expect(approved.current?.reviewed_by).toBe("business-reviewer");
    expect(approved.current?.corrected_text).toBe(passage.text);
    await expect(finalReview.getByText("已独立复核", { exact: true }).first()).toBeVisible();
  } finally {
    await reviewer.close();
  }

  const replay = await request.post(apiPath, {
    data: proposalPayload,
    headers: { ...fixtureIdentity(), "Idempotency-Key": proposalKey },
  });
  expect(replay.status(), await replay.text()).toBe(200);
  expect(replay.headers()["idempotency-replayed"]).toBe("true");
  expect(((await replay.json()) as ReviewState).current?.revision).toBe(1);
  const stale = await request.post(apiPath, {
    data: proposalPayload,
    headers: { ...fixtureIdentity(), "Idempotency-Key": randomUUID() },
  });
  expect(stale.status(), await stale.text()).toBe(409);
  const denied = await request.get(apiPath, { headers: fixtureIdentity("business-restricted") });
  expect(denied.status(), await denied.text()).toBe(404);
  const final = await request.get(apiPath, { headers: fixtureIdentity() });
  expect(final.status(), await final.text()).toBe(200);
  const persisted = (await final.json()) as ReviewState;
  expect(persisted.current?.revision).toBe(4);
  expect(persisted.current?.status).toBe("approved");
  expect(persisted.original_text).toBe(passage.text);
  expect(persisted.history.map((item) => [item.revision, item.action])).toEqual([
    [4, "approve"],
    [3, "propose"],
    [2, "reject"],
    [1, "propose"],
  ]);
  comparison = await openReview(page, passage);
  await expect(comparison.getByText("已独立复核", { exact: true }).first()).toBeVisible();
  return {
    passage_id: passage.passage_id,
    review_id: persisted.current!.review_id,
    final_revision: 4,
    final_status: "approved",
    final_kind: "confirm_original",
    rejected_incorrect_correction: true,
    independent_reviewer: "business-reviewer",
    proposer: "business-manager",
    source_binding: persisted.source_binding,
    history_hashes: persisted.history.map((item) => item.snapshot_sha256),
    original_text_unchanged: true,
    pdf_rendered: true,
    self_review_rejected: true,
    replay_preserved_history: true,
    stale_revision_rejected: true,
    restricted_read_rejected: true,
    reloaded_persisted_state: true,
  };
}
