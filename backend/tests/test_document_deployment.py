from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from scripts.render_release_manifests import render_release_manifests

from test_deployment_policy import (
    COMMIT_SHA,
    IMAGE,
    IMAGE_DIGEST,
    RELEASE_ID,
    STORAGE_CLASS,
    _resource,
)
from test_hybrid_deployment import (
    STRUCTURAL_DIGEST,
    STRUCTURAL_IMAGE,
)
from test_hybrid_deployment import (
    _documents as hybrid_documents,
)
from windops_backend.config import Settings
from windops_backend.operations.deployment_policy import verify_deployment_policy
from windops_backend.operations.document_identity import (
    DOCUMENT_ANNOTATIONS,
    DOCUMENT_WORKLOAD,
    document_bundle_id,
    document_models_claim,
)
from windops_backend.operations.hybrid_identity import hybrid_bundle_id

DOCUMENT_DIGEST = "sha256:" + "d" * 64
DOCUMENT_IMAGE = "registry.example/windops-document@" + DOCUMENT_DIGEST
MODEL_SHA = "e" * 64
OVERLAY = Path(__file__).parents[1] / "deploy/document-parser"


def _documents(tmp_path: Path):
    documents = hybrid_documents(tmp_path)
    for name in ("document-worker.yaml", "network-policy.yaml"):
        for resource in yaml.safe_load_all((OVERLAY / name).read_text()):
            resource["metadata"]["namespace"] = "windops"
            documents.append(resource)
    overlay = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
    (operation,) = yaml.safe_load(overlay["patches"][0]["patch"])
    assert operation["op"] == "replace"
    assert operation["path"] == "/spec/podSelector/matchExpressions/0/values"
    _resource(documents, "NetworkPolicy", "windops-runtime-egress")["spec"]["podSelector"][
        "matchExpressions"
    ][0]["values"] = operation["value"]
    return documents


def _render(tmp_path: Path, **overrides):
    arguments = dict(
        release_id=RELEASE_ID,
        commit_sha=COMMIT_SHA,
        image=IMAGE,
        structural_image=STRUCTURAL_IMAGE,
        document_image=DOCUMENT_IMAGE,
        model_manifest_sha256=MODEL_SHA,
    )
    arguments.update(overrides)
    return render_release_manifests(_documents(tmp_path), **arguments)


def _verify(tmp_path: Path, documents, **overrides):
    path = tmp_path / "document.yaml"
    path.write_text(yaml.safe_dump_all(documents, sort_keys=False))
    arguments = dict(
        expected_release_id=RELEASE_ID,
        expected_commit_sha=COMMIT_SHA,
        expected_image_digest=IMAGE_DIGEST,
        expected_structural_image_digest=STRUCTURAL_DIGEST,
        expected_document_image_digest=DOCUMENT_DIGEST,
        expected_model_manifest_sha256=MODEL_SHA,
        approved_backup_storage_class=STORAGE_CLASS,
    )
    arguments.update(overrides)
    return verify_deployment_policy(path, **arguments)


def test_document_release_binds_three_images_and_models_without_changing_hybrid_identity(tmp_path):
    documents = _render(tmp_path)
    report = _verify(tmp_path, documents)
    assert report["bundle_id"] == hybrid_bundle_id(
        RELEASE_ID, COMMIT_SHA, IMAGE_DIGEST, STRUCTURAL_DIGEST
    )
    assert report["document_bundle_id"] == document_bundle_id(
        RELEASE_ID, COMMIT_SHA, IMAGE_DIGEST, STRUCTURAL_DIGEST, DOCUMENT_DIGEST, MODEL_SHA
    )
    assert report["expected_model_manifest_sha256"] == MODEL_SHA
    assert len(report["workloads"]) == 10
    images = {"windops-structural-worker": STRUCTURAL_IMAGE, DOCUMENT_WORKLOAD: DOCUMENT_IMAGE}
    for workload in report["workloads"]:
        assert workload["images"] == [images.get(workload["name"], IMAGE)]
    worker = _resource(documents, "Deployment", DOCUMENT_WORKLOAD)
    claim = _resource(documents, "PersistentVolumeClaim", document_models_claim(MODEL_SHA))
    assert worker["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"] == {
        "claimName": claim["metadata"]["name"],
        "readOnly": True,
    }


def test_every_rendered_workload_has_a_valid_enabled_parser_configuration(tmp_path):
    for document in _render(tmp_path):
        if document["kind"] not in {"Deployment", "CronJob", "Job"}:
            continue
        spec = document["spec"]
        if document["kind"] == "CronJob":
            spec = spec["jobTemplate"]["spec"]
        for container in spec["template"]["spec"]["containers"]:
            settings = Settings(
                **{
                    "_env_file": None,
                    **{
                        item["name"].removeprefix("WINDOPS_").lower(): item["value"]
                        for item in container["env"]
                        if item["name"].startswith("WINDOPS_DOCUMENT_PARSER_")
                    },
                }
            )
            assert settings.document_parser_enabled
            assert settings.document_parser_model_manifest_sha256 == MODEL_SHA
            assert settings.document_parser_python == "/opt/document-parser/bin/python"
            assert settings.document_parser_models_path == Path("/models")


@pytest.mark.parametrize("field", range(6))
def test_every_release_input_changes_document_bundle_identity(field):
    values = [RELEASE_ID, COMMIT_SHA, IMAGE_DIGEST, STRUCTURAL_DIGEST, DOCUMENT_DIGEST, MODEL_SHA]
    original = document_bundle_id(*values)
    values[field] = [
        "other-release",
        "f" * 40,
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        "4" * 64,
    ][field]
    assert document_bundle_id(*values) != original


@pytest.mark.parametrize(
    "overrides",
    [
        {"document_image": IMAGE},
        {"document_image": STRUCTURAL_IMAGE},
        {"model_manifest_sha256": "0" * 64},
        {"model_manifest_sha256": "../../models"},
        {"model_manifest_sha256": None},
        {"structural_image": None},
        {"document_image": None},
    ],
)
def test_renderer_rejects_missing_or_unbound_runtime_inputs(tmp_path, overrides):
    with pytest.raises(ValueError):
        _render(tmp_path, **overrides)


@pytest.mark.parametrize(
    "violation",
    [
        "image",
        "parent",
        "models",
        "bundle",
        "annotation",
        "queue",
        "threads",
        "python",
        "enabled",
        "secret",
        "extra_config",
        "account",
        "uid",
        "container_uid",
        "capabilities",
        "sidecar",
        "init",
        "ephemeral",
        "replicas",
        "strategy",
        "budget",
        "grace",
        "scratch",
        "mount_write",
        "pvc_write",
        "pvc_other",
        "pvc_identity",
        "pvc_storage",
        "pvc_volume",
        "label",
        "broad_network",
        "extra_network",
        "ingress",
        "outbound",
        "missing_worker",
        "missing_policy",
        "missing_claim",
        "duplicate_env",
        "api_disabled",
        "api_models",
    ],
)
def test_document_deployment_rejects_identity_and_isolation_relaxation(tmp_path, violation):
    documents = _render(tmp_path)
    worker = _resource(documents, "Deployment", DOCUMENT_WORKLOAD)
    pod = worker["spec"]["template"]["spec"]
    container = pod["containers"][0]
    claim = _resource(documents, "PersistentVolumeClaim", document_models_claim(MODEL_SHA))
    policy = _resource(documents, "NetworkPolicy", "windops-document-egress")
    environment = {item["name"]: item for item in container["env"]}
    if violation == "image":
        container["image"] = IMAGE
    elif violation == "parent":
        environment["WINDOPS_API_IMAGE_DIGEST"]["value"] = DOCUMENT_DIGEST
    elif violation == "models":
        environment["WINDOPS_DOCUMENT_PARSER_MODEL_MANIFEST_SHA256"]["value"] = "f" * 64
    elif violation == "bundle":
        environment["WINDOPS_DOCUMENT_RELEASE_BUNDLE_ID"]["value"] = "sha256:" + "f" * 64
    elif violation == "annotation":
        worker["metadata"]["annotations"][DOCUMENT_ANNOTATIONS["model_manifest_sha256"]] = "f" * 64
    elif violation == "queue":
        container["args"][2] = "business"
    elif violation == "threads":
        container["args"][-1] = "4"
    elif violation == "python":
        environment["WINDOPS_DOCUMENT_PARSER_PYTHON"]["value"] = "/usr/local/bin/python"
    elif violation == "enabled":
        environment["WINDOPS_DOCUMENT_PARSER_ENABLED"]["value"] = "false"
    elif violation == "secret":
        container["envFrom"][0]["secretRef"]["name"] = "windops-runtime"
    elif violation == "extra_config":
        container["envFrom"].append({"configMapRef": {"name": "unqualified"}})
    elif violation == "account":
        pod["serviceAccountName"] = "windops-runtime"
    elif violation == "uid":
        pod["securityContext"]["runAsUser"] = 999
    elif violation == "container_uid":
        container["securityContext"]["runAsUser"] = 999
    elif violation == "capabilities":
        container["securityContext"]["capabilities"]["add"] = ["NET_ADMIN"]
    elif violation in {"sidecar", "init", "ephemeral"}:
        extra = {**copy.deepcopy(container), "name": "extra"}
        if violation == "sidecar":
            pod["containers"].append(extra)
        else:
            pod["initContainers" if violation == "init" else "ephemeralContainers"] = [extra]
    elif violation == "replicas":
        worker["spec"]["replicas"] = 2
    elif violation == "strategy":
        worker["spec"]["strategy"] = {"type": "RollingUpdate"}
    elif violation == "budget":
        container["resources"]["limits"]["cpu"] = "8"
    elif violation == "grace":
        pod["terminationGracePeriodSeconds"] = 10
    elif violation == "scratch":
        pod["volumes"][0]["emptyDir"] = {}
    elif violation == "mount_write":
        container["volumeMounts"][1]["readOnly"] = False
    elif violation == "pvc_write":
        pod["volumes"][1]["persistentVolumeClaim"]["readOnly"] = False
    elif violation == "pvc_other":
        pod["volumes"][1]["persistentVolumeClaim"]["claimName"] = "mutable-models"
    elif violation == "pvc_identity":
        claim["metadata"]["annotations"][DOCUMENT_ANNOTATIONS["model_manifest_sha256"]] = "f" * 64
    elif violation == "pvc_storage":
        claim["spec"]["storageClassName"] = "standard"
    elif violation == "pvc_volume":
        claim["spec"]["volumeName"] = "mutable-models"
    elif violation == "label":
        worker["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] = "worker"
    elif violation == "broad_network":
        _resource(documents, "NetworkPolicy", "windops-runtime-egress")["spec"]["podSelector"][
            "matchExpressions"
        ][0]["values"].remove("document-worker")
    elif violation == "extra_network":
        extra = copy.deepcopy(policy)
        extra["metadata"]["name"] = "extra-egress"
        extra["spec"]["podSelector"] = {}
        documents.append(extra)
    elif violation == "ingress":
        policy["spec"]["ingress"] = [{}]
    elif violation == "outbound":
        policy["spec"]["egress"][1]["ports"].append({"protocol": "TCP", "port": 443})
    elif violation in {"missing_worker", "missing_policy", "missing_claim"}:
        documents.remove(
            {"missing_worker": worker, "missing_policy": policy, "missing_claim": claim}[violation]
        )
    elif violation == "duplicate_env":
        container["env"].append(copy.deepcopy(container["env"][0]))
    else:
        api = _resource(documents, "Deployment", "windops-api")["spec"]["template"]["spec"][
            "containers"
        ][0]
        variable = (
            "WINDOPS_DOCUMENT_PARSER_ENABLED"
            if violation == "api_disabled"
            else "WINDOPS_DOCUMENT_PARSER_MODEL_MANIFEST_SHA256"
        )
        next(item for item in api["env"] if item["name"] == variable)["value"] = "false"
    with pytest.raises(ValueError):
        _verify(tmp_path, documents)


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_document_image_digest": None},
        {"expected_model_manifest_sha256": None},
        {"expected_structural_image_digest": None},
        {"expected_model_manifest_sha256": "f" * 64},
    ],
)
def test_policy_requires_explicit_matching_full_release_identity(tmp_path, overrides):
    with pytest.raises(ValueError):
        _verify(tmp_path, _render(tmp_path), **overrides)
