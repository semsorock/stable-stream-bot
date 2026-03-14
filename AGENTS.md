# USDM Stream Bot - AI Agent Instructions

## Project Purpose

This is a **Cardano blockchain USDM stablecoin monitoring bot** that watches for mint/burn transactions and posts summaries to Twitter/X. It's deployed as a Google Cloud Run service (FastAPI + uvicorn) triggered by Blockfrost block webhooks (`POST /`).

USDM is Mehen's fiat-backed USD stablecoin on Cardano (policy ID: `c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad`).

## Architecture Overview

### Core Data Flow

1. **Blockfrost webhook** → Cloud Run `/` endpoint
2. **Acknowledge the webhook quickly** to keep request latency low
3. **Query Cardano DB-Sync** (PostgreSQL) in background for USDM mint/burn transactions
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
│   ├── db/
│   │   ├── __init__.py
│   │   ├── queries.py           # SQL query constants
│   │   ├── repository.py        # Async data access layer (typed query functions)
│   │   └── ssh_tunnel.py        # SSH tunnel for bastion-host DB access (paramiko)
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

- `bot/db/repository.py`: Async data access functions returning typed model instances. Uses a shared `psycopg.AsyncConnection` (lazy init) with an `asyncio.Lock` to serialise queries.

- `bot/db/ssh_tunnel.py`: SSH port-forwarding tunnel using `paramiko` for accessing PostgreSQL through a bastion host.

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

## Database Integration

### DB-Sync Schema (PostgreSQL)

The bot queries a **Cardano DB-Sync instance** - a full blockchain database. Key tables:

- `ma_tx_mint`: Multi-asset mint/burn records with quantity (positive = mint, negative = burn)

- `multi_asset`: Asset metadata including policy and name

- `tx` / `block`: Transaction and block data

### Queries

All SQL is in `bot/db/queries.py`:

- `QUERY_MINTS`: Get USDM mint/burn transactions by block hash
- `QUERY_SUPPLY`: Calculate current total USDM supply

**Important**: Transactions are identified by `tx_hash` (hex-encoded).

## External Dependencies & Patterns

### External Links

The bot generates links to Cardanoscan via `bot/links.py`:

- **Cardanoscan**: `make_cardanoscan_link(tx_hash)` → `https://cardanoscan.io/transaction/{tx_hash}?tab=tokenmint`

## Development Patterns

### Error Handling & Resilience

- Uses `tenacity` library with `@retry` decorator (3 attempts, exponential backoff)

- Async PostgreSQL via `psycopg` (v3) with a shared `AsyncConnection` (lazy init, `autocommit=True`)

  - Managed in `bot/db/repository.py` — all queries go through async `_query()` helper

  - On connection errors, closes and resets connection so next call reconnects

### Environment Variables

Loaded from `.env` file (overrides system env vars) via `python-dotenv`.

```bash
# Required
DB_SYNC_URL                        # PostgreSQL connection string
BLOCKFROST_WEBHOOK_AUTH_TOKEN      # Blockfrost webhook HMAC secret

# Twitter (required if TWEET_POSTING_ENABLED=true)
API_KEY, API_SECRET_KEY            # Twitter OAuth 1.0a
ACCESS_TOKEN, ACCESS_TOKEN_SECRET  # Twitter access credentials
TWEET_POSTING_ENABLED              # "true" to enable tweet posting (default: false)

# SSH tunnel (optional — for accessing DB through bastion host)
SSH_HOST                           # Bastion host address
SSH_PORT                           # SSH port (default: 22)
SSH_USER                           # SSH username
SSH_KEY_PATH                       # Path to SSH private key file
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

1. **SQL Query Development**: Use `drafts/draft_SELECT.sql` to test queries against DB-Sync

2. **Webhook Simulation**: Sample payloads are in code comments in `bot/main.py`

3. **Local Testing**: Set environment variables, then POST webhook JSON to `/` (Blockfrost)

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
- **SSH tunnel**: Enabled when `SSH_HOST` is set in config
- **Webhook signature verification**: Skipped if `BLOCKFROST_WEBHOOK_AUTH_TOKEN` not set

## When Modifying Code

### Adding New Event Types

1. Add query constant to `bot/db/queries.py`

2. Add data model to `bot/models.py`

3. Add repository function to `bot/db/repository.py`

4. Add formatter function to `bot/twitter/formatter.py`

5. Add orchestration in `bot/main.py`

### Changing Tweet Format

- Modify formatter functions in `bot/twitter/formatter.py`

- Keep under 280 characters (Twitter limit not enforced in code)

- Use emoji consistently: 💵 (mint), 🔥 (burn), 🔗 (links)

### Database Query Changes

- Test queries in `drafts/draft_SELECT.sql` first

- Schema reference in `docs/db_sync_schema.sql`

- Always join through `tx` and `block` tables for block filtering

## Dependencies Summary

Managed via `uv` (see `pyproject.toml`). Lockfile: `uv.lock`.

```text
# Production
fastapi>=0.115,<1           # Async web framework
uvicorn[standard]>=0.34,<1  # ASGI server
xdk>=0.8.1                  # X API SDK (OAuth 1.0a + posts client)
psycopg[binary]>=3,<4       # Async PostgreSQL adapter (psycopg v3)
tenacity>=9,<10             # Retry/backoff decorator
python-dotenv>=1.2.1        # .env file loading
paramiko>=3                 # SSH tunnel for bastion-host DB access
# Dev
pytest>=8                   # Test runner
pytest-asyncio>=0.25        # Async test support (asyncio_mode = "auto")
httpx>=0.28                 # Async HTTP client (FastAPI TestClient)
ruff>=0.9                   # Formatter + linter
```

## Related Documentation

- [Cardano DB-Sync](https://github.com/IntersectMBO/cardano-db-sync): Database schema source

- [USDM Token](https://mehen.io/): Mehen fiat-backed stablecoin

- [Blockfrost Webhooks](https://blockfrost.dev/start-building/webhooks/webhooks-events): Webhook event format
