from scraper.cluster_pool import (
    parse_go_duration,
    extract_cluster_pool_metrics,
)


def test_parse_go_duration():
    assert parse_go_duration("4h0m0s") == 14400.0
    assert parse_go_duration("1h30m0s") == 5400.0
    assert parse_go_duration("0h5m30s") == 330.0
    assert parse_go_duration("30m0s") == 1800.0
    assert parse_go_duration("45s") == 45.0
    assert parse_go_duration("2h") == 7200.0
    assert parse_go_duration(None) is None
    assert parse_go_duration("") is None
    assert parse_go_duration("invalid") is None


SAMPLE_CLAIM = {
    "metadata": {
        "name": "test-claim",
        "creationTimestamp": "2026-03-13T12:19:46Z",
    },
    "spec": {
        "clusterPoolName": "opendatahub-ocp-4-19-amd64-aws",
        "lifetime": "4h0m0s",
    },
    "status": {
        "conditions": [
            {
                "type": "Pending",
                "status": "False",
                "lastTransitionTime": "2026-03-13T12:27:10Z",
                "reason": "ClusterClaimed",
            },
            {
                "type": "ClusterRunning",
                "status": "True",
                "lastTransitionTime": "2026-03-13T12:27:10Z",
                "reason": "Running",
            },
        ],
    },
}

SAMPLE_DEPLOYMENT = {
    "metadata": {
        "labels": {
            "hive.openshift.io/version": "4.19.25",
            "hive.openshift.io/cluster-platform": "aws",
        },
    },
    "spec": {
        "platform": {"aws": {"region": "us-east-1"}},
        "powerState": "Running",
        "clusterPoolRef": {
            "poolName": "opendatahub-ocp-4-19-amd64-aws",
            "claimedTimestamp": "2026-03-13T12:27:10Z",
        },
    },
    "status": {
        "installStartedTimestamp": "2026-03-13T11:20:05Z",
        "installedTimestamp": "2026-03-13T12:08:47Z",
    },
}

SAMPLE_LABELS = {"build_id": "123", "job_name": "test-job", "org": "test",
                 "repo": "test", "branch": "main", "pr_number": "1",
                 "pr_sha": "abc", "author": "dev"}


def test_extract_cluster_pool_metrics_full():
    metrics = extract_cluster_pool_metrics(SAMPLE_CLAIM, SAMPLE_DEPLOYMENT, SAMPLE_LABELS)
    names = [m.split("{")[0] for m in metrics]
    assert "ci_cluster_claim_wait_seconds" in names
    assert "ci_cluster_claim_lifetime_seconds" in names
    assert "ci_cluster_install_duration_seconds" in names
    assert "ci_cluster_idle_seconds" in names
    # All metrics should have cluster_pool label
    for m in metrics:
        assert 'cluster_pool="opendatahub-ocp-4-19-amd64-aws"' in m
        assert 'ocp_version="4.19.25"' in m


def test_claim_wait_seconds_value():
    metrics = extract_cluster_pool_metrics(SAMPLE_CLAIM, SAMPLE_DEPLOYMENT, SAMPLE_LABELS)
    wait_line = [m for m in metrics if m.startswith("ci_cluster_claim_wait_seconds")][0]
    # 12:27:10 - 12:19:46 = 444 seconds
    assert " 444.0 " in wait_line


def test_install_duration_value():
    metrics = extract_cluster_pool_metrics(SAMPLE_CLAIM, SAMPLE_DEPLOYMENT, SAMPLE_LABELS)
    install_line = [m for m in metrics if m.startswith("ci_cluster_install_duration_seconds")][0]
    # 12:08:47 - 11:20:05 = 2922 seconds
    assert " 2922.0 " in install_line


def test_idle_seconds_value():
    metrics = extract_cluster_pool_metrics(SAMPLE_CLAIM, SAMPLE_DEPLOYMENT, SAMPLE_LABELS)
    idle_line = [m for m in metrics if m.startswith("ci_cluster_idle_seconds")][0]
    # 12:27:10 - 12:08:47 = 1103 seconds
    assert " 1103.0 " in idle_line


def test_lifetime_value():
    metrics = extract_cluster_pool_metrics(SAMPLE_CLAIM, SAMPLE_DEPLOYMENT, SAMPLE_LABELS)
    lifetime_line = [m for m in metrics if m.startswith("ci_cluster_claim_lifetime_seconds")][0]
    # 4h0m0s = 14400 seconds
    assert " 14400.0 " in lifetime_line


def test_claim_only():
    metrics = extract_cluster_pool_metrics(SAMPLE_CLAIM, None, SAMPLE_LABELS)
    names = [m.split("{")[0] for m in metrics]
    assert "ci_cluster_claim_wait_seconds" in names
    assert "ci_cluster_claim_lifetime_seconds" in names
    assert "ci_cluster_install_duration_seconds" not in names


def test_deployment_only():
    metrics = extract_cluster_pool_metrics(None, SAMPLE_DEPLOYMENT, SAMPLE_LABELS)
    names = [m.split("{")[0] for m in metrics]
    assert "ci_cluster_install_duration_seconds" in names
    assert "ci_cluster_idle_seconds" in names
    assert "ci_cluster_claim_wait_seconds" not in names


def test_no_data():
    assert extract_cluster_pool_metrics(None, None, SAMPLE_LABELS) == []
