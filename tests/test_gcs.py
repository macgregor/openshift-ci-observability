import pytest
import requests
import responses

from scraper.gcs import GCSClient, GCS_BASE

BUCKET = "test-bucket"
BASE_URL = f"{GCS_BASE}/{BUCKET}"


def make_client():
    return GCSClient(requests.Session(), BUCKET)


@responses.activate
def test_fetch_object_success():
    responses.add(responses.GET, f"{BASE_URL}/some/path.json",
                  body='{"key": "value"}', status=200)
    client = make_client()
    result = client.fetch_object("some/path.json")
    assert result == '{"key": "value"}'


@responses.activate
def test_fetch_object_404():
    responses.add(responses.GET, f"{BASE_URL}/missing.json", status=404)
    client = make_client()
    assert client.fetch_object("missing.json") is None


@responses.activate
def test_fetch_object_500():
    responses.add(responses.GET, f"{BASE_URL}/error.json", status=500)
    client = make_client()
    with pytest.raises(requests.HTTPError):
        client.fetch_object("error.json")


@responses.activate
def test_list_prefixes_single_page():
    xml_body = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>false</IsTruncated>
        <CommonPrefixes><Prefix>pr-logs/pull/org_repo/100/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>pr-logs/pull/org_repo/200/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    responses.add(responses.GET, f"{BASE_URL}/", body=xml_body, status=200)
    client = make_client()
    result = client.list_prefixes("pr-logs/pull/org_repo/")
    assert result == ["pr-logs/pull/org_repo/100/", "pr-logs/pull/org_repo/200/"]


@responses.activate
def test_list_prefixes_pagination():
    page1 = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>true</IsTruncated>
        <NextMarker>pr-logs/pull/org_repo/100/</NextMarker>
        <CommonPrefixes><Prefix>pr-logs/pull/org_repo/100/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    page2 = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>false</IsTruncated>
        <CommonPrefixes><Prefix>pr-logs/pull/org_repo/200/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    responses.add(responses.GET, f"{BASE_URL}/", body=page1, status=200)
    responses.add(responses.GET, f"{BASE_URL}/", body=page2, status=200)
    client = make_client()
    result = client.list_prefixes("pr-logs/pull/org_repo/")
    assert len(result) == 2
    assert "pr-logs/pull/org_repo/100/" in result
    assert "pr-logs/pull/org_repo/200/" in result


@responses.activate
def test_list_prs():
    xml_body = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
        <IsTruncated>false</IsTruncated>
        <CommonPrefixes><Prefix>base/100/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>base/200/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    responses.add(responses.GET, f"{BASE_URL}/", body=xml_body, status=200)
    client = make_client()
    result = client.list_prs("base")
    assert result == ["100", "200"]
