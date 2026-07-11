"""Tests for stablecoin event formatting."""

import pytest

from bot.config import USDM, Stablecoin
from bot.models import MintEvent
from bot.twitter.formatter import format_event_tweet


def test_format_mint_tweet():
    """Test formatting a mint event tweet."""
    event = MintEvent(tx_hash="abc123", action="minted", quantity=1_000_000_000_000, stablecoin=USDM)  # 1M USDM
    supply = 50_000_000_000_000  # 50M USDM

    tweet = format_event_tweet(event, supply)

    assert "$USDM" in tweet
    assert "MINTED" in tweet
    assert "1,000,000.00" in tweet
    assert "50,000,000.00" in tweet
    assert "abc123" in tweet
    assert "explorer.cardano.org" in tweet


def test_format_burn_tweet():
    """Test formatting a burn event tweet."""
    event = MintEvent(tx_hash="def456", action="burned", quantity=-500_000_000_000, stablecoin=USDM)  # -500K USDM
    supply = 49_500_000_000_000  # 49.5M USDM

    tweet = format_event_tweet(event, supply)

    assert "$USDM" in tweet
    assert "BURNED" in tweet
    assert "500,000.00" in tweet
    assert "49,500,000.00" in tweet
    assert "def456" in tweet


def test_format_tweet_uses_configured_ticker_and_decimals():
    """Ticker and decimal conversion should come from the event's stablecoin config."""
    stablecoin = Stablecoin(ticker="TEST", policy_id="policy", asset_id="asset", decimals=2)
    event = MintEvent(tx_hash="ghi789", action="minted", quantity=123_456, stablecoin=stablecoin)

    tweet = format_event_tweet(event, supply=9_876_500)

    assert "$TEST" in tweet
    assert "1,234.56" in tweet
    assert "98,765.00" in tweet


def test_mint_event_properties():
    """Test MintEvent dataclass properties."""
    mint = MintEvent(tx_hash="abc", action="minted", quantity=1_000_000, stablecoin=USDM)
    burn = MintEvent(tx_hash="def", action="burned", quantity=-1_000_000, stablecoin=USDM)

    assert mint.is_mint is True
    assert mint.is_burn is False
    assert mint.display_quantity == 1.0

    assert burn.is_mint is False
    assert burn.is_burn is True
    assert burn.display_quantity == 1.0


def test_mint_event_raises_on_action_quantity_mismatch():
    """Mismatch between action and quantity sign should fail on property access."""
    bad_burn = MintEvent(tx_hash="bad1", action="burned", quantity=1, stablecoin=USDM)
    bad_mint = MintEvent(tx_hash="bad2", action="minted", quantity=-1, stablecoin=USDM)

    with pytest.raises(ValueError, match="burned quantity"):
        _ = bad_burn.is_burn

    with pytest.raises(ValueError, match="minted quantity"):
        _ = bad_mint.is_mint
