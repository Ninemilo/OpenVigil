"""Bind OCR, its model manifest and the unchanged two-image release identity."""

import hashlib
import re

from windops_backend.operations.hybrid_identity import hybrid_bundle_id

DOCUMENT_WORKLOAD = "windops-document-worker"
DOCUMENT_CONFIGURATION_RESOURCE = "windops-document-runtime"
DOCUMENT_MODELS_CLAIM = "windops-document-models"
DOCUMENT_RUNTIME_CONFIG = {
    "WINDOPS_DOCUMENT_PARSER_ENABLED": "true",
    "WINDOPS_DOCUMENT_PARSER_PYTHON": "/opt/document-parser/bin/python",
    "WINDOPS_DOCUMENT_PARSER_MODELS_PATH": "/models",
    "WINDOPS_DOCUMENT_PARSER_RUNTIME_DIRECTORY": "/tmp",  # nosec B108
}
DOCUMENT_ANNOTATIONS = {
    "document_image_digest": "windops.openai.com/document-image-digest",
    "model_manifest_sha256": "windops.openai.com/document-model-manifest-sha256",
    "document_bundle_id": "windops.openai.com/document-release-bundle-id",
}
DOCUMENT_ENV = {
    "document_image_digest": "WINDOPS_DOCUMENT_IMAGE_DIGEST",
    "model_manifest_sha256": "WINDOPS_DOCUMENT_PARSER_MODEL_MANIFEST_SHA256",
    "document_bundle_id": "WINDOPS_DOCUMENT_RELEASE_BUNDLE_ID",
}


def document_models_claim(manifest_sha256: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None or set(manifest_sha256) == {"0"}:
        raise ValueError("document models require a non-placeholder manifest SHA-256")
    return f"{DOCUMENT_MODELS_CLAIM}-{manifest_sha256}"


def document_bundle_id(
    release_id: str,
    commit_sha: str,
    api_digest: str,
    structural_digest: str,
    document_digest: str,
    manifest_sha256: str,
) -> str:
    hybrid = hybrid_bundle_id(release_id, commit_sha, api_digest, structural_digest)
    document_models_claim(manifest_sha256)
    if (
        re.fullmatch(r"sha256:[0-9a-f]{64}", document_digest) is None
        or document_digest == "sha256:" + "0" * 64
        or document_digest in {api_digest, structural_digest}
    ):
        raise ValueError("document release requires a separate immutable OCR image digest")
    payload = (
        f"openvigil.document-release.v1\n{hybrid}\n"
        f"document={document_digest}\nmodels={manifest_sha256}\n"
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
