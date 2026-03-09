"""Tests for USDM event formatting."""

from bot.models import MintEvent
from bot.twitter.formatter import format_event_tweet


def test_format_mint_tweet():
    """Test formatting a mint event tweet."""
    event = MintEvent(tx_hash="abc123", quantity=1_000_000_000_000)  # 1M USDM
    supply = 50_000_000_000_000  # 50M USDM

    tweet = format_event_tweet(event, supply)

    assert "$USDM" in tweet
    assert "MINTED" in tweet
    assert "1,000,000.00" in tweet
    assert "50,000,000.00" in tweet
    assert "abc123" in tweet
    assert "cardanoscan.io" in tweet


def test_format_burn_tweet():
    """Test formatting a burn event tweet."""
    event = MintEvent(tx_hash="def456", quantity=-500_000_000_000)  # -500K USDM
    supply = 49_500_000_000_000  # 49.5M USDM

    tweet = format_event_tweet(event, supply)

    assert "$USDM" in tweet
    assert "BURNED" in tweet
    assert "500,000.00" in tweet
    assert "49,500,000.00" in tweet
    assert "def456" in tweet


def test_mint_event_properties():
    """Test MintEvent dataclass properties."""
    mint = MintEvent(tx_hash="abc", quantity=1_000_000)
    burn = MintEvent(tx_hash="def", quantity=-1_000_000)

    assert mint.is_mint is True
    assert mint.is_burn is False
    assert mint.display_quantity == 1.0

    assert burn.is_mint is False
    assert burn.is_burn is True
    assert burn.display_quantity == 1.0
