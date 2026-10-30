import { expect, test } from "@playwright/test";

test.use({ extraHTTPHeaders: { "x-e2e-runtime": "demo" } });

test("shell search preserves keyboard navigation, focus return and the read-only action link", async ({
  page,
}) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  // This hydration marker stays in the DOM but is hidden at the desktop viewport.
  await expect(page.getByRole("button", { name: "打开导航", includeHidden: true })).toBeEnabled();
  const searchButton = page.getByRole("button", { name: "打开全局搜索" });
  await searchButton.focus();
  await page.keyboard.press("Control+k");
  const dialog = page.getByRole("dialog", { name: "OpenVigil 快捷命令" });
  const input = page.getByRole("textbox", { name: "搜索快捷命令" });
  await expect(dialog).toBeVisible();
  await expect(input).toBeFocused();
  await input.fill("  wt-064  ");
  await expect(dialog.locator("a.command-item")).toHaveCount(1);
  await expect(dialog.locator("a.command-item")).toHaveAttribute("href", "/turbines/WT-064");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(searchButton).toBeFocused();

  await searchButton.click();
  await expect(input).toHaveValue("  wt-064  ");
  await input.fill("no-such-object-583279");
  await expect(dialog.getByText("没有找到相关对象")).toBeVisible();
  await input.fill("");
  await page.keyboard.press("ArrowDown");
  await expect(dialog.locator(".command-item--active")).toHaveAttribute(
    "href",
    "/work-orders?turbineId=WT-023&intent=create",
  );
  await page.keyboard.press("ArrowUp");
  await expect(dialog.locator(".command-item--active")).toHaveAttribute("href", "/missions");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/work-orders\?turbineId=WT-023&intent=create$/);
});

test("notification toggle and Escape preserve the original three demo links", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("button", { name: "打开导航", includeHidden: true })).toBeEnabled();
  const toggle = page.getByRole("button", { name: "通知", exact: true });
  const popover = page.locator(".notification-popover");
  await expect(popover).toHaveCount(0);
  await toggle.click();
  await expect(popover.getByText("3 条未读")).toBeVisible();
  await expect(popover.locator("a")).toHaveCount(3);
  const links = await popover
    .locator("a")
    .evaluateAll((items) => items.map((item) => item.getAttribute("href")));
  expect(links).toEqual(["/missions/MISSION-2026-0823", "/alarms", "/agents"]);
  await toggle.click();
  await expect(popover).toHaveCount(0);
  await toggle.click();
  await expect(popover).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(popover).toHaveCount(0);
});
