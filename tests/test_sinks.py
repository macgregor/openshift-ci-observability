import responses
import requests

from scraper.sinks import VictoriaMetricsSink, VictoriaLogsSink


@responses.activate
def test_vm_sink_push_single_batch():
    responses.add(responses.POST, "http://vm:8428/api/v1/import/prometheus", status=204)
    session = requests.Session()
    sink = VictoriaMetricsSink(session, "http://vm:8428")
    records = [f"metric_{i} {i}" for i in range(100)]
    sink.push(records)
    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers["Content-Type"] == "text/plain"


@responses.activate
def test_vm_sink_push_multiple_batches():
    responses.add(responses.POST, "http://vm:8428/api/v1/import/prometheus", status=204)
    session = requests.Session()
    sink = VictoriaMetricsSink(session, "http://vm:8428")
    records = [f"metric_{i} {i}" for i in range(1200)]
    sink.push(records)
    assert len(responses.calls) == 3


@responses.activate
def test_vm_sink_push_empty():
    session = requests.Session()
    sink = VictoriaMetricsSink(session, "http://vm:8428")
    sink.push([])
    assert len(responses.calls) == 0


@responses.activate
def test_vl_sink_push_batching():
    responses.add(responses.POST, "http://vl:9428/insert/jsonline", status=204)
    session = requests.Session()
    sink = VictoriaLogsSink(session, "http://vl:9428")
    records = ['{"_msg":"test"}' for _ in range(1200)]
    sink.push(records)
    assert len(responses.calls) == 3
    assert responses.calls[0].request.headers["Content-Type"] == "application/stream+json"
    assert "_stream_fields=job_name%2Cbuild_id" in responses.calls[0].request.url


@responses.activate
def test_vl_sink_push_raises_on_http_error():
    responses.add(responses.POST, "http://vl:9428/insert/jsonline", status=500)
    session = requests.Session()
    sink = VictoriaLogsSink(session, "http://vl:9428")
    import pytest
    with pytest.raises(requests.HTTPError):
        sink.push(['{"_msg":"test"}'])
