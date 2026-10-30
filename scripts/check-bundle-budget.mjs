import { lstat, readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

export const bundleBudget = Object.freeze({
  maximumChunkBytes: 675 * 1024,
  maximumTotalBytes: 2700 * 1024,
  maximumChunksOver500KiB: 1,
});

async function listJavaScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const entryPath = path.join(directory, entry.name);
      if (entry.isDirectory()) return listJavaScriptFiles(entryPath);
      return entry.isFile() && entry.name.endsWith(".js") ? [entryPath] : [];
    }),
  );
  return nested.flat();
}

export async function measureBundle(chunksDirectory) {
  const files = await listJavaScriptFiles(chunksDirectory);
  const chunks = await Promise.all(
    files.map(async (filePath) => ({
      filePath,
      bytes: (await stat(filePath)).size,
    })),
  );
  chunks.sort((left, right) => right.bytes - left.bytes);
  return {
    chunks,
    totalBytes: chunks.reduce((total, chunk) => total + chunk.bytes, 0),
    chunksOver500KiB: chunks.filter((chunk) => chunk.bytes > 500 * 1024).length,
  };
}

export function evaluateBundle(measurement, budget = bundleBudget) {
  const failures = [];
  const largestChunk = measurement.chunks[0];
  if (!largestChunk) failures.push("no JavaScript chunks were found");
  if (largestChunk && largestChunk.bytes > budget.maximumChunkBytes) {
    failures.push(
      `largest chunk ${path.basename(largestChunk.filePath)} is ${largestChunk.bytes} bytes; ` +
        `budget is ${budget.maximumChunkBytes} bytes`,
    );
  }
  if (measurement.totalBytes > budget.maximumTotalBytes) {
    failures.push(
      `client JavaScript totals ${measurement.totalBytes} bytes; ` +
        `budget is ${budget.maximumTotalBytes} bytes`,
    );
  }
  if (measurement.chunksOver500KiB > budget.maximumChunksOver500KiB) {
    failures.push(
      `${measurement.chunksOver500KiB} chunks exceed 500 KiB; ` +
        `budget permits ${budget.maximumChunksOver500KiB}`,
    );
  }
  return failures;
}

export const reviewBundleBudget = Object.freeze({
  maximumUiBytes: 50 * 1024,
  maximumPdfCoreBytes: 450 * 1024,
  maximumPdfWorkerBytes: 1200 * 1024,
});

const reviewSources = Object.freeze([
  "components/pages/knowledge-source-comparison.tsx",
  "components/pages/knowledge-pdf-preview.tsx",
  "components/pages/knowledge-numeric-review.tsx",
]);
const pdfCoreSource =
  /^node_modules\/(?:\.pnpm\/pdfjs-dist@[^/]+\/node_modules\/)?pdfjs-dist\/build\/pdf\.mjs$/;
const pdfWorkerName = /^pdf\.worker(?:\.min)?-[A-Za-z0-9_-]+\.js$/;
const javaScriptName = /\.(?:[cm]?js)$/;

function summarizeChunks(chunks) {
  const sorted = [...chunks].sort((left, right) => right.bytes - left.bytes);
  return {
    chunks: sorted,
    totalBytes: sorted.reduce((total, chunk) => total + chunk.bytes, 0),
    chunksOver500KiB: sorted.filter((chunk) => chunk.bytes > 500 * 1024).length,
  };
}

function referencesWorker(source, workerUrl) {
  const parsed = ts.createSourceFile(
    "preview.js",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.JS,
  );
  const literalMatches = (node) =>
    node &&
    (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) &&
    node.text === workerUrl;
  let found = false;
  function visit(node) {
    if (
      ts.isNewExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === "Worker"
    ) {
      const target = node.arguments?.[0];
      if (
        literalMatches(target) ||
        (target &&
          ts.isNewExpression(target) &&
          ts.isIdentifier(target.expression) &&
          target.expression.text === "URL" &&
          literalMatches(target.arguments?.[0]))
      )
        found = true;
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  return parsed.parseDiagnostics.length === 0 && found;
}

// Build outputs are local inputs, but paths and symlinks must not escape this build.
async function checkedFile(clientRoot, relativePath) {
  if (
    typeof relativePath !== "string" ||
    !relativePath.startsWith("_next/static/") ||
    relativePath.includes("\\") ||
    relativePath.split("/").some((part) => !part || part === "." || part === "..") ||
    /[:?#%]/.test(relativePath) ||
    [...relativePath].some((character) => character.charCodeAt(0) < 32)
  )
    throw new Error("manifest contains an unsafe static file path");
  const pieces = relativePath.split("/");
  let current = clientRoot;
  for (let index = 0; index < pieces.length; index++) {
    current = path.join(current, pieces[index]);
    const information = await lstat(current);
    if (
      information.isSymbolicLink() ||
      (index === pieces.length - 1 ? !information.isFile() : !information.isDirectory())
    )
      throw new Error("build contains a linked or unsupported static file");
  }
  return current;
}

async function staticJavaScript(clientRoot, directory = "_next/static") {
  const absolute = path.join(clientRoot, directory);
  const information = await lstat(absolute);
  if (!information.isDirectory() || information.isSymbolicLink())
    throw new Error("static directory must be a real build directory");
  const chunks = [];
  for (const entry of await readdir(absolute, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) throw new Error("linked static build entries are forbidden");
    const relative = `${directory}/${entry.name}`;
    if (entry.isDirectory()) chunks.push(...(await staticJavaScript(clientRoot, relative)));
    else if (entry.isFile() && javaScriptName.test(entry.name)) {
      const filePath = await checkedFile(clientRoot, relative);
      chunks.push({ filePath, relativePath: relative, bytes: (await stat(filePath)).size });
    } else if (!entry.isFile()) throw new Error("unsupported static build entry");
  }
  return chunks;
}

export async function measureReviewBundle(clientDirectory) {
  const clientRoot = path.resolve(clientDirectory);
  for (const directory of [
    clientRoot,
    path.join(clientRoot, ".vite"),
    path.join(clientRoot, "_next"),
  ]) {
    const information = await lstat(directory);
    if (!information.isDirectory() || information.isSymbolicLink())
      throw new Error("bundle and manifest directories must not be links");
  }
  const manifestPath = path.join(clientRoot, ".vite/manifest.json");
  const information = await lstat(manifestPath);
  if (!information.isFile() || information.isSymbolicLink() || information.size > 4 * 1024 * 1024)
    throw new Error("missing or unsafe bundle manifest");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest))
    throw new Error("bundle manifest must be an object");
  const entries = Object.entries(manifest);
  const coreSources = entries
    .filter(([source]) => pdfCoreSource.test(source))
    .map(([source]) => source);
  if (coreSources.length !== 1) throw new Error("exactly one PDF core source is required");
  const featureSources = [...reviewSources, coreSources[0]];
  const featureSet = new Set(featureSources);
  const featureFiles = new Set();
  for (const source of featureSources) {
    const entry = manifest[source];
    if (
      !entry ||
      entry.src !== source ||
      entry.isDynamicEntry !== true ||
      entry.isEntry === true ||
      typeof entry.file !== "string" ||
      !entry.file.startsWith("_next/static/chunks/") ||
      !entry.file.endsWith(".js")
    )
      throw new Error(`review feature must be a distinct dynamic entry: ${source}`);
    if (featureFiles.has(entry.file)) throw new Error("review feature chunks must be distinct");
    featureFiles.add(entry.file);
  }
  const dynamicTargets = new Set();
  for (const [source, entry] of entries) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry))
      throw new Error("invalid manifest entry");
    await checkedFile(clientRoot, entry.file);
    if (!featureSet.has(source) && featureFiles.has(entry.file))
      throw new Error("review feature file is shared with another manifest module");
    for (const kind of ["imports", "dynamicImports"]) {
      const references = entry[kind] ?? [];
      if (!Array.isArray(references)) throw new Error("invalid manifest dependency list");
      for (const reference of references) {
        if (typeof reference !== "string" || !Object.hasOwn(manifest, reference))
          throw new Error("manifest dependency is missing");
        if (kind === "imports" && featureSet.has(reference))
          throw new Error("review feature is statically imported");
        if (kind === "dynamicImports") dynamicTargets.add(reference);
      }
    }
    for (const kind of ["css", "assets"]) {
      if (entry[kind] !== undefined && !Array.isArray(entry[kind]))
        throw new Error("invalid manifest asset list");
      for (const asset of entry[kind] ?? []) await checkedFile(clientRoot, asset);
    }
  }
  if (featureSources.some((source) => !dynamicTargets.has(source)))
    throw new Error("review feature has no dynamic import reference");
  const reachable = new Set();
  const pending = entries.filter(([, entry]) => entry.isEntry === true).map(([source]) => source);
  while (pending.length) {
    const source = pending.pop();
    if (reachable.has(source)) continue;
    reachable.add(source);
    pending.push(...(manifest[source].imports ?? []), ...(manifest[source].dynamicImports ?? []));
  }
  if (featureSources.some((source) => !reachable.has(source)))
    throw new Error("review feature is unreachable from application entries");
  if (!(manifest[reviewSources[1]].dynamicImports ?? []).includes(coreSources[0]))
    throw new Error("PDF preview must dynamically import the PDF core");
  const chunks = await staticJavaScript(clientRoot);
  const workers = chunks.filter((chunk) => path.basename(chunk.filePath).startsWith("pdf.worker"));
  if (workers.length !== 1 || !pdfWorkerName.test(path.basename(workers[0].filePath)))
    throw new Error("exactly one emitted PDF worker is required");
  if (featureFiles.has(workers[0].relativePath))
    throw new Error("PDF worker must be separate from review chunks");
  const preview = await readFile(
    await checkedFile(clientRoot, manifest[reviewSources[1]].file),
    "utf8",
  );
  const workerUrl = `/${workers[0].relativePath}`;
  // Inspect the emitted Worker construction; comments and unused URL strings do not count.
  if (!referencesWorker(preview, workerUrl))
    throw new Error("PDF preview does not reference its emitted same-origin worker");
  const uiFiles = new Set(reviewSources.map((source) => manifest[source].file));
  const application = summarizeChunks(
    chunks.filter((chunk) => !featureFiles.has(chunk.relativePath) && chunk !== workers[0]),
  );
  return {
    application,
    reviewUi: summarizeChunks(chunks.filter((chunk) => uiFiles.has(chunk.relativePath))),
    pdfCore: chunks.find((chunk) => chunk.relativePath === manifest[coreSources[0]].file),
    pdfWorker: workers[0],
    totalBytes: chunks.reduce((total, chunk) => total + chunk.bytes, 0),
    javaScriptFiles: chunks.length,
  };
}

export function evaluateReviewBundle(measurement, budget = reviewBundleBudget) {
  const failures = evaluateBundle(measurement.application);
  for (const [name, bytes, maximum] of [
    ["review UI", measurement.reviewUi.totalBytes, budget.maximumUiBytes],
    ["PDF core", measurement.pdfCore.bytes, budget.maximumPdfCoreBytes],
    ["PDF worker", measurement.pdfWorker.bytes, budget.maximumPdfWorkerBytes],
  ]) {
    if (bytes > maximum) failures.push(`${name} is ${bytes} bytes; budget is ${maximum} bytes`);
  }
  return failures;
}

export async function main(chunksDirectory = "dist/client/_next/static/chunks") {
  try {
    const measurement = await measureReviewBundle(path.resolve(chunksDirectory, "../../.."));
    const failures = evaluateReviewBundle(measurement);
    if (failures.length) {
      for (const failure of failures) console.error(`BUNDLE BUDGET FAILED: ${failure}`);
      return 1;
    }
    console.log(
      `Bundle budget passed: application ${measurement.application.totalBytes} bytes; ` +
        `lazy review UI ${measurement.reviewUi.totalBytes} bytes; ` +
        `PDF core ${measurement.pdfCore.bytes} bytes; worker ${measurement.pdfWorker.bytes} bytes; ` +
        `all ${measurement.javaScriptFiles} JavaScript files total ${measurement.totalBytes} bytes`,
    );
    return 0;
  } catch (error) {
    console.error(`BUNDLE BUDGET FAILED: ${error.message}`);
    return 1;
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = await main(process.argv[2]);
}
