"""Token identities and rollout configuration."""

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import NEW_STABLECOINS_ACTIVATED_AT, STABLECOINS, USDA, USDCX, USDM


def test_stablecoin_registry():
    """Mainnet asset IDs include the authentic token name and six decimals."""
    assert STABLECOINS == (USDM, USDCX, USDA)
    assert len({coin.asset_id for coin in STABLECOINS}) == 3
    assert [bytes.fromhex(coin.asset_id[56:]).decode() for coin in (USDCX, USDA)] == [
        "USDCx",
        "USDA",
    ]
    assert all(coin.asset_id.startswith(coin.policy_id) and coin.decimals == 6 for coin in STABLECOINS)


def test_new_token_rollout_is_fixed_istanbul_midnight():
    """Rollout never moves with wall clock time or application restarts."""
    rollout = datetime.fromtimestamp(NEW_STABLECOINS_ACTIVATED_AT, tz=ZoneInfo("Europe/Istanbul"))
    assert (rollout.year, rollout.month, rollout.day, rollout.hour, rollout.minute, rollout.second) == (
        2026,
        10,
        19,
        0,
        0,
        0,
    )
    assert all(coin.activation_timestamp == NEW_STABLECOINS_ACTIVATED_AT for coin in (USDCX, USDA))
    assert USDM.activation_timestamp is None


def test_only_usdcx_has_a_posting_minimum():
    """The initial 10,000-token noise filter applies only to USDCx."""
    assert USDCX.min_post_amount == 10_000
    assert USDM.min_post_amount == USDA.min_post_amount == 0
