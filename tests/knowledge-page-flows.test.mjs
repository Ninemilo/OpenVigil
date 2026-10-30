import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

import { StructuralArtifactCommand } from "../lib/structural-artifact-command.ts";

const require = createRequire(import.meta.url);
const uploadSource = readFileSync(
  new URL("../components/pages/knowledge-upload.tsx", import.meta.url),
  "utf8",
);

// This is an explicit hook-state driver, not a substitute for real React/browser
// validation. The actual extracted hook, API client and shared command class run;
// only hook scheduling, unrendered UI imports and the HTTP boundary are fixtures.
function uploadHarness({ configured = true, maximum = 50 * 1024 * 1024 } = {}) {
  const slots = [];
  let cursor = 0;
  let refetches = 0;
  const hooks = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], (value) => (slots[index] = value)];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = { current: initial };
      return slots[index];
    },
  };
  const loaded = { exports: {} };
  const compiled = ts.transpileModule(uploadSource, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
  const importedCommands = [];
  const load = (name) => {
    if (name === "react") return hooks;
    if (name === "@/lib/structural-artifact-command") {
      importedCommands.push(StructuralArtifactCommand);
      return { StructuralArtifactCommand };
    }
    if (name === "@/components/ui/primitives") return { Button: () => null };
    if (name.endsWith(".module.css")) return {};
    if (name === "react/jsx-runtime" || name === "lucide-react") return require(name);
    throw new Error(`Unexpected runtime import: ${name}`);
  };
  vm.runInThisContext(`(function(require,module,exports){${compiled}\n})`, {
    filename: "knowledge-upload-transpiled-test.cjs",
  })(load, loaded, loaded.exports);
  assert.deepEqual(importedCommands, [StructuralArtifactCommand]);
  const capabilities = {
    data: { docling_pdf_configured: configured, max_artifact_bytes: maximum },
  };
  const documentQuery = { refetch: async () => ++refetches };
  return {
    capabilities,
    render() {
      cursor = 0;
      return loaded.exports.useKnowledgeUpload(capabilities, documentQuery);
    },
    refetches: () => refetches,
  };
}

function transport(t, registration, put = async () => new Response(null)) {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, init) => {
    const call = { url: String(url), ...init };
    calls.push(call);
    if (call.url === "/api/backend/knowledge/documents/uploads/presign") {
      const body = JSON.parse(call.body);
      return Response.json({
        artifact_uri: `minio://declared-fixture/documents/${body.document_id}/source.pdf`,
        upload_url: "https://fixture.invalid/knowledge-upload",
        required_headers: { "Content-Type": body.content_type },
        expires_at: "2099-01-01T00:00:00Z",
      });
    }
    if (call.url === "https://fixture.invalid/knowledge-upload") return put(call);
    assert.equal(call.url, "/api/backend/knowledge/documents");
    return registration(call);
  });
  return calls;
}

const registered = (call) =>
  Response.json({
    document_id: JSON.parse(call.body).document_id,
    ingestion_status: "pending_parse",
  });

test("knowledge upload retains its exact file, parser, identity and key through unknown/403 replay", async (t) => {
  let stage = 0;
  const calls = transport(t, (call) =>
    stage < 2
      ? Response.json(
          { error: { code: "DECLARED_TRANSPORT", message: "Declared response unavailable" } },
          { status: stage === 0 ? 503 : 403 },
        )
      : registered(call),
  );
  const harness = uploadHarness();
  let upload = harness.render();
  upload.setParseScannedPdf(true);
  upload = harness.render();
  const file = new File(["%PDF-declared-wire-fixture"], "scan.pdf", { type: "application/pdf" });
  upload.uploadInputRef.current = { value: "synthetic file input value" };
  await upload.uploadKnowledgeDocument(file);
  upload = harness.render();
  assert.equal(upload.uploadUnknown, true);
  assert.equal(upload.uploading, false);
  assert.equal(upload.uploadInputRef.current.value, "");
  assert.equal(upload.frozenUpload.current.file, file);
  const frozenId = upload.frozenUpload.current.documentId;
  assert.equal(harness.refetches(), 0);
  // A capabilities refresh or a stale handler cannot replace a pending command.
  harness.capabilities.data = { docling_pdf_configured: false, max_artifact_bytes: 1 };
  upload.setParseScannedPdf(false);
  stage = 1;
  upload = harness.render();
  await upload.uploadKnowledgeDocument(new File(["different"], "replacement.txt"));
  upload = harness.render();
  assert.equal(upload.uploadUnknown, true);
  assert.equal(upload.frozenUpload.current.file, file);
  stage = 2;
  await upload.uploadKnowledgeDocument(file);
  upload = harness.render();
  assert.equal(upload.uploadUnknown, false);
  assert.equal(upload.frozenUpload.current, null);
  assert.equal(upload.uploadError, null);
  assert.equal(harness.refetches(), 1);
  const presigns = calls.filter((call) => call.url.endsWith("presign"));
  const puts = calls.filter((call) => call.method === "PUT");
  const commands = calls.filter((call) => call.url === "/api/backend/knowledge/documents");
  assert.equal(presigns.length, 1);
  assert.equal(puts.length, 1);
  assert.equal(puts[0].body, file);
  assert.equal(puts[0].credentials, "omit");
  assert.equal(commands.length, 3);
  assert.equal(new Set(commands.map((call) => call.body)).size, 1);
  const keys = commands.map((call) => new Headers(call.headers).get("idempotency-key"));
  assert.ok(keys[0]);
  assert.deepEqual(keys, [keys[0], keys[0], keys[0]]);
  const body = JSON.parse(commands[0].body);
  assert.deepEqual(body, {
    document_id: frozenId,
    title: "scan",
    document_type: "maintenance-procedure",
    document_version: "1",
    content_type: "application/pdf",
    parser: "docling",
    metadata: { summary: "scan", language: "zh-CN", tags: ["uploaded"] },
    artifact_uri: `minio://declared-fixture/documents/${frozenId}/source.pdf`,
    artifact_sha256: createHash("sha256")
      .update(Buffer.from(await file.arrayBuffer()))
      .digest("hex"),
  });
});

test("repeated knowledge upload clicks start only one command and complete one catalog refresh", async (t) => {
  let release;
  let entered;
  const waiting = new Promise((resolve) => (entered = resolve));
  const pending = new Promise((resolve) => (release = resolve));
  const calls = transport(t, registered, async () => {
    entered();
    await pending;
    return new Response(null);
  });
  const harness = uploadHarness();
  const file = new File(["knowledge"], "knowledge.txt", { type: "text/plain" });
  const first = harness.render().uploadKnowledgeDocument(file);
  try {
    await waiting;
    const uploading = harness.render();
    assert.equal(uploading.uploading, true);
    await uploading.uploadKnowledgeDocument(file);
    assert.equal(calls.length, 2);
  } finally {
    release();
    await first;
  }
  assert.equal(harness.render().uploading, false);
  assert.equal(harness.refetches(), 1);
  assert.equal(calls.length, 3);
  assert.equal(JSON.parse(calls[2].body).parser, "native");
});

test("invalid knowledge successful response stays unknown and replays the original registration", async (t) => {
  let commands = 0;
  const calls = transport(t, (call) =>
    ++commands === 1
      ? Response.json({ document_id: "wrong-document", ingestion_status: "pending_parse" })
      : registered(call),
  );
  const harness = uploadHarness();
  const file = new File(["knowledge"], "knowledge.md");
  await harness.render().uploadKnowledgeDocument(file);
  assert.equal(harness.render().uploadUnknown, true);
  await harness.render().uploadKnowledgeDocument(file);
  assert.equal(harness.render().uploadUnknown, false);
  assert.equal(harness.refetches(), 1);
  const requests = calls.filter((call) => call.url === "/api/backend/knowledge/documents");
  assert.equal(requests[0].body, requests[1].body);
  assert.equal(
    new Headers(requests[0].headers).get("idempotency-key"),
    new Headers(requests[1].headers).get("idempotency-key"),
  );
});

test("knowledge format/size/configuration failures cannot request an upload grant", async (t) => {
  t.mock.method(globalThis, "fetch", () => assert.fail("validation must precede HTTP"));
  for (const [file, options, scanned, message] of [
    [new File(["bad"], "image.png", { type: "image/png" }), {}, false, /仅支持/],
    [new File(["big"], "long.txt"), { maximum: 2 }, false, /不能超过/],
    [new File(["pdf"], "scan.pdf"), { configured: false }, true, /尚未配置/],
    [new File([], "empty.txt"), {}, false, /1 Byte/],
  ]) {
    const harness = uploadHarness(options);
    harness.render().setParseScannedPdf(scanned);
    await harness.render().uploadKnowledgeDocument(file);
    assert.match(harness.render().uploadError, message);
    assert.equal(harness.render().uploadUnknown, false);
    assert.equal(harness.render().uploading, false);
    assert.equal(harness.refetches(), 0);
  }
});

test("knowledge public page keeps table/source boundaries and imports both extracted responsibilities", () => {
  const page = readFileSync(
    new URL("../components/pages/knowledge-base-page.tsx", import.meta.url),
    "utf8",
  );
  const ast = ts.createSourceFile(
    "knowledge-base-page.tsx",
    page,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  assert.deepEqual(
    ast.statements
      .filter(
        (node) =>
          ts.isFunctionDeclaration(node) &&
          node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword),
      )
      .map((node) => node.name.text),
    ["KnowledgeBasePage"],
  );
  for (const component of [
    "KnowledgeUploadActions",
    "KnowledgeUploadFeedback",
    "KnowledgeAssistantPanel",
    "KnowledgeSourceViewer",
    "KnowledgeCitationDrawer",
    "KnowledgeParserHealth",
    "DataTable",
  ])
    assert.match(page, new RegExp(`<${component}\\b`));
  assert.match(page, /useKnowledgeUpload\(parserCapabilities, documentQuery\)/);
  assert.doesNotMatch(page, /apiPost\(|new KnowledgeArtifactCommand/);
});
