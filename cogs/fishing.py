"""
cogs/fishing.py  -  Fishing minigame commands + animated cast view.

Top-level surface (all under the ``fish`` group; ``cast`` is an alias):
    ,fish              -- cast a line (animated, interactive)
    ,fish stats [@u]   -- stat panel
    ,fish inv          -- show fish + junk + bait inventory
    ,fish history      -- recent catches
    ,fish shop         -- browse rods + bait
    ,fish buy <key>    -- upgrade rod or buy bait
    ,fish bait <key>   -- equip bait (or `none`)
    ,fish zone <name>  -- switch zones
    ,fish zones        -- list zones with availability
    ,fish sell [key|all|junk]
    ,fish lb [biggest] -- leaderboard (lifetime payout default; biggest = trophy board)
    ,fish help

Heavy lifting lives in services.fishing -- this module is presentation only.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import discord
from discord.ext import commands

import configs.fishing_config as fc
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.cooldowns import user_cooldown
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.quick_buy import QuickBuyView
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_AMBER,
    C_BLURPLE,
    C_CRIMSON,
    C_ERROR,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_NEUTRAL,
    C_PURPLE,
    C_SUBTLE,
    C_SUCCESS,
    C_TEAL,
    FormatKit,
    fmt_token,
    fmt_ts,
    fmt_usd,
    mention,
)


# Display helpers for the LURE / REEL economy. Centralised here so a
# rename of either token symbol or emoji only touches fishing_config
# (the source of truth) plus this file (the display layer).
def _fmt_lure(amount: float) -> str:
    return fmt_token(amount, fc.LURE_SYMBOL, "\U0001FA9D")


def _fmt_reel(amount: float) -> str:
    return fmt_token(amount, fc.REEL_SYMBOL, "\U0001F3A3")


def _with_usd(amount: float, oracle: float) -> str:
    """Return ``"  ≈ $X.XX"`` when both inputs are positive, else ``""``.

    Lets every embed line tack a USD equivalent onto a LURE / REEL
    amount with a single helper instead of redefining the gating
    condition in every command. Empty string when there's nothing
    meaningful to render keeps callers branch-free.
    """
    if amount <= 0 or oracle <= 0:
        return ""
    return f"  ≈ **{fmt_usd(amount * oracle)}**"


# Footer fragment appended to LURE-mint receipts (sell, trap collect,
# cast payouts) so users see an explicit "no slippage" instead of silently
# wondering why the oracle didn't move. Burn paths show slippage via
# _gear_impact_lines / the swap+cashout receipts; mint paths stay flat.
_MINT_FOOTER: str = (
    "-# LURE earned -- no oracle impact (mint, not a burn)."
)


async def _oracle_pair(ctx: "DiscoContext") -> tuple[float, float]:
    """Fetch (lure_oracle, reel_oracle) for ``ctx.guild_id``.

    Returns 0.0 for either side that has no price row yet (fresh guild,
    LURE/REEL never seeded). Callers pass the values into ``_with_usd``
    which already handles the zero case so the embed degrades cleanly
    when the oracle isn't ready.
    """
    lp_row = await ctx.db.get_price(fc.LURE_SYMBOL, ctx.guild_id)
    rp_row = await ctx.db.get_price(fc.REEL_SYMBOL, ctx.guild_id)
    return (
        float(lp_row["price"]) if lp_row else 0.0,
        float(rp_row["price"]) if rp_row else 0.0,
    )


from services import fishing as fish_svc
from services.fishing import _as_dict as _jsonb_dict
from core.framework.slot_warning import (
    maybe_warn_full_slots as _maybe_warn_full_slots,
)


def _gear_impact_lines(impact: "fish_svc.GearSpendImpact | None") -> str:
    """Render REEL gear-spend impact as the tail of a buy receipt.

    Mirrors the slippage / oracle / LP-reward block used by ,fish swap
    and ,fish cashout so the three burn surfaces all read the same.
    Returns an empty string when the spend was free (tier 0 rod) or
    the helper short-circuited so the caller can ``+=`` it without
    branching.
    """
    if impact is None:
        return ""
    lines = [
        f"-# Spent **{_fmt_reel(impact.reel_amount_human)}** "
        f"≈ **{fmt_usd(impact.usd_value)}**",
        f"-# REEL oracle: **${impact.oracle_before:,.6f} -> "
        f"${impact.oracle_after:,.6f}** "
        f"(slippage **{impact.price_impact_pct * 100:.2f}%**)",
    ]
    if impact.lp_reward_usd > 0:
        lines.append(
            f"-# Paid **{fmt_usd(impact.lp_reward_usd)}** to REEL LP holders."
        )
    return "\n".join(lines)

log = logging.getLogger(__name__)


# === HELPERS_START ===
# ============================================================================
# Embed builders + small render helpers
# ============================================================================

def _cast_context_footer(state: dict, *, combo: int | None = None) -> str:
    """Build the always-visible ``Combo xN | Lv.N | Rod | Bait | Zone``
    footer that rides under every cast-animation frame.

    Pulled from the user_fishing row state passed in (avoids a per-
    frame DB hit). ``combo`` defaults to the row's current_combo so
    callers don't have to thread a separate value through; pass it
    explicitly when the displayed value should differ from the DB
    (e.g. mid-bite-window before the resolver has bumped or reset).
    """
    rod_tier = int(state.get("rod_tier") or 0)
    rod = fc.rod_meta(rod_tier)
    zone = fc.zone_meta(str(state.get("current_zone") or fc.DEFAULT_ZONE))
    bait_key = state.get("equipped_bait") or ""
    bait = fc.bait_meta(bait_key) if bait_key else None
    bait_label = (
        f"{bait['name']}" if bait else "no bait"
    )
    cur_combo = int(combo if combo is not None else (state.get("current_combo") or 0))
    fish_xp = int(state.get("fish_xp") or 0)
    level = fc.level_from_xp(fish_xp)
    return (
        f"Combo x{cur_combo}  •  Lv.{level}  •  "
        f"{rod['name']}  •  {bait_label}  •  {zone['name']}"
    )


def _frame_embed(frame_key: str, *, title: str, color: int,
                 hint: str = "", footer: str | None = None) -> discord.Embed:
    """Render a single animation frame as an embed.

    Frames live in fishing_config.FRAMES and are wrapped in a code
    fence so monospace alignment survives Discord's font. Hint text
    and footer are optional so the same helper covers every step of
    the cast sequence. ``hint`` defaults to a random pick from
    ``HINT_POOLS[frame_key]`` if the caller passes the literal sentinel
    string ``""``; pass an explicit non-empty hint to override.
    """
    frame = fc.FRAMES.get(frame_key, "")
    desc = f"```\n{frame}\n```"
    if hint:
        desc += f"\n{hint}"
    builder = card(title, color=color).description(desc)
    if footer:
        builder = builder.footer(footer)
    return builder.build()


def _stats_embed(
    state: dict,
    *,
    member: discord.Member | None,
    lure_balance: float = 0.0,
    reel_balance: float = 0.0,
    lure_staked: float = 0.0,
    pending_reel: float = 0.0,
    lure_oracle: float = 0.0,
    reel_oracle: float = 0.0,
    lp_lines: list[str] | None = None,
) -> discord.Embed:
    """The ``,fish stats`` panel.

    Shows level, XP bar, combo, biggest catch, totals, equipped gear,
    and current zone. Mirrors the buddy-stats layout (FormatKit.bar
    for XP) so the two surfaces feel sibling. Token balances + USD
    values + any LURE/REEL LP positions render at the bottom so the
    panel is a one-stop snapshot of the player's fishing economy
    exposure.
    """
    rod_tier = int(state.get("rod_tier") or 0)
    rod = fc.rod_meta(rod_tier)
    zone = fc.zone_meta(str(state.get("current_zone") or fc.DEFAULT_ZONE))
    bait = fc.bait_meta(state.get("equipped_bait")) or {}
    fish_xp = int(state.get("fish_xp") or 0)
    level = fc.level_from_xp(fish_xp)
    into, span = fc.xp_to_next(fish_xp)
    bar = FormatKit.bar(into, max(span, 1), width=10, show_pct=False)

    biggest_key = state.get("biggest_fish") or ""
    biggest_meta = fc.fish_meta(biggest_key) if biggest_key else None
    biggest_emoji = (biggest_meta or {}).get("emoji", "") if biggest_meta else ""
    biggest_name = (biggest_meta or {}).get("name") or biggest_key or "-"
    biggest_lbs = float(state.get("biggest_lbs") or 0.0)

    lure_line = (
        f"{_fmt_lure(lure_balance)}{_with_usd(lure_balance, lure_oracle)}"
    )
    reel_line = (
        f"{_fmt_reel(reel_balance)}{_with_usd(reel_balance, reel_oracle)}"
    )
    stake_pieces = []
    if lure_staked > 0:
        stake_pieces.append(
            f"Staked {_fmt_lure(lure_staked)}"
            f"{_with_usd(lure_staked, lure_oracle)}"
        )
    if pending_reel > 0:
        stake_pieces.append(
            f"Pending {_fmt_reel(pending_reel)}"
            f"{_with_usd(pending_reel, reel_oracle)}"
        )
    stake_line = "\n".join(stake_pieces) if stake_pieces else "_(no stake)_"

    # Live oracle pair surfaced in the description so players never have
    # to guess the rate the rest of the panel is quoted at. Cleanly
    # degrades to "(no quote)" when the price row hasn't been seeded.
    lure_quote = f"${lure_oracle:,.6f}" if lure_oracle > 0 else "_(no quote)_"
    reel_quote = f"${reel_oracle:,.6f}" if reel_oracle > 0 else "_(no quote)_"
    oracle_desc = (
        f"-# Oracle: **LURE/USD {lure_quote}**  -  "
        f"**REEL/USD {reel_quote}**"
    )

    lifetime_lure_h = to_human(int(state.get("total_lure_earned_raw") or 0))
    lifetime_lure_line = (
        f"{_fmt_lure(lifetime_lure_h)}{_with_usd(lifetime_lure_h, lure_oracle)}"
    )

    name = (member.display_name if member else "Fisher")
    embed = (
        card(f"\U0001F3A3 {name}'s Tackle Box", color=C_TEAL)
        .description(oracle_desc)
        .field(
            f"Lv. {level}  -  {fish_xp:,} XP",
            f"`{bar}` {into:,}/{span:,}" if span else "`██████████` MAX",
            inline=False,
        )
        .field(f"\U0001F38B Rod", f"{rod['emoji']} **{rod['name']}**", True)
        .field(f"\U0001F41F Zone", f"{zone['emoji']} **{zone['name']}**", True)
        .field(
            "\U0001FAB1 Bait",
            (f"{bait.get('emoji', '')} **{bait.get('name', '-')}** "
             f"x {int(_jsonb_dict(state.get('bait_inventory')).get(state.get('equipped_bait') or '', 0))}")
            if state.get("equipped_bait") else "_(none)_",
            True,
        )
        .field("Caught", f"**{int(state.get('total_caught') or 0):,}**", True)
        .field("Junk pulled", f"**{int(state.get('total_junk') or 0):,}**", True)
        .field(
            "Lifetime LURE",
            lifetime_lure_line,
            True,
        )
        .field(
            "Combo",
            f"**{int(state.get('current_combo') or 0)}**  "
            f"(longest **{int(state.get('longest_combo') or 0)}**)",
            True,
        )
        .field("LURE wallet", lure_line, True)
        .field("REEL wallet", reel_line, True)
        .field("Stake", stake_line, False)
        .field(
            "Biggest catch",
            f"{biggest_emoji} **{biggest_name}** -- **{biggest_lbs:,.2f} lbs**"
            if biggest_lbs > 0 else "_(none yet)_",
            False,
        )
    )
    if lp_lines:
        embed = embed.field(
            "LURE/REEL LP positions",
            "\n".join(lp_lines),
            False,
        )
    if state.get("last_cast_at"):
        embed = embed.footer(f"Last cast {fmt_ts(state['last_cast_at'])}")
    return embed.build()


def _result_embed(result: fish_svc.CastResult, *, member: discord.Member,
                  state_after: dict, lure_oracle: float = 0.0) -> discord.Embed:
    """Final reveal embed shown when the animation ends.

    ``lure_oracle`` is the live LURE/USD price; passing it in (instead of
    fetching here) keeps the embed builder side-effect free and lets the
    caller batch the oracle fetch with whatever else it needs. When the
    oracle is 0.0 the USD line is dropped, never shown as $0.00.
    """
    if result.outcome == "fish":
        rarity_meta = fc.rarity_meta(result.rarity or "common")
        color = int(rarity_meta.get("color_hex") or C_TEAL)
        meta = result.fish_meta or {}
        emoji = str(meta.get("emoji") or "\U0001F41F")
        name = str(meta.get("name") or result.fish_key or "Fish")
        desc_lines = [
            f"```\n{fc.FRAMES['fish']}\n```",
            f"{emoji} **{name}** -- {rarity_meta.get('label', 'Common')}",
            f"Weight: **{result.weight_lbs:,.2f} lbs**",
        ]
        if result.combo_mult > 1.0:
            desc_lines.append(
                f"Combo x{result.new_combo}  -  "
                f"**+{(result.combo_mult - 1.0) * 100:.0f}%** payout boost"
            )
        if result.quality_mult >= fc.HOOK_SWEET_BONUS:
            desc_lines.append(f"✨ Sweet hook! +{(result.quality_mult-1)*100:.0f}% size")
        elif result.quality_mult <= fc.HOOK_LATE_PENALTY + 0.001:
            desc_lines.append(f"Late hook  -  -{int((1 - result.quality_mult) * 100)}% size")
        _fact = fc.fish_fact(result.fish_key or "")
        if _fact:
            desc_lines.append(f"\n_{_fact}_")
        sells_for = fc.fish_payout(
            result.fish_key or "", result.weight_lbs,
            combo_mult=1.0, quality_mult=1.0,
            zone=str(state_after.get("current_zone") or fc.DEFAULT_ZONE),
        )
        embed = (
            card("\U0001F3A3 Reel In!", color=color)
            .description("\n".join(desc_lines))
        )
        embed = embed.field("XP", f"+{result.xp_gained}", True)
        embed = embed.field(
            "Sells for",
            f"{_fmt_lure(sells_for)}{_with_usd(sells_for, lure_oracle)}",
            True,
        )
        if result.leveled_up:
            embed = embed.field(
                "Level up!", f"You're now Lv. **{result.new_level}**.", False,
            )
        embed = embed.footer(_MINT_FOOTER.lstrip("-# ").rstrip())
        return embed.build()

    if result.outcome == "junk":
        meta = result.junk_meta or {}
        emoji = str(meta.get("emoji") or "\U0001F5D1")
        name = str(meta.get("name") or result.junk_key or "Trash")
        desc = (
            f"```\n{fc.FRAMES['trash']}\n```"
            f"\n{emoji} **{name}**\n"
            f"Salvage: {_fmt_lure(result.payout_lure)}"
            f"{_with_usd(result.payout_lure, lure_oracle)}\n"
            f"{_MINT_FOOTER}"
        )
        return card("\U0001F61E Just Trash...", description=desc, color=C_NEUTRAL).build()

    if result.outcome == "money_bag":
        desc = (
            f"```\n{fc.FRAMES['bonus']}\n```"
            f"\n\U0001F4B0 **Money bag!** {_fmt_lure(result.payout_lure)}"
            f"{_with_usd(result.payout_lure, lure_oracle)} added to your tackle bag.\n"
            f"{_MINT_FOOTER}"
        )
        return card("\U0001F4B0 LURE Catch", description=desc, color=C_GOLD).build()

    if result.outcome == "mystery_box":
        desc = (
            f"```\n{fc.FRAMES['bonus']}\n```"
            f"\n\U0001F381 **Mystery box!** {_fmt_lure(result.payout_lure)}"
            f"{_with_usd(result.payout_lure, lure_oracle)} credited.\n"
            f"{_MINT_FOOTER}"
        )
        return card("\U0001F381 Mystery Box", description=desc, color=C_PURPLE).build()

    if result.outcome == "buddy_egg":
        if result.buddy_row:
            species = result.buddy_row.get("species", "?")
            name = result.buddy_row.get("name", "?")
            tier = int(result.buddy_row.get("rarity_tier") or 1)
            try:
                from configs.buddies_config import SPECIES, rarity_meta as _b_rarity
                emoji = str((SPECIES.get(species) or {}).get("emoji") or "\U0001F95A")
                tier_name = str(_b_rarity(tier).get("name") or "Common")
            except Exception:
                emoji, tier_name = "\U0001F95A", "Common"
            desc = (
                f"```\n{fc.FRAMES['egg']}\n```\n"
                f"{emoji} **{name}** the {tier_name} {species} hatched from your egg!\n"
                f"_Promote it from `,buddy` to set it active._"
            )
            return card("✨ A Buddy Egg!", description=desc, color=C_GOLD).build()
        if result.stored_egg:
            # Shelter was full but the player had room in held_eggs --
            # the egg gets saved instead of being silently liquidated.
            # Player can sell, gift, or hatch later via `,fish egg`.
            species = str(result.stored_egg.get("species") or "?")
            tier = int(result.stored_egg.get("rarity_tier") or 1)
            try:
                from configs.buddies_config import SPECIES, rarity_meta as _b_rarity
                emoji = str((SPECIES.get(species) or {}).get("emoji") or "\U0001F95A")
                tier_name = str(_b_rarity(tier).get("name") or "Common")
            except Exception:
                emoji, tier_name = "\U0001F95A", "Common"
            sell_lure = fc.egg_sell_lure(tier)
            desc = (
                f"```\n{fc.FRAMES['egg_stored']}\n```\n"
                f"{emoji} A **{tier_name} {species.title()} Egg** dropped, "
                f"but your active slots are full.\n"
                f"It's been tucked into your **held eggs** -- you can "
                f"hatch, gift, or sell it any time.\n"
                f"-# Sells for **{_fmt_lure(sell_lure)}**"
                f"{_with_usd(sell_lure, lure_oracle)}.  "
                f"Manage with `,fish egg`."
            )
            return card(
                f"✨ {tier_name} {species.title()} Egg Saved",
                description=desc, color=C_GOLD,
            ).build()
        # Fallback: shelter was full AND held-egg cap was reached.
        # Falls back to a mystery-box-style LURE payout (legacy path).
        desc = (
            f"```\n{fc.FRAMES['bonus']}\n```\n"
            f"✨ **Buddy egg!** But your active slots are full and you're "
            f"at the held-egg cap. The egg got sold for "
            f"{_fmt_lure(result.payout_lure)}"
            f"{_with_usd(result.payout_lure, lure_oracle)} instead.\n"
            f"{_MINT_FOOTER}"
        )
        return card("✨ Buddy Egg", description=desc, color=C_AMBER).build()

    if result.outcome == "wild_battle":
        wb = result.wild_buddy or {}
        species = str(wb.get("species") or "?")
        level = int(wb.get("level") or 1)
        rarity_tier = int(wb.get("rarity_tier") or 1)
        try:
            from configs.buddies_config import SPECIES, rarity_meta as _b_rarity
            emoji = str((SPECIES.get(species) or {}).get("emoji") or "\U0001F420")
            tier_name = str(_b_rarity(rarity_tier).get("name") or "Common")
        except Exception:
            emoji, tier_name = "\U0001F420", "Common"
        attractor_badge = (
            "\n\n\U0001F9F2 **Battle Attractor pulled this encounter** "
            "(your active boost lured a fight that wouldn't have spawned)."
            if getattr(result, "attractor_pulled", False) else ""
        )
        desc = (
            f"```\n{fc.FRAMES['bite']}\n```\n"
            f"{emoji} **A wild {tier_name} {species.title()}** (Lv. {level}) "
            f"surfaces and bares its teeth.\n\n"
            f"Press **Challenge** to fight it with your active buddy. "
            f"If you bail or wait too long, it slips back into the deep."
            f"{attractor_badge}"
        )
        return card(
            f"⚔️ Wild Encounter -- {species.title()}",
            description=desc, color=C_CRIMSON,
        ).build()

    # Miss
    desc = f"```\n{fc.FRAMES['miss']}\n```\nIt got away. Combo reset."
    return card("\U0001F914 The One That Got Away", description=desc,
                color=C_SUBTLE).build()


def _splash_embed(result: fish_svc.CastResult, *, member: discord.Member) -> discord.Embed:
    """Public splash announcement when someone lands a rare/epic/legendary."""
    rarity_meta = fc.rarity_meta(result.rarity or "rare")
    meta = result.fish_meta or {}
    emoji = str(meta.get("emoji") or "\U0001F420")
    name = str(meta.get("name") or result.fish_key or "Fish")
    color = int(rarity_meta.get("color_hex") or C_GOLD)
    desc = (
        f"{member.mention} just hooked a "
        f"**{rarity_meta.get('label', 'Rare')} {emoji} {name}** "
        f"weighing **{result.weight_lbs:,.2f} lbs**!"
    )
    return card("\U0001F3A3 Big Catch!", description=desc, color=color).build()


def _shop_embed(
    state: dict,
    *,
    reel_balance: float = 0.0,
    reel_oracle: float = 0.0,
    lure_oracle: float = 0.0,
) -> discord.Embed:
    """Two-column shop display: rods (left col) + bait (right col).

    The user_fishing row is passed in so we can mark the player's
    current rod tier and how many of each bait they hold without a
    second DB read. ``reel_balance`` / ``reel_oracle`` / ``lure_oracle``
    are pulled by the cog so prices render in USD alongside REEL and
    the trap base-haul line shows USD alongside LURE -- both currencies
    on every money figure so the shopper compares like with like.
    """
    cur_tier = int(state.get("rod_tier") or 0)
    bait_inv = _jsonb_dict(state.get("bait_inventory"))

    rod_lines = []
    for tier in sorted(fc.RODS.keys()):
        r = fc.RODS[tier]
        marker = " ✅" if tier == cur_tier else (" -  *next*" if tier == cur_tier + 1 else "")
        price_reel = float(r["price_reel"])
        if price_reel == 0:
            price = "free"
        else:
            price = _fmt_reel(price_reel)
            if reel_oracle > 0:
                price += f" ≈ {fmt_usd(price_reel * reel_oracle)}"
        rod_lines.append(
            f"`tier {tier}` {r['emoji']} **{r['name']}**  -  {price}{marker}\n"
            f"-# fish +{int(r['fish_bonus']*100)}% / rare +{int(r['rare_bonus']*100)}% / "
            f"weight +{int(r['weight_bonus']*100)}% / sweet +{r['sweet_window']:.2f}s"
        )

    bait_lines = []
    for k, b in fc.BAIT.items():
        owned = int(bait_inv.get(k, 0))
        each_reel = float(b["price_reel"])
        each = _fmt_reel(each_reel)
        if reel_oracle > 0 and each_reel > 0:
            each += f" ≈ {fmt_usd(each_reel * reel_oracle)}"
        bait_lines.append(
            f"{b['emoji']} **{b['name']}** (`{k}`)  -  "
            f"{each} ea\n"
            f"-# you have **{owned}/{b['max_stack']}**  -  "
            f"fish +{int(b['fish_bonus']*100)}% / rare +{int(b['rare_bonus']*100)}% / "
            f"bonus +{int(b['bonus_bonus']*100)}%"
        )

    trap_inv = _jsonb_dict(state.get("crab_trap_inventory"))
    trap_lines = []
    for k, t in fc.CRAB_TRAPS.items():
        owned = int(trap_inv.get(k, 0))
        each_reel = float(t["price_reel"])
        each = _fmt_reel(each_reel)
        if reel_oracle > 0 and each_reel > 0:
            each += f" ≈ {fmt_usd(each_reel * reel_oracle)}"
        soak_min = int(t["soak_seconds"]) // 60
        base_lure = float(t["base_yield_lure"])
        base_str = _fmt_lure(base_lure)
        if lure_oracle > 0:
            base_str += f" ≈ {fmt_usd(base_lure * lure_oracle)}"
        trap_lines.append(
            f"{t['emoji']} **{t['name']}** (`{k}`)  -  {each} ea\n"
            f"-# you have **{owned}/{t['max_stack']}**  -  "
            f"soak **{soak_min}m**  -  base haul **{base_str}**  -  "
            f"max zone tier **{t['max_zone_tier']}**"
        )

    bal_line = f"You have **{_fmt_reel(reel_balance)}**"
    if reel_oracle > 0 and reel_balance > 0:
        bal_line += f" ≈ **{fmt_usd(reel_balance * reel_oracle)}**"
    bal_line += " to spend."
    builder = (
        card("\U0001F3EA Tackle Shop", color=C_INFO)
        .description(f"{bal_line}\nBuy upgrades with `,fish buy <key> [qty]`.")
    )
    # Discord caps each field value at 1024 chars. With 9 rod tiers and
    # 9 trap tiers the rod / trap sections blow past that, so chunk
    # each section into as many sub-fields as needed.
    def _emit(label: str, lines: list[str]) -> None:
        if not lines:
            builder.field(label, "_(none)_", False)
            return
        chunk: list[str] = []
        chunk_len = 0
        first = True
        for ln in lines:
            ln_len = len(ln) + 1  # newline join
            if chunk and chunk_len + ln_len > 1024:
                builder.field(label if first else f"{label} (cont.)",
                              "\n".join(chunk), False)
                first = False
                chunk = [ln]
                chunk_len = ln_len
            else:
                chunk.append(ln)
                chunk_len += ln_len
        if chunk:
            builder.field(label if first else f"{label} (cont.)",
                          "\n".join(chunk), False)

    _emit("\U0001F38B Rods (one tier at a time)", rod_lines)
    _emit("\U0001FAB1 Bait", bait_lines)
    _emit("\U0001F980 Crab Traps (passive haul)", trap_lines)
    return (
        builder.footer(
            "Equip bait with `,fish bait <key>`  ·  "
            "Place: `,fish trap place <key> [qty]`  ·  Haul: `,fish trap collect`\n"
            "Sell: `,fish sell rod`  ·  `,fish sell traps`  ·  "
            "`,fish sell <trap_key> [qty|all]`"
        )
        .build()
    )


def _fmt_eta(seconds: int) -> str:
    """Render a coarse ETA string for trap soak countdowns.

    Optimised for the panel: ``45s``, ``12m``, ``2h 5m``. Always rounds
    down so a "1 minute" hint never fires before the trap is actually
    ready (the user follows up with ``,fish trap collect`` and the DB
    clock decides for sure).
    """
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _trap_status_embed(
    member: discord.Member, state: dict, summary: dict,
    *, lure_oracle: float = 0.0,
) -> discord.Embed:
    """Render the ``,fish trap`` overview: placed traps, ready count, ETAs.

    Crab traps are passive collectors -- this panel shows what's
    soaking, what's ready, and which trap is next to fill. Inventory
    of undeployed traps is summarised at the bottom so the player can
    decide whether to buy more or place what they have.

    ``lure_oracle`` is used to render the projected base haul in USD
    next to the LURE figure so players can compare trap tiers in the
    same currency they paid for them in.
    """
    rows = list(summary.get("rows") or [])
    placed_total = int(summary.get("placed_total") or 0)
    ready_total = int(summary.get("ready_total") or 0)

    # Group placed traps by (key, zone, ready) so a player with 8 traps
    # in one zone gets one tidy line per type instead of 8 dupes.
    grouped: dict[tuple[str, str, bool], dict] = {}
    for r in rows:
        bucket_key = (str(r.get("key")), str(r.get("zone")), bool(r.get("ready")))
        if bucket_key not in grouped:
            grouped[bucket_key] = {"count": 0, "min_eta": int(r.get("ready_in_s") or 0)}
        grouped[bucket_key]["count"] += 1
        grouped[bucket_key]["min_eta"] = min(
            grouped[bucket_key]["min_eta"], int(r.get("ready_in_s") or 0),
        )

    placed_lines = []
    for (k, z_key, is_ready), info in sorted(grouped.items(), key=lambda kv: (not kv[0][2], kv[0][0])):
        cfg = fc.crab_trap_meta(k) or {}
        z = fc.zone_meta(z_key)
        marker = "✅ ready" if is_ready else f"⏳ {_fmt_eta(info['min_eta'])}"
        placed_lines.append(
            f"{cfg.get('emoji', '')} **{cfg.get('name', k)}** x{info['count']}  -  "
            f"{z['emoji']} {z['name']}  -  {marker}"
        )

    inv = _jsonb_dict(state.get("crab_trap_inventory"))
    inv_lines = []
    for k, t in fc.CRAB_TRAPS.items():
        owned = int(inv.get(k, 0))
        if owned <= 0:
            continue
        base_lure = float(t["base_yield_lure"])
        inv_lines.append(
            f"{t['emoji']} **{t['name']}** x{owned}  -  "
            f"-# soak {int(t['soak_seconds']) // 60}m  -  "
            f"base **{_fmt_lure(base_lure)}**{_with_usd(base_lure, lure_oracle)}"
        )

    desc_lines = [
        f"```\n{_trap_frame(summary)}\n```",
        f"Placed: **{placed_total}/{fc.CRAB_TRAP_PLACED_CAP}**  -  "
        f"Ready: **{ready_total}**",
    ]
    if ready_total > 0:
        desc_lines.append(
            f"-# Pull them in with `,fish trap collect`."
        )
    elif placed_total == 0:
        desc_lines.append(
            f"-# No traps placed. Buy some with `,fish buy <trap_key>` "
            f"and place with `,fish trap place <key>`."
        )

    embed = (
        card(
            f"\U0001F980 {member.display_name}'s Crab Traps",
            color=C_TEAL,
        )
        .description("\n".join(desc_lines))
        .field(
            "Placed",
            "\n".join(placed_lines) if placed_lines else "_(none)_",
            False,
        )
        .field(
            "Undeployed",
            "\n".join(inv_lines) if inv_lines else "_(none)_",
            False,
        )
        .footer(
            "Traps are permanent gear -- they return to your inventory "
            "after each haul. Higher tiers soak longer, pay more, and "
            "bias toward rarer crabs."
        )
    )
    return embed.build()


def _trap_frame(summary: dict) -> str:
    """Pick the right ASCII frame for the current trap state."""
    if int(summary.get("ready_total") or 0) > 0:
        return fc.FRAMES.get("trap_ready", fc.FRAMES["trap_soak"])
    if int(summary.get("placed_total") or 0) > 0:
        return fc.FRAMES.get("trap_soak", "")
    return fc.FRAMES.get("trap_empty", "")


class _TrapPickSelect(discord.ui.Select):
    """Dropdown of traps the player owns (undeployed inventory).
    Selecting a row stores the chosen trap key on the parent TrapView
    so the Place button knows what to deploy.
    """

    def __init__(self, inv: dict) -> None:
        opts: list[discord.SelectOption] = []
        for k, t in fc.CRAB_TRAPS.items():
            owned = int(inv.get(k, 0))
            if owned <= 0:
                continue
            soak_min = int(t["soak_seconds"]) // 60
            opts.append(discord.SelectOption(
                label=f"{t['name']} (x{owned})",
                value=k,
                description=f"Soak {soak_min}m  -  {_fmt_lure(float(t['base_yield_lure']))} base"[:100],
                emoji=t.get("emoji", "\U0001F980"),
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no traps in inventory)",
                value="__none__",
                default=True,
            )]
        super().__init__(
            placeholder="Select trap to place...",
            options=opts[:25],
            min_values=1, max_values=1,
            row=1,
            disabled=(not opts or opts[0].value == "__none__"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "TrapView" = self.view  # type: ignore
        choice = self.values[0]
        if choice != "__none__":
            view._selected_trap = choice
        await interaction.response.defer()


class TrapView(discord.ui.View):
    """Interactive crab-trap panel.

    Owner-locked, 5-minute timeout. Buttons: Collect All, Place x1, Refresh.
    The trap dropdown selects which trap type to place; Place drops one into
    the player's current zone.
    """

    def __init__(self, ctx: "DiscoContext", cog: "Fishing") -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.cog = cog
        self.message: discord.Message | None = None
        self._selected_trap: str | None = None

    def _update_button_states(self, summary: dict) -> None:
        ready_total = int(summary.get("ready_total") or 0)
        placed_total = int(summary.get("placed_total") or 0)
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label == "Collect All":
                    child.disabled = ready_total == 0
                elif child.label == "Place x1":
                    child.disabled = placed_total >= fc.CRAB_TRAP_PLACED_CAP

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your trap panel. Run `,fish trap` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _refresh(self, interaction: discord.Interaction) -> None:
        state = await fish_svc.ensure_state(
            self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
        )
        summary = fish_svc.trap_status_summary(dict(state))
        lure_oracle, _ = await _oracle_pair(self.ctx)
        embed = _trap_status_embed(
            self.ctx.author, dict(state), summary,
            lure_oracle=lure_oracle,
        )
        # Rebuild pick select for updated inventory.
        inv = {}
        try:
            inv = _jsonb_dict(state.get("crab_trap_inventory"))
        except Exception:
            pass
        for child in list(self.children):
            if isinstance(child, _TrapPickSelect):
                self.remove_item(child)
        self.add_item(_TrapPickSelect(inv))
        self._update_button_states(summary)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Collect All", emoji="\U0001F980",
        style=discord.ButtonStyle.success, row=0,
    )
    async def btn_collect(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            res = await fish_svc.collect_crab_traps(
                self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if res.traps_collected == 0:
            await interaction.response.send_message(
                "Nothing's ready yet -- check the ETAs.", ephemeral=True,
            )
            return
        lure_oracle, _ = await _oracle_pair(self.ctx)
        desc = (
            f"```\n{fc.FRAMES['trap_haul']}\n```\n"
            f"Hauled **{res.traps_collected}** trap(s) -- "
            f"**{_fmt_lure(res.lure_paid)}** into your tackle bag."
        )
        await interaction.response.send_message(
            embed=card("\U0001F980 Crab Haul", description=desc, color=C_TEAL)
            .footer("Sell with `,fish sell` or `,fish sell <crab_key>`.")
            .build(),
            ephemeral=True,
        )
        state = await fish_svc.ensure_state(
            self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
        )
        summary = fish_svc.trap_status_summary(dict(state))
        lure_oracle2, _ = await _oracle_pair(self.ctx)
        embed = _trap_status_embed(
            self.ctx.author, dict(state), summary, lure_oracle=lure_oracle2,
        )
        inv = _jsonb_dict(state.get("crab_trap_inventory"))
        for child in list(self.children):
            if isinstance(child, _TrapPickSelect):
                self.remove_item(child)
        self.add_item(_TrapPickSelect(inv))
        self._update_button_states(summary)
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Place x1", emoji="\U0001FA9D",
        style=discord.ButtonStyle.primary, row=0,
    )
    async def btn_place(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        key = self._selected_trap
        if not key:
            await interaction.response.send_message(
                "Select a trap from the dropdown first.", ephemeral=True,
            )
            return
        try:
            state, placed = await fish_svc.place_crab_traps(
                self.ctx.db, self.ctx.guild_id, self.ctx.author.id, key, 1,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        t = fc.CRAB_TRAPS.get(key) or {}
        z = fc.zone_meta(str(state.get("current_zone") or fc.DEFAULT_ZONE))
        soak_min = int(t.get("soak_seconds", 900)) // 60
        await interaction.response.send_message(
            embed=card(
                "Trap Set",
                description=(
                    f"```\n{fc.FRAMES['trap_drop']}\n```\n"
                    f"{t.get('emoji', '')} **{t.get('name', key)}** dropped in "
                    f"{z['emoji']} **{z['name']}**.\n"
                    f"-# Soak time **{soak_min} min**."
                ),
                color=C_TEAL,
            ).build(),
            ephemeral=True,
        )
        summary = fish_svc.trap_status_summary(dict(state))
        lure_oracle, _ = await _oracle_pair(self.ctx)
        embed = _trap_status_embed(
            self.ctx.author, dict(state), summary, lure_oracle=lure_oracle,
        )
        inv = _jsonb_dict(state.get("crab_trap_inventory"))
        for child in list(self.children):
            if isinstance(child, _TrapPickSelect):
                self.remove_item(child)
        self.add_item(_TrapPickSelect(inv))
        self._update_button_states(summary)
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=0,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self._refresh(interaction)


def _egg_status_embed(
    member: discord.Member, summary: dict, *, lure_oracle: float = 0.0,
    fren_oracle: float = 0.0,
) -> discord.Embed:
    """Render ``,fish egg`` -- per-species/tier counts plus sell prices.

    Held eggs are unhatched buddies the player can sell, gift, or hatch
    later. Each row pairs the species + rarity with how many they hold
    and what one of them sells for at the live oracle, so the user can
    decide whether to liquidate or save for hatching.

    Also exposes the **stable index** for every held egg so the player
    can list a single specific one on the auction house with
    ``,ah list egg <idx> <price>`` -- handy when they hold multiple of
    the same species at different rarities and only want to sell one.
    """
    total = int(summary.get("total") or 0)
    by_st = summary.get("by_species_tier") or {}
    rows = summary.get("rows") or []

    if total <= 0:
        return (
            card(f"\U0001F95A {member.display_name}'s Held Eggs", color=C_GOLD)
            .description(
                "_(no held eggs)_\n\n"
                "Held eggs land in your inventory when a fishing buddy_egg "
                "rolls but your active slots are full. You can also buy "
                "them off the auction house with `,ah buy <listing_id>`. "
                "Once you have eggs you can hatch them, gift them, sell "
                "them to the LURE wallet, or list them on the auction "
                "house with `,ah list egg <species> <price>`."
            )
            .build()
        )

    try:
        from configs.buddies_config import (
            SPECIES,
            rarity_meta as _b_rarity,
        )
    except Exception:
        SPECIES = {}
        _b_rarity = lambda t: {"name": f"Tier {t}"}  # type: ignore

    summary_lines = []
    total_value_fren = 0.0
    # Sort by rarity desc so the most valuable rows surface first.
    for (species, tier), count in sorted(
        by_st.items(), key=lambda kv: (-kv[0][1], kv[0][0]),
    ):
        emoji = str((SPECIES.get(species) or {}).get("emoji") or "\U0001F95A")
        tier_name = str(_b_rarity(int(tier)).get("name") or f"Tier {tier}")
        sell_each = fc.egg_sell_lure(int(tier))
        total_value_fren += sell_each * int(count)
        usd_tag = (
            f"  ~ {fmt_usd(sell_each * fren_oracle)}"
            if fren_oracle > 0 else ""
        )
        summary_lines.append(
            f"{emoji} **{tier_name} {species.title()} Egg** x{count}  -  "
            f"sells for **{sell_each:,.0f} FREN**{usd_tag} ea"
        )

    # Per-egg index list so the player can quote any single one. Sorted
    # by rarity desc / species asc so the legendary you actually care
    # about isn't buried underneath ten common dupes. Eggs are
    # genderless until they hatch, so no glyph is shown here.
    indexed_lines = []
    for r in sorted(
        rows,
        key=lambda r: (-int(r.get("rarity_tier") or 1),
                       str(r.get("species") or "")),
    ):
        idx = int(r.get("idx") or 0)
        sp = str(r.get("species") or "?")
        tier = int(r.get("rarity_tier") or 1)
        emoji = str((SPECIES.get(sp) or {}).get("emoji") or "\U0001F95A")
        tier_name = str(_b_rarity(tier).get("name") or f"Tier {tier}")
        indexed_lines.append(
            f"`#{idx:>2}` {emoji} {tier_name} {sp.title()}"
        )

    total_usd_tag = (
        f"  ~ {fmt_usd(total_value_fren * fren_oracle)}"
        if fren_oracle > 0 else ""
    )
    desc_lines = [
        f"```\n{fc.FRAMES['egg_stored']}\n```",
        f"You hold **{total}/{fc.MAX_HELD_EGGS}** eggs  -  "
        f"total sale value: **{total_value_fren:,.0f} FREN**"
        f"{total_usd_tag}.",
        "-# Hatch with `,buddy egg hatch [species]` (active or storage slot needed).",
        "-# Gift with `,buddy egg gift @user [species] [count]`.",
        "-# Sell to FREN wallet with `,buddy egg sell [count|all|<species>]`.",
        "-# Auction House: `,ah list egg <idx|species[:tier]> <price>` "
        "(BUD by default; browse with `,ah browse egg`).",
    ]
    def _chunk_lines(lines: list[str], cap: int = 1000) -> list[list[str]]:
        """Pack ``lines`` into successive chunks under Discord's 1024-char
        field cap. Returns a list of line-lists, never empty."""
        out: list[list[str]] = [[]]
        cur = 0
        for ln in lines:
            if cur + len(ln) + 1 > cap and out[-1]:
                out.append([])
                cur = 0
            out[-1].append(ln)
            cur += len(ln) + 1
        return out

    builder = (
        card(
            f"\U0001F95A {member.display_name}'s Held Eggs", color=C_GOLD,
        )
        .description("\n".join(desc_lines))
    )
    # Both fields chunk: a player with many distinct (species, tier)
    # buckets can overflow "By species + rarity" the same way the
    # per-egg list does once they hold ~13+ kinds.
    for i, chunk in enumerate(_chunk_lines(summary_lines)):
        if not chunk:
            continue
        title = "By species + rarity"
        if i > 0:
            title += " (cont.)"
        builder = builder.field(title, "\n".join(chunk), False)
    for i, chunk in enumerate(_chunk_lines(indexed_lines)):
        if not chunk:
            continue
        title = "Each egg (use idx with `,ah list egg`)"
        if i > 0:
            title += " (cont.)"
        builder = builder.field(title, "\n".join(chunk), False)
    return (
        builder
        .footer(
            "Held eggs are inert -- no decay, no oracle impact. "
            "They keep their species + rarity until you hatch or sell."
        )
        .build()
    )


# ---------------------------------------------------------------------------
# Egg picker view -- powers ``,fish egg`` interactive surface.
# ---------------------------------------------------------------------------


class _EggSpeciesSelect(discord.ui.Select):
    """Dropdown of every (species, tier) bucket the player holds. The
    rendered value is ``<species>:<tier>`` so action callbacks can split
    it back into the two pieces ``services.fishing`` expects.

    The select rebuilds on every refresh so a hatch / sell / gift in
    another channel doesn't leave stale options on the dropdown.
    """

    def __init__(self, parent: "_EggPickerView") -> None:
        try:
            from configs.buddies_config import (
                SPECIES as _SPECIES,
                rarity_meta as _b_rarity,
            )
        except Exception:
            _SPECIES = {}
            _b_rarity = lambda t: {"name": f"Tier {t}"}  # type: ignore

        opts: list[discord.SelectOption] = []
        by_st = parent.summary.get("by_species_tier") or {}
        for (species, tier), count in sorted(
            by_st.items(), key=lambda kv: (-kv[0][1], kv[0][0]),
        )[:25]:
            emoji = (
                str((_SPECIES.get(species) or {}).get("emoji") or "")
                or "\U0001F95A"
            )
            tier_name = str(_b_rarity(int(tier)).get("name") or f"T{tier}")
            label = f"{tier_name} {species.title()} Egg ×{count}"
            opts.append(discord.SelectOption(
                label=label[:100],
                value=f"{species}:{int(tier)}",
                emoji=emoji,
                default=(parent.selected == (species, int(tier))),
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no eggs held)", value="_none_", default=True,
            )]
        super().__init__(
            placeholder="Pick an egg species + rarity...",
            options=opts,
            min_values=1, max_values=1, row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        species, tier_s = v.split(":", 1)
        view: "_EggPickerView" = self.view  # type: ignore[assignment]
        view.selected = (species, int(tier_s))
        # Re-render so the option highlights the new selection.
        await view._redraw(interaction)


class _EggDepositQtyModal(discord.ui.Modal, title="Deposit Eggs to Banked"):
    """Modal counterpart of ``,buddy egg deposit`` driven from the picker.

    Operates on the currently-selected species so the player doesn't
    have to retype it. Popping then re-pushing leftover eggs mirrors
    the cog command so a partial deposit never silently drops eggs.
    """

    qty = discord.ui.TextInput(
        label="Quantity",
        placeholder="1, 5, all -- moves held -> banked",
        required=True, max_length=10, default="all",
    )

    def __init__(self, parent: "_EggPickerView") -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.parent.selected is None:
            await interaction.response.send_message(
                "No species selected.", ephemeral=True,
            )
            return
        species, _tier = self.parent.selected
        s = str(self.qty.value or "").strip().lower()
        if s in ("", "all", "max", "everything"):
            n = 10**6
        else:
            try:
                n = int(s)
                if n <= 0:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "Quantity must be a positive number or 'all'.",
                    ephemeral=True,
                )
                return
        from services import buddy_storage_eggs as bse
        popped = await fish_svc.pop_held_eggs(
            self.parent.ctx.db, self.parent.ctx.guild_id,
            self.parent.ctx.author.id, n=n, species=species,
        )
        if not popped:
            await interaction.response.send_message(
                f"You have no held {species} eggs to deposit.",
                ephemeral=True,
            )
            return
        accepted = await bse.deposit(
            self.parent.ctx.db, self.parent.ctx.guild_id,
            self.parent.ctx.author.id, popped, from_="deposit",
        )
        if accepted < len(popped):
            leftovers = popped[accepted:]
            await fish_svc.push_held_eggs(
                self.parent.ctx.db, self.parent.ctx.guild_id,
                self.parent.ctx.author.id, leftovers,
            )
        if accepted == 0:
            await interaction.response.send_message(
                "Banked egg storage is full -- buy a slot with "
                "`,buddy slot eggs buy` (50 rows per upgrade).",
                ephemeral=True,
            )
            await self.parent._redraw(interaction)
            return
        await interaction.response.send_message(
            embed=card(
                "\U0001F4E5 Eggs Deposited",
                description=(
                    f"Banked **{accepted}** {species.title()} egg"
                    f"{'s' if accepted != 1 else ''} into buddy storage."
                ),
                color=C_SUCCESS,
            ).build(),
            ephemeral=True,
        )
        await self.parent._redraw(interaction)


class _EggWithdrawQtyModal(discord.ui.Modal, title="Withdraw Eggs from Banked"):
    """Modal counterpart of ``,buddy egg withdraw`` driven from the picker.

    Held cap is enforced before pulling so we never withdraw eggs that
    would have nowhere to land.
    """

    qty = discord.ui.TextInput(
        label="Quantity",
        placeholder="1, 5 -- moves banked -> held (capped at 10 held)",
        required=True, max_length=10, default="1",
    )

    def __init__(self, parent: "_EggPickerView") -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        species: str | None = None
        if self.parent.selected is not None:
            species = self.parent.selected[0]
        s = str(self.qty.value or "").strip().lower()
        from configs.buddies_config import EGG_HELD_HARD_CAP
        if s in ("", "all", "max"):
            n = EGG_HELD_HARD_CAP
        else:
            try:
                n = int(s)
                if n <= 0:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "Quantity must be a positive number.", ephemeral=True,
                )
                return
        held = await fish_svc.list_held_eggs(
            self.parent.ctx.db, self.parent.ctx.guild_id,
            self.parent.ctx.author.id,
        )
        held_room = max(0, EGG_HELD_HARD_CAP - int(held.get("total") or 0))
        if held_room <= 0:
            await interaction.response.send_message(
                f"Held egg slot is full ({EGG_HELD_HARD_CAP}). "
                f"Hatch / sell some first.",
                ephemeral=True,
            )
            return
        n = min(n, held_room)
        from services import buddy_storage_eggs as bse
        pulled = await bse.withdraw(
            self.parent.ctx.db, self.parent.ctx.guild_id,
            self.parent.ctx.author.id, n=n, species=species,
        )
        if not pulled:
            target = f" {species}" if species else ""
            await interaction.response.send_message(
                f"No matching{target} eggs in banked storage.",
                ephemeral=True,
            )
            return
        await fish_svc.push_held_eggs(
            self.parent.ctx.db, self.parent.ctx.guild_id,
            self.parent.ctx.author.id, pulled,
        )
        label = species.title() if species else "egg"
        await interaction.response.send_message(
            embed=card(
                "\U0001F4E4 Eggs Withdrawn",
                description=(
                    f"Pulled **{len(pulled)}** {label}"
                    f"{'' if species else ''}"
                    f" from banked storage into held."
                ),
                color=C_SUCCESS,
            ).build(),
            ephemeral=True,
        )
        await self.parent._redraw(interaction)


class _EggSellQtyModal(discord.ui.Modal, title="Sell Eggs"):
    qty = discord.ui.TextInput(
        label="Quantity",
        placeholder="1, 5, all -- oldest of the selected species first",
        required=True, max_length=10,
    )

    def __init__(self, parent: "_EggPickerView") -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.parent.selected is None:
            await interaction.response.send_message(
                "No species selected.", ephemeral=True,
            )
            return
        species, _ = self.parent.selected
        s = str(self.qty.value or "").strip().lower()
        count: int | None
        if s in ("all", "everything"):
            count = None
        else:
            try:
                count = int(s)
                if count <= 0:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "Quantity must be a positive number or 'all'.",
                    ephemeral=True,
                )
                return
        try:
            res = await fish_svc.sell_held_eggs(
                self.parent.ctx.db, self.parent.ctx.guild_id,
                self.parent.ctx.author.id,
                species=species, count=count,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=card(
                "\U0001F4B0 Eggs Sold",
                description=(
                    f"Sold **{res.sold_count}** {species.title()} egg"
                    f"{'s' if res.sold_count != 1 else ''} for "
                    f"**{_fmt_lure(res.lure_paid)}**."
                ),
                color=C_SUCCESS,
            ).build(),
            ephemeral=True,
        )
        await self.parent._redraw(interaction)


class _EggGiftModal(discord.ui.Modal, title="Gift Eggs"):
    recipient = discord.ui.TextInput(
        label="Recipient (@mention or numeric user id)",
        placeholder="@user OR 1234567890",
        required=True, max_length=80,
    )
    qty = discord.ui.TextInput(
        label="Quantity",
        placeholder="1, 5, all -- oldest of the selected species first",
        required=True, max_length=10,
    )

    def __init__(self, parent: "_EggPickerView") -> None:
        super().__init__()
        self.parent = parent

    @staticmethod
    def _parse_user_id(raw: str) -> int | None:
        s = (raw or "").strip()
        if not s:
            return None
        if s.startswith("<@") and s.endswith(">"):
            s = s[2:-1].lstrip("!&")
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.parent.selected is None:
            await interaction.response.send_message(
                "No species selected.", ephemeral=True,
            )
            return
        species, _ = self.parent.selected
        target_id = self._parse_user_id(str(self.recipient.value))
        if not target_id or int(target_id) == int(interaction.user.id):
            await interaction.response.send_message(
                "Pass a valid recipient who isn't you.", ephemeral=True,
            )
            return
        s = str(self.qty.value or "").strip().lower()
        try:
            count = int(s) if s not in ("all", "everything") else 9999
            if count <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Quantity must be a positive number.", ephemeral=True,
            )
            return
        try:
            res = await fish_svc.gift_held_eggs(
                self.parent.ctx.db, self.parent.ctx.guild_id,
                int(interaction.user.id), int(target_id),
                species=species, count=int(count),
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=card(
                "\U0001F381 Eggs Gifted",
                description=(
                    f"Gifted **{res.gifted_count}** {species.title()} egg"
                    f"{'s' if res.gifted_count != 1 else ''} to <@{int(target_id)}>."
                ),
                color=C_SUCCESS,
            ).build(),
            ephemeral=True,
        )
        await self.parent._redraw(interaction)


class _EggAHListModal(discord.ui.Modal, title="List Egg on Auction House"):
    price = discord.ui.TextInput(
        label="Price",
        placeholder="e.g. 5  (BUD by default)",
        required=True, max_length=20,
    )
    currency = discord.ui.TextInput(
        label="Currency (optional)",
        placeholder="leave blank for BUD",
        required=False, max_length=10,
    )

    def __init__(self, parent: "_EggPickerView") -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.parent.selected is None:
            await interaction.response.send_message(
                "No species selected.", ephemeral=True,
            )
            return
        species, tier = self.parent.selected
        try:
            price_v = float(str(self.price.value).strip())
            if price_v <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Price must be a positive number.", ephemeral=True,
            )
            return
        cur = (str(self.currency.value or "").strip().upper() or None)
        # ref = "<species>:<tier>" so the AH locks one of THIS rarity --
        # the auction service's _lock_egg already handles that shape.
        ref = f"{species}:{int(tier)}"
        try:
            from services import auction as _auc
            listing_id, _tok, msg = await _auc.create_listing(
                self.parent.ctx.db,
                guild_id=self.parent.ctx.guild_id,
                seller_user_id=int(interaction.user.id),
                kind="egg",
                ref=ref,
                qty=1,
                price=price_v,
                currency=cur,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            log.exception("egg list failed species=%s tier=%s", species, tier)
            await interaction.response.send_message(
                f"Couldn't list: `{type(e).__name__}: {e}`.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=card(
                f"\U0001F3DB Listed -- #{int(listing_id)}",
                description=msg,
                color=C_SUCCESS,
            ).build(),
            ephemeral=True,
        )
        await self.parent._redraw(interaction)


class _EggPickerView(discord.ui.View):
    """Owner-locked, 5-min interactive egg picker.

    Row 0: species/tier dropdown.
    Row 1: Withdraw / Deposit / Buddies / Refresh -- mirrors the
           ``,buddy storage`` panel layout so the two surfaces feel like
           one panel with a pivot button. ``Buddies`` swaps the message
           back to the buddy storage view on the same chat slot.
    Row 2: Hatch / Sell / Gift / List on AH -- egg-specific actions.

    Hatch acts immediately (one egg of the selected species + tier).
    Sell + Gift + List open modals so the user can pass quantity / target /
    price without typing a long command. Withdraw / Deposit shuffle eggs
    between the held inventory and banked egg storage. The view
    re-renders the embed on every state-changing action so counts stay
    current.
    """

    def __init__(
        self, ctx: DiscoContext, summary: dict, *, lure_oracle: float = 0.0,
        fren_oracle: float = 0.0,
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.summary = summary
        self.lure_oracle = lure_oracle
        self.fren_oracle = fren_oracle
        self.selected: tuple[str, int] | None = None
        self.message: discord.Message | None = None
        # Pre-pick the highest-rarity bucket so action buttons aren't
        # dead on first open.
        by_st = summary.get("by_species_tier") or {}
        if by_st:
            top = sorted(by_st.keys(), key=lambda k: (-k[1], k[0]))[0]
            self.selected = (top[0], int(top[1]))
        self._rebuild_select()
        self._sync_button_state()

    def _rebuild_select(self) -> None:
        for child in list(self.children):
            if isinstance(child, _EggSpeciesSelect):
                self.remove_item(child)
        self.add_item(_EggSpeciesSelect(self))

    def _sync_button_state(self) -> None:
        has_eggs = int(self.summary.get("total") or 0) > 0
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.label in ("Hatch", "Sell", "Gift", "List on AH"):
                child.disabled = not has_eggs

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your egg panel. Run `,fish egg` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _redraw(self, interaction: discord.Interaction) -> None:
        # Renamed from ``_refresh`` to dodge the discord.ui.View framework
        # method of the same name -- shadowing it caused
        # ``RuntimeWarning: coroutine '_EggPickerView._refresh' was never
        # awaited`` on every interaction. The framework method is sync
        # and untouched now.
        new_summary = await fish_svc.list_held_eggs(
            self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
        )
        self.summary = new_summary
        # If the selection no longer exists, drop back to top bucket.
        by_st = new_summary.get("by_species_tier") or {}
        if self.selected and self.selected not in by_st:
            self.selected = None
            if by_st:
                top = sorted(by_st.keys(), key=lambda k: (-k[1], k[0]))[0]
                self.selected = (top[0], int(top[1]))
        self._rebuild_select()
        self._sync_button_state()
        embed = _egg_status_embed(
            self.ctx.author, new_summary,
            lure_oracle=self.lure_oracle,
            fren_oracle=self.fren_oracle,
        )
        if interaction.response.is_done():
            if self.message is not None:
                try:
                    await self.message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    log.debug("egg picker: panel edit failed", exc_info=True)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    # Row 1: navigation row, mirrors the ,buddy storage panel.

    @discord.ui.button(
        label="Withdraw", emoji="\U0001F4E4",
        style=discord.ButtonStyle.success, row=1,
    )
    async def btn_withdraw(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            _EggWithdrawQtyModal(self),
        )

    @discord.ui.button(
        label="Deposit", emoji="\U0001F4E5",
        style=discord.ButtonStyle.primary, row=1,
    )
    async def btn_deposit(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            _EggDepositQtyModal(self),
        )

    @discord.ui.button(
        label="Buddies", emoji="\U0001F436",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_buddies(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        # Pivot back to the ,buddy storage panel on the same chat slot.
        # Lazy import to dodge the cogs/buddy <- cogs/fishing circular.
        try:
            from cogs.buddy import _BuddyStorageView
        except Exception:
            await interaction.response.send_message(
                "Buddy storage view unavailable.", ephemeral=True,
            )
            return
        cog = self.ctx.bot.get_cog("Buddy") or self.ctx.bot.get_cog("CC Buddy")
        if cog is None:
            await interaction.response.send_message(
                "Buddy cog not loaded.", ephemeral=True,
            )
            return
        new_view = _BuddyStorageView(cog, self.ctx)
        embed = await new_view._build_embed()
        new_view.message = self.message
        try:
            await interaction.response.edit_message(embed=embed, view=new_view)
        except discord.HTTPException:
            log.debug("egg picker -> buddies swap failed", exc_info=True)
        self.stop()

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self._redraw(interaction)

    # Row 2: per-egg actions on the selected (species, tier) bucket.

    @discord.ui.button(
        label="Hatch", emoji="\U0001F95A",
        style=discord.ButtonStyle.success, row=2,
    )
    async def btn_hatch(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if self.selected is None:
            await interaction.response.send_message(
                "No species selected.", ephemeral=True,
            )
            return
        species, _tier = self.selected
        try:
            row = await fish_svc.hatch_held_egg(
                self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
                species=species,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        sp = str(row.get("species") or species)
        nm = str(row.get("name") or "?")
        tier_v = int(row.get("rarity_tier") or 1)
        try:
            from configs.buddies_config import SPECIES, rarity_meta as _b_rarity
            emoji = str((SPECIES.get(sp) or {}).get("emoji") or "\U0001F95A")
            tier_name = str(_b_rarity(tier_v).get("name") or "Common")
        except Exception:
            emoji, tier_name = "\U0001F95A", "Common"
        await interaction.response.send_message(
            embed=card(
                "\U0001F423 Egg Hatched",
                description=(
                    f"{emoji} **{nm}** the {tier_name} {sp} hatched!\n"
                    f"_Promote it from `,buddy` to set it active._"
                ),
                color=C_SUCCESS,
            ).build(),
            ephemeral=True,
        )
        await self._redraw(interaction)

    @discord.ui.button(
        label="Sell", emoji="\U0001F4B0",
        style=discord.ButtonStyle.primary, row=2,
    )
    async def btn_sell(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(_EggSellQtyModal(self))

    @discord.ui.button(
        label="Gift", emoji="\U0001F381",
        style=discord.ButtonStyle.secondary, row=2,
    )
    async def btn_gift(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(_EggGiftModal(self))

    @discord.ui.button(
        label="List on AH", emoji="\U0001F3DB",
        style=discord.ButtonStyle.secondary, row=2,
    )
    async def btn_list(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(_EggAHListModal(self))


# Discord caps embed field VALUE length at 1024 chars. With 12 zones at
# ~110 chars each the old single-field render hit ~1300 chars. Group by
# zone tier and chunk per tier so the layout scales as more zones land.
_ZONES_FIELD_BUDGET: int = 950   # 1024 minus a safety margin for headers


def _zones_embed(state: dict) -> discord.Embed:
    cur_tier = int(state.get("rod_tier") or 0)
    cur_zone = str(state.get("current_zone") or fc.DEFAULT_ZONE)
    rod_max_zone_tier = int(fc.rod_meta(cur_tier).get("max_zone_tier") or 0)

    # Group zones by their numeric tier so the panel reads as a
    # progression ladder ("Tier 1 (Shallows)" -> "Tier 5 (Deep)").
    by_tier: dict[int, list[tuple[str, dict]]] = {}
    for key, z in fc.ZONES.items():
        by_tier.setdefault(int(z.get("tier") or 1), []).append((key, z))

    builder = (
        card("\U0001F30A Fishing Zones", color=C_NAVY)
        .description(
            "Switch with `,fish zone <key>`. "
            "✅ = open  -  ⭐ = current  -  🔒 = need a stronger rod."
        )
    )

    # Build the per-tier field values, chunking if a tier's text would
    # exceed the 1024-char field cap. Splits at line boundaries so a
    # future tier with many zones still renders cleanly.
    for tier in sorted(by_tier.keys()):
        zone_entries = by_tier[tier]
        lines: list[str] = []
        for key, z in zone_entries:
            eligible = (
                cur_tier >= int(z.get("min_rod_tier") or 0)
                and rod_max_zone_tier >= int(z.get("tier") or 0)
            )
            marker = (
                " ⭐ *here*" if key == cur_zone
                else (" ✅" if eligible else " \U0001F512")
            )
            lines.append(
                f"{z['emoji']} **{z['name']}** (`{key}`)  -  "
                f"payout x{z['payout_mult']:.2f}{marker}\n"
                f"-# {z['blurb']}"
            )

        # Chunk lines so no single field value blows the 1024-char cap.
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for ln in lines:
            cost = len(ln) + (1 if current else 0)  # +1 for joining newline
            if current and current_len + cost > _ZONES_FIELD_BUDGET:
                chunks.append("\n".join(current))
                current = [ln]
                current_len = len(ln)
            else:
                current.append(ln)
                current_len += cost
        if current:
            chunks.append("\n".join(current))

        for i, chunk_value in enumerate(chunks):
            suffix = "" if len(chunks) == 1 else f" (part {i + 1})"
            builder = builder.field(
                f"Tier {tier}{suffix}", chunk_value, False,
            )

    return builder.build()


def _chunk_lines_to_fields(
    lines: list[str], header: str, cap: int = 1000,
) -> list[tuple[str, str]]:
    """Split ``lines`` into ``(name, value)`` field tuples each under ``cap``
    chars. Subsequent fields get a "(cont.)" suffix so the section reads as
    one logical block. Empty input returns a single empty-marker field so
    the caller can drop it straight into the embed without conditional logic.
    """
    if not lines:
        return [(header, "_(empty)_")]
    out: list[tuple[str, str]] = []
    chunk: list[str] = []
    chunk_len = 0
    part = 0
    for ln in lines:
        line_len = len(ln) + 1
        if chunk and chunk_len + line_len > cap:
            part += 1
            label = header + (" (cont.)" if part > 1 else "")
            out.append((label, "\n".join(chunk)))
            chunk = []
            chunk_len = 0
        chunk.append(ln)
        chunk_len += line_len
    if chunk:
        part += 1
        label = header + (" (cont.)" if part > 1 else "")
        out.append((label, "\n".join(chunk)))
    return out


def _inventory_embed(
    member: discord.Member, summary: dict, *, lure_oracle: float = 0.0,
) -> discord.Embed | list[discord.Embed]:
    """Render the fishing inventory.

    Returns a single embed when everything fits, or a list of paginated
    embeds when a player has enough fish/junk to overflow Discord's
    1024-char-per-field or 6000-char-per-embed limits. The cog wrapper
    routes the list through ``ctx.paginate`` instead of ``send_embed``.
    """
    fish_lines = []
    for f in summary["fish"]:
        rarity = fc.rarity_meta(f["rarity"]).get("label", "Common")
        fish_lines.append(
            f"{f['emoji']} **{f['name']}** ({rarity})  -  "
            f"x{f['count']}  -  {f['total_lbs']:,.2f} lbs total "
            f"(biggest **{f['biggest_lbs']:,.2f} lbs**)"
        )
    # Junk rows quote per-unit salvage in LURE plus USD at the live
    # LURE oracle so players can decide whether to sell now or hoard.
    junk_lines = [
        f"{j['emoji']} **{j['name']}** x{j['count']}  -  "
        f"{_fmt_lure(j['salvage_each'])}"
        f"{_with_usd(j['salvage_each'], lure_oracle)} ea"
        for j in summary["junk"]
    ]
    bait_lines = [
        f"{b['emoji']} **{b['name']}** x{b['count']}"
        for b in summary["bait"]
    ]
    trap_lines = [
        f"{t['emoji']} **{t['name']}** x{t['count']}"
        for t in summary.get("traps", [])
    ]

    fields: list[tuple[str, str]] = []
    fields.extend(_chunk_lines_to_fields(fish_lines, f"Fish ({summary['fish_total']})"))
    fields.extend(_chunk_lines_to_fields(junk_lines, f"Junk ({summary['junk_total']})"))
    fields.extend(_chunk_lines_to_fields(bait_lines, "Bait"))
    fields.extend(_chunk_lines_to_fields(trap_lines, "Crab Traps (undeployed)"))

    footer_text = (
        "Sell with `,fish sell` (all), `,fish sell junk`, or "
        "`,fish sell <fish_key>`. Place traps with "
        "`,fish trap place <key>`."
    )
    title = f"\U0001F4E6 {member.display_name}'s Catch"

    # Greedy-pack fields across embeds. Discord caps an embed at 6000
    # chars total and 25 fields; we budget 5000 to leave room for the
    # title + footer + per-field name overhead.
    EMBED_BODY_BUDGET = 5000
    EMBED_FIELDS_MAX = 24

    pages_payload: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    cur_len = 0
    for nm, val in fields:
        entry_len = len(nm) + len(val) + 8
        if cur and (
            cur_len + entry_len > EMBED_BODY_BUDGET
            or len(cur) >= EMBED_FIELDS_MAX
        ):
            pages_payload.append(cur)
            cur = []
            cur_len = 0
        cur.append((nm, val))
        cur_len += entry_len
    if cur:
        pages_payload.append(cur)
    if not pages_payload:
        pages_payload = [[]]

    page_total = len(pages_payload)
    embeds: list[discord.Embed] = []
    for idx, chunk in enumerate(pages_payload):
        page_title = title
        if page_total > 1:
            page_title = f"{title}  ({idx + 1}/{page_total})"
        eb = card(page_title, color=C_TEAL)
        for nm, val in chunk:
            eb.field(nm, val, False)
        eb.footer(footer_text)
        embeds.append(eb.build())

    return embeds[0] if len(embeds) == 1 else embeds


def _history_embed(
    member: discord.Member, rows: list[dict], *, lure_oracle: float = 0.0,
) -> discord.Embed:
    if not rows:
        return card("\U0001F4DC Recent Catches",
                    description="_(no catches yet)_", color=C_NAVY).build()
    lines = []
    for r in rows:
        ts = fmt_ts(r.get("caught_at"))
        outcome = r.get("outcome", "?")
        if outcome == "fish":
            meta = fc.fish_meta(r.get("fish_key") or "") or {}
            rarity = fc.rarity_meta(r.get("rarity") or "common").get("label", "Common")
            lines.append(
                f"`{ts}` {meta.get('emoji', '')} **{meta.get('name', r.get('fish_key', '?'))}**"
                f" ({rarity}) -- {float(r.get('weight_lbs') or 0):,.2f} lbs"
            )
        elif outcome == "junk":
            meta = fc.junk_meta(r.get("junk_key") or "") or {}
            lines.append(
                f"`{ts}` {meta.get('emoji', '')} {meta.get('name', r.get('junk_key', '?'))}"
            )
        else:
            # Pre-cutover rows are tagged 'USD'; post-cutover rows 'LURE'.
            # Both flavours render with a USD companion at the live LURE
            # oracle so the column reads consistently across the cutover.
            sym = str(r.get("payout_symbol") or "LURE").upper()
            human = to_human(int(r.get("payout_lure_raw") or 0))
            if sym == "USD":
                payout_str = fmt_usd(human)
            else:
                payout_str = f"{_fmt_lure(human)}{_with_usd(human, lure_oracle)}"
            label = outcome.replace("_", " ").title()
            lines.append(f"`{ts}` \U0001F381 **{label}**  -  {payout_str}")
    return (
        card(f"\U0001F4DC {member.display_name}'s Recent Catches", color=C_NAVY)
        .description("\n".join(lines))
        .build()
    )


def _leaderboard_embed(rows: list[dict], guild: discord.Guild,
                       *, kind: str = "payout",
                       lure_oracle: float = 0.0) -> discord.Embed:
    if not rows:
        return card("\U0001F3C6 Top Fishers",
                    description="_(nobody has fished yet)_", color=C_GOLD).build()

    if kind == "biggest":
        title = "\U0001F3C6 Biggest Catches"
        lines = []
        for i, r in enumerate(rows, 1):
            uid = int(r.get("user_id") or 0)
            meta = fc.fish_meta(r.get("fish_key") or "") or {}
            label = (meta.get("name") or r.get("fish_key") or "?").title()
            rarity = fc.rarity_meta(r.get("rarity") or "common").get("label", "Common")
            lines.append(
                f"`#{i}` {meta.get('emoji', '')} **{label}** ({rarity})  -  "
                f"**{float(r.get('weight_lbs') or 0):,.2f} lbs**  -  "
                f"{mention(uid, guild=guild)}"
            )
        return (
            card(title, color=C_GOLD)
            .description("\n".join(lines))
            .footer("Use `,fish lb` for the lifetime-payout board.")
            .build()
        )

    title = "\U0001F3C6 Top Fishers"
    lines = []
    for i, r in enumerate(rows, 1):
        uid = int(r.get("user_id") or 0)
        lure_h = to_human(int(r.get("total_lure_earned_raw") or 0))
        lure_str = f"{_fmt_lure(lure_h)}{_with_usd(lure_h, lure_oracle)}"
        caught = int(r.get("total_caught") or 0)
        biggest = float(r.get("biggest_lbs") or 0)
        lvl = int(r.get("fish_level") or 1)
        lines.append(
            f"`#{i}` Lv. {lvl}  -  {lure_str}  -  **{caught:,}** caught  -  "
            f"biggest {biggest:,.2f} lbs  -  {mention(uid, guild=guild)}"
        )
    return (
        card(title, color=C_GOLD)
        .description("\n".join(lines))
        .footer("Use `,fish lb biggest` for trophy weights.")
        .build()
    )


# === HELPERS_END ===

# === VIEW_START ===
# ============================================================================
# Cast view: animated message + HOOK button
# ============================================================================
# The view drives a single edited message through the cast cycle.
#   1. cast / fly_1 / fly_2          (~1.4s, no buttons)
#   2. wait_1 / wait_2 (random 1-3 cycles, ~1s each)
#   3. nibble (0.6s)
#   4. bite + HOOK button (HOOK_WINDOW_S to react)
#   5. reel_1 / reel_2 (1s each)
#   6. final result embed (no view)
#
# The view is cancellable: a second cast attempt aborts the first, and
# any unhandled timeout falls through to a "the fish got away" miss.

class CastView(discord.ui.View):
    """One-shot interactive cast. Owner-locked to the caster."""

    def __init__(
        self, ctx: DiscoContext, cog: "Fishing", state: dict,
    ) -> None:
        # We don't use discord.py's view timeout for the bite window
        # because we need a tighter custom timer; set a generous outer
        # timeout so the view eventually goes away if the message is
        # never edited (e.g. permission revoked mid-cast).
        super().__init__(timeout=fc.SESSION_TIMEOUT_S * 2)
        self.ctx = ctx
        self.cog = cog
        # The pre-cast user_fishing snapshot. Used to render the
        # context footer ("Combo xN | Lv.N | Rod | Bait | Zone") on
        # every animation frame without a per-frame DB hit.
        self.state = state
        self.message: discord.Message | None = None
        self._bite_at: float = 0.0     # monotonic time the bite frame went live
        self._hooked: asyncio.Event = asyncio.Event()
        self._reaction_s: float | None = None
        self._aborted: bool = False
        # Per-cast computed hook window. Stamped at bite time via
        # fc.compute_hook_window so the deadline scales with rod /
        # level / zone / random rather than the legacy flat 3.0s.
        self._hook_window_s: float = float(fc.HOOK_WINDOW_S)

        # Secondary-action mid-cast prompt. Fires on a per-cast roll
        # against fc.SECONDARY_TRIGGER_CHANCE; if it fires and the
        # player misses, the catch fails. If it fires and they hit,
        # they earn a bonus on top of the hook timing.
        self._secondary_required: bool = False
        self._secondary_hit: bool = False
        self._secondary_at: float = 0.0
        self._secondary_done: asyncio.Event = asyncio.Event()

        # Pre-build the HOOK button so we can enable / disable it at
        # the right frame transitions.
        self._hook_btn = discord.ui.Button(
            label="HOOK!",
            emoji="\U0001FA9D",   # hook
            style=discord.ButtonStyle.success,
            disabled=True,
            row=0,
        )
        self._hook_btn.callback = self._on_hook
        self.add_item(self._hook_btn)
        # Secondary action button -- always present but only enabled
        # during the secondary prompt window. Sits on row 0 next to HOOK
        # so the player's eye doesn't have to jump rows mid-cast.
        self._reel_btn = discord.ui.Button(
            label="REEL!",
            emoji="\U0001F300",   # cyclone (reel motion)
            style=discord.ButtonStyle.primary,
            disabled=True,
            row=0,
        )
        self._reel_btn.callback = self._on_reel
        self.add_item(self._reel_btn)

    # ── Interaction guard ─────────────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your line.", ephemeral=True,
            )
            return False
        return True

    # ── Public driver ─────────────────────────────────────────────────────

    async def run(self) -> fish_svc.CastResult:
        """Step through the cast animation and return the resolved result.

        Animation flow (every frame carries a random hint from
        ``HINT_POOLS`` and a context footer with combo/level/gear/zone):

            cast -> fly_1 -> fly_2     -- the throw (3 fixed beats)
            wait_1/2/3 (1-3 cycles)    -- bobbing water, randomised count
            (30% interlude)            -- peek (shadow tease) or false_alarm
            nibble                     -- something brushes the line
            tug                        -- line pulls hard (NEW intermediate)
            bite                       -- HOOK button window
            (resolve service call)     -- early so reel frames can vary
            reel sequence              -- outcome-aware:
                                          fish-heavy -> reel_heavy + reel_jump + splash_in
                                          fish-light -> reel_light + splash_in
                                          junk       -> reel_1
                                          bonus      -> reel_2 (sparkle)
                                          miss       -> straight to result

        The DB writes happen inside ``cast_resolve``; this method just
        owns the visual flow + reaction-time measurement + outcome-aware
        frame selection.
        """
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        try:
            # Fly-out frames (no input).
            await self._edit("cast", title="\U0001F3A3 Casting...",
                             color=C_INFO)
            await asyncio.sleep(0.7)
            await self._edit("fly_1", title="\U0001F3A3 Casting...",
                             color=C_INFO)
            await asyncio.sleep(0.7)
            await self._edit("fly_2", title="\U0001F3A3 Casting...",
                             color=C_INFO)
            await asyncio.sleep(0.7)

            # Idle / wait frames. Randomise cycles + key so two casts
            # never feel identical. 30% chance per cast to insert a
            # "peek" (shadow tease) or "false_alarm" interlude that
            # adds a beat of suspense without changing the outcome.
            import random as _r
            cycles = _r.randint(1, 3)
            wait_keys = ("wait_1", "wait_2", "wait_3")
            for i in range(cycles):
                key = wait_keys[i % len(wait_keys)]
                await self._edit(key, title="\U0001F3A3 Waiting...",
                                 color=C_NAVY)
                await asyncio.sleep(_r.uniform(0.9, 1.3))
            if _r.random() < 0.30:
                interlude = _r.choice(("peek", "false_alarm"))
                title = ("\U0001F441 A Shadow!" if interlude == "peek"
                         else "\U0001F914 False Alarm")
                color = C_AMBER if interlude == "peek" else C_NEUTRAL
                await self._edit(interlude, title=title, color=color)
                await asyncio.sleep(0.9)

            # Nibble -> tug. The new tug frame builds a beat of "this
            # is going to be BIG" tension before the bite window opens.
            await self._edit("nibble", title="\U0001F914 Nibble!",
                             color=C_AMBER)
            await asyncio.sleep(0.55)
            await self._edit("tug", title="\U0001F525 Tug!",
                             color=C_AMBER)
            await asyncio.sleep(0.45)

            if self._aborted:
                raise asyncio.CancelledError()

            # Per-cast hook window: scales with rod tier / fishing
            # level / zone tier / random jitter (rarity hint left at
            # the default 1 -- the bite outcome isn't pre-rolled here,
            # so we can't tighten the window for legendary fish at
            # this stage; that's a follow-up if/when cast_resolve
            # gets split into a pre-roll + commit pair).
            try:
                rod_t = int(self.state.get("rod_tier") or 0)
                lvl = int(self.state.get("fish_level") or 1)
                zone_md = fc.zone_meta(
                    str(self.state.get("current_zone") or fc.DEFAULT_ZONE),
                ) or {}
                zt = int(zone_md.get("tier") or 1)
                self._hook_window_s = float(
                    fc.compute_hook_window(rod_t, lvl, zt)
                )
            except Exception:
                self._hook_window_s = float(fc.HOOK_WINDOW_S)

            # Optional secondary action (REEL!) before the bite window.
            # Per fc.SECONDARY_TRIGGER_CHANCE; if it fires and the
            # player misses, the catch fails downstream in cast_resolve
            # via the secondary_required + secondary_hit flags.
            self._secondary_required = (
                random.random() < fc.SECONDARY_TRIGGER_CHANCE
            )
            if self._secondary_required:
                self._reel_btn.disabled = False
                await self._edit(
                    "tug",
                    title="\U0001F300 PULL!",
                    color=C_INFO,
                    hint=(
                        f"**REEL** within **{fc.SECONDARY_WINDOW_S:.0f}s** "
                        f"or you lose the catch!"
                    ),
                )
                self._secondary_at = time.monotonic()
                try:
                    await asyncio.wait_for(
                        self._secondary_done.wait(),
                        timeout=fc.SECONDARY_WINDOW_S,
                    )
                except asyncio.TimeoutError:
                    self._secondary_hit = False
                self._reel_btn.disabled = True
                try:
                    if self.message:
                        await self.message.edit(view=self)
                except Exception:
                    pass

            # Bite -- the main input window. Hint shows the deadline
            # so the user knows how long they have. _bite_at is
            # stamped AFTER the edit round-trip completes so
            # reaction_seconds measures true player latency, not
            # Discord network lag. The edit enables the button;
            # asyncio's cooperative scheduler means _on_hook cannot
            # fire before we reach the next await (wait_for), so the
            # stamp is safe.
            self._hook_btn.disabled = False
            await self._edit(
                "bite",
                title="\U000026A1 STRIKE!",
                color=C_GOLD,
                hint=(
                    f"**HOOK NOW** -- within "
                    f"**{self._hook_window_s:.1f}s**! "
                    f"({fc.random_hint('bite')})"
                ),
            )
            self._bite_at = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._hooked.wait(), timeout=self._hook_window_s,
                )
            except asyncio.TimeoutError:
                self._reaction_s = None  # missed

            self._hook_btn.disabled = True
            try:
                if self.message:
                    await self.message.edit(view=self)
            except Exception:
                pass

            # EARLY resolve so the reel-in frames can be chosen based on
            # what's actually about to come up. The DB write is the
            # source of truth; the visual just narrates the outcome.
            result = await fish_svc.cast_resolve(
                self.ctx.db, gid, uid,
                reaction_seconds=self._reaction_s,
                hook_window_s=self._hook_window_s,
                secondary_required=self._secondary_required,
                secondary_hit=self._secondary_hit,
            )

            # Outcome-aware reel sequence. Miss skips reeling entirely.
            await self._reel_sequence(result)
            return result

        except asyncio.CancelledError:
            # Surface as a miss so the cog still runs the post-step
            # (release lock, fire events, etc.).
            return await fish_svc.cast_resolve(
                self.ctx.db, gid, uid, reaction_seconds=None,
                hook_window_s=self._hook_window_s,
                secondary_required=self._secondary_required,
                secondary_hit=self._secondary_hit,
            )

    async def _reel_sequence(self, result: fish_svc.CastResult) -> None:
        """Pick reel-in frames based on the resolved outcome.

        Heavy fish + bonus + wild battle each get their own visual
        beat before the result embed lands. Junk gets a single quick
        reel frame (you don't celebrate pulling up a soggy boot).
        Misses skip reeling entirely.
        """
        outcome = result.outcome
        if outcome == "miss":
            return
        if outcome == "fish":
            # Heavy / light split: legendary + epic + rare = HEAVY,
            # everything else = LIGHT. Heavy gets the extra jump frame.
            heavy_rarities = ("legendary", "epic", "rare")
            is_heavy = (result.rarity or "common") in heavy_rarities
            if is_heavy:
                await self._edit(
                    "reel_heavy", title="\U0001F501 Reeling...",
                    color=C_AMBER,
                )
                await asyncio.sleep(0.75)
                await self._edit(
                    "reel_jump", title="\U000026A1 It's a BIG one!",
                    color=C_GOLD,
                )
                await asyncio.sleep(0.7)
                await self._edit(
                    "splash_in", title="\U0001F30A SPLASH!",
                    color=C_TEAL,
                )
                await asyncio.sleep(0.6)
            else:
                await self._edit(
                    "reel_light", title="\U0001F501 Reeling...",
                    color=C_TEAL,
                )
                await asyncio.sleep(0.6)
                await self._edit(
                    "splash_in", title="\U0001F30A Surface!",
                    color=C_TEAL,
                )
                await asyncio.sleep(0.5)
            return
        if outcome == "junk":
            await self._edit(
                "reel_1", title="\U0001F501 Reeling...",
                color=C_NEUTRAL,
            )
            await asyncio.sleep(0.6)
            return
        if outcome in ("money_bag", "mystery_box", "buddy_egg"):
            await self._edit(
                "reel_2", title="\U0001F501 Reeling...",
                color=C_GOLD,
            )
            await asyncio.sleep(0.6)
            return
        if outcome == "wild_battle":
            # Wild battle reveal lands after this -- a heavy reel sets
            # the tone for the Challenge prompt the cog attaches.
            await self._edit(
                "reel_heavy", title="\U0001F525 Something Fights Back!",
                color=C_CRIMSON,
            )
            await asyncio.sleep(0.7)
            return
        # Unknown outcome (defensive) -- single neutral reel beat.
        await self._edit(
            "reel_1", title="\U0001F501 Reeling...",
            color=C_TEAL,
        )
        await asyncio.sleep(0.5)

    # ── Internal: HOOK button callback ────────────────────────────────────

    async def _on_hook(self, interaction: discord.Interaction) -> None:
        if self._hooked.is_set() or self._hook_btn.disabled:
            await interaction.response.defer()
            return
        self._reaction_s = time.monotonic() - self._bite_at
        self._hooked.set()
        # Acknowledge so Discord doesn't show "interaction failed".
        await interaction.response.defer()

    async def _on_reel(self, interaction: discord.Interaction) -> None:
        """Secondary-action callback. Stamps ``_secondary_hit=True`` if
        clicked inside the secondary window. The bite phase reads the
        flag after the prompt window closes.
        """
        if self._secondary_done.is_set() or self._reel_btn.disabled:
            await interaction.response.defer()
            return
        # Hit only counts if the click landed within the configured
        # window from the moment the prompt went live; defensive check
        # in case the timeout-vs-callback race shipped a late event.
        elapsed = time.monotonic() - self._secondary_at
        if elapsed <= float(fc.SECONDARY_WINDOW_S):
            self._secondary_hit = True
        self._secondary_done.set()
        await interaction.response.defer()

    # ── Internal: edit the carrier message ────────────────────────────────

    async def _edit(self, frame_key: str, *, title: str, color: int,
                    hint: str = "", footer: str | None = None) -> None:
        # Default hint pulls from the random pool for ``frame_key``;
        # callers can pass an explicit hint to override (e.g. the bite
        # frame appends the deadline to the random pool pick).
        if not hint:
            hint = fc.random_hint(frame_key)
        # Default footer is always the current player context so the
        # caster keeps seeing combo / level / gear / zone through every
        # frame of the animation.
        if footer is None:
            footer = _cast_context_footer(self.state)
        embed = _frame_embed(frame_key, title=title, color=color,
                             hint=hint, footer=footer)
        if not self.message:
            return
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException as exc:
            log.debug("CastView: edit failed (%s)", exc)

    async def on_timeout(self) -> None:
        self._aborted = True
        self._hooked.set()
        for item in self.children:
            try:
                item.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass


# === VIEW_END ===

# === COG_START ===
# ============================================================================
# Wild-buddy battle view
# ============================================================================
# Single-button view attached to the cast result message when a wild
# encounter rolls. Mirrors the escaped-buddy pattern in cogs/buddy.py
# (_EscapedBuddyView) so the UX feels uniform: one ⚔️ button, the cast
# owner is the only person who can fight, timeout means the wild buddy
# slips away with no penalty (and no counter bump -- we only count
# fights the player actually took).

# Wild-battle log layout. Discord caps embed FIELD values at 1024 chars
# and the entire embed at 6000. The previous render shoved the log into
# the description with a hard 900-char truncate, which routinely cut
# fights mid-round. Now we chunk into proper Battle Log fields and
# only trim the tail when a fight is genuinely enormous, with an
# explicit "(N more lines trimmed)" footer so the user knows.
_BATTLE_LOG_FIELD_BUDGET: int = 1020   # 1024 minus minimal header overhead
_BATTLE_LOG_MAX_FIELDS: int = 5        # ~5,000 chars of log space total


def _split_battle_log(raw_lines: list[str]) -> list[str]:
    """Split a battle-log line list into per-field text chunks.

    Returns a list of strings, each <= ``_BATTLE_LOG_FIELD_BUDGET``
    characters, split at line boundaries so a round header is never
    cut in half. If the log requires more than ``_BATTLE_LOG_MAX_FIELDS``
    chunks, the tail gets trimmed and the last chunk gets a
    ``...(N more lines trimmed)`` footer so the user knows we cut.
    """
    # Strip the engine's intro preamble (fighter stat lines, abilities,
    # leading blank) the same way cogs/buddy.py does -- the structured
    # fields above already cover that information.
    log_start = 0
    for i, line in enumerate(raw_lines):
        if line.startswith("__**Round "):
            log_start = i
            break
    body = raw_lines[log_start:]

    # Strip the engine's trailing "**Winner:** ... earns N XP and $Y."
    # / "**Draw.**" line. Two reasons:
    #   1. The "$Y" is the buddy-battle engine's default USD reward,
    #      which contradicts the actual LURE + REEL reward already
    #      rendered above the log on the wild-battle embed.
    #   2. The line was getting orphaned into a "Battle Log (cont. 2)"
    #      field by itself when the rest of the rounds maxed out the
    #      first chunk -- looked disjointed.
    while body and (body[-1].startswith("**Winner:**")
                    or body[-1].startswith("**Draw.**")
                    or not body[-1].strip()):
        body = body[:-1]

    if not body:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for ln in body:
        cost = len(ln) + (1 if current else 0)  # +1 for the newline join
        if current and current_len + cost > _BATTLE_LOG_FIELD_BUDGET:
            chunks.append("\n".join(current))
            if len(chunks) >= _BATTLE_LOG_MAX_FIELDS:
                # Cap reached. The current line + remaining tail get
                # accounted for by the trim footer below.
                current = []
                current_len = 0
                break
            current = [ln]
            current_len = len(ln)
        else:
            current.append(ln)
            current_len += cost
    if current and len(chunks) < _BATTLE_LOG_MAX_FIELDS:
        chunks.append("\n".join(current))

    # If we hit the cap, count how many lines we truncated and append
    # a footer to the last chunk so the user sees the trim explicitly.
    if len(chunks) >= _BATTLE_LOG_MAX_FIELDS:
        rendered = sum(c.count("\n") + 1 for c in chunks)
        remaining = max(0, len(body) - rendered)
        if remaining > 0:
            footer = f"\n...*({remaining} more line(s) trimmed)*"
            last = chunks[-1]
            # Make room for the footer by dropping tail lines from the
            # last chunk if it'd overflow the field budget otherwise.
            while len(last) + len(footer) > _BATTLE_LOG_FIELD_BUDGET and "\n" in last:
                last = last.rsplit("\n", 1)[0]
                remaining += 1
                footer = f"\n...*({remaining} more line(s) trimmed)*"
            chunks[-1] = last + footer
    return chunks


# ============================================================================
# Interactive wild-battle combat
# ============================================================================
# Wild battles layer an interactive turn-based view on top of the auto-run
# engine in services/buddy_battle.py. The combat math (LiveBattle dataclass,
# action resolution, AI policy, performance bonus) lives in that shared
# module so cogs/dungeon.py and cogs/buddy.py can use the same engine for
# delve wild battles + buddy arenas.

from services.buddy_battle import (
    INTERACTIVE_BATTLE_MAX_ROUNDS as _BATTLE_MAX_ROUNDS,
    INTERACTIVE_PLAYER_STAMINA_MAX as _ACT_PLAYER_STAMINA_MAX,
    INTERACTIVE_SPECIAL_STAMINA_COST as _ACT_SPECIAL_STAMINA_COST,
    LiveBattle as _LiveBattleShared,
    apply_player_action as _shared_apply_player_action,
    compute_battle_bonus as _shared_compute_battle_bonus,
    enemy_ai_turn as _shared_enemy_ai_turn,
    hp_bar as _shared_hp_bar,
)

# Loot-drop chance per win. ~1-in-20 wins drops a bonus item.
_LOOT_DROP_CHANCE: float                = 0.05
_LOOT_DROP_KINDS: tuple[tuple[str, int], ...] = (
    ("treasure_map", 40),
    ("magic_bait",   25),
    ("chum_bait",    20),
    ("wild_egg",     15),
)


# Local thin aliases keep the rest of this file's call-sites unchanged.
_LiveBattle = _LiveBattleShared
_apply_player_action = _shared_apply_player_action
_enemy_ai_turn = _shared_enemy_ai_turn
_compute_battle_bonus = _shared_compute_battle_bonus
_hp_bar = _shared_hp_bar


def _roll_loot_drop() -> dict | None:
    """Roll the bonus loot drop for a wild-battle win. ``None`` most calls."""
    import random as _r
    if _r.random() >= _LOOT_DROP_CHANCE:
        return None
    keys = [k for k, _ in _LOOT_DROP_KINDS]
    weights = [w for _, w in _LOOT_DROP_KINDS]
    kind = _r.choices(keys, weights=weights, k=1)[0]
    qty = 1
    if kind in ("magic_bait", "chum_bait"):
        qty = _r.randint(3, 7)
    return {"kind": kind, "qty": qty}


class _CastResultBaitSelect(discord.ui.Select):
    """Bait equip dropdown attached directly to the cast-result panel
    so the player can swap bait without opening ``,fish stats`` first.

    Lighter than ``_BaitEquipSelect`` because there's no parent panel
    to re-render -- success just sends an ephemeral confirmation.
    """

    def __init__(self, owner_id: int, ctx: DiscoContext) -> None:
        self.owner_id = int(owner_id)
        self.ctx = ctx
        # Build options async-deferred -- discord.ui.Select wants
        # options at construction time, so we ship a placeholder
        # "loading" state and let the constructor be re-run when
        # the cast-result view is rebuilt.
        super().__init__(
            placeholder="Loading bait...",
            options=[discord.SelectOption(
                label="(loading)", value="__loading__", default=True,
            )],
            min_values=1, max_values=1,
            row=1,
            disabled=True,
        )

    @classmethod
    async def build(
        cls, owner_id: int, ctx: DiscoContext,
    ) -> "_CastResultBaitSelect":
        """Async factory that hydrates the dropdown from current state."""
        sel = cls(owner_id, ctx)
        try:
            state = await fish_svc.ensure_state(
                ctx.db, ctx.guild_id, owner_id,
            )
        except Exception:
            return sel
        equipped = state.get("equipped_bait")
        inv = _jsonb_dict(state.get("bait_inventory")) if state else {}
        opts: list[discord.SelectOption] = [
            discord.SelectOption(
                label="(unequip)",
                value="__none__",
                emoji="\U0001F6AB",
                default=(not equipped),
            )
        ]
        for key, count in sorted(
            inv.items(), key=lambda kv: -int(kv[1] or 0),
        ):
            try:
                cnt = int(count or 0)
            except (TypeError, ValueError):
                cnt = 0
            if cnt <= 0:
                continue
            meta = fc.bait_meta(key) or {}
            label = f"{meta.get('name', key)} (x{cnt})"[:100]
            opts.append(discord.SelectOption(
                label=label,
                value=str(key),
                emoji=str(meta.get("emoji") or "")[:1] or None,
                default=(equipped == key),
            ))
        if len(opts) == 1:
            opts.append(discord.SelectOption(
                label="(no bait owned -- visit ,fish shop)",
                value="__empty__",
                default=False,
            ))
        sel.options = opts[:25]
        sel.placeholder = "Equip bait..."
        sel.disabled = False
        return sel

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This isn't your line.", ephemeral=True,
            )
            return
        choice = self.values[0]
        if choice == "__empty__" or choice == "__loading__":
            await interaction.response.send_message(
                "Buy bait via `,fish shop` first.", ephemeral=True,
            )
            return
        target = None if choice == "__none__" else choice
        try:
            await fish_svc.set_bait(
                self.ctx.db, self.ctx.guild_id,
                interaction.user.id, target,
            )
        except ValueError as e:
            await interaction.response.send_message(
                str(e), ephemeral=True,
            )
            return
        if target:
            meta = fc.bait_meta(target) or {}
            await interaction.response.send_message(
                f"{meta.get('emoji', '')} Equipped **"
                f"{meta.get('name', target)}**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Bait removed.", ephemeral=True,
            )


class _CastResultView(discord.ui.View):
    """Persistent post-cast panel: Cast Again + Bump.

    Attached to the cast result embed so the panel never silently
    disappears (no autodelete) and the player can either re-run the
    cast in place or bump the embed to the bottom of the channel
    without having to type ,fish again. Owner-locked.
    """

    def __init__(self, cog: "Fishing", ctx: DiscoContext) -> None:
        # No timeout: the user explicitly asked these embeds to stay
        # interactive. They can still time out via Discord's hard 15-min
        # ack window on individual button clicks if abandoned, but the
        # view itself doesn't go grey.
        super().__init__(timeout=None)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.message: discord.Message | None = None

        from core.framework.persistent_embeds import (
            BumpButton as _BumpButton,
            CallbackButton as _CallbackButton,
        )

        async def _on_cast_again(interaction: discord.Interaction) -> None:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            # Re-run the cast on the SAME message the result landed on
            # so the player gets one continuously-updating panel
            # instead of a new chat reply every time they tap.
            try:
                await self.cog._cmd_cast(
                    self.ctx, existing_message=interaction.message,
                )
            except Exception:
                log.debug("fishing: cast-again failed", exc_info=True)

        # Cast Again is the post-cast restart action; HOOK! during the
        # bite window uses success/green (the urgent strike). Keeping
        # Cast Again on primary/blue makes the two trivially
        # distinguishable when both surfaces appear in close succession.
        self.add_item(_CallbackButton(
            self.owner_id,
            _on_cast_again,
            label="Cast Again",
            emoji="\U0001F3A3",
            style=discord.ButtonStyle.primary,
            row=0,
        ))

        async def _on_open_panel(interaction: discord.Interaction) -> None:
            # Pop the player's tackle box panel as a follow-up so they
            # can swap bait or check stats without leaving the channel.
            try:
                state = await fish_svc.ensure_state(
                    self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
                )
                lure_balance = to_human(
                    await fish_svc.get_lure_wallet_raw(
                        self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
                    )
                )
                reel_balance = to_human(
                    await fish_svc.get_reel_wallet_raw(
                        self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
                    )
                )
                lure_staked = to_human(int(state.get("lure_staked_raw") or 0))
                pending_reel = to_human(
                    await fish_svc.accrued_stake_yield(
                        self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
                    )
                )
                lp_row = await self.ctx.db.get_price(
                    fc.LURE_SYMBOL, self.ctx.guild_id,
                )
                rp_row = await self.ctx.db.get_price(
                    fc.REEL_SYMBOL, self.ctx.guild_id,
                )
                lure_oracle = float(lp_row["price"]) if lp_row else 0.0
                reel_oracle = float(rp_row["price"]) if rp_row else 0.0
                embed = _stats_embed(
                    dict(state), member=self.ctx.author,
                    lure_balance=lure_balance, reel_balance=reel_balance,
                    lure_staked=lure_staked, pending_reel=pending_reel,
                    lure_oracle=lure_oracle, reel_oracle=reel_oracle,
                )
                view = _FishStatsView(self.cog, self.ctx, self.ctx.author)
                view.add_item(_BaitEquipSelect(
                    state, state.get("equipped_bait"),
                ))
                await interaction.response.send_message(
                    embed=embed, view=view, ephemeral=True,
                )
                try:
                    view.message = await interaction.original_response()
                except Exception:
                    pass
            except Exception as e:
                log.debug("cast-result Open Panel failed", exc_info=True)
                await interaction.response.send_message(
                    f"Couldn't open panel: `{type(e).__name__}: {e}`.",
                    ephemeral=True,
                )

        self.add_item(_CallbackButton(
            self.owner_id,
            _on_open_panel,
            label="Panel",
            emoji="\U0001F4CB",
            style=discord.ButtonStyle.secondary,
            row=0,
        ))
        # Bump on its own bottom row -- convention is no action buttons
        # share a row with refresh/bump controls.
        self.add_item(_BumpButton(self.owner_id, row=4))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This isn't your line.", ephemeral=True,
            )
            return False
        return True


class _WildBattleView(discord.ui.View):
    """⚔️ Challenge button for a fishing wild-buddy encounter.

    The cog attaches this view to the CastView's result message when
    cast_resolve returns ``outcome='wild_battle'``. Only the original
    caster can press the button. Win / loss / capture get persisted
    via ``services.fishing.resolve_wild_battle`` and the corresponding
    bus events fire so achievements / quests / challenges all tick.
    """

    def __init__(
        self,
        *,
        cog: "Fishing",
        owner_id: int,
        guild_id: int,
        zone: str,
        opponent: dict,
    ) -> None:
        super().__init__(timeout=fc.WILD_BATTLE_PROMPT_TIMEOUT_S)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.zone = zone
        self.opponent = opponent
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._resolved = False
        # Interactive battle state. Initialised on Challenge press; the
        # action buttons consult it on every turn. None until then.
        self._battle: _LiveBattle | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This is someone else's hook -- cast your own line.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self._resolved or self.message is None:
            return
        try:
            for child in self.children:
                child.disabled = True  # type: ignore[attr-defined]
            await self.message.edit(
                embed=card(
                    "🌊 The Wild Buddy Slipped Away",
                    description=(
                        f"You hesitated and the **{str(self.opponent.get('species') or '?').title()}** "
                        f"vanished into the water. No fight, no reward, no harm done."
                    ),
                    color=C_NEUTRAL,
                ).build(),
                view=None,
            )
        except (discord.NotFound, discord.HTTPException):
            pass

    @discord.ui.button(label="Challenge", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def challenge_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        if self._lock.locked() or self._resolved or self._battle is not None:
            await interaction.response.send_message(
                "You already engaged this fight.", ephemeral=True,
            )
            return
        async with self._lock:
            if self._resolved or self._battle is not None:
                return

            db = self.cog.bot.db
            # Caster's active buddy required.
            from cogs.buddy import _fetch_active, _expedition_busy_message
            p1 = await _fetch_active(db, self.guild_id, self.owner_id)
            if not p1 or _expedition_busy_message(dict(p1) if p1 else {}):
                # No active buddy OR active buddy is on expedition.
                # Surface a precise message: if the player has any owned
                # buddy that isn't on expedition, they just need to swap
                # active. If every owned buddy is deployed, tell them
                # straight up that nobody's home.
                roster = await db.fetch_one(
                    """
                    SELECT
                        COUNT(b.id)                                  AS total,
                        COUNT(b.id) FILTER (
                            WHERE e.expedition_id IS NOT NULL
                        )                                            AS away
                      FROM cc_buddies b
                      LEFT JOIN buddy_expeditions e
                             ON e.buddy_id = b.id
                            AND e.status   = 'running'
                     WHERE b.guild_id = $1 AND b.owner_user_id = $2
                       AND b.status = 'owned'
                    """,
                    self.guild_id, self.owner_id,
                )
                total = int((roster or {}).get("total") or 0)
                away  = int((roster or {}).get("away")  or 0)
                if total > 0 and total == away:
                    msg = (
                        f"All **{total}** of your buddies are out on "
                        f"expeditions -- there's no one home to fight "
                        f"the wild buddy. `,expedition` to track them. "
                        f"The wild buddy escapes."
                    )
                elif p1 and bool(dict(p1).get("on_expedition")):
                    msg = (
                        "Your active buddy is on an expedition -- swap "
                        "to a buddy that's home (`,buddy` panel) and "
                        "retry. The wild buddy escapes."
                    )
                else:
                    msg = (
                        "You need an active buddy to fight. Try "
                        "`,buddy hatch` or `,buddy shelter` first -- "
                        "the wild buddy escapes and your cast counts "
                        "as a normal miss."
                    )
                await interaction.response.send_message(msg, ephemeral=True)
                self._resolved = True
                if self.message is not None:
                    for child in self.children:
                        child.disabled = True  # type: ignore[attr-defined]
                    try:
                        await self.message.edit(view=self)
                    except (discord.NotFound, discord.HTTPException):
                        pass
                return

            # Build live fighters from the same Fighter.from_row path
            # the auto-run engine uses, so stat seeding (level + tier +
            # mood + alloc) stays identical between the two surfaces.
            try:
                from services.buddy_battle import Fighter
                player_f = Fighter.from_row(dict(p1))
                enemy_f = Fighter.from_row(dict(self.opponent))
            except Exception:
                log.exception(
                    "fish wild battle: Fighter.from_row failed gid=%s uid=%s",
                    self.guild_id, self.owner_id,
                )
                self._resolved = True
                if self.message is not None:
                    try:
                        await self.message.edit(
                            embed=card(
                                "💥 Something went wrong",
                                description=(
                                    "The wild buddy escaped while we "
                                    "tangled the line. No counters bumped."
                                ),
                                color=C_ERROR,
                            ).build(),
                            view=None,
                        )
                    except (discord.NotFound, discord.HTTPException):
                        pass
                return

            self._battle = _LiveBattle(player=player_f, enemy=enemy_f)

            # Replace the Challenge button with the four interactive
            # action buttons. Re-rendering the message attaches the
            # new view layout in the same interaction round-trip.
            self.clear_items()
            self.add_item(self._make_action_button(
                "Strike", "⚔️", "strike", discord.ButtonStyle.primary,
            ))
            # Special button surfaces the player buddy's named ability
            # (e.g. "Pack Howl") so the player knows what they're
            # casting -- matches ,buddy map battle / arena.
            self.add_item(self._make_action_button(
                str(player_f.ability_name or "Special")[:20] or "Special",
                "💥", "special", discord.ButtonStyle.success,
            ))
            self.add_item(self._make_action_button(
                "Brace", "🛡️", "brace", discord.ButtonStyle.secondary,
            ))
            self.add_item(self._make_action_button(
                "Risky", "🎯", "risky", discord.ButtonStyle.danger,
            ))
            # Pokemon-style in-fight Capture affordance. Disabled until
            # enemy HP drops below CAPTURE_HP_THRESHOLD, then the label
            # live-updates with the rolled chance so the player can read
            # whether to throw now or wear the wild buddy down further.
            # Mirrors the delve wild-battle pattern (cogs/dungeon.py).
            cap_btn = discord.ui.Button(
                label="Capture", emoji="\U0001F9F2",
                style=discord.ButtonStyle.secondary,
                disabled=True, row=1,
            )
            cap_btn.callback = self._capture_callback
            self.add_item(cap_btn)
            self._refresh_action_button_state()
            _embed, _file = self._round_embed(opening=True)
            edit_kw: dict = {"embed": _embed, "view": self}
            if _file is not None:
                edit_kw["attachments"] = [_file]
            await interaction.response.edit_message(**edit_kw)

    def _make_action_button(
        self, label: str, emoji: str, action_key: str,
        style: "discord.ButtonStyle",
    ) -> "discord.ui.Button":
        """Construct an action button with a closure-bound callback.

        Wraps ``_handle_action(action_key)`` so all four buttons share
        the same handler without needing a decorator per action.
        """
        btn = discord.ui.Button(
            label=label, emoji=emoji, style=style, disabled=False,
        )
        # Stamp the action_key onto the button so refresh logic can
        # find the Special button by intent rather than by label (the
        # label is now the buddy's named ability).
        btn.action_key = action_key  # type: ignore[attr-defined]

        async def _cb(interaction: discord.Interaction) -> None:
            await self._handle_action(interaction, action_key)

        btn.callback = _cb
        return btn

    def _refresh_action_button_state(self) -> None:
        """Disable Special when the player lacks stamina for it.

        Also gates the in-fight Capture button: disabled while the wild
        buddy is above ``CAPTURE_HP_THRESHOLD`` HP, enabled below it
        with a live ``Capture (NN%)`` label so the player can decide
        whether to throw now or chip the enemy down further.
        """
        if not self._battle:
            return
        b = self._battle
        hp_frac = float(b.enemy.hp) / max(1, float(b.enemy.max_hp))
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if getattr(child, "action_key", None) == "special":
                child.disabled = (
                    b.player_stamina < _ACT_SPECIAL_STAMINA_COST
                )
            elif child.label == "Capture" or (
                isinstance(child.label, str) and child.label.startswith("Capture")
            ):
                from configs.dungeon_config import CAPTURE_HP_THRESHOLD as _CAP_HP
                child.disabled = hp_frac > _CAP_HP
                if child.disabled:
                    child.label = "Capture"
                else:
                    pct = int(self._capture_chance() * 100)
                    child.label = f"Capture ({pct}%)"

    def _capture_chance(self) -> float:
        """Compute the in-fight wild-buddy capture chance.

        Mirrors the delve formula: tier penalty baked in, then a
        HP-based bonus that scales linearly with how low the enemy is
        below the HP threshold, so the capture-low-then-throw flow
        rewards weakening the wild buddy first.
        """
        b = self._battle
        if not b:
            return 0.0
        from configs.dungeon_config import (
            CAPTURE_HP_THRESHOLD as _CAP_HP,
            CAPTURE_BASE_CHANCE as _CAP_BASE,
            CAPTURE_PER_TIER_PENALTY as _CAP_PEN,
        )
        hp_frac = float(b.enemy.hp) / max(1, float(b.enemy.max_hp))
        if hp_frac > _CAP_HP:
            return 0.0
        rarity_tier = int(self.opponent.get("rarity_tier") or 1)
        base = max(0.0, _CAP_BASE - max(0, rarity_tier - 1) * _CAP_PEN)
        bonus = (1.0 - hp_frac / _CAP_HP) * 0.50
        return max(0.05, min(0.95, base + bonus))

    async def _capture_callback(
        self, interaction: discord.Interaction,
    ) -> None:
        """In-fight capture attempt for fishing wild battles.

        Gated by enemy HP via the button's ``disabled`` flag (refreshed
        every round). On success the enemy HP is forced to 0 and the
        battle finalises as a captured win; the explicit cc_buddies
        insert path runs ahead of resolve_wild_battle so the manual
        capture is guaranteed (the post-fight auto-roll would otherwise
        compete with its own dice). On failure the wild buddy gets a
        free turn and the fight continues. Mirrors the delve pattern.
        """
        if self._resolved or not self._battle:
            await interaction.response.defer()
            return
        if self._lock.locked():
            await interaction.response.defer()
            return
        async with self._lock:
            if self._resolved or not self._battle:
                return
            b = self._battle
            from configs.dungeon_config import (
                CAPTURE_HP_THRESHOLD as _CAP_HP,
            )
            chance = self._capture_chance()
            if chance <= 0:
                await interaction.response.send_message(
                    f"Get the wild buddy below "
                    f"**{int(_CAP_HP * 100)}%** HP first.",
                    ephemeral=True,
                )
                return
            roll = random.random()
            if roll <= chance:
                # KO + capture path. Insert cc_buddies row directly so
                # the manual capture always lands; resolve_wild_battle
                # is then called with skip_capture_roll=True so the
                # auto-roll can't double-insert.
                b.enemy.hp = 0
                b.log_lines.append(
                    f"\U0001F4AB You hurl a charm. "
                    f"It works! ({int(chance * 100)}% rolled {int(roll * 100)})"
                )
                db = self.cog.bot.db
                try:
                    from services.buddy_economy import (
                        capture_destination as _capture_destination,
                    )
                    capture_dest = await _capture_destination(
                        db, self.guild_id, self.owner_id,
                    )
                    if capture_dest is not None:
                        capture_status = (
                            "owned" if capture_dest == "battle" else "stored"
                        )
                        species = str(self.opponent.get("species") or "")
                        try:
                            from configs.buddies_config import SPECIES as _SPECIES
                            sp_meta = _SPECIES.get(species, {})
                            name_pool = sp_meta.get("name_pool") or [species.title()]
                        except Exception:
                            name_pool = [species.title() if species else "Buddy"]
                        try:
                            from services.buddy_names import generate_name
                            name = await generate_name(
                                species, db, self.guild_id,
                            )
                        except Exception:
                            name = random.choice(name_pool)
                        from configs.buddies_config import (
                            roll_gender as _roll_gender,
                            xp_for_level as _xp_for_level,
                        )
                        _cap_lvl = int(self.opponent.get("level") or 1)
                        _cap_tier = int(self.opponent.get("rarity_tier") or 1)
                        await db.fetch_one(
                            """
                            INSERT INTO cc_buddies
                                (guild_id, owner_user_id, species, name,
                                 status, is_active, rarity_tier, level, xp,
                                 gender, capture_message_id,
                                 capture_channel_id)
                            VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7, $8,
                                    $9, $10, $11)
                            RETURNING id
                            """,
                            self.guild_id, self.owner_id,
                            species, name, str(capture_status),
                            int(max(1, _cap_tier)),
                            _cap_lvl,
                            int(_xp_for_level(_cap_lvl)),
                            _roll_gender(),
                            self.message.id if self.message else None,
                            self.message.channel.id if self.message else None,
                        )
                        try:
                            await interaction.followup.send(
                                (
                                    "Buddy went to your **storage** "
                                    "(`,buddy storage`) -- active slots full."
                                    if capture_dest == "storage"
                                    else "Buddy joined your active roster."
                                ),
                                ephemeral=True,
                            )
                        except discord.HTTPException:
                            pass
                    else:
                        try:
                            await interaction.followup.send(
                                "Battle + storage both full -- couldn't "
                                "store the buddy. Free a slot via "
                                "`,buddy store` or surrender first.",
                                ephemeral=True,
                            )
                        except discord.HTTPException:
                            pass
                except Exception:
                    log.debug(
                        "fish wild capture: cc_buddies insert failed",
                        exc_info=True,
                    )
                self._manual_capture_done = True
                await self._finalize(interaction)
                return
            # Capture failed -- enemy gets a free turn.
            b.log_lines.append(
                f"\U0001F4A8 The {self.opponent.get('species', 'wild buddy')} "
                f"slipped the charm! ({int(chance * 100)}% rolled "
                f"{int(roll * 100)})"
            )
            ai_lines = _enemy_ai_turn(b)
            b.log_lines.extend(ai_lines)
            b.round_num += 1
            if b.is_over():
                await self._finalize(interaction)
                return
            self._refresh_action_button_state()
            _embed, _file = self._round_embed()
            try:
                _kw: dict = {"embed": _embed, "view": self}
                if _file is not None:
                    _kw["attachments"] = [_file]
                await interaction.response.edit_message(**_kw)
            except discord.HTTPException:
                log.debug(
                    "fish wild capture: round edit failed", exc_info=True,
                )

    async def _handle_action(
        self, interaction: discord.Interaction, action_key: str,
    ) -> None:
        """Per-turn handler shared by the 4 action buttons.

        Resolves player action -> opponent AI -> updates state and
        either re-renders the round embed or finalises the fight if
        someone hit 0 HP.
        """
        if self._resolved or not self._battle:
            await interaction.response.defer()
            return
        if self._lock.locked():
            await interaction.response.defer()
            return
        async with self._lock:
            if self._resolved or not self._battle:
                return
            b = self._battle

            # Defer up front -- burst animations push us past the 3s
            # interaction window, so we edit self.message directly after.
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                log.debug("fish wild battle: defer failed", exc_info=True)

            # Player swing burst.
            from services.buddy_battle_scene import play_battle_action_burst
            await play_battle_action_burst(
                self, b.player, b.enemy,
                actor_side="p1",
                action=str(action_key),
                round_num=int(b.round_num),
                max_rounds=_BATTLE_MAX_ROUNDS,
                ability_name=str(getattr(b.player, "ability_name", "") or ""),
            )

            # Player turn first. If the player drops the enemy to 0 the
            # opponent doesn't get a counter swing.
            new_lines = _apply_player_action(b, action_key)
            b.log_lines.extend(new_lines)
            if b.is_over():
                b.log_lines.append("")  # blank separator before verdict
                await self._finalize(interaction)
                return

            # Opponent swing burst -- enemy AI always strikes; we use
            # "strike" as the action so the overlay is yellow slash
            # arcs (matches the AI swing flavour).
            await play_battle_action_burst(
                self, b.player, b.enemy,
                actor_side="p2", action="strike",
                round_num=int(b.round_num),
                max_rounds=_BATTLE_MAX_ROUNDS,
            )

            # Opponent AI turn.
            ai_lines = _enemy_ai_turn(b)
            b.log_lines.extend(ai_lines)
            b.round_num += 1
            if b.is_over():
                b.log_lines.append("")
                await self._finalize(interaction)
                return

            # Refresh button availability + re-render.
            self._refresh_action_button_state()
            _embed, _file = self._round_embed()
            try:
                _kw: dict = {"embed": _embed, "view": self}
                if _file is not None:
                    _kw["attachments"] = [_file]
                if self.message is not None:
                    await self.message.edit(**_kw)
            except discord.HTTPException:
                log.debug(
                    "fish wild battle: round edit failed", exc_info=True,
                )

    def _round_embed(
        self, *, opening: bool = False, action_banner: str = "",
    ) -> tuple[discord.Embed, "discord.File | None"]:
        """Render the per-round combat panel + battle scene PNG.

        Returns ``(embed, scene_file)`` so callers attach the PNG via
        ``file=`` (or ``attachments=[file]`` for an edit). Same scene
        renderer as the arena map view -- fishing wild-buddy fights now
        ship the same Pokemon-Stadium-style visual as every other buddy
        battle in the game.
        """
        b = self._battle
        assert b is not None
        p, e = b.player, b.enemy
        p_emoji = p.emoji or "🦆"
        e_emoji = e.emoji or "🐙"

        tail_lines = [ln for ln in b.log_lines[-6:] if ln.strip()]
        if opening or not tail_lines:
            tail = "_Choose your move..._"
        else:
            tail = "\n".join(tail_lines)

        title = f"⚔️ Round {b.round_num}  -  Wild {e.species.title()}"
        stamina_pips = "●" * b.player_stamina + "○" * (_ACT_PLAYER_STAMINA_MAX - b.player_stamina)
        desc_lines = [
            f"{p_emoji} **{p.name}**  Lv.{p.level} {p.tier_name}",
            f"  HP `{_hp_bar(p.hp, p.max_hp)}`  -  ATK {int(p.atk)}",
            f"  Stamina `{stamina_pips}` ({b.player_stamina}/{_ACT_PLAYER_STAMINA_MAX})",
            "",
            f"{e_emoji} **Wild {e.name}**  Lv.{e.level} {e.tier_name}",
            f"  HP `{_hp_bar(e.hp, e.max_hp)}`  -  ATK {int(e.atk)}",
            "",
            tail,
        ]
        if opening:
            desc_lines.append(
                f"-# Strike (+1 stamina)  •  Special ({_ACT_SPECIAL_STAMINA_COST} stamina)  "
                f"•  Brace (heal + halve next hit)  •  Risky (60% huge / 25% miss / 15% backfire)"
            )

        # Battle scene PNG -- shared renderer across every battle view.
        scene_file: "discord.File | None" = None
        try:
            from services.buddy_battle_scene import (
                fighters_to_scene_state, render_battle_frame,
            )
            import io as _io
            state = fighters_to_scene_state(
                p, e,
                round_num=b.round_num,
                max_rounds=_BATTLE_MAX_ROUNDS,
                action_banner=action_banner or ("FIGHT!" if opening else ""),
                is_player_turn=True,
            )
            png = render_battle_frame(state)
            scene_file = discord.File(_io.BytesIO(png), filename="battle.png")
        except Exception:
            log.debug("fishing wild battle: scene render failed", exc_info=True)

        builder = card(title, color=C_AMBER).description("\n".join(desc_lines))
        if scene_file is not None:
            builder = builder.image("attachment://battle.png")
        return builder.build(), scene_file

    async def _finalize(self, interaction: discord.Interaction) -> None:
        """End-of-fight: persist + render result. Replaces the view.

        Defers the interaction up front so a slow ``resolve_wild_battle``
        DB round-trip doesn't blow the 3-second response window. The
        final edit then goes through ``self.message`` directly (works
        regardless of whether the interaction is still alive).
        """
        self._resolved = True
        b = self._battle
        assert b is not None
        won = b.player_won()
        # Compute performance bonus + maybe roll a loot drop. Both
        # only matter on a win; on a loss the floor stays 0 reward.
        bonus_pct = _compute_battle_bonus(b) if won else 0.0
        loot_drop = _roll_loot_drop() if won else None
        # ``_manual_capture_done`` is set by the in-fight Capture button
        # right before it forces a finalise. When True the resolver
        # skips its auto-capture roll (cc_buddies insert already ran in
        # the button path) so the two surfaces can't double-insert.
        manual_captured = bool(getattr(self, "_manual_capture_done", False))

        # Acknowledge the click immediately so Discord doesn't show
        # "Interaction failed" while we hit the DB. Skip the defer when
        # _handle_action already deferred up-front (which it does so
        # the per-move burst frames don't blow the 3s response window).
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass

        # Disable every button before the network round-trip so a
        # stuck DB doesn't leave clickable buttons hanging mid-write.
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass

        db = self.cog.bot.db
        try:
            # Capture pulls the opponent's actual species + level + tier
            # off the live Fighter so a captured Lv.20 Epic Octopus joins
            # the shelter as a Lv.20 Epic Octopus, not a freshly-rolled
            # commoner. Falls back to the synthesised opponent dict's
            # values when the in-memory Fighter wasn't built (rare race).
            opp_level = int(getattr(b.enemy, "level", None)
                            or self.opponent.get("level") or 1)
            opp_tier = int(getattr(b.enemy, "tier", None)
                           or self.opponent.get("rarity_tier") or 1)
            resolution = await fish_svc.resolve_wild_battle(
                db, self.guild_id, self.owner_id,
                won=won, zone=self.zone,
                opponent_species=str(self.opponent.get("species") or ""),
                opponent_level=opp_level,
                opponent_rarity_tier=opp_tier,
                bonus_pct=bonus_pct,
                loot_drop=loot_drop,
                capture_message_id=(self.message.id if self.message else None),
                capture_channel_id=(self.message.channel.id if self.message else None),
                skip_capture_roll=manual_captured,
            )
        except Exception:
            log.exception(
                "fish wild battle: resolve failed gid=%s uid=%s",
                self.guild_id, self.owner_id,
            )
            resolution = None

        # Live oracles so the receipt can quote USD next to LURE / REEL.
        lure_oracle = 0.0
        reel_oracle = 0.0
        try:
            lp_row = await db.get_price(fc.LURE_SYMBOL, self.guild_id)
            rp_row = await db.get_price(fc.REEL_SYMBOL, self.guild_id)
            lure_oracle = float(lp_row["price"]) if lp_row else 0.0
            reel_oracle = float(rp_row["price"]) if rp_row else 0.0
        except Exception:
            pass

        embed = self._render_final_embed(
            b, resolution, lure_oracle, reel_oracle,
        )
        target = self.message
        try:
            if target is not None:
                await target.edit(embed=embed, view=None)
        except discord.HTTPException:
            log.debug("fish wild battle: final edit failed", exc_info=True)

        # Bus events fan-out -- same as before so quests / achievements /
        # challenges keep ticking.
        await fish_svc._publish_economy_event(
            self.cog.bot,
            event=(fish_svc.EVENT_WILD_WIN if won else fish_svc.EVENT_WILD_LOSS),
            guild_id=self.guild_id, user_id=self.owner_id,
            wild_species=str(self.opponent.get("species") or ""),
            wild_level=int(self.opponent.get("level") or 1),
            lure_reward_raw=(getattr(resolution, "lure_reward_raw", 0) if resolution else 0),
            reel_reward_raw=(getattr(resolution, "reel_reward_raw", 0) if resolution else 0),
        )
        if resolution and resolution.captured_species:
            await fish_svc._publish_economy_event(
                self.cog.bot,
                event=fish_svc.EVENT_WILD_CAPTURE,
                guild_id=self.guild_id, user_id=self.owner_id,
                captured_species=resolution.captured_species,
            )
        elif manual_captured:
            # The cog inserted cc_buddies directly via the in-fight
            # Capture button, so resolve_wild_battle was called with
            # skip_capture_roll=True and never published the capture
            # event itself. Publish here so achievements / quests /
            # challenges that watch for wild captures still credit it.
            await fish_svc._publish_economy_event(
                self.cog.bot,
                event=fish_svc.EVENT_WILD_CAPTURE,
                guild_id=self.guild_id, user_id=self.owner_id,
                captured_species=str(self.opponent.get("species") or ""),
            )

        # Unified buddy_battle_win / _loss bus event so the cross-surface
        # achievements / quests / challenges (Buddy Champion / Legend /
        # Dynasty, etc.) tick on a fish wild battle the same as a PvP
        # win or an arena fight. Plus cc_buddies.wins/losses for the
        # player's buddy so its per-buddy W/L stays unified across every
        # surface it can fight on.
        try:
            from services.buddy_battle import record_pve_battle_result as _rec_pve
            player_buddy_id = int(getattr(b.player, "id", 0) or 0) or None
            await _rec_pve(
                self.cog.bot.db,
                player_buddy_id=player_buddy_id,
                won=bool(won),
                rounds=int(b.round_num),
            )
            bus = getattr(self.cog.bot, "bus", None)
            if bus is not None:
                if won:
                    await bus.publish(
                        "buddy_battle_win",
                        guild=self.guild_id, user_id=self.owner_id,
                        winner_buddy_id=player_buddy_id,
                        loser_buddy_id=None,
                        source="fish_wild",
                    )
                else:
                    await bus.publish(
                        "buddy_battle_loss",
                        guild=self.guild_id, user_id=self.owner_id,
                        winner_buddy_id=None,
                        loser_buddy_id=player_buddy_id,
                        source="fish_wild",
                    )
        except Exception:
            log.debug("fish wild battle: unified buddy_battle event failed",
                      exc_info=True)

    def _render_final_embed(
        self, b: _LiveBattle, resolution: "Any",
        lure_oracle: float = 0.0, reel_oracle: float = 0.0,
    ) -> discord.Embed:
        """Final result embed for an interactive wild battle.

        Mirrors the auto-run path's _render_battle_embed but adds the
        bonus_pct breakdown and loot-drop line when populated.
        """
        species = str(self.opponent.get("species") or "?").title()
        won = b.player_won()

        if won and resolution is not None:
            lure_h = to_human(int(resolution.lure_reward_raw))
            reel_h = to_human(int(getattr(resolution, "reel_reward_raw", 0) or 0))
            bonus_pct = float(getattr(resolution, "bonus_pct_applied", 0.0) or 0.0)
            reward_lines = [
                f"💰 LURE: **{_fmt_lure(lure_h)}**{_with_usd(lure_h, lure_oracle)}"
            ]
            if reel_h > 0:
                reward_lines.append(
                    f"🎣 REEL: **{_fmt_reel(reel_h)}**"
                    f"{_with_usd(reel_h, reel_oracle)}"
                )
            lines = [
                f"⚔️ You beat the wild **{species}** in {b.round_num} rounds.",
                *reward_lines,
                _MINT_FOOTER,
            ]
            if bonus_pct > 0:
                lines.append(
                    f"-# Performance bonus: **+{bonus_pct * 100:.0f}%** "
                    f"(rounds / HP remaining / action variety)"
                )
            # Surface buddy XP credited for this win so the player sees
            # the same XP hit they'd get from a buddy-vs-buddy battle.
            if int(getattr(resolution, "buddy_xp_awarded", 0) or 0) > 0:
                fighter_id = getattr(resolution, "fighter_buddy_id", None)
                tag = f" (#{int(fighter_id)})" if fighter_id else ""
                lines.append(
                    f"\U0001F436 Your buddy{tag} earns "
                    f"**+{int(resolution.buddy_xp_awarded):,}** XP."
                )
            loot = getattr(resolution, "loot_dropped", None)
            if loot:
                lines.append(
                    f"🎁 **Bonus loot:** {loot.get('label') or loot.get('kind')}"
                )
            if resolution.captured_species and resolution.buddy_row:
                buddy_name = str(resolution.buddy_row.get("name") or species)
                buddy_id = int(resolution.buddy_row.get("id") or 0)
                tier_n = int(resolution.buddy_row.get("rarity_tier") or 1)
                lvl_n = int(resolution.buddy_row.get("level") or 1)
                gender_code = str(resolution.buddy_row.get("gender") or "")
                try:
                    from configs.buddies_config import (
                        rarity_meta as _b_rarity,
                        gender_glyph as _b_gender,
                    )
                    tier_label = str(_b_rarity(tier_n).get("name") or "Common")
                    glyph = _b_gender(gender_code)
                except Exception:
                    tier_label = "Common"
                    glyph = ""
                glyph_part = f" {glyph}" if glyph else ""
                id_tag = f" `#{buddy_id}`" if buddy_id else ""
                cap_status = str(resolution.buddy_row.get("status") or "owned")
                destination_line = (
                    "Active slots were full, so they went to your "
                    "**storage** -- view / withdraw via `,buddy storage`."
                    if cap_status == "stored"
                    else (
                        "Added to your active roster as inactive -- view it "
                        "with `,buddy stats` (page through) or "
                        f"`,buddy find {resolution.captured_species}`."
                    )
                )
                lines.append(
                    f"✨ **Captured!** The wild **{resolution.captured_species}** "
                    f"joined your collection as **{buddy_name}**{glyph_part}"
                    f"{id_tag} (Lv.{lvl_n} {tier_label}). {destination_line}"
                )
            elif resolution.capture_refused_full:
                cap = int(resolution.owned_cap or 0)
                cap_tag = f" ({cap}/{cap})" if cap > 0 else ""
                lines.append(
                    f"😔 **Almost!** The capture roll hit but your active "
                    f"**and** storage slots are both full{cap_tag}, so "
                    f"the wild **{species}** got away. Free a slot via "
                    f"`,buddy store <id>`, `,buddy surrender <id>`, or "
                    f"buy more from `,buddy shop` before the next wild fight."
                )
            lines.append(
                f"-# Wild battles: **{resolution.new_won_total}** won / "
                f"**{resolution.new_lost_total}** lost  -  "
                f"**{resolution.new_capture_total}** captured."
            )
            color = C_SUCCESS
            title = f"🏆 Wild {species} Defeated"
        elif resolution is not None:
            lines = [
                f"💀 The wild **{species}** beat your buddy in {b.round_num} rounds.",
                "No penalty -- but no reward either. Try again with a tougher buddy.",
                f"-# Wild battles: **{resolution.new_won_total}** won / "
                f"**{resolution.new_lost_total}** lost  -  "
                f"**{resolution.new_capture_total}** captured.",
            ]
            color = C_ERROR
            title = f"💀 Defeated by Wild {species}"
        else:
            lines = [
                f"⚔️ Battle vs wild **{species}** ended in {b.round_num} rounds.",
                "Could not persist the result -- counters not updated.",
            ]
            color = C_NEUTRAL
            title = f"⚔️ Wild {species}"

        # Battle log: pull from b.log_lines (the interactive log we built
        # round-by-round) and feed it through the same chunker the
        # auto-run path uses so long fights still split cleanly across
        # multiple Battle Log fields.
        log_chunks = _split_battle_log(list(b.log_lines)) if b.log_lines else []

        builder = card(
            title, description="\n".join(lines), color=color,
        )
        for i, chunk in enumerate(log_chunks):
            field_name = "Battle Log" if i == 0 else f"Battle Log (cont. {i + 1})"
            builder = builder.field(field_name, chunk, False)
        return builder.build()

# ============================================================================
# Cog
# ============================================================================

class _BaitEquipSelect(discord.ui.Select):
    """Dropdown that lists every bait the player owns; selecting one
    runs ``fish_svc.set_bait`` to equip it. Shows up on the
    ``,fish stats`` panel so a player can swap bait without typing keys.
    """

    def __init__(
        self, state: dict, equipped: str | None,
    ) -> None:
        inv = _jsonb_dict(state.get("bait_inventory")) if state else {}
        opts: list[discord.SelectOption] = []
        # Always offer an "unequip" row.
        opts.append(discord.SelectOption(
            label="(unequip)",
            value="__none__",
            emoji="\U0001F6AB",
            default=(not equipped),
        ))
        for key, count in sorted(
            inv.items(), key=lambda kv: -int(kv[1] or 0),
        ):
            try:
                cnt = int(count or 0)
            except (TypeError, ValueError):
                cnt = 0
            if cnt <= 0:
                continue
            meta = fc.bait_meta(key) or {}
            label = f"{meta.get('name', key)} (x{cnt})"[:100]
            opts.append(discord.SelectOption(
                label=label,
                value=str(key),
                emoji=str(meta.get("emoji") or "")[:1] or None,
                default=(equipped == key),
            ))
        if len(opts) == 1:
            opts.append(discord.SelectOption(
                label="(no bait owned)",
                value="__empty__", default=False,
            ))
        super().__init__(
            placeholder="Equip bait...",
            options=opts[:25],
            min_values=1, max_values=1,
            row=0,
            disabled=False,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_FishStatsView" = self.view  # type: ignore
        choice = self.values[0]
        if choice == "__empty__":
            await interaction.response.send_message(
                "Buy bait via `,fish shop` first.", ephemeral=True,
            )
            return
        target = None if choice == "__none__" else choice
        try:
            await fish_svc.set_bait(
                view.ctx.db, view.ctx.guild_id,
                interaction.user.id, target,
            )
        except ValueError as e:
            await interaction.response.send_message(
                str(e), ephemeral=True,
            )
            return
        await view.refresh(interaction)


class _FishStatsView(discord.ui.View):
    """Owner-locked panel for ``,fish stats``.

    Bait dropdown to equip from inventory + Cast button + Refresh.
    Cast routes through the existing cog method so the animation +
    bite-window UX stays unchanged.
    """

    def __init__(
        self, cog: "Fishing", ctx: DiscoContext,
        target_user: discord.Member,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.target_user = target_user
        self.message: discord.Message | None = None

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        # Bait + Cast are personal -- only the panel owner can click.
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your panel. Run `,fish stats` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _build(self) -> tuple[discord.Embed, dict]:
        state = await fish_svc.ensure_state(
            self.ctx.db, self.ctx.guild_id, self.target_user.id,
        )
        # Reuse the existing _stats_embed builder so the panel matches
        # the static ,fish stats output exactly.
        lure_balance = to_human(
            await fish_svc.get_lure_wallet_raw(
                self.ctx.db, self.ctx.guild_id, self.target_user.id,
            )
        )
        reel_balance = to_human(
            await fish_svc.get_reel_wallet_raw(
                self.ctx.db, self.ctx.guild_id, self.target_user.id,
            )
        )
        lure_staked = to_human(int(state.get("lure_staked_raw") or 0))
        pending_reel = to_human(
            await fish_svc.accrued_stake_yield(
                self.ctx.db, self.ctx.guild_id, self.target_user.id,
            )
        )
        lp_row = await self.ctx.db.get_price(fc.LURE_SYMBOL, self.ctx.guild_id)
        rp_row = await self.ctx.db.get_price(fc.REEL_SYMBOL, self.ctx.guild_id)
        lure_oracle = float(lp_row["price"]) if lp_row else 0.0
        reel_oracle = float(rp_row["price"]) if rp_row else 0.0
        embed = _stats_embed(
            dict(state),
            member=self.target_user,
            lure_balance=lure_balance, reel_balance=reel_balance,
            lure_staked=lure_staked, pending_reel=pending_reel,
            lure_oracle=lure_oracle, reel_oracle=reel_oracle,
        )
        return embed, state

    async def refresh(self, interaction: discord.Interaction) -> None:
        embed, state = await self._build()
        # Rebuild bait select (options bind at construction time).
        equipped = state.get("equipped_bait")
        for child in list(self.children):
            if isinstance(child, _BaitEquipSelect):
                self.remove_item(child)
        self.add_item(_BaitEquipSelect(state, equipped))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Cast", emoji="\U0001F3A3",
        style=discord.ButtonStyle.success, row=1,
    )
    async def btn_cast(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        # Fire the cast in a follow-up so the panel stays put. The
        # cast flow has its own message/animation -- we just let it
        # post into the channel.
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        await self.cog._cmd_cast(self.ctx)

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self.refresh(interaction)


class Fishing(commands.Cog):
    """Fishing minigame: cast, catch, sell, climb the leaderboard."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        # Per-user concurrency lock so a fast double-tap on ,fish
        # doesn't spawn two parallel views racing the same DB row.
        self._cast_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def cog_load(self) -> None:
        # Wire fishing event listeners into achievements/quests/etc.
        # Done at load time so a hot-reload picks the updates up
        # without restarting the bot.
        try:
            fish_svc.attach_listeners(self.bot)
        except Exception:
            log.exception("fishing: attach_listeners failed")

    async def cog_check(self, ctx) -> bool:
        """Module + premium gate.

        Module gate: ``,admin module fishing off`` disables every command
        in this cog (admins always bypass).

        Premium gate: fishing is a paid feature; the host guild and any
        guild with an active premium subscription bypass it. Admins do NOT
        bypass premium -- they're the ones who pay."""
        if not await module_cog_check(self.bot, ctx, "fishing"):
            return False
        from services import entitlements
        from core.framework.premium import PremiumGateFailure
        if not await entitlements.is_premium(ctx.guild_id, ctx.db):
            raise PremiumGateFailure("fishing")
        return True

    # -- Helpers ----------------------------------------------------------

    def _cast_lock(self, uid: int, gid: int) -> asyncio.Lock:
        return self._cast_locks.setdefault((uid, gid), asyncio.Lock())

    async def _post_splash(self, ctx: DiscoContext, result: fish_svc.CastResult) -> None:
        """Send a public splash embed to the configured fishing /
        events channel when a rare+ fish is landed.

        Falls back silently if no channel is configured -- the result
        embed in the original channel still shows.
        """
        if not result.splash:
            return
        try:
            settings = await ctx.bot.db.get_guild_settings(ctx.guild_id)
            ch_id = (settings or {}).get("fishing_channel") \
                or (settings or {}).get("events_channel")
            if not ch_id:
                return
            ch = ctx.guild.get_channel(int(ch_id))
            if not isinstance(ch, discord.TextChannel):
                return
            await ch.send(embed=_splash_embed(result, member=ctx.author))  # type: ignore[arg-type]
        except Exception:
            log.debug("fishing: splash post failed", exc_info=True)

    # -- Group root -------------------------------------------------------

    @commands.hybrid_group(
        name="fish", aliases=["cast", "fishing"],
        invoke_without_command=True, with_app_command=False,
    )
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.CAST_COOLDOWN_S)
    async def fish(self, ctx: DiscoContext) -> None:
        """Cast your line. Pull up fish, junk, money, or rarer things."""
        # The bare `,fish` invocation runs the cast flow. Subcommands
        # below handle stats, inventory, shop, leaderboard, etc.
        from services.onboarding import maybe_send_intro
        await maybe_send_intro(ctx, "fishing")
        await _maybe_warn_full_slots(ctx, surface="fishing", phase="game_start")
        await self._cmd_cast(ctx)

    # -- Cast flow --------------------------------------------------------

    async def _cmd_cast(
        self,
        ctx: DiscoContext,
        *,
        existing_message: discord.Message | None = None,
    ) -> None:
        """Run a cast.

        ``existing_message`` lets the Cast Again button drive the new
        cast on the SAME message the result panel landed on -- the
        animation, the result embed, and the next ``_CastResultView``
        all replace the previous content in place instead of posting a
        fresh chat message every time.
        """
        gid, uid = ctx.guild_id, ctx.author.id
        lock = self._cast_lock(uid, gid)
        if lock.locked():
            await ctx.reply_error(
                "You're already mid-cast. Hook the fish on screen first."
            )
            return

        async with lock:
            # Reserve the soft DB lock + consume one bait. If begin_cast
            # returns None we lost a race with a parallel command path,
            # which shouldn't happen given the asyncio.Lock above but
            # we guard for safety.
            state = await fish_svc.begin_cast(ctx.db, gid, uid)
            if state is None:
                # Stale-lock recovery already runs inside _set_casting;
                # if begin_cast still bounced, force-clear and retry once
                # so a wedged row doesn't pin the user forever.
                cleared = await fish_svc.force_unstuck(ctx.db, gid, uid)
                if cleared:
                    state = await fish_svc.begin_cast(ctx.db, gid, uid)
                if state is None:
                    await ctx.reply_error_action(
                        "Couldn't start a cast (your line is tangled). "
                        "Try again, or run `,fish unstuck` to reset.",
                        button_label="Unstuck",
                        command="fish unstuck",
                    )
                    return

            # Send the initial frame so the view has a message to edit.
            # The state dict gets passed into CastView so every
            # subsequent frame's footer reflects the player's combo,
            # level, gear, and zone without re-querying the DB.
            initial = _frame_embed(
                "cast",
                title="\U0001F3A3 Casting...",
                color=C_INFO,
                hint=fc.random_hint("cast"),
                footer=_cast_context_footer(dict(state)),
            )
            view = CastView(ctx, self, dict(state))
            try:
                if existing_message is not None:
                    # Reuse the result message: edit it back to the
                    # opening cast frame, then let CastView's
                    # animation drive subsequent edits in place.
                    await existing_message.edit(embed=initial, view=view)
                    view.message = existing_message
                else:
                    view.message = await ctx.reply(
                        embed=initial, view=view, mention_author=False,
                    )
            except Exception:
                await fish_svc.end_cast(ctx.db, gid, uid)
                raise

            try:
                result = await view.run()
            finally:
                # cast_resolve already releases the soft lock as part
                # of its single UPDATE; this is a belt-and-suspenders
                # call for the cancelled / errored paths.
                await fish_svc.end_cast(ctx.db, gid, uid)

            # Render the final embed in the SAME message so the whole
            # cast reads as one continuous event.
            after = await fish_svc.list_state(ctx.db, gid, uid)
            # Wild-buddy battle outcome: attach the Challenge view to the
            # cast message instead of clearing it. The view runs the PvE
            # fight and edits the same message with the result embed.
            wild_view: _WildBattleView | None = None
            if result.outcome == "wild_battle" and result.wild_buddy:
                wild_view = _WildBattleView(
                    cog=self,
                    owner_id=ctx.author.id,
                    guild_id=ctx.guild_id,
                    zone=str(after.get("current_zone") or fc.DEFAULT_ZONE),
                    opponent=dict(result.wild_buddy),
                )
                # Surface the slot warning at fight-start so the player
                # knows up-front that a winning capture won't drop into
                # their shelter.
                await _maybe_warn_full_slots(
                    ctx, surface="fishing", phase="fight_start",
                )

            # Pull the live LURE oracle so the result embed can render
            # USD next to every LURE payout (sells-for, salvage, money
            # bag, mystery box, egg fallback). Best-effort; the embed
            # degrades to LURE-only when the oracle hasn't been seeded.
            lure_oracle, _ = await _oracle_pair(ctx)
            try:
                final_embed = _result_embed(
                    result, member=ctx.author, state_after=after,
                    lure_oracle=lure_oracle,
                )
                # Wild battle takes precedence -- attaches its own
                # Challenge view + Capture / Flee buttons to the same
                # message. Otherwise we attach a persistent
                # CastResultView with Cast Again + Bump so the panel
                # stays useful and never auto-disappears.
                if wild_view is not None:
                    next_view: discord.ui.View | None = wild_view
                else:
                    next_view = _CastResultView(self, ctx)
                    # Hydrate + attach the bait dropdown before sending
                    # so the player can swap bait directly from the
                    # cast-result panel without opening ,fish stats.
                    try:
                        sel = await _CastResultBaitSelect.build(
                            int(ctx.author.id), ctx,
                        )
                        next_view.add_item(sel)
                    except Exception:
                        log.debug(
                            "cast-result bait select hydrate failed",
                            exc_info=True,
                        )
                if view.message:
                    await view.message.edit(
                        embed=final_embed,
                        view=next_view,
                    )
                    if isinstance(next_view, _WildBattleView):
                        next_view.message = view.message
                    elif isinstance(next_view, _CastResultView):
                        next_view.message = view.message
            except Exception:
                log.debug("fishing: result edit failed", exc_info=True)

            # Public splash + bus events fan-out.
            await self._post_splash(ctx, result)
            try:
                await fish_svc.fire_catch_events(self.bot, ctx.guild, ctx.author, result)
            except Exception:
                log.debug("fishing: fire_catch_events failed", exc_info=True)

    # -- ,fish stats ------------------------------------------------------

    @fish.command(name="stats", aliases=["profile", "panel"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_stats(self, ctx: DiscoContext, member: discord.Member | None = None) -> None:
        """Show your tackle box (or someone else's)."""
        target = member or ctx.author
        state = await fish_svc.ensure_state(ctx.db, ctx.guild_id, target.id)
        lure_balance = to_human(
            await fish_svc.get_lure_wallet_raw(ctx.db, ctx.guild_id, target.id)
        )
        reel_balance = to_human(
            await fish_svc.get_reel_wallet_raw(ctx.db, ctx.guild_id, target.id)
        )
        lure_staked = to_human(int(state.get("lure_staked_raw") or 0))
        pending_reel = to_human(
            await fish_svc.accrued_stake_yield(ctx.db, ctx.guild_id, target.id)
        )
        lp_row = await ctx.db.get_price(fc.LURE_SYMBOL, ctx.guild_id)
        rp_row = await ctx.db.get_price(fc.REEL_SYMBOL, ctx.guild_id)
        lure_oracle = float(lp_row["price"]) if lp_row else 0.0
        reel_oracle = float(rp_row["price"]) if rp_row else 0.0

        # Surface any LP positions (user-created pools) whose pool holds
        # LURE or REEL. Pulled best-effort: if the user has none, the
        # field is skipped entirely so the embed stays tight.
        lp_lines: list[str] = []
        try:
            positions = await ctx.db.get_user_lp_positions(target.id, ctx.guild_id)
            for lp in positions:
                ta, tb = lp["token_a"], lp["token_b"]
                if fc.LURE_SYMBOL not in (ta, tb) and fc.REEL_SYMBOL not in (ta, tb):
                    continue
                total_lp = int(lp["total_lp"]) if lp["total_lp"] else 0
                if total_lp <= 0:
                    continue
                frac = int(lp["lp_shares"]) / total_lp
                val_a = to_human(int(lp["reserve_a"])) * frac
                val_b = to_human(int(lp["reserve_b"])) * frac
                pa = await ctx.db.get_price(ta, ctx.guild_id)
                pb = await ctx.db.get_price(tb, ctx.guild_id)
                price_a = float(pa["price"]) if pa else 0.0
                price_b = float(pb["price"]) if pb else 0.0
                usd_val = val_a * price_a + val_b * price_b
                lp_lines.append(
                    f"**{ta}/{tb}**: {val_a:,.4f} {ta} + {val_b:,.4f} {tb} "
                    f"≈ **{fmt_usd(usd_val)}**"
                )
        except Exception:
            log.debug("fish stats: lp lookup failed", exc_info=True)

        embed = _stats_embed(
            dict(state),
            member=target,
            lure_balance=lure_balance,
            reel_balance=reel_balance,
            lure_staked=lure_staked,
            pending_reel=pending_reel,
            lure_oracle=lure_oracle,
            reel_oracle=reel_oracle,
            lp_lines=lp_lines,
        )
        # Interactive view only when looking at YOUR OWN tackle box
        # (the bait equip + cast actions only make sense for self).
        if target.id == ctx.author.id:
            view = _FishStatsView(self, ctx, target)
            view.add_item(_BaitEquipSelect(state, state.get("equipped_bait")))
            msg = await ctx.reply(
                embed=embed, view=view, mention_author=False,
            )
            view.message = msg
        else:
            await ctx.send_embed(embed)

    # -- ,fish inv --------------------------------------------------------

    @fish.command(name="inv", aliases=["inventory", "bag", "tacklebox"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_inv(self, ctx: DiscoContext) -> None:
        """List the fish, junk, and bait you're holding."""
        state = await fish_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        summary = fish_svc.inventory_summary(dict(state))
        lure_oracle, _ = await _oracle_pair(ctx)
        result = _inventory_embed(
            ctx.author, summary, lure_oracle=lure_oracle,
        )
        # _inventory_embed returns a single embed when the haul fits, or a
        # list of embeds when chunking is required (1024-char-per-field /
        # 6000-char-per-embed caps). Route accordingly.
        if isinstance(result, list):
            await ctx.paginate(result)
        else:
            await ctx.send_embed(result)

    # -- ,fish history ----------------------------------------------------

    @fish.command(name="history", aliases=["log", "recent"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_history(self, ctx: DiscoContext) -> None:
        """Your last few catches."""
        rows = await fish_svc.get_user_recent(ctx.db, ctx.guild_id, ctx.author.id, limit=10)
        lure_oracle, _ = await _oracle_pair(ctx)
        await ctx.send_embed(_history_embed(
            ctx.author, rows, lure_oracle=lure_oracle,
        ))

    # -- ,fish shop -------------------------------------------------------

    @fish.command(name="shop", aliases=["store"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_shop(self, ctx: DiscoContext) -> None:
        """Browse rod tiers and bait prices."""
        state = await fish_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        reel_balance = to_human(
            await fish_svc.get_reel_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        )
        lure_oracle, reel_oracle = await _oracle_pair(ctx)
        view = QuickBuyView(
            ctx=ctx,
            command_template="fish buy {item}",
            accepted_currency=fc.REEL_SYMBOL,
            item_label="What to buy",
            item_placeholder="rod | worm 50 | minnow 20 | wire 5",
            modal_title=f"Fish Quick Buy ({fc.REEL_SYMBOL})",
        )
        sent = await ctx.reply(
            embed=_shop_embed(
                dict(state),
                reel_balance=reel_balance,
                reel_oracle=reel_oracle,
                lure_oracle=lure_oracle,
            ),
            view=view,
            mention_author=False,
        )
        view.message = sent

    # -- ,fish buy --------------------------------------------------------

    @fish.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_buy(self, ctx: DiscoContext, item: str, qty: int = 1) -> None:
        """Buy a rod upgrade (`rod`/`upgrade`) or bait (key + qty)."""
        item = (item or "").strip().lower()

        # Rod path: synonyms `rod`, `upgrade`, or `rod<tier>`.
        if item in ("rod", "upgrade"):
            state = await fish_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
            tgt = int(state.get("rod_tier") or 0) + 1
            try:
                state, impact = await fish_svc.buy_rod(
                    ctx.db, ctx.guild_id, ctx.author.id, tgt,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            r = fc.rod_meta(int(state.get("rod_tier") or 0))
            msg = (
                f"{r['emoji']} Upgraded to **{r['name']}** "
                f"(tier {state.get('rod_tier')}). _{r['blurb']}_"
            )
            msg += "\n" + _gear_impact_lines(impact)
            await ctx.reply_success(msg, title="Rod Upgraded")
            return

        # Bait path. The catalog key is canonical (snake_case).
        if item in fc.BAIT:
            try:
                state, impact, actual = await fish_svc.buy_bait(
                    ctx.db, ctx.guild_id, ctx.author.id, item, qty,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            b = fc.BAIT[item]
            owned = int(_jsonb_dict(state.get("bait_inventory")).get(item, 0))
            msg = (
                f"{b['emoji']} Bought **{actual}× {b['name']}**. "
                f"You now hold **{owned}**."
            )
            if actual < qty:
                msg += f" (capped from {qty} at the stack max)"
            msg += "\n" + _gear_impact_lines(impact)
            await ctx.reply_success(msg, title="Tackle Stocked")
            return

        # Crab trap path. Same buy semantics as bait -- REEL burn,
        # gear-spend chart impact, max-stack cap.
        if item in fc.CRAB_TRAPS:
            try:
                state, impact, actual = await fish_svc.buy_crab_trap(
                    ctx.db, ctx.guild_id, ctx.author.id, item, qty,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            t = fc.CRAB_TRAPS[item]
            owned = int(_jsonb_dict(state.get("crab_trap_inventory")).get(item, 0))
            msg = (
                f"{t['emoji']} Bought **{actual}× {t['name']}**. "
                f"You now hold **{owned}** undeployed.\n"
                f"-# Place them with `,fish trap place {item} {actual}`."
            )
            if actual < qty:
                msg += f"\n(capped from {qty} at the stack max)"
            msg += "\n" + _gear_impact_lines(impact)
            await ctx.reply_success(msg, title="Trap Stocked")
            return

        await ctx.reply_error_hint(
            f"Unknown item `{item}`.",
            hint="fish buy rod  -  fish buy worm 50  -  fish buy wire 5",
            command_name="fish buy",
        )

    # -- ,fish bait -------------------------------------------------------

    @fish.command(name="bait", aliases=["equip"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_bait(self, ctx: DiscoContext, key: str | None = None) -> None:
        """Equip a bait by key, or pass `none` to unequip."""
        if key is None:
            state = await fish_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
            cur = state.get("equipped_bait")
            if cur:
                b = fc.BAIT.get(cur, {"emoji": "", "name": cur})
                await ctx.send_embed(card(
                    "Equipped Bait",
                    description=f"{b.get('emoji', '')} **{b.get('name', cur)}** is on the hook.",
                    color=C_INFO,
                ).build())
            else:
                await ctx.send_embed(card(
                    "Equipped Bait",
                    description="No bait equipped. Try `,fish bait worm`.",
                    color=C_INFO,
                ).build())
            return
        try:
            state = await fish_svc.set_bait(ctx.db, ctx.guild_id, ctx.author.id, key)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        cur = state.get("equipped_bait")
        if cur:
            b = fc.BAIT[cur]
            await ctx.reply_success(
                f"{b['emoji']} Equipped **{b['name']}**.", title="Bait On",
            )
        else:
            await ctx.reply_success("Cleared bait.", title="Bait Off")

    # -- ,fish zone / ,fish zones ----------------------------------------

    @fish.command(name="zones")
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_zones(self, ctx: DiscoContext) -> None:
        """List every fishing zone and which ones you can enter."""
        state = await fish_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        await ctx.send_embed(_zones_embed(dict(state)))

    @fish.command(name="zone", aliases=["go"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_zone(self, ctx: DiscoContext, key: str) -> None:
        """Switch fishing zones."""
        try:
            state = await fish_svc.set_zone(ctx.db, ctx.guild_id, ctx.author.id, key.lower())
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        z = fc.zone_meta(str(state.get("current_zone")))
        await ctx.reply_success(
            f"{z['emoji']} Heading to **{z['name']}**.\n_{z['blurb']}_",
            title="Zone Set",
        )

    # -- ,fish sell -------------------------------------------------------

    @fish.command(name="sell")
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_sell(self, ctx: DiscoContext, target: str = "all", amount: str = "1") -> None:
        """Sell caught fish, rods, or crab traps.

        Targets:
          ``all`` / ``junk`` / ``<fish_key>`` -- inventory sells (LURE)
          ``rod``                              -- downgrade by 1 tier (REEL)
          ``traps`` / ``pots``                 -- bulk-sell every crab trap (REEL)
          ``trap`` / ``<trap_key> [qty|all]``  -- sell a specific trap stack (REEL)

        Rod + trap sells return 50% of the original REEL price.
        """
        t = (target or "all").lower()

        # ── Rod sell (downgrade by 1 tier, 50% refund in REEL) ──────────────
        if t == "rod":
            try:
                refund, sold_name, new_name = await fish_svc.sell_rod(
                    ctx.db, ctx.guild_id, ctx.author.id,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            await ctx.reply_success(
                f"Sold **{sold_name}** for **{refund:,.4f} REEL** (50% back).\n"
                f"You're now using the **{new_name}**.",
                title="Rod Sold",
            )
            return

        # ── Trap sell (from crab_trap_inventory, 50% refund in REEL) ────────
        # ``traps`` / ``pots`` (plural) sells EVERY crab trap in inventory
        # in one call -- the bulk-sell shortcut. ``trap`` / ``crab`` /
        # ``pot`` (singular) without a key falls back to the picker hint.
        if t in ("traps", "pots", "allgear", "all_gear"):
            try:
                refund_total, sold_map = await fish_svc.sell_all_traps(
                    ctx.db, ctx.guild_id, ctx.author.id,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            lines = []
            for k, q in sold_map.items():
                m = fc.crab_trap_meta(k) or {}
                lines.append(f"{m.get('emoji', '')} **{q}x {m.get('name', k)}**")
            await ctx.reply_success(
                "\n".join(lines)
                + f"\n\nRefunded **{refund_total:,.4f} REEL** (50% back).",
                title="All Crab Traps Sold",
            )
            return
        if t in ("trap", "crab", "pot") or t in fc.CRAB_TRAPS:
            trap_key = t if t in fc.CRAB_TRAPS else None
            if trap_key is None:
                trap_keys = ", ".join(f"`{k}`" for k in fc.CRAB_TRAPS)
                await ctx.reply_error_hint(
                    "Specify which trap to sell, or use `traps` to sell them all.",
                    hint=(
                        f"fish sell <trap_key> [qty|all]  --  keys: {trap_keys}\n"
                        f"fish sell traps  --  bulk-sell every trap you own"
                    ),
                    command_name="fish sell",
                )
                return
            # ``all`` selects the entire owned stack of this trap key.
            if str(amount).lower() in ("all", "everything", "max"):
                qty = 10**9  # sell_trap clamps to owned
            else:
                try:
                    qty = max(1, int(amount))
                except (ValueError, TypeError):
                    qty = 1
            try:
                refund, sold_qty = await fish_svc.sell_trap(
                    ctx.db, ctx.guild_id, ctx.author.id, trap_key, qty,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            meta = fc.crab_trap_meta(trap_key) or {}
            await ctx.reply_success(
                f"Sold **{sold_qty}x {meta.get('name', trap_key)}** for "
                f"**{refund:,.4f} REEL** (50% back).",
                title="Trap Sold",
            )
            return

        # ── Fish / junk inventory sell (existing paths, paid in LURE) ───────
        try:
            if t in ("all", "everything"):
                count, lure = await fish_svc.sell_inventory(ctx.db, ctx.guild_id, ctx.author.id)
            elif t in ("junk", "trash"):
                count, lure = await fish_svc.sell_inventory(
                    ctx.db, ctx.guild_id, ctx.author.id, junk_only=True,
                )
            elif t in fc.FISH:
                count, lure = await fish_svc.sell_inventory(
                    ctx.db, ctx.guild_id, ctx.author.id, fish_key=t,
                )
            else:
                await ctx.reply_error_hint(
                    f"Don't know how to sell `{target}`.",
                    hint=(
                        "fish sell all  -  fish sell junk  -  "
                        "fish sell rod  -  fish sell traps (all crab pots)\n"
                        "fish sell wire 3  -  fish sell wire all  -  "
                        "fish sell <fish_key>"
                    ),
                    command_name="fish sell",
                )
                return
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if count == 0:
            await ctx.reply_error("Nothing to sell.")
            return
        lure_oracle, _ = await _oracle_pair(ctx)
        await ctx.reply_success(
            f"Sold **{count}** items for **{_fmt_lure(lure)}**"
            f"{_with_usd(float(lure), lure_oracle)}. Tackle bag updated.\n"
            f"{_MINT_FOOTER}",
            title="Catch Sold",
        )

    # -- ,fish lb --------------------------------------------------------

    @fish.command(name="lb", aliases=["leaderboard", "top"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_lb(self, ctx: DiscoContext, kind: str = "payout") -> None:
        """Top fishers by lifetime payout (default) or `biggest` for trophies."""
        from core.framework.leaderboard import filter_lb_user_ids
        k = (kind or "payout").lower()
        if k in ("biggest", "trophy", "trophies", "weight"):
            rows = await fish_svc.get_biggest_catches(ctx.db, ctx.guild_id, limit=50)
            keep = await filter_lb_user_ids(
                ctx, [int(r.get("user_id") or 0) for r in rows],
            )
            rows = [r for r in rows if int(r.get("user_id") or 0) in keep][:10]
            await ctx.send_embed(_leaderboard_embed(rows, ctx.guild, kind="biggest"))
        else:
            rows = await fish_svc.get_top_fishers(ctx.db, ctx.guild_id, limit=50)
            keep = await filter_lb_user_ids(
                ctx, [int(r.get("user_id") or 0) for r in rows],
            )
            rows = [r for r in rows if int(r.get("user_id") or 0) in keep][:10]
            lure_oracle, _ = await _oracle_pair(ctx)
            await ctx.send_embed(_leaderboard_embed(
                rows, ctx.guild, kind="payout", lure_oracle=lure_oracle,
            ))

    # -- ,fish trap (group) ----------------------------------------------

    @fish.group(
        name="trap", aliases=["traps", "pot", "pots"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_trap(self, ctx: DiscoContext) -> None:
        """Interactive crab-trap panel: place, collect, and monitor traps.

        Subcommands:
            ``,fish trap place <key> [qty]``   -- deploy traps in this zone
            ``,fish trap collect``             -- haul every soaked trap
            ``,fish trap buy <key> [qty]``     -- alias of ,fish buy <key>
        """
        state = await fish_svc.ensure_state(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        summary = fish_svc.trap_status_summary(dict(state))
        lure_oracle, _ = await _oracle_pair(ctx)
        view = TrapView(ctx, self)
        inv = _jsonb_dict(state.get("crab_trap_inventory"))
        view.add_item(_TrapPickSelect(inv))
        embed = _trap_status_embed(
            ctx.author, dict(state), summary, lure_oracle=lure_oracle,
        )
        view._update_button_states(summary)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    async def _fish_trap_place_all(self, ctx: DiscoContext) -> None:
        """Place all owned traps eligible for the current zone."""
        uid = ctx.author.id
        gid = ctx.guild_id

        state = await fish_svc.ensure_state(ctx.db, gid, uid)
        zone_key = str(state.get("current_zone") or fc.DEFAULT_ZONE)
        zone = fc.zone_meta(zone_key)
        zone_tier = int(zone.get("tier") or 0)

        inv = _jsonb_dict(state.get("crab_trap_inventory"))
        placed_now = len(list(state.get("placed_crab_traps") or []))
        room = max(0, fc.CRAB_TRAP_PLACED_CAP - placed_now)

        # Sort highest tier first -- best traps get priority on the cap.
        eligible: list[tuple[str, dict, int]] = []
        skipped: list[dict] = []
        for k, cfg in sorted(
            fc.CRAB_TRAPS.items(),
            key=lambda kv: int(kv[1].get("max_zone_tier") or 0),
            reverse=True,
        ):
            owned = int(inv.get(k, 0))
            if owned <= 0:
                continue
            if zone_tier > int(cfg.get("max_zone_tier") or 0):
                skipped.append(cfg)
            else:
                eligible.append((k, cfg, owned))

        if not eligible and not skipped:
            await ctx.reply_error("You don't have any traps in your inventory.")
            return
        if not eligible:
            skip_names = ", ".join(f"{c['emoji']} **{c['name']}**" for c in skipped)
            await ctx.reply_error(
                f"None of your traps can be placed in {zone['emoji']} **{zone['name']}** "
                f"(zone tier {zone_tier}).\nToo low tier: {skip_names}"
            )
            return
        if room <= 0:
            await ctx.reply_error(
                f"You already have the maximum **{fc.CRAB_TRAP_PLACED_CAP}** traps placed. "
                f"Collect them first with `,fish trap collect`."
            )
            return

        placed_lines: list[str] = []
        cap_hit = False
        for k, cfg, owned in eligible:
            try:
                _, n = await fish_svc.place_crab_traps(ctx.db, gid, uid, k, owned)
                soak_min = int(cfg["soak_seconds"]) // 60
                placed_lines.append(
                    f"{cfg['emoji']} **{n}x {cfg['name']}** -- {soak_min} min soak"
                )
            except ValueError as exc:
                msg = str(exc)
                if "maximum" in msg:
                    cap_hit = True
                    break
                skipped.append(cfg)

        if not placed_lines:
            await ctx.reply_error("No traps could be placed -- check the cap or your inventory.")
            return

        desc_parts = [
            f"```\n{fc.FRAMES['trap_drop']}\n```",
            f"Placed in {zone['emoji']} **{zone['name']}** (tier {zone_tier}):\n",
            "\n".join(placed_lines),
        ]
        if cap_hit:
            desc_parts.append(
                f"\n-# Cap of **{fc.CRAB_TRAP_PLACED_CAP}** traps reached -- "
                f"remaining traps left in inventory."
            )
        if skipped:
            skip_names = ", ".join(f"{c['emoji']} {c['name']}" for c in skipped)
            desc_parts.append(f"-# Skipped (zone too high for trap): {skip_names}")
        desc_parts.append("-# Collect with `,fish trap collect` when ready.")

        await ctx.send_embed(card(
            "All Traps Set", description="\n".join(desc_parts), color=C_TEAL,
        ).build())

    @fish_trap.command(name="place", aliases=["set", "drop", "deploy"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_trap_place(
        self, ctx: DiscoContext, key: str, qty: int = 1,
    ) -> None:
        """Place traps in your current zone. Use ``all`` as key to deploy every eligible trap type at once."""
        k = (key or "").strip().lower()
        if k == "all":
            await self._fish_trap_place_all(ctx)
            return
        if k not in fc.CRAB_TRAPS:
            await ctx.reply_error_hint(
                f"Unknown trap `{key}`.",
                hint="fish trap place wire 3  -  fish trap place steel  -  fish trap place all",
                command_name="fish trap place",
            )
            return
        try:
            state, placed = await fish_svc.place_crab_traps(
                ctx.db, ctx.guild_id, ctx.author.id, k, qty,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        t = fc.CRAB_TRAPS[k]
        z = fc.zone_meta(str(state.get("current_zone") or fc.DEFAULT_ZONE))
        soak_min = int(t["soak_seconds"]) // 60
        desc = (
            f"```\n{fc.FRAMES['trap_drop']}\n```\n"
            f"{t['emoji']} Dropped **{placed}× {t['name']}** in "
            f"{z['emoji']} **{z['name']}**.\n"
            f"-# Soak time **{soak_min} min** -- come back with "
            f"`,fish trap collect`."
        )
        await ctx.send_embed(card(
            "Trap Set", description=desc, color=C_TEAL,
        ).build())

    @fish_trap.command(name="collect", aliases=["haul", "pull", "check"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_trap_collect(self, ctx: DiscoContext) -> None:
        """Pull every soaked trap and pay out the haul."""
        try:
            res = await fish_svc.collect_crab_traps(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # Empty haul -- nothing was ready yet.
        if res.traps_collected == 0:
            state = await fish_svc.list_state(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            summary = fish_svc.trap_status_summary(dict(state))
            if summary["placed_total"] == 0:
                await ctx.reply_error(
                    "No traps placed. Set some with "
                    "`,fish trap place <key> [qty]`."
                )
                return
            # Find the trap closest to ready so we can give a helpful hint.
            soonest = min(
                (r for r in summary["rows"] if not r["ready"]),
                key=lambda r: int(r["ready_in_s"]),
                default=None,
            )
            wait_s = int((soonest or {}).get("ready_in_s") or 0)
            await ctx.reply_error(
                f"Nothing's soaked yet. **{summary['placed_total']}** trap(s) "
                f"still soaking; next ready in **{_fmt_eta(wait_s)}**."
            )
            return

        lure_oracle, _ = await _oracle_pair(ctx)
        # Per-trap haul detail. Crabs come in as fish_inventory entries
        # so they sell through the normal ,fish sell path. Each row
        # quotes the trap's LURE haul plus its USD equivalent at the
        # live oracle so users can compare runs across price moves.
        trap_lines = []
        for h in res.per_trap_haul[:10]:
            t = fc.CRAB_TRAPS.get(h["key"]) or {}
            z = fc.zone_meta(h["zone"])
            crab_emojis = " ".join(
                (fc.fish_meta(c) or {}).get("emoji", "") for c in h["crabs"]
            )
            trap_lines.append(
                f"{t.get('emoji', '')} **{t.get('name', h['key'])}** in "
                f"{z['emoji']} {z['name']}  -  "
                f"{_fmt_lure(h['lure'])}{_with_usd(h['lure'], lure_oracle)}  "
                f"{crab_emojis}"
            )
        if len(res.per_trap_haul) > 10:
            trap_lines.append(f"-# ...and {len(res.per_trap_haul) - 10} more.")
        crab_lines = [
            f"{(fc.fish_meta(k) or {}).get('emoji', '')} "
            f"**{(fc.fish_meta(k) or {}).get('name', k)}** x{n}"
            for k, n in res.crabs_added.items()
        ]
        desc = (
            f"```\n{fc.FRAMES['trap_haul']}\n```\n"
            f"Hauled **{res.traps_collected}** trap(s) -- "
            f"**{_fmt_lure(res.lure_paid)}**"
            f"{_with_usd(res.lure_paid, lure_oracle)} into your tackle bag.\n"
            f"{_MINT_FOOTER}"
        )
        if res.leftover_traps > 0:
            desc += (
                f"\n-# **{res.leftover_traps}** trap(s) still soaking. "
                f"Check back later with `,fish trap`."
            )
        embed = (
            card("\U0001F980 Crab Haul", color=C_TEAL)
            .description(desc)
            .field("Per Trap", "\n".join(trap_lines), False)
            .field(
                f"Crabs Added ({sum(res.crabs_added.values())})",
                "\n".join(crab_lines) if crab_lines else "_(none)_",
                False,
            )
            .footer("Sell crabs the usual way: `,fish sell` or `,fish sell <crab_key>`.")
            .build()
        )
        await ctx.send_embed(embed)

    @fish_trap.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_trap_buy(
        self, ctx: DiscoContext, key: str, qty: int = 1,
    ) -> None:
        """Alias of ``,fish buy <trap_key> <qty>`` for muscle-memory."""
        await self.fish_buy(ctx, key, qty)

    @fish_trap.command(name="shop")
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_trap_shop(self, ctx: DiscoContext) -> None:
        """Alias of ``,fish shop`` -- the trap section lives there."""
        await self.fish_shop(ctx)

    # -- ,fish egg (group) -----------------------------------------------

    @fish.command(
        name="egg", aliases=["eggs"],
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_egg(self, ctx: DiscoContext, *, _ignored: str = "") -> None:
        """Sunset: held-egg ops moved to ``,buddy egg``.

        Eggs are buddy items, so all of hatch / sell / gift / list /
        deposit / withdraw / panel live under the buddy surface now.
        This stub points at the new home so old muscle memory still
        works.
        """
        prefix = await ctx.get_guild_prefix()
        await ctx.send_embed(
            card(
                "\U0001F95A Eggs Moved to `,buddy egg`",
                color=C_INFO,
                description=(
                    "Held + banked eggs consolidated under the buddy "
                    "surface so there's one place to manage them.\n\n"
                    f"**Panel:** `{prefix}buddy egg`\n"
                    f"**Hatch:** `{prefix}buddy egg hatch [species]`\n"
                    f"**Sell:** `{prefix}buddy egg sell [count|all|<species>]`\n"
                    f"**Gift:** `{prefix}buddy egg gift @user [species] [count]`\n"
                    f"**Deposit / Withdraw:** "
                    f"`{prefix}buddy egg deposit` / "
                    f"`{prefix}buddy egg withdraw`\n"
                    f"**Auction House:** `{prefix}ah list egg <idx|species[:tier]> <price>`"
                ),
            ).build()
        )

    # -- ,fish dig -------------------------------------------------------

    @fish.command(name="dig", aliases=["treasure", "excavate"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_dig(self, ctx: DiscoContext) -> None:
        """Consume one Soggy Treasure Map and roll a treasure outcome.

        Outcomes range from a small LURE cache to a legendary
        "Ancient Relic" jackpot fish that drops at max weight straight
        into your sellable inventory. All payouts are mints (no oracle
        impact).
        """
        try:
            res = await fish_svc.dig_treasure_map(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # Brief "unfolds the map" pre-frame so the reveal lands with
        # a beat. Same send-then-edit pattern the cast view uses.
        pre_msg = None
        try:
            pre_msg = await ctx.reply(embed=card(
                "🪙 Following the X...",
                description=f"```\n{fc.FRAMES['dig_start']}\n```",
                color=C_AMBER,
            ).build(), mention_author=False)
            await asyncio.sleep(0.8)
        except Exception:
            log.debug("fishing: dig pre-frame send failed", exc_info=True)
        lure_oracle, reel_oracle = await _oracle_pair(ctx)

        # Pick the right ASCII frame: empty hole if literally nothing
        # was credited, treasure chest otherwise. Frame stays in the
        # description so the embed reads as one continuous moment.
        nothing = (
            res.lure_credited == 0
            and res.reel_credited == 0
            and res.bait_added is None
            and res.trap_added is None
            and res.egg_added is None
            and res.fish_added is None
        )
        frame = fc.FRAMES["dig_empty" if nothing else "dig_chest"]

        # Per-outcome detail line. The dataclass keeps everything flat
        # so this is a single switch -- no follow-up DB reads needed.
        detail_lines: list[str] = []
        if res.lure_credited > 0:
            detail_lines.append(
                f"💰 **{_fmt_lure(res.lure_credited)}**"
                f"{_with_usd(res.lure_credited, lure_oracle)}"
            )
        if res.reel_credited > 0:
            detail_lines.append(
                f"🎣 **{_fmt_reel(res.reel_credited)}**"
                f"{_with_usd(res.reel_credited, reel_oracle)}"
            )
        if res.bait_added:
            bk, n = res.bait_added
            b = fc.BAIT.get(bk) or {}
            detail_lines.append(
                f"{b.get('emoji', '')} **{n}× {b.get('name', bk)}** bait"
            )
        if res.trap_added:
            tk, n = res.trap_added
            t = fc.CRAB_TRAPS.get(tk) or {}
            detail_lines.append(
                f"{t.get('emoji', '')} **{n}× {t.get('name', tk)}** trap(s)"
            )
        if res.egg_added:
            sp = str(res.egg_added.get("species") or "?")
            tier = int(res.egg_added.get("rarity_tier") or 1)
            try:
                from configs.buddies_config import SPECIES, rarity_meta as _b_rarity
                emoji = str((SPECIES.get(sp) or {}).get("emoji") or "\U0001F95A")
                tier_name = str(_b_rarity(tier).get("name") or "Common")
            except Exception:
                emoji, tier_name = "\U0001F95A", "Common"
            sell_lure = fc.egg_sell_lure(tier)
            detail_lines.append(
                f"{emoji} **{tier_name} {sp.title()} Egg**  -  "
                f"sells for **{_fmt_lure(sell_lure)}**"
                f"{_with_usd(sell_lure, lure_oracle)}"
            )
        if res.fish_added:
            fk, lbs = res.fish_added
            spec = fc.FISH.get(fk) or {}
            emoji = str(spec.get("emoji") or "\U0001F420")
            name = str(spec.get("name") or fk)
            sells = fc.fish_payout(
                fk, lbs, combo_mult=1.0, quality_mult=1.0,
                zone=fc.DEFAULT_ZONE,
            )
            detail_lines.append(
                f"✨ {emoji} **{name}** at **{lbs:,.2f} lbs**  -  "
                f"sells for **{_fmt_lure(sells)}**"
                f"{_with_usd(sells, lure_oracle)}"
            )

        title = "🪙 " + (res.label if not nothing else "Just a Muddy Hole")
        color = (
            C_GOLD if res.outcome_key == "ancient_relic"
            else C_PURPLE if res.outcome_key == "wild_egg"
            else C_TEAL if not nothing
            else C_NEUTRAL
        )
        # Inflation-style chart impact -- minting LURE/REEL out of thin
        # air pushes the respective oracle DOWN. Render the move on the
        # receipt so the dig stays consistent with the rest of the
        # economy: every credit / debit shows its chart footprint.
        impact_lines: list[str] = []
        for label_, impact in (("LURE", res.lure_impact), ("REEL", res.reel_impact)):
            if impact is None:
                continue
            impact_lines.append(
                f"-# {label_} oracle: **${impact.oracle_before:,.6f} -> "
                f"${impact.oracle_after:,.6f}** "
                f"(inflation **-{impact.price_impact_pct * 100:.2f}%**)"
            )
        desc = (
            f"```\n{frame}\n```\n"
            + ("\n".join(detail_lines) if detail_lines
               else "Just dirt and worms. The map was a fake.")
        )
        if impact_lines:
            desc += "\n" + "\n".join(impact_lines)
        desc += f"\n-# **{res.leftover_maps}** map(s) remaining."
        final_embed = card(title, description=desc, color=color).build()
        # If the pre-frame went out, edit it in place so the dig reads
        # as one continuous beat. If the pre-send failed (perms, etc.)
        # we fall back to a plain reply so the user still sees the haul.
        if pre_msg is not None:
            try:
                await pre_msg.edit(embed=final_embed)
                return
            except Exception:
                log.debug("fishing: dig final edit failed", exc_info=True)
        await ctx.send_embed(final_embed)

    # -- ,fish beachcomb -------------------------------------------------

    @fish.command(name="beachcomb", aliases=["comb", "shore", "scavenge", "wander"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_beachcomb(self, ctx: DiscoContext) -> None:
        """Wander the shoreline for a randomized payout.

        Free roll every 10 minutes. Drops range from a small LURE / REEL
        purse up to a stash of bait, an occasional Soggy Treasure Map
        (which feeds back into ``,fish dig``), or the rare Ancient Relic
        jackpot -- a max-weight legendary fish straight to your inventory.
        Mirrors ``,farm forage`` in shape and feel.
        """
        try:
            res = await fish_svc.beachcomb(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # Pre-frame so the reveal lands with a beat -- same send-then-edit
        # cadence the dig and cast views use.
        pre_msg = None
        try:
            pre_msg = await ctx.reply(embed=card(
                "🏖️ Combing the shoreline...",
                description=f"```\n{fc.FRAMES['beachcomb_start']}\n```",
                color=C_AMBER,
            ).build(), mention_author=False)
            await asyncio.sleep(0.8)
        except Exception:
            log.debug("fishing: beachcomb pre-frame send failed", exc_info=True)
        lure_oracle, reel_oracle = await _oracle_pair(ctx)

        # Pick the right ASCII frame for the reveal.
        frame_key = {
            "lure_purse_small":  "beachcomb_lure_purse",
            "lure_purse_big":    "beachcomb_lure_purse",
            "reel_kicker_small": "beachcomb_reel_kicker",
            "reel_kicker_big":   "beachcomb_reel_kicker",
            "bait_stash":        "beachcomb_bait_stash",
            "treasure_map":      "beachcomb_treasure_map",
            "ancient_relic":     "beachcomb_jackpot",
            "empty":             "beachcomb_empty",
        }.get(res.outcome_key, "beachcomb_empty")
        frame = fc.FRAMES.get(frame_key, "")

        # Per-outcome detail lines. Same flat-dataclass + single switch
        # pattern as ,fish dig and ,farm forage.
        detail_lines: list[str] = []
        if res.lure_credited > 0:
            detail_lines.append(
                f"💰 **{_fmt_lure(res.lure_credited)}**"
                f"{_with_usd(res.lure_credited, lure_oracle)}"
            )
        if res.reel_credited > 0:
            detail_lines.append(
                f"🎣 **{_fmt_reel(res.reel_credited)}**"
                f"{_with_usd(res.reel_credited, reel_oracle)}"
            )
        for bk, n in res.baits_added:
            b = fc.BAIT.get(bk) or {}
            detail_lines.append(
                f"{b.get('emoji', '')} **{n}× {b.get('name', bk)}** bait"
            )
        if res.maps_added > 0:
            detail_lines.append(
                f"🗺 **{res.maps_added}× Soggy Treasure Map** "
                f"(use with `{ctx.prefix}fish dig`)"
            )
        if res.fish_added:
            fk, lbs = res.fish_added
            spec = fc.FISH.get(fk) or {}
            emoji = str(spec.get("emoji") or "\U0001F420")
            name = str(spec.get("name") or fk)
            sells = fc.fish_payout(
                fk, lbs, combo_mult=1.0, quality_mult=1.0, zone=fc.DEFAULT_ZONE,
            )
            detail_lines.append(
                f"✨ {emoji} **{name}** at **{lbs:,.2f} lbs**  -  "
                f"sells for **{_fmt_lure(sells)}**"
                f"{_with_usd(sells, lure_oracle)}"
            )

        title = "🏖️ " + res.label
        color = (
            C_GOLD     if res.outcome_key == "ancient_relic"
            else C_TEAL if res.outcome_key in ("bait_stash", "treasure_map")
            else C_AMBER if res.outcome_key.startswith(("lure_", "reel_"))
            else C_NEUTRAL
        )

        # Inflation-style oracle move (same as ,fish dig). Mint of LURE
        # or REEL out of thin air pushes the respective oracle DOWN.
        impact_lines: list[str] = []
        for lbl, impact in (("LURE", res.lure_impact), ("REEL", res.reel_impact)):
            if impact is None:
                continue
            impact_lines.append(
                f"-# {lbl} oracle: **${impact.oracle_before:,.6f} -> "
                f"${impact.oracle_after:,.6f}** "
                f"(inflation **-{impact.price_impact_pct * 100:.2f}%**)"
            )

        desc = (
            f"```\n{frame}\n```\n"
            + ("\n".join(detail_lines) if detail_lines
               else "_The shore only had stickers and seaweed for you._")
        )
        if impact_lines:
            desc += "\n" + "\n".join(impact_lines)
        desc += f"\n-# Next beachcomb in {fc.BEACHCOMB_COOLDOWN_S // 60}m."

        final_embed = card(title, description=desc, color=color).build()
        if pre_msg is not None:
            try:
                await pre_msg.edit(embed=final_embed)
                return
            except Exception:
                log.debug("fishing: beachcomb final edit failed", exc_info=True)
        await ctx.send_embed(final_embed)

    # -- ,fish help ------------------------------------------------------

    # -- ,fish unstuck ---------------------------------------------------

    @fish.command(name="unstuck", aliases=["unjam", "release"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_unstuck(self, ctx: DiscoContext) -> None:
        """Force-release a wedged casting lock on your row.

        Used when a previous cast crashed and left ``is_casting=TRUE``
        in the DB so every new ``,fish`` reports "try again later".
        Safe to call when nothing is stuck -- the SQL is a no-op.
        """
        cleared = await fish_svc.force_unstuck(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if cleared:
            await ctx.reply_success(
                "Untangled your line. You can `,fish` again now.",
                title="Unstuck",
            )
        else:
            await ctx.reply_success(
                "Your line was already free. Cast away.",
                title="All Clear",
            )

    # -- ,fish help ------------------------------------------------------

    # -- ,fish swap -------------------------------------------------------

    @fish.command(name="swap")
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_swap(self, ctx: DiscoContext, amount: str) -> None:
        """Burn LURE for REEL at the instant burn rate. `all` = entire wallet."""
        s = (amount or "").strip().lower()
        if s in ("all", "everything", "max"):
            req_raw = await fish_svc.get_lure_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if req_raw <= 0:
                await ctx.reply_error("You have no LURE to swap.")
                return
        else:
            try:
                amt = float(s.replace(",", "").replace("_", ""))
            except ValueError:
                await ctx.reply_error(f"Invalid amount: `{amount}`.")
                return
            if amt <= 0:
                await ctx.reply_error("Amount must be positive.")
                return
            req_raw = to_raw(amt)
        try:
            res = await fish_svc.swap_lure_to_reel(
                ctx.db, ctx.guild_id, ctx.author.id, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await fish_svc._publish_economy_event(
            self.bot, event=fish_svc.EVENT_LURE_SWAP,
            guild_id=ctx.guild_id, user_id=ctx.author.id,
            lure_burned_raw=res.lure_burned_raw,
            reel_minted_raw=res.reel_minted_raw,
            price_impact_pct=res.price_impact_pct,
        )
        impact_pct = res.price_impact_pct * 100.0
        lure_human = to_human(res.lure_burned_raw)
        reel_human = to_human(res.reel_minted_raw)
        # Both oracles are USD-quoted, and the burn preserves USD value
        # at the live LURE oracle (the input side). Quoting the LURE
        # USD value is enough -- the REEL USD comes out within a few
        # decimals because of the post-impact average-price formula.
        usd_in = lure_human * res.lure_oracle_before
        usd_out = reel_human * res.reel_oracle_after
        msg = (
            f"Burned **{_fmt_lure(lure_human)}** ≈ **{fmt_usd(usd_in)}** -> "
            f"minted **{_fmt_reel(reel_human)}** ≈ **{fmt_usd(usd_out)}**.\n"
            f"LURE oracle: **${res.lure_oracle_before:,.6f} -> "
            f"${res.lure_oracle_after:,.6f}**\n"
            f"REEL oracle: **${res.reel_oracle_before:,.6f} -> "
            f"${res.reel_oracle_after:,.6f}**\n"
            f"-# Slippage: **{impact_pct:.2f}%**. Stake (`,fish stake`) "
            f"pays better long-term."
        )
        if res.lp_reward_usd > 0:
            msg += (
                f"\n-# Paid **{fmt_usd(res.lp_reward_usd)}** to "
                f"LURE/REEL LP holders."
            )
        await ctx.reply_success(msg, title="LURE Burned for REEL")

    # -- ,fish stake ------------------------------------------------------

    @fish.command(name="stake")
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_stake(self, ctx: DiscoContext, amount: str = "") -> None:
        """Stake LURE to passively earn REEL.

        With no amount: opens the unified stake panel (Stake / Unstake /
        Claim / Refresh buttons -- same shape as ,farm stake / ,craft
        stake / ,buddy stake / ,delve stake).
        """
        s = (amount or "").strip().lower()
        if not s:
            await self._open_stake_panel(ctx)
            return
        if s in ("all", "everything", "max"):
            req_raw = await fish_svc.get_lure_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if req_raw <= 0:
                await ctx.reply_error("You have no LURE to stake.")
                return
        else:
            try:
                amt = float(s.replace(",", "").replace("_", ""))
            except ValueError:
                await ctx.reply_error(f"Invalid amount: `{amount}`.")
                return
            if amt <= 0:
                await ctx.reply_error("Amount must be positive.")
                return
            req_raw = to_raw(amt)
        try:
            res = await fish_svc.stake_lure(
                ctx.db, ctx.guild_id, ctx.author.id, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await fish_svc._publish_economy_event(
            self.bot, event=fish_svc.EVENT_LURE_STAKE,
            guild_id=ctx.guild_id, user_id=ctx.author.id,
            lure_added_raw=res.lure_delta_raw,
            lure_staked_total_raw=res.lure_staked_raw,
        )
        from core.framework.staking import stake_receipt
        lure_oracle, _ = await _oracle_pair(ctx)
        await ctx.reply(
            embed=stake_receipt(
                action="Staked",
                stake_symbol=fc.LURE_SYMBOL, stake_emoji="\U0001FA9D",
                delta_h=to_human(res.lure_delta_raw),
                total_h=to_human(res.lure_staked_raw),
                stake_oracle=lure_oracle,
                note=(
                    f"LURE locked / unlocked -- no oracle impact (not a trade). "
                    f"Earns {fc.LURE_STAKE_REEL_PER_DAY:g} REEL per LURE per day."
                ),
            ),
            mention_author=False,
        )

    async def _open_stake_panel(self, ctx: DiscoContext) -> None:
        """Open the unified stake panel for LURE -> REEL."""
        from core.framework.staking import StakeAdapter, StakePanelView, StakeToken

        async def _state(c: DiscoContext) -> dict:
            state = await fish_svc.list_state(c.db, c.guild_id, c.author.id)
            staked_raw = int(state.get("lure_staked_raw") or 0)
            pending_raw = int(
                await fish_svc.accrued_stake_yield(
                    c.db, c.guild_id, c.author.id,
                ) or 0
            )
            wallet_raw = int(
                await fish_svc.get_lure_wallet_raw(
                    c.db, c.guild_id, c.author.id,
                ) or 0
            )
            staked_h = to_human(staked_raw)
            daily_h = staked_h * float(fc.LURE_STAKE_REEL_PER_DAY)
            lure_oracle, reel_oracle = await _oracle_pair(c)
            return {
                "staked_by_sym": {fc.LURE_SYMBOL: staked_raw},
                "wallet_by_sym": {fc.LURE_SYMBOL: wallet_raw},
                "stake_oracle_by_sym": {fc.LURE_SYMBOL: lure_oracle},
                "yield_oracle": reel_oracle,
                "pending_raw": pending_raw,
                "daily_rate_raw": int(to_raw(daily_h)),
            }

        async def _stake(c: DiscoContext, raw: int, _sym: str) -> int:
            res = await fish_svc.stake_lure(
                c.db, c.guild_id, c.author.id, int(raw),
            )
            return int(res.lure_staked_raw)

        async def _unstake(c: DiscoContext, raw: int, _sym: str) -> int:
            res = await fish_svc.unstake_lure(
                c.db, c.guild_id, c.author.id, int(raw),
            )
            return int(res.lure_staked_raw)

        async def _claim(c: DiscoContext) -> int:
            res = await fish_svc.claim_stake_yield(
                c.db, c.guild_id, c.author.id,
            )
            return int(getattr(res, "reel_yield_paid_raw", 0) or 0)

        adapter = StakeAdapter(
            title="\U0001F3A3 Fishing Stake (LURE -> REEL)",
            color=C_TEAL,
            stake_tokens=[StakeToken(fc.LURE_SYMBOL, "\U0001FA9D")],
            yield_symbol=fc.REEL_SYMBOL, yield_emoji="\U0001F3A3",
            get_state=_state, do_stake=_stake,
            do_unstake=_unstake, do_claim=_claim,
            note=(
                f"Stake LURE to drip REEL. Yield: "
                f"{fc.LURE_STAKE_REEL_PER_DAY:g} REEL per LURE per day."
            ),
        )
        await StakePanelView.send(ctx, adapter)

    # -- ,fish unstake ----------------------------------------------------

    @fish.command(name="unstake")
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_unstake(self, ctx: DiscoContext, amount: str) -> None:
        """Unstake LURE back to your wallet. `all` = unstake everything."""
        s = (amount or "").strip().lower()
        if s in ("all", "everything", "max"):
            state = await fish_svc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
            req_raw = int(state.get("lure_staked_raw") or 0)
            if req_raw <= 0:
                await ctx.reply_error("You have no LURE staked.")
                return
        else:
            try:
                amt = float(s.replace(",", "").replace("_", ""))
            except ValueError:
                await ctx.reply_error(f"Invalid amount: `{amount}`.")
                return
            if amt <= 0:
                await ctx.reply_error("Amount must be positive.")
                return
            req_raw = to_raw(amt)
        try:
            res = await fish_svc.unstake_lure(
                ctx.db, ctx.guild_id, ctx.author.id, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        from core.framework.staking import stake_receipt
        lure_oracle, reel_oracle = await _oracle_pair(ctx)
        note = "LURE locked / unlocked -- no oracle impact (not a trade)."
        if res.reel_yield_paid_raw > 0:
            note += (
                "  REEL minted from stake yield -- no oracle impact "
                "(swap or cashout to actually move the chart)."
            )
        await ctx.reply(
            embed=stake_receipt(
                action="Unstaked",
                stake_symbol=fc.LURE_SYMBOL, stake_emoji="\U0001FA9D",
                delta_h=to_human(abs(int(res.lure_delta_raw))),
                total_h=to_human(int(res.lure_staked_raw)),
                stake_oracle=lure_oracle,
                yield_symbol=fc.REEL_SYMBOL, yield_emoji="\U0001F3A3",
                yield_paid_h=to_human(int(res.reel_yield_paid_raw)),
                yield_oracle=reel_oracle,
                note=note,
            ),
            mention_author=False,
        )

    # -- ,fish claim ------------------------------------------------------

    @fish.command(name="claim", aliases=["yield"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_claim(self, ctx: DiscoContext) -> None:
        """Pay out accrued REEL from your LURE stake. Stake stays locked."""
        try:
            res = await fish_svc.claim_stake_yield(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        from core.framework.staking import claim_receipt
        lure_oracle, reel_oracle = await _oracle_pair(ctx)
        await ctx.reply(
            embed=claim_receipt(
                yield_symbol=fc.REEL_SYMBOL, yield_emoji="\U0001F3A3",
                yield_paid_h=to_human(int(res.reel_yield_paid_raw)),
                yield_oracle=reel_oracle,
                stake_symbol=fc.LURE_SYMBOL, stake_emoji="\U0001FA9D",
                total_staked_h=to_human(int(res.lure_staked_raw)),
                stake_oracle=lure_oracle,
                note=(
                    "REEL minted from stake yield -- no oracle impact "
                    "(swap or cashout to actually move the chart)."
                ),
            ),
            mention_author=False,
        )

    # -- ,fish cashout ----------------------------------------------------

    @fish.command(name="cashout", aliases=["burn"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fish_cashout(self, ctx: DiscoContext, amount: str) -> None:
        """Burn REEL for USD wallet credit at oracle minus the burn fee. `all` = entire wallet."""
        s = (amount or "").strip().lower()
        if s in ("all", "everything", "max"):
            req_raw = await fish_svc.get_reel_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if req_raw <= 0:
                await ctx.reply_error("You have no REEL to cash out.")
                return
        else:
            try:
                amt = float(s.replace(",", "").replace("_", ""))
            except ValueError:
                await ctx.reply_error(f"Invalid amount: `{amount}`.")
                return
            if amt <= 0:
                await ctx.reply_error("Amount must be positive.")
                return
            req_raw = to_raw(amt)
        try:
            res = await fish_svc.cashout_reel(
                ctx.db, ctx.guild_id, ctx.author.id, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # V3 Pillar 2: fisher mastery XP scales with USD cashed out.
        try:
            from services import mastery as _mastery
            _xp = _mastery.xp_for_action(to_human(int(res.usd_credited_raw)))
            await _mastery.add_mastery(
                ctx.db, ctx.author.id, ctx.guild_id, "fisher", _xp,
            )
        except Exception:
            pass
        await fish_svc._publish_economy_event(
            self.bot, event=fish_svc.EVENT_REEL_CASHOUT,
            guild_id=ctx.guild_id, user_id=ctx.author.id,
            reel_burned_raw=res.reel_burned_raw,
            usd_credited_raw=res.usd_credited_raw,
            reel_oracle_before=res.reel_oracle_before,
            reel_oracle_after=res.reel_oracle_after,
            price_impact_pct=res.price_impact_pct,
        )
        from core.framework.staking import cashout_receipt
        await ctx.reply(
            embed=cashout_receipt(
                burned_symbol=fc.REEL_SYMBOL, burned_emoji="\U0001F3A3",
                burned_h=to_human(int(res.reel_burned_raw)),
                usd_credited_h=to_human(int(res.usd_credited_raw)),
                oracle_before=float(res.reel_oracle_before),
                oracle_after=float(res.reel_oracle_after),
                impact_pct=float(res.price_impact_pct),
                revenue_usd=float(res.revenue_usd or 0.0),
                lp_reward_usd=float(res.lp_reward_usd or 0.0),
            ),
            mention_author=False,
        )

    @fish.command(name="help")
    @guild_only
    async def fish_help(self, ctx: DiscoContext) -> None:
        """Show fishing help."""
        prefix = await ctx.get_guild_prefix()
        embed = (
            card("\U0001F3A3 Fishing Help", color=C_BLURPLE)
            .description(
                "Cast a line, hook the fish, sell what you pull. Junk is "
                "salvageable, money bags pay instantly, and very rarely "
                "an egg hatches a water-type buddy. Deep-water casts can "
                "also hook **wild aquatic buddies** -- press Challenge to "
                "fight them with your active buddy for LURE and a chance "
                "at capture.\n\n"
                "**Economy:** fishing pays in **LURE**. Burn or stake LURE "
                "for **REEL**, the network coin. REEL buys all rods and "
                "bait, and can be cashed out for USD."
            )
            .field(
                "Core",
                f"`{prefix}fish` -- cast (also `{prefix}cast`)\n"
                f"`{prefix}fish stats [@user]` -- tackle box panel\n"
                f"`{prefix}fish inv` -- show what's in your bag\n"
                f"`{prefix}fish history` -- recent catches",
                False,
            )
            .field(
                "Shop & Gear (REEL)",
                f"`{prefix}fish shop` -- browse rods, bait, and crab traps\n"
                f"`{prefix}fish buy rod` -- upgrade to next rod tier\n"
                f"`{prefix}fish buy <bait_key> <qty>` -- stock bait\n"
                f"`{prefix}fish buy <trap_key> <qty>` -- stock crab traps\n"
                f"`{prefix}fish bait <bait_key|none>` -- equip / unequip bait",
                False,
            )
            .field(
                "Crab Traps (passive haul)",
                f"`{prefix}fish trap` -- show placed traps + readiness\n"
                f"`{prefix}fish trap place <key> [qty]` -- deploy in current zone\n"
                f"`{prefix}fish trap collect` -- haul every soaked trap for LURE + crabs",
                False,
            )
            .field(
                "Held Eggs (when active slots are full)",
                f"`{prefix}fish egg` -- show your held eggs\n"
                f"`{prefix}fish egg hatch [species]` -- hatch the oldest\n"
                f"`{prefix}fish egg gift @user [species] [count]` -- free P2P transfer\n"
                f"`{prefix}fish egg sell [count|all|<species>]` -- liquidate to LURE wallet\n"
                f"`{prefix}ah list egg <species> <price>` -- list on the auction house\n"
                f"`{prefix}ah browse egg` -- browse egg listings",
                False,
            )
            .field(
                "Treasure Maps",
                f"`{prefix}fish dig` -- consume a Soggy Treasure Map for "
                f"a weighted roll (LURE caches, REEL kicker, rare bait, "
                f"crab traps, eggs, or a legendary jackpot fish)",
                False,
            )
            .field(
                "Beachcomb",
                f"`{prefix}fish beachcomb` -- wander the shoreline for "
                f"random loot (LURE/REEL purses, bait stash, Soggy "
                f"Treasure Map, or a rare Ancient Relic jackpot fish)\n"
                f"-# Free roll, "
                f"{fc.BEACHCOMB_COOLDOWN_S // 60}-minute cooldown.",
                False,
            )
            .field(
                "Zones & Selling",
                f"`{prefix}fish zones` -- list zones and access\n"
                f"`{prefix}fish zone <key>` -- switch zone\n"
                f"`{prefix}fish sell [all|junk|<fish_key>]` -- sell catches for LURE",
                False,
            )
            .field(
                "Token Economy (one-way)",
                f"`{prefix}fish swap <amt|all>` -- burn LURE, mint REEL "
                f"at the live oracle (slippage scales with size)\n"
                f"`{prefix}fish stake [amt|all]` -- stake LURE for "
                f"{fc.LURE_STAKE_REEL_PER_DAY:g} REEL/LURE/day\n"
                f"`{prefix}fish claim` -- collect accrued REEL\n"
                f"`{prefix}fish unstake <amt|all>` -- pull LURE back (auto-claims)\n"
                f"`{prefix}fish cashout <amt|all>` -- burn REEL for USD "
                f"(same price impact as `.sell`)",
                False,
            )
            .field(
                "Leaderboards",
                f"`{prefix}fish lb` -- lifetime LURE earned\n"
                f"`{prefix}fish lb biggest` -- trophy weights",
                False,
            )
            .footer(
                "Hook the fish in the SWEET window for a size bonus. "
                "Combo streaks boost payouts up to "
                f"{int((fc.COMBO_MAX - 1) * 100)}%."
            )
        )
        await ctx.send_embed(embed.build())


# === COG_END ===

# === SETUP_START ===
async def setup(bot: Discoin) -> None:
    await bot.add_cog(Fishing(bot))
# === SETUP_END ===
