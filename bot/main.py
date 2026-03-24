"""FastAPI app and async webhook handler."""

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .blockfrost_client import get_mints_for_block, get_total_supply
from .logging import get_logger, setup_logging
from .twitter.client import post_tweet
from .twitter.formatter import format_event_tweet
from .webhook_auth import verify_webhook_signature

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - startup and shutdown."""
    logger.info("Starting USDM Stream Bot")
    yield
    logger.info("Shutting down USDM Stream Bot")


app = FastAPI(title="USDM Stream Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/")
async def webhook(request: Request):
    """
    Handle Blockfrost block webhook.

    Blockfrost sends a POST request with block data when a new block is added.
    See: https://blockfrost.dev/start-building/webhooks/webhooks-events#block
    """
    # Read raw body for signature verification
    body = await request.body()

    # Verify webhook signature
    signature = request.headers.get("Blockfrost-Signature")
    if not verify_webhook_signature(body, signature):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse JSON payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"Received webhook: {payload.get('type', 'unknown')}")

    # Extract block hash from payload
    block_data = payload.get("payload", {})
    block_hash = block_data.get("hash")

    if not block_hash:
        logger.warning("No block hash in payload")
        return {"status": "skipped", "reason": "no block hash"}

    logger.info(f"Accepted block {block_hash} for background processing")
    task = asyncio.create_task(_process_block(block_hash), name=f"process-block-{block_hash[:12]}")
    task.add_done_callback(_log_background_task_result)

    return {"status": "accepted", "block_hash": block_hash}


async def _process_block(block_hash: str) -> None:
    """Process a block for USDM mint/burn events."""
    logger.info(f"Processing block: {block_hash}")

    # Get all mint/burn events for this block
    events = await get_mints_for_block(block_hash)

    if not events:
        logger.info(f"No USDM events in block {block_hash}")
        return

    logger.info(f"Found {len(events)} USDM event(s) in block {block_hash}")

    # Get current total supply
    supply = await get_total_supply()
    logger.info(f"Current total supply: {supply}")

    # Process each event
    for event in events:
        logger.info(f"Processing event: tx={event.tx_hash}, quantity={event.quantity}")

        # Format and post tweet
        tweet_text = format_event_tweet(event, supply)
        tweet_id = await asyncio.to_thread(post_tweet, tweet_text)

        if tweet_id:
            logger.info(f"Posted tweet {tweet_id} for tx {event.tx_hash}")
        else:
            logger.info(f"Tweet not posted for tx {event.tx_hash}")


def _log_background_task_result(task: asyncio.Task[None]) -> None:
    """Log background task failures so they are not silently dropped."""
    try:
        task.result()
    except Exception:
        logger.exception("Background block processing failed")
