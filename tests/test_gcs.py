"""Tests for GCSClient (pure HTTP, no caching)."""
import pytest
import requests
import responses

from scraper.gcs import GCSClient, GCS_BASE

BUCKET = "test-bucket"
BASE_URL = f"{GCS_BASE}/{BUCKET}"


def make_client():
    return GCSClient(requests.Session(), BUCKET)


@responses.activate
def test_fetch_text_success():
    responses.add(responses.GET, f"{BASE_URL}/some/path.json",
                  body='{"key": "value"}', status=200)
    client = make_client()
    result = client.fetch_text("some/path.json")
    assert result == '{"key": "value"}'


@responses.activate
def test_fetch_text_404():
    responses.add(responses.GET, f"{BASE_URL}/missing.json", status=404)
    client = make_client()
    assert client.fetch_text("missing.json") is None


@responses.activate
def test_fetch_text_500():
    responses.add(responses.GET, f"{BASE_URL}/error.json", status=500)
    client = make_client()
    with pytest.raises(requests.HTTPError):
        client.fetch_text("error.json")


@responses.activate
def test_fetch_bytes_success():
    responses.add(responses.GET, f"{BASE_URL}/data.tar",
                  body=b'\x00\x01\x02\x03', status=200)
    client = make_client()
    assert client.fetch_bytes("data.tar") == b'\x00\x01\x02\x03'


@responses.activate
def test_fetch_bytes_404():
    responses.add(responses.GET, f"{BASE_URL}/missing.tar", status=404)
    client = make_client()
    assert client.fetch_bytes("missing.tar") is None


@responses.activate
def test_head_exists():
    responses.add(responses.HEAD, f"{BASE_URL}/exists.json", status=200)
    client = make_client()
    assert client.head("exists.json") is True


@responses.activate
def test_head_missing():
    responses.add(responses.HEAD, f"{BASE_URL}/missing.json", status=404)
    client = make_client()
    assert client.head("missing.json") is False


@responses.activate
def test_stream_success():
    responses.add(responses.GET, f"{BASE_URL}/large.tar",
                  body=b"streaming content here", status=200)
    client = make_client()
    chunks = client.stream("large.tar")
    assert chunks is not None
    content = b"".join(chunks)
    assert content == b"streaming content here"


@responses.activate
def test_stream_404():
    responses.add(responses.GET, f"{BASE_URL}/missing.tar", status=404)
    client = make_client()
    assert client.stream("missing.tar") is None


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
