"""Tests for the Blockfrost client cross-referencing logic."""

import pytest

import bot.blockfrost_client as bf_client
from bot.models import MintEvent


@pytest.mark.asyncio
async def test_get_mints_for_block_finds_mint_event(monkeypatch):
    """Should return a MintEvent for a minted tx that appears in the block."""
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_abc", "tx_def"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda: [{"tx_hash": "tx_abc", "action": "minted", "amount": 5_000_000}],
    )

    events = await bf_client.get_mints_for_block("block-1")

    assert len(events) == 1
    assert events[0] == MintEvent(tx_hash="tx_abc", quantity=5_000_000)
    assert events[0].is_mint


@pytest.mark.asyncio
async def test_get_mints_for_block_finds_burn_event(monkeypatch):
    """Should return a MintEvent with negative quantity for a burned tx."""
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_burn"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda: [{"tx_hash": "tx_burn", "action": "burned", "amount": 3_000_000}],
    )

    events = await bf_client.get_mints_for_block("block-2")

    assert len(events) == 1
    assert events[0] == MintEvent(tx_hash="tx_burn", quantity=-3_000_000)
    assert events[0].is_burn


@pytest.mark.asyncio
async def test_get_mints_for_block_empty_when_no_match(monkeypatch):
    """Should return an empty list when no mint/burn tx matches the block."""
    monkeypatch.setattr(bf_client, "_fetch_block_tx_hashes", lambda _: {"tx_other"})
    monkeypatch.setattr(
        bf_client,
        "_fetch_recent_mint_burns",
        lambda: [{"tx_hash": "tx_abc", "action": "minted", "amount": 1_000_000}],
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
        lambda: [
            {"tx_hash": "tx_a", "action": "minted", "amount": 10_000_000},
            {"tx_hash": "tx_b", "action": "burned", "amount": 2_000_000},
            {"tx_hash": "tx_z", "action": "minted", "amount": 500_000},
        ],
    )

    events = await bf_client.get_mints_for_block("block-4")

    assert len(events) == 2
    assert events[0] == MintEvent(tx_hash="tx_a", quantity=10_000_000)
    assert events[1] == MintEvent(tx_hash="tx_b", quantity=-2_000_000)


@pytest.mark.asyncio
async def test_get_total_supply(monkeypatch):
    """Should return the total supply from Blockfrost."""
    monkeypatch.setattr(bf_client, "_fetch_total_supply", lambda: 100_000_000)

    supply = await bf_client.get_total_supply()

    assert supply == 100_000_000
