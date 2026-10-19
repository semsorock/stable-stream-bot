"""Editable tweet text templates."""

# Variables: {ticker}, {quantity}, {supply}, {link}
MINT = """💵 ${ticker} has been MINTED!
quantity: +{quantity}
total supply: {supply}
{link}"""

BURN = """🔥 ${ticker} has been BURNED!
quantity: -{quantity}
total supply: {supply}
{link}"""
