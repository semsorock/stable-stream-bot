# Stable Stream Bot - AI Agent Instructions

## Project Purpose

This is a **Cardano stablecoin monitoring bot** that watches USDM, USDCx, and USDA mint/burn transactions and posts summaries with token banners to Twitter/X. Stablecoins are configured in `bot/config.py`. It's deployed as a Google Cloud Run service (FastAPI + uvicorn) triggered by Blockfrost block webhooks (`POST /`).

USDM is Mehen's fiat-backed USD stablecoin on Cardano (policy ID: `c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad`).

## Architecture Overview

### Core Data Flow

1. **Blockfrost webhook** → Cloud Run `/` endpoint
2. **Acknowledge the webhook quickly** to keep request latency low
3. **Query Blockfrost API** in background for configured stablecoin mint/burn transactions in the block
4. **Filter events** by each token's fixed activation timestamp and minimum post amount
5. **Post tweets** via X API (XDK) with quantity, supply, Cardano Explorer link, and a token/action banner

### Project Structure

```
├── main.py                      # Entry point shim — re-exports FastAPI `app` from bot.main
├── bot/
│   ├── __init__.py
│   ├── config.py                # Centralised config (.env via dotenv), validation + feature flags
│   ├── assets/                  # Runtime banners; development logos/fonts and provenance
│   ├── logging.py               # Logging setup (setup_logging, get_logger)
│   ├── models.py                # Dataclasses: MintEvent
│   ├── links.py                 # External link builders (Cardano Explorer)
│   ├── main.py                  # FastAPI app + fast-ack webhook handler
│   ├── webhook_auth.py          # Blockfrost HMAC-SHA256 signature verification
│   ├── blockfrost_client.py     # Blockfrost API queries (mint/burn detection + supply)
│   ├── blockfrost_transport.py  # SDK-compatible reads with connect/read timeouts
│   └── twitter/
│       ├── __init__.py
│       ├── client.py            # XDK wrapper with TWEET_POSTING_ENABLED gate
│       ├── formatter.py         # Tweet text builders for mint/burn events
│       ├── media.py             # Token/action banner path selection
│       └── templates.py         # Editable tweet text templates
├── scripts/render_banners.py    # Optional Pillow development renderer
├── tests/                       # Pytest test suite
├── .github/workflows/ci.yml     # CI pipeline (ruff + pytest)
├── .env.example                 # Template for required env vars
├── .dockerignore                # Docker build context exclusions
├── pyproject.toml               # Project config, dependencies, ruff & pytest settings
├── uv.lock                      # Locked dependency versions
└── Dockerfile                   # Container image (uses uv, non-root user)
```

### Key Components

- `bot/config.py`: All env vars loaded into a frozen `Config` dataclass via `python-dotenv` (`.env` overrides system vars). `Stablecoin` defines asset identity, decimals, `activation_timestamp`, and `min_post_amount` in whole tokens.

- `bot/models.py`: Typed dataclasses for all domain objects:
  - `MintEvent`: Stablecoin mint/burn transaction data

- `bot/blockfrost_client.py`: Async functions that query the Blockfrost API. Uses a stateless cross-referencing algorithm for each configured stablecoin:
  1. Get block transactions via `api.block_transactions()`
  2. Page stablecoin mint/burn histories via `api.asset_history()` through the target block
  3. Cross-reference to find events in the current block
  4. Filter events for posting eligibility before supply lookup
  5. Get total supply via `api.asset()` for stablecoins with reportable events

- `bot/twitter/client.py`: `post_tweet()` via XDK — logs content and only posts when `TWEET_POSTING_ENABLED=true`. Uploads the banner and alt text, caches its media ID until X's expiry, and attaches it to the post. Dry-run sends no X requests. Missing post IDs have an unknown outcome and are not automatically retried.

- `bot/twitter/media.py`: Derives `bot/assets/banners/{ticker.lower()}-{action}.png` from the event, checks that the file exists, and requires an ASCII alphanumeric ticker. There is no separate runtime token registry.

- `bot/twitter/formatter.py`: Pure functions that build tweet text strings:
  - `format_mint_tweet()`: Stablecoin mint event
  - `format_burn_tweet()`: Stablecoin burn event
  - `format_event_tweet()`: Routes an event to the appropriate formatter

- `bot/twitter/templates.py`: Centralised tweet copy templates:
  - `MINT`: Stablecoin minted template
  - `BURN`: Stablecoin burned template

- `bot/links.py`: URL builders for Cardano Explorer.

- `bot/main.py`: FastAPI `app` instance with a fast-ack `POST /` webhook handler that continues block processing asynchronously in-process. Each event has its own error boundary; a missing banner or failed post logs the event and processing continues.

- `bot/logging.py`: `setup_logging()` + `get_logger()` — stdlib logging, structured for Cloud Run.

## Blockfrost API Integration

The bot queries the **Blockfrost API** (via `blockfrost-python` SDK) to detect configured stablecoin mint/burn events. This is a stateless approach — no database or persistent state required.

### Detection Algorithm (per block)

1. `api.block_transactions(block_hash)` → set of tx hashes in the block
2. Skip activation-ineligible tokens before history lookup; for eligible entries in `STABLECOINS`, page `api.asset_history(asset_id, order='desc', count=100)` through the target block; 100 is a page size, not a total history limit
3. Cross-reference each history with the block transaction hashes
4. Apply activation and amount filters before supply lookup
5. `api.asset(asset_id).quantity` → current total supply for each stablecoin with reportable events

History pagination prevents bursts of small USDCx events from hiding a reportable event beyond the first page. A fixed 100-entry history window is insufficient for a high-activity asset.

### Posting Eligibility

- USDCx and USDA have a fixed rollout boundary: **2026-10-19 00:00 Europe/Istanbul**. Earlier blocks, including replayed webhooks, are suppressed. Never replace this with a moving `today()` cutoff.
- Blockfrost block metadata verifies the timestamp. Verification fails closed for activation-gated tokens after retries are exhausted; USDM has no activation cutoff.
- USDCx requires `abs(quantity) >= 10_000 * 10**decimals`; both mint and burn amounts include exactly 10,000 tokens. USDM and USDA have no amount floor.
- All configured tokens use six decimals. DJED is not monitored.

**USDM Asset ID**: `c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d` (policy ID + CIP-68 label 333 `0014df10` + hex "USDM")

## External Dependencies & Patterns

### External Links

The bot generates links to Cardano Explorer via `bot/links.py`:

- **Cardano Explorer**: `make_explorer_link(tx_hash)` → `https://explorer.cardano.org/transaction/{tx_hash}`

## Development Patterns

### Error Handling & Resilience

- All Blockfrost reads use a local SDK-compatible adapter (`bot/blockfrost_transport.py`) over one pooled keep-alive `requests.Session` with 3.05-second connect and 10-second read-idle timeouts. No global socket or requests defaults are changed. `tests/test_blockfrost_transport.py` fails if the client calls an SDK read the adapter does not override, so a new read must be added there together with a decoding case. Each read owns its own five-attempt `tenacity` policy (built by `_read_retry`) covering connection errors, truncated responses (`ChunkedEncodingError`), timeouts, 429, and 5xx. The wait depends on the cause: contention (429, 5xx, transport failures) uses fully jittered exponential backoff (1–8 seconds per wait); indexing lag (404 on block-related reads, logged at INFO) uses a fixed 2, 2, 4, 8-second schedule plus up to 1 second of jitter, so an unindexed block always gets at least 16 seconds before the final attempt. Other retries log WARNING. `SSLError` and `ProxyError` mean local misconfiguration and are never retried; other 4xx errors are not retried either. Both block transactions and histories retry each page independently; X post creation and whole blocks are never retried.

- Metadata (timestamp and height) is shared within each block request. Activation-gated tokens verify time before history lookup, avoiding lifetime scans on old replays. A bounded cache of boundary transaction heights avoids repeat calls for quiet tokens. Later-page or boundary failures retain collected history and explicitly log that history is incomplete; initial history failures and individual event failures leave other tokens/events processing. Cancellation propagates.

- Blockfrost SDK-compatible calls are synchronous, dispatched with `run_in_executor()` to a dedicated eight-worker pool (`POOL_SIZE`, which also sizes the session's connection pool). Network I/O and backoff do not occupy the shared executor used for X posting. The FastAPI lifespan calls `shutdown_reads()` on shutdown, which cancels queued reads; a later read creates a fresh pool. This bounds worker concurrency, not queued webhook backlog; no durable replay queue exists.

- Block transactions and asset history are fetched concurrently via `asyncio.gather()`

### Environment Variables

Loaded from `.env` file (overrides system env vars) via `python-dotenv`.

```bash
# Currently required by the configuration loader
BLOCKFROST_PROJECT_ID              # Blockfrost mainnet project ID
API_KEY, API_SECRET_KEY            # Twitter OAuth 1.0a
ACCESS_TOKEN, ACCESS_TOKEN_SECRET  # Twitter access credentials

# Optional
BLOCKFROST_WEBHOOK_AUTH_TOKEN      # Blockfrost webhook HMAC secret
TWEET_POSTING_ENABLED              # "true" to enable tweet posting (default: false)
```

### Code Style & Patterns

- **Formatter & linter**: ruff (configured in `pyproject.toml`)
- Line length: 120 chars
- Lint rules: `E`, `F`, `W`, `I` (isort), `UP` (pyupgrade)
- All domain objects are frozen dataclasses in `bot/models.py`
- Tweet formatting is pure (functions return strings, no side effects)
- `post_tweet()` is the single point for Twitter output — gated by config flag
- Uses stdlib `logging` everywhere (no `print()` calls)

## Workflow & Commands

### Deployment Target

Google Cloud Run (FastAPI + uvicorn, containerized)

- Continuously deployed from GitHub via Cloud Run source-based deployment
- Docker image built automatically by Cloud Build on push to `main`
- Entry point: `uvicorn bot.main:app` (root `main.py` re-exports `app`)
- `POST /` handles Blockfrost block events
- Returns JSON responses with appropriate HTTP status codes

### Local Development

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync dependencies (creates .venv automatically)
uv sync

# Run locally
uv run uvicorn bot.main:app --reload --port 8080
# Access at http://localhost:8080/
# Endpoint: POST / (Blockfrost)

# Run tests
uv run pytest -v

# Format & lint
uv run ruff format .
uv run ruff check --fix .
```

### Testing Workflow

Set environment variables, then POST a Blockfrost block-event JSON payload to `/`.

## Important Quirks & Edge Cases

### Mint vs Burn Detection

- Use Blockfrost's `action` field (`minted` or `burned`) to classify the event.
- The live Blockfrost API returns signed history amounts: positive for mints and negative for burns. Validate that the sign agrees with `action`; the OpenAPI example incorrectly shows positive burn amounts.

### USDM Token Details

- **Policy ID**: `c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad`
- **Decimals**: 6 (divide by 1,000,000 for display)
- **Explorer**: [CExplorer Policy](https://cexplorer.io/policy/c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad)

### Feature Flags

- **Tweet posting**: Controlled by `TWEET_POSTING_ENABLED` env var (default: off)
- **Webhook signature verification**: Skipped if `BLOCKFROST_WEBHOOK_AUTH_TOKEN` not set

## When Modifying Code

### Adding New Event Types

1. Add data model to `bot/models.py`

2. Add Blockfrost query function to `bot/blockfrost_client.py`

3. Add formatter function to `bot/twitter/formatter.py`

4. Add orchestration in `bot/main.py`

### Adding a Stablecoin

1. Add a `Stablecoin` to `STABLECOINS` in `bot/config.py` with its authentic full asset ID and decimals. Choose a fixed activation timestamp and optional whole-token `min_post_amount`.
2. Add `bot/assets/banners/{ticker.lower()}-minted.png` and `-burned.png` (1200 × 400 PNGs). `tests/test_media.py` checks every configured token automatically. No change to `bot/twitter/media.py` is needed.
3. To use the renderer, add the token logo to `bot/assets/logos/` and its color to `scripts/render_banners.py`'s `TOKENS`, then run `uv run --with pillow python scripts/render_banners.py`. Record provenance in `bot/assets/README.md`.
4. The production Docker context excludes source logos and fonts; it includes the generated banners. Protocols with pre-minted reserves need a custom detector.

### Changing Tweet Format

- Modify formatter functions in `bot/twitter/formatter.py`

- Keep under 280 characters (Twitter limit not enforced in code)

- Use emoji consistently: 💵 (mint), 🔥 (burn), 🔗 (links)

- Templates contain no hashtags. Each post uses the matching token/action banner; an upload failure does not fall back to a text-only post.

## Dependencies Summary

Managed via `uv` (see `pyproject.toml`). Lockfile: `uv.lock`.

```text
# Production
fastapi>=0.115,<1           # Async web framework
uvicorn[standard]>=0.34,<1  # ASGI server
xdk>=0.8.1                  # X API SDK (OAuth 1.0a + posts client)
blockfrost-python>=0.7,<1   # Blockfrost API SDK
tenacity>=9,<10             # Retry/backoff decorator
python-dotenv>=1.2.1        # .env file loading
tzdata>=2025.2              # ZoneInfo fallback for slim images without system timezone data
# Dev
pytest>=8                   # Test runner
pytest-asyncio>=0.25        # Async test support (asyncio_mode = "auto")
httpx>=0.28                 # Async HTTP client (FastAPI TestClient)
ruff>=0.9                   # Formatter + linter
```

## Related Documentation

- [Blockfrost Python SDK](https://github.com/blockfrost/blockfrost-python): API client library

- [USDM Token](https://mehen.io/): Mehen fiat-backed stablecoin

- [Blockfrost Webhooks](https://blockfrost.dev/start-building/webhooks/webhooks-events): Webhook event format
