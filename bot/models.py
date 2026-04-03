"""Domain dataclasses for USDM events."""

from dataclasses import dataclass
from typing import Literal

EventAction = Literal["minted", "burned"]


@dataclass(frozen=True)
class MintEvent:
    """A USDM mint or burn event."""

    tx_hash: str
    action: EventAction
    quantity: int  # Keep Blockfrost amount exactly as returned by API

    def _validate_action_quantity_match(self) -> None:
        """Ensure action and amount sign agree with Blockfrost semantics."""
        if self.action == "minted" and self.quantity < 0:
            raise ValueError(f"Invalid event: minted quantity must be >= 0, got {self.quantity}")
        if self.action == "burned" and self.quantity > 0:
            raise ValueError(f"Invalid event: burned quantity must be <= 0, got {self.quantity}")

    @property
    def is_mint(self) -> bool:
        """True if this is a mint event."""
        self._validate_action_quantity_match()
        return self.action == "minted"

    @property
    def is_burn(self) -> bool:
        """True if this is a burn event."""
        self._validate_action_quantity_match()
        return self.action == "burned"

    @property
    def display_quantity(self) -> float:
        """Quantity in display units (divided by 1,000,000)."""
        self._validate_action_quantity_match()
        return abs(self.quantity) / 1_000_000
