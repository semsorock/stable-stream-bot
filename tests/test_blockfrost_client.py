"""Tests for the Blockfrost client cross-referencing logic."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from blockfrost import ApiError
from requests.exceptions import ConnectionError, Timeout
from tenacity import RetryError, wait_none

import bot.blockfrost_client as bf_client
from bot.config import NEW_STABLECOINS_ACTIVATED_AT, USDA, USDCX, USDM, Stablecoin
from bot.models import MintEvent


@pytest.fixture(autouse=True)
def default_to_usdm(monkeypatch):
    """Keep existing single-token fixtures scoped to USDM."""
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM,))
    bf_client._fetch_transaction_block_height.cache_clear()
    monkeypatch.setattr(bf_client._fetch_block_metadata.retry, "wait", wait_none())
    monkeypatch.setattr(bf_client._fetch_history_page.retry, "wait", wait_none())
    monkeypatch.setattr(bf_client._fetch_transaction_block_height.__wrapped__.retry, "wait", wait_none())


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
        history.side_effect = [history.return_value, *(api_error(429) for _ in range(3))]
    api = SimpleNamespace(asset_history=history, block=block, transaction=transaction)
    monkeypatch.setattr(bf_client, "_get_api", lambda: api)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"known-usdm"})

    assert await bf_client.get_mints_for_block("partially-readable") == [MintEvent("known-usdm", "minted", 1, USDM)]
    assert "history incomplete" in caplog.text
    assert "retaining 100 collected events" in caplog.text
    assert history.call_count == (4 if failure == "later-history-page" else 1)


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
