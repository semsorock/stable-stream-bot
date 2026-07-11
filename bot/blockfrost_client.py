"""Blockfrost API client for querying stablecoin mint/burn events."""

import asyncio
from functools import lru_cache

from blockfrost import ApiError, BlockFrostApi
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import STABLECOINS, get_config
from .logging import get_logger
from .models import MintEvent

logger = get_logger(__name__)


def _is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, ApiError) and exc.status_code == 429


def _is_rate_limited_or_not_found(exc: BaseException) -> bool:
    return isinstance(exc, ApiError) and exc.status_code in (404, 429)


@lru_cache(maxsize=1)
def _get_api() -> BlockFrostApi:
    """Get or create the shared BlockFrost API instance."""
    return BlockFrostApi(project_id=get_config().blockfrost_project_id)


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception(_is_rate_limited_or_not_found),
)
def _fetch_block_tx_hashes(block_hash: str) -> set[str]:
    """Fetch all transaction hashes for a given block."""
    api = _get_api()
    tx_hashes: set[str] = set()
    page = 1
    while True:
        results = api.block_transactions(block_hash, page=page)
        if not results:
            break
        tx_hashes.update(results)
        if len(results) < 100:
            break
        page += 1
    return tx_hashes


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), retry=retry_if_exception(_is_rate_limited))
def _fetch_recent_mint_burns(asset_id: str) -> list[dict]:
    """Fetch recent mint/burn history entries for an asset."""
    api = _get_api()
    history = api.asset_history(asset_id, order="desc", count=100)
    return [
        {"tx_hash": entry.tx_hash, "action": entry.action, "amount": int(entry.amount)}
        for entry in history
        if entry.action in ("minted", "burned")
    ]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), retry=retry_if_exception(_is_rate_limited))
def _fetch_total_supply(asset_id: str) -> int:
    """Fetch the current total supply for an asset."""
    api = _get_api()
    asset = api.asset(asset_id)
    return int(asset.quantity)


async def get_mints_for_block(block_hash: str) -> list[MintEvent]:
    """Get configured stablecoin mint/burn events for a specific block."""
    logger.info(f"Querying Blockfrost for block {block_hash}")

    # Fetch block transactions once and each stablecoin's history concurrently.
    block_tx_hashes, *stablecoin_histories = await asyncio.gather(
        asyncio.to_thread(_fetch_block_tx_hashes, block_hash),
        *(asyncio.to_thread(_fetch_recent_mint_burns, stablecoin.asset_id) for stablecoin in STABLECOINS),
    )

    history_count = sum(len(history) for history in stablecoin_histories)
    logger.info(f"Block has {len(block_tx_hashes)} transactions, histories have {history_count} recent events")

    # Cross-reference: find mint/burn events that belong to this block
    events = []
    for stablecoin, recent_events in zip(STABLECOINS, stablecoin_histories, strict=True):
        for entry in recent_events:
            if entry["tx_hash"] in block_tx_hashes:
                events.append(
                    MintEvent(
                        tx_hash=entry["tx_hash"],
                        action=entry["action"],
                        quantity=entry["amount"],
                        stablecoin=stablecoin,
                    )
                )

    return events


async def get_total_supply(asset_id: str) -> int:
    """Get the current total supply for an asset."""
    return await asyncio.to_thread(_fetch_total_supply, asset_id)
