import json
from pathlib import Path

import pytest
import responses

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def metrics_json():
    with open(FIXTURES / "ci-operator-metrics.json") as f:
        return json.load(f)


@pytest.fixture
def ci_operator_log():
    with open(FIXTURES / "ci-operator.log") as f:
        return f.read()


@pytest.fixture
def sample_job_labels():
    return {
        "org": "opendatahub-io",
        "repo": "opendatahub-operator",
        "branch": "main",
        "job_name": "pull-ci-opendatahub-io-opendatahub-operator-main-opendatahub-operator-e2e",
        "pr_number": "3260",
        "pr_sha": "d26c6146301d",
        "author": "carlkyrillos",
        "build_id": "2031880686163464192",
    }



def mock_gcs_object(responses_mock, path, body, status=200):
    from scraper.gcs import GCS_BASE
    url = f"{GCS_BASE}/test-platform-results/{path}"
    responses_mock.add(responses.GET, url, body=body, status=status)
