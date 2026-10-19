"""Bundled token banners for X posts."""

from pathlib import Path

from ..models import MintEvent

_BANNERS_DIR = Path(__file__).resolve().parents[1] / "assets" / "banners"


def get_event_banner(event: MintEvent) -> Path:
    """Select one reusable banner per token and action; fail if it is missing."""
    ticker = event.stablecoin.ticker
    if not ticker.isascii() or not ticker.isalnum():
        raise ValueError(f"Invalid banner ticker: {ticker!r}")
    path = _BANNERS_DIR / f"{ticker.lower()}-{event.action}.png"
    if not path.is_file():
        raise FileNotFoundError(f"Missing event banner: {path}")
    return path
