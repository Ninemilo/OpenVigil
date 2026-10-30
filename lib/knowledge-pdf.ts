import {
  knowledgePageRegions,
  type NativeKnowledgePassage,
  type SourceDownload,
} from "./knowledge-source.ts";

export const PDF_MAX_BYTES = 50 * 1024 * 1024;
export const PDF_MAX_PAGES = 200;
export const PDF_MAX_PIXELS = 4 * 1024 * 1024;
export const PDF_MAX_SIDE = 4096;
export const PDF_MAX_ZOOM = 8;
export const PDF_TIMEOUT_MS = 30_000;

export class KnowledgePdfError extends Error {
  readonly code: "expired" | "identity" | "type" | "size" | "download" | "checksum";
  constructor(code: "expired" | "identity" | "type" | "size" | "download" | "checksum") {
    super(
      {
        expired: "原件授权已过期，请重新获取。",
        identity: "原件与当前引用的版本或身份不一致，请重新获取。",
        type: "原件不是可预览的 PDF，请核对文件类型。",
        size: "原件超过 50 MiB 预览上限，或下载长度不完整。",
        download: "原件下载失败，请检查网络和对象存储访问配置后重试。",
        checksum: "原件校验未通过，下载字节与已授权版本不一致。请重新获取原件。",
      }[code],
    );
    this.name = "KnowledgePdfError";
    this.code = code;
  }
}

export function validatePdfGrant(
  passage: NativeKnowledgePassage,
  source: SourceDownload,
  now = Date.now(),
): void {
  if (!Number.isFinite(Date.parse(source.expires_at)) || Date.parse(source.expires_at) <= now)
    throw new KnowledgePdfError("expired");
  if (source.content_type !== "application/pdf") throw new KnowledgePdfError("type");
  if (
    passage.page_number !== null &&
    (!Number.isInteger(passage.page_number) ||
      passage.page_number < 1 ||
      passage.page_number > PDF_MAX_PAGES)
  )
    throw new KnowledgePdfError("identity");
  if (
    !/^[a-f0-9]{64}$/.test(source.artifact_sha256) ||
    source.artifact_sha256 !== passage.source_sha256 ||
    source.document_id !== passage.document_id ||
    source.document_version !== passage.document_version ||
    source.passage_id !== passage.passage_id ||
    source.page_number !== passage.page_number
  )
    throw new KnowledgePdfError("identity");
  let url: URL;
  try {
    url = new URL(source.download_url);
  } catch {
    throw new KnowledgePdfError("download");
  }
  if (!["https:", "http:"].includes(url.protocol) || url.username || url.password)
    throw new KnowledgePdfError("download");
}

/** Fetch only the short-lived source grant; never delegate URLs/range requests to PDF.js. */
export async function fetchVerifiedPdf(
  passage: NativeKnowledgePassage,
  source: SourceDownload,
  signal: AbortSignal,
): Promise<Uint8Array<ArrayBuffer>> {
  validatePdfGrant(passage, source);
  signal.throwIfAborted();
  const response = await fetch(source.download_url, {
    signal,
    credentials: "omit",
    cache: "no-store",
    redirect: "error",
    referrerPolicy: "no-referrer",
  });
  if (!response.ok || !response.body) {
    await response.body?.cancel();
    throw new KnowledgePdfError("download");
  }
  const type = response.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
  if (type !== "application/pdf") {
    await response.body.cancel();
    throw new KnowledgePdfError("type");
  }
  const lengthHeader = response.headers.get("content-length");
  const expectedLength = lengthHeader === null ? null : Number(lengthHeader);
  if (
    expectedLength !== null &&
    (!Number.isSafeInteger(expectedLength) || expectedLength < 5 || expectedLength > PDF_MAX_BYTES)
  ) {
    await response.body.cancel();
    throw new KnowledgePdfError("size");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > PDF_MAX_BYTES) throw new KnowledgePdfError("size");
      chunks.push(value);
    }
    if (length < 5 || (expectedLength !== null && length !== expectedLength))
      throw new KnowledgePdfError("size");
  } catch (error) {
    await reader.cancel().catch(() => undefined); // Fetch abort may have already closed the stream.
    throw error;
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  if (String.fromCharCode(...bytes.subarray(0, 5)) !== "%PDF-") throw new KnowledgePdfError("type");
  signal.throwIfAborted();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
  if (sha !== source.artifact_sha256) throw new KnowledgePdfError("checksum");
  validatePdfGrant(passage, source);
  signal.throwIfAborted();
  return bytes;
}

export interface PdfPageGeometry {
  readonly pageNumber: number;
  readonly rotate: number;
  readonly userUnit: number;
  readonly view: readonly number[];
}
export interface PdfViewportGeometry {
  readonly width: number;
  readonly height: number;
  convertToViewportPoint(x: number, y: number): number[];
}
export interface PdfHighlight {
  readonly left: number;
  readonly top: number;
  readonly width: number;
  readonly height: number;
}

/** Current locator has no rotation/crop-origin contract: those pages deliberately stay page-only. */
export function pdfHighlights(
  passage: NativeKnowledgePassage,
  page: PdfPageGeometry,
  viewport: PdfViewportGeometry,
): readonly PdfHighlight[] {
  const regions = knowledgePageRegions(passage);
  if (
    page.rotate !== 0 ||
    page.userUnit !== 1 ||
    page.view.length !== 4 ||
    page.view[0] !== 0 ||
    page.view[1] !== 0 ||
    page.pageNumber !== passage.page_number ||
    !Number.isFinite(viewport.width) ||
    !Number.isFinite(viewport.height) ||
    viewport.width <= 0 ||
    viewport.height <= 0
  )
    return [];
  if (
    !regions.length ||
    regions.some(
      (region) =>
        Math.abs(region.width - page.view[2]) > 0.01 ||
        Math.abs(region.height - page.view[3]) > 0.01,
    )
  )
    return [];
  const result: PdfHighlight[] = [];
  for (const region of regions) {
    const [left, top, right, bottom] = region.bbox;
    const rect = [
      ...viewport.convertToViewportPoint(left, region.height - bottom),
      ...viewport.convertToViewportPoint(right, region.height - top),
    ];
    const x = Math.min(rect[0], rect[2]),
      y = Math.min(rect[1], rect[3]);
    const width = Math.abs(rect[2] - rect[0]),
      height = Math.abs(rect[3] - rect[1]);
    if (
      ![x, y, width, height].every(Number.isFinite) ||
      x < 0 ||
      y < 0 ||
      width <= 0 ||
      height <= 0 ||
      x + width > viewport.width + 0.01 ||
      y + height > viewport.height + 0.01
    )
      return [];
    result.push({
      left: (x / viewport.width) * 100,
      top: (y / viewport.height) * 100,
      width: (width / viewport.width) * 100,
      height: (height / viewport.height) * 100,
    });
  }
  return result;
}

export function pdfRenderScale(
  width: number,
  height: number,
  containerWidth: number,
  zoom: number,
  devicePixelRatio: number,
): { scale: number; outputScale: number; limited: boolean } {
  if (
    ![width, height, containerWidth, zoom, devicePixelRatio].every(
      (value) => Number.isFinite(value) && value > 0,
    ) ||
    width > 14400 ||
    height > 14400 ||
    zoom > PDF_MAX_ZOOM
  )
    throw new Error("PDF 页面尺寸超出预览限制。");
  const outputScale = Math.min(devicePixelRatio, 2);
  const requested = (Math.min(containerWidth, 1200) / width) * zoom;
  const scale = Math.min(
    requested,
    PDF_MAX_SIDE / Math.max(width, height) / outputScale,
    Math.sqrt(PDF_MAX_PIXELS / (width * height)) / outputScale,
  );
  return { scale, outputScale, limited: scale < requested };
}
