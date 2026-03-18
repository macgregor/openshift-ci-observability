import json

from scraper.build_resources import (
    _extract_events,
    _extract_pods,
    _extract_deployments,
    _parse_iso_ts,
)

LABELS = {"build_id": "123", "job_name": "test-job", "org": "test",
          "repo": "test", "branch": "main", "pr_number": "1",
          "pr_sha": "abc", "author": "dev"}


def test_parse_iso_ts():
    assert _parse_iso_ts("2026-03-13T16:19:26Z") is not None
    assert _parse_iso_ts("2026-03-13T16:19:26+00:00") is not None
    assert _parse_iso_ts("") is None
    assert _parse_iso_ts(None) is None
    assert _parse_iso_ts("invalid") is None


SAMPLE_EVENTS = {
    "items": [
        {
            "involvedObject": {
                "kind": "Kserve",
                "name": "default-kserve",
                "namespace": "default",
            },
            "reason": "ProvisioningError",
            "type": "Warning",
            "message": "error retrieving value for key 'service' from configmap",
            "count": 4,
            "lastTimestamp": "2026-03-13T16:19:26Z",
            "source": {"component": "kserve-controller"},
        },
        {
            "involvedObject": {
                "kind": "Pod",
                "name": "test-pod",
                "namespace": "opendatahub",
            },
            "reason": "Pulled",
            "type": "Normal",
            "message": "Successfully pulled image",
            "count": 1,
            "firstTimestamp": "2026-03-13T16:00:00Z",
            "source": {"component": "kubelet"},
        },
    ]
}


def test_extract_events():
    records = _extract_events(SAMPLE_EVENTS, "cluster", LABELS)
    assert len(records) == 2
    r0 = json.loads(records[0])
    assert r0["source"] == "k8s_event"
    assert r0["reason"] == "ProvisioningError"
    assert r0["type"] == "Warning"
    assert r0["object_kind"] == "Kserve"
    assert r0["object_name"] == "default-kserve"
    assert r0["scope"] == "cluster"
    assert r0["event_count"] == 4
    assert "configmap" in r0["_msg"]
    assert r0["build_id"] == "123"


def test_extract_events_empty():
    assert _extract_events({"items": []}, "build", LABELS) == []
    assert _extract_events({}, "build", LABELS) == []


SAMPLE_PODS = {
    "items": [
        {
            "metadata": {
                "name": "kserve-controller-manager-abc",
                "namespace": "opendatahub",
                "creationTimestamp": "2026-03-13T16:00:00Z",
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"name": "manager", "ready": True, "restartCount": 0,
                     "state": {"running": {}}},
                ],
            },
        },
        {
            "metadata": {
                "name": "crashy-pod",
                "namespace": "opendatahub",
                "creationTimestamp": "2026-03-13T16:01:00Z",
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"name": "main", "ready": False, "restartCount": 5,
                     "state": {"waiting": {"reason": "CrashLoopBackOff"}}},
                ],
            },
        },
    ]
}


def test_extract_pods():
    records = _extract_pods(SAMPLE_PODS, "cluster", LABELS)
    assert len(records) == 2

    r0 = json.loads(records[0])
    assert r0["source"] == "k8s_pod"
    assert r0["pod_name"] == "kserve-controller-manager-abc"
    assert r0["phase"] == "Running"
    assert r0["restart_count"] == 0
    assert r0["_msg"] == "phase=Running"

    r1 = json.loads(records[1])
    assert r1["pod_name"] == "crashy-pod"
    assert r1["restart_count"] == 5
    assert "CrashLoopBackOff" in r1["_msg"]


def test_extract_pods_empty():
    assert _extract_pods({"items": []}, "build", LABELS) == []


SAMPLE_DEPLOYMENTS = {
    "items": [
        {
            "metadata": {
                "name": "kserve-controller-manager",
                "namespace": "opendatahub",
                "creationTimestamp": "2026-03-13T15:00:00Z",
            },
            "spec": {"replicas": 1},
            "status": {
                "readyReplicas": 0,
                "availableReplicas": 0,
                "unavailableReplicas": 1,
                "conditions": [
                    {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"},
                    {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"},
                ],
            },
        },
        {
            "metadata": {
                "name": "rhods-dashboard",
                "namespace": "opendatahub",
                "creationTimestamp": "2026-03-13T15:00:00Z",
            },
            "spec": {"replicas": 2},
            "status": {
                "readyReplicas": 2,
                "availableReplicas": 2,
                "conditions": [
                    {"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"},
                ],
            },
        },
    ]
}


def test_extract_deployments():
    records = _extract_deployments(SAMPLE_DEPLOYMENTS, "cluster", LABELS)
    assert len(records) == 2

    r0 = json.loads(records[0])
    assert r0["source"] == "k8s_deployment"
    assert r0["deployment_name"] == "kserve-controller-manager"
    assert r0["replicas"] == 1
    assert r0["ready_replicas"] == 0
    assert r0["unavailable_replicas"] == 1
    assert "0/1 ready" in r0["_msg"]
    assert "MinimumReplicasUnavailable" in r0["_msg"]

    r1 = json.loads(records[1])
    assert r1["deployment_name"] == "rhods-dashboard"
    assert r1["ready_replicas"] == 2
    assert "2/2 ready" in r1["_msg"]


def test_extract_deployments_empty():
    assert _extract_deployments({"items": []}, "cluster", LABELS) == []


def test_all_records_have_pipeline_field():
    """Every record must have pipeline=build_resources for dedup."""
    for extractor, data in [
        (_extract_events, SAMPLE_EVENTS),
        (_extract_pods, SAMPLE_PODS),
        (_extract_deployments, SAMPLE_DEPLOYMENTS),
    ]:
        records = extractor(data, "cluster", LABELS)
        for r in records:
            parsed = json.loads(r)
            assert parsed["pipeline"] == "build_resources"
