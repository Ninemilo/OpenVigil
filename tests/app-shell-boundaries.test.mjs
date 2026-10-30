import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { runInThisContext } from "node:vm";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as linkShim from "vinext/shims/link";
import * as imageShim from "vinext/shims/image";
import ts from "typescript";

const root = path.resolve(import.meta.dirname, "..");
const require = createRequire(import.meta.url);
const cache = new Map();

// Compile the actual TSX owners without building shared dist/.next. External
// dependencies are real; next/link uses the same installed vinext shim as the app.
function load(relativePath) {
  const filename = path.resolve(root, relativePath);
  if (cache.has(filename)) return cache.get(filename).exports;
  if (filename.endsWith(".css")) {
    // Only preserve declared class names for SSR; browser layout remains a separate gate.
    return Object.fromEntries(
      [...readFileSync(filename, "utf8").matchAll(/\.([a-zA-Z_][\w-]*)/g)].map((match) => [
        match[1],
        match[1],
      ]),
    );
  }
  const compiledModule = { exports: {} };
  cache.set(filename, compiledModule);
  const localRequire = (specifier) => {
    if (specifier === "next/link") return { ...linkShim, __esModule: true };
    if (specifier === "next/image") return { ...imageShim, __esModule: true };
    if (!specifier.startsWith(".") && !specifier.startsWith("@/")) return require(specifier);
    const base = specifier.startsWith("@/")
      ? path.join(root, specifier.slice(2))
      : path.resolve(path.dirname(filename), specifier);
    const target = [base, `${base}.ts`, `${base}.tsx`, `${base}.js`].find(existsSync);
    assert.ok(target, `unresolved runtime import: ${specifier}`);
    return load(target);
  };
  const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {
    fileName: filename,
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  }).outputText;
  runInThisContext(`(function(require,module,exports){${compiled}\n})`, { filename })(
    localRequire,
    compiledModule,
    compiledModule.exports,
  );
  return compiledModule.exports;
}

const { navigation, visibleNavigationFor } = load("components/layout/app-shell-navigation.ts");
const { commandResults } = load("components/layout/app-shell-search.tsx");
const { ShellSidebar } = load("components/layout/app-shell-sidebar.tsx");
const { notificationItems, ShellNotifications } = load(
  "components/layout/app-shell-notifications.tsx",
);
const { IdentityProvider } = load("components/providers/identity-provider.tsx");
const { canonicalDemoWorkflow } = load("lib/demo-workflow.ts");
const { turbines } = load("lib/farm-data.ts");
const { alarms, missions, workOrders } = load("lib/operations-data.ts");
const { agents } = load("lib/agent-data.ts");

const expectedHrefs = [
  "/",
  "/wind-farms",
  "/turbines/WT-023",
  "/scada",
  "/health",
  "/structural",
  "/alarms",
  "/diagnosis",
  "/predictive-maintenance",
  "/agents",
  "/missions",
  "/decisions",
  "/work-orders",
  "/maintenance",
  "/resources",
  "/knowledge",
  "/digital-twin",
  "/knowledge-graph",
  "/data",
  "/reports",
  "/models",
  "/settings",
];

test("navigation retains every route in order and filters production permissions before groups", () => {
  assert.deepEqual(
    navigation.flatMap((group) => group.items.map((item) => item.href)),
    expectedHrefs,
  );
  assert.deepEqual(
    visibleNavigationFor("demo", () => false),
    navigation,
  );
  assert.deepEqual(
    visibleNavigationFor("production", () => false),
    [],
  );
  const knowledge = visibleNavigationFor(
    "production",
    (capability) => capability === "view.knowledge",
  );
  assert.deepEqual(
    knowledge.map((group) => group.label),
    ["知识与数据"],
  );
  assert.deepEqual(
    knowledge[0].items.map((item) => item.href),
    ["/knowledge", "/knowledge-graph"],
  );
  assert.equal(navigation.flatMap((group) => group.items).length, 22);
});

test("production search shares navigation permissions, remaps demo turbine links and leaks no demo IDs", () => {
  const all = commandResults("", "production", () => true);
  assert.deepEqual(
    all.map((item) => item.href),
    expectedHrefs.map((href) => (href === "/turbines/WT-023" ? "/wind-farms" : href)),
  );
  assert.equal(
    all.some((item) => /WT-023|MISSION-2026|ALARM-0031/.test(JSON.stringify(item))),
    false,
  );
  assert.deepEqual(
    commandResults("", "production", () => false),
    [],
  );
  assert.deepEqual(
    commandResults("WT-023", "production", () => true),
    [],
  );
  const knowledgeOnly = (capability) => capability === "view.knowledge";
  assert.deepEqual(
    commandResults("", "production", knowledgeOnly).map((item) => item.href),
    ["/knowledge", "/knowledge-graph"],
  );
  assert.deepEqual(commandResults("机组", "production", knowledgeOnly), []);
});

test("demo search preserves readonly action intents, complete domain lookup and normalized matching", () => {
  const defaults = commandResults("", "demo", () => false);
  assert.equal(defaults.length, 18);
  assert.equal(defaults[1].href, "/work-orders?turbineId=WT-023&intent=create");
  assert.match(defaults[1].description, /此命令不会创建工单/);
  assert.equal(defaults[2].href, "/missions?turbineId=WT-023&intent=diagnosis");
  assert.match(defaults[2].description, /不会启动新 Agent 或写入诊断/);
  assert.deepEqual(
    commandResults("  ", "demo", () => false),
    defaults,
  );
  assert.deepEqual(
    commandResults("  wt-023  ", "demo", () => false),
    commandResults("WT-023", "demo", () => true),
  );
  for (const [items, label] of [
    [turbines, "id"],
    [alarms, "id"],
    [missions, "id"],
    [workOrders, "id"],
    [agents, "shortName"],
  ]) {
    for (const item of items) {
      assert.ok(
        commandResults(item[label], "demo", () => false).some(
          (result) => result.label === item[label],
        ),
        item[label],
      );
    }
  }
  assert.deepEqual(
    commandResults("no-such-business-object-583279", "demo", () => true),
    [],
  );
});

test("notifications retain demo precedence, link kinds and production authority-only entries", () => {
  const cases = [
    [
      {
        missionStatus: "completed",
        decisionStatus: "under-review",
        workOrderStatus: "in-progress",
      },
      "WT-023 闭环已完成",
    ],
    [
      { missionStatus: "running", decisionStatus: "under-review", workOrderStatus: "in-progress" },
      "WT-023 等待人工审批",
    ],
    [
      { missionStatus: "running", decisionStatus: "approved", workOrderStatus: "in-progress" },
      "WT-023 现场任务执行中",
    ],
    [
      {
        missionStatus: "waiting-resources",
        decisionStatus: "approved",
        workOrderStatus: "pending",
      },
      "作业资源等待最终确认",
    ],
  ];
  for (const [state, title] of cases) {
    const items = notificationItems(false, { ...state, missionProgress: 37 });
    assert.equal(items[0][0], linkShim.default);
    assert.equal(items[0][4], title);
    assert.equal(items[0][5], `${state.missionStatus.replaceAll("-", " ")} · 37%`);
    assert.deepEqual(
      items.map(([tag, href]) => [typeof tag === "string" ? tag : "Link", href]),
      [
        ["Link", "/missions/MISSION-2026-0823"],
        ["a", "/alarms"],
        ["a", "/agents"],
      ],
    );
  }
  const production = notificationItems(true, canonicalDemoWorkflow);
  assert.deepEqual(
    production.map((item) => item[1]),
    ["/alarms", "/missions"],
  );
  assert.ok(production.every((item) => item[0] === linkShim.default));
  assert.doesNotMatch(JSON.stringify(production), /WT-023|ALARM-0031|3 条未读/);
});

test("notification render and controlled toggle preserve closed/open and demo/production states", () => {
  let toggles = 0;
  const props = {
    open: false,
    onToggle: () => {
      toggles++;
    },
    runtimeMode: "demo",
    workflow: canonicalDemoWorkflow,
  };
  const closed = renderToStaticMarkup(createElement(ShellNotifications, props));
  assert.match(closed, /aria-label="通知"/);
  assert.match(closed, /notification-dot/);
  assert.doesNotMatch(closed, /notification-popover/);
  const tree = ShellNotifications(props);
  tree.props.children[0].props.onClick();
  tree.props.children[0].props.onClick();
  assert.equal(toggles, 2);
  assert.equal(props.open, false);
  const demo = renderToStaticMarkup(createElement(ShellNotifications, { ...props, open: true }));
  assert.match(demo, /3 条未读/);
  assert.match(demo, /href="\/missions\/MISSION-2026-0823"/);
  const production = renderToStaticMarkup(
    createElement(ShellNotifications, { ...props, open: true, runtimeMode: "production" }),
  );
  assert.match(production, /权威事件入口/);
  assert.doesNotMatch(production, /notification-dot|WT-023|ALARM-0031|3 条未读/);
});

function sidebarMarkup({
  runtimeMode = "demo",
  mobileOpen = false,
  overlayNavigation = false,
  capabilities = [],
} = {}) {
  return renderToStaticMarkup(
    createElement(
      IdentityProvider,
      {
        runtimeMode,
        session: { capabilities },
      },
      createElement(ShellSidebar, {
        runtimeMode,
        activePath: "/knowledge",
        displayAgentCount: 9,
        pendingDecisionCount: 2,
        workflowKpis: {
          activeAlarmCount: 7,
          activeMissionCount: 8,
          activeAgentCount: 6,
          onlineAgentCount: 9,
        },
        systemHealth: {
          status: "offline",
          label: "Offline",
          detail: "权威依赖不可用",
          lastSuccessAt: null,
        },
        navigation: {
          collapsed: false,
          mobileOpen,
          overlayNavigation,
          setMobileOpen() {},
          sidebarDialogRef: { current: null },
          toggleCollapsed() {},
        },
      }),
    ),
  );
}

test("sidebar renders actual permission filtering, active link and mobile dialog/inert contracts", () => {
  const production = sidebarMarkup({ runtimeMode: "production", capabilities: ["view.knowledge"] });
  assert.match(production, /href="\/knowledge"[^>]*aria-current="page"/);
  assert.match(production, /href="\/knowledge-graph"/);
  assert.doesNotMatch(production, /href="\/alarms"|href="\/missions"|WT-023/);
  assert.match(production, /data-health="offline"/);
  assert.match(production, /Production · Offline/);
  assert.match(sidebarMarkup({ overlayNavigation: true }), /aria-hidden="true" inert=""/);
  const drawer = sidebarMarkup({ overlayNavigation: true, mobileOpen: true });
  assert.match(drawer, /role="dialog" aria-modal="true" aria-label="主导航抽屉"/);
  assert.match(drawer, /mobile-backdrop/);
  assert.doesNotMatch(drawer, /aria-hidden="true" inert=""/);
});

test("the stable shell exports only AppShell and delegates to bounded owners without reverse imports", () => {
  const files = [
    "app-shell.tsx",
    "app-shell-navigation.ts",
    "app-shell-sidebar.tsx",
    "app-shell-search.tsx",
    "app-shell-notifications.tsx",
  ];
  const parse = (file) =>
    ts.createSourceFile(
      file,
      readFileSync(path.join(root, "components/layout", file), "utf8"),
      ts.ScriptTarget.Latest,
      true,
    );
  const shell = parse(files[0]);
  const exports = shell.statements.filter((node) =>
    node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword),
  );
  assert.deepEqual(
    exports.map((node) => node.name?.text),
    ["AppShell"],
  );
  const imports = (file) =>
    parse(file)
      .statements.filter(ts.isImportDeclaration)
      .map((node) => node.moduleSpecifier.text);
  for (const owner of files.slice(1)) {
    assert.ok(imports("app-shell.tsx").includes(`./${owner.replace(/\.tsx?$/, "")}`), owner);
    assert.ok(
      imports(owner).every((specifier) => !/app-shell$|app-shell\.tsx$/.test(specifier)),
      owner,
    );
  }
  assert.ok(imports("app-shell-search.tsx").includes("./app-shell-navigation"));
  assert.ok(imports("app-shell-sidebar.tsx").includes("./app-shell-navigation"));
  assert.ok(
    imports("app-shell.tsx").every((specifier) => !specifier.endsWith("use-accessible-dialog")),
  );
});
