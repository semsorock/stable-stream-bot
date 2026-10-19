"""XDK wrapper for posting tweets with feature flag gate."""

from base64 import b64encode
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import Lock
from time import monotonic

from requests import HTTPError
from xdk import Client
from xdk.media.models import CreateMetadataRequest, UploadRequest
from xdk.oauth1_auth import OAuth1
from xdk.posts.models import CreateRequest, CreateRequestMedia

from ..config import get_config
from ..logging import get_logger

logger = get_logger(__name__)

_client: Client | None = None
_client_lock = Lock()
_media_cache_lock = Lock()
_MEDIA_EXPIRY_MARGIN_SECONDS = 30
_MAX_ERROR_BODY_LENGTH = 2_000


@dataclass(frozen=True)
class _CachedMedia:
    media_id: str
    expires_at: float


_media_cache: dict[tuple[Path, str | None], _CachedMedia] = {}


def _get_client() -> Client:
    """Get or create the XDK client."""
    global _client
    with _client_lock:
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


def _response_field(response, field: str):
    return response.get(field) if isinstance(response, dict) else getattr(response, field, None)


def _extract_post_id(response) -> str | None:
    """Extract an ID from an XDK post or media response."""
    value = _response_field(_response_field(response, "data"), "id")
    return str(value) if value else None


def _upload_banner(client: Client, image_path: Path, alt_text: str | None) -> str:
    """Reuse static media until its server-provided expiry, including upload time."""
    image_path = image_path.resolve()
    cache_key = (image_path, alt_text)
    with _media_cache_lock:
        upload_started = monotonic()
        cached = _media_cache.get(cache_key)
        if cached is not None and upload_started < cached.expires_at:
            return cached.media_id
        _media_cache.pop(cache_key, None)

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
        if _response_field(response, "errors"):
            raise RuntimeError("X rejected banner upload")
        if alt_text:
            metadata = client.media.create_metadata(
                CreateMetadataRequest(id=media_id, metadata={"alt_text": {"text": alt_text}})
            )
            if _response_field(metadata, "errors"):
                raise RuntimeError("X rejected banner alt text")

        lifetime = _response_field(_response_field(response, "data"), "expires_after_secs")
        if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool):
            try:
                expires_at = upload_started + float(lifetime) - _MEDIA_EXPIRY_MARGIN_SECONDS
            except OverflowError:
                return media_id
            if isfinite(expires_at) and monotonic() < expires_at:
                _media_cache[cache_key] = _CachedMedia(media_id=media_id, expires_at=expires_at)
        return media_id


def post_tweet(text: str, *, image_path: Path | None = None, image_alt_text: str | None = None) -> str | None:
    """
    Post a tweet if TWEET_POSTING_ENABLED is true.

    Args:
        text: Tweet content
        image_path: Optional bundled PNG to upload and attach
        image_alt_text: Accessible description of the banner

    Returns:
        Tweet ID if confirmed, None if posting is disabled or its outcome is unknown
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
            logger.warning("X post outcome UNKNOWN: no post ID returned; not retried to avoid duplicate posts")
            return None
        logger.info(f"Tweet posted successfully: {tweet_id}")
        return tweet_id
    except HTTPError as exc:
        response = exc.response
        if response is not None:
            logger.exception(
                "Failed to post tweet (X HTTP %s): %s",
                response.status_code,
                response.text[:_MAX_ERROR_BODY_LENGTH],
            )
        else:
            logger.exception("Failed to post tweet (X HTTP error without a response body)")
        raise
    except Exception:
        logger.exception("Failed to post tweet")
        raise
