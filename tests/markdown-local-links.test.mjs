import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const repositoryRoot = path.resolve(import.meta.dirname, "..");
const repositoryFiles = execFileSync("git", ["ls-files", "-coz", "--exclude-standard"], {
  cwd: repositoryRoot,
  encoding: "utf8",
})
  .split("\0")
  .filter(Boolean);
const markdownFiles = repositoryFiles.filter((file) => file.endsWith(".md"));

const localTarget = (rawTarget) => {
  let target = rawTarget.trim();
  if (target.startsWith("<") && target.endsWith(">")) target = target.slice(1, -1);
  target = target.replace(/\s+(?:"[^"]*"|'[^']*')$/, "");
  if (
    !target ||
    target.startsWith("#") ||
    target.startsWith("/") ||
    /^[a-z][a-z0-9+.-]*:/i.test(target)
  ) {
    return null;
  }
  const withoutFragment = target.split("#", 1)[0].split("?", 1)[0];
  try {
    return decodeURIComponent(withoutFragment);
  } catch {
    return withoutFragment;
  }
};

test("all Markdown local links resolve inside the repository", () => {
  const missing = [];
  for (const relativeFile of markdownFiles) {
    const source = readFileSync(path.join(repositoryRoot, relativeFile), "utf8");
    const targets = [
      ...[...source.matchAll(/!?\[[^\]]*\]\(([^)]+)\)/g)].map((match) => match[1]),
      ...[...source.matchAll(/^\s*\[[^\]]+\]:\s*(\S+)/gm)].map((match) => match[1]),
    ];
    for (const rawTarget of targets) {
      const target = localTarget(rawTarget);
      if (!target) continue;
      const resolved = path.resolve(repositoryRoot, path.dirname(relativeFile), target);
      const relativeTarget = path.relative(repositoryRoot, resolved).split(path.sep).join("/");
      // Ignored local files must not hide links that break in a clean CI clone.
      const included = repositoryFiles.some(
        (file) => file === relativeTarget || file.startsWith(`${relativeTarget}/`),
      );
      if (!included || !existsSync(resolved)) missing.push(`${relativeFile} -> ${target}`);
    }
  }
  assert.deepEqual(missing, []);
});
