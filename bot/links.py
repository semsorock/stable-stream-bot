"""External link builders."""


def make_explorer_link(tx_hash: str) -> str:
    """Build a Cardano Explorer transaction link."""
    return f"https://explorer.cardano.org/transaction/{tx_hash}"
