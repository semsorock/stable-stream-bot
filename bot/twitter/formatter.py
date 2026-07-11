"""Tweet text builders for stablecoin events."""

from ..links import make_explorer_link
from ..models import MintEvent
from . import templates


def _format_quantity(amount: float) -> str:
    """Format a quantity with thousand separators and 2 decimal places."""
    return f"{amount:,.2f}"


def format_mint_tweet(event: MintEvent, supply: int) -> str:
    """Format a tweet for a stablecoin mint event."""
    quantity_display = _format_quantity(event.display_quantity)
    supply_display = _format_quantity(supply / 10**event.stablecoin.decimals)
    link = make_explorer_link(event.tx_hash)

    return templates.MINT.format(
        ticker=event.stablecoin.ticker,
        quantity=quantity_display,
        supply=supply_display,
        link=link,
    )


def format_burn_tweet(event: MintEvent, supply: int) -> str:
    """Format a tweet for a stablecoin burn event."""
    quantity_display = _format_quantity(event.display_quantity)
    supply_display = _format_quantity(supply / 10**event.stablecoin.decimals)
    link = make_explorer_link(event.tx_hash)

    return templates.BURN.format(
        ticker=event.stablecoin.ticker,
        quantity=quantity_display,
        supply=supply_display,
        link=link,
    )


def format_event_tweet(event: MintEvent, supply: int) -> str:
    """Format a tweet for any stablecoin event (mint or burn)."""
    if event.is_mint:
        return format_mint_tweet(event, supply)
    else:
        return format_burn_tweet(event, supply)
