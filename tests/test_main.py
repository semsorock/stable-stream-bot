"""Tests for the FastAPI webhook handler."""

import asyncio
import os
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret")
os.environ.setdefault("ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("ACCESS_TOKEN_SECRET", "test-access-secret")
os.environ.setdefault("BLOCKFROST_PROJECT_ID", "mainnetTEST")

import bot.main as main_module
from bot.config import USDA, USDCX, USDM
from bot.models import MintEvent


@pytest.mark.asyncio
async def test_webhook_accepts_block_and_processes_in_background(monkeypatch):
    """The webhook should acknowledge quickly and schedule work in the background."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_process_block(block_hash: str) -> None:
        assert block_hash == "block-123"
        started.set()
        await release.wait()

    monkeypatch.setattr(main_module, "verify_webhook_signature", lambda *_: True)
    monkeypatch.setattr(main_module, "_process_block", fake_process_block)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/", json={"type": "block", "payload": {"hash": "block-123"}})

        assert response.status_code == 200
        assert response.json() == {"status": "accepted", "block_hash": "block-123"}

        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_process_block_posts_tweets(monkeypatch):
    """Block processing should query supply and post a formatted tweet with its banner."""
    event = MintEvent(tx_hash="abc123", action="minted", quantity=1_000_000, stablecoin=USDM)
    posted_texts: list[str] = []

    async def fake_get_mints_for_block(block_hash: str) -> list[MintEvent]:
        assert block_hash == "block-123"
        return [event]

    async def fake_get_total_supply(asset_id: str) -> int:
        assert asset_id == event.stablecoin.asset_id
        return 50_000_000

    def fake_post_tweet(text: str, *, image_path, image_alt_text) -> str:
        assert image_path.name == "usdm-minted.png"
        assert image_alt_text == "USDM — MINTED"
        posted_texts.append(text)
        return "tweet-1"

    monkeypatch.setattr(main_module, "get_mints_for_block", fake_get_mints_for_block)
    monkeypatch.setattr(main_module, "get_total_supply", fake_get_total_supply)
    monkeypatch.setattr(main_module, "post_tweet", fake_post_tweet)

    await main_module._process_block("block-123")

    assert len(posted_texts) == 1
    assert "abc123" in posted_texts[0]
    assert "$USDM" in posted_texts[0]


@pytest.mark.parametrize("action,sign", [("minted", 1), ("burned", -1)])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["one-micro-below", "exactly-10k", "one-micro-above"])
@pytest.mark.asyncio
async def test_usdcx_posting_minimum_is_inclusive(monkeypatch, action, sign, offset):
    """Filter signed raw amounts before rounding, supply requests, or posting."""
    event = MintEvent(tx_hash="tx-usdcx", action=action, quantity=sign * (10_000_000_000 + offset), stablecoin=USDCX)
    supply = AsyncMock(return_value=50_000_000_000)
    post = Mock(return_value="tweet-1")
    monkeypatch.setattr(main_module, "get_mints_for_block", AsyncMock(return_value=[event]))
    monkeypatch.setattr(main_module, "get_total_supply", supply)
    monkeypatch.setattr(main_module, "post_tweet", post)

    await main_module._process_block("block-threshold")

    if offset < 0:
        supply.assert_not_awaited()
        post.assert_not_called()
    else:
        supply.assert_awaited_once_with(USDCX.asset_id)
        post.assert_called_once()
        assert "tx-usdcx" in post.call_args.args[0]
        assert post.call_args.kwargs["image_path"].name == f"usdcx-{action}.png"
        assert post.call_args.kwargs["image_alt_text"] == f"USDCx — {action.upper()}"


@pytest.mark.parametrize("include_qualifying_usdcx", [False, True])
@pytest.mark.asyncio
async def test_mixed_block_filters_only_small_usdcx_and_fetches_each_needed_supply_once(
    monkeypatch, include_qualifying_usdcx
):
    """USDM/USDA keep even tiny events; only posted tokens need supply and media."""
    filtered = MintEvent(tx_hash="small-usdcx", action="minted", quantity=9_999_999_999, stablecoin=USDCX)
    expected = [
        MintEvent(tx_hash="tiny-usdm-mint", action="minted", quantity=1, stablecoin=USDM),
        MintEvent(tx_hash="tiny-usdm-burn", action="burned", quantity=-1, stablecoin=USDM),
        MintEvent(tx_hash="tiny-usda-mint", action="minted", quantity=1, stablecoin=USDA),
        MintEvent(tx_hash="tiny-usda-burn", action="burned", quantity=-1, stablecoin=USDA),
    ]
    if include_qualifying_usdcx:
        expected.extend(
            [
                MintEvent(tx_hash="large-usdcx-mint", action="minted", quantity=10_000_000_000, stablecoin=USDCX),
                MintEvent(tx_hash="large-usdcx-burn", action="burned", quantity=-10_000_000_001, stablecoin=USDCX),
            ]
        )
    supply = AsyncMock(return_value=50_000_000_000)
    post = Mock(return_value="tweet-1")
    monkeypatch.setattr(main_module, "get_mints_for_block", AsyncMock(return_value=[filtered, *expected]))
    monkeypatch.setattr(main_module, "get_total_supply", supply)
    monkeypatch.setattr(main_module, "post_tweet", post)

    await main_module._process_block("block-mixed")

    expected_assets = {event.stablecoin.asset_id for event in expected}
    assert {call.args[0] for call in supply.await_args_list} == expected_assets
    assert supply.await_count == len(expected_assets)
    assert post.call_count == len(expected)
    for call, event in zip(post.call_args_list, expected, strict=True):
        assert event.tx_hash in call.args[0]
        assert "small-usdcx" not in call.args[0]
        assert call.kwargs["image_path"].name == f"{event.stablecoin.ticker.lower()}-{event.action}.png"
        assert call.kwargs["image_alt_text"] == f"{event.stablecoin.ticker} — {event.action.upper()}"


@pytest.mark.asyncio
async def test_replayed_pre_activation_block_never_posts_new_tokens(monkeypatch):
    """Even amounts meeting the new minimum cannot bypass the original rollout guard."""
    import bot.blockfrost_client as blockfrost
    from bot.config import NEW_STABLECOINS_ACTIVATED_AT

    monkeypatch.setattr(blockfrost, "STABLECOINS", (USDCX, USDA))
    monkeypatch.setattr(blockfrost, "_fetch_block_tx_hashes", lambda _: {"old-mint", "old-burn"})
    monkeypatch.setattr(blockfrost, "_fetch_block_timestamp", lambda _: NEW_STABLECOINS_ACTIVATED_AT - 1)
    monkeypatch.setattr(
        blockfrost,
        "_fetch_recent_mint_burns",
        lambda _: [
            {"tx_hash": "old-mint", "action": "minted", "amount": 10_000_000_000},
            {"tx_hash": "old-burn", "action": "burned", "amount": -10_000_000_000},
        ],
    )
    supply = AsyncMock()
    post = Mock()
    monkeypatch.setattr(main_module, "get_total_supply", supply)
    monkeypatch.setattr(main_module, "post_tweet", post)

    await main_module._process_block("historical-block")
    await main_module._process_block("historical-block")

    supply.assert_not_awaited()
    post.assert_not_called()
