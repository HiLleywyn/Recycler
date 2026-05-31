"""
PoS validator constants  -  block production, slashing, delegation, gas, mempool.
"""
from __future__ import annotations

VALIDATOR_TICK: int = 120
VALIDATOR_REWARD: float = 0.90
TREASURY_CUT: float = 0.10
MIN_STAKE: float = 100.0
MIN_VALIDATORS: int = 2
STAKE_LOCK_SECS: int = 86_400
MAX_SLASH_COUNT: int = 5
SLASH_RATE: float = 0.05
SLASH_DECAY_SECS: int = 604_800
MAX_MEMPOOL: int = 50
DELEGATION_VALIDATOR_KEEP: float = 0.80
DELEGATION_POOL_SHARE: float = 0.20
DELEGATION_LOCK_SECS: int = 86_400
MIN_DELEGATION: float = 50.0
MAX_DELEGATIONS: int = 3
REJECTION_SLASH_RATE: float = 0.01
GAS_TIERS: dict[str, float] = {"high": 0.50, "medium": 0.20, "low": 0.05}
GAS_MIN_MULT: float = 0.1
GAS_MAX_MULT: float = 100.0
NET_SHORT: dict[str, str] = {
    "Sun Network": "sun",
    "Moneta Chain": "mta",
    "Arcadia Network": "arc",
    "Discoin Network": "dsc",
    # Bridged pseudo-network shared by all group tokens so they can swap
    # freely across the chains their founders pair them with. Group tokens
    # still have a mining chain (mining_groups.token_network) for vault-pool
    # pairing and block rewards, but for trading/AMM purposes they all live
    # on the same logical network so cross-group partnership pools work.
    "Moon Network": "moon",
    # Lure Network: fishing-only earn economy. LURE (token) and REEL
    # (coin) live here. Both are in Config.EARN_ONLY_TOKENS so .buy /
    # .swap / LP creation are all blocked from outside; the only inflows
    # are ,fish casts (LURE) and burn-swap or staking of LURE (REEL).
    "Lure Network": "lur",
    # Crypt Network: dungeon-only earn economy. COPPER / SILVER / GOLD
    # are mined ore tiers, RUNE is the network coin. All four live in
    # Config.EARN_ONLY_TOKENS so .buy / .swap / LP creation are blocked
    # from outside; the only inflows are ,delve mine (ore) and burn-swap
    # or ore-stake (RUNE). RUNE -> USD cashout closes the loop.
    "Crypt Network": "cry",
    # Buddy Network: companion-economy network. BUD is the network coin,
    # FREN is the staking token. Both EARN_ONLY -- BUD inflows are FREN
    # stake-yield, BUD ↔ FREN burn-swap, and BUD ↔ {REEL, RUNE, MOON}
    # carve-out swaps. Buddy Market + Buddy Shop both denominate in BUD.
    "Buddy Network": "bud",
    "Harvest Network": "har",
    # Forge Network: crafting-economy network. FORGE is the network coin
    # (swappable, oracle-priced). FGD is the network's stablecoin used to
    # price crafted items in shop listings. INGOT is the earn-only token
    # awarded for every successful craft -- mirrors the LURE/SEED/COPPER
    # role on their respective earn-only networks. INGOT and FORGE both
    # live in EARN_ONLY_TOKENS so .buy / .swap / LP are blocked from
    # outside; the only inflows are ,craft make (INGOT) and burn-swap or
    # ingot-stake (FORGE). FORGE -> USD cashout closes the loop.
    "Forge Network": "fge",
    # Gamba Network: gambling-economy network. GBC is the network coin and
    # the eight game-themed tokens (GAMBIT/CROWN/VEIN/PIP/EDGE/ACE/NOIR/
    # CHERRY) mint on game wins. All nine live in EARN_ONLY_TOKENS; .buy /
    # .swap / LP creation are blocked from outside. Inflows come from gamba
    # game wins (game tokens) and from staking game tokens (GBC). The only
    # USD off-ramp is ,gamba cashout that burns GBC. Players hold these in
    # their DeFi wallet on the "gam" network short, same as every other
    # earn-only network coin.
    "Gamba Network": "gam",
    # Sage Network: crypto learn-and-earn economy. SAGE is the network coin
    # and EDU is the game token minted on correct answers in the three
    # Sage games (,pattern / ,gauge / ,tknom). Both live in EARN_ONLY_TOKENS
    # so .buy / .swap / LP creation are blocked from outside; inflows come
    # from correct answers (EDU + small SAGE drip) and from staking EDU
    # (SAGE drip). The only USD off-ramp is ,sage cashout that burns SAGE.
    "Sage Network": "sag",
}
