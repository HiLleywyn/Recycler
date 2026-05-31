"""configs/market_events_config.py  -  Multi-phase market event definitions.

Each event is a sequence of phases that evolve over time, affecting
volatility, price bias, fees, liquidity, mining, staking, and lending.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from constants.ui import C_BULL, C_BEAR, C_VOLATILE, C_CATASTROPHE, C_GRAY  # noqa: F401


# ── Phase & Event dataclasses ────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EventPhase:
    name: str
    duration_minutes: int
    vol_multiplier: float
    price_bias_pct_per_day: float
    fee_multiplier: float = 1.0
    mining_difficulty_mult: float = 1.0
    staking_apy_mult: float = 1.0
    lending_rate_mult: float = 1.0
    liquidity_drain_pct: float = 0.0
    slippage_mult: float = 1.0
    embed_color: int = C_GRAY
    flavor_text: str = ""


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    display_name: str
    emoji: str
    description: str
    rarity_weight: int
    cooldown_minutes: int
    phases: tuple[EventPhase, ...]
    on_start_effects: tuple[str, ...] = ()
    on_end_effects: tuple[str, ...] = ()
    cancels: tuple[str, ...] = ()
    stackable: bool = False

    @property
    def total_duration_seconds(self) -> int:
        return sum(p.duration_minutes * 60 for p in self.phases)


@dataclass
class ActiveEvent:
    """Tracks a running event for a guild  -  serialisable to/from Redis."""
    guild_id: int
    event_id: str
    phase_index: int
    phase_started_at: float       # epoch seconds
    event_started_at: float       # epoch seconds
    start_prices: dict[str, float] = field(default_factory=dict)

    @property
    def phase_elapsed(self) -> float:
        return time.time() - self.phase_started_at

    def to_dict(self) -> dict:
        return {
            "guild_id": self.guild_id,
            "event_id": self.event_id,
            "phase_index": self.phase_index,
            "phase_started_at": self.phase_started_at,
            "event_started_at": self.event_started_at,
            "start_prices": self.start_prices,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ActiveEvent:
        return cls(
            guild_id=int(d["guild_id"]),
            event_id=d["event_id"],
            phase_index=int(d["phase_index"]),
            phase_started_at=float(d["phase_started_at"]),
            event_started_at=float(d["event_started_at"]),
            start_prices=d.get("start_prices", {}),
        )


# ── Embed color constants ────────────────────────────────────────────────────

# Market event colors are defined in constants.ui and imported above.


# ── Event Registry ───────────────────────────────────────────────────────────

EVENT_REGISTRY: dict[str, MarketEvent] = {}


def _r(ev: MarketEvent) -> MarketEvent:
    EVENT_REGISTRY[ev.event_id] = ev
    return ev


# ── BULL RUN ─────────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="bull_run",
    display_name="Bull Run",
    emoji="\U0001f402",  # ox
    description="Smart money is accumulating. A massive rally is forming across the board.",
    rarity_weight=30,
    cooldown_minutes=90,
    cancels=("bear_market",),
    on_start_effects=("boost_staking_apy",),
    phases=(
        EventPhase(
            name="accumulation", duration_minutes=10,
            vol_multiplier=0.6, price_bias_pct_per_day=0.3,
            staking_apy_mult=1.3,
            embed_color=C_BULL, flavor_text="Smart money is moving...",
        ),
        EventPhase(
            name="breakout", duration_minutes=15,
            vol_multiplier=1.2, price_bias_pct_per_day=1.5,
            fee_multiplier=0.8, staking_apy_mult=1.3,
            embed_color=C_BULL, flavor_text="Markets are surging! \U0001f4c8",
        ),
        EventPhase(
            name="euphoria", duration_minutes=10,
            vol_multiplier=1.8, price_bias_pct_per_day=2.5,
            slippage_mult=1.5, staking_apy_mult=1.3,
            embed_color=C_BULL, flavor_text="EVERYTHING IS GOING UP!",
        ),
        EventPhase(
            name="cooldown", duration_minutes=10,
            vol_multiplier=1.0, price_bias_pct_per_day=0.2,
            embed_color=C_GRAY, flavor_text="The rally is losing steam...",
        ),
    ),
))

# ── BEAR MARKET ──────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="bear_market",
    display_name="Bear Market",
    emoji="\U0001f43b",  # bear
    description="Fear is spreading. Capitulation is near. Diamond hands only.",
    rarity_weight=30,
    cooldown_minutes=90,
    cancels=("bull_run",),
    phases=(
        EventPhase(
            name="denial", duration_minutes=10,
            vol_multiplier=0.8, price_bias_pct_per_day=-0.2,
            embed_color=C_BEAR, flavor_text="It's just a dip... right?",
        ),
        EventPhase(
            name="capitulation", duration_minutes=15,
            vol_multiplier=2.0, price_bias_pct_per_day=-1.5,
            liquidity_drain_pct=10.0,
            embed_color=C_BEAR, flavor_text="Liquidity providers are pulling out!",
        ),
        EventPhase(
            name="max_pain", duration_minutes=10,
            vol_multiplier=2.5, price_bias_pct_per_day=-2.0,
            slippage_mult=2.0, lending_rate_mult=1.5,
            embed_color=C_CATASTROPHE, flavor_text="Blood in the streets.",
        ),
        EventPhase(
            name="recovery_signal", duration_minutes=10,
            vol_multiplier=1.2, price_bias_pct_per_day=0.3,
            embed_color=C_BULL, flavor_text="Bottom fishers entering...",
        ),
    ),
))

# ── FED RATE HIKE ────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="fed_rate_hike",
    display_name="Fed Rate Hike",
    emoji="\U0001f3db\ufe0f",  # classical building
    description="The Central Bank raised interest rates. Risk assets dumping.",
    rarity_weight=25,
    cooldown_minutes=120,
    on_start_effects=("increase_loan_rates",),
    phases=(
        EventPhase(
            name="announcement", duration_minutes=3,
            vol_multiplier=3.0, price_bias_pct_per_day=-2.0,
            embed_color=C_BEAR,
            flavor_text="\U0001f3e6 BREAKING: Central Bank raises rates!",
        ),
        EventPhase(
            name="panic", duration_minutes=7,
            vol_multiplier=2.5, price_bias_pct_per_day=-1.5,
            lending_rate_mult=2.0, staking_apy_mult=0.7,
            embed_color=C_BEAR,
            flavor_text="Borrowing costs surge across the network.",
        ),
        EventPhase(
            name="repricing", duration_minutes=10,
            vol_multiplier=1.5, price_bias_pct_per_day=-0.5,
            lending_rate_mult=1.5,
            embed_color=C_VOLATILE,
            flavor_text="Markets adjusting to the new reality.",
        ),
    ),
))

# ── FED RATE CUT ─────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="fed_rate_cut",
    display_name="Fed Rate Cut",
    emoji="\U0001f3db\ufe0f",
    description="The Central Bank cut rates. Cheap money flooding in.",
    rarity_weight=25,
    cooldown_minutes=120,
    phases=(
        EventPhase(
            name="announcement", duration_minutes=3,
            vol_multiplier=2.0, price_bias_pct_per_day=1.0,
            embed_color=C_BULL,
            flavor_text="\U0001f3e6 BREAKING: Central Bank cuts rates!",
        ),
        EventPhase(
            name="rally", duration_minutes=10,
            vol_multiplier=1.5, price_bias_pct_per_day=1.5,
            lending_rate_mult=0.5, staking_apy_mult=1.3,
            embed_color=C_BULL,
            flavor_text="Cheap money is flooding in.",
        ),
        EventPhase(
            name="new_normal", duration_minutes=10,
            vol_multiplier=0.5, price_bias_pct_per_day=0.3,
            lending_rate_mult=0.7,
            embed_color=C_BULL,
            flavor_text="A new era of easy money.",
        ),
    ),
))

# ── BLACK SWAN ───────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="black_swan",
    display_name="Black Swan Event",
    emoji="\U0001f9a2",  # swan
    description="A catastrophic, unpredictable event just hit the market. Total chaos.",
    rarity_weight=5,
    cooldown_minutes=240,
    cancels=("bull_run", "whale_pump", "adoption"),
    on_start_effects=("force_liquidate_undercollateralised", "pause_lending"),
    phases=(
        EventPhase(
            name="impact", duration_minutes=2,
            vol_multiplier=6.0, price_bias_pct_per_day=-5.0,
            slippage_mult=3.0, liquidity_drain_pct=25.0,
            embed_color=C_CATASTROPHE,
            flavor_text="\u26a0\ufe0f CATASTROPHIC EVENT DETECTED \u26a0\ufe0f",
        ),
        EventPhase(
            name="freefall", duration_minutes=5,
            vol_multiplier=5.0, price_bias_pct_per_day=-4.0,
            fee_multiplier=2.0, slippage_mult=2.5,
            embed_color=C_CATASTROPHE,
            flavor_text="Markets in freefall. Trading halted on some pairs.",
        ),
        EventPhase(
            name="dead_cat_bounce", duration_minutes=3,
            vol_multiplier=4.0, price_bias_pct_per_day=3.0,
            embed_color=C_VOLATILE,
            flavor_text="A brief recovery... or a trap?",
        ),
        EventPhase(
            name="aftershock", duration_minutes=5,
            vol_multiplier=3.0, price_bias_pct_per_day=-2.0,
            embed_color=C_BEAR,
            flavor_text="The aftershocks continue.",
        ),
        EventPhase(
            name="stabilization", duration_minutes=5,
            vol_multiplier=1.5, price_bias_pct_per_day=-0.3,
            embed_color=C_GRAY,
            flavor_text="Volatility slowly subsiding...",
        ),
    ),
))

# ── WHALE PUMP ───────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="whale_pump",
    display_name="Whale Pump",
    emoji="\U0001f40b",  # whale
    description="A massive whale just entered the market. Brace for impact.",
    rarity_weight=15,
    cooldown_minutes=60,
    phases=(
        EventPhase(
            name="accumulation", duration_minutes=2,
            vol_multiplier=1.0, price_bias_pct_per_day=0.5,
            embed_color=C_BULL,
            flavor_text="\U0001f40b Unusual buy volume detected...",
        ),
        EventPhase(
            name="pump", duration_minutes=3,
            vol_multiplier=3.0, price_bias_pct_per_day=5.0,
            slippage_mult=2.0,
            embed_color=C_BULL,
            flavor_text="\U0001f40b\U0001f40b\U0001f40b WHALE ALERT! Massive buys incoming!",
        ),
        EventPhase(
            name="dump_risk", duration_minutes=3,
            vol_multiplier=3.5, price_bias_pct_per_day=-1.0,
            embed_color=C_VOLATILE,
            flavor_text="The whale has stopped buying. Will they dump?",
        ),
        EventPhase(
            name="aftermath", duration_minutes=2,
            vol_multiplier=1.5, price_bias_pct_per_day=-0.5,
            embed_color=C_GRAY,
            flavor_text="Dust settling...",
        ),
    ),
))

# ── RUG PULL ─────────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="rug_pull",
    display_name="Major Rug Pull",
    emoji="\U0001faa4",  # mouse trap
    description="Something doesn't smell right. A project just announced a 'partnership'...",
    rarity_weight=8,
    cooldown_minutes=180,
    phases=(
        EventPhase(
            name="the_setup", duration_minutes=3,
            vol_multiplier=0.5, price_bias_pct_per_day=1.0,
            embed_color=C_BULL,
            flavor_text="\U0001f911 A new partnership announcement! Things are looking great!",
        ),
        EventPhase(
            name="the_pull", duration_minutes=2,
            vol_multiplier=5.0, price_bias_pct_per_day=-8.0,
            liquidity_drain_pct=40.0, slippage_mult=3.0,
            embed_color=C_CATASTROPHE,
            flavor_text="\u26a0\ufe0f LIQUIDITY PULLED! IT'S A RUG!",
        ),
        EventPhase(
            name="the_cope", duration_minutes=5,
            vol_multiplier=3.0, price_bias_pct_per_day=-2.0,
            embed_color=C_BEAR,
            flavor_text="Devs are... not responding.",
        ),
        EventPhase(
            name="dust", duration_minutes=5,
            vol_multiplier=1.5, price_bias_pct_per_day=-0.3,
            embed_color=C_GRAY,
            flavor_text="Never trust, always verify.",
        ),
    ),
))

# ── PANDEMIC ─────────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="pandemic",
    display_name="Global Pandemic",
    emoji="\U0001f9a0",  # microbe
    description="A new global health crisis. Supply chains disrupted. Fear at all-time highs.",
    rarity_weight=8,
    cooldown_minutes=360,
    phases=(
        EventPhase(
            name="rumors", duration_minutes=10,
            vol_multiplier=1.2, price_bias_pct_per_day=-0.3,
            embed_color=C_VOLATILE,
            flavor_text="\U0001f9a0 Reports of a new virus variant...",
        ),
        EventPhase(
            name="confirmation", duration_minutes=10,
            vol_multiplier=2.0, price_bias_pct_per_day=-1.5,
            mining_difficulty_mult=1.3,
            embed_color=C_BEAR,
            flavor_text="WHO declares pandemic. Supply chains disrupted.",
        ),
        EventPhase(
            name="lockdown", duration_minutes=15,
            vol_multiplier=3.0, price_bias_pct_per_day=-2.5,
            mining_difficulty_mult=1.5, fee_multiplier=1.5,
            embed_color=C_CATASTROPHE,
            flavor_text="Total lockdown. Mining rigs going offline.",
        ),
        EventPhase(
            name="stimulus", duration_minutes=10,
            vol_multiplier=2.0, price_bias_pct_per_day=1.0,
            staking_apy_mult=1.5,
            embed_color=C_BULL,
            flavor_text="\U0001f4b0 Emergency stimulus! Money printer goes brrr.",
        ),
        EventPhase(
            name="recovery", duration_minutes=10,
            vol_multiplier=1.0, price_bias_pct_per_day=0.5,
            embed_color=C_BULL,
            flavor_text="Light at the end of the tunnel.",
        ),
    ),
))

# ── REGULATION ───────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="regulation",
    display_name="New Regulation",
    emoji="\u2696\ufe0f",  # scales
    description="Governments cracking down on crypto. Compliance FUD everywhere.",
    rarity_weight=20,
    cooldown_minutes=90,
    on_start_effects=("increase_fees",),
    phases=(
        EventPhase(
            name="leak", duration_minutes=5,
            vol_multiplier=1.5, price_bias_pct_per_day=-0.5,
            embed_color=C_VOLATILE,
            flavor_text="\U0001f4cb Leaked draft shows new crypto regulations...",
        ),
        EventPhase(
            name="announcement", duration_minutes=5,
            vol_multiplier=2.0, price_bias_pct_per_day=-1.0,
            fee_multiplier=1.5,
            embed_color=C_BEAR,
            flavor_text="Regulations officially announced. KYC requirements increased.",
        ),
        EventPhase(
            name="compliance", duration_minutes=10,
            vol_multiplier=1.2, price_bias_pct_per_day=-0.3,
            fee_multiplier=1.3,
            embed_color=C_GRAY,
            flavor_text="Exchanges scrambling to comply.",
        ),
    ),
))

# ── MASS ADOPTION ────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="adoption",
    display_name="Mass Adoption",
    emoji="\U0001f680",  # rocket
    description="Mainstream is coming. A major tech company just announced crypto integration.",
    rarity_weight=12,
    cooldown_minutes=120,
    on_start_effects=("reduce_mining_difficulty",),
    phases=(
        EventPhase(
            name="catalyst", duration_minutes=5,
            vol_multiplier=0.8, price_bias_pct_per_day=0.5,
            embed_color=C_BULL,
            flavor_text="\U0001f4f1 Major tech company announces crypto integration!",
        ),
        EventPhase(
            name="fomo", duration_minutes=10,
            vol_multiplier=1.5, price_bias_pct_per_day=2.0,
            slippage_mult=1.5,
            embed_color=C_BULL,
            flavor_text="New users flooding in! Transaction volume through the roof!",
        ),
        EventPhase(
            name="mainstream", duration_minutes=15,
            vol_multiplier=0.5, price_bias_pct_per_day=1.0,
            staking_apy_mult=1.2, mining_difficulty_mult=0.8,
            embed_color=C_BULL,
            flavor_text="Crypto goes mainstream. Your grandma is asking about DeFi.",
        ),
    ),
))

# ── ETF APPROVED ─────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="etf_approved",
    display_name="ETF Approved",
    emoji="\U0001f4ca",  # chart
    description="A major crypto ETF just got approved. Institutional money is coming.",
    rarity_weight=15,
    cooldown_minutes=120,
    phases=(
        EventPhase(
            name="filing", duration_minutes=5,
            vol_multiplier=1.2, price_bias_pct_per_day=0.5,
            embed_color=C_BULL,
            flavor_text="\U0001f4c4 ETF filing spotted on SEC website...",
        ),
        EventPhase(
            name="approved", duration_minutes=3,
            vol_multiplier=2.5, price_bias_pct_per_day=3.0,
            embed_color=C_BULL,
            flavor_text="\u2705 ETF APPROVED! Institutional money incoming!",
        ),
        EventPhase(
            name="inflows", duration_minutes=12,
            vol_multiplier=1.0, price_bias_pct_per_day=1.5,
            liquidity_drain_pct=-15.0,
            embed_color=C_BULL,
            flavor_text="Billions flowing in. Liquidity deepening.",
        ),
        EventPhase(
            name="sell_the_news", duration_minutes=5,
            vol_multiplier=1.5, price_bias_pct_per_day=-0.5,
            embed_color=C_VOLATILE,
            flavor_text="Buy the rumor, sell the news...",
        ),
    ),
))

# ── MOON ─────────────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="moon",
    display_name="Going to the Moon",
    emoji="\U0001f315",  # full moon
    description="Something has changed. The market is doing things that should not be possible. Everything is going up and no one is selling.",
    rarity_weight=2,
    cooldown_minutes=720,
    cancels=("bear_market", "black_swan", "rug_pull", "pandemic", "exchange_hack", "regulation"),
    on_start_effects=("boost_staking_apy",),
    phases=(
        EventPhase(
            name="ignition", duration_minutes=15,
            vol_multiplier=0.8, price_bias_pct_per_day=8.0,
            fee_multiplier=0.5, staking_apy_mult=1.5,
            embed_color=C_BULL,
            flavor_text="\U0001f315 Something is happening. Charts are going vertical.",
        ),
        EventPhase(
            name="liftoff", duration_minutes=20,
            vol_multiplier=2.0, price_bias_pct_per_day=12.0,
            fee_multiplier=0.5, staking_apy_mult=1.8, slippage_mult=2.0,
            embed_color=C_BULL,
            flavor_text="\U0001f680\U0001f315 WE ARE LEAVING EARTH. SELL ORDERS DELETED.",
        ),
        EventPhase(
            name="moon", duration_minutes=30,
            vol_multiplier=3.5, price_bias_pct_per_day=10.0,
            fee_multiplier=0.3, staking_apy_mult=2.0, slippage_mult=3.0,
            embed_color=C_BULL,
            flavor_text="\U0001f315\U0001f315\U0001f315 MOON CONFIRMED. UP ONLY. NO LAWS.",
        ),
        EventPhase(
            name="atmosphere", duration_minutes=15,
            vol_multiplier=2.5, price_bias_pct_per_day=6.0,
            staking_apy_mult=1.8, slippage_mult=2.0,
            embed_color=C_BULL,
            flavor_text="We are in orbit. Prices settling at new all-time highs.",
        ),
        EventPhase(
            name="orbit", duration_minutes=20,
            vol_multiplier=1.2, price_bias_pct_per_day=3.0,
            staking_apy_mult=1.3,
            embed_color=C_BULL,
            flavor_text="New paradigm established. Analysts are speechless.",
        ),
    ),
))


# ── EXCHANGE HACK ────────────────────────────────────────────────────────────

_r(MarketEvent(
    event_id="exchange_hack",
    display_name="Exchange Hack",
    emoji="\U0001f480",  # skull
    description="A major exchange has been compromised. User funds at risk.",
    rarity_weight=6,
    cooldown_minutes=240,
    phases=(
        EventPhase(
            name="breach", duration_minutes=2,
            vol_multiplier=4.0, price_bias_pct_per_day=-3.0,
            embed_color=C_CATASTROPHE,
            flavor_text="\U0001f534 SECURITY BREACH DETECTED on major exchange!",
        ),
        EventPhase(
            name="panic", duration_minutes=5,
            vol_multiplier=4.5, price_bias_pct_per_day=-4.0,
            fee_multiplier=2.0, liquidity_drain_pct=20.0,
            embed_color=C_CATASTROPHE,
            flavor_text="Users rushing to withdraw. Exchange halts trading.",
        ),
        EventPhase(
            name="assessment", duration_minutes=8,
            vol_multiplier=2.5, price_bias_pct_per_day=-1.0,
            embed_color=C_BEAR,
            flavor_text="Damage being assessed... millions reportedly stolen.",
        ),
        EventPhase(
            name="insurance", duration_minutes=5,
            vol_multiplier=1.5, price_bias_pct_per_day=0.5,
            embed_color=C_BULL,
            flavor_text="Exchange announces insurance fund will cover losses.",
        ),
    ),
))
