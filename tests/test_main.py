"""Tests for the FastAPI webhook handler."""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret")
os.environ.setdefault("ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("ACCESS_TOKEN_SECRET", "test-access-secret")
os.environ.setdefault("BLOCKFROST_PROJECT_ID", "mainnetTEST")

import bot.main as main_module
from bot.config import USDM
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
    """Block processing should query the DB and post a formatted tweet."""
    event = MintEvent(tx_hash="abc123", action="minted", quantity=1_000_000, stablecoin=USDM)
    posted_texts: list[str] = []

    async def fake_get_mints_for_block(block_hash: str) -> list[MintEvent]:
        assert block_hash == "block-123"
        return [event]

    async def fake_get_total_supply(asset_id: str) -> int:
        assert asset_id == event.stablecoin.asset_id
        return 50_000_000

    def fake_post_tweet(text: str) -> str:
        posted_texts.append(text)
        return "tweet-1"

    monkeypatch.setattr(main_module, "get_mints_for_block", fake_get_mints_for_block)
    monkeypatch.setattr(main_module, "get_total_supply", fake_get_total_supply)
    monkeypatch.setattr(main_module, "post_tweet", fake_post_tweet)

    await main_module._process_block("block-123")

    assert len(posted_texts) == 1
    assert "abc123" in posted_texts[0]
    assert "$USDM" in posted_texts[0]
