"""services/showcase.py -- aggregate every game's stats for ``,me``.

Single entry point: ``compute_showcase(db, gid, uid)`` returns a
``ShowcaseResult`` with one section per game / system. The cog
(cogs/showcase.py) renders it into a paginated embed view.

Reads exclusively. Pulls existing rollups -- compute_net_worth,
the user-fishing / user-farming / user-dungeon / user-crafting / user-
buddy-economy rows -- and composes them into a single shape so the
,me command doesn't have to know which table owns what.

Failures degrade silently: a missing user_fishing row is fine, the
section just renders empty. The whole thing must NEVER raise to the
cog -- ,me is purely a read-only view.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database

log = logging.getLogger("discoin.showcase")


@dataclass(slots=True)
class ShowcaseSection:
    """One pane on the ,me showcase.

    ``title`` is the tab label. ``lines`` is the body content in
    order. Every line is a string; the cog wraps them in code blocks
    or fields depending on whether ``inline_field`` is set.
    """
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ShowcaseResult:
    """Composed snapshot of every system the user touches.

    Sections are returned in a stable order so the paginator's tabs
    don't shuffle between calls. Empty sections are kept (so a player
    who hasn't touched fishing still sees a Fishing tab saying so).
    """
    overview:    ShowcaseSection                         # name + level + net worth
    wallet:      ShowcaseSection                         # USD + every held token
    fishing:     ShowcaseSection
    farming:     ShowcaseSection
    dungeon:     ShowcaseSection
    crafting:    ShowcaseSection
    buddy:       ShowcaseSection
    achievements: ShowcaseSection                        # count + recent badges


def _empty_section(title: str) -> ShowcaseSection:
    return ShowcaseSection(title=title, lines=["_(no activity yet)_"])


async def _compute_overview(
    db: "Database", gid: int, uid: int,
    member_name: str,
) -> ShowcaseSection:
    sec = ShowcaseSection(title="\U0001F464 Overview")
    sec.lines.append(f"**{member_name}**")
    try:
        from services.net_worth import compute_net_worth
        nw = await compute_net_worth(uid, gid, db)
        sec.lines.append(f"Net worth: **${nw.total:,.2f}**")
        sec.lines.append(
            f"  • Wallet: ${nw.wallet:,.2f}  •  Bank: ${nw.bank:,.2f}"
        )
        sec.lines.append(
            f"  • CeFi: ${nw.cefi_crypto:,.2f}  •  DeFi: ${nw.defi_wallet:,.2f}"
        )
        sec.lines.append(
            f"  • LP: ${nw.lp_value:,.2f}  •  Stake: "
            f"${(nw.stake_value + nw.pos_stake_value):,.2f}"
        )
    except Exception:
        log.debug("showcase overview: net worth failed", exc_info=True)
        sec.lines.append("_(net worth unavailable)_")
    return sec


async def _compute_wallet(
    db: "Database", gid: int, uid: int,
) -> ShowcaseSection:
    sec = ShowcaseSection(title="\U0001F4B0 Wallet")
    try:
        user_row = await db.get_user(uid, gid)
        sec.lines.append(f"USD wallet: **${(user_row or {}).h('wallet'):,.2f}**")
        sec.lines.append(f"USD bank:   **${(user_row or {}).h('bank'):,.2f}**")
    except Exception:
        sec.lines.append("_(wallet unavailable)_")

    # Pull every wallet_holdings row across all networks. Sort by
    # USD value descending so the most-valuable holdings sit at the
    # top. Quietly drops zero-balance rows.
    try:
        rows = await db.fetch_all(
            "SELECT network, symbol, amount FROM wallet_holdings "
            "WHERE user_id = $1 AND guild_id = $2 AND amount > 0 "
            "ORDER BY symbol",
            int(uid), int(gid),
        )
    except Exception:
        rows = []
    if rows:
        sec.lines.append("")
        sec.lines.append("**Holdings:**")
        for r in rows:
            sym = str(r.get("symbol") or "?")
            net = str(r.get("network") or "?")
            sec.lines.append(f"  `{sym:<7}` ({net}): {r.h('amount'):,.4f}")
    return sec


async def _compute_fishing(
    db: "Database", gid: int, uid: int,
) -> ShowcaseSection:
    sec = ShowcaseSection(title="\U0001F3A3 Fishing")
    try:
        row = await db.fetch_one(
            "SELECT fish_level, fish_xp, rod_tier, total_lure_earned_raw, "
            "total_reel_earned_raw, total_caught, biggest_lbs, "
            "wild_battles_won, wild_battles_lost, wild_buddies_captured "
            "FROM user_fishing WHERE guild_id=$1 AND user_id=$2",
            int(gid), int(uid),
        )
    except Exception:
        row = None
    if not row:
        return _empty_section(sec.title)
    sec.lines.append(
        f"Lv **{int(row.get('fish_level') or 1)}** "
        f"({int(row.get('fish_xp') or 0):,} XP) "
        f"· Rod tier **{int(row.get('rod_tier') or 0)}**"
    )
    sec.lines.append(f"Caught: **{int(row.get('total_caught') or 0):,}**")
    big = float(row.get("biggest_lbs") or 0.0)
    if big > 0:
        sec.lines.append(f"Biggest: **{big:,.2f} lb**")
    sec.lines.append(
        f"Lifetime LURE: **{row.h('total_lure_earned_raw'):,.2f}** · "
        f"REEL: **{row.h('total_reel_earned_raw'):,.2f}**"
    )
    won = int(row.get("wild_battles_won") or 0)
    lost = int(row.get("wild_battles_lost") or 0)
    cap = int(row.get("wild_buddies_captured") or 0)
    if won or lost or cap:
        sec.lines.append(
            f"Wild battles: **{won}W / {lost}L** · captures **{cap}**"
        )
    return sec


async def _compute_farming(
    db: "Database", gid: int, uid: int,
) -> ShowcaseSection:
    sec = ShowcaseSection(title="\U0001F33E Farming")
    try:
        row = await db.fetch_one(
            "SELECT farm_level, farm_xp, plot_tier, total_planted, total_harvested, "
            "total_seed_earned_raw, total_hrv_earned_raw, "
            "wild_battles_won, wild_battles_lost, wild_buddies_captured "
            "FROM user_farming WHERE guild_id=$1 AND user_id=$2",
            int(gid), int(uid),
        )
    except Exception:
        row = None
    if not row:
        return _empty_section(sec.title)
    sec.lines.append(
        f"Lv **{int(row.get('farm_level') or 1)}** "
        f"({float(row.get('farm_xp') or 0):,.0f} XP) · "
        f"Plot tier **{int(row.get('plot_tier') or 1)}**"
    )
    sec.lines.append(
        f"Planted **{int(row.get('total_planted') or 0):,}** · "
        f"Harvested **{int(row.get('total_harvested') or 0):,}**"
    )
    sec.lines.append(
        f"Lifetime SEED: **{row.h('total_seed_earned_raw'):,.2f}** · "
        f"HRV: **{row.h('total_hrv_earned_raw'):,.2f}**"
    )
    won = int(row.get("wild_battles_won") or 0)
    lost = int(row.get("wild_battles_lost") or 0)
    cap = int(row.get("wild_buddies_captured") or 0)
    if won or lost or cap:
        sec.lines.append(
            f"Wild battles: **{won}W / {lost}L** · captures **{cap}**"
        )
    return sec


async def _compute_dungeon(
    db: "Database", gid: int, uid: int,
) -> ShowcaseSection:
    sec = ShowcaseSection(title="\U0001F5FA Dungeon")
    try:
        row = await db.fetch_one(
            "SELECT class_key, level, xp, deepest_floor, total_kills, "
            "total_captures, wild_battles_won, wild_battles_lost, "
            "wild_buddies_captured "
            "FROM user_dungeon WHERE guild_id=$1 AND user_id=$2",
            int(gid), int(uid),
        )
    except Exception:
        row = None
    if not row:
        return _empty_section(sec.title)
    sec.lines.append(
        f"Class: **{(row.get('class_key') or '?').title()}** · "
        f"Lv **{int(row.get('level') or 1)}** "
        f"({int(row.get('xp') or 0):,} XP)"
    )
    sec.lines.append(
        f"Deepest floor: **F{int(row.get('deepest_floor') or 0)}** · "
        f"Kills **{int(row.get('total_kills') or 0):,}** · "
        f"Captures **{int(row.get('total_captures') or 0):,}**"
    )
    won = int(row.get("wild_battles_won") or 0)
    lost = int(row.get("wild_battles_lost") or 0)
    cap = int(row.get("wild_buddies_captured") or 0)
    if won or lost or cap:
        sec.lines.append(
            f"Wild battles: **{won}W / {lost}L** · captures **{cap}**"
        )
    return sec


async def _compute_crafting(
    db: "Database", gid: int, uid: int,
) -> ShowcaseSection:
    sec = ShowcaseSection(title="\U0001F528 Crafting")
    try:
        row = await db.fetch_one(
            "SELECT crafting_level, crafting_xp, total_crafts, "
            "total_ingot_earned_raw, total_forge_earned_raw "
            "FROM user_crafting WHERE guild_id=$1 AND user_id=$2",
            int(gid), int(uid),
        )
    except Exception:
        row = None
    if not row:
        return _empty_section(sec.title)
    sec.lines.append(
        f"Lv **{int(row.get('crafting_level') or 1)}** "
        f"({int(row.get('crafting_xp') or 0):,} XP) · "
        f"Crafts **{int(row.get('total_crafts') or 0):,}**"
    )
    sec.lines.append(
        f"Lifetime INGOT: **{row.h('total_ingot_earned_raw'):,.2f}** · "
        f"FORGE: **{row.h('total_forge_earned_raw'):,.2f}**"
    )
    return sec


async def _compute_buddy(
    db: "Database", gid: int, uid: int,
) -> ShowcaseSection:
    sec = ShowcaseSection(title="\U0001F436 Buddies")
    try:
        rows = await db.fetch_all(
            "SELECT name, species, level, rarity_tier, is_active "
            "FROM cc_buddies WHERE guild_id=$1 AND owner_user_id=$2 "
            "AND status='owned' "
            "ORDER BY is_active DESC, level DESC, id ASC",
            int(gid), int(uid),
        )
    except Exception:
        rows = []
    if not rows:
        return _empty_section(sec.title)
    rarity = ("Common", "Uncommon", "Rare", "Epic", "Legendary")
    sec.lines.append(f"Owned: **{len(rows)}**")
    for r in rows[:8]:
        rt = max(0, min(4, int(r.get("rarity_tier") or 1) - 1))
        active = "✨ " if r.get("is_active") else "  "
        sec.lines.append(
            f"{active}**{r.get('name')}** "
            f"({(r.get('species') or '').title()}, "
            f"Lv {int(r.get('level') or 1)}, {rarity[rt]})"
        )
    if len(rows) > 8:
        sec.lines.append(f"_+{len(rows) - 8} more_")
    return sec


async def _compute_achievements(
    db: "Database", gid: int, uid: int,
) -> ShowcaseSection:
    sec = ShowcaseSection(title="\U0001F3C5 Achievements")
    try:
        from services import achievements as _ach
        badges = await _ach.user_badges(db, int(uid), int(gid))
    except Exception:
        badges = []
    if not badges:
        return _empty_section(sec.title)
    sec.lines.append(f"Earned: **{len(badges)}**")
    # List the 8 most recent.
    for b in badges[:8]:
        icon = b.get("icon") or "\U0001F3C5"
        name = b.get("name") or b.get("badge_id") or "?"
        sec.lines.append(f"{icon}  **{name}**")
    if len(badges) > 8:
        sec.lines.append(f"_+{len(badges) - 8} more_")
    return sec


async def compute_showcase(
    db: "Database", gid: int, uid: int, *, member_name: str = "",
) -> ShowcaseResult:
    """Aggregate every system's snapshot for one user. Best-effort
    end-to-end: any individual section that fails to load renders as
    empty rather than aborting the whole call.
    """
    return ShowcaseResult(
        overview     = await _compute_overview(db, gid, uid, member_name or "Player"),
        wallet       = await _compute_wallet(db, gid, uid),
        fishing      = await _compute_fishing(db, gid, uid),
        farming      = await _compute_farming(db, gid, uid),
        dungeon      = await _compute_dungeon(db, gid, uid),
        crafting     = await _compute_crafting(db, gid, uid),
        buddy        = await _compute_buddy(db, gid, uid),
        achievements = await _compute_achievements(db, gid, uid),
    )
