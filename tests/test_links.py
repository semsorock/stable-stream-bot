"""Tests for link builders."""

from bot.links import make_cardanoscan_link


def test_make_cardanoscan_link():
    """Test Cardanoscan link generation."""
    link = make_cardanoscan_link("abc123def456")

    assert link == "https://cardanoscan.io/transaction/abc123def456?tab=tokenmint"
