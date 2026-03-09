"""FastAPI app and async webhook handler."""

from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException, Request

from .config import get_config
from .db.repository import close_connection, get_mints_for_block, get_total_supply, set_db_url
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
    await close_connection()

    if _ssh_tunnel:
        _ssh_tunnel.stop()


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
        payload = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"Received webhook: {payload.get('type', 'unknown')}")

    # Extract block hash from payload
    block_data = payload.get("payload", {})
    block_hash = block_data.get("hash")

    if not block_hash:
        logger.warning("No block hash in payload")
        return {"status": "skipped", "reason": "no block hash"}

    # Process the block
    try:
        await _process_block(block_hash)
    except Exception as e:
        logger.error(f"Error processing block {block_hash}: {e}")
        # Return 200 to acknowledge receipt - Blockfrost will retry on 5xx
        return {"status": "error", "message": str(e)}

    return {"status": "processed", "block_hash": block_hash}


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
        tweet_id = post_tweet(tweet_text)

        if tweet_id:
            logger.info(f"Posted tweet {tweet_id} for tx {event.tx_hash}")
        else:
            logger.info(f"Tweet not posted for tx {event.tx_hash}")
