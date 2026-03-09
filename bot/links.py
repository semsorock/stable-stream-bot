"""External link builders."""


def make_cardanoscan_link(tx_hash: str) -> str:
    """Build a Cardanoscan transaction link with tokenmint tab."""
    return f"https://cardanoscan.io/transaction/{tx_hash}?tab=tokenmint"
