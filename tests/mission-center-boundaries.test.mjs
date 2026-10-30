import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

import {
  columns,
  formatResolutionDuration,
  formatUpdatedAt,
  severityOptions,
  severityTone,
  statusLabel,
} from "../components/pages/mission-center-support.ts";

const owners = Object.fromEntries(
  await Promise.all(
    [
      "mission-center-page.tsx",
      "mission-center-views.tsx",
      "mission-center-support.ts",
      "mission-create-drawer.tsx",
    ].map(async (filename) => {
      const source = await readFile(
        new URL(`../components/pages/${filename}`, import.meta.url),
        "utf8",
      );
      return [filename, ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true)];
    }),
  ),
);

function declarations(sourceFile) {
  return sourceFile.statements.flatMap((statement) =>
    ts.isVariableStatement(statement)
      ? statement.declarationList.declarations.map((declaration) =>
          declaration.name.getText(sourceFile),
        )
      : statement.name
        ? [statement.name.getText(sourceFile)]
        : [],
  );
}

function exportedNames(sourceFile) {
  return declarations({
    ...sourceFile,
    statements: sourceFile.statements.filter((statement) =>
      statement.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword),
    ),
  }).sort();
}

test("mission page exports its original entry and each view or command has one owner", () => {
  assert.deepEqual(exportedNames(owners["mission-center-page.tsx"]), ["MissionCenterPage"]);
  assert.deepEqual(exportedNames(owners["mission-center-views.tsx"]), [
    "MissionCard",
    "MissionList",
    "MissionTimeline",
  ]);
  assert.deepEqual(exportedNames(owners["mission-create-drawer.tsx"]), ["CreateMissionDrawer"]);

  for (const [name, expectedOwner] of [
    ["MissionCenterPage", "mission-center-page.tsx"],
    ["MissionCard", "mission-center-views.tsx"],
    ["MissionList", "mission-center-views.tsx"],
    ["MissionTimeline", "mission-center-views.tsx"],
    ["CreateMissionDrawer", "mission-create-drawer.tsx"],
    ["columns", "mission-center-support.ts"],
    ["statusLabel", "mission-center-support.ts"],
  ]) {
    assert.deepEqual(
      Object.entries(owners)
        .filter(([, sourceFile]) => declarations(sourceFile).includes(name))
        .map(([filename]) => filename),
      [expectedOwner],
      name,
    );
  }

  const localImports = (filename) =>
    owners[filename].statements
      .filter(ts.isImportDeclaration)
      .map((statement) => statement.moduleSpecifier.text)
      .filter((specifier) => specifier.startsWith("./mission-"));
  assert.deepEqual(localImports("mission-center-page.tsx"), [
    "./mission-center-support",
    "./mission-center-support",
    "./mission-center-views",
    "./mission-create-drawer",
  ]);
  assert.deepEqual(localImports("mission-center-views.tsx"), ["./mission-center-support"]);
  assert.deepEqual(localImports("mission-create-drawer.tsx"), []);
  assert.deepEqual(localImports("mission-center-support.ts"), []);
});

test("mission status and severity presentation retains the complete workflow ordering and fallback", () => {
  assert.deepEqual(
    columns.map(({ status }) => status),
    [
      "detected",
      "investigating",
      "diagnosed",
      "decision-pending",
      "under-review",
      "approved",
      "executing",
      "rejected",
      "completed",
    ],
  );
  assert.equal(new Set(columns.map(({ status }) => status)).size, columns.length);
  for (const column of columns) {
    assert.equal(statusLabel(column.status), column.label);
    assert.ok(column.description.length > 0);
  }
  assert.equal(statusLabel("future-status"), "future-status");
  assert.deepEqual(severityOptions, ["critical", "high", "medium", "low"]);
  assert.deepEqual(severityOptions.map(severityTone), ["critical", "warning", "info", "neutral"]);
  assert.equal(severityTone("future-severity"), "neutral");
});

test("mission duration formatting retains minute rounding at the hour boundary and local timestamps", () => {
  for (const [minutes, expected] of [
    [0, "0m"],
    [1.49, "1m"],
    [1.5, "2m"],
    [59.49, "59m"],
    [59.5, "1h 0m"],
    [60, "1h 0m"],
    [61, "1h 1m"],
    [119.5, "2h 0m"],
  ]) {
    assert.equal(formatResolutionDuration(minutes), expected);
  }
  const timestamp = "2026-10-29T01:05:00Z";
  assert.equal(
    formatUpdatedAt(timestamp),
    new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(timestamp)),
  );
});
