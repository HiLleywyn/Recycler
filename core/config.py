import json as _json
import logging
import os
from dotenv import load_dotenv

from configs.items_config import SHOP_ITEMS as _SHOP_ITEMS, GROUP_HALL_UPGRADES as _GROUP_HALL_UPGRADES

_S = 10 ** 18  # canonical scale factor -- 1 human USD/token = _S raw units

# Merge any admin runtime overrides on top of the file defaults.
# shop_runtime.json is written by the admin dashboard and gitignored.
_SHOP_ITEMS = dict(_SHOP_ITEMS)  # shallow copy so we don't mutate items_config
try:
    with open(os.path.join(os.path.dirname(__file__), "shop_runtime.json")) as _f:
        for _k, _v in _json.load(_f).items():
            _SHOP_ITEMS[_k] = _v
except FileNotFoundError:
    pass

load_dotenv(override=False)  # never override vars already set in the environment (Railway, Docker)

log = logging.getLogger("discoin.config")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("Invalid integer for %s=%r; using default %r", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("Invalid float for %s=%r; using default %r", name, raw, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    norm = raw.strip().lower()
    if norm in {"1", "true", "yes", "on"}:
        return True
    if norm in {"0", "false", "no", "off"}:
        return False
    log.warning("Invalid boolean for %s=%r; using default %r", name, raw, default)
    return default


def _env_first_int(*names: str, default: int | None = None) -> int | None:
    for name in names:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.warning("Invalid integer for %s=%r; ignoring it", name, raw)
    return default


class Config:
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
    # Auto-fix target repo. Set both to enable AI-authored PRs from
    # ,admin reports autofix / report auto-diagnose. Both blank = the
    # whole feature stays inert at startup, no PRs ever opened.
    AUTOFIX_REPO_OWNER: str = os.getenv("AUTOFIX_REPO_OWNER", "")
    AUTOFIX_REPO_NAME:  str = os.getenv("AUTOFIX_REPO_NAME",  "")
    AUTOFIX_BASE_BRANCH: str = os.getenv("AUTOFIX_BASE_BRANCH", "main")
    COMMUNITY_RESERVE_USER_ID: int = 0  # Discord IDs are never 0  -  safe sentinel for protocol reserve
    REPORT_TARGET_USER_ID: int = _env_int("REPORT_TARGET_USER_ID", 0)
    REPORT_DM_CLEANUP_DAYS: int = _env_int("REPORT_DM_CLEANUP_DAYS", 7)  # delete closed report DMs after N days

    # ── Clanktank containment system ─────────────────────────────
    # Channel where CLANKER users are visible and Disco ambience is active.
    CLANKTANK_CHANNEL_ID: int = _env_int("CLANKTANK_CHANNEL_ID", 0)
    # Mod/staff log channel for clanker add/remove/escape events.
    CLANKTANK_LOG_CHANNEL_ID: int = _env_int("CLANKTANK_LOG_CHANNEL_ID", 0)
    # Discord role ID for the "Clanker" containment role. ID lookup is
    # preferred; name-based fallback ("Clanker") is used when this is 0.
    CLANKER_ROLE_ID: int = _env_int("CLANKER_ROLE_ID", 0)
    # Pre-existing public thread where all escape room cases are posted.
    # Create the thread manually, copy its ID, set this var. Required for escape room.
    CLANK_ESCAPE_THREAD_ID: int = _env_int("CLANK_ESCAPE_THREAD_ID", 0)
    # Minutes the inmate must sit in the Mandatory Reflection waiting room.
    CLANK_ESCAPE_WAIT_MINUTES: int = _env_int("CLANK_ESCAPE_WAIT_MINUTES", 8)
    TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    PREFIX: str = os.getenv("PREFIX", "$")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://discoin:discoin@localhost:5432/discoin")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
    TX_SALT: str = os.getenv("TX_SALT", "econbot-default-salt")

    # ── JWT ─────────────────────────────────────────────────────
    # Discord OAuth vars (CLIENT_ID, CLIENT_SECRET, REDIRECT_URI) live in api/v2/config.py.
    JWT_SECRET:            str = os.getenv("JWT_SECRET", "change-me-in-production")

    # ── Premium / Multi-tenant ───────────────────────────────
    # Discoin runs as a single shared bot. The host guild (the one we
    # operate ourselves) is auto-unlocked for every premium feature; all
    # other guilds need a paid subscription or an admin grant. Defaults
    # to the operator's home server (1467740704725012638); override per
    # deployment with the HOST_GUILD_ID env var.
    HOST_GUILD_ID: int = _env_int("HOST_GUILD_ID", 1467740704725012638)
    # Additional dev / staging guilds that get the same auto-unlocked
    # treatment as HOST_GUILD_ID (every premium feature on, no DB row
    # required, every gated command available). Comma-separated list
    # via the DEV_GUILD_IDS env var; defaults bake in the operator's
    # personal dev server so a fresh deploy can test new features
    # without paying premium to itself.
    DEV_GUILD_IDS: frozenset = frozenset(
        {int(s) for s in (
            os.getenv("DEV_GUILD_IDS", "1478968538579337460").split(",")
        ) if s.strip().lstrip("-").isdigit()}
    )
    # Bot owner (allowed to run ,admin premium grant for arbitrary guilds
    # from the host server). Falls back to Discord application owner.
    BOT_OWNER_ID:  int = _env_int("BOT_OWNER_ID", 0)
    # Optional trial granted automatically the first time a guild runs a
    # gated command. 0 = no trial.
    PREMIUM_TRIAL_DAYS: int = _env_int("PREMIUM_TRIAL_DAYS", 0)

    # ── PayPal Subscriptions (per-guild premium) ─────────────
    # Sandbox: https://developer.paypal.com/dashboard/applications/sandbox
    # Set PAYPAL_MODE=live to switch to production once tested.
    PAYPAL_MODE:           str = os.getenv("PAYPAL_MODE", "sandbox")
    PAYPAL_CLIENT_ID:      str = os.getenv("PAYPAL_CLIENT_ID", "")
    PAYPAL_CLIENT_SECRET:  str = os.getenv("PAYPAL_CLIENT_SECRET", "")
    PAYPAL_WEBHOOK_ID:     str = os.getenv("PAYPAL_WEBHOOK_ID", "")
    PAYPAL_PLAN_ID_MONTHLY: str = os.getenv("PAYPAL_PLAN_ID_MONTHLY", "")
    PAYPAL_PLAN_ID_YEARLY:  str = os.getenv("PAYPAL_PLAN_ID_YEARLY", "")
    # Where PayPal sends the user after approve / cancel. The {gid} token
    # is replaced server-side so a single value works for every guild.
    PAYPAL_RETURN_URL:     str = os.getenv("PAYPAL_RETURN_URL", "")
    PAYPAL_CANCEL_URL:     str = os.getenv("PAYPAL_CANCEL_URL", "")
    # Public price strings shown to users in ,premium info. Pure UI; PayPal
    # is the source of truth for what the customer is actually charged.
    PREMIUM_PRICE_MONTHLY_DISPLAY: str = os.getenv("PREMIUM_PRICE_MONTHLY_DISPLAY", "$5/mo")
    PREMIUM_PRICE_YEARLY_DISPLAY:  str = os.getenv("PREMIUM_PRICE_YEARLY_DISPLAY",  "$50/yr")

    # ── Economy ──────────────────────────────────────────────
    STARTING_BALANCE: int = int(_env_float("STARTING_BALANCE", 20.0) * _S)
    DAILY_AMOUNT: int = int(_env_float("DAILY_AMOUNT", 150.0) * _S)
    DAILY_STREAK_BONUS: int = int(3.0 * _S)
    DAILY_MAX_STREAK: int = 365
    DAILY_COOLDOWN: int = 86400   # exactly 24 hours (UTC)

    # ── Work ─────────────────────────────────────────────────
    WORK_COOLDOWN: int = _env_int("WORK_COOLDOWN", 900)

    # ── Gambling ─────────────────────────────────────────────
    MIN_BET: int = int(1.0 * _S)
    MAX_BET = None  # No upper limit on bets
    MAX_LEVERAGE: int = 10

    # ── Drops ────────────────────────────────────────────────
    AUTO_DROP_INTERVAL: int = _env_int("AUTO_DROP_INTERVAL", 1800)
    DROP_MIN: float = _env_float("DROP_MIN", 100.0)
    DROP_MAX: float = _env_float("DROP_MAX", 2000.0)
    DROP_COLLECT_WINDOW: int = _env_int("DROP_COLLECT_WINDOW", 30)

    # ── Crypto / Price Engine ─────────────────────────────────
    PRICE_TICK_SECONDS: int = 15           # GBM drift tick interval
    MM_TRADE_INTERVAL: tuple = (120, 300)  # random seconds between MM trades

    # Auto-pump scheduler. Roughly once per hour the admin cog picks a
    # random eligible non-stable token in each crypto-enabled guild, rolls
    # a random chart pattern + magnitude + duration, and writes an event
    # into ``_admin_price_events`` -- the same dict ``,admin pump`` uses.
    # The price tick keeps the chart on-pattern while the event is live;
    # ``.buy`` / ``.sell`` / pool swaps continue to apply per-trade impact
    # and slippage on top of the pumped oracle, so the firewall stays up.
    AUTO_PUMP_ENABLED: bool = True
    AUTO_PUMP_INTERVAL_MIN_S: float = 60.0     # 1 minute lower bound
    AUTO_PUMP_INTERVAL_MAX_S: float = 3600.0   # 60 minutes upper bound
    # ── Tokens ────────────────────────────────────────────────
    # Single source of truth for all tradeable assets.
    # consensus: "PoW" | "PoS" | "Fiat"
    # network: network name or None (orphan/independent)
    # stakeable: True = primary staking token for that network
    # mineable: True = earned via PoW mining
    TOKENS: dict = {
        # ══════════════════════════════════════════════════════════════════════
        # Configurable token fields:
        #   name, emoji         -  display strings
        #   consensus           -  "PoW" | "PoS" | "Fiat"
        #   network             -  network name string or None
        #   start_price         -  genesis oracle price (USD) for fresh economies
        #   daily_vol           -  daily price volatility coefficient (0.0 - 0.15)
        #   stakeable           -  True = primary staking token for its network
        #   mineable            -  True = earned via PoW block reward
        #   stablecoin          -  True = price-pegged to $1 USD
        #   max_supply          -  hard cap on total tokens (int)
        #   decimals            -  decimal precision (8 for MTA-style, 18 for EVM)
        #   tx_fee_rate         -  % fee deducted on every transfer (e.g. 0.001 = 0.1%)
        #   gas_fee             -  reference base gas cost per tx in USD (display/seed)
        #   burn_rate           -  fraction of tx value permanently burned (0.0 = no burn)
        # ══════════════════════════════════════════════════════════════════════

        # ── Moneta Chain (PoW · single-coin) ─────────────────────────────
        # Genesis starting conditions. No stablecoin. No yield token.
        "MTA": {
            "name": "Moneta",      "emoji": "🟡",
            "consensus": "PoW",     "network": "Moneta Chain",
            "start_price": 0.10,    "daily_vol": 0.04,
            "stakeable": False,     "mineable": True,
            "max_supply": 21_000_000,
            "decimals": 8,
            "tx_fee_rate": 0.0005,  # 0.05 %
            "gas_fee": 0.50,
            "burn_rate": 0.001,     # 0.1 % of every trade permanently burned
        },

        # ── Sun Network (PoW · single-coin · mirrors MTA) ───────────────────
        # Same scarcity model as Moneta. Lower barrier to entry.
        "SUN": {
            "name": "Sun",          "emoji": "☀",
            "consensus": "PoW",     "network": "Sun Network",
            "start_price": 0.01,    "daily_vol": 0.05,
            "stakeable": False,     "mineable": True,
            "max_supply": 21_000_000,
            "decimals": 8,
            "tx_fee_rate": 0.001,   # 0.1 %  -  2x MTA (game-network tax, accessible mining)
            "gas_fee": 0.01,
            "burn_rate": 0.002,     # 0.2 % of every trade permanently burned
        },

        # ── Arcadia Network (PoS · 3-token) ────────────────────────────────
        # ARC (stake) + USDC (stable) + VTR (yield/DeFi)
        "ARC": {
            "name": "Arcadia",     "emoji": "🔵",
            "consensus": "PoS",     "network": "Arcadia Network",
            "start_price": 0.31,    "daily_vol": 0.05,
            "stakeable": True,      "mineable": False,
            "max_supply": 120_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,   # 0.1 %
            "gas_fee": 0.50,
            "burn_rate": 0.002,     # protocol-level burn  -  0.2 % of tx value burned
        },
        "USDC": {
            "name": "USD Coin",     "emoji": "💲",
            "consensus": "Fiat",    "network": "Arcadia Network",
            "start_price": 1.0,     "daily_vol": 0.0,
            "stakeable": False,     "mineable": False,
            "stablecoin": True,
            "max_supply": 50_000_000_000,
            "decimals": 6,
            "tx_fee_rate": 0.0005,  # 0.05 %
            "gas_fee": 0.10,
            "burn_rate": 0.0,       # stablecoin  -  no burn
        },
        "VTR": {
            "name": "Vantor",         "emoji": "🟣",
            "consensus": "PoS",     "network": "Arcadia Network",
            "start_price": 250.0,   "daily_vol": 0.08,
            "stakeable": False,     "mineable": False,
            "max_supply": 16_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,   # 0.1 %
            "gas_fee": 0.50,
            "burn_rate": 0.001,     # protocol-level burn  -  0.1 %
        },
        "STR": {
            # Arcadia-side meme token. Tiny unit price, deep supply, max volatility.
            # Deliberately NOT stakeable / mineable / buyable-with-USD -- lives
            # only in AMM pools so degens have to swap into it.
            "name": "Stratum",         "emoji": "⭐",
            "consensus": "PoS",     "network": "Arcadia Network",
            "start_price": 0.0001,  "daily_vol": 0.14,
            "stakeable": False,     "mineable": False,
            "max_supply": 100_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.002,   # 0.2 %  -  meme tax
            "gas_fee": 0.50,
            "burn_rate": 0.005,     # 0.5 %  -  meme burn, keeps supply twitchy
        },

        # ── Discoin Network (PoS · 3-token · mirrors Arcadia) ──────────────
        # DSC (stake) + DSD (stable) + DSY (yield)
        # Same structure as Arcadia; tighter supply, slightly higher burn,
        # cheaper gas  -  the game's native network.
        "DSC": {
            "name": "Discoin",      "emoji": "🪙",
            "consensus": "PoS",     "network": "Discoin Network",
            "start_price": 0.05,    "daily_vol": 0.06,
            "stakeable": True,      "mineable": False,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,   # 0.1 %  -  same as ARC
            "gas_fee": 0.05,        # 10x cheaper gas than Arcadia
            "burn_rate": 0.003,     # 0.3 %  -  more aggressive burn than ARC
        },
        "DSD": {
            "name": "Disdollar",    "emoji": "💵",
            "consensus": "Fiat",    "network": "Discoin Network",
            "start_price": 1.0,     "daily_vol": 0.0,
            "stakeable": False,     "mineable": False,
            "stablecoin": True,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.0005,  # 0.05 %
            "gas_fee": 0.01,
            "burn_rate": 0.0,       # stablecoin  -  no burn
        },
        "DSY": {
            "name": "Disyield",     "emoji": "📈",
            "consensus": "PoS",     "network": "Discoin Network",
            "start_price": 5.0,     "daily_vol": 0.07,
            "stakeable": False,     "mineable": False,
            "max_supply": 50_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,   # 0.1 %
            "gas_fee": 0.05,
            "burn_rate": 0.0015,    # 0.15 %  -  slightly higher than VTR
        },
        "DEGEN": {
            # Discoin-side meme / casino token. Highest permitted volatility,
            # aggressive 1% burn -- supply thins every trade so every 10k swap
            # is deflationary. Not buyable with USD; swap-only on Discoin Net.
            "name": "Degen",        "emoji": "🎰",
            "consensus": "PoS",     "network": "Discoin Network",
            "start_price": 0.02,    "daily_vol": 0.15,
            "stakeable": False,     "mineable": False,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.002,   # 0.2 %  -  degen tax
            "gas_fee": 0.05,
            "burn_rate": 0.01,      # 1 %  -  every swap sacrifices a bit
        },
        "DRIP": {
            # Discoin-side yield / LP token. Mid-vol, mid-burn -- sits between
            # the DSC / DSY majors and the DEGEN meme, rounding out the network
            # so users have a 5-token map instead of 3.
            "name": "Drip",         "emoji": "💧",
            "consensus": "PoS",     "network": "Discoin Network",
            "start_price": 2.50,    "daily_vol": 0.09,
            "stakeable": False,     "mineable": False,
            "max_supply": 25_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,   # 0.1 %
            "gas_fee": 0.05,
            "burn_rate": 0.002,     # 0.2 %
        },
        "DFUN": {
            # Disc.Fun launchpad currency. Every proto token deployed via
            # `,fun deploy` trades against virtual DFUN reserves on its
            # bonding curve, and the deploy fee is paid in DFUN. Auto-seed
            # creates DSC/DFUN, DFUN/DSD, and DFUN/MOON pools at boot, so
            # players can on-ramp from any major. High volatility + 0.5%
            # burn matches the casino feel of meme launches.
            "name": "Disc.Fun",     "emoji": "🎢",
            "consensus": "PoS",     "network": "Discoin Network",
            "start_price": 0.10,    "daily_vol": 0.12,
            "stakeable": False,     "mineable": False,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,   # 0.1 %
            "gas_fee": 0.02,
            "burn_rate": 0.005,     # 0.5 %  -  light deflation per swap
        },

        # ── Moon Network (bridged · group-token yield) ───────────────────────
        # MOON is the native yield token of Moon Network. Earn-only at genesis
        # (NOT in BUYABLE_WITH_USD): the only way in is to stake a group
        # token into the Lunar Mint (see cogs/moons.py). Group tokens were
        # previously dead weight; MOON gives them a reason to exist. 1% burn
        # on every trade offsets the emission so supply stays deflationary
        # once activity picks up.
        "MOON": {
            "name": "Moons",        "emoji": "\U0001F315",  # full moon
            "consensus": "PoS",     "network": "Moon Network",
            "start_price": 0.50,    "daily_vol": 0.08,
            "stakeable": False,     "mineable": False,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,   # 0.1 %
            "gas_fee": 0.02,
            "burn_rate": 0.01,      # 1 %  -  deflationary pressure on emission
        },

        # ── Moon-Network Wrapped Coins (MMTA, MSUN) ─────────────────────────
        # Synthetic 1:1 IOUs that let native MTA / SUN move into Moon
        # Network trading pools. Users mint them with ``.moon wrap mta <amt>``
        # / ``.moon wrap sun <amt>`` (burning native coin on its home chain)
        # and redeem them 1:1 with ``.moon unwrap mmta <amt>`` /
        # ``.moon unwrap msun <amt>``.
        #
        # Symbols are ALL-CAPS to match the rest of the token registry --
        # every write path (update_wallet_holding, make_pool_id, ...)
        # uppercases the symbol, so a lowercase-keyed Config row was
        # silently missed by lookups and a swap output landed in CeFi
        # crypto_holdings with an "Other Network" label. Display is still
        # "Moon Moneta" / "Moon Sun" via the ``name`` field.
        #
        # Oracle price is ANCHORED to the underlying: each price tick clamps
        # the wrapped price to [native * (1 - peg_band), native * (1 + peg_band)]
        # so the two drift together but MMTA can still wiggle a few percent
        # around MTA (and the AMM price can sit slightly off-peg without the
        # oracle snapping it instantly). Not a hard peg like USDC:USD; the
        # small deviation leaves room for opportunistic arbitrage without
        # making the wrapper feel like a second copy of the underlying.
        #
        # Every group token pairs with MMTA and MSUN at creation so trading
        # goes through wrapped coins, mirroring wrapped tokens in real DeFi.
        "MMTA": {
            "name": "Moon Moneta",  "emoji": "🌙",   # crescent moon
            "consensus": "PoS",      "network": "Moon Network",
            "start_price": 0.10,     "daily_vol": 0.01,       # tight, anchor does the work
            "stakeable": False,      "mineable": False,
            "max_supply": 21_000_000,
            "decimals": 8,
            "tx_fee_rate": 0.0005,
            "gas_fee": 0.02,
            "burn_rate": 0.0,        # wrapped coins don't burn -- would break the peg
            "peg_to": "MTA",         # oracle clamps within peg_band of this symbol
            "peg_band": 0.02,        # +/- 2% deviation allowed
        },
        "MSUN": {
            "name": "Moon Sun",      "emoji": "\U0001F31E",   # sun
            "consensus": "PoS",      "network": "Moon Network",
            "start_price": 0.01,     "daily_vol": 0.01,
            "stakeable": False,      "mineable": False,
            "max_supply": 21_000_000,
            "decimals": 8,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.0,
            "peg_to": "SUN",
            "peg_band": 0.02,
        },

        # ── Lure Network (fishing-only · 2-token earn economy) ──────────────
        # LURE is the fishing-earned token. Players ONLY get it by ,fish
        # casts -- never via .buy or .swap (locked behind EARN_ONLY_TOKENS).
        # It has a one-way burn path to REEL (the network coin), and that
        # is the only way LURE leaves a wallet.
        #
        # REEL is the Lure Network coin. Players ONLY get it by burn-swap
        # of LURE or by passively staking LURE (see services/fishing.py).
        # Rods, bait, and other shop gear cost REEL (replacing the old USD
        # rod prices). REEL has a one-way burn-for-USD cashout that pays
        # the player's wallet at the live oracle price minus a flat haircut.
        #
        # Both tokens are in EARN_ONLY_TOKENS so AUTO_SEED_POOLS skips them
        # and the trade / swap engine refuses to route USD into either side.
        # That is the entire pay-to-win firewall: a $100M USD whale cannot
        # fast-path the abyss rod without actually fishing.
        "LURE": {
            "name": "Lure",          "emoji": "\U0001FA9D",   # hook
            "consensus": "PoS",      "network": "Lure Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,    # 0.1 %
            "gas_fee": 0.02,
            "burn_rate": 0.005,      # 0.5 %  -  emission offset
        },
        "REEL": {
            "name": "Reel",          "emoji": "\U0001F3A3",   # fishing pole
            "consensus": "PoS",      "network": "Lure Network",
            "start_price": 1.00,     "daily_vol": 0.04,
            "stakeable": False,      "mineable": False,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,    # 0.1 %
            "gas_fee": 0.02,
            "burn_rate": 0.01,       # 1 %  -  deflationary on every shop / cashout
        },

        # ── Crypt Network (dungeon-only · 4-token earn economy) ─────────────
        # Three ore tiers (COPPER/SILVER/GOLD) are MINED in the dungeon by
        # ,delve mine. RUNE is the Crypt Network coin: players acquire it
        # ONLY by burn-swapping ore or by passively staking ore (mirrors the
        # fishing LURE -> REEL relationship). RUNE -> USD cashout is the only
        # off-ramp and uses the same impact-based slippage as ,fish cashout.
        #
        # All four are in EARN_ONLY_TOKENS so AUTO_SEED_POOLS skips them and
        # the trade / swap engine refuses to route USD into any side. That
        # is the entire pay-to-win firewall: a USD whale cannot fast-path to
        # a mythril sword without actually delving.
        "COPPER": {
            "name": "Copper Ore",    "emoji": "\U0001FA99",   # coin
            "consensus": "PoS",      "network": "Crypt Network",
            "start_price": 0.05,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "SILVER": {
            "name": "Silver Ore",    "emoji": "\U0001F948",   # silver medal
            "consensus": "PoS",      "network": "Crypt Network",
            "start_price": 0.50,     "daily_vol": 0.05,
            "stakeable": True,       "mineable": False,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "GOLD": {
            "name": "Gold Ore",      "emoji": "\U0001F947",   # gold medal
            "consensus": "PoS",      "network": "Crypt Network",
            "start_price": 5.00,     "daily_vol": 0.04,
            "stakeable": True,       "mineable": False,
            "max_supply": 200_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "RUNE": {
            "name": "Rune",          "emoji": "\U0001FAA8",   # rock / rune
            "consensus": "PoS",      "network": "Crypt Network",
            "start_price": 1.00,     "daily_vol": 0.04,
            "stakeable": False,      "mineable": False,
            "max_supply": 100_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.01,
        },

        # ── Buddy Network (companion economy · 2-token earn surface) ────────
        # BUD is the Buddy Network coin. The ONLY ways to acquire BUD are
        # FREN-stake yield or burn-swap from {FREN, REEL, RUNE, MOON} (the
        # carve-out pairs registered in Config.BUD_SWAPPABLE_TOKENS), so
        # players can rotate adjacent earn-economy tokens through BUD when
        # they want shop / market access without an off-ramp through USD.
        #
        # FREN is the staking token. Players acquire FREN by burning BUD
        # (one-way: stake -> yield -> stake more) or by hatching event drops.
        # Both are in EARN_ONLY_TOKENS so .buy / .swap / LP creation are
        # blocked from outside the network. Buddy Market + Buddy Shop both
        # denominate in BUD; the market accepts a USD payment at swap time
        # via the auto-route in services/buddy_market.py.
        "BUD": {
            "name": "Buddy",         "emoji": "\U0001F436",   # dog face
            "consensus": "PoS",      "network": "Buddy Network",
            "start_price": 1.00,     "daily_vol": 0.04,
            "stakeable": False,      "mineable": False,
            "max_supply": 100_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.01,
        },
        "FREN": {
            "name": "Fren",          "emoji": "\U0001F49E",   # sparkling heart
            "consensus": "PoS",      "network": "Buddy Network",
            "start_price": 0.10,     "daily_vol": 0.05,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        # BBT (Buddy Battle Token) -- the unified battle reward across
        # every minigame. Wild fish battles, delve buddy battles, farm
        # buddy battles, and arena fights ALL mint BBT alongside their
        # native token. Earn-only on Buddy Network; the only inflows
        # are battle wins (services/buddy_battle + the wild-battle
        # resolvers in services/{fishing,dungeon,farming,arena}). The
        # only outflows are burn-swap to BUD (carve-out) and a one-way
        # USD cashout that goes through the standard impact slippage.
        # Used as the pricing currency for Bloodstone in the item shop.
        "BBT": {
            "name": "Buddy Battle Token", "emoji": "\U0001F94A",  # boxing glove
            "consensus": "PoS",      "network": "Buddy Network",
            "start_price": 0.50,     "daily_vol": 0.05,
            "stakeable": False,      "mineable": False,
            "max_supply": 100_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },

        # ── Harvest Network (farming economy · 2-token earn surface) ────────
        # HRV is the Harvest Network coin. The ONLY ways to acquire HRV are
        # SEED-stake yield or burn-swap from {REEL, RUNE, BUD} (the carve-out
        # pairs registered in Config.HRV_SWAPPABLE_TOKENS), so players can
        # rotate adjacent earn-economy tokens through HRV when they want
        # shop / market access without an off-ramp through USD.
        #
        # SEED is the staking token. Both HRV and SEED are in EARN_ONLY_TOKENS
        # so .buy / .swap / LP creation are blocked from outside the network.
        # One-way exits live in services/farming.py.
        "HRV": {
            "name": "Harvest",       "emoji": "\U0001F33E",
            "consensus": "PoS",      "network": "Harvest Network",
            "start_price": 1.00,     "daily_vol": 0.04,
            "stakeable": False,      "mineable": False,
            "max_supply": 100_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.01,
        },
        "SEED": {
            "name": "Seedling",      "emoji": "\U0001F331",
            "consensus": "PoS",      "network": "Harvest Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },

        # ── Forge Network (crafting economy · 3-token earn surface) ─────────
        # FORGE is the network coin (oracle-priced, EARN_ONLY -- inflows are
        # INGOT stake-yield + burn-swap from {REEL, RUNE, BUD, HRV} via the
        # FORGE_SWAPPABLE_TOKENS carve-out). FGD is the network stablecoin,
        # used by the crafting shop to price recipe-input bundles and crafted
        # items at a stable USD value (so a recipe priced at 50 FGD costs the
        # same in dollars whether FORGE oracle is up 30% or down 10%). INGOT
        # is the earn-only token minted by ,craft make -- one-way burn-swap
        # to FORGE mirrors SEED -> HRV.
        "FORGE": {
            "name": "Forge",         "emoji": "\U0001F528",   # hammer
            "consensus": "PoS",      "network": "Forge Network",
            "start_price": 1.00,     "daily_vol": 0.04,
            "stakeable": False,      "mineable": False,
            "max_supply": 100_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.01,
        },
        "FGD": {
            "name": "Forge Dollar",  "emoji": "\U0001F4B5",   # banknote
            "consensus": "Fiat",     "network": "Forge Network",
            "start_price": 1.0,      "daily_vol": 0.0,
            "stakeable": False,      "mineable": False,
            "stablecoin": True,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.0005,
            "gas_fee": 0.01,
            "burn_rate": 0.0,
        },
        "INGOT": {
            "name": "Ingot",         "emoji": "\U0001F9F1",   # brick
            "consensus": "PoS",      "network": "Forge Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },

        # ── Gamba Network (gambling economy · 9-token earn surface) ─────────
        # GBC is the network coin (oracle-priced, EARN_ONLY -- inflows are
        # game-token stake-yield + burn-swap from the eight game tokens via
        # the GAMBA_SWAPPABLE_TOKENS carve-out). Each gamba game has its own
        # earn-only token that mints on wins (chess -> GAMBIT, checkers ->
        # CROWN, mines -> VEIN, dice -> PIP, coinflip -> EDGE, blackjack ->
        # ACE, roulette -> NOIR, slots -> CHERRY). All nine tokens are in
        # EARN_ONLY_TOKENS so .buy / .swap / LP creation are blocked from
        # outside the network. One-way exits live in services/gamba.py:
        #     GAMETOKEN -> GBC  via stake yield (GAMBA_STAKE_GBC_PER_DAY)
        #     GBC       -> USD  via burn cashout (impact-based slippage)
        # That is the entire pay-to-win firewall: a USD whale cannot fast-
        # path the Gamba Shop without actually gambling.
        "GBC": {
            "name": "Gamba Coin",    "emoji": "\U0001F3B0",   # slot machine
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 1.00,     "daily_vol": 0.04,
            "stakeable": False,      "mineable": False,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.01,
        },
        "GAMBIT": {
            "name": "Gambit",        "emoji": "♞",       # chess knight
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "CROWN": {
            "name": "Crown",         "emoji": "\U0001F451",   # crown
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "VEIN": {
            "name": "Vein",          "emoji": "\U0001F48E",   # gem
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "PIP": {
            "name": "Pip",           "emoji": "\U0001F3B2",   # die
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "EDGE": {
            "name": "Edge",          "emoji": "\U0001FA99",   # coin
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "ACE": {
            "name": "Ace",           "emoji": "\U0001F0A1",   # ace of spades
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "NOIR": {
            "name": "Noir",          "emoji": "⚫",       # black circle
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        "CHERRY": {
            "name": "Cherry",        "emoji": "\U0001F352",   # cherries
            "consensus": "PoS",      "network": "Gamba Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        # ── Sage Network (crypto learn-and-earn · 2-token earn surface) ─────
        # SAGE is the network coin. EDU is the game token minted on every
        # correct answer across the three Sage games (,pattern / ,gauge /
        # ,tknom). Players stake EDU to drip SAGE; SAGE -> USD via burn
        # cashout closes the loop. Mirrors the Lure/Harvest shape: one
        # earn-only game token, one earn-only network coin, no stablecoin.
        "SAGE": {
            "name": "Sage Coin",     "emoji": "\U0001F4DA",   # books
            "consensus": "PoS",      "network": "Sage Network",
            "start_price": 1.00,     "daily_vol": 0.04,
            "stakeable": False,      "mineable": False,
            "max_supply": 100_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.01,
        },
        "EDU": {
            "name": "Edu Token",     "emoji": "\U0001F393",   # graduation cap
            "consensus": "PoS",      "network": "Sage Network",
            "start_price": 0.10,     "daily_vol": 0.06,
            "stakeable": True,       "mineable": False,
            "max_supply": 5_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.005,
        },
        # ── EatChain Network (Eat the Rich / EatChain minigame) ────────────
        # EAT is the native token of EatChain, the satirical simulated
        # Layer-2 the ,eat minigame runs on. It is EARN-ONLY (in
        # EARN_ONLY_TOKENS): the only inflows are successful eats, prep/cook
        # combos, the ,eat gm tip and $EAT stake yield -- it can never be
        # bought with USD or swapped in from another network, and it does
        # not auto-seed an AMM pool. Liquid $EAT lives in wallet_holdings on
        # the `eat` network; staked $EAT lives in exploit_stats.eat_staked.
        # All EatChain tuning lives in configs/eatchain_config.py.
        "EAT": {
            "name": "EatChain",      "emoji": "\U0001F37D",   # fork and knife plate
            "consensus": "PoS",      "network": "EatChain Network",
            "start_price": 0.25,     "daily_vol": 0.07,
            # EatChain staking is handled entirely by ,eat stake (see
            # cogs/eat_the_rich.py); EAT is intentionally NOT stakeable in
            # the generic validator / yield-farm system.
            "stakeable": False,      "mineable": False,
            "max_supply": 1_000_000_000,
            "decimals": 18,
            "tx_fee_rate": 0.001,
            "gas_fee": 0.02,
            "burn_rate": 0.01,
        },
    }
    # ── USD (fiat base currency) token metadata ──────────────
    # Used by .tokeninfo USD so it can display name/emoji/price like any token.
    # USD is not in TOKENS because it is never traded or held in token balances.
    USD_META: dict = {
        "name":       "US Dollar",
        "emoji":      "💵",
        "consensus":  "Fiat",
        "network":    None,
        "start_price": 1.0,
        "daily_vol":  0.0,
        "stablecoin": True,
    }

    # ── Per-network stablecoin / network coin mappings ────────
    # Which stablecoin each PoS network uses for swaps
    NETWORK_STABLECOIN: dict = {
        # Stablecoin used for pools and swaps on each network.
        # PoW networks (SUN, MTA) have no stablecoin  -  set to None.
        "Moneta Chain":  None,   # PoW  -  no stablecoin
        "Arcadia Network": "USDC",
        "Discoin Network":  "DSD",
        "Sun Network":      None,   # PoW  -  no stablecoin
        # Moon Network is a bridged pseudo-network shared by every group
        # token so they can swap freely across the chains their founders
        # mined them on. It has no stablecoin of its own -- pricing flows
        # through the group token's mining-chain vault pool.
        "Moon Network":     None,
        # Lure Network has no stablecoin. The only USD off-ramp is the
        # one-way ,fish cashout that burns REEL at oracle minus a haircut;
        # AMM stablecoin pools would let users skirt the burn fee.
        "Lure Network":     None,
        # Crypt Network has no stablecoin -- mirrors Lure Network. The
        # only USD off-ramp is ,delve cashout that burns RUNE at oracle
        # price minus impact slippage.
        "Crypt Network":    None,
        # Buddy Network has no stablecoin -- BUD <-> USD goes via the
        # Buddy Market auto-swap (slippage applies) or via burn-cashout
        # in services/buddy_economy.py.
        "Buddy Network":    None,
        # Harvest Network has no stablecoin -- HRV <-> USD goes via burn
        # cashout in services/farming.py, mirroring the Buddy Network shape.
        "Harvest Network":  None,
        # Forge Network DOES have a stablecoin (FGD) so the crafting shop can
        # price recipe-input bundles and crafted items at a fixed USD value.
        # FGD is bought with USD (like USDC / DSD) and never minted by the
        # earn loop, so the firewall rule still holds: FORGE / INGOT can only
        # come from crafting + carve-out swaps + stake yield.
        "Forge Network":    "FGD",
        # Gamba Network has no stablecoin -- mirrors Lure Network. Shop
        # items are priced in GBC (the network coin); the only USD off-
        # ramp is ,gamba cashout that burns GBC at oracle minus impact
        # slippage. AMM stablecoin pools would let users skirt the burn.
        "Gamba Network":    None,
        # Sage Network has no stablecoin -- mirrors Lure / Gamba. The
        # only USD off-ramp is ,sage cashout that burns SAGE at oracle
        # minus impact slippage.
        "Sage Network":     None,
    }
    # Primary staking/mined coin per network
    NETWORK_COINS: dict = {
        "Moneta Chain":  "MTA",
        "Arcadia Network": "ARC",
        "Discoin Network":  "DSC",
        "Sun Network":      "SUN",
        # No native coin -- group tokens route value through their mining
        # chain's vault pool (e.g. COOK/MTA, FEM/SUN).
        "Moon Network":     None,
        # REEL is the Lure Network coin BUT it is earn-only (in
        # EARN_ONLY_TOKENS) so it is not buyable with USD via .buy. The
        # mapping exists for display / network-coin lookup parity with the
        # other PoS chains.
        "Lure Network":     "REEL",
        # RUNE is the Crypt Network coin -- earn-only, same shape as REEL.
        "Crypt Network":    "RUNE",
        # BUD is the Buddy Network coin -- earn-only, same shape as REEL.
        "Buddy Network":    "BUD",
        # HRV is the Harvest Network coin -- earn-only, same shape as REEL.
        "Harvest Network":  "HRV",
        # FORGE is the Forge Network coin -- earn-only, same shape as REEL/HRV.
        "Forge Network":    "FORGE",
        # GBC is the Gamba Network coin -- earn-only, same shape as REEL.
        "Gamba Network":    "GBC",
        # SAGE is the Sage Network coin -- earn-only, same shape as REEL.
        "Sage Network":     "SAGE",
    }
    # Tokens directly buyable with USD via .buy (network coins + stablecoins).
    # All other tokens require .swap.  SUN can also be used as payment for .buy.
    BUYABLE_WITH_USD: frozenset = frozenset({
        # Network coins (directly buyable with USD via .buy)
        "MTA", "SUN", "ARC", "DSC",
        # Stablecoins
        "USDC", "DSD", "FGD",
    })

    # Tokens that can ONLY be acquired through their native earn mechanism --
    # never via .buy, .swap, or a liquidity pool. MOON is emitted exclusively
    # by the Lunar Mint (see cogs/moons.py); allowing it in AMM pools would
    # break the economy by letting players bypass the stake-to-earn loop.
    EARN_ONLY_TOKENS: frozenset = frozenset({
        "MOON",
        # Lure Network economy. LURE is the fishing-earned token (ONLY way
        # to acquire is ,fish casts). REEL is the network coin (ONLY way to
        # acquire is burn-swap or stake of LURE). Neither can be touched by
        # .buy or .swap from any other token, and neither auto-seeds an AMM
        # pool. Their one-way exits live in services/fishing.py:
        #     LURE -> REEL  via burn-swap or LURE staking yield
        #     REEL -> USD   via burn cashout
        # That is the entire pay-to-win firewall: a USD whale cannot fast-
        # path the abyss rod without actually fishing.
        "LURE",
        "REEL",
        # Crypt Network economy. COPPER/SILVER/GOLD are mined in the
        # dungeon (,delve mine). RUNE is minted from ore burn-swap or
        # ore staking. Neither can be touched by .buy or .swap from any
        # other token, and none auto-seed an AMM pool. One-way exits
        # live in services/dungeon.py:
        #     ORE  -> RUNE  via burn-swap or ore staking yield
        #     RUNE -> USD   via burn cashout
        # That is the entire pay-to-win firewall: a USD whale cannot
        # fast-path to a mythril sword without actually delving.
        "COPPER",
        "SILVER",
        "GOLD",
        "RUNE",
        # Buddy Network economy. BUD is the network coin (only inflows:
        # FREN stake-yield + burn-swap from REEL / RUNE / MOON / FREN).
        # FREN is the staking token (only inflow: burn-swap from BUD).
        # Neither can be touched by .buy or .swap from any other token
        # outside the BUD_SWAPPABLE_TOKENS carve-out, and neither auto-
        # seeds an AMM pool. One-way exits live in services/buddy_economy.py.
        "BUD",
        "FREN",
        # BBT (Buddy Battle Token) -- minted on every battle win across
        # the network (fishing wilds, delve wilds, farm wilds, arenas,
        # buddy PvP). One-way exits live in services/buddy_economy.py:
        #     BBT -> BUD   via burn-swap (carve-out below)
        #     BBT -> USD   via burn cashout (impact-based slippage)
        # Used as the Bloodstone pricing currency in the shop.
        "BBT",
        # Harvest Network economy. HRV is the network coin, SEED is the
        # staking token. Neither can be touched by .buy or .swap from any
        # other token outside the HRV_SWAPPABLE_TOKENS carve-out, and
        # neither auto-seeds an AMM pool. One-way flows:
        #     SEED -> HRV  via burn-swap or SEED staking yield (services/farming.py)
        #     HRV  -> USD  via burn cashout (services/farming.py)
        #     HRV  <-> {REEL, RUNE, BUD}  via the HRV-swappable carve-out
        # That is the entire pay-to-win firewall: a USD whale cannot fast-
        # path the harvest economy without actually farming.
        "HRV",
        "SEED",
        # Forge Network economy. FORGE is the network coin (only inflows:
        # INGOT stake-yield + burn-swap from {REEL, RUNE, BUD, HRV, INGOT}
        # via the FORGE_SWAPPABLE_TOKENS carve-out). INGOT is the earn-only
        # token (only inflow: ,craft make). Neither can be touched by .buy
        # or .swap from any other token outside the carve-out, and neither
        # auto-seeds an AMM pool. One-way exits live in services/crafting.py:
        #     INGOT -> FORGE  via burn-swap or INGOT staking yield
        #     FORGE -> USD    via burn cashout
        # FGD is the network's stablecoin and stays buyable-with-USD so the
        # crafting shop has a stable unit of account; that does NOT break
        # the firewall because FGD never enters the FORGE/INGOT supply --
        # it only buys recipe-input bundles and finished items in FGD.
        "FORGE",
        "INGOT",
        # Gamba Network economy. GBC is the network coin (only inflows:
        # game-token stake-yield + burn-swap from the eight game tokens
        # via the GAMBA_SWAPPABLE_TOKENS carve-out). The eight game
        # tokens are minted only on game wins (chess/checkers/mines/
        # dice/coinflip/blackjack/roulette/slots). Neither GBC nor any
        # game token can be touched by .buy or .swap from any other
        # token outside the carve-out, and none auto-seed an AMM pool.
        # One-way exits live in services/gamba.py:
        #     GAMETOKEN -> GBC  via stake-yield (or burn-swap carve-out)
        #     GBC       -> USD  via burn cashout (impact-based slippage)
        # That is the entire pay-to-win firewall: a USD whale cannot
        # fast-path the Gamba Shop without actually gambling.
        "GBC",
        "GAMBIT",
        "CROWN",
        "VEIN",
        "PIP",
        "EDGE",
        "ACE",
        "NOIR",
        "CHERRY",
        # Sage Network economy. SAGE is the network coin (only inflows:
        # EDU stake-yield + burn-swap from EDU via the SAGE_SWAPPABLE_TOKENS
        # carve-out). EDU is minted only on correct answers in the three
        # Sage games (,pattern / ,gauge / ,tknom). Neither SAGE nor EDU
        # can be touched by .buy or .swap from any other token outside
        # the carve-out, and neither auto-seeds an AMM pool. One-way
        # exits live in services/sage.py:
        #     EDU  -> SAGE via stake-yield (or burn-swap carve-out)
        #     SAGE -> USD  via burn cashout (impact-based slippage)
        "SAGE",
        "EDU",
        # EatChain Network economy. EAT is the native token of the ,eat
        # minigame's simulated Layer-2. The only inflows are successful
        # eats, prep/cook combos, the ,eat gm tip and $EAT stake yield --
        # it cannot be bought with USD or swapped in from any other token,
        # and it does not auto-seed an AMM pool. The whole EatChain economy
        # lives in cogs/eat_the_rich.py + configs/eatchain_config.py.
        "EAT",
    })

    # Built-in tokens that may swap into AND out of MOON in a regular AMM
    # pool. The general earn-only firewall still blocks every other path
    # into MOON; this set is the one explicit carve-out. mMTA and mSUN are
    # the wrapped Moon-Network coins, so a bidirectional MOON pair lets
    # players cycle MTA- and SUN-denominated value through the Lunar Mint
    # economy without breaking the stake-to-earn loop (MOON still has to
    # come from somewhere -- the LP that originally seeded the pool).
    #
    # Player-deployed tokens opt into this carve-out individually via
    # ``token_contracts.params["moon_swappable"] = True`` (see
    # cogs/nfts.py::token_deploy). Group tokens on Moon Network keep the
    # legacy MOON->TOKEN one-way semantics so the Lunar Mint stays the only
    # way to mint new MOON against a group token.
    MOON_SWAPPABLE_TOKENS: frozenset = frozenset({
        "MMTA",
        "MSUN",
    })

    # Built-in tokens that may swap into AND out of BUD in a regular AMM
    # pool. Mirrors MOON_SWAPPABLE_TOKENS but for the Buddy Network.
    # The whitelisted partners (REEL, RUNE, MOON, FREN, HRV, BBT, INGOT,
    # GBC) let players rotate adjacent earn-economy tokens through BUD
    # without ever touching the broader USD AMM. Every other path into
    # BUD is blocked by the EARN_ONLY firewall, so the carve-out is the
    # ONLY way fresh BUD enters circulation outside FREN stake-yield.
    BUD_SWAPPABLE_TOKENS: frozenset = frozenset({
        "REEL",
        "RUNE",
        "MOON",
        "FREN",
        "HRV",
        "BBT",
        # INGOT is earn-only (only inflow is ,craft make), but BUD itself
        # is also earn-only -- a USD whale has no .buy / .swap path into
        # BUD, so making BUD <-> INGOT bidirectional doesn't open a USD
        # exposure on INGOT. The closed earn-economy loop already locks
        # the firewall; keeping INGOT in the loop lets crafters round-trip
        # forge output through the buddy market without the previous
        # one-way penalty.
        "INGOT",
        # GBC is the Gamba Network coin -- earn-only, same firewall logic
        # as INGOT. Stored in crypto_holdings (not wallet_holdings) like
        # the rest of the gamba surface; services/buddy_economy.py
        # dispatches reads/writes through update_holding for GBC so the
        # bidirectional convert works without forcing a storage migration.
        "GBC",
        # The eight Gamba game tokens (mint on a gamba win, stake on the
        # gamba surface for GBC drip). Adding them as bidirectional BUD
        # partners closes the circular buddy <-> gamba loop:
        #     BUD -> any game token (burn-swap)
        #         -> stake for GBC (existing gamba flow)
        #             -> GBC -> BUD (existing convert)
        # All eight are earn-only and live in crypto_holdings, same as
        # GBC, so the storage-dispatch path already handles them.
        "GAMBIT",
        "CROWN",
        "VEIN",
        "PIP",
        "EDGE",
        "ACE",
        "NOIR",
        "CHERRY",
        # SAGE is the Sage Network coin -- earn-only (mints on correct
        # answers in ,pattern / ,gauge / ,tknom / ,cycle), same firewall
        # logic as INGOT / GBC. EDU (the Sage game token) intentionally
        # stays out of buddy convert: its only canonical exit is via stake
        # -> SAGE drip -> SAGE cashout, and routing EDU through BUD would
        # collapse the stake-loop into a direct sell path.
        "SAGE",
    })

    # Reserved for future earn-only -> BUD one-way carve-outs. INGOT used to
    # live here when the firewall design was more conservative; today every
    # buddy partner is bidirectional. Kept as an explicit empty set so the
    # rest of the codebase can still union against it without conditionals.
    BUD_ONEWAY_IN_TOKENS: frozenset = frozenset()

    # Built-in tokens that may swap into AND out of HRV in a regular AMM
    # pool. Mirrors BUD_SWAPPABLE_TOKENS but for the Harvest Network.
    # The whitelisted partners (REEL, RUNE, BUD) let players rotate
    # adjacent earn-economy tokens through HRV without ever touching the
    # broader USD AMM. Every other path into HRV is blocked by the
    # EARN_ONLY firewall, so the carve-out is the ONLY way fresh HRV
    # enters circulation outside SEED stake-yield.
    HRV_SWAPPABLE_TOKENS: frozenset = frozenset({
        "REEL",
        "RUNE",
        "BUD",
    })

    # Built-in tokens that may swap into AND out of FORGE in a regular AMM
    # pool. Mirrors HRV_SWAPPABLE_TOKENS but for the Forge Network. The
    # whitelisted partners (REEL, RUNE, BUD, HRV, INGOT) let players rotate
    # adjacent earn-economy tokens through FORGE without ever touching the
    # broader USD AMM. INGOT is included so the in-network INGOT -> FORGE
    # burn-swap (the canonical earn-loop exit) shows up as a registered
    # pair the swap engine recognises; the actual conversion still uses
    # the burn path with slippage in services/crafting.py.
    FORGE_SWAPPABLE_TOKENS: frozenset = frozenset({
        "REEL",
        "RUNE",
        "BUD",
        "HRV",
        "INGOT",
    })

    # Built-in tokens that may swap into AND out of GBC in a regular AMM
    # pool. Mirrors FORGE_SWAPPABLE_TOKENS but for the Gamba Network. The
    # whitelisted partners (REEL, RUNE, BUD, HRV, FORGE) let players
    # rotate adjacent earn-economy tokens through GBC without touching
    # the broader USD AMM. The eight game tokens are also included so
    # the in-network burn-swap (the canonical earn-loop exit) shows up
    # as a registered pair the swap engine recognises; the actual
    # conversion still uses the burn path with slippage in
    # services/gamba.py.
    GAMBA_SWAPPABLE_TOKENS: frozenset = frozenset({
        "REEL",
        "RUNE",
        "BUD",
        "HRV",
        "FORGE",
        "GAMBIT",
        "CROWN",
        "VEIN",
        "PIP",
        "EDGE",
        "ACE",
        "NOIR",
        "CHERRY",
    })

    # Built-in tokens that may swap into AND out of SAGE in a regular AMM
    # pool. Mirrors GAMBA_SWAPPABLE_TOKENS but for the Sage Network. The
    # whitelisted partners (REEL, RUNE, BUD, HRV, FORGE, GBC) let players
    # rotate adjacent earn-economy coins through SAGE without touching
    # the broader USD AMM. EDU is included so the in-network EDU -> SAGE
    # burn-swap (canonical earn-loop exit) is visible to the swap engine;
    # the actual conversion still uses the burn path in services/sage.py.
    SAGE_SWAPPABLE_TOKENS: frozenset = frozenset({
        "REEL",
        "RUNE",
        "BUD",
        "HRV",
        "FORGE",
        "GBC",
        "EDU",
    })

    # ── Gamba Network ──────────────────────────────────────────────────
    # Mapping of game name -> earn-only token symbol. Each game in the
    # gamba surface (chess, checkers, mines, dice, coinflip, blackjack,
    # roulette, slots) mints a small amount of its themed token alongside
    # the USD payout on every win. Players can stake those tokens to
    # passively drip GBC. Mirrors the LURE -> REEL relationship but
    # parameterised across eight games.
    GAMBA_NETWORK_SHORT: str = "gam"
    GAMBA_COIN: str = "GBC"
    GAMBA_GAME_TOKEN: dict = {
        "chess":     "GAMBIT",
        "checkers":  "CROWN",
        "mines":     "VEIN",
        "dice":      "PIP",
        "coinflip":  "EDGE",
        "blackjack": "ACE",
        "roulette":  "NOIR",
        "slots":     "CHERRY",
    }
    # Per-game-token GBC drip rate (GBC per token per day). All eight
    # game tokens use the same rate: 0.0025 GBC per game-token per day.
    # Holding 1000 PIP for a day drips 2.5 GBC. APY scales with the GBC
    # oracle. Tightened from the original 0.01 to slow gamba-network
    # emission and keep the closed-loop sink ahead of mint pressure.
    GAMBA_STAKE_GBC_PER_DAY: float = 0.0025
    # Same rate when the stake is set to drip BUD instead of GBC
    # (per-position yield_target='BUD' on gamba_stakes). Tracks
    # GAMBA_STAKE_GBC_PER_DAY in lockstep so the buddy-target option
    # doesn't tilt emission relative to the GBC default.
    GAMBA_STAKE_BUD_PER_DAY: float = 0.0025
    # Mint rate on a game win: amount of game-themed token credited per
    # 1 USD of profit (delta). 0.125 means winning $20 in mines mints
    # 2.5 VEIN. Tightened from 0.50 alongside the stake-drip cut so the
    # full mint-and-stake loop slows together.
    GAMBA_TOKEN_MINT_PER_USD_WIN: float = 0.125
    # Bet-token whitelist: which symbols can be wagered in chess /
    # checkers / etc. USD plus the network coin so PvP bets settle in
    # something with stable price.
    GAMBA_BET_TOKENS: frozenset = frozenset({"USD", "GBC"})

    # ── Sage Network ──────────────────────────────────────────────────
    # Mapping of game name -> earn-only token symbol. All three games
    # (pattern / gauge / tknom) mint the same EDU game token alongside
    # a small SAGE coin drip on every correct answer. The 10/90 split
    # follows the same pattern as fishing's LURE/REEL mint: 10% of the
    # USD-equivalent reward lands as SAGE (network coin) and 90% as EDU
    # (game token), keeping the stake-to-earn loop slower than direct
    # SAGE accumulation.
    SAGE_NETWORK_SHORT: str = "sag"
    SAGE_COIN: str = "SAGE"
    SAGE_GAME_TOKEN_SYM: str = "EDU"
    SAGE_GAME_TOKEN: dict = {
        "pattern":    "EDU",
        "gauge":      "EDU",
        "tknom":      "EDU",
    }
    # Per-EDU SAGE drip rate (SAGE per EDU per day). Matches the Gamba
    # stake-rate so an EDU position drips at parity with a PIP / ACE
    # position, then APY scales with the live SAGE oracle.
    SAGE_STAKE_RATE_PER_DAY: float = 0.0025
    # USD-equivalent value of a single correct answer at base difficulty
    # (round 1). Subsequent rounds multiply this by a per-round factor.
    # Split 10% SAGE / 90% EDU on every correct answer, mirroring the
    # fishing LURE/REEL split shape so the firewall reads identically.
    SAGE_REWARD_USD_BASE: float = 0.20
    # Reward multiplier per round of consecutive correct answers in a
    # run. Round 1 = 1.0x, round 10 ~= 1.9x, round 20 ~= 2.9x. Floor at
    # 1.0 so a fresh run never under-pays the base.
    SAGE_REWARD_ROUND_MULT: float = 0.10
    SAGE_REWARD_MAX_ROUND_MULT: float = 4.0
    # Split of the per-correct reward between SAGE (10%) and EDU (90%).
    SAGE_COIN_SHARE: float = 0.10
    SAGE_TOKEN_SHARE: float = 0.90
    # Per-game timer in seconds. Gauge / Cycle get 2x for reading time.
    SAGE_TIMER_PATTERN_S: int = 15
    SAGE_TIMER_GAUGE_S: int = 30
    SAGE_TIMER_TKNOM_S: int = 15
    SAGE_TIMER_CYCLE_S: int = 30
    # Cashout LP-holder kickback (bps) on SAGE -> USD burns. Mirrors
    # GBC_CASHOUT_LP_REWARD_BPS so any future SAGE pool earns parity.
    SAGE_CASHOUT_LP_REWARD_BPS: int = 100
    # Auto-seed TOKEN/stablecoin, COIN/TOKEN, TOKEN/USD, and intra-network
    # TOKEN/TOKEN pools on bot startup. Default TRUE so every network gets
    # complete swap routing (Discoin, Arcadia, PoW coins all become
    # swappable amongst themselves and into their stablecoin) without an
    # admin having to flip a .env flag. INSERT ... ON CONFLICT DO NOTHING
    # keeps it idempotent; existing pools are never overwritten.
    AUTO_SEED_POOLS: bool = _env_bool("AUTO_SEED_POOLS", True)

    # Pool seed: USD value per side. $10,000 default keeps price impact meaningful
    # for users starting with $20 (~0.2% slippage on a full-balance trade).
    POOL_SEED_STABLECOIN: int = int(_env_float("POOL_SEED_STABLECOIN", 10000.0) * _S)

    # ── Disc.Fun (proto-token launchpad) ───────────────────────────────────
    # Pump.fun-style bonding curve on the Discoin Network. Anyone can deploy
    # a proto-token cheaply (no Protocol Dev tier gate); they pick name,
    # symbol and emoji and nothing else. Every other knob (supply, virtual
    # liquidity, graduation threshold, fees) is locked to these defaults,
    # which is the deliberate trade-off vs `,token deploy`.
    #
    # Quote currency: DFUN (Discoin Network). Acquire via DSC/DFUN, DSD/DFUN
    # or DFUN/MOON pools auto-seeded by ``seed_pools``. Curve math is the
    # standard Uniswap-v2 constant product on virtual reserves:
    #
    #   tokens_out = quote_in * V_tok / (V_quote + quote_in)
    #   quote_at_grad = curve_supply * V_quote / (V_tok - curve_supply)
    #
    # The defaults below give a ~121x price ramp from launch to graduation.
    # Threshold is a flat **10 million DFUN** collected on the curve, so
    # the milestone stays denominated in the launchpad's native quote and
    # doesn't drift if the DFUN/USD oracle moves.
    #   start price = V_q / V_t              = 1M / 880M    ≈ 1.14e-3 DFUN/token
    #   final price = (V_q + 10M) / 80M      = 11M / 80M    ≈ 1.38e-1 DFUN/token
    DISCFUN: dict = {
        "quote_symbol":          "DFUN",         # bonding-curve quote token
        "deploy_fee":            10_000.0,       # DFUN charged on deploy
        "trade_fee_bps":          100,           # 1% fee on every buy & sell
        "total_supply":          1_000_000_000,  # 1B tokens minted at graduation
        "curve_supply":            800_000_000,  # 800M sold on the curve
        "graduation_quote":     10_000_000.0,    # 10M DFUN threshold to graduate
        "initial_virtual_quote":  1_000_000.0,
        "initial_virtual_token":   880_000_000,  # tuned so curve_supply -> graduation
        "default_emoji":         "🚀",
        # Quick-buy chip values (DFUN). Used in the inline buy view.
        "quickbuy_chips":        [100.0, 500.0, 2_000.0, 10_000.0, 50_000.0],
        # Locked-in token contract params for graduated Disc.Fun tokens.
        # `,token deploy` lets the deployer pick these freely; here they're fixed.
        "graduation_burn_rate":    0.005,        # 0.5% per transfer
        "graduation_transfer_fee": 0.005,        # 0.5% per transfer
        "graduation_daily_vol":    0.20,         # 20% daily vol post-graduation
        # ── Staking (graduated tokens -> DFUN yield) ────────────────────
        # Holders of graduated Disc.Fun tokens can `,fun stake SYM AMT`
        # them back into the launchpad to earn DFUN. Yield is denominated
        # in DFUN and proportional to the position's spot DFUN value via
        # the live SYMBOL/DFUN pool reserves. Lazy accrual on the DB clock
        # (services/discfun.py::_accrue_stake) so positions keep earning
        # even when the bot is offline.
        #
        # APY is emission-based and variable, mirroring Safety Module
        # (VTR / DSY) staking: a fixed per-day DFUN pool is split among
        # all stakers in the guild, so early stakers with low TVL can
        # earn near the max cap while the rate compresses as TVL grows
        # but never drops below the configured floor.
        #   daily_rate = emission_dfun_per_day / total_staked_dfun_value
        # ``staking_apy`` is kept as a legacy fallback when the variable
        # path can't compute (e.g. no graduated stakes yet) -- never let
        # APY drop below ``staking_min_apy_pct`` regardless.
        "staking_apy":                       2.00,         # 200% APY (legacy fallback only)
        "staking_emission_dfun_per_day": 20_000.0,         # 20k DFUN/day shared across all stakers
        "staking_max_apy_pct":           40_000.0,         # cap at 40,000% APY
        "staking_min_apy_pct":              100.0,         # floor at 100% APY
    }

    # ── Mining (PoW) ──────────────────────────────────────────
    # Fully modular: add or remove any PoW network here at will.
    # Keys match the token symbols in Config.TOKENS (with "mineable": True).
    # Fields per network:
    #   symbol            -  must match a key in Config.TOKENS
    #   name, emoji       -  display strings
    #   initial_reward    -  block reward at genesis
    #   min_reward        -  floor after halvings
    #   halving_blocks    -  blocks between reward halvings
    #   target_block_time  -  seconds per block (used for difficulty retargeting)
    #   difficulty_window  -  blocks between difficulty retargets
    #   initial_difficulty  -  starting difficulty (MH·s per block)
    #   max_group_share   -  max fraction any mining group can earn (0.40 = 40%)
    #   electricity_rate  -  USD per kWh charged to miners each tick
    POW_NETWORKS: dict = {
        # ── Mining Balance Notes ──────────────────────────────────────────────
        # expected_block_time_solo = initial_difficulty / miner_hashrate  (seconds)
        # GTX1060 = 15 MH/s | RTX2080 = 110 MH/s | RTX4090 = 950 MH/s | ASIC_S19 = 70,000 MH/s
        #
        # initial_difficulty acts as a hard floor  -  difficulty never drops below it.
        # Miners whose live target (hashrate × target_block_time) is below the floor
        # mine slower than the target rate; those above it mine at target rate.
        #
        # SUN (casual PoW, accessible with mid-tier rigs):
        #   GTX1060  solo: 60,000 / 15       =  4,000s  (~67 min)   -  playable entry
        #   RTX2080  solo: 60,000 / 110       =    545s  (~9 min)    -  near target rate
        #   RTX4090  solo: 570,000 / 950      =    600s  (10 min)    -  at target rate
        #   ASIC_S19 solo: 42,000,000 / 70000 =    600s  (10 min)    -  difficulty scales up
        #
        # MTA (premium PoW, designed for A100/ASIC-class rigs):
        #   GTX1080  solo: 1,000,000 / 40     = 25,000s  (~417 min)  -  very slow, floor active
        #   RTX2080  solo: 1,000,000 / 110    =  9,090s  (~152 min)  -  floor active
        #   RTX4090  solo: 1,000,000 / 950    =  1,053s  (~18 min)   -  viable but slow
        #   A100     solo: 2,100,000 / 3500   =    600s  (10 min)    -  at target rate
        #   ASIC_S19 solo: 42,000,000 / 70000 =    600s  (10 min)    -  ideal tier for MTA
        #   Difficulty retargets every 2,016 blocks to maintain target_block_time.
        #
        # All values are configurable here  -  adjust initial_difficulty to tune
        # how hard the first blocks are before the retarget window kicks in.
        # ─────────────────────────────────────────────────────────────────────

        "SUN": {
            "name": "Sun", "symbol": "SUN", "emoji": "☀",
            "initial_reward":   500.0,        # SUN per block at genesis
            "min_reward":         0.001,       # floor after all halvings
            "halving_blocks":   210_000,       # reward halves every 210k blocks
            "target_block_time":    600,       # 10 minutes target
            "difficulty_window":    144,       # retarget every 144 blocks (~1 day)
            "initial_difficulty": 60_000.0,   # MH·s/block  -  accessible for mid-tier rigs
            "max_group_share":      0.40,      # anti-pool-dominance cap (40%)
            "solo_share_cap":       0.20,      # single miner capped at 20% of network reward
            "electricity_rate":     0.16,      # USD/kWh
            "electricity_scaling":  1.08,      # +8% cost per additional rig (diminishing returns)
            "warmup_blocks":       200,        # reward ramps 0→100% over first 200 blocks (cubic curve)
        },
        "MTA": {
            "name": "Moneta", "symbol": "MTA", "emoji": "🟡",
            "initial_reward":    62.5,        # MTA per block (current halving era)
            "min_reward":         0.00000001,  # 1 satoshi floor
            "halving_blocks":   210_000,
            "target_block_time":    600,       # 10 minutes target
            "difficulty_window":  2_016,       # retarget every 2,016 blocks (~2 weeks)
            "initial_difficulty": 1_000_000.0, # MH·s/block  -  requires RTX4090+ or ASIC
            "max_group_share":      0.40,
            "solo_share_cap":       0.15,      # tighter cap (15%)  -  MTA is more exploitable when thin
            "electricity_rate":     0.22,      # higher electricity cost than SUN
            "electricity_scaling":  1.08,
            "warmup_blocks":       500,        # reward ramps 0→100% over first 500 blocks (cubic curve)
        },
    }
    # rig_id → { name, tier, hashrate (MH/s), power (W), price (USD coins), emoji }
    # REBALANCED: smoother price/hashrate curve with diminishing returns at top tiers.
    # Price-per-MH/s: ~$125 (T1) → ~$100 (T3) → ~$85 (T5) → ~$75 (T7) → ~$70 (T8)
    # Power efficiency improves with tier: 10 W/MH (T1) → 0.05 W/MH (T8)
    # ROI days (at current SUN mining rates) range from ~12 days (T1) to ~25 days (T8)
    MINING_RIGS: dict = {
        "GTX1060": {"name": "GTX 1060",     "tier": 1, "hashrate": 15,      "power": 120,  "price": int(    1_800 * _S), "emoji": "🖥"},
        "GTX1080": {"name": "GTX 1080",     "tier": 2, "hashrate": 40,      "power": 150,  "price": int(    4_500 * _S), "emoji": "🖥"},
        "RTX2080": {"name": "RTX 2080",     "tier": 3, "hashrate": 110,     "power": 180,  "price": int(   11_000 * _S), "emoji": "💻"},
        "RTX3090": {"name": "RTX 3090",     "tier": 4, "hashrate": 320,     "power": 270,  "price": int(   30_000 * _S), "emoji": "💻"},
        "RTX4090": {"name": "RTX 4090",     "tier": 5, "hashrate": 950,     "power": 340,  "price": int(   80_000 * _S), "emoji": "🖱"},
        "A100":    {"name": "A100 PCIe",    "tier": 6, "hashrate": 3_500,   "power": 300,  "price": int(  280_000 * _S), "emoji": "⚙"},
        "H100":    {"name": "H100 NVL",     "tier": 7, "hashrate": 15_000,  "power": 550,  "price": int(1_100_000 * _S), "emoji": "🔬"},
        "ASIC_S19":{"name": "Antminer S19", "tier": 8, "hashrate": 70_000,  "power": 3200, "price": int(4_800_000 * _S), "emoji": "⛏"},
    }

    # ── Jobs ──────────────────────────────────────────────────
    # Crypto-culture job ladder. Work every 15min → requirements scaled accordingly.
    # Perks: daily_bonus (multiplier on daily reward), swap_fee (fee rebate on swaps),
    #        stake_bonus (multiplier on staking rewards), mining_bonus (multiplier on mining hashrate),
    #        interest_bonus (multiplier on savings APY).
    JOBS: dict = {
        # ── ECONOMY BALANCE NOTES ──────────────────────────────────────────────
        # Target balance: Work pays ~2-5x mining, staking is a viable passive alternative.
        # A GTX1060 ($2,500) mines ~$200/day SUN or ~$1200/day MTA (while MTA network is thin).
        # Staking $2K should yield $20-75/day depending on validator risk (see VALIDATORS above).
        # Staking $100K should yield $1,000-3,800/day  -  outscales mining at high capital.
        "HOMELESS": {
            "title": "Homeless",
            "min_work": 0, "min_wealth": 0,
            "earn": (int(5 * _S), int(20 * _S)),
            "rig_slots": 2,
            "perks": {},
            "description": "Found a phone on the ground. Downloading Discord.",
        },
        "TWITTER_SHILL": {
            "title": "Twitter Shill",
            "min_work": 2, "min_wealth": 100,
            "earn": (int(8 * _S), int(30 * _S)),
            "rig_slots": 3,
            "perks": {},
            "description": "Tweeting 'gm' to 4 followers. Wagmi -- We Are Grinding Mostly Imaginary.",
        },
        "AIRDROP_FARMER": {
            "title": "Airdrop Farmer",
            "min_work": 5, "min_wealth": 500,
            "earn": (int(15 * _S), int(50 * _S)),
            "rig_slots": 4,
            "perks": {},
            "description": "Farming airdrops with 47 wallets. Sybil check pending.",
        },
        "POAP_HUNTER": {
            "title": "POAP Hunter",
            "min_work": 8, "min_wealth": 1_000,
            "earn": (int(20 * _S), int(70 * _S)),
            "rig_slots": 5,
            "perks": {"daily_bonus": 0.03},
            "description": "Collecting POAPs at virtual conferences nobody attends.",
        },
        "LARPER": {
            "title": "Larper",
            "min_work": 15, "min_wealth": 2_000,
            "earn": (int(30 * _S), int(100 * _S)),
            "rig_slots": 6,
            "perks": {"daily_bonus": 0.05},
            "description": "Posting fake trading screenshots. The followers don't know.",
        },
        "WHITELIST_FARMER": {
            "title": "Whitelist Farmer",
            "min_work": 30, "min_wealth": 8_000,
            "earn": (int(60 * _S), int(200 * _S)),
            "rig_slots": 8,
            "perks": {"daily_bonus": 0.10},
            "description": "In 47 Discord servers. Haven't slept since Tuesday.",
        },
        "NFT_FLIPPER": {
            "title": "NFT Flipper",
            "min_work": 45, "min_wealth": 15_000,
            "earn": (int(80 * _S), int(300 * _S)),
            "rig_slots": 10,
            "perks": {"daily_bonus": 0.12, "swap_fee": 0.0035},
            "description": "Bought the floor, listed for 2x. Still listed.",
        },
        "SHITCOIN_TRENCHER": {
            "title": "Shitcoin Trencher",
            "min_work": 60, "min_wealth": 25_000,
            "earn": (int(100 * _S), int(400 * _S)),
            "rig_slots": 12,
            "perks": {"daily_bonus": 0.15, "swap_fee": 0.003},
            "description": "Bought the top of every dog coin in 2021. Still holding.",
        },
        "DISCORD_MOD": {
            "title": "Discord Mod",
            "min_work": 100, "min_wealth": 75_000,
            "earn": (int(200 * _S), int(700 * _S)),
            "rig_slots": 16,
            "perks": {"daily_bonus": 0.20, "swap_fee": 0.002},
            "description": "Muting 'wen token' every 90 seconds. Unpaid. Proud.",
        },
        "CT_INFLUENCER": {
            "title": "CT Influencer",
            "min_work": 140, "min_wealth": 130_000,
            "earn": (int(300 * _S), int(950 * _S)),
            "rig_slots": 20,
            "perks": {"daily_bonus": 0.22, "swap_fee": 0.0017},
            "description": "Quote-tweeting for engagement. Reply guy ascendancy.",
        },
        "DEFI_DEGEN": {
            "title": "DeFi Degen",
            "min_work": 175, "min_wealth": 200_000,
            "earn": (int(400 * _S), int(1_200 * _S)),
            "rig_slots": 24,
            "work_cooldown": 1200,
            "perks": {"daily_bonus": 0.25, "swap_fee": 0.0015, "stake_bonus": 0.05},
            "description": "Aping into 40,000% APY farms. The IL is fine.",
        },
        "YIELD_FARMER": {
            "title": "Yield Farmer",
            "min_work": 220, "min_wealth": 400_000,
            "earn": (int(550 * _S), int(1_600 * _S)),
            "rig_slots": 28,
            "work_cooldown": 1350,
            "perks": {"daily_bonus": 0.27, "swap_fee": 0.0013, "stake_bonus": 0.08},
            "description": "Auto-compounding rewards into a token that lost 90%.",
        },
        "TRADER": {
            "title": "Trader",
            "min_work": 275, "min_wealth": 600_000,
            "earn": (int(700 * _S), int(2_000 * _S)),
            "rig_slots": 32,
            "work_cooldown": 1500,
            "perks": {"daily_bonus": 0.30, "swap_fee": 0.001, "stake_bonus": 0.10, "mining_bonus": 0.10},
            "description": "PnL screenshot with 3 winning trades and 9 hidden ones.",
        },
        "MEV_SEARCHER": {
            "title": "MEV Searcher",
            "min_work": 340, "min_wealth": 1_200_000,
            "earn": (int(850 * _S), int(2_750 * _S)),
            "rig_slots": 40,
            "work_cooldown": 1650,
            "perks": {"daily_bonus": 0.32, "swap_fee": 0.0008, "stake_bonus": 0.12, "mining_bonus": 0.12, "interest_bonus": 0.05},
            "description": "Front-running grandmas via mempool. Gas wars are personal.",
        },
        "COURSE_SELLER": {
            "title": "Course Seller",
            "min_work": 400, "min_wealth": 2_000_000,
            "earn": (int(1_000 * _S), int(3_500 * _S)),
            "rig_slots": 48,
            "work_cooldown": 1800,
            "perks": {"daily_bonus": 0.35, "swap_fee": 0.0005, "stake_bonus": 0.15, "mining_bonus": 0.15, "interest_bonus": 0.10},
            "description": "Selling a $997 course on how you made $500 on-chain.",
        },
        "ANALYST": {
            "title": "Onchain Analyst",
            "min_work": 475, "min_wealth": 4_000_000,
            "earn": (int(1_250 * _S), int(4_200 * _S)),
            "rig_slots": 56,
            "work_cooldown": 2200,
            "perks": {"daily_bonus": 0.37, "swap_fee": 0.00035, "stake_bonus": 0.18, "mining_bonus": 0.18, "interest_bonus": 0.12},
            "description": "Posting market reports nobody reads. Everyone is bullish anyway.",
        },
        "VALIDATOR_OP": {
            "title": "Liquidity Baron",
            "min_work": 550, "min_wealth": 7_500_000,
            "earn": (int(1_500 * _S), int(5_000 * _S)),
            "rig_slots": 64,
            "work_cooldown": 2700,
            "perks": {"daily_bonus": 0.40, "swap_fee": 0.0002, "stake_bonus": 0.20, "mining_bonus": 0.20, "interest_bonus": 0.15},
            "description": "Farming every pool on three networks. Net APY opaque even to you.",
        },
        "VC_PARTNER": {
            "title": "VC Partner",
            "min_work": 650, "min_wealth": 15_000_000,
            "earn": (int(1_900 * _S), int(6_300 * _S)),
            "rig_slots": 80,
            "work_cooldown": 3150,
            "perks": {"daily_bonus": 0.42, "swap_fee": 0.00015, "stake_bonus": 0.22, "mining_bonus": 0.22, "interest_bonus": 0.20},
            "description": "Wrote a thesis. The thesis was 'number go up'.",
        },
        "PROTOCOL_DEV": {
            "title": "Protocol Dev",
            "min_work": 750, "min_wealth": 25_000_000,
            "earn": (int(2_500 * _S), int(8_000 * _S)),
            "rig_slots": 96,
            "work_cooldown": 3600,
            "perks": {"daily_bonus": 0.45, "swap_fee": 0.0001, "stake_bonus": 0.25, "mining_bonus": 0.25, "interest_bonus": 0.25, "can_deploy_token": True},
            "description": "Audited six protocols. All six got exploited a month later.",
        },
        "EXPLOITER": {
            "title": "Exploiter",
            "min_work": 1000, "min_wealth": 100_000_000,
            "earn": (int(4_000 * _S), int(12_000 * _S)),
            "rig_slots": 128,
            "work_cooldown": 3600,
            "perks": {"daily_bonus": 0.50, "swap_fee": 0.0, "stake_bonus": 0.30, "mining_bonus": 0.30, "interest_bonus": 0.30, "can_create_pool": True, "can_deploy_token": True},
            "description": "Returned the funds. Kept a 10% 'bug bounty'. Untraceable.",
        },
        "WHITE_HAT": {
            "title": "White Hat",
            "min_work": 1300, "min_wealth": 300_000_000,
            "earn": (int(6_000 * _S), int(18_000 * _S)),
            "rig_slots": 192,
            "work_cooldown": 3600,
            "perks": {
                "daily_bonus": 0.55, "swap_fee": 0.0,
                "stake_bonus": 0.45, "mining_bonus": 0.28,
                "interest_bonus": 0.45,
                "ape_bonus": 0.20,
                "title_flair": "shield",
                "can_create_pool": True, "can_deploy_token": True,
            },
            "description": "Disclosed the 0-day. Got a $50k bounty for a $50M save.",
        },
        "CARTEL_BOSS": {
            "title": "Cartel Boss",
            "min_work": 1700, "min_wealth": 1_000_000_000,
            "earn": (int(9_000 * _S), int(27_000 * _S)),
            "rig_slots": 256,
            "work_cooldown": 3600,
            "perks": {
                "daily_bonus": 0.65, "swap_fee": 0.0,
                "stake_bonus": 0.32, "mining_bonus": 0.55,
                "interest_bonus": 0.30,
                "ape_bonus": 0.50,
                "title_flair": "megaphone",
                "can_create_pool": True, "can_deploy_token": True,
            },
            "description": "Run a Telegram pump group with 47k members and a Lambo.",
        },
        "L2_FOUNDER": {
            "title": "L2 Founder",
            "min_work": 2200, "min_wealth": 3_000_000_000,
            "earn": (int(14_000 * _S), int(42_000 * _S)),
            "rig_slots": 384,
            "work_cooldown": 3600,
            "perks": {
                "daily_bonus": 0.78, "swap_fee": 0.0,
                "stake_bonus": 0.55, "mining_bonus": 0.65,
                "interest_bonus": 0.50,
                "ape_bonus": 0.30,
                "title_flair": "sequencer",
                "can_create_pool": True, "can_deploy_token": True,
            },
            "description": "Raised $80M for a rollup. The rollup uses one sequencer (yours).",
        },
        "SATOSHI": {
            "title": "Satoshi",
            "min_work": 3000, "min_wealth": 10_000_000_000,
            "earn": (int(22_000 * _S), int(65_000 * _S)),
            "rig_slots": 512,
            "work_cooldown": 3600,
            "perks": {
                "daily_bonus": 1.00, "swap_fee": 0.0,
                "stake_bonus": 0.75, "mining_bonus": 0.75,
                "interest_bonus": 0.75,
                "ape_bonus": 1.00,
                "title_flair": "genesis",
                "can_create_pool": True, "can_deploy_token": True,
            },
            "description": "Disappeared in 2010. Wallet untouched. Still richer than you.",
        },
    }
    JOB_ORDER: list = [
        "HOMELESS", "TWITTER_SHILL", "AIRDROP_FARMER", "POAP_HUNTER",
        "LARPER", "WHITELIST_FARMER", "NFT_FLIPPER", "SHITCOIN_TRENCHER",
        "DISCORD_MOD", "CT_INFLUENCER", "DEFI_DEGEN", "YIELD_FARMER",
        "TRADER", "MEV_SEARCHER", "COURSE_SELLER", "ANALYST",
        "VALIDATOR_OP", "VC_PARTNER", "PROTOCOL_DEV", "EXPLOITER",
        "WHITE_HAT", "CARTEL_BOSS", "L2_FOUNDER", "SATOSHI",
    ]

    # ── Group Hall Upgrades ───────────────────────────────────
    # Hall-focused one-time purchases paid from the group reserve (USD).
    # See items_config.GROUP_HALL_UPGRADES for effect keys and tier info.
    GROUP_HALL_UPGRADES: dict = _GROUP_HALL_UPGRADES

    # ── Backup ────────────────────────────────────────────────
    BACKUP_INTERVAL_HOURS: int = _env_int("BACKUP_INTERVAL_HOURS", 6)
    BACKUP_KEEP: int = _env_int("BACKUP_KEEP", 7)
    BACKUP_MAX_AGE_DAYS: int = _env_int("BACKUP_MAX_AGE_DAYS", 0)  # 0 = disabled

    # ── Economy Snapshots ────────────────────────────────────
    SNAPSHOT_INTERVAL_MINUTES: int = _env_int("SNAPSHOT_INTERVAL_MINUTES", 30)
    SNAPSHOT_KEEP: int = _env_int("SNAPSHOT_KEEP", 48)  # 48 = 24h at 30-min intervals

    # ── Staking Validators ────────────────────────────────────
    # 5 validators per network (50 total). PoS networks use real staking services.
    # Sun Network uses PoW mining pools instead of PoS validators.
    # Net daily ~ uptime * reward_rate - (1 - uptime) * slash_rate * 24
    # Reward rates below are DAILY rates (not annual).
    # APY ≈ reward_rate * 365  (with DIVISOR=0.5, effective APY = reward_rate * 365 * 2)
    # REBALANCED: Staking competes with mining  -  a GTX1060 ($2,500) earns ~$200/day mining,
    # so a $2K stake should earn $20 - 75/day depending on validator risk tier.
    # Slash rates are scaled up so high-risk validators genuinely sting (~$5/day EV loss at $2K).
    # Expected net daily (at $2K stake) = reward - slash_EV:
    #   safe (~$20-32/day net), moderate (~$35-47/day net), high-risk (~$50-71/day net)
    # Higher-risk validators carry higher reward AND higher slash rates.
    VALIDATORS: dict = {
        # ── Arcadia Network ──────────────────────────────────────────────────
        # $2K stake net daily: CBETH ~$20, LIDO ~$24, RKTPL ~$35, SWISE ~$50, EIGENV ~$67
        "LIDO":   {"name": "Lido Finance",   "emoji": "💧", "network": "Arcadia Network", "uptime_rate": 0.995, "reward_rate": 0.00600, "slash_rate": 0.015},   # ~438% APY (safe)
        "CBETH":  {"name": "Coinbase Prime", "emoji": "🏦", "network": "Arcadia Network", "uptime_rate": 0.998, "reward_rate": 0.00500, "slash_rate": 0.010},   # ~365% APY (ultra-safe)
        "RKTPL":  {"name": "Rocket Pool",    "emoji": "🚀", "network": "Arcadia Network", "uptime_rate": 0.975, "reward_rate": 0.00900, "slash_rate": 0.035},   # ~657% APY (moderate)
        "EIGENV": {"name": "EigenLayer",     "emoji": "🔷", "network": "Arcadia Network", "uptime_rate": 0.880, "reward_rate": 0.01800, "slash_rate": 0.080},   # ~1314% APY (high risk restaking)
        "SWISE":  {"name": "StakeWise",      "emoji": "🌿", "network": "Arcadia Network", "uptime_rate": 0.930, "reward_rate": 0.01300, "slash_rate": 0.060},   # ~949% APY (moderate-high)

        # ── Discoin Network ────────────────────────────────────────────────────
        # Native Discoin validators. $2K stake net daily: DSCV1 ~$28, DSCV2 ~$39, DSCV3 ~$54, DSCV4 ~$71
        "DSCV1":  {"name": "Discoin Core",   "emoji": "🪙", "network": "Discoin Network", "uptime_rate": 0.997, "reward_rate": 0.00700, "slash_rate": 0.015},   # ~511% APY  -  safe, steady
        "DSCV2":  {"name": "Dis Validator",  "emoji": "⚡", "network": "Discoin Network", "uptime_rate": 0.970, "reward_rate": 0.01000, "slash_rate": 0.040},   # ~730% APY  -  balanced
        "DSCV3":  {"name": "Yield Engine",   "emoji": "📈", "network": "Discoin Network", "uptime_rate": 0.920, "reward_rate": 0.01400, "slash_rate": 0.065},   # ~1022% APY  -  higher risk
        "DSCV4":  {"name": "DSD Reserve",    "emoji": "💵", "network": "Discoin Network", "uptime_rate": 0.880, "reward_rate": 0.01900, "slash_rate": 0.090},   # ~1387% APY  -  high risk

        # ── Sun Network (PoW Mining Pools) ──────────────────────────────────────
        # SUN uses PoW mining, not PoS staking.
        # Mining pools listed here as pseudo-validators for pool payouts.
        # $2K stake net daily: SUNPL ~$32, SOLRH ~$47, NOVAMN ~$50
        "SUNPL":  {"name": "SunPool Prime",  "emoji": "☀",  "network": "Sun Network", "uptime_rate": 0.997, "reward_rate": 0.00800, "slash_rate": 0.015},   # ~584% APY (safe pool)
        "SOLRH":  {"name": "Solar Hashworks","emoji": "🔆", "network": "Sun Network", "uptime_rate": 0.975, "reward_rate": 0.01200, "slash_rate": 0.045},   # ~876% APY
        "NOVAMN": {"name": "Nova Hash Pool", "emoji": "✴",  "network": "Sun Network", "uptime_rate": 0.920, "reward_rate": 0.01300, "slash_rate": 0.065},   # ~949% APY (risky)
    }
    # Which token is accepted for staking on each PoS network
    NETWORK_STAKE_TOKEN: dict = {
        # Which coin is accepted for staking on each PoS network.
        # Sun Network is PoW-only  -  SUN cannot be staked, only mined.
        "Arcadia Network": "ARC",
        "Discoin Network":  "DSC",
    }
    # ── Wallet / DeFi ─────────────────────────────────────────
    # Percentage-based platform fee on CeFi→DeFi withdrawals and buy/sell trades.
    # 1/4 of every fee is deposited to the Community Reserve (user_id=0 savings pool).
    # Future: CREDIT_SCORE_ENABLED  -  repaying loans on time → higher credit tier → lower PCT multiplier.
    WALLET_PLATFORM_FEE_PCT: float = _env_float("WALLET_PLATFORM_FEE_PCT", 0.002)  # 0.2% of USD value (reduced from 0.5%)
    WALLET_PLATFORM_FEE_MIN: float = _env_float("WALLET_PLATFORM_FEE_MIN", 0.01)   # floor: $0.01 (human-scale USD; was $0.10 which made small token moves cost 100-200% in fees)
    WALLET_PLATFORM_FEE_MAX: float = _env_float("WALLET_PLATFORM_FEE_MAX", 20.00)  # cap: $20.00 (human-scale USD)

    # ── Pools ─────────────────────────────────────────────────
    POOL_ARB_THRESHOLD: float = 0.02    # 2% oracle deviation triggers rebalance (up from 0.5% - let pools lead price discovery)
    POOL_ARB_COOLDOWN:  int   = 300     # Minimum seconds between oracle rebalances per pool (5 min, up from 2 min)

    # ── Swap Limits ──────────────────────────────────────────
    MAX_SWAP_FRACTION: float = 0.40                  # max 40% of reserve per swap (relaxed for player trading)
    USER_SWAP_HOURLY_LIMIT_USD: float = 100_000_000.0         # per-user rolling 1-hour volume cap (human-scale USD)
    LOW_LIQUIDITY_THRESHOLD: int = int(100_000.0 * _S)        # pools below this TVL get stricter limits
    LOW_LIQUIDITY_SWAP_FRACTION: float = 0.20         # 20% max swap for thin pools (relaxed)
    FEE_BURN_FRACTION: float = 0.0                     # 0% burn -- all swap fees accrue to LPs (was 10%)

    # ── LP Protection ────────────────────────────────────────
    LP_LOCK_SECONDS: int = 7200                        # 2-hour minimum hold after adding LP
    LP_MAX_CONCENTRATION: float = 0.50                 # no single LP > 50% of pool
    LP_LARGE_REMOVAL_THRESHOLD: float = 0.25           # removals > 25% of pool need throttle

    # ── LP Time-Lock Boost ───────────────────────────────────
    # Opt-in commitment: lock a position for N days to earn a Liqstone-XP
    # multiplier on that specific LP. Tier 0 is the unlocked default.
    # Breaking a lock before locked_until burns LP_EARLY_UNLOCK_BURN of the
    # user's shares for that pool (burned = removed from both the user's
    # lp_shares and pool.total_lp, which concentrates value in other LPs).
    LP_LOCK_TIERS: dict = {
        1: {"days":  7, "xp_mult": 1.50, "label": "7d"},
        2: {"days": 30, "xp_mult": 2.50, "label": "30d"},
        3: {"days": 90, "xp_mult": 4.00, "label": "90d"},
    }
    LP_EARLY_UNLOCK_BURN: float = 0.10                 # 10% share burn to break a lock early

    # ── User-Created-Token LP Hooks ──────────────────────────
    # Providing liquidity on any user-created token pool (mining-group
    # token, tier-11 deploy, or admin-added -- anything in guild_tokens)
    # unlocks a small stack of bonuses so user-created tokens actually
    # matter beyond trading.
    #   * work / daily payout tilt: linear in USD LP exposure, capped.
    #   * Liqstone XP multiplier on any position where at least one side
    #     is a user-created token -- stacks multiplicatively with the
    #     time-lock tier multiplier, so 90d + user-token LP is the degen
    #     ceiling.
    USER_LP_WORK_BONUS_PER_USD: float = 0.00001       # +0.001% per $1 LP -> $1000 = +1%
    USER_LP_WORK_BONUS_CAP:     float = 0.08          # hard cap: +8% total
    USER_LP_LIQSTONE_MULT:      float = 1.30          # 1.3x Liqstone XP weight on user-created LP
    LP_LARGE_REMOVAL_COOLDOWN: int = 600               # 10-min cooldown between large removals

    # ── LP Yield Rewards ────────────────────────────────────
    # Inflation-style USD yield paid hourly to every LP provider, on top of
    # the natural swap-fee accrual baked into pool reserves. The fee model
    # alone is too thin to incentivize providing LP at this server's player
    # count -- a few dozen swappers can't generate meaningful per-share fee
    # income. This yield makes LP a first-class income stream so people
    # actually want to seed pools, which unblocks the rest of the economy.
    #
    # Multipliers stack: base APR * lock multiplier * (user-token? 1.5 : 1.0)
    # * (group-pool? 2.0 : 1.0). Per-tick payout is also capped per user so
    # whales can't drain the whole budget in one tick.
    LP_YIELD_APR: float = 0.60                         # 60% APR base on LP USD value
    LP_YIELD_TICK_HOURS: float = 1.0                   # pay every hour
    LP_YIELD_LOCK_BONUS: dict = {                      # multiplier by lock_tier
        0: 1.00,                                       # unlocked
        1: 1.30,                                       # 7d lock  -> +30%
        2: 1.75,                                       # 30d lock -> +75%
        3: 2.50,                                       # 90d lock -> +150%
    }
    LP_YIELD_USER_TOKEN_BONUS: float = 1.50            # 1.5x for pools with a user-created token side
    LP_YIELD_GROUP_POOL_BONUS: float = 2.00            # 2x for cross-group partnership pools (paid to group reserve)
    LP_YIELD_MIN_USD: float = 1.00                     # skip positions worth less than $1
    LP_YIELD_MAX_PER_TICK_USD: float = 5_000.0         # per-user cap per tick

    # ── LP Bootstrap (pool-seeder) Incentive ─────────────────
    # Pays a per-tick yield bonus to LP positions in low-liquidity / low-
    # volume pools so the FIRST seeders into a brand-new pool have a
    # reason to plant capital before swap fees alone can sustain them.
    # Bonus diminishes as TVL grows AND as recent trade volume picks up,
    # so once a pool starts moving the bonus naturally tapers off.
    #
    # bootstrap_mult = 1 + (BOOTSTRAP_MAX_BONUS - 1) * tvl_factor * volume_factor
    #   tvl_factor    = max(0, 1 - tvl_usd       / BOOTSTRAP_TVL_THRESHOLD_USD)
    #   volume_factor = max(0, 1 - recent_vol_usd / BOOTSTRAP_VOLUME_THRESHOLD_USD)
    # At TVL=0 and volume=0 the bonus is at its full BOOTSTRAP_MAX_BONUS;
    # at either threshold the bonus is fully decayed back to 1.0. The
    # multiplier compounds with the existing lock / user-token / group-
    # pool multipliers in services/lp_yield.py.
    LP_BOOTSTRAP_MAX_BONUS: float           = 5.00     # up to 5x base APR on a $0/$0 pool
    LP_BOOTSTRAP_TVL_THRESHOLD_USD: float   = 10_000.0 # bonus fully decays once TVL >= $10k
    LP_BOOTSTRAP_VOLUME_THRESHOLD_USD: float = 5_000.0 # bonus fully decays once recent vol >= $5k
    # Recent-volume window decay: the rolling counter on pools is reduced
    # by this fraction every tick so a pool that traded a lot but went
    # quiet eases back into bonus eligibility over time.
    LP_BOOTSTRAP_VOLUME_DECAY_PER_TICK: float = 0.10   # -10% per LP-yield tick

    # ── Group Pool Auto-Seeding ──────────────────────────────
    # When two groups accept a pool partnership, each contributes this fraction
    # of their vault_token_bal (in USD value) as the initial seed liquidity.
    # Both sides always contribute equal USD so LP is split 50/50.
    GROUP_POOL_SEED_PCT: float = 0.05          # 5% of vault balance (in USD) per group
    GROUP_POOL_SEED_MIN_USD: float = 5.0       # skip seeding if contribution < $5
    GROUP_POOL_SEED_MAX_USD: float = 200.0     # cap at $200 per group
    GROUP_POOL_HARVEST_COOLDOWN: int = 86400   # 24 h between LP harvests per group/pool

    # ── Group Token Genesis Pools ────────────────────────────
    # When a group is created, the bot system-mints liquidity for two
    # AMM pools on its new token so the token is actually tradeable the
    # instant the group exists:
    #   * TOKEN / DSD  -- stablecoin pair, bidirectional price discovery
    #   * TOKEN / MOON -- Moon Network off-ramp (MOON -> TOKEN only,
    #     because EARN_ONLY_TOKENS blocks swapping back out of MOON;
    #     the one-way pool still gives Lunar Mint stakers a way to
    #     convert earned MOON into community tokens).
    # "System-minted" means the seed liquidity comes from thin air; it
    # does not touch circulating supply on either side and does not cost
    # the founder anything. The USD-side value is small by design so
    # early trades still move price meaningfully (thin pools = degen).
    GROUP_TOKEN_GENESIS_SEED_USD: float = 200.0   # $200 per side per pool

    # ── Group Vault Pool Seed ───────────────────────────────
    # When a group picks its mining-chain network (.group token network sun|mta),
    # the bot creates a vault-locked TOKEN/PoW-coin pool that acts as the
    # group's treasury anchor -- reserves grow as the group mines blocks. It
    # is vault_locked so players can't swap against it, but the reserve values
    # still show up in the pool panel / treasury view, so they need to be
    # large enough to read like real liquidity (not 42-cent dust). Seeded as
    # $GROUP_VAULT_POOL_SEED_USD per side at current oracle prices.
    GROUP_VAULT_POOL_SEED_USD: float = 10_000.0

    # ── Lending ───────────────────────────────────────────────
    LENDING: dict = {
        "MAX_LTV": 0.65,
        "DAILY_RATE": 0.02,
        "LIQUIDATION_THRESHOLD": 0.80,
        "LIQUIDATION_PENALTY": 0.05,         # 5% burned on liquidation
        "INTEREST_TICK": 1800,               # check every 30 min (down from 1h)
        "COLLATERAL_SEASONING": 3600,        # 1-hour hold required before collateral eligible
    }

    # ── Savings / Market Rate Model ───────────────────────────
    # Vantor V2-style utilization kink model.
    # Borrow rate (daily) = base_rate + slope1 * (util / opt_util)        [util ≤ opt]
    #                     = base_rate + slope1 + slope2 * ((util-opt)/(1-opt))  [util > opt]
    # Savings rate (daily) = max(base_savings_rate, borrow_rate * utilization * (1 - reserve_factor))
    # base_savings_rate ensures savers always earn something even with 0 borrowing.
    SAVINGS_RATE_MODEL: dict = {
        "optimal_utilization": 0.80,    # kink point
        "base_rate":           0.0005,  # min borrow rate/day (0.05%  -  10x reduction from 0.5%)
        "slope1":              0.0015,  # rate at kink = base + slope1 = 0.20%/day (~73% APY)
        "slope2":              0.015,   # steep slope above kink (up to ~1.7%/day at 100% util)
        "reserve_factor":      0.15,    # 15% of interest kept as protocol reserve (up from 10%)
        "base_savings_rate":   0.000165, # 0.0165%/day ≈ 6% APY guaranteed floor so passive saving still feels worthwhile
        "min_deposit":         int(1.0 * _S),  # minimum savings deposit (raw scaled int)
    }

    # ── Safety Module (VTR/DSY staking) ─────────────────────
    # Mirrors Vantor Safety Module: stake yield tokens to earn protocol fees.
    # Yield is paid in the network stablecoin (USDC for VTR, DSD for DSY).
    # Liquidity mining (lm_daily) rewards savers who deposit stablecoins.
    SAFETY_MODULE: dict = {
        "VTR": {
            "network":              "arc",
            "yield_token":          "USDC",  # earned by Safety Module stakers
            # Emission-based variable APY: daily_rate = emission_usd_per_day / total_staked_usd.
            # High when TVL is tiny (up to max_apy_pct), compresses as staking grows,
            # but never below min_apy_pct regardless of TVL.
            "emission_usd_per_day": 50000.0,  # $50k/day keeps APY generous at high TVL
            "max_apy_pct":          10000.0,  # cap at 10,000% APY
            "min_apy_pct":          50.0,     # floor at 50% APY
            "lm_token":             "VTR",  # earned by USDC savers (liquidity mining)
            "lm_daily":             0.000137,
            "cooldown_secs":        86400,   # 24h unstake cooldown
            "slash_rate":           0.10,    # 10% burned in shortfall event
            "min_stake":            0.001,
        },
        "DSY": {
            "network":              "dsc",
            "yield_token":          "DSD",
            "emission_usd_per_day": 50000.0,
            "max_apy_pct":          10000.0,
            "min_apy_pct":          50.0,
            "lm_token":             "DSY",
            "lm_daily":             0.000137,
            "cooldown_secs":        86400,
            "slash_rate":           0.10,
            "min_stake":            1.0,
        },
    }

    # ── Oracle ────────────────────────────────────────────────
    MAX_TICK_CHANGE: float = 0.03              # legacy fallback cap (overridden by regime caps below)
    LOG_LARGE_PRICE_MOVES: bool = True         # warn when single tick exceeds 1.5%
    ORACLE_TWAP_WINDOW: int = 40               # ticks of candle history for TWAP (10 min at 15s - shorter so TWAP tracks rising prices faster)
    ORACLE_REVERSION_STRENGTH: float = 0.007   # 0.7% pull toward TWAP per tick (down from 2% - allows sustained moves)
    ORACLE_RECOVERY_BIAS: float = 60.0         # % daily upward drift injected when price < start_price (scales with undervaluation)
    ORACLE_DAILY_MAX_DRIFT: float = 0.30       # +/-30% daily drift limit (up from 20% - gives room for real rallies)
    ORACLE_CAP_NORMAL: float = 0.018           # 1.8% per-tick cap when within 1 stddev (up from 1.5%)
    ORACLE_CAP_CAUTIOUS: float = 0.013         # 1.3% per-tick cap when 1-2 stddev (up from 1.0%)
    ORACLE_CAP_CONTAINMENT: float = 0.010      # 1.0% per-tick cap when >2 stddev (up from 0.5% - was killing breakouts)

    # ── Depeg Protection ─────────────────────────────────────
    # Disabled: no buy caps, no recovery throttling.
    # The oracle recovery bias handles price movement organically.
    DEPEG_THRESHOLD: float = 0.0              # DISABLED - never triggers depeg mode
    DEPEG_DAILY_BUY_USD: float = 999_999_999  # effectively unlimited
    ORACLE_RECOVERY_CAP: float = 0.30         # matches ORACLE_DAILY_MAX_DRIFT (no tightening)

    # ── MEV / Sandwich Protection ────────────────────────────
    MEV_SHUFFLE_WITHIN_TIER: bool = True       # randomize tx order within same gas tier
    MEV_VALIDATOR_LAST: bool = True            # validator's own txs execute last in their block
    MEV_MAX_SWAPS_PER_USER_PER_BLOCK: int = 2  # max swaps per user per validator block
    MICRO_SWAP_MIN_USD: float = 1.0             # minimum USD value of a swap; below this is rejected
    MICRO_SWAP_VALIDATOR_SLASH_RATE: float = 0.05  # 5% slash rate applied to validators caught doing micro-swaps

    # ── Staking ──────────────────────────────────────────────
    # Reward formula per hourly tick:
    #   reward = stake_amount * reward_rate / DIVISOR / 24 * warmup_factor * (1+bonus)
    # With DIVISOR=1 and reward_rate as an annual %, e.g. 0.04 = 4% APY:
    #   hourly reward = amount * 0.04 / 1 / 24 / 365  (too small  -  game uses /1 daily-rate style)
    # Convention: reward_rate in VALIDATORS is the DAILY rate (not annual).
    #   e.g. LIDO reward_rate=0.00011 → 0.011%/day → ~4% APY  (realistic ARC staking)
    # STAKING_REWARD_DIVISOR scales reward_rate down further. With DIVISOR=1 and daily rates:
    #   hourly = amount * daily_rate / 24
    STAKING_REWARD_DIVISOR: float = 0.5        # DIVISOR=0.5 effectively doubles rewards (amount / 0.5 = amount * 2)
    STAKING_WARMUP_SECONDS: int = 43200        # 12h linear ramp to full rewards so new stakes start paying sooner
    STAKING_SLASH_TICK_DIVISOR: float = 96.0   # spread slash risk across hourly ticks so validators stay risky, not mathematically doomed
    STAKING_EARLY_UNSTAKE_WINDOW: int = 172800  # 48h (2-day) window for early unstake penalty
    STAKING_EARLY_UNSTAKE_PENALTY: float = 0.05  # 5% burn on early unstake
    # ── Hashrate imbalance staking bonus ─────────────────────────────────────
    # When one PoW network has far fewer miners than its peer, validators tied to
    # the underdog network get a proportional bonus to attract participation:
    #   Sun Network validators → bonus when SUN mining hashrate < MTA × THRESHOLD
    #   ARC / DSC validators   → bonus when MTA hashrate < SUN × THRESHOLD
    # Bonus scales linearly from 0 (at threshold) to BONUS_MAX (network has 0 miners).
    STAKING_IMBALANCE_BONUS_MAX: float = 0.50  # up to +50% staking reward when peer network dominates
    STAKING_IMBALANCE_THRESHOLD: float = 0.50  # kicks in when one network has <50% of peer hashrate

    # ── Work Income Scaling ──────────────────────────────────
    # Progressive tax: above threshold, earnings are taxed at the excess rate.
    WORK_PROGRESSIVE_TAX_THRESHOLD: int = int(5_000.0 * _S)    # earnings above this are taxed
    WORK_PROGRESSIVE_TAX_RATE: float = 0.65            # 65% tax on excess (steeper to curb top-tier inflation)

    # Per-player daily work income cap by job tier (USD, before progressive tax).
    # Prevents sessions-per-day manipulation from causing runaway supply growth.
    # Formula: cap = earn_max * sessions_per_tier_per_day * cap_multiplier
    # These are generous  -  active players hit cap only if they work at maximum frequency.
    WORK_DAILY_CAP: dict = {
        "HOMELESS":          int(        800 * _S),
        "TWITTER_SHILL":     int(      1_200 * _S),
        "AIRDROP_FARMER":    int(      2_000 * _S),
        "POAP_HUNTER":       int(      3_000 * _S),
        "LARPER":            int(      5_000 * _S),
        "WHITELIST_FARMER":  int(     12_000 * _S),
        "NFT_FLIPPER":       int(     18_000 * _S),
        "SHITCOIN_TRENCHER": int(     25_000 * _S),
        "DISCORD_MOD":       int(     42_000 * _S),
        "CT_INFLUENCER":     int(     53_000 * _S),
        "DEFI_DEGEN":        int(     65_000 * _S),
        "YIELD_FARMER":      int(     78_000 * _S),
        "TRADER":            int(     90_000 * _S),
        "MEV_SEARCHER":      int(    105_000 * _S),
        "COURSE_SELLER":     int(    120_000 * _S),
        "ANALYST":           int(    140_000 * _S),
        "VALIDATOR_OP":      int(    160_000 * _S),
        "VC_PARTNER":        int(    180_000 * _S),
        "PROTOCOL_DEV":      int(    200_000 * _S),
        "EXPLOITER":         int(    250_000 * _S),
        "WHITE_HAT":         int(    320_000 * _S),
        "CARTEL_BOSS":       int(    420_000 * _S),
        "L2_FOUNDER":        int(    560_000 * _S),
        "SATOSHI":           int(    750_000 * _S),
    }

    # ── XP Scaling ───────────────────────────────────────────
    XP_STAKE_REFERENCE_USD: float = 25_000.0   # staking $25K = 1x XP rate
    XP_SAVINGS_REFERENCE_USD: float = 25_000.0 # saving $25K = 1x XP rate
    XP_SCALE_MAX: float = 3.0                  # cap: 3x base XP

    # ── Wealth-Scaled Daily ──────────────────────────────────
    # ── Wealth Bottleneck (rank-based gain throttle + inline UBI) ────
    # Replaces the legacy "Wealth Equalizer" (daily wealth tax + UBI cycle)
    # and the V3 "Continuous Wealth Equalizer" (Gini PI controller +
    # streaming UBI ticks). The bottleneck never touches existing holdings
    # (stones, bags, rigs, NFTs, savings deposits, validator stakes,
    # delegations, mining rigs, LP positions, gamba stakes, moon stakes
    # are all permanently off-limits). It only scales each fresh credit
    # by a leaderboard-rank-based multiplier:
    #   poorest -> 1.50x boost, median -> 1.00x neutral, richest -> 0.10x drag.
    # Drag taken off the top of the leaderboard funds a per-guild USD
    # pool; boost paid to the bottom is drawn from that pool. When the
    # pool is empty the boost falls to 1.0x (no inflation).
    BOTTLENECK_ENABLED: bool = True
    # Curve anchors. Each entry is (percentile, multiplier). Percentiles
    # outside [0, 1] clamp to the endpoints; values between anchors are
    # linearly interpolated. Median (0.50) sits at exactly 1.00 so the
    # bottom half is boosted and the top half is dragged. The default
    # mirrors the curve documented in ``services.bottleneck``; tweak in
    # place to retune without code change.
    BOTTLENECK_CURVE: list = [
        (0.00, 1.50),
        (0.25, 1.20),
        (0.50, 1.00),
        (0.75, 0.85),
        (0.90, 0.55),
        (0.99, 0.20),
        (1.00, 0.10),
    ]
    # Small-server gate: guilds with fewer than this many ranked holders
    # bypass the bottleneck entirely (every credit is 1.0x). Prevents a
    # solo / two-player guild from dragging the only active player.
    BOTTLENECK_MIN_HOLDERS: int = 5
    # Boost cap as a multiple of the gross USD-equivalent of the credit.
    # 1.0 = the boost can at most double a single credit, regardless of
    # how flush the pool is. Stops a single ,beg from minting a fortune
    # for a brand-new player when the pool is large.
    BOTTLENECK_MAX_BOOST_MULTIPLE_OF_GROSS: float = 1.0

    # V3 Pillar 2: Apex Mastery knobs.
    MASTERY_RESET_BASE_USD: float = 25_000.0
    # V3 Pillar 6: Apex Events knobs.
    APEX_EVENTS_ENABLED: bool = True
    APEX_EVENT_TICK: int = 30           # seconds between roll attempts
    APEX_EVENT_ROLL_PCT: float = 0.05   # probability of starting a new event per tick
    # V3 Pillar 3: Clan Wars knobs.
    CLAN_WARS_ENABLED: bool = True
    CLAN_WARS_TICK: int = 60            # seconds between matchmaking / settle ticks
    CLAN_WARS_DURATION_DAYS: int = 7

    # ── Adaptive Faucet (auto-scale by per-capita money supply) ──────
    # Auto-faucet drops were nominally "GDP-scaled" but the only knob was a
    # static admin multiplier, so they didn't react to a server's actual
    # money supply. The adaptive multiplier inspects bulk net-worth to
    # tilt faucet payouts so a poor server keeps generous drops while a
    # mature, supply-heavy server dials them back. Curve:
    #   mult = clamp(REF / (REF + per_capita), MIN_MULT, MAX_MULT)
    # Per-capita == REF -> mult ~ 0.5 (cuts payouts in half); per-capita
    # << REF -> mult ramps up to MAX_MULT; per-capita >> REF -> floors at
    # MIN_MULT. Stacks multiplicatively with the existing ``faucet_multiplier``
    # admin override, so operators retain final say.
    FAUCET_ADAPTIVE_ENABLED: bool = True
    FAUCET_ADAPTIVE_REFERENCE_USD: float = 50_000.0
    FAUCET_ADAPTIVE_MIN_MULT: float = 0.20
    FAUCET_ADAPTIVE_MAX_MULT: float = 3.00

    # ── Whale Alerts ─────────────────────────────────────────
    WHALE_ALERT_THRESHOLD_USD: int = int(_env_float("WHALE_ALERT_THRESHOLD_USD", 50000.0) * _S)

    # ── Chain Blocks (deterministic transaction bundler) ───────
    CHAIN_BLOCK_INTERVAL: int = _env_int("CHAIN_BLOCK_INTERVAL", 1800)

    # ── Rugpull Minigame ────────────────────────────────────────
    RUGPULL_ROLE_ID: int = _env_int("RUGPULL_ROLE", 0)
    # Queen of Rugs role is awarded when the monarch is female. Same benefits as King.
    RUGPULL_QUEEN_ROLE_ID: int = _env_int("RUGPULL_QUEEN_ROLE", 0)
    RUGPULL_WORK_BONUS: float = 0.05     # base 5% extra cash from work
    RUGPULL_APE_BONUS: float = 0.10      # base 10% bonus on ape payouts
    RUGPULL_MAX_WORK_BONUS: float = 0.15  # cap at 15% after long reign
    RUGPULL_MAX_APE_BONUS: float = 0.25   # cap at 25% after long reign
    RUGPULL_PERK_HOURS: int = 24          # hours to reach max reign perks
    RUGPULL_DEFENSE_BONUS: float = 0.02   # +2% success chance per defense streak
    RUGPULL_MAX_DEFENSE_BONUS: float = 0.15  # cap defense streak bonus at 15%
    RUGPULL_SABOTAGE_DECAY: float = 0.10  # each $1 in sabotage pool = -10% defense bonus
    RUGPULL_MIN_BOUNTY: int = int(100.0 * _S)     # minimum bounty placement
    RUGPULL_MIN_TAX: float = 0.25         # king can't go below 25% tax
    RUGPULL_COOLDOWN: int = 30            # seconds between rugpull attempts
    # Crown discount: challengers pay 50% less to rug a monarch by default, with extra
    # discount that grows linearly the longer the monarch has held the crown. After
    # RUGPULL_CROWN_DISCOUNT_HOURS the discount reaches RUGPULL_CROWN_MAX_DISCOUNT.
    RUGPULL_CROWN_DISCOUNT: float = 0.50
    RUGPULL_CROWN_MAX_DISCOUNT: float = 0.85
    RUGPULL_CROWN_DISCOUNT_HOURS: int = 48
    # Active monarch defense (paid from wallet, like ,fortify in the Eat the Rich game).
    # Each USD spent buys ``RUGPULL_DEFEND_PCT_PER_USD`` defense bonus (capped).
    RUGPULL_DEFEND_MIN_USD: int = int(50.0 * _S)
    RUGPULL_DEFEND_PCT_PER_USD: float = 0.0004   # +0.04% per $1 spent
    RUGPULL_DEFEND_MAX_BONUS: float = 0.40       # cap monarch-funded defense at 40%
    RUGPULL_DEFEND_DURATION: int = 7200          # active defense lasts 2h
    RUGPULL_DEFEND_COOLDOWN: int = 3600          # 1h between paid defenses
    # Tiers: cost_pct of net worth (not just wallet balance)
    RUGPULL_TIERS: dict = {
        "low":    {"cost_pct": 0.004, "min_cost": int(50 * _S),   "success": 0.05},
        "medium": {"cost_pct": 0.020, "min_cost": int(250 * _S),  "success": 0.40},
        "high":   {"cost_pct": 0.040, "min_cost": int(500 * _S),  "success": 0.75},
    }

    # ── Eat the Rich Minigame ─────────────────────────────────
    EAT_COOLDOWN: int = 120                 # seconds between eat attempts
    EAT_FORTIFY_COST: int = int(500.0 * _S)         # cost to hire a security detail
    EAT_FORTIFY_DURATION: int = 7200        # security detail lasts 2 hours
    EAT_FORTIFY_COOLDOWN: int = 14400       # 4h cooldown between hires
    EAT_FAIL_PENALTY_PCT: float = 0.30      # lose 30% of stake on failure

    # Reward model. A successful eat removes a GROSS slice of the target's
    # liquid wealth. The slice scales with the wealth gap -- the further you
    # punch up, the bigger the bite -- ramping linearly from EAT_STEAL_PCT_MIN
    # at parity to EAT_STEAL_PCT_MAX once the target is EAT_GAP_FULL_X times
    # richer. The gross is then SPLIT by the chosen tactic (see EAT_TACTICS).
    # EAT_MAX_STEAL caps any single eat -- but a COOKED eat lifts the cap
    # entirely. The leaderboard runs on billions, so these are large.
    EAT_STEAL_PCT_MIN: float = 0.02         # 2% of the pool at parity
    EAT_STEAL_PCT_MAX: float = 0.12         # 12% of the pool at a wide gap
    EAT_GAP_FULL_X: float = 10.0            # wealth-gap multiple that reaches MAX
    EAT_MAX_STEAL: int = int(100_000_000_000.0 * _S)   # $100B cap (lifted when cooked)
    EAT_STEAL_VARIANCE: float = 0.15        # +/- random swing on the gross take

    # Wealth-gap success bonus: each 1x of gap above parity adds odds.
    EAT_GAP_BONUS_PER_X: float = 0.05
    EAT_GAP_BONUS_CAP: float = 0.20
    EAT_CHANCE_CAP: float = 0.95

    # Anti-whale. Punching DOWN (a target who is not richer than you) is
    # allowed but discouraged: success odds are slashed and a loss costs a
    # flat "class traitor" fee on top of the normal stake penalty. The
    # poorest ACTIVE player stays fully uneatable regardless.
    EAT_PUNCH_DOWN_ODDS_MULT: float = 0.5
    EAT_CLASS_TRAITOR_FEE: int = int(2500.0 * _S)
    EAT_ACTIVE_DAYS: int = 30               # "active" window for the poorest-player rule

    # The three tactic buttons. Each is a free choice every eat; the button
    # decides how the GROSS steal is split four ways. keep -> the attacker,
    # burn -> destroyed forever, bowl -> the multi-currency salad bowl,
    # airdrop -> shared among the poorest active players. The four fractions
    # of each tactic MUST sum to exactly 1.0.
    EAT_TACTICS: dict = {
        "skim": {
            "cost_pct": 0.01, "min_cost": int(50 * _S), "success": 0.60,
            "keep_pct": 0.10, "burn_pct": 0.00, "bowl_pct": 0.90, "airdrop_pct": 0.00,
            "steal_mult": 1.0, "label": "🥄 Skim", "type_no": 1,
        },
        "shakedown": {
            "cost_pct": 0.03, "min_cost": int(150 * _S), "success": 0.58,
            "keep_pct": 0.20, "burn_pct": 0.05, "bowl_pct": 0.50, "airdrop_pct": 0.25,
            "steal_mult": 1.0, "label": "🔪 Shakedown", "type_no": 2,
        },
        "guillotine": {
            "cost_pct": 0.05, "min_cost": int(300 * _S), "success": 0.55,
            "keep_pct": 0.50, "burn_pct": 0.25, "bowl_pct": 0.25, "airdrop_pct": 0.00,
            "steal_mult": 1.0, "label": "🗡️ Guillotine", "type_no": 3,
        },
    }
    EAT_AIRDROP_RECIPIENTS: int = 6         # poorest active players an airdrop is split among

    # ,eat bite -- precision strike on one named balance pool (wallet / crypto
    # / defi / bank). Uses the same three tactic buttons (and stakes) as a
    # plain eat; the gross comes only from that pool and is split + paid in
    # that pool's own asset type.
    EAT_BITE_MIN_POOL: int = int(100.0 * _S)        # pools below this are "bone dry"
    EAT_BITE_MIN_COST: int = int(150.0 * _S)        # minimum wager for a bite
    EAT_BITE_COST_PCT: float = 0.03                  # 3% of wallet as wager
    EAT_BITE_SUCCESS: float = 0.58                   # base success odds for a bite
    EAT_BITE_STEAL_MULT: float = 1.0                 # gross steal multiplier for bites

    # ,eat prep -> ,eat cook -- the two-stage powerup chain. Each command
    # spends a fee, then CHARGES for its duration; once charged the powerup
    # is "armed" and is consumed by the next eat. Cook requires an armed prep
    # first. Prep cases the joint (target intel + bypasses their security);
    # cook cooks the books (uncaps the steal + redirects the burn slice into
    # your own cut) and is the key that unlocks ,eat salad.
    EAT_PREP_COST: int = int(2_500.0 * _S)
    EAT_PREP_DURATION: int = 300            # prep charges for 5 min
    EAT_PREP_COOLDOWN: int = 120            # ,eat prep command cooldown
    EAT_COOK_COST: int = int(10_000.0 * _S)
    EAT_COOK_DURATION: int = 300            # cook charges for 5 min
    EAT_COOK_COOLDOWN: int = 120            # ,eat cook command cooldown
    EAT_COOK_BONUS: float = 0.12            # +12% success odds when prep is armed
    EAT_COOK_WINDOW: int = 300             # seconds the prep buff stays armed

    # ,eat salad -- a 1% gamble on the whole multi-currency salad bowl.
    # Requires an armed cook (consumes prep + cook). Win: take
    # EAT_SALAD_WIN_PCT of every currency in the bowl, the rest burns
    # forever. Loss: EAT_SALAD_LOSS_BURN_PCT of the bowl burns forever.
    EAT_SALAD_WIN_CHANCE: float = 0.01
    EAT_SALAD_WIN_PCT: float = 0.05
    EAT_SALAD_LOSS_BURN_PCT: float = 0.05

    # ── Security Engine ───────────────────────────────────────
    # Set SECURITY_SYSTEM=false in .env to disable the security/anti-abuse engine entirely.
    SECURITY_SYSTEM: bool = _env_bool("SECURITY_SYSTEM", True)

    # ── Debug ─────────────────────────────────────────────────
    DEBUG: bool = _env_bool("DEBUG", False)

    # ── REST API ──────────────────────────────────────────────
    API_PORT: int | None = _env_first_int("PORT", "API_PORT", default=8080)
    # Public-facing dashboard URL (used to generate deep-links in Discord embeds)
    # Set DASHBOARD_URL env var to your dashboard's public address, e.g. https://econbot.example.com
    DASHBOARD_URL: str = os.getenv("DASHBOARD_URL", "")

    # ── Price Impact ──────────────────────────────────────────
    PRICE_IMPACT_DIVISOR: float = 5_000_000.0
    # $5,000 trade = 0.1% impact | $50,000 = 1% | $200,000 = 4%

    # ── Anti-Bot ──────────────────────────────────────────────
    # Consecutive same-game plays before a CAPTCHA is triggered (random in range).
    ANTIBOT_MIN_GAMES: int = _env_int("ANTIBOT_MIN_GAMES", 50)
    ANTIBOT_MAX_GAMES: int = _env_int("ANTIBOT_MAX_GAMES", 100)

    # ── Shop items ────────────────────────────────────────────
    # Defined in items_config.py  -  edit that file to add or tweak items.
    SHOP_ITEMS: dict = _SHOP_ITEMS

    # ── Token Labels ──────────────────────────────────────────

    @classmethod
    def currency_label(cls, symbol: str, detail: bool = False) -> str:
        """Return a human-readable label for a token symbol.

        Basic:   "☀ SUN"
        Detail:  "Sun (☀ SUN)"
        """
        tok = cls.TOKENS.get(symbol.upper())
        if tok is None:
            return symbol.upper()
        emoji = tok.get("emoji", "●")
        name = tok.get("name", symbol.upper())
        if detail:
            return f"{name} ({emoji} {symbol.upper()})"
        return f"{emoji} {symbol.upper()}"

    # ── Real-crypto market feed (CoinGecko) ───────────────────
    # The $-prefixed real-crypto commands ($chart / $info) fetch live OHLC,
    # market data, and news from CoinGecko. The free tier needs no key; set
    # COINGECKO_API_KEY to use a Pro tier with higher rate limits.
    REAL_MARKET_ENABLED:           bool = _env_bool("REAL_MARKET_ENABLED", True)
    REAL_MARKET_API_BASE:          str  = os.getenv("REAL_MARKET_API_BASE", "https://api.coingecko.com/api/v3")
    REAL_MARKET_CACHE_TTL_OHLC:    int  = _env_int("REAL_MARKET_CACHE_TTL_OHLC", 60)
    REAL_MARKET_CACHE_TTL_OVIEW:   int  = _env_int("REAL_MARKET_CACHE_TTL_OVIEW", 60)
    REAL_MARKET_CACHE_TTL_NEWS:    int  = _env_int("REAL_MARKET_CACHE_TTL_NEWS", 300)
    REAL_MARKET_CACHE_TTL_SYMBOL:  int  = _env_int("REAL_MARKET_CACHE_TTL_SYMBOL", 86400)
    REAL_MARKET_CACHE_TTL_GLOBAL:  int  = _env_int("REAL_MARKET_CACHE_TTL_GLOBAL", 120)
    REAL_MARKET_CACHE_TTL_MARKETS: int  = _env_int("REAL_MARKET_CACHE_TTL_MARKETS", 120)
    REAL_MARKET_CACHE_TTL_TRENDING: int = _env_int("REAL_MARKET_CACHE_TTL_TRENDING", 300)
    REAL_MARKET_CACHE_TTL_FNG:     int  = _env_int("REAL_MARKET_CACHE_TTL_FNG", 600)
    REAL_MARKET_CACHE_TTL_TICKERS: int  = _env_int("REAL_MARKET_CACHE_TTL_TICKERS", 120)
    REAL_MARKET_HTTP_TIMEOUT:      int  = _env_int("REAL_MARKET_HTTP_TIMEOUT", 10)
    COINGECKO_API_KEY:             str  = os.getenv("COINGECKO_API_KEY", "")
    # Alternative.me Fear & Greed Index -- public free API, no key required.
    REAL_MARKET_FNG_BASE:          str  = os.getenv("REAL_MARKET_FNG_BASE", "https://api.alternative.me/fng/")

    # ── Cross-asset market providers (all optional) ────────────────
    # Every provider here is additive. The router skips a provider that's
    # disabled / missing its key and falls through to the next one in the
    # capability matrix. The bot still serves $help and $chart mta 1d with
    # none of these set.
    #
    # Equities / ETFs / forex / indices / commodities -- Yahoo Finance has
    # public endpoints that work without a key. Set YAHOO_ENABLED=0 to turn
    # it off if a deploy has its own ToS concerns.
    YAHOO_ENABLED:        bool = _env_bool("YAHOO_ENABLED", True)
    # Finnhub -- equities news, fundamentals, earnings calendar.
    # Free tier: 60 req/min. Sign up at https://finnhub.io.
    FINNHUB_API_KEY:      str  = os.getenv("FINNHUB_API_KEY", "")
    # DexScreener -- DEX pairs across most chains. Public API, no key.
    DEXSCREENER_ENABLED:  bool = _env_bool("DEXSCREENER_ENABLED", True)
    # Pyth Hermes -- realtime oracle prices, no key. Override only if you
    # run a private Hermes endpoint.
    PYTH_HERMES_URL:      str  = os.getenv("PYTH_HERMES_URL", "https://hermes.pyth.network")
    # RedStone -- oracle backup. Public gateway; override to a private one
    # for higher throughput.
    REDSTONE_GATEWAY_URL: str  = os.getenv(
        "REDSTONE_GATEWAY_URL",
        "https://oracle-gateway-1.a.redstone.finance",
    )
    # Switchboard -- oracle reads via the public Crossbar gateway
    # (https://crossbar.switchboard.xyz). No SDK, no on-chain RPC, no
    # keypair required. SWITCHBOARD_FEEDS is a JSON map of
    # ``{"SYMBOL/USD": "0x<feed-hash>"}`` -- the operator wires real
    # feed hashes from https://ondemand.switchboard.xyz/. With it empty
    # the provider stays disabled and the router falls through to
    # Pyth + RedStone (which together cover every major).
    SWITCHBOARD_RPC_URL:        str = os.getenv("SWITCHBOARD_RPC_URL", "")
    SWITCHBOARD_CROSSBAR_URL:   str = os.getenv("SWITCHBOARD_CROSSBAR_URL", "https://crossbar.switchboard.xyz")
    SWITCHBOARD_NETWORK:        str = os.getenv("SWITCHBOARD_NETWORK", "solana/mainnet")
    SWITCHBOARD_FEEDS:          str = os.getenv("SWITCHBOARD_FEEDS", "")
    # CoinGlass -- perp funding / OI / liquidations / long-short ratio.
    # Free tier requires a key. Sign up at https://www.coinglass.com.
    COINGLASS_API_KEY:    str  = os.getenv("COINGLASS_API_KEY", "")
    # Coinalyze -- derivatives backup. Free key from https://coinalyze.net.
    COINALYZE_API_KEY:    str  = os.getenv("COINALYZE_API_KEY", "")
    # TradingView UDF feed -- only used when a self-hosted UDF URL is set.
    # Leave blank for vanilla deploys; we never reach out to TradingView.com
    # directly.
    TRADINGVIEW_UDF_URL:  str  = os.getenv("TRADINGVIEW_UDF_URL", "")

    # ── Extra cache TTLs the new provider layer reads ──────────────
    CACHE_TTL_QUOTE:        int = _env_int("CACHE_TTL_QUOTE", 10)
    CACHE_TTL_ORACLE:       int = _env_int("CACHE_TTL_ORACLE", 5)
    CACHE_TTL_DERIVATIVES:  int = _env_int("CACHE_TTL_DERIVATIVES", 30)

    # ── Market-AI gate ($scan ai / $query) ─────────────────────────
    # Enabled by default when OPENROUTER_API_KEY is configured. Operators
    # can force-disable by setting MARKET_AI_ENABLED=0.
    MARKET_AI_ENABLED:    bool = _env_bool(
        "MARKET_AI_ENABLED", bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
    )

    # ── $watch alerts ──────────────────────────────────────────────
    MARKET_ALERT_INTERVAL: int = _env_int("MARKET_ALERT_INTERVAL", 60)
    MARKET_WATCH_MAX_PER_USER: int = _env_int("MARKET_WATCH_MAX_PER_USER", 20)

    # ── OpenRouter AI ─────────────────────────────────────────
    OPENROUTER_API_KEY:    str  = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL:      str  = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    # ── Sojourns AI backend (AI lives in Sojourns, not in this bot) ──
    # Recycler ships WITHOUT a built-in model brain. Every AI feature
    # (Disco / AI Chat, the clanker pattern controller, memory, tools)
    # is served by the Sojourns platform over an OpenAI-compatible HTTP
    # backend. AI is OPTIONAL: with it off, or the backend unreachable,
    # the full economy still runs and each AI-gated feature degrades
    # gracefully (the inference call returns None and callers fall back).
    #   AI_BACKEND_URL  OpenAI-compatible base URL (".../v1") of the
    #                   Sojourns AI backend. Defaults to OpenRouter so a
    #                   standalone deploy without Sojourns still works.
    #   AI_BACKEND_KEY  bearer token the backend expects; defaults to
    #                   OPENROUTER_API_KEY for the standalone path.
    #   AI_ENABLED      master switch for all AI features; on by default
    #                   whenever a backend key is configured.
    AI_BACKEND_URL:        str  = os.getenv("AI_BACKEND_URL", "https://openrouter.ai/api/v1").rstrip("/")
    AI_BACKEND_KEY:        str  = os.getenv("AI_BACKEND_KEY", "") or os.getenv("OPENROUTER_API_KEY", "")
    AI_ENABLED:            bool = _env_bool("AI_ENABLED", bool((os.getenv("AI_BACKEND_KEY", "") or os.getenv("OPENROUTER_API_KEY", "")).strip()))
    # ── AI Tools backend (when keyword tools fire) ────────────
    # TOOLS_BACKEND=ollama routes tool-augmented calls to Ollama instead of OpenRouter.
    # Leave blank or set to "openrouter" to keep everything on OpenRouter.
    TOOLS_BACKEND:         str  = os.getenv("TOOLS_BACKEND", "openrouter")
    # Empty default so the OpenRouter path falls through to OPENROUTER_MODEL
    # (which is vision-capable). Operators who want Ollama can set this to
    # "llama3.2" (or any local model) along with TOOLS_BACKEND=ollama.
    TOOLS_MODEL:           str  = os.getenv("TOOLS_MODEL", "")
    # ── AI Chat backend (casual chat, no tool match) ──────────
    # Routes the no-tool casual-chat path independently of TOOLS_BACKEND so a
    # slow Ollama model (e.g. gemma4:31b-cloud) can still serve tool loops
    # while quick "hi"/"what's up" turns go through a fast OpenRouter model.
    # Set CHAT_BACKEND=openrouter (with OPENROUTER_API_KEY set) to get 3-5s
    # casual replies. Empty/unset: casual chat follows TOOLS_BACKEND.
    CHAT_BACKEND:          str  = os.getenv("CHAT_BACKEND", "")
    # Ollama ``keep_alive`` field passed on every Ollama request so the model
    # stays resident between calls. Without it, cloud Ollama unloads after
    # ~30s of idle and the next request pays a 5-15s cold reload. Format is
    # Ollama's duration string ("10m", "30s", "-1" for forever). Empty
    # disables the field on the wire.
    OLLAMA_KEEP_ALIVE:     str  = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
    # When True, on Ollama empty-response, fall over to OpenRouter so the
    # user still gets *some* reply. Default False -- operators who chose
    # Ollama explicitly should not be silently billed OpenRouter on every
    # transient empty response. Set to "1" to re-enable the old behaviour.
    AI_CROSS_PROVIDER_RESCUE: bool = _env_bool("AI_CROSS_PROVIDER_RESCUE", False)
    # Hard cap on the outer wait_for budget for a single ,ask / mention /
    # reply turn. Default 90s -- replies that take longer than that on a
    # cloud Ollama model are almost certainly stuck in a retry storm or
    # waiting on a model that's gone catatonic. Better to surface a clear
    # "took too long" message and let the user retry than to leave the
    # placeholder frozen on "thinking..." for 3+ minutes. Bumped to 120s
    # automatically when the message has image attachments (vision adds
    # 15-30s on top of the chat call).
    AI_REPLY_TIMEOUT_S:    int  = _env_int("AI_REPLY_TIMEOUT_S", 90)
    # ── Web search backend (used by data.web_search agent tool) ──────────────
    # SEARCH_BACKEND=ddg (default/blank): DuckDuckGo HTML scraping, no key needed.
    # SEARCH_BACKEND=brave: Brave Search API (api.search.brave.com).
    #   Requires BRAVE_SEARCH_API_KEY.  Returns raw {title, url, snippet} rows
    #   from the engine -- no AI summary, so SEARCH_MODEL does not apply.
    #   Free tier: https://brave.com/search/api/.  Falls back to DDG on
    #   missing key or HTTP error.
    # SEARCH_BACKEND=openrouter: route through OpenRouter using SEARCH_MODEL.
    #   Any OpenRouter model with live-web access works, e.g.:
    #     perplexity/sonar               (fast, free tier available)
    #     perplexity/sonar-pro           (higher quality, costs more)
    #     perplexity/sonar-reasoning     (CoT + citations)
    #   Requires OPENROUTER_API_KEY. Falls back to DDG if the key is missing.
    # SEARCH_BACKEND=perplexity: direct Perplexity API (api.perplexity.ai).
    #   Requires PERPLEXITY_API_KEY. SEARCH_MODEL selects the Perplexity model
    #   (default: sonar). Falls back to DDG if the key is missing.
    # SEARCH_BACKEND=ollama: route through OLLAMA_BASE_URL using SEARCH_MODEL.
    #   Useful when running a local model that has web access (e.g. via Searxng).
    #   Falls back to DDG if OLLAMA_BASE_URL is not set.
    #
    # SEARCH_MODEL is consumed by the AI-summary backends (openrouter,
    # perplexity, ollama).  Raw-results backends (ddg, brave) ignore it.
    SEARCH_BACKEND:        str  = os.getenv("SEARCH_BACKEND", "ddg")
    SEARCH_MODEL:          str  = os.getenv("SEARCH_MODEL", "perplexity/sonar")
    PERPLEXITY_API_KEY:    str  = os.getenv("PERPLEXITY_API_KEY", "")
    BRAVE_SEARCH_API_KEY:  str  = os.getenv("BRAVE_SEARCH_API_KEY", "")
    # ── Vision backend and model ──────────────────────────────────────────────
    # VISION_BACKEND: "" (default) -> ollama. Set to "openrouter" to skip Ollama
    # and route vision directly through OpenRouter (OPENROUTER_MODEL).
    VISION_BACKEND:        str  = os.getenv("VISION_BACKEND", "")
    # VISION_MODEL: "" (default) follows TOOLS_MODEL, then "gemma3:27b" for ollama.
    VISION_MODEL:          str  = os.getenv("VISION_MODEL", "")
    # OpenRouter fallback vision models. Tried in order until one returns a
    # description -- the old single-model fallback meant one bad slug
    # (gemini-flash-1.5 went 404 "no endpoints" when OpenRouter renamed it)
    # killed the whole chain. Comma-separated env list overrides the default.
    # Defaults are current as of 2025: gemini-2.5-flash (cheap, fast, very
    # reliable), gpt-4o-mini (broad provider coverage), claude-3-haiku
    # (different upstream so a single-provider outage doesn't take vision
    # down). All three accept data URIs and HTTP image URLs.
    OPENROUTER_VISION_MODELS: str = os.getenv(
        "OPENROUTER_VISION_MODELS",
        "google/gemini-2.5-flash,openai/gpt-4o-mini,anthropic/claude-3-haiku",
    )
    # Legacy single-model knob kept for backwards compatibility -- still
    # honoured if set, prepended to the fallback list above.
    OPENROUTER_VISION_MODEL: str = os.getenv("OPENROUTER_VISION_MODEL", "")
    # ── Image generation ──────────────────────────────────────────────────────
    # The image.generate agent tool is always REGISTERED (so the chat model
    # always sees it and can advertise it to users) but only RUNS when
    # IMAGE_GEN_ENABLED is truthy. Default derives from OPENROUTER_API_KEY
    # presence so operators who've already set up OpenRouter get image gen
    # out of the box without a separate flag. Explicit IMAGE_GEN_ENABLED
    # env var still wins in both directions.
    # IMAGE_GEN_MODEL: any OpenRouter chat-completions model that emits an
    #   image URL in the response. Good options:
    #     black-forest-labs/flux-schnell (fast, cheap -- default)
    #     black-forest-labs/flux-1.1-pro (higher quality)
    #     stabilityai/stable-diffusion-3-5-large
    IMAGE_GEN_ENABLED:     bool = _env_bool(
        "IMAGE_GEN_ENABLED", bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
    )
    IMAGE_GEN_MODEL:       str  = os.getenv("IMAGE_GEN_MODEL", "black-forest-labs/flux-schnell")
    # Feature flags  -  set to "0" in .env to disable
    AI_MM_ENABLED:         bool = _env_bool("AI_MM_ENABLED", True)
    AI_CHAT_ENABLED:       bool = _env_bool("AI_CHAT_ENABLED", True)
    AI_COMMENTARY_ENABLED: bool = _env_bool("AI_COMMENTARY_ENABLED", True)
    AI_FLAVOR_ENABLED:     bool = _env_bool("AI_FLAVOR_ENABLED", False)
    AI_EVENTS_ENABLED:     bool = _env_bool("AI_EVENTS_ENABLED", True)

    # ── AI chat queue ─────────────────────────────────────────────────────
    # Per-backend slot caps for the chat queue in core/framework/ai/queue.py.
    # This deployment runs Ollama as the primary backend (everything goes
    # through it; OpenRouter is the cheap fallback / occasional vision
    # path), so Ollama gets the larger cap. If you flip primary, swap the
    # numbers via env. SYSTEM_RESERVED carves out a sub-pool that
    # background commentary / flavor traffic can't deplete, so user-
    # facing chat never starves behind cron jobs.
    AI_QUEUE_OLLAMA_CAP:     int = _env_int("AI_QUEUE_OLLAMA_CAP", 24)
    AI_QUEUE_OPENROUTER_CAP: int = _env_int("AI_QUEUE_OPENROUTER_CAP", 8)
    AI_QUEUE_SYSTEM_RESERVED: int = _env_int("AI_QUEUE_SYSTEM_RESERVED", 4)

    # ── AI passive learning ──────────────────────────────────────────────
    # When enabled, every chat turn fires a lightweight LLM call that
    # extracts candidate trait signals from the (user_msg, assistant_reply)
    # pair and upserts them into ai_user_traits with confidence=0.3 and
    # source='passive_chat'. The existing decay/promotion logic in
    # ai_traits.py handles cleanup of one-shot noise, and signals that
    # show up repeatedly climb naturally toward the 'stable' layer.
    #
    # MIN_INTERVAL_S + EVERY_N_TURNS gate the extraction call per user so
    # we don't spam an LLM call on every single message. The Ollama-side
    # cap (AI_QUEUE_OLLAMA_CAP) also gates: if Ollama is already queuing
    # users, extraction is skipped to avoid stealing capacity from the
    # chat path. Per-user opt-out via ``,disco optout`` still applies;
    # opted-out users never get passive traits written.
    AI_AUTO_LEARN_ENABLED:        bool = _env_bool("AI_AUTO_LEARN_ENABLED", True)
    AI_AUTO_LEARN_MIN_INTERVAL_S: int  = _env_int("AI_AUTO_LEARN_MIN_INTERVAL_S", 600)
    AI_AUTO_LEARN_EVERY_N_TURNS:  int  = _env_int("AI_AUTO_LEARN_EVERY_N_TURNS", 3)
    # Blank = use the deployment's configured Ollama default; set to an
    # OpenRouter slug to force a specific extraction model.
    AI_AUTO_LEARN_MODEL:          str  = os.getenv("AI_AUTO_LEARN_MODEL", "")

    # ── AI regenerate / try-harder ──────────────────────────────────────
    # TTL for the in-memory _AskState entry that backs the Regenerate
    # button. After this many seconds the button auto-disables and the
    # state entry is pruned.
    AI_REGEN_TTL_S:               int   = _env_int("AI_REGEN_TTL_S", 900)
    # How much to bump temperature when "Try harder" is clicked. Capped
    # at 1.5 in code so a misconfigured value can't push the model into
    # incoherent territory.
    AI_REGEN_TRY_HARDER_TEMP_BUMP: float = _env_float("AI_REGEN_TRY_HARDER_TEMP_BUMP", 0.35)

    # ── DiscoAI: self-hosted LLM ───────────────────────────────────────────
    # ── DiscoAI memory sidecar ───────────────────────────────────────────
    # No local inference anymore -- generation stays on OpenRouter via
    # core.framework.ai.  These knobs govern the disco_facts / disco_episodes /
    # disco_training_turns plumbing only.  See ai/config.py for the typed
    # settings object loaded from these vars.
    DISCOAI_SHORT_TERM_TURNS:       int   = _env_int("DISCOAI_SHORT_TERM_TURNS", 12)
    DISCOAI_SHORT_TERM_TTL_S:       int   = _env_int("DISCOAI_SHORT_TERM_TTL_S", 3600)
    DISCOAI_PASSIVE_LEARNING:       bool  = _env_bool("DISCOAI_PASSIVE_LEARNING", False)
    DISCOAI_RATE_LIMIT_PER_USER_PER_MIN: int = _env_int("DISCOAI_RATE_LIMIT_PER_USER_PER_MIN", 8)
    # Internal API base (the FastAPI server the bot already runs). The tool
    # registry's handlers call this instead of touching the DB directly.
    DISCOAI_API_BASE_URL:           str   = os.getenv(
        "DISCOAI_API_BASE_URL", f"http://127.0.0.1:{_env_first_int('PORT', 'API_PORT', default=8080)}"
    )

    # ── GIPHY integration ─────────────────────────────────────────────────
    # Public API key from https://developers.giphy.com/dashboard/
    # Leave blank to disable GIF replies entirely.
    GIPHY_API_KEY:        str   = os.getenv("GIPHY_API_KEY", "")
    # Probability (0.0-1.0) that Disco appends a GIF after an AI chat reply.
    # 0.15 = ~1 in 7 replies gets a GIF. Set 0 to disable automatic GIFs.
    GIPHY_GIF_PROBABILITY: float = _env_float("GIPHY_GIF_PROBABILITY", 0.15)
    # GIPHY content rating: g | pg | pg-13 | r  (default g for all audiences)
    GIPHY_GIF_RATING:     str   = os.getenv("GIPHY_GIF_RATING", "g")
