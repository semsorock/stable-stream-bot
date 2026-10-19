"""Bundled images must match every supported token and event action."""

import struct

import pytest

from bot.config import STABLECOINS, Stablecoin
from bot.models import MintEvent
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
def test_unsupported_token_does_not_get_another_tokens_banner(ticker):
    event = MintEvent(
        tx_hash="tx",
        action="minted",
        quantity=1,
        stablecoin=Stablecoin(ticker=ticker, policy_id="policy", asset_id="asset", decimals=6),
    )

    with pytest.raises(ValueError, match="No banner"):
        get_event_banner(event)
