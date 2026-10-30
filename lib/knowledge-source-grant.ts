import {
  verifiedSourceHref,
  type NativeKnowledgePassage,
  type SourceDownload,
} from "./knowledge-source.ts";

/** Bind a short-lived grant to the exact excerpt selected by the user. */
export function verifyPassageSource(
  source: SourceDownload,
  passage: NativeKnowledgePassage,
  now = Date.now(),
): void {
  if (
    source.document_id !== passage.document_id ||
    source.passage_id !== passage.passage_id ||
    source.document_version !== passage.document_version ||
    !passage.source_sha256 ||
    !/^[a-f0-9]{64}$/.test(passage.source_sha256) ||
    source.artifact_sha256 !== passage.source_sha256 ||
    source.page_number !== passage.page_number
  ) {
    throw new Error("原件授权与当前段落或文件版本不一致，请重新读取来源。");
  }
  verifiedSourceHref(source.download_url, source.page_number);
  const expiresAt = Date.parse(source.expires_at);
  if (!Number.isFinite(expiresAt) || expiresAt <= now) {
    throw new Error("原文链接已过期，请重新获取。");
  }
}
