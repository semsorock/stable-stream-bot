"""Tests for the Blockfrost client cross-referencing logic."""

import asyncio
import ssl
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from http.client import RemoteDisconnected
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from blockfrost import ApiError
from requests.exceptions import ChunkedEncodingError, ConnectionError, ProxyError, SSLError, Timeout
from tenacity import RetryError, Retrying, wait_none
from urllib3.exceptions import MaxRetryError
from urllib3.exceptions import SSLError as Urllib3SSLError

import bot.blockfrost_client as bf_client
from bot.config import NEW_STABLECOINS_ACTIVATED_AT, USDA, USDCX, USDM, Stablecoin
from bot.models import MintEvent


def _retried_reads():
    """Every module-level read and the tenacity policy it owns, looking through lru_cache."""
    for name in sorted(vars(bf_client)):
        member = getattr(bf_client, name)
        for candidate in (member, getattr(member, "__wrapped__", None)):
            if isinstance(getattr(candidate, "retry", None), Retrying):
                yield name, candidate.retry
                break


@pytest.fixture(autouse=True)
def default_to_usdm(monkeypatch):
    """Keep existing single-token fixtures scoped to USDM."""
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM,))
    bf_client._fetch_transaction_block_height.cache_clear()
    # Each read owns its policy, so silence all of them rather than one shared instance.
    for _, policy in _retried_reads():
        monkeypatch.setattr(policy, "wait", wait_none())


@pytest.mark.asyncio
async def test_get_mints_for_block_finds_mint_event(monkeypatch):
    """Should return a MintEvent for a minted tx that appears in the block."""
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_abc", "tx_def"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx_abc", "action": "minted", "amount": 5_000_000}],
    )

    events = await bf_client.get_mints_for_block("block-1")

    assert len(events) == 1
    assert events[0] == MintEvent(tx_hash="tx_abc", action="minted", quantity=5_000_000, stablecoin=USDM)
    assert events[0].is_mint


@pytest.mark.asyncio
async def test_get_mints_for_block_finds_burn_event(monkeypatch):
    """Should return a MintEvent with negative quantity for a burned tx."""
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_burn"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx_burn", "action": "burned", "amount": -3_000_000}],
    )

    events = await bf_client.get_mints_for_block("block-2")

    assert len(events) == 1
    assert events[0] == MintEvent(tx_hash="tx_burn", action="burned", quantity=-3_000_000, stablecoin=USDM)
    assert events[0].is_burn


@pytest.mark.asyncio
async def test_get_mints_for_block_empty_when_no_match(monkeypatch):
    """Should return an empty list when no mint/burn tx matches the block."""
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_other"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx_abc", "action": "minted", "amount": 1_000_000}],
    )

    events = await bf_client.get_mints_for_block("block-3")

    assert events == []


@pytest.mark.asyncio
async def test_get_mints_for_block_multiple_events(monkeypatch):
    """Should return multiple events when multiple mint/burn txs are in the block."""
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_a", "tx_b", "tx_c"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [
            {"tx_hash": "tx_a", "action": "minted", "amount": 10_000_000},
            {"tx_hash": "tx_b", "action": "burned", "amount": -2_000_000},
            {"tx_hash": "tx_z", "action": "minted", "amount": 500_000},
        ],
    )

    events = await bf_client.get_mints_for_block("block-4")

    assert len(events) == 2
    assert events[0] == MintEvent(tx_hash="tx_a", action="minted", quantity=10_000_000, stablecoin=USDM)
    assert events[1] == MintEvent(tx_hash="tx_b", action="burned", quantity=-2_000_000, stablecoin=USDM)


@pytest.mark.asyncio
async def test_get_total_supply(monkeypatch):
    """Should return the total supply from Blockfrost."""
    monkeypatch.setattr(bf_client, "_fetch_total_supply", lambda asset_id: 100_000_000 if asset_id == "asset-1" else 0)

    supply = await bf_client.get_total_supply("asset-1")

    assert supply == 100_000_000


@pytest.mark.asyncio
async def test_get_mints_for_block_checks_each_configured_stablecoin(monkeypatch):
    """Should associate events with the stablecoin whose asset history contained them."""
    coin_a = Stablecoin(ticker="AAA", policy_id="policy-a", asset_id="asset-a", decimals=6)
    coin_b = Stablecoin(ticker="BBB", policy_id="policy-b", asset_id="asset-b", decimals=2)
    histories = {
        "asset-a": [{"tx_hash": "tx_a", "action": "minted", "amount": 1_000_000}],
        "asset-b": [{"tx_hash": "tx_b", "action": "burned", "amount": -250}],
    }

    monkeypatch.setattr(bf_client, "STABLECOINS", (coin_a, coin_b))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_a", "tx_b"})
    monkeypatch.setattr(bf_client, "_fetch_recent_mint_burns", lambda asset_id, block: histories[asset_id])

    events = await bf_client.get_mints_for_block("block-multi")

    assert [event.stablecoin for event in events] == [coin_a, coin_b]
    assert [event.display_quantity for event in events] == [1.0, 2.5]


@pytest.mark.parametrize("stablecoin", [USDCX, USDA], ids=lambda coin: coin.ticker)
@pytest.mark.parametrize("action,amount", [("minted", 10_000_000_000), ("burned", -10_000_000_000)])
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.asyncio
async def test_new_token_activation_boundary(monkeypatch, stablecoin, action, amount, offset):
    """Both actions are blocked before rollout, allowed at the inclusive boundary."""
    monkeypatch.setattr(bf_client, "STABLECOINS", (stablecoin,))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx-new"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx-new", "action": action, "amount": amount}],
    )
    monkeypatch.setattr(
        bf_client,
        "_fetch_block_metadata",
        lambda _: SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT + offset),
    )

    events = await bf_client.get_mints_for_block("block-boundary")

    expected = [MintEvent(tx_hash="tx-new", action=action, quantity=amount, stablecoin=stablecoin)]
    assert events == (expected if offset >= 0 else [])


@pytest.mark.asyncio
async def test_replayed_old_block_uses_api_timestamp_and_preserves_usdm(monkeypatch):
    """Untrusted history timestamps cannot turn old/replayed events into new events."""
    looked_up_blocks = []

    def block(block_hash):
        looked_up_blocks.append(block_hash)
        return SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT - 1)

    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM, USDCX, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(block=block))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx-old"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [
            {
                "tx_hash": "tx-old",
                "action": "minted",
                "amount": 10_000_000_000,
                "time": NEW_STABLECOINS_ACTIVATED_AT + 1,
            }
        ],
    )

    for _ in range(2):
        events = await bf_client.get_mints_for_block("historical-block")
        assert events == [MintEvent(tx_hash="tx-old", action="minted", quantity=10_000_000_000, stablecoin=USDM)]

    assert looked_up_blocks == ["historical-block", "historical-block"]


@pytest.mark.asyncio
async def test_block_time_lookup_failure_suppresses_only_new_tokens(monkeypatch):
    """Missing authoritative time fails closed for newly enabled tokens."""

    def fail(_):
        raise RuntimeError("Blockfrost unavailable")

    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM, USDCX, USDA))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx", "action": "burned", "amount": -10_000_000_000}],
    )
    monkeypatch.setattr(bf_client, "_fetch_block_metadata", fail)

    events = await bf_client.get_mints_for_block("block-unknown-time")

    assert events == [MintEvent(tx_hash="tx", action="burned", quantity=-10_000_000_000, stablecoin=USDM)]


@pytest.mark.parametrize("timestamp", [None, True, -1, 123.45, "1792357200"])
@pytest.mark.asyncio
async def test_malformed_api_block_time_suppresses_new_tokens(monkeypatch, timestamp):
    """Malformed block metadata never bypasses the rollout guard."""
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDCX,))
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(block=lambda _: SimpleNamespace(time=timestamp)))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx", "action": "minted", "amount": 10_000_000_000}],
    )

    assert await bf_client.get_mints_for_block("block-invalid-time") == []


@pytest.mark.asyncio
async def test_usdm_does_not_require_block_time(monkeypatch):
    """The existing token keeps processing when no activation check is needed."""

    def unexpected(_):
        raise AssertionError("USDM must not need a timestamp")

    monkeypatch.setattr(bf_client, "_fetch_block_metadata", unexpected)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx", "action": "minted", "amount": 1_000_000}],
    )

    events = await bf_client.get_mints_for_block("old-usdm-block")

    assert [event.stablecoin for event in events] == [USDM]


@pytest.mark.parametrize("action,sign", [("minted", 1), ("burned", -1)])
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.asyncio
async def test_usdcx_minimum_uses_exact_base_units(monkeypatch, caplog, action, sign, offset):
    """The inclusive 10,000-token floor must not round up a microtoken below it."""
    caplog.set_level("INFO", logger="bot.blockfrost_client")
    amount = sign * (10_000_000_000 + offset)
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDCX,))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx", "action": action, "amount": amount}],
    )
    metadata = Mock(return_value=SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT))
    monkeypatch.setattr(bf_client, "_fetch_block_metadata", metadata)

    events = await bf_client.get_mints_for_block("block-threshold")

    assert events == ([MintEvent("tx", action, amount, USDCX)] if offset >= 0 else [])
    assert metadata.call_count == 1
    if offset < 0:
        assert f"Skipping USDCx {action} below 10000-token threshold: tx=tx, quantity={amount}" in caplog.text


@pytest.mark.parametrize("stablecoin", [USDM, USDA], ids=lambda coin: coin.ticker)
@pytest.mark.parametrize("action,amount", [("minted", 1), ("burned", -1)])
@pytest.mark.asyncio
async def test_other_tokens_have_no_amount_floor(monkeypatch, stablecoin, action, amount):
    monkeypatch.setattr(bf_client, "STABLECOINS", (stablecoin,))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx", "action": action, "amount": amount}],
    )
    monkeypatch.setattr(
        bf_client, "_fetch_block_metadata", lambda _: SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT)
    )

    assert await bf_client.get_mints_for_block("block-tiny") == [MintEvent("tx", action, amount, stablecoin)]


def api_error(status):
    return ApiError(
        SimpleNamespace(status_code=status, json=lambda: {"status_code": status, "error": "", "message": ""})
    )


def tls_eof_error(legacy=False):
    """Match both wrapped TLS EOF variants observed in Cloud Run logs."""
    reason = (
        ssl.SSLError(5, "[SSL: UNEXPECTED_EOF_WHILE_READING] unexpected eof while reading")
        if legacy
        else ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
    )
    return SSLError(MaxRetryError(None, "/api/v0/assets/asset/history", reason=Urllib3SSLError(reason)))


_READ_CASES = [
    pytest.param("_fetch_history_page", "asset_history", ("asset", 2), [], [], 5, False, id="history-page"),
    pytest.param(
        "_fetch_total_supply", "asset", ("asset",), SimpleNamespace(quantity="123"), 123, 5, False, id="supply"
    ),
    pytest.param("_fetch_block_tx_hashes", "block_transactions", ("block",), ["tx"], {"tx"}, 5, True, id="block-txs"),
    pytest.param(
        "_fetch_block_metadata",
        "block",
        ("block",),
        SimpleNamespace(height=1),
        SimpleNamespace(height=1),
        5,
        True,
        id="block-metadata",
    ),
    pytest.param(
        "_fetch_transaction_block_height",
        "transaction",
        ("tx",),
        SimpleNamespace(block_height=1),
        1,
        5,
        True,
        id="boundary-height",
    ),
]


@pytest.mark.parametrize("fetcher,method,args,response,expected,attempts,retry_404", _READ_CASES)
@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("Connection aborted.", RemoteDisconnected("Remote end closed connection without response")),
        ChunkedEncodingError("Response ended prematurely"),
        tls_eof_error(),
        tls_eof_error(legacy=True),
        SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] unexpected eof while reading"),
        Timeout("read timed out"),
        api_error(429),
        api_error(500),
        api_error(502),
        api_error(503),
    ],
    ids=[
        "remote-disconnected",
        "mid-body-disconnect",
        "tls-eof",
        "legacy-tls-eof",
        "flattened-tls-eof",
        "timeout",
        "429",
        "500",
        "502",
        "503",
    ],
)
def test_blockfrost_reads_retry_transient_failures(
    monkeypatch, caplog, fetcher, method, args, response, expected, attempts, retry_404, error
):
    request = Mock(side_effect=[error, response])
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(**{method: request}))

    assert getattr(bf_client, fetcher)(*args) == expected

    assert request.call_count == 2
    assert "Retrying" in caplog.text
    assert not any(record.levelname == "ERROR" for record in caplog.records)
    assert request.call_args_list[0] == request.call_args_list[1]


@pytest.mark.parametrize("fetcher,method,args,response,expected,attempts,retry_404", _READ_CASES)
@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("closed"),
        ChunkedEncodingError("truncated"),
        Timeout("timed out"),
        api_error(502),
        tls_eof_error(),
    ],
)
def test_blockfrost_read_retries_are_bounded(
    monkeypatch, fetcher, method, args, response, expected, attempts, retry_404, error
):
    request = Mock(side_effect=error)
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(**{method: request}))

    with pytest.raises(RetryError) as failure:
        getattr(bf_client, fetcher)(*args)

    assert request.call_count == attempts
    assert failure.value.last_attempt.exception() is error


@pytest.mark.parametrize("fetcher,method,args,response,expected,attempts,retry_404", _READ_CASES)
@pytest.mark.parametrize("status", [400, 401, 403])
def test_blockfrost_reads_do_not_retry_permanent_errors(
    monkeypatch, fetcher, method, args, response, expected, attempts, retry_404, status
):
    error = api_error(status)
    request = Mock(side_effect=error)
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(**{method: request}))

    with pytest.raises(ApiError) as failure:
        getattr(bf_client, fetcher)(*args)

    assert failure.value is error
    request.assert_called_once()


@pytest.mark.parametrize("fetcher,method,args,response,expected,attempts,retry_404", _READ_CASES)
@pytest.mark.parametrize(
    "error",
    [
        SSLError("certificate verify failed"),
        SSLError(
            MaxRetryError(
                None,
                "/",
                reason=Urllib3SSLError(
                    ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] hostname mismatch"),
                ),
            )
        ),
        SSLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")),
        SSLError("unknown TLS failure"),
        ProxyError("407 Proxy Authentication Required"),
        ProxyError(tls_eof_error()),
    ],
    ids=["tls", "wrapped-certificate", "wrong-tls-version", "unknown-tls", "proxy", "proxy-tls-eof"],
)
def test_blockfrost_reads_do_not_retry_local_transport_misconfiguration(
    monkeypatch, fetcher, method, args, response, expected, attempts, retry_404, error
):
    """A broken CA bundle or proxy is not transient; it must surface at once, not after the budget."""
    request = Mock(side_effect=error)
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(**{method: request}))

    with pytest.raises(type(error)) as failure:
        getattr(bf_client, fetcher)(*args)

    assert failure.value is error
    request.assert_called_once()


def test_certificate_failure_takes_priority_over_wrapped_eof():
    error = tls_eof_error()
    error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")

    assert not bf_client._is_transient_error(error)


def test_tls_eof_in_explicit_cause_is_retryable():
    error = SSLError("TLS handshake closed")
    error.__cause__ = ssl.SSLEOFError(8, "EOF occurred in violation of protocol")

    assert bf_client._is_transient_error(error)


def test_tls_eof_classifier_ignores_unrelated_context_and_handles_cycles():
    error = SSLError("unknown TLS failure")
    error.__context__ = tls_eof_error()
    error.__cause__ = error

    assert not bf_client._is_transient_error(error)


@pytest.mark.parametrize("action,sign", [("minted", 1), ("burned", -1)])
@pytest.mark.asyncio
async def test_tls_eof_recovery_keeps_usdm_and_other_tokens(monkeypatch, caplog, action, sign):
    calls = []

    def history(asset_id, **kwargs):
        calls.append(asset_id)
        if asset_id == USDM.asset_id and calls.count(asset_id) == 1:
            raise tls_eof_error()
        return [SimpleNamespace(tx_hash="event", action=action, amount=str(sign * 10_000_000_000))]

    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM, USDCX, USDA))
    api = SimpleNamespace(
        asset_history=history,
        block=lambda _: SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT, height=100),
    )
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"event"})

    events = await bf_client.get_mints_for_block("tls-recovery-block")

    assert [event.stablecoin for event in events] == [USDM, USDCX, USDA]
    assert all(event.quantity == sign * 10_000_000_000 and event.action == action for event in events)
    assert calls.count(USDM.asset_id) == 2
    assert calls.count(USDCX.asset_id) == calls.count(USDA.asset_id) == 1
    assert "Cannot read USDM history" not in caplog.text


@pytest.mark.parametrize("fetcher,method,args,response,expected,attempts,retry_404", _READ_CASES)
def test_only_block_related_reads_retry_indexing_404(
    monkeypatch, caplog, fetcher, method, args, response, expected, attempts, retry_404
):
    caplog.set_level("INFO", logger="bot.blockfrost_client")
    request = Mock(side_effect=[api_error(404), response])
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(**{method: request}))

    if retry_404:
        assert getattr(bf_client, fetcher)(*args) == expected
        assert request.call_count == 2
        assert "Retrying" in caplog.text
        assert all(record.levelname == "INFO" for record in caplog.records)
    else:
        with pytest.raises(ApiError):
            getattr(bf_client, fetcher)(*args)
        request.assert_called_once()


@pytest.mark.asyncio
async def test_remote_disconnect_recovery_retains_usdm_event(monkeypatch, caplog):
    """Reproduce the deployed first-page disconnect without dropping the block's event."""
    history = Mock(
        side_effect=[
            ConnectionError("Connection aborted.", RemoteDisconnected("Remote end closed connection without response")),
            [SimpleNamespace(tx_hash="usdm-mint", action="minted", amount="1000000")],
        ]
    )
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(asset_history=history))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"usdm-mint"})

    assert await bf_client.get_mints_for_block("affected-block") == [MintEvent("usdm-mint", "minted", 1_000_000, USDM)]
    assert history.call_count == 2
    assert "Cannot read USDM history" not in caplog.text


def test_block_transaction_retry_does_not_refetch_completed_pages(monkeypatch):
    page_one = [f"tx-{i}" for i in range(100)]
    pages = Mock(side_effect=[page_one, api_error(503), ["last"]])
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(block_transactions=pages))

    assert bf_client._fetch_block_tx_hashes("busy-block") == {*page_one, "last"}

    assert [call.kwargs["page"] for call in pages.call_args_list] == [1, 2, 2]


def test_history_uses_full_retry_budget_before_dropping_events(monkeypatch):
    entries = [SimpleNamespace(tx_hash="mint", action="minted", amount="1")]
    history = Mock(side_effect=[*(api_error(503) for _ in range(4)), entries])
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(asset_history=history))

    assert bf_client._fetch_recent_mint_burns("asset", bf_client._BlockMetadata("block")) == [
        {"tx_hash": "mint", "action": "minted", "amount": 1}
    ]
    assert history.call_count == 5


def _failed(error):
    outcome = Future()
    outcome.set_exception(error)
    return outcome


def test_each_read_owns_its_retry_policy():
    policies = dict(_retried_reads())

    assert set(policies) == {
        "_fetch_block_metadata",
        "_fetch_block_transactions_page",
        "_fetch_history_page",
        "_fetch_total_supply",
        "_fetch_transaction_block_height",
    }
    assert len({id(policy) for policy in policies.values()}) == len(policies)
    # The autouse fixture must have reached every one of them, not a single shared instance.
    assert all(isinstance(policy.wait, wait_none) for policy in policies.values())


@pytest.mark.parametrize(
    "error", [api_error(503), api_error(429), ConnectionError("reset")], ids=["503", "429", "reset"]
)
def test_contention_waits_use_full_jitter(monkeypatch, error):
    uniform = Mock(side_effect=lambda low, high: high)
    monkeypatch.setattr("tenacity.wait.random.uniform", uniform)
    state = SimpleNamespace(attempt_number=1, outcome=_failed(error))

    assert bf_client._wait_for_read(state) == 2
    uniform.assert_called_once_with(1, 2)
    state.attempt_number = 4
    assert bf_client._wait_for_read(state) == 8
    assert uniform.call_args.args == (1, 8)


def test_indexing_waits_keep_a_floor_jitter_cannot_shorten(monkeypatch):
    """A block Blockfrost has not indexed yet always gets 16 seconds before the final attempt."""
    spread = Mock(return_value=0.0)
    monkeypatch.setattr("tenacity.wait.random.random", spread)
    outcome = _failed(api_error(404))

    waits = [bf_client._wait_for_read(SimpleNamespace(attempt_number=n, outcome=outcome)) for n in range(1, 5)]

    assert waits == [2, 2, 4, 8]
    assert spread.call_count == 4
    spread.return_value = 1.0
    assert bf_client._wait_for_read(SimpleNamespace(attempt_number=1, outcome=outcome)) == 3


@pytest.mark.asyncio
async def test_blockfrost_pool_is_bounded_and_leaves_default_executor_free(monkeypatch):
    release = Event()
    full = Event()
    lock = Lock()
    started = 0

    def blocked():
        nonlocal started
        with lock:
            started += 1
            if started == 2:
                full.set()
        release.wait(timeout=2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(bf_client, "_executor", pool)
        tasks = [asyncio.create_task(bf_client._run_read(blocked)) for _ in range(4)]
        try:
            assert await asyncio.to_thread(full.wait, 1)
            assert started == 2
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), timeout=1) == "free"
        finally:
            release.set()
            await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_dedicated_executor_preserves_request_context():
    trace = ContextVar("trace", default="missing")
    token = trace.set("request-trace")
    try:
        assert await bf_client._run_read(trace.get) == "request-trace"
    finally:
        trace.reset(token)


@pytest.mark.asyncio
async def test_shutdown_drops_queued_reads_and_later_reads_get_a_fresh_pool(monkeypatch):
    started = Event()
    release = Event()

    def occupy_worker():
        started.set()
        release.wait(timeout=2)
        return "finished"

    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(bf_client, "_executor", pool)
    active = asyncio.create_task(bf_client._run_read(occupy_worker))
    queued = asyncio.create_task(bf_client._run_read(lambda: "never runs"))
    try:
        assert await asyncio.to_thread(started.wait, 1)

        bf_client.shutdown_reads()

        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        assert await active == "finished"
        assert bf_client._executor is None
        assert await bf_client._run_read(lambda: "fresh pool") == "fresh pool"
        assert bf_client._executor is not pool
    finally:
        release.set()
        bf_client.shutdown_reads()


@pytest.mark.parametrize("error", [api_error(502), Timeout("read timed out"), ConnectionError("connection reset")])
@pytest.mark.asyncio
async def test_transient_block_metadata_failure_recovers(monkeypatch, error):
    metadata = Mock(side_effect=[error, SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT)])
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDCX, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(block=metadata))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx", "action": "minted", "amount": 10_000_000_000}],
    )

    events = await bf_client.get_mints_for_block("block-recovered")

    assert [event.stablecoin for event in events] == [USDCX, USDA]
    assert metadata.call_count == 2


@pytest.mark.parametrize("error,attempts", [(api_error(502), 5), (Timeout("timed out"), 5), (api_error(403), 1)])
@pytest.mark.asyncio
async def test_metadata_failure_exhausts_retries_then_preserves_usdm(monkeypatch, error, attempts):
    metadata = Mock(side_effect=error)
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM, USDCX, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(block=metadata))
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _, block: [{"tx_hash": "tx", "action": "minted", "amount": 10_000_000_000}],
    )

    assert await bf_client.get_mints_for_block("block-unavailable") == [MintEvent("tx", "minted", 10_000_000_000, USDM)]
    assert metadata.call_count == attempts


def history_entry(tx_hash, action="minted", amount=1):
    return SimpleNamespace(tx_hash=tx_hash, action=action, amount=str(amount))


@pytest.mark.asyncio
async def test_pagination_finds_large_mint_after_more_than_100_small_events(monkeypatch):
    """Later bridge activity cannot evict a qualifying event from the target block."""
    first_page = [history_entry(f"small-{i}") for i in range(100)]
    second_page = [history_entry("small-100"), history_entry("large", amount=25_000_000_000)]
    history = Mock(side_effect=[first_page, second_page])
    api = SimpleNamespace(
        asset_history=history,
        block=Mock(return_value=SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT, height=100)),
        transaction=Mock(return_value=SimpleNamespace(block_height=101)),
    )
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDCX,))
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"small-100", "large"})

    events = await bf_client.get_mints_for_block("block-busy")

    assert events == [MintEvent("large", "minted", 25_000_000_000, USDCX)]
    assert [call.kwargs["page"] for call in history.call_args_list] == [1, 2]
    assert all(call.kwargs["order"] == "desc" and call.kwargs["count"] == 100 for call in history.call_args_list)
    api.block.assert_called_once_with("block-busy")
    api.transaction.assert_called_once_with("small-99")


def test_pagination_continues_through_multiple_pages_in_target_block(monkeypatch):
    pages = [
        [history_entry(f"tx-{i}") for i in range(100)],
        [history_entry(f"tx-{i}") for i in range(100, 200)],
        [history_entry("last")],
    ]
    api = SimpleNamespace(
        asset_history=Mock(side_effect=pages),
        block=Mock(return_value=SimpleNamespace(height=100)),
        transaction=Mock(return_value=SimpleNamespace(block_height=100)),
    )
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)

    events = bf_client._fetch_recent_mint_burns(USDM.asset_id, bf_client._BlockMetadata("block"))

    assert len(events) == 201
    assert api.asset_history.call_count == 3
    assert api.transaction.call_count == 2
    api.block.assert_called_once_with("block")


def test_pagination_stops_when_full_page_reaches_older_block(monkeypatch):
    history = [history_entry("current"), *(history_entry(f"old-{i}") for i in range(99))]
    api = SimpleNamespace(
        asset_history=Mock(return_value=history),
        block=Mock(return_value=SimpleNamespace(height=100)),
        transaction=Mock(return_value=SimpleNamespace(block_height=99)),
    )
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)

    events = bf_client._fetch_recent_mint_burns(USDM.asset_id, bf_client._BlockMetadata("block"))

    assert len(events) == 100
    api.asset_history.assert_called_once_with(USDM.asset_id, order="desc", count=100, page=1)
    api.transaction.assert_called_once_with("old-98")


@pytest.mark.asyncio
async def test_pagination_deduplicates_overlapping_pages(monkeypatch):
    first_page = [history_entry(f"tx-{i}") for i in range(100)]
    api = SimpleNamespace(
        asset_history=Mock(side_effect=[first_page, [history_entry("tx-99"), history_entry("last")]]),
        block=Mock(return_value=SimpleNamespace(height=100)),
        transaction=Mock(return_value=SimpleNamespace(block_height=100)),
    )
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx-99", "last"})

    events = await bf_client.get_mints_for_block("block-overlap")

    assert [event.tx_hash for event in events] == ["tx-99", "last"]


@pytest.mark.asyncio
async def test_metadata_lookup_shared_across_concurrent_full_histories_and_activation(monkeypatch):
    history = [history_entry("current", amount=10_000_000_000), *(history_entry(f"old-{i}") for i in range(99))]
    api = SimpleNamespace(
        asset_history=Mock(return_value=history),
        block=Mock(return_value=SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT, height=100)),
        transaction=Mock(return_value=SimpleNamespace(block_height=99)),
    )
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM, USDCX, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"current"})

    events = await bf_client.get_mints_for_block("block-shared")

    assert [event.stablecoin for event in events] == [USDM, USDCX, USDA]
    api.block.assert_called_once_with("block-shared")


def test_page_boundary_height_is_cached_across_blocks(monkeypatch):
    api = SimpleNamespace(
        asset_history=Mock(return_value=[history_entry(f"old-{i}") for i in range(100)]),
        block=Mock(side_effect=[SimpleNamespace(height=100), SimpleNamespace(height=101)]),
        transaction=Mock(return_value=SimpleNamespace(block_height=99)),
    )
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)

    bf_client._fetch_recent_mint_burns(USDM.asset_id, bf_client._BlockMetadata("block-100"))
    bf_client._fetch_recent_mint_burns(USDM.asset_id, bf_client._BlockMetadata("block-101"))

    api.transaction.assert_called_once_with("old-99")
    assert api.block.call_count == 2


@pytest.mark.asyncio
async def test_one_unavailable_history_does_not_drop_other_tokens(monkeypatch):
    """An unreadable pagination boundary cannot discard unrelated USDM events."""
    api = SimpleNamespace(
        asset_history=Mock(
            side_effect=lambda asset_id, **kwargs: (
                [history_entry("usdm", amount=10_000_000_000)]
                if asset_id == USDM.asset_id
                else [history_entry(f"busy-{i}") for i in range(100)]
            )
        ),
        block=Mock(side_effect=api_error(502)),
    )
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM, USDCX, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"usdm"})

    assert await bf_client.get_mints_for_block("block-partial") == [MintEvent("usdm", "minted", 10_000_000_000, USDM)]
    assert api.block.call_count == 5


@pytest.mark.parametrize("height", [None, True, -1, 123.45, "100"])
def test_invalid_page_boundary_height_is_rejected(monkeypatch, height):
    monkeypatch.setattr(
        bf_client, "_get_api", lambda: SimpleNamespace(transaction=lambda _: SimpleNamespace(block_height=height))
    )

    with pytest.raises(ValueError, match="Invalid block height"):
        bf_client._fetch_transaction_block_height("invalid")


def test_shared_metadata_failure_is_not_retried_per_worker(monkeypatch):
    metadata = Mock(side_effect=api_error(502))
    monkeypatch.setattr(bf_client, "_get_api", lambda: SimpleNamespace(block=metadata))
    block = bf_client._BlockMetadata("unavailable")

    for lookup in (block.height, block.timestamp):
        with pytest.raises(RetryError):
            lookup()

    assert metadata.call_count == 5


@pytest.mark.asyncio
async def test_pre_activation_replay_never_reads_new_token_histories(monkeypatch):
    history = Mock(return_value=[history_entry("old-usdm", amount=1_000_000)])
    api = SimpleNamespace(
        asset_history=history,
        block=Mock(return_value=SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT - 1, height=1)),
    )
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM, USDCX, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"old-usdm"})

    assert await bf_client.get_mints_for_block("old-block") == [MintEvent("old-usdm", "minted", 1_000_000, USDM)]
    history.assert_called_once_with(USDM.asset_id, order="desc", count=100, page=1)


@pytest.mark.parametrize("timestamp", [None, True, -1, NEW_STABLECOINS_ACTIVATED_AT - 1])
@pytest.mark.asyncio
async def test_inactive_tokens_do_not_query_any_history(monkeypatch, timestamp):
    api = SimpleNamespace(
        asset_history=Mock(),
        block=Mock(return_value=SimpleNamespace(time=timestamp, height=1)),
    )
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDCX, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"old"})

    assert await bf_client.get_mints_for_block("inactive-block") == []
    api.asset_history.assert_not_called()


@pytest.mark.asyncio
async def test_exhausted_activation_metadata_retries_never_read_new_histories(monkeypatch):
    api = SimpleNamespace(asset_history=Mock(), block=Mock(side_effect=Timeout("unavailable")))
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDCX, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"old"})

    assert await bf_client.get_mints_for_block("unavailable-block") == []
    assert api.block.call_count == 5
    api.asset_history.assert_not_called()


@pytest.mark.asyncio
async def test_history_cancellation_propagates(monkeypatch):
    def cancelled(_asset_id, _block):
        raise asyncio.CancelledError

    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(bf_client, "_fetch_recent_mint_burns", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await bf_client.get_mints_for_block("cancelled-block")


@pytest.mark.parametrize("failure", ["block-metadata", "boundary-transaction", "later-history-page"])
@pytest.mark.asyncio
async def test_incomplete_history_preserves_known_usdm_events(monkeypatch, caplog, failure):
    history = Mock(return_value=[history_entry("known-usdm"), *(history_entry(f"other-{i}") for i in range(99))])
    block = Mock(return_value=SimpleNamespace(height=100))
    transaction = Mock(return_value=SimpleNamespace(block_height=100))
    if failure == "block-metadata":
        block.side_effect = api_error(502)
    elif failure == "boundary-transaction":
        transaction.side_effect = Timeout("boundary transaction unavailable")
    else:
        history.side_effect = [history.return_value, *(api_error(429) for _ in range(5))]
    api = SimpleNamespace(asset_history=history, block=block, transaction=transaction)
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"known-usdm"})

    assert await bf_client.get_mints_for_block("partially-readable") == [MintEvent("known-usdm", "minted", 1, USDM)]
    assert "history incomplete" in caplog.text
    assert "retaining 100 collected events" in caplog.text
    assert history.call_count == (6 if failure == "later-history-page" else 1)


@pytest.mark.asyncio
async def test_initial_history_failure_still_preserves_other_tokens(monkeypatch, caplog):
    def history(asset_id, **_kwargs):
        if asset_id == USDA.asset_id:
            raise RuntimeError("USDA unavailable")
        return [history_entry("known-usdm")]

    api = SimpleNamespace(
        asset_history=Mock(side_effect=history),
        block=Mock(return_value=SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT)),
    )
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM, USDA))
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"known-usdm"})

    assert await bf_client.get_mints_for_block("initial-failure") == [MintEvent("known-usdm", "minted", 1, USDM)]
    assert "Cannot read USDA history" in caplog.text
