import pytest
import requests
import responses

from scraper.gcs import GCSClient, GCS_BASE

BUCKET = "test-bucket"
BASE_URL = f"{GCS_BASE}/{BUCKET}"


def make_client(cache_dir=None):
    return GCSClient(requests.Session(), BUCKET, cache_dir=cache_dir)


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


# --- Cache tests ---

@responses.activate
def test_cache_fetch_object_hit(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/data.json",
                  body='{"cached": true}', status=200)
    client = make_client(cache_dir=str(tmp_path))
    # First call fetches from GCS and caches
    assert client.fetch_object("data.json") == '{"cached": true}'
    assert len(responses.calls) == 1
    # Second call serves from cache
    assert client.fetch_object("data.json") == '{"cached": true}'
    assert len(responses.calls) == 1  # no new HTTP call


@responses.activate
def test_cache_fetch_object_miss(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/missing.json", status=404)
    client = make_client(cache_dir=str(tmp_path))
    assert client.fetch_object("missing.json") is None
    assert len(responses.calls) == 1
    # Second call returns None from cache without HTTP
    assert client.fetch_object("missing.json") is None
    assert len(responses.calls) == 1


@responses.activate
def test_cache_fetch_binary_hit(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/data.tar",
                  body=b'\x00\x01\x02\x03', status=200)
    client = make_client(cache_dir=str(tmp_path))
    assert client.fetch_binary("data.tar") == b'\x00\x01\x02\x03'
    assert len(responses.calls) == 1
    assert client.fetch_binary("data.tar") == b'\x00\x01\x02\x03'
    assert len(responses.calls) == 1


@responses.activate
def test_cache_head_uses_fetch_entry(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/exists.json",
                  body='content', status=200)
    client = make_client(cache_dir=str(tmp_path))
    # Fetch populates cache
    client.fetch_object("exists.json")
    # Head reads from cache without HTTP
    assert client.head_object("exists.json") is True
    assert len(responses.calls) == 1  # only the original fetch


@responses.activate
def test_cache_head_miss_cached(tmp_path):
    responses.add(responses.HEAD, f"{BASE_URL}/gone.tar", status=404)
    client = make_client(cache_dir=str(tmp_path))
    assert client.head_object("gone.tar") is False
    assert len(responses.calls) == 1
    # Cached miss
    assert client.head_object("gone.tar") is False
    assert len(responses.calls) == 1


@responses.activate
def test_cache_disabled_no_caching():
    responses.add(responses.GET, f"{BASE_URL}/data.json",
                  body='content', status=200)
    client = make_client(cache_dir=None)
    client.fetch_object("data.json")
    client.fetch_object("data.json")
    assert len(responses.calls) == 2  # no caching, both hit GCS


@responses.activate
def test_cache_persists_across_clients(tmp_path):
    responses.add(responses.GET, f"{BASE_URL}/data.json",
                  body='original', status=200)
    client1 = make_client(cache_dir=str(tmp_path))
    client1.fetch_object("data.json")
    assert len(responses.calls) == 1
    # New client, same cache dir
    client2 = make_client(cache_dir=str(tmp_path))
    assert client2.fetch_object("data.json") == 'original'
    assert len(responses.calls) == 1  # served from disk
