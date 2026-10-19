"""Centralised configuration loaded from environment variables."""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from dotenv import load_dotenv

# Load .env file (overrides system env vars)
load_dotenv(override=True)


@dataclass(frozen=True)
class Config:
    """Application configuration from environment variables."""

    # Twitter OAuth 1.0a credentials
    api_key: str
    api_secret_key: str
    access_token: str
    access_token_secret: str

    # Blockfrost
    blockfrost_project_id: str

    # Blockfrost webhook
    blockfrost_webhook_auth_token: str | None

    # Feature flags
    tweet_posting_enabled: bool


@dataclass(frozen=True)
class Stablecoin:
    """A stablecoin monitored by the bot."""

    ticker: str
    policy_id: str
    asset_id: str
    decimals: int
    activation_timestamp: int | None = None
    min_post_amount: int = 0  # Whole tokens; applies equally to mints and burns.


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Load and cache configuration from environment variables."""

    def _get_required(key: str) -> str:
        value = os.environ.get(key)
        if not value:
            raise ValueError(f"Missing required environment variable: {key}")
        return value

    def _get_optional(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key, default)

    def _get_bool(key: str, default: bool = False) -> bool:
        return os.environ.get(key, "").lower() == "true" if os.environ.get(key) else default

    return Config(
        api_key=_get_required("API_KEY"),
        api_secret_key=_get_required("API_SECRET_KEY"),
        access_token=_get_required("ACCESS_TOKEN"),
        access_token_secret=_get_required("ACCESS_TOKEN_SECRET"),
        blockfrost_project_id=_get_required("BLOCKFROST_PROJECT_ID"),
        blockfrost_webhook_auth_token=_get_optional("BLOCKFROST_WEBHOOK_AUTH_TOKEN"),
        tweet_posting_enabled=_get_bool("TWEET_POSTING_ENABLED", default=False),
    )


USDM = Stablecoin(
    ticker="USDM",
    policy_id="c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad",
    asset_id="c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d",
    decimals=6,
)

# Fixed rollout boundary: 2026-10-19 00:00 Europe/Istanbul. Never advances on restart.
NEW_STABLECOINS_ACTIVATED_AT = int(datetime(2026, 10, 18, 21, tzinfo=UTC).timestamp())

USDCX = Stablecoin(
    ticker="USDCx",
    policy_id="1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e34",
    asset_id="1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e345553444378",
    decimals=6,
    activation_timestamp=NEW_STABLECOINS_ACTIVATED_AT,
    min_post_amount=10_000,
)

USDA = Stablecoin(
    ticker="USDA",
    policy_id="fe7c786ab321f41c654ef6c1af7b3250a613c24e4213e0425a7ae456",
    asset_id="fe7c786ab321f41c654ef6c1af7b3250a613c24e4213e0425a7ae45655534441",
    decimals=6,
    activation_timestamp=NEW_STABLECOINS_ACTIVATED_AT,
)

# Add stablecoins to this tuple to monitor them.
STABLECOINS = (USDM, USDCX, USDA)
