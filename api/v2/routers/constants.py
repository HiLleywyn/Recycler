"""
GET /api/v2/constants  -  public endpoint exposing business constants to the frontend.
"""
from __future__ import annotations

from fastapi import APIRouter

from constants import validators, trading, economy, games

router = APIRouter(prefix="/constants", tags=["constants"])


@router.get("")
async def get_constants() -> dict:
    """Return public business constants for the frontend."""
    return {
        "validators": {
            "max_slash_count": validators.MAX_SLASH_COUNT,
            "min_stake": validators.MIN_STAKE,
            "stake_lock_secs": validators.STAKE_LOCK_SECS,
            "delegation_lock_secs": validators.DELEGATION_LOCK_SECS,
            "min_delegation": validators.MIN_DELEGATION,
            "max_delegations": validators.MAX_DELEGATIONS,
            "gas_tiers": validators.GAS_TIERS,
            "validator_reward_pct": validators.VALIDATOR_REWARD,
            "treasury_cut_pct": validators.TREASURY_CUT,
        },
        "trading": {
            "default_swap_fee": trading.DEFAULT_SWAP_FEE,
            "slippage_warn": trading.SLIPPAGE_WARN,
            "min_trade_usd": trading.MIN_TRADE_USD,
            "usd_precision": trading.USD_PRECISION,
            "token_precision": trading.TOKEN_PRECISION,
        },
        "games": {
            "mines_total_tiles": games.MINES_TOTAL_TILES,
            "mines_default_bombs": games.MINES_DEFAULT_BOMBS,
            "mines_min_bombs": games.MINES_MIN_BOMBS,
            "mines_max_bombs": games.MINES_MAX_BOMBS,
        },
        "economy": {
            "chain_switch_cooldown": economy.CHAIN_SWITCH_COOLDOWN,
            "ws_heartbeat_interval": economy.WS_HEARTBEAT_INTERVAL,
        },
    }
