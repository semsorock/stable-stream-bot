"""Tests for link builders."""

from bot.links import make_explorer_link


def test_make_explorer_link():
    """Test Cardano Explorer link generation."""
    link = make_explorer_link("abc123def456")

    assert link == "https://explorer.cardano.org/transaction/abc123def456"
