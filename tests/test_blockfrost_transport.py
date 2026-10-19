"""Exercise the timeout adapter through SDK decoding and real requests calls."""

import ast
import inspect
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from unittest.mock import Mock

import pytest
from blockfrost import ApiError
from requests import Response, Session
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


@pytest.fixture(autouse=True)
def default_sdk_endpoint(monkeypatch):
    """A developer's preprod .env must not redirect the mainnet URL assertions below."""
    monkeypatch.delenv("BLOCKFROST_API_URL", raising=False)
    monkeypatch.delenv("BLOCKFROST_API_VERSION", raising=False)


def http_response(data, status=200):
    response = Response()
    response.status_code = status
    response._content = json.dumps(data).encode()
    return response


@contextmanager
def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _sdk_reads_used_by_client() -> set[str]:
    """Method names the client calls on _get_api(); aliasing the API object would hide them, so it is refused."""
    used = set()
    for node in ast.walk(ast.parse(inspect.getsource(client))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if isinstance(receiver, ast.Call) and getattr(receiver.func, "id", None) == "_get_api":
                used.add(node.func.attr)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            assert getattr(node.value.func, "id", None) != "_get_api", "an alias of _get_api() hides reads"
    return used


def test_every_sdk_read_the_client_uses_is_timeout_bounded():
    """A new _get_api().<read>() call must come with a bounded override here and a decoding case above."""
    bounded = {
        name
        for name, member in vars(transport.TimeoutBlockFrostApi).items()
        if callable(member) and not name.startswith("_")
    }

    assert _sdk_reads_used_by_client() == bounded == {case.values[0] for case in _READS}


@pytest.mark.parametrize("method,args,kwargs,path,data", _READS)
def test_every_used_sdk_read_sets_timeout_and_preserves_decoding(monkeypatch, method, args, kwargs, path, data):
    get = Mock(return_value=http_response(data))
    monkeypatch.setattr(transport._session, "get", get)
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
        transport._session,
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
    monkeypatch.setattr(transport._session, "get", get)
    monkeypatch.setattr(client, "_get_api", lambda: transport.TimeoutBlockFrostApi(project_id="mainnetTEST"))
    monkeypatch.setattr(client._fetch_history_page.retry, "wait", wait_none())

    entries = client._fetch_history_page("asset", 2)

    assert entries[0].tx_hash == "mint"
    assert get.call_count == 2
    assert get.call_args_list[0] == get.call_args_list[1]
    assert get.call_args.kwargs["timeout"] == (3.05, 10)


def test_reads_share_one_session_pooled_for_the_read_workers():
    assert isinstance(transport._session, Session)
    for scheme in ("https://", "http://"):
        adapter = transport._session.get_adapter(f"{scheme}cardano-mainnet.blockfrost.io")
        assert adapter._pool_maxsize == transport.POOL_SIZE


def test_sequential_reads_reuse_one_connection():
    connections = []

    class KeepAlive(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            connections.append(self.client_address)

        def do_GET(self):
            body = json.dumps({"height": 123}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    with _serve(KeepAlive) as port:
        api = transport.TimeoutBlockFrostApi(project_id="mainnetTEST", base_url=f"http://127.0.0.1:{port}")
        assert api.block("one").height == 123
        assert api.block("two").height == 123

    assert len(connections) == 1


@pytest.mark.parametrize(
    "failure,timeout",
    [
        pytest.param(ReadTimeout, (0.5, 0.05), id="black-hole"),
        pytest.param(ChunkedEncodingError, (0.5, 2), id="truncated-body"),
    ],
)
def test_real_transport_failures(monkeypatch, failure, timeout):
    """Local server stalls or closes mid-body; no external API or synthetic exception."""
    received = Event()
    release = Event()

    class Broken(BaseHTTPRequestHandler):
        def do_GET(self):
            received.set()
            if failure is ReadTimeout:
                release.wait(timeout=2)
                return
            # One write carries the headers and the partial body, so a scheduling gap between
            # them cannot turn this case into a read timeout on a loaded runner.
            self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"height":123')
            self.close_connection = True

        def log_message(self, *_):
            pass

    monkeypatch.setattr(transport, "REQUEST_TIMEOUT", timeout)
    with _serve(Broken) as port:
        api = transport.TimeoutBlockFrostApi(project_id="mainnetTEST", base_url=f"http://127.0.0.1:{port}")
        try:
            with pytest.raises(failure):
                api.block("hash")
            assert received.is_set()
        finally:
            release.set()
