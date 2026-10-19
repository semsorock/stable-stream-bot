"""Verify X media requests using the real SDK and a mocked HTTP transport."""

from base64 import b64decode
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from xdk import Client
from xdk.oauth1_auth import OAuth1

from bot.twitter import client as twitter


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


def test_missing_post_id_is_an_error(sdk):
    sdk.session.post.return_value = response({"data": {}})

    with pytest.raises(RuntimeError, match="no post ID"):
        twitter.post_tweet("text")
