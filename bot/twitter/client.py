"""XDK wrapper for posting tweets with feature flag gate."""

from xdk import XClient
from xdk.auth import OAuth1UserAuth

from ..config import get_config
from ..logging import get_logger

logger = get_logger(__name__)

_client: XClient | None = None


def _get_client() -> XClient:
    """Get or create the XDK client."""
    global _client
    if _client is None:
        config = get_config()
        auth = OAuth1UserAuth(
            consumer_key=config.api_key,
            consumer_secret=config.api_secret_key,
            access_token=config.access_token,
            access_token_secret=config.access_token_secret,
        )
        _client = XClient(auth=auth)
    return _client


def _extract_post_id(response) -> str | None:
    """Extract tweet ID from XDK response."""
    try:
        # XDK returns response with data attribute containing id
        if hasattr(response, "data") and hasattr(response.data, "id"):
            return str(response.data.id)
        # Fallback for dict-like response
        if isinstance(response, dict) and "data" in response:
            return str(response["data"].get("id"))
    except Exception as e:
        logger.warning(f"Failed to extract post ID: {e}")
    return None


def post_tweet(text: str) -> str | None:
    """
    Post a tweet if TWEET_POSTING_ENABLED is true.

    Args:
        text: Tweet content

    Returns:
        Tweet ID if posted successfully, None otherwise
    """
    config = get_config()

    logger.info(f"Tweet content ({len(text)} chars):\n{text}")

    if not config.tweet_posting_enabled:
        logger.info("Tweet posting disabled - skipping")
        return None

    try:
        client = _get_client()
        response = client.posts.create(text=text)
        tweet_id = _extract_post_id(response)
        logger.info(f"Tweet posted successfully: {tweet_id}")
        return tweet_id
    except Exception as e:
        logger.error(f"Failed to post tweet: {e}")
        return None
