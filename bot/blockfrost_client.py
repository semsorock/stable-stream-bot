"""Blockfrost API client for querying stablecoin mint/burn events."""

import asyncio
from functools import lru_cache
from threading import Lock

from blockfrost import ApiError, BlockFrostApi
from requests.exceptions import ConnectionError, Timeout
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import STABLECOINS, Stablecoin, get_config
from .logging import get_logger
from .models import MintEvent

logger = get_logger(__name__)


def _is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, ApiError) and exc.status_code == 429


def _is_rate_limited_or_not_found(exc: BaseException) -> bool:
    return isinstance(exc, ApiError) and exc.status_code in (404, 429)


def _is_transient_metadata_error(exc: BaseException) -> bool:
    return isinstance(exc, (ConnectionError, Timeout)) or (
        isinstance(exc, ApiError) and (exc.status_code in (404, 429) or 500 <= exc.status_code < 600)
    )


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


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception(_is_transient_metadata_error),
)
def _fetch_block_metadata(block_hash: str):
    """Read authoritative time and height, retrying transient Blockfrost failures."""
    return _get_api().block(block_hash)


class _BlockMetadata:
    """Lazily share one metadata lookup, including its failure, across a block's workers."""

    def __init__(self, block_hash: str):
        self.block_hash = block_hash
        self._data = None
        self._loaded = False
        self._error: Exception | None = None
        self._lock = Lock()

    def _value(self, name: str) -> int:
        with self._lock:
            if not self._loaded:
                try:
                    self._data = _fetch_block_metadata(self.block_hash)
                except Exception as exc:
                    self._error = exc
                self._loaded = True
            if self._error is not None:
                raise self._error
            value = getattr(self._data, name, None)
        if type(value) is not int or value < 0:
            raise ValueError(f"Invalid {name} for block {self.block_hash}")
        return value

    def timestamp(self) -> int:
        return self._value("time")

    def height(self) -> int:
        return self._value("height")


@lru_cache(maxsize=4096)
@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception(_is_transient_metadata_error),
)
def _fetch_transaction_block_height(tx_hash: str) -> int:
    """Cache page-boundary transaction heights so quiet histories need no repeated lookup."""
    height = getattr(_get_api().transaction(tx_hash), "block_height", None)
    if type(height) is not int or height < 0:
        raise ValueError(f"Invalid block height for transaction {tx_hash}")
    return height


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), retry=retry_if_exception(_is_rate_limited))
def _fetch_history_page(asset_id: str, page: int):
    return _get_api().asset_history(asset_id, order="desc", count=100, page=page)


def _fetch_recent_mint_burns(asset_id: str, block: _BlockMetadata) -> list[dict]:
    """Page through the target block, stopping once history reaches an older block."""
    events = []
    seen = set()
    page = 1
    try:
        while True:
            history = _fetch_history_page(asset_id, page)
            for entry in history:
                if entry.action not in ("minted", "burned"):
                    continue
                amount = int(entry.amount)
                key = (entry.tx_hash, entry.action, amount)
                if key not in seen:
                    seen.add(key)
                    events.append({"tx_hash": entry.tx_hash, "action": entry.action, "amount": amount})
            if len(history) < 100:
                break
            # History entries do not include heights. One cached transaction lookup
            # at each full page's oldest entry bounds scanning to the requested block.
            target_height = block.height()
            if _fetch_transaction_block_height(history[-1].tx_hash) < target_height:
                break
            page += 1
    except Exception:
        if page == 1 and not events:
            raise
        logger.exception(
            "Asset %s history incomplete at page %s for block %s; retaining %s collected events",
            asset_id,
            page,
            block.block_hash,
            len(events),
        )
    return events


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), retry=retry_if_exception(_is_rate_limited))
def _fetch_total_supply(asset_id: str) -> int:
    """Fetch the current total supply for an asset."""
    api = _get_api()
    asset = api.asset(asset_id)
    return int(asset.quantity)


async def get_mints_for_block(block_hash: str) -> list[MintEvent]:
    """Get configured stablecoin mint/burn events for a specific block."""
    logger.info(f"Querying Blockfrost for block {block_hash}")
    block = _BlockMetadata(block_hash)
    timestamp = None
    if any(stablecoin.activation_timestamp is not None for stablecoin in STABLECOINS):
        try:
            timestamp = await asyncio.to_thread(block.timestamp)
        except Exception:
            logger.exception(f"Cannot verify block time for {block_hash}; suppressing activation-gated events")
    # Reject historical rollouts before reading their history, which can otherwise
    # require paging through the token's entire lifetime on an old replay.
    stablecoins = tuple(stablecoin for stablecoin in STABLECOINS if _is_active(stablecoin, timestamp))

    # Fetch block transactions once and each stablecoin's history concurrently.
    block_tx_hashes, *stablecoin_histories = await asyncio.gather(
        asyncio.to_thread(_fetch_block_tx_hashes, block_hash),
        *(asyncio.to_thread(_fetch_recent_mint_burns, stablecoin.asset_id, block) for stablecoin in stablecoins),
        return_exceptions=True,
    )
    if isinstance(block_tx_hashes, BaseException):
        raise block_tx_hashes

    history_count = sum(len(history) for history in stablecoin_histories if not isinstance(history, BaseException))
    logger.info(f"Block has {len(block_tx_hashes)} transactions, histories have {history_count} recent events")

    # Cross-reference: find mint/burn events that belong to this block
    events = []
    for stablecoin, recent_events in zip(stablecoins, stablecoin_histories, strict=True):
        if isinstance(recent_events, Exception):
            logger.error(
                "Cannot read %s history for block %s; continuing with other tokens",
                stablecoin.ticker,
                block_hash,
                exc_info=(type(recent_events), recent_events, recent_events.__traceback__),
            )
            continue
        if isinstance(recent_events, BaseException):
            raise recent_events
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

    return _filter_reportable_events(events, timestamp)


def _is_active(stablecoin: Stablecoin, timestamp: int | None) -> bool:
    """Unknown authoritative time fails closed only for activation-gated tokens."""
    return stablecoin.activation_timestamp is None or (
        timestamp is not None and timestamp >= stablecoin.activation_timestamp
    )


def _filter_reportable_events(events: list[MintEvent], timestamp: int | None) -> list[MintEvent]:
    """Apply each token's inclusive amount floor and fixed activation boundary."""
    reportable = []
    for event in events:
        if not _is_active(event.stablecoin, timestamp):
            continue
        if abs(event.quantity) < event.stablecoin.min_post_amount * 10**event.stablecoin.decimals:
            logger.info(
                "Skipping %s %s below %s-token threshold: tx=%s, quantity=%s",
                event.stablecoin.ticker,
                event.action,
                event.stablecoin.min_post_amount,
                event.tx_hash,
                event.quantity,
            )
            continue
        reportable.append(event)
    return reportable


async def get_total_supply(asset_id: str) -> int:
    """Get the current total supply for an asset."""
    return await asyncio.to_thread(_fetch_total_supply, asset_id)
