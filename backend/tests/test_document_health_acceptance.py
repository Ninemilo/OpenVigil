"""Owned fault controller behavior; no real process or container is affected."""

import json

import pytest
from scripts.document_e2e_evidence import verify_worker_health_evidence
from scripts.structural_scope_evidence import OwnedInfrastructureChallenges


def test_document_controller_applies_stop_and_start_once_to_its_owned_worker(tmp_path):
    calls = []
    controller = OwnedInfrastructureChallenges(
        tmp_path,
        [],
        "unit-owned-project",
        lambda: calls.append("stop"),
        lambda: calls.append("start"),
        worker_service="document-worker",
    )
    for identity, action in (("1", "stop"), ("2", "start")):
        (tmp_path / f"fault-request-{identity}.json").write_text(
            json.dumps(
                {
                    "id": identity,
                    "service": "document-worker",
                    "action": action,
                }
            )
        )
        controller.handle_pending()
        controller.handle_pending()
        assert json.loads((tmp_path / f"fault-response-{identity}.json").read_text()) == {
            "id": identity,
            "service": "document-worker",
            "action": action,
            "status": "applied",
        }
    assert calls == ["stop", "start"]


@pytest.mark.parametrize("service", ["structural-worker", "unknown-worker"])
def test_document_controller_cannot_target_another_worker(tmp_path, service):
    def forbidden():
        pytest.fail("another worker must not be touched")

    controller = OwnedInfrastructureChallenges(
        tmp_path, [], "unit-owned-project", forbidden, forbidden, worker_service="document-worker"
    )
    (tmp_path / "fault-request-1.json").write_text(
        json.dumps({"id": "1", "service": service, "action": "stop"})
    )
    with pytest.raises(ValueError, match="unsupported infrastructure"):
        controller.handle_pending()
    assert not (tmp_path / "fault-response-1.json").exists()


def test_existing_structural_controller_default_is_preserved(tmp_path):
    calls = []
    controller = OwnedInfrastructureChallenges(
        tmp_path,
        [],
        "unit-owned-project",
        lambda: calls.append("stop"),
        lambda: calls.append("start"),
    )
    (tmp_path / "fault-request-1.json").write_text(
        json.dumps({"id": "1", "service": "structural-worker", "action": "stop"})
    )
    controller.handle_pending()
    assert calls == ["stop"]


@pytest.fixture
def verified_health():
    return {
        "initial_ready": True,
        "forced_stop_observed_offline": True,
        "queued_while_offline": 1,
        "restored_ready": True,
        "failed_document_visible": True,
        "restricted_failure_count": 0,
    }


def test_health_acceptance_preserves_verified_observations(verified_health):
    assert verify_worker_health_evidence({"worker_health": verified_health}) == verified_health


@pytest.mark.parametrize("value", [None, [], "ready", True])
def test_health_acceptance_rejects_absent_or_invalid_evidence(value):
    with pytest.raises(RuntimeError, match="worker_health_missing"):
        verify_worker_health_evidence({"worker_health": value})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("initial_ready", False),
        ("initial_ready", 1),
        ("forced_stop_observed_offline", None),
        ("restored_ready", False),
        ("failed_document_visible", "true"),
        ("queued_while_offline", 0),
        ("queued_while_offline", True),
        ("queued_while_offline", "1"),
        ("restricted_failure_count", 1),
        ("restricted_failure_count", False),
        ("restricted_failure_count", None),
    ],
)
def test_health_acceptance_rejects_partial_or_failed_observations(verified_health, key, value):
    verified_health[key] = value
    with pytest.raises(RuntimeError, match="worker_health_"):
        verify_worker_health_evidence({"worker_health": verified_health})
