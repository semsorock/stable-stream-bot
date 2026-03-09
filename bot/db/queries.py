"""SQL query constants for DB-Sync."""

# Get USDM mint/burn transactions for a specific block
QUERY_MINTS = """
SELECT encode(tx.hash, 'hex'), mtm.quantity
  FROM ma_tx_mint AS mtm
  JOIN multi_asset AS ma ON mtm.ident = ma.id
  JOIN tx ON tx.id = mtm.tx_id
  JOIN block ON block.id = tx.block_id
 WHERE encode(ma.policy, 'hex') = %s
   AND encode(block.hash, 'hex') = %s
"""

# Get current total USDM supply
QUERY_SUPPLY = """
SELECT COALESCE(SUM(mtm.quantity), 0)
  FROM ma_tx_mint AS mtm
  JOIN multi_asset AS ma ON mtm.ident = ma.id
 WHERE encode(ma.policy, 'hex') = %s
"""
