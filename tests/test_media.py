"""Bundled images must match every supported token and event action."""

import struct

import pytest

from bot.config import STABLECOINS, Stablecoin
from bot.models import MintEvent
from bot.twitter import media
from bot.twitter.media import get_event_banner


@pytest.mark.parametrize("stablecoin", STABLECOINS, ids=lambda coin: coin.ticker)
@pytest.mark.parametrize(("action", "quantity"), [("minted", 1_000_000), ("burned", -1_000_000)])
def test_bundled_banner_is_valid_png(stablecoin, action, quantity):
    event = MintEvent(tx_hash="tx", action=action, quantity=quantity, stablecoin=stablecoin)

    banner = get_event_banner(event)

    assert banner.name == f"{stablecoin.ticker.lower()}-{action}.png"
    data = banner.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (1200, 400)
    assert len(data) < 5_000_000


@pytest.mark.parametrize("ticker", ["UNKNOWN", "DJED"])
def test_missing_token_banner_does_not_get_another_tokens_banner(ticker):
    event = MintEvent(
        tx_hash="tx",
        action="minted",
        quantity=1,
        stablecoin=Stablecoin(ticker=ticker, policy_id="policy", asset_id="asset", decimals=6),
    )

    with pytest.raises(FileNotFoundError, match="Missing event banner"):
        get_event_banner(event)


@pytest.mark.parametrize(("action", "quantity"), [("minted", 1), ("burned", -1)])
def test_configured_token_needs_only_its_banner_files(tmp_path, monkeypatch, action, quantity):
    """A newly configured ticker works without a second runtime token registry."""
    monkeypatch.setattr(media, "_BANNERS_DIR", tmp_path)
    expected_banner = tmp_path / f"custom-{action}.png"
    expected_banner.write_bytes(b"banner")
    event = MintEvent(
        tx_hash="tx",
        action=action,
        quantity=quantity,
        stablecoin=Stablecoin(ticker="CUSTOM", policy_id="policy", asset_id="asset", decimals=6),
    )

    assert get_event_banner(event) == expected_banner


@pytest.mark.parametrize("ticker", ["../USDM", "/USDM", "", "USDM/minted", "ＵＳＤＭ"])
def test_banner_ticker_cannot_escape_assets_directory(ticker):
    event = MintEvent(
        tx_hash="tx",
        action="minted",
        quantity=1,
        stablecoin=Stablecoin(ticker=ticker, policy_id="policy", asset_id="asset", decimals=6),
    )

    with pytest.raises(ValueError, match="Invalid banner ticker"):
        get_event_banner(event)
