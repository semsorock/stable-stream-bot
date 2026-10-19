"""Bundled token banners for X posts."""

from pathlib import Path

from ..models import MintEvent

_BANNERS_DIR = Path(__file__).resolve().parents[1] / "assets" / "banners"
_SUPPORTED_TICKERS = {"USDM", "USDCx", "USDA"}


def get_event_banner(event: MintEvent) -> Path:
    """Select one reusable banner per token and action; fail if it is missing."""
    if event.stablecoin.ticker not in _SUPPORTED_TICKERS or event.action not in ("minted", "burned"):
        raise ValueError(f"No banner for {event.stablecoin.ticker} {event.action}")
    action = "minted" if event.is_mint else "burned"
    path = _BANNERS_DIR / f"{event.stablecoin.ticker.lower()}-{action}.png"
    if not path.is_file():
        raise FileNotFoundError(f"Missing event banner: {path}")
    return path
