"""Domain dataclasses for USDM events."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MintEvent:
    """A USDM mint or burn event."""

    tx_hash: str
    quantity: int  # Positive = mint, negative = burn

    @property
    def is_mint(self) -> bool:
        """True if this is a mint event."""
        return self.quantity > 0

    @property
    def is_burn(self) -> bool:
        """True if this is a burn event."""
        return self.quantity < 0

    @property
    def display_quantity(self) -> float:
        """Quantity in display units (divided by 1,000,000)."""
        return abs(self.quantity) / 1_000_000
