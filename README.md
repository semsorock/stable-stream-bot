# Stable Stream Bot

A Cardano monitoring bot that watches USDM, USDCx, and USDA for mint and burn transactions and posts summaries with token banners to Twitter/X.

## How It Works

```
Blockfrost Webhook (POST /) → FastAPI on Cloud Run → Immediate ack → Blockfrost API query → Post to X
```

1. **Blockfrost** sends block webhooks to `/`
2. The bot acknowledges the webhook quickly to keep request latency low
3. Background processing queries the **Blockfrost API** for configured stablecoin mint/burn events in that block
4. Summaries without hashtags are posted to **Twitter/X** via `xdk`, with a token-specific mint or burn banner

### Stablecoin Configuration

Stablecoins are configured in `bot/config.py`. All three use six decimals. USDCx
and USDA activate at **2026-10-19 00:00 Europe/Istanbul**
(`2026-10-18T21:00:00Z`). Events from earlier blocks are suppressed, including
replayed webhooks. The cutoff is fixed in config and does not change at midnight
or on restart. USDM retains its existing behavior. Block time is verified using
the Blockfrost API; timestamp lookup retries transient server and connection
errors before failing closed for new-token events. The production `tzdata`
dependency supplies timezone data when the base image has none.

USDCx posts require at least **10,000 tokens** per mint or burn event, including
exactly 10,000. Smaller events are skipped before supply lookup and posting.
Adjust `USDCX.min_post_amount` in `bot/config.py` to change the threshold; its
value is in whole tokens. USDM and USDA have no minimum post amount.

Add another `Stablecoin` entry to the `STABLECOINS` tuple with its ticker,
policy ID, full Blockfrost asset ID, decimal count, and a fixed activation
timestamp when historical events should be excluded:

```python
STABLECOINS = (
    USDM, USDCX, USDA,
    Stablecoin(
        ticker="ANOTHER",
        policy_id="...",
        asset_id="...",
        decimals=6,
        activation_timestamp=1792357200,  # Choose a fixed rollout date
        min_post_amount=0,  # Optional minimum in whole tokens, for mints and burns
    ),
)
```

For every configured stablecoin, the bot monitors both mints and burns.
New tokens need two 1200 × 400 PNGs named
`bot/assets/banners/{ticker.lower()}-minted.png` and `-burned.png`; tickers must
contain only ASCII letters and digits. No second runtime token registry needs
updating. `tests/test_media.py` checks banners for every configured token.
To regenerate artwork, add the source logo and renderer color as described below.
Protocols with pre-minted reserves need a custom detector.

Asset histories are fetched in 100-entry pages through the target block, so a
burst of small events cannot hide a qualifying event beyond the first page.
Pre-activation tokens are skipped before history lookup. If a later page or
boundary lookup fails, collected events are retained and incomplete history is
logged. Activation and amount filters run together before supply lookup.

### Post Images

Each post attaches one bundled **1200 × 400 PNG**, selected by token and action:
six images in `bot/assets/banners/` for the three tokens. Each shows the token's
official registry logo and ticker, plus a vertical MINTED or BURNED label.
The bot uploads the image and descriptive alt text using the existing OAuth
credentials, caches its media ID until X's upload expiry, then attaches it to
the post. Repeated posts reuse the uploaded banner while it remains valid.
Upload failures do not fall back to posts without images. Event processing is
isolated: a missing banner or failed post logs that event and continues with
other events in the block. A missing post ID is logged as an unknown outcome
and is not automatically retried. With `TWEET_POSTING_ENABLED=false`, neither
image uploads nor posts are sent.

See [banner sources and regeneration](bot/assets/README.md) for artwork provenance
and the optional `scripts/render_banners.py` development renderer. No image
library is needed in production; source logos and fonts are excluded from the
Docker context, leaving the six runtime banners.

## Prerequisites

- **Google Cloud Platform** account with a project
- **Blockfrost** account with a mainnet project ID and webhook configured
- **Twitter/X API** credentials (OAuth 1.0a user tokens; posting is disabled by default)

## Environment Variables

The bot loads `.env` locally (`python-dotenv`) and can also read from Cloud Run environment variables or Secret Manager. The current configuration loader requires the four Twitter credentials and the Blockfrost project ID even when tweet posting is disabled; the webhook authentication token is optional.

| Variable | Description |
|---|---|
| `API_KEY` | Twitter OAuth 1.0a consumer key |
| `API_SECRET_KEY` | Twitter OAuth 1.0a consumer secret |
| `ACCESS_TOKEN` | Twitter access token |
| `ACCESS_TOKEN_SECRET` | Twitter access token secret |
| `BLOCKFROST_PROJECT_ID` | Blockfrost mainnet project ID (e.g. `mainnetXXXX...`) |
| `BLOCKFROST_WEBHOOK_AUTH_TOKEN` | Optional shared secret used to verify `Blockfrost-Signature`; verification is skipped when omitted |
| `TWEET_POSTING_ENABLED` | Set to `true` to enable posting tweets (default: `false`) |

## Local Development

```bash
# Clone the repository
git clone https://github.com/semsorock/stable-stream-bot.git
cd stable-stream-bot

# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync dependencies (creates .venv automatically)
uv sync

# Configure environment variables (or copy from .env.example)
cp .env.example .env

# Run locally
uv run uvicorn bot.main:app --reload --port 8080
# Server starts at http://localhost:8080
# Endpoint: POST / (Blockfrost)

# Run tests
uv run pytest -v

# Format & lint
uv run ruff format .
uv run ruff check --fix .
```

### Local Docker Build

```bash
docker build -t stable-stream-bot .
docker run --rm -p 8080:8080 \
  -e API_KEY=your_key \
  -e API_SECRET_KEY=your_secret \
  -e ACCESS_TOKEN=your_token \
  -e ACCESS_TOKEN_SECRET=your_token_secret \
  -e BLOCKFROST_PROJECT_ID=mainnetXXXX \
  -e BLOCKFROST_WEBHOOK_AUTH_TOKEN=your_webhook_secret \
  -e TWEET_POSTING_ENABLED=false \
  stable-stream-bot
```

## Deployment

The bot is deployed to **Google Cloud Run** with continuous deployment from this GitHub repository.

`POST /` returns an accepted response immediately and continues block processing asynchronously inside the app process. This reduces webhook backpressure, but it is still an in-process handoff rather than an external durable queue.

### One-Time Setup

1. **Store secrets** in Google Secret Manager for your GCP project:
  - `api-key`, `api-secret-key`, `access-token`, `access-token-secret`
  - `blockfrost-project-id`, `blockfrost-webhook-auth-token`

2. **Create a Cloud Run service**:
   - Go to [Cloud Run Console](https://console.cloud.google.com/run)
   - Click **Create Service** → **Continuously deploy from a repository**
   - Connect to your GitHub repository, branch: `main`
   - Build type: **Dockerfile**
   - Configure environment variables to reference Secret Manager secrets
   - Allow unauthenticated invocations (required for Blockfrost webhooks)

3. **Configure Blockfrost webhook** to point to your Cloud Run service URL:
   - Block event webhook URL: `https://YOUR_SERVICE_URL/`

### How Deployments Work

Every push to the `main` branch automatically triggers:
1. Cloud Build builds a new Docker image from the `Dockerfile`
2. Cloud Run deploys the new revision
3. Traffic shifts to the new revision once healthy

## Project Structure

```
├── main.py                      # Entry point shim (re-exports FastAPI app)
├── bot/
│   ├── config.py                # Environment and monitored stablecoin config
│   ├── assets/                  # Bundled token logos and mint/burn banners
│   ├── links.py                 # External link builders (Cardano Explorer)
│   ├── logging.py               # Structured logging setup
│   ├── main.py                  # FastAPI app + fast-ack webhook handler
│   ├── models.py                # Domain dataclasses
│   ├── webhook_auth.py          # Blockfrost HMAC signature verification
│   ├── blockfrost_client.py     # Blockfrost API queries (mint/burn detection + supply)
│   └── twitter/
│       ├── client.py            # XDK posting client
│       ├── formatter.py         # Tweet composition logic
│       ├── media.py             # Token/action banner selection
│       └── templates.py         # Editable tweet templates
├── tests/                       # Pytest test suite
├── pyproject.toml               # Dependency/tool config
├── uv.lock                      # Locked dependency versions
├── Dockerfile                   # Container image
└── .env.example                 # Environment variable template
```

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
