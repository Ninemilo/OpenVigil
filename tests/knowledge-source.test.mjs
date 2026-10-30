import assert from "node:assert/strict";
import test from "node:test";

import {
  knowledgeIngestionLabel,
  knowledgePageRegions,
  knowledgeSourceLocation,
  verifiedSourceHref,
} from "../lib/knowledge-source.ts";
import { verifyPassageSource } from "../lib/knowledge-source-grant.ts";

test("source labels distinguish verified PDF pages from native text and historical unknowns", () => {
  assert.equal(knowledgeSourceLocation({ page_number: 7, native_locator: {} }), "第 7 页");
  assert.equal(
    knowledgeSourceLocation({
      page_number: null,
      native_locator: { kind: "text_lines", line_start: 4, line_end: 8 },
    }),
    "第 4–8 行",
  );
  assert.equal(
    knowledgeSourceLocation({
      page_number: null,
      native_locator: { kind: "docx_table_cell", row_index: 1, column_index: 0 },
    }),
    "表格 · 第 2 行 / 第 1 列",
  );
  assert.equal(
    knowledgeSourceLocation({ page_number: null, native_locator: { kind: "database_text" } }),
    "正文位置未标注",
  );
});

test("comparison grants bind document, passage, immutable version, hash and page before rendering", () => {
  const now = Date.parse("2026-10-23T02:00:00Z");
  const passage = {
    document_id: "doc-a",
    passage_id: "passage-a",
    document_version: "v1",
    source_sha256: "a".repeat(64),
    page_number: 2,
  };
  const source = {
    document_id: passage.document_id,
    passage_id: passage.passage_id,
    document_version: passage.document_version,
    artifact_sha256: passage.source_sha256,
    page_number: 2,
    download_url: "https://objects.example/document.pdf?signature=synthetic",
    expires_at: new Date(now + 300_000).toISOString(),
    content_type: "application/pdf",
    native_locator: null,
  };
  assert.doesNotThrow(() => verifyPassageSource(source, passage, now));
  for (const change of [
    { document_id: "doc-b" },
    { passage_id: "passage-b" },
    { passage_id: null },
    { document_version: "v2" },
    { artifact_sha256: "b".repeat(64) },
    { page_number: 1 },
    { download_url: "javascript:alert(1)" },
    { expires_at: "not-a-date" },
    { expires_at: new Date(now).toISOString() },
  ]) {
    assert.throws(() => verifyPassageSource({ ...source, ...change }, passage, now));
  }
  assert.throws(() => verifyPassageSource(source, { ...passage, source_sha256: null }, now));
});

test("PDF navigation preserves a signed source URL and rejects executable protocols", () => {
  const original =
    "https://objects.example/source.pdf?X-Amz-Signature=synthetic-signature&response-content-type=application%2Fpdf";
  const result = new URL(verifiedSourceHref(original, 7));
  assert.equal(result.hash, "#page=7");
  assert.equal(result.search, new URL(original).search);
  assert.equal(new URL(verifiedSourceHref(original, null)).hash, "");
  assert.throws(() => verifiedSourceHref("javascript:alert(1)", null));
  assert.throws(() => verifiedSourceHref("data:text/html,test", null));
});

test("background parsing states do not appear indexed or healthy", () => {
  assert.equal(knowledgeIngestionLabel("pending_parse"), "等待 PDF 解析");
  assert.equal(knowledgeIngestionLabel("parsing"), "正在解析 PDF");
  assert.equal(knowledgeIngestionLabel("parse_failed"), "PDF 解析失败");
  assert.equal(knowledgeIngestionLabel("failed"), "索引失败");
});

test("page-region display uses actual source geometry and refuses cross-page or invalid boxes", () => {
  const passage = {
    page_number: 2,
    native_locator: {
      kind: "docling_pdf_item",
      regions: [
        {
          page_number: 2,
          page_width: 650,
          page_height: 850,
          coordinate_space: "pdf_points_top_left",
          bbox: [20, 40, 200, 80],
        },
      ],
    },
  };
  assert.deepEqual(knowledgePageRegions(passage), [
    {
      pageNumber: 2,
      width: 650,
      height: 850,
      bbox: [20, 40, 200, 80],
    },
  ]);
  const otherPage = structuredClone(passage);
  otherPage.native_locator.regions[0].page_number = 1;
  assert.deepEqual(knowledgePageRegions(otherPage), []);
  const outside = structuredClone(passage);
  outside.native_locator.regions[0].bbox = [20, 40, 700, 80];
  assert.deepEqual(knowledgePageRegions(outside), []);
  const unknownUnits = structuredClone(passage);
  unknownUnits.native_locator.regions[0].coordinate_space = "pixels";
  assert.deepEqual(knowledgePageRegions(unknownUnits), []);
  assert.deepEqual(knowledgePageRegions({ page_number: null, native_locator: {} }), []);
});
