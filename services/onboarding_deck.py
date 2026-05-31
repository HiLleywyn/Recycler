"""Discoin onboarding deck for ``,tour``.

Each card is one PNG plus an embed body. The deck walks a new player
through every major surface of the bot:

    1) Wallet -- balance / bank / move / pay
    2) Earn   -- daily / work / faucet
    3) Trade  -- buy / sell / swap / portfolio
    4) Mastery -- the cross-system progression hook (9 tracks, 20 nodes)
    5) World Events -- the rolling guild-wide buff/debuff layer
    6) Buddy  -- companions + arena
    7) PvP    -- exploit raids + defence
    8) Items  -- stones, consumables, shop
    9) Help   -- where to look next

Progress persists in ``user_onboarding`` so a player who steps away
can rerun ``,tour`` later and pick up where they left off.

NOTE: ``DECK[i]["title"]`` is the short headline drawn at the top of
the PNG and surfaced as the embed bold line. ``DECK[i]["blurb"]`` is a
one-sentence framing line the embed shows just under the title.
``DECK[i]["lines"]`` is the full body rendered inside the PNG card.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from constants.ui import (
    C_AMBER,
    C_BUDDY,
    C_CHART_BG,
    C_ERROR,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_PURPLE,
    C_SUCCESS,
    C_TEAL,
    C_VOLATILE,
)
from core.framework.render import RenderCanvas

log = logging.getLogger(__name__)


DECK: list[dict] = [
    {
        "id": "wallet",
        "title": "Wallet, Bank, Move, Pay",
        "blurb": (
            "Every player has a liquid wallet and a savings bank. Knowing "
            "how to shuffle between them is the very first skill."
        ),
        "color": C_GOLD,
        "lines": [
            "`,balance` -- see wallet (liquid) and bank (saving) side by side.",
            "`,move all bank` -- shift everything to savings in one shot.",
            "`,move 500 wallet` -- pull cash out for spending or trading.",
            "Bank pays compound interest on a fixed tick. Wallet doesn't.",
            "`,pay @user 100` -- direct P2P transfer between players.",
            "`,profile` -- your card: title, banner, frame, sigil, totals.",
        ],
    },
    {
        "id": "earn",
        "title": "Earn -- daily, work, faucet",
        "blurb": (
            "Three baseline earners with different cooldowns. Stack them "
            "with mastery passives once you've got a few points."
        ),
        "color": C_SUCCESS,
        "lines": [
            "`,daily` -- once every 24h. Baseline payout, scales with streaks.",
            "`,work` -- shorter cooldown. Variable payout, scales with mastery.",
            "`,faucet` -- continuous tap, low rate, no cooldown. Set & forget.",
            "`,fish`, `,farm harvest`, `,delve`, `,craft` -- minigame earners.",
            "Every credit pays a small Chain-Wide-Earning tax that funds UBI.",
            "Watch `,mastery` -- minigame earners feed your mastery tracks.",
            "Chain harvests within 10s for a 6-step farm combo bonus.",
        ],
    },
    {
        "id": "trade",
        "title": "Trade -- buy, sell, swap, chart",
        "blurb": (
            "The Discoin market is a real candle chart fed by player flow. "
            "Every buy and sell nudges the price."
        ),
        "color": C_INFO,
        "lines": [
            "`,prices` -- the full token board with last price + 24h change.",
            "`,chart MTA` -- live candlesticks. Add `1d` / `1w` for windows.",
            "`,buy MTA 100` / `,sell MTA 0.5` -- USD market orders.",
            "`,swap ARC MTA 1` -- AMM swap on the liquidity pool.",
            "`,portfolio` -- your positions, P/L, and exposure breakdown.",
            "Listed coins: MTA, ARC, SOL, DSC, DSD (stable), and more.",
        ],
    },
    {
        "id": "mastery",
        "title": "Mastery -- nine tracks, twenty nodes",
        "blurb": (
            "Mastery is Discoin's cross-system progression layer. Every "
            "minigame feeds an XP track; levels grant points to spend."
        ),
        "color": C_AMBER,
        "lines": [
            "Ten tracks: Fisher, Farmer, Delver, Trader, Gambler, Raider,",
            "Tamer, Validator, Crafter, Scholar. Each minigame XPs its own track.",
            "Hit level milestones to bank mastery points (+1 per level,",
            "with a bonus every 10 levels).",
            "Spend points on the 20-node tree across 4 branches:",
            "Economy / Combat / Luck / Utility -- passives apply bot-wide.",
            "`,mastery` -- full board PNG.  `,mastery tracks` -- XP sources.",
            "`,mastery info <id>` -- inspect a single node before spending.",
        ],
    },
    {
        "id": "events",
        "title": "World Events -- rolling buffs & debuffs",
        "blurb": (
            "Codenamed Apex Events internally. Server-wide modifiers that "
            "spawn on a roll, last for a window, then expire."
        ),
        "color": C_VOLATILE,
        "lines": [
            "Roll every 30s. Most rolls miss -- live events are uncommon.",
            "Examples: Solar Flare (+50% hashrate, -20% fish catch),",
            "Blood Moon (gamba payouts +15%, savings APR -10%),",
            "Harvest Bloom (crops 2x, LP fees halved), and more.",
            "`,apex` -- show what's live right now in this server.",
            "`,apex catalog` -- browse every event in the rotation.",
            "`,apex info <id>` -- read flavour + every modifier on one card.",
            "`,apex history` -- the last 10 events that ran here.",
        ],
    },
    {
        "id": "buddy",
        "title": "Buddy -- companions, battles + arena",
        "blurb": (
            "Pick a creature, feed it, and field it in PvP duels or the PvE "
            "arena. The Tamer mastery track buffs every buddy you own."
        ),
        "color": C_BUDDY,
        "lines": [
            "`,buddy adopt` / `,buddy hatch` -- pick a starter / hatch a new egg.",
            "`,buddy feed` -- keep your buddy happy and earning daily yield.",
            "`,buddy battle` -- help panel.  `,buddy battle fight @rival` for PvP duels.",
            "`,buddy arena` -- help panel.  `,buddy arena fight` to enter the PvE arena.",
            "`,buddy arena boss` -- once-a-day boss for fat BUD + BBT.",
            "Tamer track XP triggers on every feed / battle / hatch.",
        ],
    },
    {
        "id": "pvp",
        "title": "PvP -- raids, buddy duels, delve arena",
        "blurb": (
            "Three flavours of PvP. Wallet raids (Raider lifestyle), buddy "
            "duels (battle your active buddy), and the delve arena "
            "(ranked + live duels with your dungeon kit)."
        ),
        "color": C_ERROR,
        "lines": [
            "`,eat @user` -- eat a player richer than you.",
            "`,buddy battle fight @rival [amt]` -- buddy PvP, optional stake.",
            "`,delve arena fight` -- ranked async PvP, Copper -> Rune ladder.",
            "`,delve arena duel @user` -- live turn-based delve duel.",
            "Rewards: copper / silver / gold / RUNE based on rank band.",
            "World event Raider Dawn doubles raid damage AND defence rewards.",
        ],
    },
    {
        "id": "sage",
        "title": "Sage -- crypto learn-and-earn (pattern / gauge / tknom)",
        "blurb": (
            "Three timed quiz games that mint EDU (game token) + SAGE "
            "(network coin) on every correct answer. One wrong answer "
            "ends the run; every round shows the educational explanation."
        ),
        "color": C_GOLD,
        "lines": [
            "`,pattern` -- identify a chart pattern (17 patterns, 15s).",
            "`,gauge` -- bear/neutral/bull on an indicator card (30s).",
            "`,tknom` -- inflate/deflate/stable/rug on a token card (15s).",
            "Reward split per correct: 10% SAGE + 90% EDU, scales per round.",
            "`,sage stake <amt>` -- lock EDU to drip SAGE.",
            "`,sage cashout <amt>` -- burn SAGE for USD (oracle minus impact).",
            "`,sage lb` -- per-game leaderboards. `,sage me` -- your bests.",
            "Disco refuses to give the answer mid-run -- use your eyes.",
        ],
    },
    {
        "id": "realmarket",
        "title": "Real Markets -- the $-prefix",
        "blurb": (
            "A separate namespace for LIVE markets (crypto, stocks, ETFs, "
            "forex, commodities, perps, oracles). Fully isolated from the "
            "game's simulated tokens -- no game data appears here, ever."
        ),
        "color": C_GOLD,
        "lines": [
            "`$help` -- tour-style help for the entire $ ecosystem.",
            "`$chart MTA 1d` / `$chart MSFT 1w` -- live candle charts.",
            "`$info SYMBOL` -- snapshot: price + oracle + funding + news.",
            "`$scan ARC 4h ai` -- pattern + indicator scan + AI commentary.",
            "`$market fear|heatmap|gainers|losers|trending|top|dom|global`.",
            "`$compare MTA SPY` / `$oracle SOL` / `$funding MTA` / `$oi MTA`.",
            "`$watch add MTA 75000 above` -- one-shot price alert.",
            "`$query <question>` -- professional AI Q&A with Sources button.",
        ],
    },
    {
        "id": "items",
        "title": "Items -- shop, stones, consumables",
        "blurb": (
            "Stones are your big permanent investments. Consumables are "
            "single-use boosts. The shop accepts stablecoins only (DSD/USDC)."
        ),
        "color": C_PURPLE,
        "lines": [
            "`,shop` -- the storefront. All prices are in stablecoins.",
            "Stones: Hash (mining), Lock (defence), Vault (savings),",
            "Liq (LP yield), Gamba (payout boost). Each levels via XP.",
            "Consumables: Charms, Gambling Saves, Validator Guards, etc.",
            "`,inv` -- your inventory. `,shop sell <item>` -- offload.",
            "`,craft` -- build higher-tier consumables from base materials.",
        ],
    },
    {
        "id": "help",
        "title": "Where to look next",
        "blurb": (
            "You've covered the major loops. Bookmark these commands -- "
            "they're your map for everything else."
        ),
        "color": C_TEAL,
        "lines": [
            "`,help` -- the full command index, grouped by system.",
            "`,help <command>` -- argument signature + flags for one command.",
            "`,help realmarket` -- the $-prefix real-market namespace.",
            "`$help` -- tour-style help for live cross-asset markets.",
            "`,changelog` -- what changed in the last release.",
            "`,inbox` -- notifications you've collected (event starts, etc).",
            "`,leaderboard` -- top players in this server by net worth.",
            "`,profile equip <slot> <item>` -- customise your card.",
            "`,tour` again any time to re-read this deck.",
        ],
    },
]


async def get_progress(db, user_id: int) -> int:
    """Return the index of the next card to show (0 if untouched, len(DECK) if done)."""
    try:
        row = await db.fetch_one(
            "SELECT deck_progress, completed_at FROM user_onboarding "
            "WHERE user_id = $1",
            user_id,
        )
        if not row:
            return 0
        if row.get("completed_at"):
            return len(DECK)
        return min(len(DECK), int(row.get("deck_progress") or 0))
    except Exception:
        return 0


async def set_progress(db, user_id: int, idx: int) -> int:
    """Persist the user's current deck index.

    Clamps to ``[0, len(DECK)]``. When ``idx >= len(DECK)`` stamps
    ``completed_at`` (keeping any prior completion timestamp). Returns the
    clamped index actually written. The caller owns the navigation cursor
    -- this function never reads the existing row, so reruns after a prior
    completion can't bleed stale state into a fresh walkthrough.
    """
    try:
        nxt = max(0, min(len(DECK), int(idx)))
        if nxt >= len(DECK):
            await db.execute(
                "INSERT INTO user_onboarding (user_id, deck_progress, completed_at, last_seen_at) "
                "VALUES ($1, $2, $3, $3) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "  deck_progress = EXCLUDED.deck_progress, "
                "  completed_at = COALESCE(user_onboarding.completed_at, EXCLUDED.completed_at), "
                "  last_seen_at = EXCLUDED.last_seen_at",
                user_id, nxt, datetime.now(timezone.utc),
            )
        else:
            await db.execute(
                "INSERT INTO user_onboarding (user_id, deck_progress, completed_at, last_seen_at) "
                "VALUES ($1, $2, NULL, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "  deck_progress = EXCLUDED.deck_progress, "
                "  completed_at = NULL, "
                "  last_seen_at = EXCLUDED.last_seen_at",
                user_id, nxt,
            )
        return nxt
    except Exception:
        log.exception("onboarding: set_progress failed uid=%s idx=%s", user_id, idx)
        return 0


async def skip(db, user_id: int) -> None:
    try:
        await db.execute(
            "INSERT INTO user_onboarding (user_id, deck_progress, skipped_at, last_seen_at) "
            "VALUES ($1, $2, now(), now()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "  skipped_at = EXCLUDED.skipped_at, "
            "  deck_progress = EXCLUDED.deck_progress",
            user_id, len(DECK),
        )
    except Exception:
        log.exception("onboarding: skip failed uid=%s", user_id)


def render_card(idx: int, total: int = len(DECK)) -> bytes:
    """Render one deck card (1200x720)."""
    if idx < 0 or idx >= len(DECK):
        return _render_done()
    deck_card = DECK[idx]
    canvas = RenderCanvas(1200, 720, bg=C_NAVY, gradient_to=C_CHART_BG)
    canvas.title(
        f"Card {idx + 1} of {total}  -  {deck_card['title']}",
        subtitle="Welcome to Discoin -- a guided tour",
        color=int(deck_card["color"]),
    )
    # Halo behind the title so the section colour reads at a glance.
    canvas.halo((40, 30, 1140, 110), int(deck_card["color"]), radius=18, alpha=90)
    # Blurb panel (top)
    canvas.rounded_panel((40, 120, 1160, 200), color=C_CHART_BG, radius=14)
    blurb = deck_card.get("blurb") or ""
    _draw_wrapped(canvas, blurb, x=60, y=138, width=1100, color=0xBFC7D5, size=16)
    # Lines panel (bottom)
    canvas.rounded_panel((40, 220, 1160, 660), color=C_CHART_BG, radius=14)
    y = 248
    for line in deck_card["lines"]:
        canvas.text((70, y), line, color=0xDDE2EB, size=18)
        y += 38
    # Progress bar
    canvas.progress_bar(
        (40, 678, 1160, 700),
        (idx + 1) / total,
        color=int(deck_card["color"]),
        label=f"{idx + 1} / {total}",
    )
    return canvas.to_png_bytes()


def _render_done() -> bytes:
    canvas = RenderCanvas(1200, 720, bg=C_NAVY, gradient_to=C_CHART_BG)
    canvas.title(
        "Tour complete  -  welcome to Discoin.",
        subtitle="Run ,help anytime to dig deeper.",
        color=C_GOLD,
    )
    canvas.halo((40, 30, 1140, 110), C_GOLD, radius=18, alpha=100)
    canvas.rounded_panel((40, 120, 1160, 660), color=C_CHART_BG, radius=14)
    lines = [
        "Every command you saw is available right now -- pick one and try it.",
        "`,profile` -- set your title / banner / frame / sigil.",
        "`,mastery` -- inspect your nine skill tracks and the node tree.",
        "`,apex` -- see what world event is currently buffing the server.",
        "`,inbox` -- read notifications you've collected during the tour.",
        "`,help` -- the full command index, grouped by system.",
        "`,leaderboard` -- see who's at the top of this server.",
        "`,changelog` -- what changed in the latest release.",
        "`,tour` again any time to re-read this deck.",
    ]
    y = 160
    for line in lines:
        canvas.text((70, y), line, color=0xDDE2EB, size=18)
        y += 38
    canvas.footer("Discoin tour complete")
    return canvas.to_png_bytes()


def _draw_wrapped(
    canvas: RenderCanvas,
    text: str,
    *,
    x: int,
    y: int,
    width: int,
    color: int,
    size: int,
    line_height: int | None = None,
) -> None:
    """Word-wrap ``text`` against ``width`` px and draw it on ``canvas``."""
    if not text:
        return
    line_height = line_height or (size + 6)
    words = text.split()
    cur = ""
    cy = y
    from core.framework.render_primitives import font as _font
    f = _font(size)
    for word in words:
        candidate = (cur + " " + word).strip()
        if int(canvas.draw.textlength(candidate, font=f)) <= width:
            cur = candidate
            continue
        canvas.text((x, cy), cur, color=color, size=size)
        cy += line_height
        cur = word
    if cur:
        canvas.text((x, cy), cur, color=color, size=size)
