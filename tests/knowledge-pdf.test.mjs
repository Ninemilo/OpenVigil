import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import {
  fetchVerifiedPdf,
  pdfHighlights,
  pdfRenderScale,
  PDF_MAX_BYTES,
  PDF_MAX_PIXELS,
  PDF_MAX_SIDE,
} from "../lib/knowledge-pdf.ts";

const bytes = new TextEncoder().encode(
  "%PDF-1.7\nsynthetic bytes for hash/stream checks only\n%%EOF\n",
);
const sha = createHash("sha256").update(bytes).digest("hex");
const passage = {
  passage_id: "passage",
  document_id: "document",
  document_version: "v1",
  source_sha256: sha,
  page_number: 2,
  ordinal: 0,
  section: null,
  text: "Synthetic region",
  text_sha256: "a".repeat(64),
  native_locator: {
    kind: "docling_pdf_item",
    regions: [
      {
        page_number: 2,
        page_width: 300,
        page_height: 400,
        coordinate_space: "pdf_points_top_left",
        bbox: [30, 80, 120, 120],
      },
    ],
  },
};
const source = () => ({
  document_id: "document",
  document_version: "v1",
  passage_id: "passage",
  artifact_sha256: sha,
  content_type: "application/pdf",
  native_locator: passage.native_locator,
  page_number: 2,
  download_url: "https://source.invalid/private.pdf",
  expires_at: new Date(Date.now() + 60_000).toISOString(),
});
const geometry = { pageNumber: 2, rotate: 0, userUnit: 1, view: [0, 0, 300, 400] };
const viewport = {
  width: 600,
  height: 800,
  convertToViewportPoint: (x, y) => [x * 2, (400 - y) * 2],
};

test("verified bytes use a bounded credential-free fetch, actual digest and original byte identity", async (t) => {
  let observed;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    observed = { url, options };
    return new Response(bytes, {
      headers: { "content-type": "application/pdf", "content-length": String(bytes.length) },
    });
  });
  const result = await fetchVerifiedPdf(passage, source(), new AbortController().signal);
  assert.deepEqual(result, bytes);
  assert.equal(observed.options.credentials, "omit");
  assert.equal(observed.options.redirect, "error");
  assert.equal(observed.options.cache, "no-store");
  assert.equal(observed.options.referrerPolicy, "no-referrer");
});

test("identity, expiry, MIME and URL errors stop before requesting bytes", async (t) => {
  const request = t.mock.method(globalThis, "fetch", async () => {
    throw new Error("unexpected request");
  });
  for (const updates of [
    { expires_at: "invalid" },
    { expires_at: new Date(0).toISOString() },
    { document_id: "another" },
    { document_version: "v2" },
    { artifact_sha256: "b".repeat(64) },
    { passage_id: "another" },
    { page_number: 1 },
    { content_type: "text/html" },
    { download_url: "javascript:alert(1)" },
    { download_url: "https://secret@source.invalid/source.pdf" },
  ]) {
    await assert.rejects(
      fetchVerifiedPdf(passage, { ...source(), ...updates }, new AbortController().signal),
    );
  }
  assert.equal(request.mock.callCount(), 0);
});

test("content type, claimed size, missing bytes and digest mismatch fail closed", async (t) => {
  for (const [body, headers, code] of [
    [bytes, { "content-type": "text/html" }, "type"],
    [
      bytes,
      { "content-type": "application/pdf", "content-length": String(PDF_MAX_BYTES + 1) },
      "size",
    ],
    [
      bytes,
      { "content-type": "application/pdf", "content-length": String(bytes.length + 1) },
      "size",
    ],
    [new TextEncoder().encode("%PDF-tampered"), { "content-type": "application/pdf" }, "checksum"],
    [new TextEncoder().encode("<html>no pdf"), { "content-type": "application/pdf" }, "type"],
  ]) {
    t.mock.method(globalThis, "fetch", async () => new Response(body, { headers }));
    await assert.rejects(
      fetchVerifiedPdf(passage, source(), new AbortController().signal),
      (error) => error.code === code,
    );
  }
});

test("actual streamed bytes are bounded even without Content-Length", async (t) => {
  let cancelled = false;
  t.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(
        new ReadableStream({
          pull(controller) {
            controller.enqueue(new Uint8Array(PDF_MAX_BYTES + 1));
          },
          cancel() {
            cancelled = true;
          },
        }),
        { headers: { "content-type": "application/pdf" } },
      ),
  );
  await assert.rejects(
    fetchVerifiedPdf(passage, source(), new AbortController().signal),
    (error) => error.code === "size",
  );
  assert.ok(cancelled);
});

test("cancelled retrieval is never presented as verified bytes", async (t) => {
  const controller = new AbortController();
  const request = t.mock.method(globalThis, "fetch", async () => new Response(bytes));
  controller.abort();
  await assert.rejects(fetchVerifiedPdf(passage, source(), controller.signal), {
    name: "AbortError",
  });
  assert.equal(request.mock.callCount(), 0);
});

test("an HTTP error cancels its unread response stream", async (t) => {
  let cancelled = false;
  t.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(
        new ReadableStream({
          cancel() {
            cancelled = true;
          },
        }),
        { status: 503 },
      ),
  );
  await assert.rejects(
    fetchVerifiedPdf(passage, source(), new AbortController().signal),
    (error) => error.code === "download",
  );
  assert.equal(cancelled, true);
});

test("ordinary page geometry uses the PDF viewport and stays aligned after scaling", () => {
  assert.deepEqual(pdfHighlights(passage, geometry, viewport), [
    { left: 10, top: 20, width: 30, height: 10 },
  ]);
  assert.deepEqual(
    pdfHighlights(passage, geometry, {
      width: 300,
      height: 400,
      convertToViewportPoint: (x, y) => [x, 400 - y],
    }),
    [{ left: 10, top: 20, width: 30, height: 10 }],
  );
});

test("unproven rotation, crop origin, dimensions and wrong pages are page-only", () => {
  for (const change of [
    { rotate: 90 },
    { userUnit: 2 },
    { view: [20, 40, 280, 380] },
    { view: [0, 0, 600, 800] },
    { pageNumber: 1 },
  ])
    assert.deepEqual(pdfHighlights(passage, { ...geometry, ...change }, viewport), []);
  assert.deepEqual(pdfHighlights({ ...passage, native_locator: {} }, geometry, viewport), []);
  assert.deepEqual(
    pdfHighlights(passage, geometry, { ...viewport, convertToViewportPoint: () => [NaN, 0] }),
    [],
  );
});

test("page zoom and DPR cannot exceed canvas pixel or side limits", () => {
  for (const [width, height, available, zoom, dpr] of [
    [300, 400, 600, 1, 1],
    [300, 400, 126, 4, 1],
    [300, 400, 126, 8, 2],
    [14400, 14400, 1200, 8, 4],
    [1, 14400, 1200, 8, 2],
    [14400, 1, 1200, 8, 2],
  ]) {
    const result = pdfRenderScale(width, height, available, zoom, dpr);
    const w = width * result.scale * result.outputScale,
      h = height * result.scale * result.outputScale;
    assert.ok(w <= PDF_MAX_SIDE + 0.01 && h <= PDF_MAX_SIDE + 0.01);
    assert.ok(w * h <= PDF_MAX_PIXELS + 0.01);
  }
  for (const invalid of [NaN, Infinity, -1, 0, 20000])
    assert.throws(() => pdfRenderScale(invalid, 400, 600, 1, 1));
  assert.throws(() => pdfRenderScale(300, 400, 600, 9, 1));
});
