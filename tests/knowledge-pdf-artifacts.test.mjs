import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import test from "node:test";

test("the three PDF browser fixtures survive Git selection without text normalization", () => {
  for (const name of ["plain", "rotated", "cropped"]) {
    const path = `tests/fixtures/knowledge-pdf/${name}.pdf`;
    const ignored = spawnSync("git", ["check-ignore", "--no-index", "--quiet", path]);
    assert.equal(ignored.status, 1, `${path} must not be ignored`);
    const attributes = execFileSync("git", ["check-attr", "binary", "text", "--", path], {
      encoding: "utf8",
    });
    assert.equal(attributes, `${path}: binary: set\n${path}: text: unset\n`);
  }
  const unrelated = spawnSync("git", ["check-ignore", "--no-index", "--quiet", "private.pdf"]);
  assert.equal(unrelated.status, 0, "unrelated private PDFs remain ignored");
});
