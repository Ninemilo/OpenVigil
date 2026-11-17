import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const styleFiles = [
  "01-foundations.css",
  "02-shell-and-shared.css",
  "03-dashboard.css",
  "04-fleet.css",
  "05-asset-detail.css",
  "06-alarms.css",
  "07-agents.css",
  "08-missions.css",
  "09-decisions.css",
  "10-work-orders.css",
  "11-overlays-and-responsive.css",
  "12-health.css",
  "13-access-recovery.css",
  "14-motion.css",
  "15-button-glass.css",
];

test("the global stylesheet preserves import order and the reviewed rule stream", async () => {
  const entry = await readFile(new URL("app/globals.css", root), "utf8");
  const imports = [...entry.matchAll(/@import "\.\/styles\/([^"]+)";/g)].map((match) => match[1]);
  assert.deepEqual(imports, styleFiles);
  assert.doesNotMatch(entry, /(^|\n)\s*[^@\s][^{]*\{/);

  const modules = await Promise.all(
    styleFiles.map((file) => readFile(new URL(`app/styles/${file}`, root), "utf8")),
  );
  const ruleStream = modules.join("").replaceAll(/\s+/g, "");
  // Includes the guide entry's desktop and mobile top-bar rules.
  assert.equal(
    createHash("sha256").update(ruleStream).digest("hex"),
    "c1f5174a355cda509f9985e5e67335a79b4457cb3c65ab564f4f7b586efc0605",
  );
});
