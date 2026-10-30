import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  bundleBudget,
  evaluateBundle,
  evaluateReviewBundle,
  measureBundle,
  measureReviewBundle,
  reviewBundleBudget,
} from "../scripts/check-bundle-budget.mjs";

async function withChunks(files, assertion) {
  const root = await mkdtemp(path.join(tmpdir(), "windops-bundle-budget-"));
  try {
    for (const [relativePath, bytes] of Object.entries(files)) {
      const filePath = path.join(root, relativePath);
      await mkdir(path.dirname(filePath), { recursive: true });
      await writeFile(filePath, Buffer.alloc(bytes));
    }
    await assertion(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("bundle budget measures nested JavaScript chunks without counting maps", async () => {
  await withChunks(
    { "route-a.js": 40, "nested/route-b.js": 70, "nested/route-b.js.map": 500 },
    async (root) => {
      const measurement = await measureBundle(root);
      assert.equal(measurement.chunks.length, 2);
      assert.equal(measurement.totalBytes, 110);
      assert.equal(path.basename(measurement.chunks[0].filePath), "route-b.js");
      assert.deepEqual(
        evaluateBundle(measurement, {
          maximumChunkBytes: 70,
          maximumTotalBytes: 110,
          maximumChunksOver500KiB: 0,
        }),
        [],
      );
    },
  );
});

test("bundle budget reports chunk, total, and large-chunk regressions", async () => {
  await withChunks({ "route-a.js": 600 * 1024, "route-b.js": 550 * 1024 }, async (root) => {
    const failures = evaluateBundle(await measureBundle(root), {
      maximumChunkBytes: 575 * 1024,
      maximumTotalBytes: 1100 * 1024,
      maximumChunksOver500KiB: 1,
    });
    assert.equal(failures.length, 3);
    assert.ok(failures.some((failure) => failure.includes("largest chunk")));
    assert.ok(failures.some((failure) => failure.includes("totals")));
    assert.ok(failures.some((failure) => failure.includes("exceed 500 KiB")));
  });
});

test("bundle budget fails closed when the build emitted no JavaScript", async () => {
  await withChunks({}, async (root) => {
    assert.deepEqual(
      evaluateBundle(await measureBundle(root), {
        maximumChunkBytes: 1,
        maximumTotalBytes: 1,
        maximumChunksOver500KiB: 0,
      }),
      ["no JavaScript chunks were found"],
    );
  });
});

const comparison = "components/pages/knowledge-source-comparison.tsx";
const preview = "components/pages/knowledge-pdf-preview.tsx";
const numericReview = "components/pages/knowledge-numeric-review.tsx";
const pdfCore = "node_modules/.pnpm/pdfjs-dist@6.4.299/node_modules/pdfjs-dist/build/pdf.mjs";
const workerFile = "_next/static/pdf.worker.min-fixture.js";

function reviewFixture() {
  const manifest = {
    app: {
      file: "_next/static/chunks/app.js",
      isEntry: true,
      dynamicImports: [comparison],
    },
  };
  for (const [source, file, dynamicImports] of [
    [comparison, "comparison.js", [preview, numericReview]],
    [preview, "preview.js", [pdfCore]],
    [numericReview, "numeric.js", []],
    [pdfCore, "pdf.js", []],
  ]) {
    manifest[source] = {
      file: `_next/static/chunks/${file}`,
      src: source,
      isDynamicEntry: true,
      dynamicImports,
    };
  }
  const files = Object.fromEntries(Object.values(manifest).map((entry) => [entry.file, 128]));
  files[manifest[preview].file] = `new Worker(new URL("/${workerFile}", import.meta.url));`.padEnd(
    128,
  );
  files[workerFile] = 256;
  return { manifest, files };
}

async function withReviewBuild(change, assertion) {
  const base = path.resolve(".artifacts/bundle-budget-tests");
  await mkdir(base, { recursive: true });
  const root = await mkdtemp(path.join(base, "build-"));
  try {
    const fixture = reviewFixture();
    change(fixture);
    await mkdir(path.join(root, ".vite"));
    if (fixture.manifest !== undefined)
      await writeFile(path.join(root, ".vite/manifest.json"), JSON.stringify(fixture.manifest));
    for (const [relative, value] of Object.entries(fixture.files)) {
      const file = path.join(root, relative);
      await mkdir(path.dirname(file), { recursive: true });
      await writeFile(file, typeof value === "number" ? Buffer.alloc(value) : value);
    }
    await assertion(root);
  } finally {
    assert.ok(root.startsWith(`${base}${path.sep}`), "test cleanup escaped its directory");
    await rm(root, { recursive: true, force: true });
  }
}

test("lazy review budgets preserve every original application ceiling and count the worker", async () => {
  assert.deepEqual(bundleBudget, {
    maximumChunkBytes: 675 * 1024,
    maximumTotalBytes: 2700 * 1024,
    maximumChunksOver500KiB: 1,
  });
  assert.deepEqual(reviewBundleBudget, {
    maximumUiBytes: 50 * 1024,
    maximumPdfCoreBytes: 450 * 1024,
    maximumPdfWorkerBytes: 1200 * 1024,
  });
  await withReviewBuild(
    ({ files }) => {
      files["_next/static/otherwise-unlisted.js"] = 17;
      files["_next/static/nested/module.mjs"] = 19;
      files["_next/static/chunks/app.js.map"] = 50;
    },
    async (root) => {
      const measured = await measureReviewBundle(root);
      assert.equal(measured.application.totalBytes, 128 + 17 + 19);
      assert.equal(measured.reviewUi.totalBytes, 384);
      assert.equal(measured.pdfCore.bytes, 128);
      assert.equal(measured.pdfWorker.bytes, 256);
      assert.equal(measured.totalBytes, 128 * 5 + 256 + 17 + 19);
      assert.equal(measured.javaScriptFiles, 8);
      assert.deepEqual(evaluateReviewBundle(measured), []);
    },
  );
});

const invalidBuilds = [
  ["missing manifest", (fixture) => delete fixture.manifest, /ENOENT/],
  ["nonobject manifest", (fixture) => (fixture.manifest = []), /must be an object/],
  ["missing comparison module", ({ manifest }) => delete manifest[comparison], /dynamic entry/],
  ["missing PDF core module", ({ manifest }) => delete manifest[pdfCore], /one PDF core/],
  ["eager review entry", ({ manifest }) => (manifest[comparison].isEntry = true), /dynamic entry/],
  [
    "non-dynamic PDF entry",
    ({ manifest }) => (manifest[pdfCore].isDynamicEntry = false),
    /dynamic entry/,
  ],
  [
    "wrong source identity",
    ({ manifest }) => (manifest[preview].src = "other.tsx"),
    /dynamic entry/,
  ],
  [
    "static feature import",
    ({ manifest }) => (manifest.app.imports = [preview]),
    /statically imported/,
  ],
  [
    "indirect static feature import",
    ({ manifest, files }) => {
      manifest.app.imports = ["shared"];
      manifest.shared = { file: "_next/static/chunks/shared.js", imports: [pdfCore] };
      files[manifest.shared.file] = 10;
    },
    /statically imported/,
  ],
  ["orphan feature", ({ manifest }) => (manifest.app.dynamicImports = []), /no dynamic import/],
  [
    "unreachable dynamic cycle",
    ({ manifest }) => {
      manifest.app.dynamicImports = [];
      manifest[numericReview].dynamicImports = [comparison];
    },
    /unreachable/,
  ],
  [
    "core outside preview",
    ({ manifest }) => {
      manifest[preview].dynamicImports = [];
      manifest.app.dynamicImports.push(pdfCore);
    },
    /preview must dynamically import/,
  ],
  [
    "feature alias mixed with another module",
    ({ manifest }) => {
      manifest.unrelated = { file: manifest[preview].file };
    },
    /shared with another/,
  ],
  [
    "two feature sources in one file",
    ({ manifest }) => {
      manifest[numericReview].file = manifest[comparison].file;
    },
    /must be distinct/,
  ],
  [
    "missing imported module",
    ({ manifest }) => (manifest.app.imports = ["absent"]),
    /dependency is missing/,
  ],
  [
    "unsafe path traversal",
    ({ manifest }) => (manifest.app.file = "_next/static/../../outside.js"),
    /unsafe static/,
  ],
  [
    "absolute static path",
    ({ manifest }) => (manifest.app.file = "/_next/static/chunks/app.js"),
    /unsafe static/,
  ],
  [
    "encoded path traversal",
    ({ manifest }) => (manifest.app.file = "_next/static/%2e%2e/app.js"),
    /unsafe static/,
  ],
  [
    "missing emitted feature file",
    ({ manifest, files }) => delete files[manifest[numericReview].file],
    /ENOENT/,
  ],
  [
    "missing emitted stylesheet",
    ({ manifest }) => (manifest[preview].css = ["_next/static/absent.css"]),
    /ENOENT/,
  ],
  ["missing worker", ({ files }) => delete files[workerFile], /one emitted PDF worker/],
  [
    "multiple workers",
    ({ files }) => (files["_next/static/nested/pdf.worker-extra.js"] = 10),
    /one emitted PDF worker/,
  ],
  [
    "unused local worker",
    ({ manifest, files }) => (files[manifest[preview].file] = "export const preview = 1;"),
    /same-origin worker/,
  ],
  [
    "remote worker URL",
    ({ manifest, files }) => {
      files[manifest[preview].file] = `new Worker("https://example.invalid/${workerFile}");`;
    },
    /same-origin worker/,
  ],
  [
    "worker mentioned only in a comment",
    ({ manifest, files }) => {
      files[manifest[preview].file] = `/* new Worker("/${workerFile}"); */`;
    },
    /same-origin worker/,
  ],
  [
    "unused worker URL literal",
    ({ manifest, files }) => {
      files[manifest[preview].file] = `const unused = "/${workerFile}"; new Worker("/other.js");`;
    },
    /same-origin worker/,
  ],
];
for (const [name, change, failure] of invalidBuilds) {
  test(`review bundle fails closed for ${name}`, async () => {
    await withReviewBuild(change, async (root) => {
      await assert.rejects(() => measureReviewBundle(root), failure);
    });
  });
}

test("review UI, PDF core, and worker have independent strict ceilings", async () => {
  await withReviewBuild(
    ({ manifest, files }) => {
      files[manifest[comparison].file] = reviewBundleBudget.maximumUiBytes - 256;
      files[manifest[pdfCore].file] = reviewBundleBudget.maximumPdfCoreBytes;
      files[workerFile] = reviewBundleBudget.maximumPdfWorkerBytes;
    },
    async (root) => {
      const measured = await measureReviewBundle(root);
      assert.deepEqual(evaluateReviewBundle(measured), []);
      for (const [component, message] of [
        ["reviewUi", "review UI"],
        ["pdfCore", "PDF core"],
        ["pdfWorker", "PDF worker"],
      ]) {
        const changed = structuredClone(measured);
        changed[component][component === "reviewUi" ? "totalBytes" : "bytes"]++;
        const failures = evaluateReviewBundle(changed);
        assert.equal(failures.length, 1);
        assert.ok(failures[0].startsWith(message));
      }
    },
  );
});

test("unlisted JS and shared dependencies cannot be hidden in the review exemption", async () => {
  await withReviewBuild(
    ({ manifest, files }) => {
      manifest[preview].imports = ["large-dependency"];
      manifest["large-dependency"] = { file: "_next/static/chunks/dependency.js" };
      files[manifest["large-dependency"].file] = 675 * 1024;
      files["_next/static/unlisted.js"] = 675 * 1024 + 1;
      files["_next/static/a.js"] = 675 * 1024;
      files["_next/static/b.js"] = 675 * 1024;
    },
    async (root) => {
      const measured = await measureReviewBundle(root);
      const failures = evaluateReviewBundle(measured);
      assert.equal(failures.length, 3);
      assert.ok(failures.some((failure) => failure.includes("largest chunk")));
      assert.ok(failures.some((failure) => failure.includes("totals")));
      assert.ok(failures.some((failure) => failure.includes("exceed 500 KiB")));
    },
  );
});
