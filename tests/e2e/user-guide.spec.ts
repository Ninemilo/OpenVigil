import { expect, test } from "@playwright/test";
import { guideChapters } from "../../lib/user-guide";

test("the top bar opens the guide and its chapter links lead back into the workspace", async ({
  page,
}) => {
  await page.setExtraHTTPHeaders({ "x-e2e-runtime": "demo" });
  await page.goto("/");
  const entry = page.locator("header.topbar").getByRole("link", { name: "使用指南" });
  await entry.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/guide$/);
  await expect(page.getByRole("heading", { name: "使用指南", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "快速开始" })).toBeVisible();

  const chapters = page.getByRole("navigation", { name: "指南章节" });
  await expect(chapters.getByRole("link")).toHaveCount(guideChapters.length);
  await chapters.getByRole("link", { name: "告警与诊断" }).click();
  await expect(page).toHaveURL(/\/guide\/alarm-diagnosis$/);
  await expect(chapters.getByRole("link", { name: "告警与诊断" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await page
    .getByRole("navigation", { name: "本页目录" })
    .getByRole("link", { name: "跟踪或创建 Mission" })
    .click();
  await expect(page).toHaveURL(/#follow-mission$/);
  await page.reload();
  await expect(page.getByRole("heading", { name: "跟踪或创建 Mission" })).toBeVisible();

  await page
    .getByRole("navigation", { name: "相邻章节" })
    .getByRole("link", { name: /下一章 审批与工单/ })
    .click();
  await expect(page.getByRole("heading", { name: "审批与工单", level: 1 })).toBeVisible();
  await page.getByRole("link", { name: "打开工单中心" }).click();
  await expect(page).toHaveURL(/\/work-orders$/);
  await expect(page.getByRole("heading", { name: "工单中心", level: 1 })).toBeVisible();
});

test("newcomers can follow the WT-023 walkthrough in read-only Demo", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-runtime": "demo" });
  await page.goto("/guide");
  await page.getByRole("link", { name: "开始 WT-023 新手练习" }).click();
  await expect(page).toHaveURL(/\/guide\/wt-023-walkthrough$/);

  await page.getByRole("link", { name: "打开告警中心开始练习" }).click();
  await page.getByPlaceholder("搜索告警、机组或代码…").fill("MB-VIB-RMS-HI");
  const alarm = page.getByRole("dialog", { name: "ALARM-0031 告警详情" });
  const alarmRow = page.getByRole("row", { name: /主轴承振动 RMS 持续升高/ });
  await alarmRow.getByRole("checkbox").check();
  await expect(alarm).toBeHidden();
  await alarmRow.getByText("主轴承振动 RMS 持续升高", { exact: true }).click();
  await expect(alarm.getByText("4.81 mm/s", { exact: true })).toBeVisible();
  await expect(alarm.getByRole("link", { name: "打开趋势" })).toHaveAttribute(
    "href",
    "/scada?turbineId=WT-023",
  );
  await expect(alarm.getByRole("link", { name: "进入 Mission" })).toHaveAttribute(
    "href",
    "/missions/MISSION-2026-0823",
  );

  await page.goBack();
  await page.getByRole("link", { name: "打开 WT-023 演示 Mission" }).click();
  await expect(page).toHaveURL(/\/missions\/MISSION-2026-0823$/);
  await page.getByRole("button", { name: "证据", exact: true }).click();
  await expect(page.getByRole("region", { name: /Agent 活动时间线/ })).toContainText("关联证据");
  await expect(page.getByRole("heading", { name: "工程师审批" })).toBeVisible();

  await page.goBack();
  await page.getByRole("link", { name: "打开决策中心比较方案" }).click();
  await expect(page.getByRole("heading", { name: "决策中心", level: 1 })).toBeVisible();
  await expect(page.getByText("降载运行并在 72 小时内检查").first()).toBeVisible();

  await page.goBack();
  await page.getByRole("link", { name: "打开工单中心核对任务" }).click();
  await page.getByPlaceholder("搜索工单、机组或班组…").fill("WO-20260823-017");
  await page
    .getByRole("row", { name: /WT-023 主轴承检查/ })
    .getByText("WT-023 主轴承检查", { exact: true })
    .click();
  const order = page.getByRole("dialog", { name: "WO-20260823-017 工单详情" });
  await expect(order.getByRole("heading", { name: "任务清单" })).toBeVisible();
  await expect(order.getByRole("checkbox")).toHaveCount(5);

  await page.goBack();
  await page.getByRole("link", { name: "继续练习预览和导出报告" }).click();
  await page.getByRole("link", { name: "打开运维报告" }).click();
  await page.getByRole("searchbox", { name: "搜索报告" }).fill("RPT-AI-WT023-20260813");
  const preview = page.getByRole("article", { name: "AI 诊断报告预览" });
  await expect(preview.getByText("WT-023 工作流追踪")).toBeVisible();
  await expect(preview.getByRole("link", { name: "导出 PDF" })).toBeVisible();
});

test("guide entry and chapter menu work at narrow widths and keep the current theme", async ({
  page,
}) => {
  await page.setExtraHTTPHeaders({ "x-e2e-runtime": "demo" });
  await page.setViewportSize({ width: 320, height: 700 });
  await page.addInitScript(() => localStorage.setItem("openvigil-theme", "dark"));
  await page.goto("/");
  const entry = page.locator("header.topbar").getByRole("link", { name: "使用指南" });
  const target = await entry.boundingBox();
  expect(target?.width).toBeGreaterThanOrEqual(44);
  expect(target?.height).toBeGreaterThanOrEqual(44);
  expect(target!.x + target!.width).toBeLessThanOrEqual(320);
  await entry.click();
  await expect(page).toHaveURL(/\/guide$/);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  const chapters = page.getByRole("navigation", { name: "指南章节" });
  await expect(chapters).toBeHidden();
  await page.getByText("指南章节", { exact: true }).click();
  await chapters.getByRole("link", { name: "知识库", exact: true }).click();
  await expect(page.getByRole("heading", { name: "知识库", level: 1 })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "本页目录" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);

  await page.getByRole("link", { name: "返回工作台" }).click();
  await expect(page).toHaveURL(/\/$/);
});

test("the walkthrough screenshots load on mobile and link to the originals", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-runtime": "demo" });
  await page.setViewportSize({ width: 320, height: 720 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/guide/wt-023-walkthrough");

  const figures = page.locator("main figure");
  await expect(figures).toHaveCount(13);
  for (const figure of await figures.all()) {
    const image = figure.locator("img");
    await image.scrollIntoViewIfNeeded();
    await expect
      .poll(() =>
        image.evaluate(
          (img: HTMLImageElement) =>
            img.complete &&
            img.naturalWidth > 0 &&
            img.naturalWidth === Number(img.getAttribute("width")) &&
            img.naturalHeight === Number(img.getAttribute("height")),
        ),
      )
      .toBe(true);
    await expect(figure.locator("figcaption")).toBeVisible();
  }
  await expect(page.locator("figcaption").filter({ hasText: "MB-VIB-RMS-HI" })).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);

  const [original] = await Promise.all([
    page.waitForEvent("popup"),
    figures
      .first()
      .getByRole("link", { name: /在新标签页查看大图/ })
      .click(),
  ]);
  await expect(original).toHaveURL(/\/guide\/wt-023\/01-demo-navigation\.png$/);
  await expect(original.locator("img")).toBeVisible();
  await original.close();
});

test("an invalid guide chapter provides a link back to the guide", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-runtime": "demo" });
  await page.goto("/guide/unknown-chapter");
  await expect(page.getByRole("heading", { name: "指南章节未找到" })).toBeVisible();
  await page.getByRole("link", { name: "返回使用指南" }).click();
  await expect(page).toHaveURL(/\/guide$/);
});

test("the production guide preserves the workspace sign-in flow", async ({ page }) => {
  await page.goto("/guide/asset-monitoring");
  await expect(page.getByRole("link", { name: "使用 ChatGPT 登录" })).toHaveAttribute(
    "href",
    "/signin-with-chatgpt?return_to=%2Fguide%2Fasset-monitoring",
  );

  await page.setExtraHTTPHeaders({
    "oai-authenticated-user-id": "user-manager",
    "oai-authenticated-user-email": "user-manager@example.com",
  });
  await page.goto("/guide/asset-monitoring");
  await expect(page.getByRole("heading", { name: "资产与监测", level: 1 })).toBeVisible();
  await page.getByRole("link", { name: "返回工作台" }).click();
  await expect(page.locator("header.topbar").getByRole("link", { name: "使用指南" })).toBeVisible();
});
