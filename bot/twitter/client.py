"""XDK wrapper for posting tweets with feature flag gate."""

from base64 import b64encode
from pathlib import Path

from xdk import Client
from xdk.media.models import CreateMetadataRequest, UploadRequest
from xdk.oauth1_auth import OAuth1
from xdk.posts.models import CreateRequest, CreateRequestMedia

from ..config import get_config
from ..logging import get_logger

logger = get_logger(__name__)

_client: Client | None = None


def _get_client() -> Client:
    """Get or create the XDK client."""
    global _client
    if _client is None:
        config = get_config()
        oauth1 = OAuth1(
            api_key=config.api_key,
            api_secret=config.api_secret_key,
            callback="oob",
            access_token=config.access_token,
            access_token_secret=config.access_token_secret,
        )
        _client = Client(auth=oauth1)
    return _client


def _extract_post_id(response) -> str | None:
    """Extract an ID from an XDK post or media response."""
    data = response.get("data") if isinstance(response, dict) else getattr(response, "data", None)
    value = data.get("id") if isinstance(data, dict) else getattr(data, "id", None)
    return str(value) if value else None


def _upload_banner(client: Client, image_path: Path, alt_text: str | None) -> str:
    """Upload a PNG using the existing OAuth credentials before creating its post."""
    response = client.media.upload(
        UploadRequest(
            media=b64encode(image_path.read_bytes()).decode("ascii"),
            media_category="tweet_image",
            media_type="image/png",
        )
    )
    media_id = _extract_post_id(response)
    if not media_id:
        raise RuntimeError("X media upload returned no media ID")
    if alt_text:
        metadata = client.media.create_metadata(
            CreateMetadataRequest(id=media_id, metadata={"alt_text": {"text": alt_text}})
        )
        if getattr(metadata, "errors", None):
            raise RuntimeError("X rejected banner alt text")
    return media_id


def post_tweet(text: str, *, image_path: Path | None = None, image_alt_text: str | None = None) -> str | None:
    """
    Post a tweet if TWEET_POSTING_ENABLED is true.

    Args:
        text: Tweet content
        image_path: Optional bundled PNG to upload and attach
        image_alt_text: Accessible description of the banner

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
        media = None
        if image_path is not None:
            media_id = _upload_banner(client, image_path, image_alt_text)
            media = CreateRequestMedia(media_ids=[media_id])
        response = client.posts.create(CreateRequest(text=text, media=media))
        tweet_id = _extract_post_id(response)
        if not tweet_id:
            raise RuntimeError("X post creation returned no post ID")
        logger.info(f"Tweet posted successfully: {tweet_id}")
        return tweet_id
    except Exception:
        logger.exception("Failed to post tweet")
        raise
