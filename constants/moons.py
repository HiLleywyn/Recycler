"""
Moons (MOON) economy constants for Moon Network.

MOON is the native yield token of the bridged Moon Network. Slice 1 adds the
Lunar Mint: players stake a group token (CAT, COOK, FEM, ...) into the mint
and earn MOON on an hourly tick. All knobs live here so emission, caps, and
safeguards can be tuned in one place without touching the cog.

See :mod:`cogs.moons` for the tick loop and commands, and
:mod:`database.moons` for the DB helpers that read/write ``lunar_stakes``.
"""
from __future__ import annotations

# ── Emission ─────────────────────────────────────────────────────────────────
# Daily nominal emission as a fraction of the stake's USD value (TWAP). At
# 0.02 a $100 stake earns ~2 MOON/day pre-caps; at MOON $0.50 that is
# ~$1/day. Bumped from 0.008 to make Lunar Mint actually feel worth staking --
# per-user / per-guild caps still prevent runaway emission.
MOON_EMISSION_RATE: float = 0.02

# Maximum activity bonus applied on top of the base emission. Active groups
# earn up to +50% more MOON for their stakers than zombie groups. Bumped
# from +25% so the reward for running a lively group is meaningful.
GROUP_ACTIVITY_BONUS_MAX: float = 0.50

# Minimum activity floor for the full bonus: >= N distinct miners AND >=
# M blocks in the last 24h. Below the floor the bonus scales linearly.
GROUP_ACTIVITY_MIN_MINERS: int = 3
GROUP_ACTIVITY_MIN_BLOCKS: int = 2
GROUP_ACTIVITY_WINDOW_SECS: int = 86_400  # 24h

# ── Caps ─────────────────────────────────────────────────────────────────────
# Per-user daily MOON emission cap across all that user's Lunar Mint
# positions in a guild. Blunts alt-account farming and whale dominance.
# Bumped from 500 to 1500 so the cap does not strangle the bumped emission.
PER_USER_DAILY_MOON_CAP: float = 1500.0

# Per-guild daily MOON emission cap. Stops a single guild minting unbounded
# MOON into the shared Moon Network supply. Bumped from 10k to 25k to match
# the higher per-user cap.
PER_GUILD_DAILY_MOON_CAP: float = 25_000.0

# Number of seconds covered by the per-user / per-guild cap window. 24h.
CAP_WINDOW_SECS: int = 86_400

# ── Oracle ───────────────────────────────────────────────────────────────────
# TWAP window (in 1-minute price candles) used to value staked group tokens
# for emission. 1440 = 24h. Guards against one-trade price pumps that would
# let a whale farm MOON at an inflated valuation and dump.
MOON_TWAP_WINDOW: int = 1440

# ── Early unstake ────────────────────────────────────────────────────────────
# Mirrors the existing NPC-stake pattern (Config.STAKING_EARLY_UNSTAKE_*).
# Both values are overridable by a guild-wide setting if we ever need per-
# server tuning; for now we reuse the network-wide defaults from core/config.py.
# Lunar Mint unstakes within this window burn this fraction of the principal.

# ── Token identity ───────────────────────────────────────────────────────────
MOON_SYMBOL: str = "MOON"
MOON_NETWORK: str = "Moon Network"
MOON_NETWORK_SHORT: str = "moon"

# ── Wrapped mining-coin mapping ─────────────────────────────────────────────
# Group-token trading pools live on Moon Network and pair against synthetic
# 1:1 wrappers of the underlying PoW coin (mMTA for MTA, mSUN for SUN) --
# users mint them by running ``.moon wrap mta <amt>`` or ``.moon wrap sun
# <amt>``. The dict is the single source of truth so cogs don't hard-code
# the mapping; ``wrapped_coin("MTA") -> "mMTA"``.
WRAPPED_FOR_NATIVE: dict[str, str] = {
    "MTA": "MMTA",
    "SUN": "MSUN",
}
NATIVE_FOR_WRAPPED: dict[str, str] = {v: k for k, v in WRAPPED_FOR_NATIVE.items()}


def wrapped_coin(native_sym: str) -> str:
    """Return the Moon Network wrapped symbol for a native mining coin.

    Falls back to the input unchanged if the coin has no wrapper defined
    (e.g. mining chains without Moon-Network pairing), so callers can
    use the result blindly when building pool ids.
    """
    return WRAPPED_FOR_NATIVE.get(native_sym.upper(), native_sym.upper())


def native_coin_for_wrapped(wrapped_sym: str) -> str | None:
    """Inverse of ``wrapped_coin``: mMTA -> MTA, mSUN -> SUN, else None."""
    return NATIVE_FOR_WRAPPED.get(wrapped_sym)

# ── Tier 2: Moon Pool (MOON stake -> DSD real yield)  [Slice 2] ──────────────
# Fraction of every new Moon Network vault inflow that is re-earmarked as
# distributable to MOON stakers. The rest continues to feed the vault
# level-threshold progression in constants/vaults.py so the existing vault
# levelling UX is untouched. A 25% split keeps level progression visible
# while still giving stakers a steady stream of real yield. Bumped from 25%
# to 50% so Moon Pool stakers get a meaningful slice of Moon Network volume.
LUNAR_VAULT_SHARE: float = 0.50

# Hourly drip of the Moon Network vault's distributable balance. 1/96 pays
# it out over 4 days (down from 7) so stakers see responsive yield without
# letting a single whale swap empty the pool instantly; buyers of MOON in
# anticipation of the next drip still get paid.
HOURLY_DRIP_FRACTION: float = 1.0 / 96.0

# Minimum MOON that must be staked into the Moon Pool. Stops dust stakes
# from spamming the distribution loop. 10 MOON at $0.50 = $5 minimum.
MOON_POOL_MIN_STAKE: float = 10.0

# Moon Pool yield payout basket.
#
# Tier 2 pays stakers a little bit of each network's native tradeable coin
# instead of a stablecoin. Drip budget is the same USD value as before (1/96
# of the Moon Network vault's distributable balance per hour); that value is
# split equally across the basket, then converted to each symbol via the
# per-guild crypto_prices row. MOON itself is NOT in the basket -- staking
# MOON does not print more MOON, keeping Tier 2 a pure revenue share with
# no inflation loop on the yield token.
#
# Each entry is (symbol, network_short_key). Networks must match the ones
# used by update_wallet_holding so the credit lands in the right wallet.
MOON_POOL_YIELD_BASKET: tuple[tuple[str, str], ...] = (
    ("MTA", "mta"),
    ("ARC", "arc"),
    ("DSC", "dsc"),
    ("SUN", "sun"),
)

# ── Slice 3: Vault-level emission bonus ──────────────────────────────────────
# Moon Network's network_vaults.level (0..15) scales Tier 1 emission as a
# reward for servers that actively trade on Moon Network. +3% per level
# capped at +50% (bumped from +2% / +30%). Stacks multiplicatively with
# the per-group activity bonus, so an active group on a mature Moon Network
# earns up to 1.5 * 1.5 = 2.25x the base emission. Per-user / per-guild caps
# still hold the final ceiling.
VAULT_LEVEL_EMISSION_BONUS: float = 0.03
VAULT_LEVEL_EMISSION_BONUS_MAX: float = 0.50

# ── Burn-to-Basket fee (gas-like) ────────────────────────────────────────────
# `,moon burn <amt>` destroys MOON in exchange for a USD-equal slice of every
# Moon Network group token. The fee is taken in MOON itself (extra burn on top
# of the basket conversion) and represents the on-chain "gas" cost of the
# atomic burn-and-mint. Burned MOON is destroyed -- it does NOT route to the
# Moon Network vault, so the fee is purely deflationary. 0.5% mirrors the
# contract-level burn rates on other tokens.
MOON_BURN_FEE_PCT: float = 0.005


# ── Moon gas (per-action MOON network fee)  [overhaul] ──────────────────────
# Every Moon Network interaction charges a flat MOON fee. MOON_GAS_BURN_PCT of
# the fee is burned (destroyed -- decrements MOON circulating supply); the
# remainder is credited to the Moon Network vault's distributable bucket,
# feeding Moon Pool yield. ``wrap`` is intentionally free: it is the network
# on-ramp, and a player holding 0 MOON must still be able to wrap their way
# in. Keys are action names passed to services.moon_gas.charge_gas().
MOON_GAS_COSTS: dict[str, float] = {
    "wrap":        0.0,    # on-ramp -- always free
    "unwrap":      0.25,
    "swap":        0.50,
    "stake":       0.25,
    "unstake":     0.25,
    "claim":       0.10,
    "pool_add":    0.50,
    "pool_remove": 0.50,
    "burn":        0.10,
}
MOON_GAS_DEFAULT: float = 0.25     # fallback cost for any unlisted action
MOON_GAS_BURN_PCT: float = 0.60    # 60% of every gas fee is burned, 40% to vault


# ── Wrapped-asset dual-yield staking (mMTA / mSUN)  [overhaul] ──────────────
# Stake mMTA -> earn mMTA + MOON; stake mSUN -> earn mSUN + MOON. Both legs
# are freshly emitted and capped. Rates are daily-nominal fractions of the
# staked USD value (same shape as MOON_EMISSION_RATE), accrued hourly into a
# pending bucket and released by ,moon stake claim.
#
# Deflation: every stake / claim / unstake on a wrapped position pays MOON
# gas (see MOON_GAS_*), 60% of which is burned. The MOON emission leg below
# is sized so that, for a normally-active staker, the MOON burned via gas
# over a staking cycle outweighs the MOON minted -- the network trends
# deflationary. ,moon supply surfaces emitted-vs-burned so this is tunable.
WRAPPED_STAKE_SELF_RATE: float = 0.010   # daily emission of the staked asset
WRAPPED_STAKE_MOON_RATE: float = 0.012   # daily MOON emission on a wrapped stake
WRAPPED_STAKE_WARMUP_SECS: int = 43_200  # 12h linear warmup ramp (mirrors Tier 1)

# Per-user / per-guild daily caps on the MOON leg of wrapped-stake emission.
WRAPPED_STAKE_USER_MOON_CAP: float = 750.0
WRAPPED_STAKE_GUILD_MOON_CAP: float = 12_000.0

# Minimum stake per wrapped asset (human units). mMTA tracks MTA (~$60k) so
# its minimum is tiny; mSUN tracks SUN.
WRAPPED_STAKE_MIN: dict[str, float] = {"MMTA": 0.0005, "MSUN": 1.0}

# Symbols that can be staked in the wrapped-asset tier.
WRAPPED_STAKE_SYMBOLS: tuple[str, ...] = ("MMTA", "MSUN")

