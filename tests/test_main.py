"""Tests for the FastAPI webhook handler."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret")
os.environ.setdefault("ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("ACCESS_TOKEN_SECRET", "test-access-secret")
os.environ.setdefault("BLOCKFROST_PROJECT_ID", "mainnetTEST")

import bot.main as main_module
from bot.config import USDA, USDCX, USDM, Stablecoin
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


@pytest.mark.asyncio
async def test_mixed_block_fetches_each_needed_supply_once(monkeypatch):
    """Detection returns reportable events; posting retains token/action banners and supply deduplication."""
    events = [
        MintEvent(tx_hash="tiny-usdm-mint", action="minted", quantity=1, stablecoin=USDM),
        MintEvent(tx_hash="tiny-usdm-burn", action="burned", quantity=-1, stablecoin=USDM),
        MintEvent(tx_hash="tiny-usda-mint", action="minted", quantity=1, stablecoin=USDA),
        MintEvent(tx_hash="tiny-usda-burn", action="burned", quantity=-1, stablecoin=USDA),
        MintEvent(tx_hash="large-usdcx-mint", action="minted", quantity=10_000_000_000, stablecoin=USDCX),
        MintEvent(tx_hash="large-usdcx-burn", action="burned", quantity=-10_000_000_001, stablecoin=USDCX),
    ]
    supply = AsyncMock(return_value=50_000_000_000)
    post = Mock(return_value="tweet-1")
    monkeypatch.setattr(main_module, "get_mints_for_block", AsyncMock(return_value=events))
    monkeypatch.setattr(main_module, "get_total_supply", supply)
    monkeypatch.setattr(main_module, "post_tweet", post)

    await main_module._process_block("block-mixed")

    expected_assets = {event.stablecoin.asset_id for event in events}
    assert {call.args[0] for call in supply.await_args_list} == expected_assets
    assert supply.await_count == len(expected_assets)
    assert post.call_count == len(events)
    for call, event in zip(post.call_args_list, events, strict=True):
        assert event.tx_hash in call.args[0]
        assert call.kwargs["image_path"].name == f"{event.stablecoin.ticker.lower()}-{event.action}.png"
        assert call.kwargs["image_alt_text"] == f"{event.stablecoin.ticker} — {event.action.upper()}"


@pytest.mark.asyncio
async def test_replayed_pre_activation_block_never_posts_new_tokens(monkeypatch):
    """Even amounts meeting the new minimum cannot bypass the original rollout guard."""
    import bot.blockfrost_client as blockfrost
    from bot.config import NEW_STABLECOINS_ACTIVATED_AT

    monkeypatch.setattr(blockfrost, "STABLECOINS", (USDCX, USDA))
    monkeypatch.setattr(blockfrost, "_fetch_block_tx_hashes", lambda _: {"old-mint", "old-burn"})
    monkeypatch.setattr(
        blockfrost,
        "_fetch_block_metadata",
        lambda _: SimpleNamespace(time=NEW_STABLECOINS_ACTIVATED_AT - 1, height=100),
    )
    monkeypatch.setattr(
        blockfrost,
        "_fetch_recent_mint_burns",
        lambda _asset_id, _block: [
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


@pytest.mark.parametrize("failure", ["formatter", "missing-banner", "upload", "missing-post-id"])
@pytest.mark.asyncio
async def test_one_bad_event_does_not_drop_remaining_events(monkeypatch, caplog, failure):
    """The webhook is already acked, so errors must be contained within one event."""
    caplog.set_level("INFO", logger="bot.main")
    coin = Stablecoin(ticker="NEW", policy_id="new-policy", asset_id="new-asset", decimals=6)
    bad = MintEvent(
        tx_hash="bad-first",
        action="minted",
        quantity=-1 if failure == "formatter" else 1,
        stablecoin=coin if failure == "missing-banner" else USDM,
    )
    good = [
        MintEvent(tx_hash="good-mint", action="minted", quantity=1, stablecoin=USDM),
        MintEvent(tx_hash="good-burn", action="burned", quantity=-1, stablecoin=USDM),
    ]
    post = Mock(return_value="tweet-id")
    if failure == "upload":
        post.side_effect = [RuntimeError("media upload failed"), "tweet-mint", "tweet-burn"]
    elif failure == "missing-post-id":
        post.side_effect = [None, "tweet-mint", "tweet-burn"]
    monkeypatch.setattr(main_module, "get_mints_for_block", AsyncMock(return_value=[bad, *good]))
    monkeypatch.setattr(main_module, "get_total_supply", AsyncMock(return_value=50_000_000))
    monkeypatch.setattr(main_module, "post_tweet", post)

    await main_module._process_block("block-with-bad-event")

    assert "good-mint" in post.call_args_list[-2].args[0]
    assert "good-burn" in post.call_args_list[-1].args[0]
    assert post.call_count == (3 if failure in ("upload", "missing-post-id") else 2)
    assert "bad-first" in caplog.text
    if failure == "missing-post-id":
        assert "No post confirmed" in caplog.text


@pytest.mark.asyncio
async def test_missing_banner_in_dry_run_does_not_stop_valid_events(monkeypatch, caplog):
    from bot.twitter import client as twitter

    caplog.set_level("INFO", logger="bot.twitter.client")
    coin = Stablecoin(ticker="NEW", policy_id="new-policy", asset_id="new-asset", decimals=6)
    events = [
        MintEvent(tx_hash="missing-banner", action="minted", quantity=1, stablecoin=coin),
        MintEvent(tx_hash="dry-run-mint", action="minted", quantity=1, stablecoin=USDM),
        MintEvent(tx_hash="dry-run-burn", action="burned", quantity=-1, stablecoin=USDA),
    ]
    post = Mock(wraps=twitter.post_tweet)
    client = Mock()
    monkeypatch.setattr(main_module, "get_mints_for_block", AsyncMock(return_value=events))
    monkeypatch.setattr(main_module, "get_total_supply", AsyncMock(return_value=50_000_000))
    monkeypatch.setattr(main_module, "post_tweet", post)
    monkeypatch.setattr(twitter, "get_config", lambda: SimpleNamespace(tweet_posting_enabled=False))
    monkeypatch.setattr(twitter, "_get_client", client)

    await main_module._process_block("block-dry-run")

    assert post.call_count == 2
    assert "dry-run-mint" in caplog.text
    assert "dry-run-burn" in caplog.text
    client.assert_not_called()


@pytest.mark.asyncio
async def test_one_supply_failure_does_not_drop_other_tokens(monkeypatch, caplog):
    events = [
        MintEvent(tx_hash="bad-usda", action="minted", quantity=1, stablecoin=USDA),
        MintEvent(tx_hash="good-usdm", action="minted", quantity=1, stablecoin=USDM),
        MintEvent(tx_hash="good-usdcx", action="burned", quantity=-10_000_000_000, stablecoin=USDCX),
        MintEvent(tx_hash="also-bad-usda", action="burned", quantity=-1, stablecoin=USDA),
    ]

    async def supply(asset_id):
        if asset_id == USDA.asset_id:
            raise RuntimeError("Blockfrost unavailable for USDA")
        return 50_000_000

    post = Mock(return_value="tweet-id")
    fetch_supply = AsyncMock(side_effect=supply)
    monkeypatch.setattr(main_module, "get_mints_for_block", AsyncMock(return_value=events))
    monkeypatch.setattr(main_module, "get_total_supply", fetch_supply)
    monkeypatch.setattr(main_module, "post_tweet", post)

    await main_module._process_block("block-supply-error")

    assert post.call_count == 2
    assert "good-usdm" in post.call_args_list[0].args[0]
    assert "good-usdcx" in post.call_args_list[1].args[0]
    assert fetch_supply.await_count == 3
    assert "Skipping USDA" in caplog.text


@pytest.mark.asyncio
async def test_supply_cancellation_propagates(monkeypatch):
    event = MintEvent(tx_hash="tx", action="minted", quantity=1, stablecoin=USDM)
    monkeypatch.setattr(main_module, "get_mints_for_block", AsyncMock(return_value=[event]))
    monkeypatch.setattr(main_module, "get_total_supply", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await main_module._process_block("cancelled-block")


@pytest.mark.asyncio
async def test_lifespan_shutdown_drops_queued_blockfrost_reads(monkeypatch):
    """Queued reads belong to blocks that will not be posted once the process is gone."""
    shutdown = Mock()
    monkeypatch.setattr(main_module, "shutdown_reads", shutdown)

    async with main_module.app.router.lifespan_context(main_module.app):
        shutdown.assert_not_called()

    shutdown.assert_called_once_with()
