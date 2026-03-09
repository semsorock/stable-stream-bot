"""Tweet text builders for USDM events."""

from ..links import make_cardanoscan_link
from ..models import MintEvent
from . import templates


def _format_quantity(amount: float) -> str:
    """Format a quantity with thousand separators and 2 decimal places."""
    return f"{amount:,.2f}"


def format_mint_tweet(event: MintEvent, supply: int) -> str:
    """Format a tweet for a USDM mint event."""
    quantity_display = _format_quantity(event.display_quantity)
    supply_display = _format_quantity(supply / 1_000_000)
    link = make_cardanoscan_link(event.tx_hash)

    return templates.MINT.format(
        quantity=quantity_display,
        supply=supply_display,
        link=link,
    )


def format_burn_tweet(event: MintEvent, supply: int) -> str:
    """Format a tweet for a USDM burn event."""
    quantity_display = _format_quantity(event.display_quantity)
    supply_display = _format_quantity(supply / 1_000_000)
    link = make_cardanoscan_link(event.tx_hash)

    return templates.BURN.format(
        quantity=quantity_display,
        supply=supply_display,
        link=link,
    )


def format_event_tweet(event: MintEvent, supply: int) -> str:
    """Format a tweet for any USDM event (mint or burn)."""
    if event.is_mint:
        return format_mint_tweet(event, supply)
    else:
        return format_burn_tweet(event, supply)
