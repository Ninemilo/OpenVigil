"""Additional isolation policy for the optional, model-bound OCR worker."""

from typing import Any

from windops_backend.operations.document_artifact import DOCUMENT_COMMAND, DOCUMENT_ENVIRONMENT
from windops_backend.operations.document_identity import (
    DOCUMENT_ANNOTATIONS,
    DOCUMENT_CONFIGURATION_RESOURCE,
    document_models_claim,
)


def validate_document_workload(
    workload: dict[str, Any],
    model_claim: dict[str, Any],
    selected_policies: list[dict[str, Any]],
    manifest_sha256: str,
) -> None:
    spec = workload["spec"]
    template = spec["template"]
    pod = template["spec"]
    if spec.get("replicas") != 1 or spec.get("strategy") != {"type": "Recreate"}:
        raise ValueError("document worker requires one replica and Recreate")
    if any(
        pod["securityContext"].get(key) != 10001 for key in ("runAsUser", "runAsGroup", "fsGroup")
    ):
        raise ValueError("document worker requires UID/GID 10001")
    if len(pod["containers"]) != 1 or pod.get("initContainers") or pod.get("ephemeralContainers"):
        raise ValueError("document worker cannot add sidecars or initialization processes")
    container = pod["containers"][0]
    if container.get("command", []) + container.get("args", []) != DOCUMENT_COMMAND:
        raise ValueError("document worker requires its bounded dedicated queue")
    if any(
        container["securityContext"].get(key, 10001) != 10001 for key in ("runAsUser", "runAsGroup")
    ) or container["securityContext"].get("capabilities", {}).get("add"):
        raise ValueError("document worker cannot override its execution identity or capabilities")
    if container.get("envFrom") != [{"secretRef": {"name": DOCUMENT_CONFIGURATION_RESOURCE}}]:
        raise ValueError("document worker requires only its isolated external secret")
    environment = {item["name"]: item.get("value") for item in container["env"]}
    expected = {
        **DOCUMENT_ENVIRONMENT,
        "WINDOPS_DOCUMENT_PARSER_ENABLED": "true",
        "WINDOPS_DOCUMENT_PARSER_MODEL_MANIFEST_SHA256": manifest_sha256,
    }
    if any(environment.get(name) != value for name, value in expected.items()):
        raise ValueError("document worker requires isolated parser and model configuration")
    if container["resources"] != {
        "requests": {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "128Mi"},
        "limits": {"cpu": "2", "memory": "3Gi", "ephemeral-storage": "256Mi"},
    }:
        raise ValueError("document worker requires the approved OCR resource budget")
    claim_name = document_models_claim(manifest_sha256)
    if (
        pod.get("terminationGracePeriodSeconds") != 210
        or pod.get("volumes")
        != [
            {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
            {
                "name": "models",
                "persistentVolumeClaim": {"claimName": claim_name, "readOnly": True},
            },
        ]
        or container.get("volumeMounts")
        != [
            {"name": "tmp", "mountPath": "/tmp"},  # nosec B108
            {"name": "models", "mountPath": "/models", "readOnly": True},
        ]
    ):
        raise ValueError("document worker requires bounded scratch and versioned read-only models")
    if (
        model_claim["metadata"]["name"] != claim_name
        or model_claim["metadata"]
        .get("annotations", {})
        .get(DOCUMENT_ANNOTATIONS["model_manifest_sha256"])
        != manifest_sha256
        or model_claim.get("spec")
        != {
            "accessModes": ["ReadOnlyMany"],
            "volumeMode": "Filesystem",
            "storageClassName": "",
            "volumeName": claim_name,
            "resources": {"requests": {"storage": "2Gi"}},
        }
    ):
        raise ValueError("document models must bind an explicitly provisioned immutable volume")
    if template["metadata"]["labels"].get("app.kubernetes.io/component") != "document-worker":
        raise ValueError("document worker requires its isolated component label")
    expected_egress = [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"windops.openai.com/document-dependency-access": "true"}
                    },
                    "podSelector": {
                        "matchLabels": {"windops.openai.com/document-dependency-access": "true"}
                    },
                }
            ],
            "ports": [{"protocol": "TCP", "port": port} for port in (5432, 6379, 9000)],
        },
    ]
    found = False
    for policy in selected_policies:
        network = policy["spec"]
        if network.get("ingress"):
            raise ValueError("document worker cannot receive network ingress")
        if policy["metadata"]["name"] != "windops-document-egress":
            if network.get("egress"):
                raise ValueError("additional policy grants document worker network egress")
            continue
        found = True
        if network != {
            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "document-worker"}},
            "policyTypes": ["Egress"],
            "egress": expected_egress,
        }:
            raise ValueError(
                "document worker may only reach scoped DNS, PostgreSQL, Redis and MinIO"
            )
    if not found:
        raise ValueError("document worker requires its scoped network policy")
