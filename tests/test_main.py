"""Tests for the FastAPI webhook handler."""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient
from tenacity import wait_none

os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret")
os.environ.setdefault("ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("ACCESS_TOKEN_SECRET", "test-access-secret")
os.environ.setdefault("DB_SYNC_URL", "postgresql://user:pass@localhost:5432/db")

import bot.main as main_module
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
    event = MintEvent(tx_hash="abc123", quantity=1_000_000)
    posted_texts: list[str] = []

    async def fake_block_exists(block_hash: str) -> bool:
        return True

    async def fake_get_mints_for_block(block_hash: str) -> list[MintEvent]:
        assert block_hash == "block-123"
        return [event]

    async def fake_get_total_supply() -> int:
        return 50_000_000

    def fake_post_tweet(text: str) -> str:
        posted_texts.append(text)
        return "tweet-1"

    monkeypatch.setattr(main_module, "block_exists", fake_block_exists)
    monkeypatch.setattr(main_module, "get_mints_for_block", fake_get_mints_for_block)
    monkeypatch.setattr(main_module, "get_total_supply", fake_get_total_supply)
    monkeypatch.setattr(main_module, "post_tweet", fake_post_tweet)

    await main_module._process_block("block-123")

    assert len(posted_texts) == 1
    assert "abc123" in posted_texts[0]
    assert "$USDM" in posted_texts[0]


@pytest.mark.asyncio
async def test_process_block_retries_on_empty_result(monkeypatch):
    """Block processing should retry the DB query when it returns empty (DB-Sync lag)."""
    event = MintEvent(tx_hash="abc123", quantity=1_000_000)
    exists_call_count = 0
    posted_texts: list[str] = []

    async def fake_block_exists(block_hash: str) -> bool:
        nonlocal exists_call_count
        exists_call_count += 1
        return exists_call_count >= 3

    async def fake_get_mints_for_block(block_hash: str) -> list[MintEvent]:
        return [event]

    async def fake_get_total_supply() -> int:
        return 50_000_000

    def fake_post_tweet(text: str) -> str:
        posted_texts.append(text)
        return "tweet-1"

    monkeypatch.setattr(main_module, "block_exists", fake_block_exists)
    monkeypatch.setattr(main_module, "get_mints_for_block", fake_get_mints_for_block)
    monkeypatch.setattr(main_module, "get_total_supply", fake_get_total_supply)
    monkeypatch.setattr(main_module, "post_tweet", fake_post_tweet)

    # Speed up retry waits for testing
    original_wait = main_module._wait_for_block_indexed.retry.wait
    main_module._wait_for_block_indexed.retry.wait = wait_none()
    try:
        await main_module._process_block("block-123")
    finally:
        main_module._wait_for_block_indexed.retry.wait = original_wait

    assert exists_call_count == 3
    assert len(posted_texts) == 1


@pytest.mark.asyncio
async def test_process_block_continues_after_tweet_failure(monkeypatch):
    """If posting a tweet fails, remaining events in the block should still be processed."""
    events = [
        MintEvent(tx_hash="fail_tx", quantity=1_000_000),
        MintEvent(tx_hash="ok_tx", quantity=2_000_000),
    ]
    posted_texts: list[str] = []

    async def fake_block_exists(block_hash: str) -> bool:
        return True

    async def fake_get_mints_for_block(block_hash: str) -> list[MintEvent]:
        return events

    async def fake_get_total_supply() -> int:
        return 50_000_000

    def fake_post_tweet(text: str) -> str | None:
        if "fail_tx" in text:
            raise RuntimeError("Twitter API error")
        posted_texts.append(text)
        return "tweet-2"

    monkeypatch.setattr(main_module, "block_exists", fake_block_exists)
    monkeypatch.setattr(main_module, "get_mints_for_block", fake_get_mints_for_block)
    monkeypatch.setattr(main_module, "get_total_supply", fake_get_total_supply)
    monkeypatch.setattr(main_module, "post_tweet", fake_post_tweet)

    # Should NOT raise even though first event fails
    await main_module._process_block("block-456")

    assert len(posted_texts) == 1
    assert "ok_tx" in posted_texts[0]


@pytest.mark.asyncio
async def test_webhook_skips_non_dict_payload(monkeypatch):
    """The webhook should handle a non-dict payload gracefully."""
    monkeypatch.setattr(main_module, "verify_webhook_signature", lambda *_: True)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/", json={"type": "block", "payload": [{"hash": "abc"}]})

        assert response.status_code == 200
        assert response.json()["status"] == "skipped"
