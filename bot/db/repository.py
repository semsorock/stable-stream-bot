"""Async data access layer for DB-Sync."""

import asyncio

import psycopg

from ..config import USDM_POLICY_ID, get_config
from ..logging import get_logger
from ..models import MintEvent
from .queries import QUERY_MINTS, QUERY_SUPPLY

logger = get_logger(__name__)

# Shared connection state
_conn: psycopg.AsyncConnection | None = None
_conn_lock = asyncio.Lock()


async def _get_connection() -> psycopg.AsyncConnection:
    """Get or create the shared database connection."""
    global _conn
    if _conn is None or _conn.closed:
        config = get_config()
        logger.info("Opening new database connection")
        _conn = await psycopg.AsyncConnection.connect(config.db_sync_url, autocommit=True)
    return _conn


async def _query(sql: str, params: tuple) -> list[tuple]:
    """Execute a query and return all rows."""
    global _conn
    async with _conn_lock:
        try:
            conn = await _get_connection()
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()
        except psycopg.Error as e:
            logger.error(f"Database error: {e}")
            # Reset connection so next call reconnects
            if _conn is not None:
                try:
                    await _conn.close()
                except Exception:
                    pass
                _conn = None
            raise


async def get_mints_for_block(block_hash: str) -> list[MintEvent]:
    """Get all USDM mint/burn events for a block."""
    rows = await _query(QUERY_MINTS, (USDM_POLICY_ID, block_hash))
    return [MintEvent(tx_hash=row[0], quantity=row[1]) for row in rows]


async def get_total_supply() -> int:
    """Get the current total USDM supply in base units."""
    rows = await _query(QUERY_SUPPLY, (USDM_POLICY_ID,))
    return int(rows[0][0]) if rows and rows[0][0] is not None else 0


async def close_connection() -> None:
    """Close the database connection if open."""
    global _conn
    async with _conn_lock:
        if _conn is not None:
            try:
                await _conn.close()
            except Exception:
                pass
            _conn = None
