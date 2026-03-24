"""FastAPI app and async webhook handler."""

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException, Request
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential

from .config import get_config
from .db.repository import block_exists, close_connection, get_mints_for_block, get_total_supply, set_db_url
from .db.ssh_tunnel import SSHTunnel, create_tunnel_if_configured
from .logging import get_logger, setup_logging
from .twitter.client import post_tweet
from .twitter.formatter import format_event_tweet
from .webhook_auth import verify_webhook_signature

setup_logging()
logger = get_logger(__name__)

# SSH tunnel instance (if configured)
_ssh_tunnel: SSHTunnel | None = None


def _get_tunneled_url(original_url: str, local_port: int) -> str:
    """Rewrite db_sync_url to route through the local tunnel port."""
    parsed = urlparse(original_url)
    local = f"127.0.0.1:{local_port}"
    if parsed.password:
        netloc = f"{parsed.username}:{parsed.password}@{local}"
    else:
        netloc = f"{parsed.username}@{local}"
    return urlunparse(parsed._replace(netloc=netloc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - startup and shutdown."""
    global _ssh_tunnel

    # Startup
    logger.info("Starting USDM Stream Bot")

    # Start SSH tunnel if configured
    _ssh_tunnel = create_tunnel_if_configured()
    if _ssh_tunnel:
        local_port = _ssh_tunnel.start()
        logger.info(f"SSH tunnel started on port {local_port}")

        # Update DB URL to use local tunnel
        config = get_config()
        tunneled_url = _get_tunneled_url(config.db_sync_url, local_port)
        set_db_url(tunneled_url)

    yield

    # Shutdown
    logger.info("Shutting down USDM Stream Bot")

    if _ssh_tunnel:
        _ssh_tunnel.stop()

    await close_connection()


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

    webhook_type = payload.get("type", "unknown")
    logger.info(f"Received webhook: {webhook_type}")
    logger.debug(f"Webhook payload keys: {list(payload.keys())}")

    # Extract block hash from payload
    block_data = payload.get("payload", {})
    block_hash = block_data.get("hash") if isinstance(block_data, dict) else None

    if not block_hash:
        if isinstance(block_data, dict):
            logger.debug(f"Payload 'payload' keys: {list(block_data.keys())}")
        else:
            logger.debug(f"Payload 'payload' is not a dict: {type(block_data).__name__}")
        logger.warning("No block hash in payload")
        return {"status": "skipped", "reason": "no block hash"}

    logger.info(f"Accepted block {block_hash} for background processing")
    task = asyncio.create_task(_process_block(block_hash), name=f"process-block-{block_hash[:12]}")
    task.add_done_callback(_log_background_task_result)

    return {"status": "accepted", "block_hash": block_hash}


@retry(
    retry=retry_if_result(lambda exists: not exists),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
)
async def _wait_for_block_indexed(block_hash: str) -> bool:
    """Wait until DB-Sync has indexed the block (handles indexing lag)."""
    exists = await block_exists(block_hash)
    if not exists:
        logger.info(f"Block {block_hash} not yet indexed in DB-Sync, retrying...")
    return exists


async def _process_block(block_hash: str) -> None:
    """Process a block for USDM mint/burn events."""
    logger.info(f"Processing block: {block_hash}")

    # Wait for DB-Sync to index the block (handles indexing lag)
    try:
        indexed = await _wait_for_block_indexed(block_hash)
    except Exception:
        logger.exception(f"Failed waiting for block {block_hash} to be indexed")
        raise

    if not indexed:
        logger.warning(f"Block {block_hash} not found in DB-Sync after retries")
        return

    # Get all mint/burn events for this block
    events = await get_mints_for_block(block_hash)

    if not events:
        logger.info(f"No USDM events in block {block_hash}")
        return

    logger.info(f"Found {len(events)} USDM event(s) in block {block_hash}")

    # Get current total supply
    supply = await get_total_supply()
    logger.info(f"Current total supply: {supply}")

    # Process each event independently so one failure doesn't skip the rest
    for event in events:
        try:
            logger.info(f"Processing event: tx={event.tx_hash}, quantity={event.quantity}")

            # Format and post tweet
            tweet_text = format_event_tweet(event, supply)
            tweet_id = await asyncio.to_thread(post_tweet, tweet_text)

            if tweet_id:
                logger.info(f"Posted tweet {tweet_id} for tx {event.tx_hash}")
            else:
                logger.info(f"Tweet not posted for tx {event.tx_hash}")
        except Exception:
            logger.exception(f"Failed to process event tx={event.tx_hash} in block {block_hash}")

    logger.info(f"Finished processing block {block_hash}")


def _log_background_task_result(task: asyncio.Task[None]) -> None:
    """Log background task failures so they are not silently dropped."""
    try:
        task.result()
    except Exception:
        logger.exception(f"Background block processing failed: {task.get_name()}")
