import { expect, test, type Browser, type Page, type Route } from "@playwright/test";
import type { Alarm } from "../../lib/types";

const baseURL = `http://127.0.0.1:${Number(process.env.WINDOPS_E2E_PORT ?? "4179")}`;

// These cases exercise the built UI in Chromium with explicit HTTP fixtures;
// they do not establish acceptance against a real Mission backend.
async function productionPage(browser: Browser) {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
    extraHTTPHeaders: {
      "oai-authenticated-user-id": "user-manager",
      "oai-authenticated-user-email": "user-manager@example.com",
    },
  });
  return { context, page: await context.newPage() };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function alarm(overrides: Partial<Alarm> = {}): Alarm {
  return {
    id: "ALARM-CREATE-001",
    code: "BEARING_VIBRATION",
    turbineId: "WT-CREATE-001",
    subsystem: "main-bearing",
    severity: "major",
    title: "主轴承振动异常",
    description: "用于验证创建表单的浏览器传输夹具。",
    triggeredAt: new Date().toISOString(),
    durationMinutes: 15,
    status: "active",
    aiStatus: "queued",
    assignee: null,
    acknowledgedAt: null,
    resolvedAt: null,
    currentValue: null,
    threshold: null,
    unit: null,
    sourceVariable: "bearing_vibration_rms",
    missionId: null,
    evidenceIds: [],
    ...overrides,
  };
}

async function installCatalogRoutes(page: Page, alarms: readonly Alarm[]) {
  const reads = { missions: 0, alarms: 0 };
  await page.route("**/api/runtime", (route) =>
    fulfillJson(route, {
      data: {
        runtimeMode: "production",
        backendAuthentication: "delegation",
        productionReady: true,
        fixtureFallbackAllowed: false,
        backend: { reachable: true },
      },
      error: null,
      meta: { readOnly: true, secretsRedacted: true },
    }),
  );
  await page.route(/\/api\/alarms$/, (route) => {
    reads.alarms += 1;
    return fulfillJson(route, { data: alarms, meta: { count: alarms.length } });
  });
  await page.route(/\/api\/missions$/, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    reads.missions += 1;
    return fulfillJson(route, { data: [] });
  });
  return reads;
}

async function openCreateDrawer(page: Page, path = "/missions") {
  await page.goto(path);
  const create = page.getByRole("button", { name: "创建 Mission", exact: true });
  await expect(create).toBeEnabled();
  await create.click();
  const dialog = page.getByRole("dialog", { name: "创建 Mission", exact: true });
  await expect(dialog).toBeVisible();
  return dialog;
}

test("Mission creation sends one command while pending, then refreshes both catalogs and closes", async ({
  browser,
}) => {
  const { context, page } = await productionPage(browser);
  let releaseResponse!: () => void;
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  try {
    const reads = await installCatalogRoutes(page, [alarm()]);
    const commands: { body: unknown; key: string | undefined }[] = [];
    await page.route(/\/api\/missions$/, async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      commands.push({
        body: route.request().postDataJSON(),
        key: route.request().headers()["idempotency-key"],
      });
      await responseGate;
      await fulfillJson(route, { mission_id: "MISSION-CREATED-001" }, 201);
    });
    const dialog = await openCreateDrawer(page);
    await dialog.getByLabel("Mission 标题", { exact: true }).fill("受控轴承调查");
    const beforeReads = { ...reads };
    await dialog.getByRole("button", { name: "创建并启动分析" }).click();
    await expect(dialog.getByRole("button", { name: "正在提交…" })).toBeDisabled();
    await expect(dialog.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
    await dialog.getByLabel("Mission 标题", { exact: true }).press("Enter");
    await expect.poll(() => commands.length).toBe(1);
    expect(commands[0].key).toMatch(/^mission-create-[0-9a-f-]{36}$/);
    expect(commands[0].body).toEqual({
      alarm_id: "ALARM-CREATE-001",
      title: "受控轴承调查",
      analysis_profile: {
        component: "main_bearing",
        primary_variable: "bearing_vibration_rms",
        related_variables: [],
        knowledge_query: "main bearing inspection failure diagnosis",
      },
    });
    releaseResponse();
    await expect(dialog).toHaveCount(0);
    await expect.poll(() => reads.missions).toBeGreaterThan(beforeReads.missions);
    await expect.poll(() => reads.alarms).toBeGreaterThan(beforeReads.alarms);
    expect(commands).toHaveLength(1);
  } finally {
    releaseResponse();
    await context.close();
  }
});

test("Mission creation keeps entered values and exposes a failed command without closing", async ({
  browser,
}) => {
  const { context, page } = await productionPage(browser);
  try {
    const reads = await installCatalogRoutes(page, [alarm()]);
    let commands = 0;
    await page.route(/\/api\/missions$/, async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      commands += 1;
      await fulfillJson(
        route,
        { error: { code: "MISSION_CREATE_FAILED", message: "任务事务未完成，请重试。" } },
        500,
      );
    });
    const dialog = await openCreateDrawer(page);
    await dialog.getByLabel("Mission 标题", { exact: true }).fill("保留失败输入");
    await dialog.getByLabel("主信号变量", { exact: true }).fill("custom_bearing_signal");
    const beforeReads = { ...reads };
    await dialog.getByRole("button", { name: "创建并启动分析" }).click();
    await expect(dialog.getByRole("alert")).toContainText("创建失败");
    await expect(dialog.getByRole("alert")).toContainText("任务事务未完成，请重试。");
    await expect(dialog.getByLabel("Mission 标题", { exact: true })).toHaveValue("保留失败输入");
    await expect(dialog.getByLabel("主信号变量", { exact: true })).toHaveValue(
      "custom_bearing_signal",
    );
    await expect(dialog.getByRole("button", { name: "创建并启动分析" })).toBeEnabled();
    expect(commands).toBe(1);
    expect(reads).toEqual(beforeReads);
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await expect(dialog).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test("Mission creation excludes linked alarms and sends no command when none is eligible", async ({
  browser,
}) => {
  const { context, page } = await productionPage(browser);
  try {
    await installCatalogRoutes(page, [alarm({ missionId: "MISSION-ALREADY-LINKED" })]);
    let commands = 0;
    await page.route(/\/api\/missions$/, (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      commands += 1;
      return fulfillJson(route, { error: { code: "UNEXPECTED_COMMAND" } }, 500);
    });
    const dialog = await openCreateDrawer(page);
    await expect(dialog.getByRole("heading", { name: "没有可用告警" })).toBeVisible();
    await expect(dialog.getByRole("combobox")).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "创建并启动分析" })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(commands).toBe(0);
  } finally {
    await context.close();
  }
});

test("Mission creation prefers the route turbine and updates the analysis profile on alarm selection", async ({
  browser,
}) => {
  const { context, page } = await productionPage(browser);
  try {
    await installCatalogRoutes(page, [
      alarm(),
      alarm({
        id: "ALARM-PREFERRED-002",
        turbineId: "WT-PREFERRED",
        subsystem: "gearbox",
        title: "齿轮箱温度异常",
        sourceVariable: null,
      }),
      alarm({ id: "ALARM-LINKED-003", missionId: "MISSION-LINKED" }),
    ]);
    const dialog = await openCreateDrawer(
      page,
      "/missions?turbineId=wt-preferred&intent=diagnosis",
    );
    const selection = dialog.getByRole("combobox", { name: "未关联告警", exact: true });
    await expect(selection).toHaveValue("ALARM-PREFERRED-002");
    await expect(selection.getByRole("option")).toHaveCount(2);
    await expect(dialog.getByLabel("Mission 标题", { exact: true })).toHaveValue(
      "WT-PREFERRED 齿轮箱温度异常 Investigation",
    );
    await expect(dialog.getByLabel("部件标识", { exact: true })).toHaveValue("gearbox");
    await expect(dialog.getByLabel("主信号变量", { exact: true })).toHaveValue(
      "gearbox_condition_signal",
    );
    await expect(dialog.getByRole("textbox", { name: "知识检索查询", exact: true })).toHaveValue(
      "gearbox inspection failure diagnosis",
    );
    await selection.selectOption("ALARM-CREATE-001");
    await expect(dialog.getByLabel("部件标识", { exact: true })).toHaveValue("main_bearing");
    await expect(dialog.getByLabel("主信号变量", { exact: true })).toHaveValue(
      "bearing_vibration_rms",
    );
    await expect(dialog.getByRole("textbox", { name: "知识检索查询", exact: true })).toHaveValue(
      "main bearing inspection failure diagnosis",
    );
  } finally {
    await context.close();
  }
});
