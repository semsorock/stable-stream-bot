-- Draft SQL queries for DB-Sync
-- Use this file to test queries before adding to bot/db/queries.py

-- Get USDM mints/burns for a specific block
SELECT encode(tx.hash, 'hex') AS tx_hash, mtm.quantity
  FROM ma_tx_mint AS mtm
  JOIN multi_asset AS ma ON mtm.ident = ma.id
  JOIN tx ON tx.id = mtm.tx_id
  JOIN block ON block.id = tx.block_id
 WHERE encode(ma.policy, 'hex') = 'c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad'
   AND encode(block.hash, 'hex') = '<BLOCK_HASH_HERE>';

-- Get current total USDM supply
SELECT COALESCE(SUM(mtm.quantity), 0) AS total_supply
  FROM ma_tx_mint AS mtm
  JOIN multi_asset AS ma ON mtm.ident = ma.id
 WHERE encode(ma.policy, 'hex') = 'c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad';

-- Get recent USDM mints/burns (for debugging)
SELECT encode(tx.hash, 'hex') AS tx_hash,
       mtm.quantity,
       block.block_no,
       block.time
  FROM ma_tx_mint AS mtm
  JOIN multi_asset AS ma ON mtm.ident = ma.id
  JOIN tx ON tx.id = mtm.tx_id
  JOIN block ON block.id = tx.block_id
 WHERE encode(ma.policy, 'hex') = 'c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad'
 ORDER BY block.time DESC
 LIMIT 10;
