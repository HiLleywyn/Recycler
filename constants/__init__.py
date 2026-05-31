"""
constants  -  Single source of truth for all Discoin business constants.

Pure Python. No framework, Discord, or database imports.
Designed to be extractable as `discoin-core` in the future.

Usage:
    from constants.validators import MAX_SLASH_COUNT
    from constants.trading import DEFAULT_SWAP_FEE
    from constants.economy import CHAIN_SWITCH_COOLDOWN

Or for quick access:
    from constants import validators, trading, economy, games, ui
"""
from constants import validators, trading, economy, games, ui, security  # noqa: F401
