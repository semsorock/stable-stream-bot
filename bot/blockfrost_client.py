"""Blockfrost API client for querying stablecoin mint/burn events."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import lru_cache
from threading import Lock

from blockfrost import ApiError
from requests.exceptions import ChunkedEncodingError, ConnectionError, ProxyError, SSLError, Timeout
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_combine,
    wait_exponential,
    wait_random,
    wait_random_exponential,
)

from .blockfrost_transport import POOL_SIZE, TimeoutBlockFrostApi
from .config import STABLECOINS, Stablecoin, get_config
from .logging import get_logger
from .models import MintEvent

logger = get_logger(__name__)
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    """Create the Blockfrost read pool on first use so a shutdown pool is never reused."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="blockfrost")
    return _executor


def shutdown_reads() -> None:
    """Drop reads still queued at shutdown; their blocks will not be posted once the process is gone."""
    global _executor
    executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _is_transient_error(exc: BaseException) -> bool:
    """Retry safe reads on transport failures, rate limits, and server errors."""
    if isinstance(exc, (SSLError, ProxyError)):
        return False  # Local TLS or proxy misconfiguration: surface it at once instead of burning the budget.
    return isinstance(exc, (ConnectionError, Timeout, ChunkedEncodingError)) or (
        isinstance(exc, ApiError) and (exc.status_code == 429 or 500 <= exc.status_code < 600)
    )


def _is_indexing_lag(exc: BaseException) -> bool:
    """Blockfrost returns 404 for a block its webhook announced before its API indexed it."""
    return isinstance(exc, ApiError) and exc.status_code == 404


def _is_transient_or_not_found(exc: BaseException) -> bool:
    """Block-related reads also tolerate delayed Blockfrost indexing."""
    return _is_transient_error(exc) or _is_indexing_lag(exc)


# Contention with the other reads of the same block on Blockfrost's rate limiter: full jitter spreads retries.
_CONTENTION_WAIT = wait_random_exponential(multiplier=2, min=1, max=8)
# Waiting for Blockfrost to index a fresh block: a fixed 2+2+4+8s schedule that jitter can only lengthen,
# so an unindexed block always gets at least 16 seconds before the final attempt.
_INDEXING_WAIT = wait_combine(wait_exponential(min=2, max=8), wait_random(0, 1))


def _wait_for_read(state: RetryCallState) -> float:
    """Choose the wait by cause: indexing lag needs a floor, everything else needs spread."""
    if _is_indexing_lag(state.outcome.exception()):
        return _INDEXING_WAIT(state)
    return _CONTENTION_WAIT(state)


def _log_retry(state: RetryCallState) -> None:
    error = state.outcome.exception()
    level = logging.INFO if _is_indexing_lag(error) else logging.WARNING
    logger.log(
        level,
        "Retrying %s in %.2fs after attempt %s: %s",
        state.fn.__name__,
        state.next_action.sleep,
        state.attempt_number,
        error,
    )


def _read_retry(predicate):
    """Give each decorated read its own five-attempt policy; nothing is shared between reads."""

    def decorate(function):
        policy = Retrying(
            stop=stop_after_attempt(5),
            wait=_wait_for_read,
            retry=retry_if_exception(predicate),
            before_sleep=_log_retry,
        )
        return policy.wraps(function)

    return decorate


_retry_read = _read_retry(_is_transient_error)
_retry_block_read = _read_retry(_is_transient_or_not_found)


@lru_cache(maxsize=1)
def _get_api() -> TimeoutBlockFrostApi:
    """Get or create the SDK-compatible, timeout-bounded read client."""
    return TimeoutBlockFrostApi(project_id=get_config().blockfrost_project_id)


@_retry_block_read
def _fetch_block_transactions_page(block_hash: str, page: int):
    return _get_api().block_transactions(block_hash, count=100, page=page)


def _fetch_block_tx_hashes(block_hash: str) -> set[str]:
    """Fetch all transaction hashes for a given block."""
    tx_hashes: set[str] = set()
    page = 1
    while True:
        results = _fetch_block_transactions_page(block_hash, page)
        if not results:
            break
        tx_hashes.update(results)
        if len(results) < 100:
            break
        page += 1
    return tx_hashes


@_retry_block_read
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
@_retry_block_read
def _fetch_transaction_block_height(tx_hash: str) -> int:
    """Cache page-boundary transaction heights so quiet histories need no repeated lookup."""
    height = getattr(_get_api().transaction(tx_hash), "block_height", None)
    if type(height) is not int or height < 0:
        raise ValueError(f"Invalid block height for transaction {tx_hash}")
    return height


@_retry_read
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


@_retry_read
def _fetch_total_supply(asset_id: str) -> int:
    """Fetch the current total supply for an asset."""
    return int(_get_api().asset(asset_id).quantity)


async def get_mints_for_block(block_hash: str) -> list[MintEvent]:
    """Get configured stablecoin mint/burn events for a specific block."""
    logger.info(f"Querying Blockfrost for block {block_hash}")
    block = _BlockMetadata(block_hash)
    timestamp = None
    if any(stablecoin.activation_timestamp is not None for stablecoin in STABLECOINS):
        try:
            timestamp = await _run_read(block.timestamp)
        except Exception:
            logger.exception(f"Cannot verify block time for {block_hash}; suppressing activation-gated events")
    # Reject historical rollouts before reading their history, which can otherwise
    # require paging through the token's entire lifetime on an old replay.
    stablecoins = tuple(stablecoin for stablecoin in STABLECOINS if _is_active(stablecoin, timestamp))

    # Fetch block transactions once and each stablecoin's history concurrently.
    block_tx_hashes, *stablecoin_histories = await asyncio.gather(
        _run_read(_fetch_block_tx_hashes, block_hash),
        *(_run_read(_fetch_recent_mint_burns, stablecoin.asset_id, block) for stablecoin in stablecoins),
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
    return await _run_read(_fetch_total_supply, asset_id)


async def _run_read(function, *args):
    """Keep Blockfrost I/O and backoff off the executor used by X and other work."""
    return await asyncio.get_running_loop().run_in_executor(_get_executor(), copy_context().run, function, *args)
