"""Blockfrost webhook authentication via HMAC-SHA256."""

import hashlib
import hmac
import time

from .config import get_config
from .logging import get_logger

logger = get_logger(__name__)

# Maximum age of a webhook request (5 minutes)
MAX_TIMESTAMP_AGE_SECONDS = 300


def verify_webhook_signature(payload: bytes, signature_header: str | None) -> bool:
    """
    Verify Blockfrost webhook signature.

    Args:
        payload: Raw request body bytes
        signature_header: Value of Blockfrost-Signature header

    Returns:
        True if signature is valid, False otherwise
    """
    config = get_config()

    # Skip verification if no auth token configured
    if not config.blockfrost_webhook_auth_token:
        logger.warning("Webhook signature verification skipped - no auth token configured")
        return True

    if not signature_header:
        logger.warning("Missing Blockfrost-Signature header")
        return False

    # Parse signature header: t=<timestamp>,v1=<signature>
    parts = {}
    for part in signature_header.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            parts[key] = value

    timestamp_str = parts.get("t")
    signature = parts.get("v1")

    if not timestamp_str or not signature:
        logger.warning("Invalid Blockfrost-Signature header format")
        return False

    # Check timestamp is recent
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        logger.warning("Invalid timestamp in signature header")
        return False

    current_time = int(time.time())
    if abs(current_time - timestamp) > MAX_TIMESTAMP_AGE_SECONDS:
        logger.warning("Webhook timestamp too old or in future")
        return False

    # Compute expected signature
    signed_payload = f"{timestamp_str}.{payload.decode('utf-8')}"
    expected_signature = hmac.new(
        config.blockfrost_webhook_auth_token.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Compare signatures
    if not hmac.compare_digest(signature, expected_signature):
        logger.warning("Webhook signature mismatch")
        return False

    return True
