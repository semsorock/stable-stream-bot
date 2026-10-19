"""Verify X media requests using the real SDK and a mocked HTTP transport."""

from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import HTTPError, Response
from xdk import Client
from xdk.oauth1_auth import OAuth1

from bot.twitter import client as twitter


@pytest.fixture(autouse=True)
def isolated_media_cache(monkeypatch):
    monkeypatch.setattr(twitter, "_media_cache", {})


@pytest.fixture
def sdk(monkeypatch):
    client = Client(
        auth=OAuth1(
            api_key="test-key",
            api_secret="test-secret",
            callback="oob",
            access_token="test-access",
            access_token_secret="test-access-secret",
        )
    )
    client.session = Mock()
    monkeypatch.setattr(twitter, "_get_client", lambda: client)
    monkeypatch.setattr(twitter, "get_config", lambda: SimpleNamespace(tweet_posting_enabled=True))
    return client


@pytest.fixture
def banner(tmp_path):
    image = tmp_path / "banner.png"
    image.write_bytes(b"test-png-bytes")
    return image


def response(data):
    result = Mock()
    result.json.return_value = data
    return result


def test_uploads_png_sets_alt_text_and_attaches_media(sdk, tmp_path):
    image = tmp_path / "banner.png"
    image.write_bytes(b"test-png-bytes")
    sdk.session.post.side_effect = [
        response({"data": {"id": "media-123"}}),
        response({"data": {"id": "media-123"}}),
        response({"data": {"id": "tweet-123"}}),
    ]

    tweet_id = twitter.post_tweet("mint text", image_path=image, image_alt_text="USDCx — MINTED")

    assert tweet_id == "tweet-123"
    upload, metadata, post = sdk.session.post.call_args_list
    assert upload.args[0] == "https://api.x.com/2/media/upload"
    body = upload.kwargs["json"]
    assert b64decode(body["media"]) == image.read_bytes()
    assert body["media_category"] == "tweet_image"
    assert body["media_type"] == "image/png"
    assert upload.kwargs["headers"]["Authorization"].startswith("OAuth ")
    assert metadata.args[0] == "https://api.x.com/2/media/metadata"
    assert metadata.kwargs["json"] == {
        "id": "media-123",
        "metadata": {"alt_text": {"text": "USDCx — MINTED"}},
    }
    assert post.args[0] == "https://api.x.com/2/tweets"
    assert post.kwargs["json"] == {"text": "mint text", "media": {"media_ids": ["media-123"]}}


def test_posting_disabled_does_not_upload_or_read_image(monkeypatch, tmp_path):
    monkeypatch.setattr(twitter, "get_config", lambda: SimpleNamespace(tweet_posting_enabled=False))
    get_client = Mock()
    monkeypatch.setattr(twitter, "_get_client", get_client)

    assert twitter.post_tweet("text", image_path=tmp_path / "missing.png") is None

    get_client.assert_not_called()


@pytest.mark.parametrize("payload", [{"data": {}}, {"errors": [{"title": "upload failed"}]}])
def test_missing_media_id_prevents_post(sdk, tmp_path, payload):
    image = tmp_path / "banner.png"
    image.write_bytes(b"test-png-bytes")
    sdk.session.post.return_value = response(payload)

    with pytest.raises(RuntimeError, match="no media ID"):
        twitter.post_tweet("text", image_path=image)

    assert sdk.session.post.call_count == 1


def test_upload_failure_prevents_post(sdk, tmp_path):
    image = tmp_path / "banner.png"
    image.write_bytes(b"test-png-bytes")
    sdk.session.post.side_effect = RuntimeError("upload failed")

    with pytest.raises(RuntimeError, match="upload failed"):
        twitter.post_tweet("text", image_path=image)

    assert sdk.session.post.call_count == 1


def test_alt_text_failure_prevents_post(sdk, tmp_path):
    image = tmp_path / "banner.png"
    image.write_bytes(b"test-png-bytes")
    sdk.session.post.side_effect = [
        response({"data": {"id": "media-123"}}),
        response({"errors": [{"title": "metadata failed"}]}),
    ]

    with pytest.raises(RuntimeError, match="alt text"):
        twitter.post_tweet("text", image_path=image, image_alt_text="USDA — BURNED")

    assert sdk.session.post.call_count == 2


def test_missing_image_prevents_any_api_call(sdk, tmp_path):
    with pytest.raises(FileNotFoundError):
        twitter.post_tweet("text", image_path=tmp_path / "missing.png")

    sdk.session.post.assert_not_called()


def test_missing_post_id_has_unknown_outcome_and_is_not_retried(sdk, caplog):
    sdk.session.post.return_value = response({"data": {}, "errors": [{"title": "partial response"}]})

    assert twitter.post_tweet("text") is None

    assert sdk.session.post.call_count == 1
    assert "outcome UNKNOWN" in caplog.text
    assert "not retried" in caplog.text


def test_cached_media_skips_upload_and_metadata(sdk, banner):
    sdk.session.post.side_effect = [
        response({"data": {"id": "media-123", "expires_after_secs": 3600}}),
        response({"data": {"id": "media-123"}}),
        response({"data": {"id": "tweet-123"}}),
        response({"data": {"id": "tweet-456"}}),
    ]

    assert twitter.post_tweet("first", image_path=banner, image_alt_text="minted") == "tweet-123"
    assert twitter.post_tweet("second", image_path=banner, image_alt_text="minted") == "tweet-456"

    assert [call.args[0] for call in sdk.session.post.call_args_list] == [
        "https://api.x.com/2/media/upload",
        "https://api.x.com/2/media/metadata",
        "https://api.x.com/2/tweets",
        "https://api.x.com/2/tweets",
    ]
    assert sdk.session.post.call_args.kwargs["json"]["media"]["media_ids"] == ["media-123"]


@pytest.mark.parametrize("clock_times", [(100, 101, 3671, 3672), (100, 3701, 3702, 3703)])
def test_expired_media_is_refreshed_including_time_spent_uploading(sdk, banner, monkeypatch, clock_times):
    monkeypatch.setattr(twitter, "monotonic", Mock(side_effect=clock_times))
    sdk.session.post.side_effect = [
        response({"data": {"id": "media-123", "expires_after_secs": 3600}}),
        response({"data": {"id": "media-123"}}),
        response({"data": {"id": "tweet-123"}}),
        response({"data": {"id": "media-456", "expires_after_secs": 3600}}),
        response({"data": {"id": "media-456"}}),
        response({"data": {"id": "tweet-456"}}),
    ]

    assert twitter.post_tweet("first", image_path=banner, image_alt_text="minted") == "tweet-123"
    assert twitter.post_tweet("second", image_path=banner, image_alt_text="minted") == "tweet-456"

    assert sdk.session.post.call_count == 6
    assert sdk.session.post.call_args.kwargs["json"]["media"]["media_ids"] == ["media-456"]


@pytest.mark.parametrize("lifetime", [None, 0, -1, 30, True, "3600", float("nan"), float("inf"), 10**1000])
def test_missing_or_invalid_media_expiry_is_not_cached(banner, lifetime):
    upload = Mock(return_value={"data": {"id": "media-123", "expires_after_secs": lifetime}})
    client = SimpleNamespace(media=SimpleNamespace(upload=upload))

    assert twitter._upload_banner(client, banner, None) == "media-123"
    assert twitter._upload_banner(client, banner, None) == "media-123"

    assert upload.call_count == 2


def test_metadata_failure_is_not_cached(sdk, banner):
    sdk.session.post.side_effect = [
        response({"data": {"id": "media-123", "expires_after_secs": 3600}}),
        response({"errors": [{"title": "metadata failed"}]}),
        response({"data": {"id": "media-456", "expires_after_secs": 3600}}),
        response({"data": {"id": "media-456"}}),
        response({"data": {"id": "tweet-456"}}),
    ]

    with pytest.raises(RuntimeError, match="alt text"):
        twitter.post_tweet("first", image_path=banner, image_alt_text="minted")
    assert twitter.post_tweet("second", image_path=banner, image_alt_text="minted") == "tweet-456"

    assert sdk.session.post.call_count == 5
    assert sdk.session.post.call_args.kwargs["json"]["media"]["media_ids"] == ["media-456"]


def test_upload_partial_error_is_not_cached_or_posted(sdk, banner):
    sdk.session.post.side_effect = [
        response({"data": {"id": "media-123", "expires_after_secs": 3600}, "errors": [{"title": "rejected"}]}),
        response({"data": {"id": "media-456", "expires_after_secs": 3600}}),
        response({"data": {"id": "tweet-456"}}),
    ]

    with pytest.raises(RuntimeError, match="rejected banner upload"):
        twitter.post_tweet("first", image_path=banner)
    assert twitter.post_tweet("second", image_path=banner) == "tweet-456"

    assert sdk.session.post.call_count == 3


@pytest.mark.parametrize("change", ["path", "alt_text"])
def test_media_cache_distinguishes_banner_path_and_alt_text(sdk, banner, change):
    second_image = banner.with_name("other.png")
    second_image.write_bytes(b"other-png-bytes")
    sdk.session.post.side_effect = [
        response({"data": {"id": "media-123", "expires_after_secs": 3600}}),
        response({"data": {"id": "media-123"}}),
        response({"data": {"id": "tweet-123"}}),
        response({"data": {"id": "media-456", "expires_after_secs": 3600}}),
        response({"data": {"id": "media-456"}}),
        response({"data": {"id": "tweet-456"}}),
    ]

    twitter.post_tweet("first", image_path=banner, image_alt_text="minted")
    twitter.post_tweet(
        "second",
        image_path=second_image if change == "path" else banner,
        image_alt_text="burned" if change == "alt_text" else "minted",
    )

    assert sdk.session.post.call_count == 6


def test_cache_serializes_concurrent_banner_uploads(banner):
    upload = Mock(return_value={"data": {"id": "media-123", "expires_after_secs": 3600}})
    metadata = Mock(return_value={})
    client = SimpleNamespace(media=SimpleNamespace(upload=upload, create_metadata=metadata))

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _: twitter._upload_banner(client, banner, "minted"), range(8)))

    assert ids == ["media-123"] * 8
    upload.assert_called_once()
    metadata.assert_called_once()


@pytest.mark.parametrize("endpoint", ["upload", "metadata"])
def test_http_error_logs_bounded_x_error_body(sdk, banner, caplog, endpoint):
    failed_response = Response()
    failed_response.status_code = 403
    failed_response._content = ('{"detail":"media.write permission missing"}' + "x" * 3000 + "SECRET-TAIL").encode()
    failure = response({})
    failure.raise_for_status.side_effect = HTTPError("403 Client Error", response=failed_response)
    sdk.session.post.side_effect = (
        [failure]
        if endpoint == "upload"
        else [response({"data": {"id": "media-123", "expires_after_secs": 3600}}), failure]
    )

    with pytest.raises(HTTPError):
        twitter.post_tweet("text", image_path=banner, image_alt_text="minted")

    assert "X HTTP 403" in caplog.text
    assert "media.write permission missing" in caplog.text
    assert "SECRET-TAIL" not in caplog.text
    assert not twitter._media_cache
