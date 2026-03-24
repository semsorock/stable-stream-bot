# USDM Stream Bot - AI Agent Instructions

## Project Purpose

This is a **Cardano blockchain USDM stablecoin monitoring bot** that watches for mint/burn transactions and posts summaries to Twitter/X. It's deployed as a Google Cloud Run service (FastAPI + uvicorn) triggered by Blockfrost block webhooks (`POST /`).

USDM is Mehen's fiat-backed USD stablecoin on Cardano (policy ID: `c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad`).

## Architecture Overview

### Core Data Flow

1. **Blockfrost webhook** → Cloud Run `/` endpoint
2. **Acknowledge the webhook quickly** to keep request latency low
3. **Query Blockfrost API** in background for USDM mint/burn transactions in the block
4. **Post tweets** via X API (XDK) with quantity, supply, and Cardanoscan link

### Project Structure

```
├── main.py                      # Entry point shim — re-exports FastAPI `app` from bot.main
├── bot/
│   ├── __init__.py
│   ├── config.py                # Centralised config (.env via dotenv), validation + feature flags
│   ├── logging.py               # Logging setup (setup_logging, get_logger)
│   ├── models.py                # Dataclasses: MintEvent
│   ├── links.py                 # External link builders (Cardanoscan)
│   ├── main.py                  # FastAPI app + fast-ack webhook handler
│   ├── webhook_auth.py          # Blockfrost HMAC-SHA256 signature verification
│   ├── blockfrost_client.py     # Blockfrost API queries (mint/burn detection + supply)
│   └── twitter/
│       ├── __init__.py
│       ├── client.py            # XDK wrapper with TWEET_POSTING_ENABLED gate
│       ├── formatter.py         # Tweet text builders for mint/burn events
│       └── templates.py         # Editable tweet text templates
├── tests/                       # Pytest test suite
├── .github/workflows/ci.yml     # CI pipeline (ruff + pytest)
├── .env.example                 # Template for required env vars
├── .dockerignore                # Docker build context exclusions
├── pyproject.toml               # Project config, dependencies, ruff & pytest settings
├── uv.lock                      # Locked dependency versions
├── Dockerfile                   # Container image (uses uv, non-root user)
├── docs/                        # Reference docs (DB-Sync schema)
└── drafts/                      # Development drafts and sample data (not deployed)
```

### Key Components

- `bot/config.py`: All env vars loaded into a frozen `Config` dataclass via `python-dotenv` (`.env` overrides system vars). Includes feature flags.

- `bot/models.py`: Typed dataclasses for all domain objects:
  - `MintEvent`: USDM mint/burn transaction data

- `bot/blockfrost_client.py`: Async functions that query the Blockfrost API. Uses a stateless cross-referencing algorithm:
  1. Get block transactions via `api.block_transactions()`
  2. Get recent USDM mint/burn history via `api.asset_history()`
  3. Cross-reference to find events in the current block
  4. Get total supply via `api.asset()` (only if events found)

- `bot/twitter/client.py`: `post_tweet()` via XDK — logs content and only posts when `TWEET_POSTING_ENABLED=true`.

- `bot/twitter/formatter.py`: Pure functions that build tweet text strings:
  - `format_mint_tweet()`: USDM mint event
  - `format_burn_tweet()`: USDM burn event

- `bot/twitter/templates.py`: Centralised tweet copy templates:
  - `MINT`: USDM minted template
  - `BURN`: USDM burned template

- `bot/links.py`: URL builders for Cardanoscan.

- `bot/main.py`: FastAPI `app` instance with a fast-ack `POST /` webhook handler that continues block processing asynchronously in-process.

- `bot/logging.py`: `setup_logging()` + `get_logger()` — stdlib logging, structured for Cloud Run.

## Blockfrost API Integration

The bot queries the **Blockfrost API** (via `blockfrost-python` SDK) to detect USDM mint/burn events. This is a stateless approach — no database or persistent state required.

### Detection Algorithm (per block)

1. `api.block_transactions(block_hash)` → set of tx hashes in the block
2. `api.asset_history(USDM_ASSET_ID, order='desc', count=100)` → recent mint/burn events
3. Cross-reference: filter history entries where `tx_hash` is in the block
4. `api.asset(USDM_ASSET_ID).quantity` → current total supply (only if events found)

USDM mints are infrequent (~1-2/day), so 100 history entries covers weeks.

**USDM Asset ID**: `c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d` (policy ID + CIP-68 label 333 `0014df10` + hex "USDM")

## External Dependencies & Patterns

### External Links

The bot generates links to Cardanoscan via `bot/links.py`:

- **Cardanoscan**: `make_cardanoscan_link(tx_hash)` → `https://cardanoscan.io/transaction/{tx_hash}?tab=tokenmint`

## Development Patterns

### Error Handling & Resilience

- Uses `tenacity` library with `@retry` decorator (3 attempts, exponential backoff) on Blockfrost 429 (rate limit) errors

- Blockfrost SDK calls are synchronous, wrapped in `asyncio.to_thread()` for async compatibility

- Block transactions and asset history are fetched concurrently via `asyncio.gather()`

### Environment Variables

Loaded from `.env` file (overrides system env vars) via `python-dotenv`.

```bash
# Required
BLOCKFROST_PROJECT_ID              # Blockfrost mainnet project ID
BLOCKFROST_WEBHOOK_AUTH_TOKEN      # Blockfrost webhook HMAC secret

# Twitter (required if TWEET_POSTING_ENABLED=true)
API_KEY, API_SECRET_KEY            # Twitter OAuth 1.0a
ACCESS_TOKEN, ACCESS_TOKEN_SECRET  # Twitter access credentials
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

1. **Webhook Simulation**: Sample payloads are in code comments in `bot/main.py`

2. **Local Testing**: Set environment variables, then POST webhook JSON to `/` (Blockfrost)

## Important Quirks & Edge Cases

### Mint vs Burn Detection

- `quantity > 0` → MINT event
- `quantity < 0` → BURN event

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

### Changing Tweet Format

- Modify formatter functions in `bot/twitter/formatter.py`

- Keep under 280 characters (Twitter limit not enforced in code)

- Use emoji consistently: 💵 (mint), 🔥 (burn), 🔗 (links)

## Dependencies Summary

Managed via `uv` (see `pyproject.toml`). Lockfile: `uv.lock`.

```text
# Production
fastapi>=0.115,<1           # Async web framework
uvicorn[standard]>=0.34,<1  # ASGI server
xdk>=0.8.1                  # X API SDK (OAuth 1.0a + posts client)
blockfrost-python>=0.6,<1   # Blockfrost API SDK
tenacity>=9,<10             # Retry/backoff decorator
python-dotenv>=1.2.1        # .env file loading
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
