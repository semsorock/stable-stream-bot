"""Exercise the timeout adapter through SDK decoding and real requests calls."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from unittest.mock import Mock

import pytest
from blockfrost import ApiError
from requests import Response
from requests.exceptions import ChunkedEncodingError, ReadTimeout
from tenacity import wait_none

import bot.blockfrost_client as client
import bot.blockfrost_transport as transport

_READS = [
    pytest.param("block", ("hash",), {}, "/blocks/hash", {"height": 123}, id="block"),
    pytest.param(
        "block_transactions", ("hash",), {"count": 100, "page": 3}, "/blocks/hash/txs", ["tx"], id="block-txs"
    ),
    pytest.param("transaction", ("hash",), {}, "/txs/hash", {"block_height": 123}, id="transaction"),
    pytest.param("asset", ("asset",), {}, "/assets/asset", {"quantity": "1000000"}, id="asset"),
    pytest.param(
        "asset_history",
        ("asset",),
        {"count": 100, "page": 2, "order": "desc"},
        "/assets/asset/history",
        [{"tx_hash": "mint", "action": "minted", "amount": "1000000"}],
        id="history",
    ),
]


def http_response(data, status=200):
    response = Response()
    response.status_code = status
    response._content = json.dumps(data).encode()
    return response


@pytest.mark.parametrize("method,args,kwargs,path,data", _READS)
def test_every_used_sdk_read_sets_timeout_and_preserves_decoding(monkeypatch, method, args, kwargs, path, data):
    get = Mock(return_value=http_response(data))
    monkeypatch.setattr(transport.requests, "get", get)
    api = transport.TimeoutBlockFrostApi(project_id="mainnetTEST")

    result = getattr(api, method)(*args, **kwargs)

    if isinstance(data, dict):
        assert result.to_dict() == data
    else:
        assert [item.to_dict() if hasattr(item, "to_dict") else item for item in result] == data
    call = get.call_args.kwargs
    assert call["url"] == f"https://cardano-mainnet.blockfrost.io/api/v0{path}"
    assert call["headers"]["project_id"] == "mainnetTEST"
    assert call["timeout"] == (3.05, 10)
    assert call["params"] == (api.query_parameters(kwargs) if kwargs else None)


@pytest.mark.parametrize("method,args,kwargs,path,data", _READS)
def test_sdk_error_contract_is_preserved(monkeypatch, method, args, kwargs, path, data):
    monkeypatch.setattr(
        transport.requests,
        "get",
        Mock(
            return_value=http_response(
                {"status_code": 503, "error": "Service Unavailable", "message": "retry later"},
                status=503,
            )
        ),
    )
    api = transport.TimeoutBlockFrostApi(project_id="mainnetTEST")

    with pytest.raises(ApiError) as failure:
        getattr(api, method)(*args, **kwargs)

    assert failure.value.status_code == 503


@pytest.mark.parametrize("error", [ReadTimeout("idle response"), ChunkedEncodingError("truncated JSON")])
def test_timeout_and_mid_body_failures_recover_through_sdk(monkeypatch, error):
    get = Mock(side_effect=[error, http_response([{"tx_hash": "mint", "action": "minted", "amount": "1"}])])
    monkeypatch.setattr(transport.requests, "get", get)
    monkeypatch.setattr(client, "_get_api", lambda: transport.TimeoutBlockFrostApi(project_id="mainnetTEST"))
    monkeypatch.setattr(client._fetch_history_page.retry, "wait", wait_none())

    entries = client._fetch_history_page("asset", 2)

    assert entries[0].tx_hash == "mint"
    assert get.call_count == 2
    assert get.call_args_list[0] == get.call_args_list[1]
    assert get.call_args.kwargs["timeout"] == (3.05, 10)


@pytest.mark.parametrize("failure", [ReadTimeout, ChunkedEncodingError], ids=["black-hole", "truncated-body"])
def test_real_transport_failures(monkeypatch, failure):
    """Local server stalls or closes mid-body; no external API or synthetic exception."""
    received = Event()
    release = Event()

    class BlackHole(BaseHTTPRequestHandler):
        def do_GET(self):
            received.set()
            if failure is ReadTimeout:
                release.wait(timeout=2)
            else:
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"height":123')
                self.wfile.flush()
                self.close_connection = True

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BlackHole)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setattr(transport, "REQUEST_TIMEOUT", (0.5, 0.05))
    api = transport.TimeoutBlockFrostApi(project_id="mainnetTEST", base_url=f"http://127.0.0.1:{server.server_port}")
    try:
        with pytest.raises(failure):
            api.block("hash")
        assert received.is_set()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
