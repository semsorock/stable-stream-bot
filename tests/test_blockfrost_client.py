"""Tests for the Blockfrost client cross-referencing logic."""

from types import SimpleNamespace

import pytest

import bot.blockfrost_client as bf_client
from bot.config import NEW_STABLECOINS_ACTIVATED_AT, USDA, USDCX, USDM, Stablecoin
from bot.models import MintEvent


@pytest.fixture(autouse=True)
def default_to_usdm(monkeypatch):
    """Keep existing single-token fixtures scoped to USDM."""
    monkeypatch.setattr(bf_client, "STABLECOINS", (USDM,))


@pytest.mark.asyncio
async def test_get_mints_for_block_finds_mint_event(monkeypatch):
    """Should return a MintEvent for a minted tx that appears in the block."""
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_abc", "tx_def"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _: [{"tx_hash": "tx_abc", "action": "minted", "amount": 5_000_000}],
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
        lambda _: [{"tx_hash": "tx_burn", "action": "burned", "amount": -3_000_000}],
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
        lambda _: [{"tx_hash": "tx_abc", "action": "minted", "amount": 1_000_000}],
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
        lambda _: [
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
    monkeypatch.setattr(bf_client, "_fetch_recent_mint_burns", histories.__getitem__)

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
        lambda _: [{"tx_hash": "tx-new", "action": action, "amount": amount}],
    )
    monkeypatch.setattr(bf_client, "_fetch_block_timestamp", lambda _: NEW_STABLECOINS_ACTIVATED_AT + offset)

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
        lambda _: [
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
        lambda _: [{"tx_hash": "tx", "action": "burned", "amount": -10_000_000_000}],
    )
    monkeypatch.setattr(bf_client, "_fetch_block_timestamp", fail)

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
        lambda _: [{"tx_hash": "tx", "action": "minted", "amount": 10_000_000_000}],
    )

    assert await bf_client.get_mints_for_block("block-invalid-time") == []


@pytest.mark.asyncio
async def test_usdm_does_not_require_block_time(monkeypatch):
    """The existing token keeps processing when no activation check is needed."""

    def unexpected(_):
        raise AssertionError("USDM must not need a timestamp")

    monkeypatch.setattr(bf_client, "_fetch_block_timestamp", unexpected)
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda _: [{"tx_hash": "tx", "action": "minted", "amount": 1_000_000}],
    )

    events = await bf_client.get_mints_for_block("old-usdm-block")

    assert [event.stablecoin for event in events] == [USDM]
