import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { runBusinessE2E } from "../scripts/run-business-e2e.mjs";

test("business launcher preserves scenarios and paths as distinct arguments without a shell", () => {
  for (const scenario of [
    "maintenance",
    "structural",
    "structural-modal",
    "structural-scope",
    "document",
  ]) {
    const args = ["--scenario", scenario, "--document-fixture", "a path with spaces/$().pdf"];
    let observed;
    const code = runBusinessE2E(args, {
      python: process.execPath,
      spawn: (...values) => {
        observed = values;
        return { status: 0 };
      },
    });
    assert.equal(code, 0);
    assert.equal(observed[0], process.execPath);
    assert.deepEqual(observed[1], [
      fileURLToPath(new URL("../backend/scripts/run_business_e2e.py", import.meta.url)),
      ...args,
    ]);
    assert.equal(observed[2].shell, undefined);
    assert.equal(observed[2].windowsHide, true);
    assert.equal(observed[2].stdio, "inherit");
  }
});

test("business launcher retains failures, signal termination and startup errors", () => {
  for (const status of [0, 1, 2, 17, null]) {
    assert.equal(
      runBusinessE2E([], { python: process.execPath, spawn: () => ({ status }) }),
      status ?? 1,
    );
  }
  const error = new Error("unable to start interpreter");
  assert.throws(
    () => runBusinessE2E([], { python: process.execPath, spawn: () => ({ error }) }),
    error,
  );
});
