"""
cogs/dungeon.py  -  Delve crawler commands and panels.

Top-level surface (all under the ``delve`` group; ``dungeon`` is an alias):
    ,delve                   -- show the current room or surface panel
    ,delve start             -- begin a new run
    ,delve next              -- advance to next room
    ,delve descend           -- take stairs to the next floor
    ,delve rest              -- end the run + full heal at the surface
    ,delve attack            -- basic swing in combat
    ,delve skill             -- class skill swing in combat
    ,delve flee              -- flee combat (HP penalty)
    ,delve capture           -- attempt to tame the active mob
    ,delve mine              -- mine the ore vein in this room
    ,delve open              -- open the chest in this room
    ,delve pray              -- activate the shrine in this room
    ,delve junk [use|sell key] -- view / use / sell salvage + mat + usable drops
    ,delve relic [equip key] -- list / equip / unequip relics
    ,delve curse [set key]   -- arm an opt-in run modifier
    ,delve use <item>        -- use a consumable
    ,delve class <warrior|mage|rogue>
    ,delve upgrade [...]     -- spend earned stat points
    ,delve respec            -- refund every spent stat point (USD fee)
    ,delve shop              -- list weapons / armor / potions
    ,delve buy <kind> <key>  -- buy an item
    ,delve equip <kind> <key> -- equip a weapon or armor
    ,delve inv               -- inventory + ore + RUNE balances
    ,delve party             -- captured buddies
    ,delve summon <id|none>  -- set the active assist buddy
    ,delve release <id>      -- release a captured buddy
    ,delve stats [@u]        -- panel
    ,delve swap <ore> <amt>  -- burn ore -> mint RUNE
    ,delve stake <ore> <amt> -- lock ore for RUNE yield
    ,delve stake all         -- stake every ore in wallet
    ,delve unstake <ore> <amt>
    ,delve unstake all       -- unstake every ore + claim yield
    ,delve sell all          -- sell every unequipped non-starter piece
    ,delve claim             -- claim accrued RUNE yield
    ,delve cashout <amt>     -- burn RUNE -> credit USD
    ,delve lb                -- deepest-floor leaderboard
    ,delve help

Heavy lifting lives in services.dungeon -- this module is presentation
only.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

import configs.dungeon_config as dc
from configs.buddies_config import SPECIES, roll_rarity
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.cooldowns import user_cooldown
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.quick_buy import QuickBuyButton
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_AMBER,
    C_CRIMSON,
    C_ERROR,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_NEUTRAL,
    C_PURPLE,
    C_SUCCESS,
    C_TEAL,
    FormatKit,
    RARITY_COLORS,
    fmt_rel,
    fmt_token,
    fmt_ts,
    fmt_usd,
)

from services import dungeon as dsvc

log = logging.getLogger(__name__)


# ============================================================================
# Display helpers
# ============================================================================

def _fmt_ore(symbol: str, amount: float) -> str:
    emoji = {
        dc.COPPER_SYMBOL: dc.COPPER_EMOJI,
        dc.SILVER_SYMBOL: dc.SILVER_EMOJI,
        dc.GOLD_SYMBOL:   dc.GOLD_EMOJI,
    }.get(symbol, "")
    return fmt_token(amount, symbol, emoji)


def _fmt_rune(amount: float) -> str:
    return fmt_token(amount, dc.RUNE_SYMBOL, dc.RUNE_EMOJI)


def _with_usd(amount: float, oracle: float) -> str:
    if amount <= 0 or oracle <= 0:
        return ""
    return f"  ~ **{fmt_usd(amount * oracle)}**"


async def _oracles(ctx: DiscoContext) -> dict[str, float]:
    """Fetch the live oracle for COPPER/SILVER/GOLD/RUNE, defaulting to 0."""
    out = {dc.COPPER_SYMBOL: 0.0, dc.SILVER_SYMBOL: 0.0,
           dc.GOLD_SYMBOL: 0.0, dc.RUNE_SYMBOL: 0.0}
    for sym in out:
        row = await ctx.db.get_price(sym, ctx.guild_id)
        if row and row.get("price") is not None:
            out[sym] = float(row["price"])
    return out


def _frame_block(frame_key: str, **subs: str) -> str:
    """Render a FRAMES entry in a code fence with simple {} substitutions."""
    body = dc.FRAMES.get(frame_key, "")
    if subs:
        try:
            body = body.format(**{k: v for k, v in subs.items()})
        except (KeyError, IndexError):
            pass
    return f"```\n{body}\n```"


# ---------------------------------------------------------------------------
# Relics + Curses panel helpers
# ---------------------------------------------------------------------------

def _relic_effect_lines(effects: dict) -> list[str]:
    """Render an effect dict as human-readable bullets."""
    out: list[str] = []
    label = {
        "hp_max_mult":     "Max HP",
        "spd_bonus":       "Speed",
        "crit_bonus":      "Crit chance",
        "int_dmg_mult":    "Spell damage",
        "mine_yield_mult": "Mining yield",
        "rune_drop_mult":  "RUNE drops",
        "lifesteal_pct":   "Lifesteal",
        "thorns_pct":      "Thorns",
    }
    for k, v in effects.items():
        name = label.get(k, k)
        if k.endswith("_mult"):
            pct = (float(v) - 1.0) * 100
            out.append(f"- {name}: **+{pct:.0f}%**")
        else:
            pct = float(v) * 100
            out.append(f"- {name}: **+{pct:.0f}%**")
    return out


def _relic_info_embed(meta: dict) -> discord.Embed:
    rarity = str(meta.get("rarity") or "common")
    color = RARITY_COLORS.get(rarity, C_NEUTRAL)
    b = card(
        f"{meta.get('emoji', '')} {meta.get('name', '?')} ({rarity.title()})",
        color=color,
        description=str(meta.get("blurb") or ""),
    )
    effects = dict(meta.get("effects") or {})
    if effects:
        b.field("Effects", "\n".join(_relic_effect_lines(effects)) or "_none_", False)
    return b.build()


def _relics_panel_embed(
    author: discord.Member, owned: dict, equipped: str | None,
) -> discord.Embed:
    eq_meta = dc.relic_meta(equipped)
    eq_line = (
        f"{eq_meta['emoji']} **{eq_meta['name']}** ({str(eq_meta.get('rarity', 'common')).title()})"
        if eq_meta else "_none equipped_"
    )
    b = card(
        f"\U0001F48E {author.display_name}'s Relics",
        color=C_GOLD,
    ).field("Equipped", eq_line, False)
    if owned:
        # Sort by rarity tier so legendaries surface first
        rarity_order = ("legendary", "epic", "rare", "uncommon", "common")
        ordered: list[tuple[str, int]] = []
        for r in rarity_order:
            for k, count in owned.items():
                m = dc.relic_meta(k)
                if m and str(m.get("rarity") or "common") == r:
                    ordered.append((k, int(count)))
        lines = []
        for k, count in ordered:
            m = dc.relic_meta(k) or {}
            mark = " (equipped)" if k == equipped else ""
            tag = f" x{count}" if count > 1 else ""
            lines.append(
                f"`{k:<14}` {m.get('emoji', '')} **{m.get('name', k)}** "
                f"({str(m.get('rarity', 'common')).title()}){tag}{mark}"
            )
        b.field("Owned", "\n".join(lines), False)
    else:
        b.field(
            "Owned",
            f"_None yet. Crack chests on floor {dc.RELIC_MIN_FLOOR}+ to find them._",
            False,
        )
    b.footer("Equip with `,delve relic equip <key>`.")
    return b.build()


def _curses_panel_embed(
    author: discord.Member, active_key: str | None,
) -> discord.Embed:
    active_meta = dc.curse_meta(active_key)
    active_line = (
        f"{active_meta['emoji']} **{active_meta['name']}** -- {active_meta.get('blurb', '')}"
        if active_meta else "_no curse armed_"
    )
    b = card(
        f"\U0001F480 {author.display_name}'s Curse Compendium",
        color=C_PURPLE,
    ).field("Active", active_line, False)
    lines = []
    for key, meta in dc.RUN_CURSES.items():
        rune = (float(meta.get("rune_mult", 1.0)) - 1.0) * 100
        ore  = (float(meta.get("ore_mult", 1.0))  - 1.0) * 100
        chest = (float(meta.get("chest_mult", 1.0)) - 1.0) * 100
        dmg  = (float(meta.get("mob_dmg_mult", 1.0)) - 1.0) * 100
        hp   = (float(meta.get("mob_hp_mult", 1.0))  - 1.0) * 100
        block = " | NO POTIONS" if meta.get("block_potions") else ""
        lines.append(
            f"{meta['emoji']} `{key:<10}` **{meta['name']}**\n"
            f"   _{meta.get('blurb', '')}_\n"
            f"   +{rune:.0f}% RUNE, +{ore:.0f}% ore, +{chest:.0f}% chests | "
            f"mobs +{hp:.0f}% HP, +{dmg:.0f}% dmg{block}"
        )
    b.field("Available curses", "\n\n".join(lines), False)
    b.footer("Arm one with `,delve curse set <key>` BEFORE `,delve start`.")
    return b.build()


def _junk_panel_embed(
    author: discord.Member, junk_inv: dict,
) -> discord.Embed:
    """Render the player's junk inventory grouped by kind.

    Salvage / craft mat / usable get their own field; each line shows
    quantity, total salvage RUNE for that stack, and (for usable) the
    in-run effect summary so the player knows when to ``use`` vs
    ``sell``. Counts chunked into 1024-cap-safe sub-fields if the pile
    grows large.
    """
    b = card(
        f"\U0001F392 {author.display_name}'s Delve Junk",
        color=C_NEUTRAL,
    )
    if not junk_inv:
        return b.description(
            "_No junk yet. Kill mobs, crack chests, or mine ore -- "
            "salvage, craft mats, and the occasional usable item drop "
            "as a secondary loot._"
        ).build()
    by_kind: dict[str, list[tuple[str, int]]] = {}
    for k, qty in junk_inv.items():
        meta = dc.junk_meta(k) or {}
        kind = str(meta.get("kind") or "salvage")
        by_kind.setdefault(kind, []).append((k, int(qty or 0)))
    section_titles = {
        "salvage": "\U0001F5D1 Salvage",
        "mat":     "\U00002692\U0000FE0F Craft Mats",
        "usable":  "\U0001F9EA Usables",
    }
    total_rune = 0.0
    for kind in ("usable", "mat", "salvage"):
        rows = by_kind.get(kind) or []
        if not rows:
            continue
        rows.sort(
            key=lambda kv: (
                # Sort by rarity (rarer first), then by per-unit salvage
                # value. Keeps the legendary scrap at the top of the list.
                -dc.RARITY_RANK.get(dc.item_rarity(dc.junk_meta(kv[0])), 0),
                -float(dc.effective_salvage_rune(dc.junk_meta(kv[0]))),
                kv[0],
            ),
        )
        lines: list[str] = []
        for k, qty in rows:
            m = dc.junk_meta(k) or {}
            rune_per = float(dc.effective_salvage_rune(m))
            stack_rune = rune_per * qty
            total_rune += stack_rune
            extra = ""
            if kind == "usable":
                use_kind = str(m.get("use_kind") or "")
                if use_kind == "heal":
                    extra = f"  ·  heal {int(float(m.get('use_value') or 0) * 100)}% HP"
                elif use_kind == "escape":
                    extra = "  ·  escape any non-boss"
                elif use_kind == "buff_crit":
                    extra = (
                        f"  ·  +{int(float(m.get('use_value') or 0) * 100)}% "
                        f"SPD/crit {int(m.get('use_duration') or 0)} rds"
                    )
                elif use_kind == "ammo":
                    extra = f"  ·  +{int(m.get('use_value') or 0)} ammo"
            rdot = dc.rarity_dot(dc.item_rarity(m))
            lines.append(
                f"{rdot} `{k:<18}` {m.get('emoji', '')} **{m.get('name', k)}**  "
                f"x{qty}  ·  sells {fmt_token(stack_rune, 'RUNE', dc.RUNE_EMOJI)}{extra}"
            )
        # Chunk into 1024-cap-safe sub-fields.
        title = section_titles.get(kind, kind.title())
        idx = 0
        buf = ""
        for ln in lines:
            sep = "\n" if buf else ""
            if buf and len(buf) + len(sep) + len(ln) > 1000:
                b.field(title if idx == 0 else f"{title} (cont)", buf, False)
                buf = ln
                idx += 1
            else:
                buf += sep + ln
        if buf:
            b.field(title if idx == 0 else f"{title} (cont)", buf, False)
    b.footer(
        f"Total salvage value: {fmt_token(total_rune, 'RUNE', dc.RUNE_EMOJI)}  ·  "
        "`,delve junk sell all` to dump for RUNE  ·  "
        "`,delve junk use <key>` for usables"
    )
    return b.build()


def _hp_bar(cur: int, mx: int, *, width: int = 12) -> str:
    """Plain-ASCII HP bar -- still used by the surface panel + run-state embed.

    Kept as ASCII so monospace sections (which other panels rely on)
    line up. The combat embed uses ``_cute_hp_bar`` for the
    Pokemon-style fight panel.
    """
    if mx <= 0:
        return "[" + " " * width + "]"
    pct = max(0.0, min(1.0, cur / mx))
    fill = int(round(pct * width))
    return "[" + "#" * fill + "-" * (width - fill) + "]"


def _cute_hp_bar(cur: int, mx: int, *, width: int = 10) -> str:
    """Block-style HP bar with a heart emoji and a percentage tag.

    Designed for the interactive Delve combat embed. Color cues come
    from the surrounding embed. Switches the fill character at low HP
    so players get a visual nudge before they actually die.
    """
    if mx <= 0:
        return "\U0001F90D " + "\U00002B1C" * width + " 0%"
    pct = max(0.0, min(1.0, cur / mx))
    fill = int(round(pct * width))
    # █ full block, ░ light shade, ▓ dark shade for crit-low.
    if pct >= 0.5:
        bar = "█" * fill + "░" * (width - fill)
        heart = "\U0001F49A"  # green heart
    elif pct >= 0.20:
        bar = "▓" * fill + "░" * (width - fill)
        heart = "\U0001F49B"  # yellow heart
    else:
        bar = "▓" * fill + "░" * (width - fill)
        heart = "❤️"  # red heart
    return f"{heart} {bar} {int(pct * 100)}%"


def _balance_lines(holdings: dict[str, int], oracles: dict[str, float]) -> list[str]:
    """Render a compact wallet block for the panel."""
    lines: list[str] = []
    for sym in (dc.COPPER_SYMBOL, dc.SILVER_SYMBOL, dc.GOLD_SYMBOL, dc.RUNE_SYMBOL):
        amt_h = to_human(int(holdings.get(sym, 0) or 0))
        if amt_h <= 0:
            continue
        formatter = _fmt_rune if sym == dc.RUNE_SYMBOL else (lambda a, s=sym: _fmt_ore(s, a))
        lines.append(f"{formatter(amt_h)}{_with_usd(amt_h, oracles.get(sym, 0.0))}")
    return lines or ["_(empty)_"]


async def _gather_holdings(ctx: DiscoContext, uid: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for sym in (dc.COPPER_SYMBOL, dc.SILVER_SYMBOL, dc.GOLD_SYMBOL, dc.RUNE_SYMBOL):
        row = await ctx.db.get_wallet_holding(
            uid, ctx.guild_id, dc.CRYPT_NETWORK_SHORT, sym,
        )
        out[sym] = int((row or {}).get("amount") or 0)
    return out


def _floor_color(state: dict) -> int:
    floor = int(state.get("current_floor") or 0)
    if floor <= 0:
        return C_NAVY
    fmeta = dc.floor_meta(floor)
    return int(fmeta.get("color_hex") or C_NEUTRAL)


# ============================================================================
# Interactive battle view
# ============================================================================
#
# ``_DelveBattleView`` drives an in-place combat embed: Strike / Skill / Flee /
# Capture all live as buttons on the same message and each click edits the
# embed instead of spamming the channel.  The view also rolls for two random
# delve-only events:
#   * ``5%`` of mob encounters become a wild buddy battle (captureable like
#     fishing).  The encounter resolves through the same swing/flee/capture
#     buttons but routes the "tame" success straight into the player's
#     ``cc_buddies`` shelter row.
#   * ``8%`` of mob deaths drop a buddy egg into the player's held-egg slot
#     instead of (or in addition to) the normal ore/RUNE drop.

import random as _random


_BATTLE_VIEW_TIMEOUT_S = 180
_DELVE_WILD_BUDDY_CHANCE = 0.05
_DELVE_BUDDY_EGG_DROP_CHANCE = 0.08
_ROOM_VIEW_TIMEOUT_S = 600  # ten minutes -- generous because exploration is slow


# Dungeon-mob -> buddies_config.SPECIES mapping. The dungeon mob keys
# (goblin / kobold / skeleton / ...) don't exist in the buddy SPECIES
# catalog so a captured mob would land in cc_buddies as a "stub" with
# no emoji, ability, or ASCII art. Mapping each tier to a thematic
# existing species lets the captured creature show up as a real buddy
# with the same ability + portrait + name pool the rest of the buddy
# system already supports.
_DUNGEON_SPECIES_BY_TIER: dict[int, tuple[str, ...]] = {
    1: ("fox", "cobble"),                 # tier 1 -- small / nimble
    2: ("glitch", "pyper", "cobble"),     # tier 2 -- weird / tricky
    3: ("wolf", "glitch"),                # tier 3 -- predator / bug
    4: ("nimbus", "wecco"),               # tier 4 -- skybound / clutch
    5: ("zenny", "wecco"),                # tier 5 -- legendary fliers
}


def _pick_dungeon_buddy_species(mob_tier: int) -> str:
    pool = _DUNGEON_SPECIES_BY_TIER.get(
        max(1, min(5, int(mob_tier))), ("fox",),
    )
    return _random.choice(pool)


# Stat-name aliases for ,delve upgrade. Each value resolves to the index
# (0=HP, 1=ATK, 2=SPD, 3=INT) the spend_stat_points call expects.
_DELVE_STAT_ALIASES: dict[str, int] = {
    "hp": 0, "hardiness": 0, "health": 0, "vit": 0, "vitality": 0,
    "atk": 1, "attack": 1, "power": 1, "str": 1, "strength": 1, "dmg": 1, "damage": 1,
    "spd": 2, "speed": 2, "vigor": 2, "agi": 2, "agility": 2, "dex": 2, "dexterity": 2,
    "int": 3, "intelligence": 3, "wisdom": 3, "wis": 3, "magic": 3, "mag": 3, "spell": 3,
}


def _parse_delve_upgrade_args(args: tuple[str, ...]) -> tuple[int, int, int, int]:
    """Parse mixed positional / named args into (hp, atk, spd, int_).

    Accepts:
      * positional ints      ``2 1 0 0`` -- assigned in HP/ATK/SPD/INT order
      * named pairs          ``atk 2 hp 1``
      * mixed                ``2 atk 1`` -- positionals fill from HP outward

    Raises ``ValueError`` with a friendly message when the args contain a
    placeholder like ``<atk>`` (the help-text literal users sometimes copy)
    or any non-integer / unknown stat name.
    """
    out = [0, 0, 0, 0]
    pos_idx = 0
    i = 0
    cleaned = [str(a).strip() for a in args if str(a).strip()]
    while i < len(cleaned):
        tok = cleaned[i]
        low = tok.lower()
        if low.startswith("<") and low.endswith(">"):
            raise ValueError(
                f"`{tok}` looks like a placeholder. Replace it with a number "
                "(e.g. `2`) or with a stat keyword like `atk 5`."
            )
        if low in _DELVE_STAT_ALIASES:
            stat_idx = _DELVE_STAT_ALIASES[low]
            if i + 1 >= len(cleaned):
                raise ValueError(f"`{tok}` needs a number after it (e.g. `{tok} 2`).")
            try:
                amt = max(0, int(cleaned[i + 1]))
            except ValueError:
                raise ValueError(
                    f"`{cleaned[i + 1]}` after `{tok}` must be a whole number."
                ) from None
            out[stat_idx] += amt
            i += 2
            continue
        try:
            amt = max(0, int(low))
        except ValueError:
            raise ValueError(
                f"`{tok}` is not a number or stat name. Use HP/ATK/SPD/INT."
            ) from None
        if pos_idx >= 4:
            raise ValueError(
                "Too many numbers; only HP / ATK / SPD / INT are allowed."
            )
        out[pos_idx] += amt
        pos_idx += 1
        i += 1
    return tuple(out)  # type: ignore[return-value]


class _DelveUpgradeButton(discord.ui.Button):
    """One stat = one button. Spends 1 point in that stat per click."""

    def __init__(self, stat_idx: int, label: str, emoji: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, emoji=emoji, style=style, row=0)
        self._stat_idx = stat_idx

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DelveUpgradeView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "This isn't your panel.", ephemeral=True,
            )
            return
        if view.available <= 0:
            await interaction.response.send_message(
                "No points left to spend.", ephemeral=True,
            )
            return
        kwargs = {"hp": 0, "atk": 0, "spd": 0, "int_": 0}
        keys = ("hp", "atk", "spd", "int_")
        kwargs[keys[self._stat_idx]] = 1
        try:
            await dsvc.spend_stat_points(
                view.cog.bot.db, interaction.guild_id, view.owner_id, **kwargs,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        view.available = max(0, view.available - 1)
        for child in view.children:
            if isinstance(child, _DelveUpgradeButton):
                child.disabled = view.available <= 0
        emoji = self.emoji or ""
        await interaction.response.send_message(
            f"{emoji} +1 {self.label}. **{view.available}** point(s) left.",
            ephemeral=True,
        )
        try:
            await interaction.message.edit(view=view)  # type: ignore[union-attr]
        except (discord.HTTPException, AttributeError):
            pass


class _DelveUpgradeView(discord.ui.View):
    """Quick-spend panel for ``,delve upgrade``. One click = one point.

    Lets a player who's spooked by the positional-args syntax just hit
    the stat they want without remembering the ``<hp> <atk> <spd> <int>``
    column order. Each click validates the cap server-side so racing
    clicks can't double-spend.
    """

    def __init__(self, cog: "Dungeon", owner_id: int, *, available: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.available = max(0, int(available))
        self.add_item(_DelveUpgradeButton(0, "Hardiness", "❤️", discord.ButtonStyle.danger))
        self.add_item(_DelveUpgradeButton(1, "Power",     "⚔️", discord.ButtonStyle.primary))
        self.add_item(_DelveUpgradeButton(2, "Vigor",     "\U0001F4A8", discord.ButtonStyle.success))
        self.add_item(_DelveUpgradeButton(3, "Wisdom",    "✨", discord.ButtonStyle.secondary))
        for child in self.children:
            if isinstance(child, _DelveUpgradeButton):
                child.disabled = self.available <= 0


async def _bump_panel(view: discord.ui.View, interaction: discord.Interaction) -> None:
    """Re-send this view's message at the bottom of the channel.

    Used by the Bump button on both ``_DelveRoomView`` and
    ``_DelveBattleView``. Owner-locked via the view's interaction_check.
    Deletes the source message + re-posts the same embed + view at the
    bottom and rebinds ``view.message`` so subsequent button clicks edit
    the new message.
    """
    try:
        await interaction.response.defer()
    except discord.HTTPException:
        pass
    msg = interaction.message
    if msg is None:
        return
    embeds = list(msg.embeds) if msg.embeds else []
    channel = msg.channel
    try:
        await msg.delete()
    except (discord.NotFound, discord.HTTPException):
        log.debug("delve bump: source delete failed", exc_info=True)
    try:
        sent = await channel.send(embeds=embeds, view=view)
    except discord.HTTPException:
        log.debug("delve bump: re-post failed", exc_info=True)
        return
    try:
        view.message = sent  # type: ignore[attr-defined]
    except Exception:
        pass


def _delve_set_button_visibility(view: discord.ui.View, room_type: str) -> None:
    """Show / hide the room-action buttons based on the current room type.

    Discord views can't dynamically add or remove children once posted,
    so the view declares every possible button up front and we just
    flip ``disabled`` (and re-tag the labels) per room.

    Always-on:    Next, Rest
    ore room:     + Mine
    chest room:   + Open
    stairs room:  + Descend  (Next is hidden on the stairs themselves
                              so the player commits to going deeper or
                              retreating; using Next on stairs is a
                              footgun -- you'd waste the floor's last
                              room).
    """
    visible = {
        "Next": True,
        "Mine": False,
        "Open": False,
        "Descend": False,
        "Pray": False,
        "Rest": True,
    }
    if room_type == "ore":
        visible["Mine"] = True
    elif room_type == "chest":
        visible["Open"] = True
    elif room_type == "stairs":
        visible["Descend"] = True
        visible["Next"] = False
    elif room_type == "shrine":
        # Shrine: Pray triggers the boon roll; Rest (full heal + end
        # run) and Next (skip and keep delving) still apply.
        visible["Pray"] = True

    for child in view.children:
        label = getattr(child, "label", "") or ""
        # discord.py exposes children in declaration order; toggling
        # disabled keeps the layout stable.
        try:
            child.disabled = not visible.get(label, True)  # type: ignore[attr-defined]
        except Exception:
            pass


class _DelveConsumableSelect(discord.ui.Select):
    """Mid-room consumables picker. Lists what the player owns and on
    select runs ``dsvc.use_consumable`` for that key. Heals + buffs
    work mid-room (escape only fires inside combat -- use that one
    from the battle view, not here).

    Rebuilt on every refresh so the qty + new buys show up.
    """

    def __init__(self, cons: dict) -> None:
        opts: list[discord.SelectOption] = []
        for key, qty in sorted(
            (cons or {}).items(), key=lambda kv: -int(kv[1] or 0),
        ):
            try:
                n = int(qty or 0)
            except (TypeError, ValueError):
                n = 0
            if n <= 0:
                continue
            meta = dc.consumable_meta(key) or {}
            label = f"{meta.get('name', key)} (x{n})"[:100]
            opts.append(discord.SelectOption(
                label=label,
                value=str(key),
                emoji=str(meta.get("emoji") or "")[:1] or None,
                description=str(meta.get("kind") or "")[:100] or None,
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no consumables owned)",
                value="__empty__",
                default=True,
            )]
        super().__init__(
            placeholder="Use a consumable...",
            options=opts[:25],
            min_values=1, max_values=1,
            row=2,
            disabled=(opts[0].value == "__empty__"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DelveRoomView" = self.view  # type: ignore
        choice = self.values[0]
        if choice == "__empty__":
            await interaction.response.send_message(
                "Buy some via `,delve shop consumables` first.",
                ephemeral=True,
            )
            return
        try:
            res = await dsvc.use_consumable(
                view.cog.bot.db,
                view.ctx.guild_id, interaction.user.id,
                str(choice),
            )
        except ValueError as e:
            await interaction.response.send_message(
                str(e), ephemeral=True,
            )
            return
        except Exception as e:
            log.exception(
                "delve consumable click failed key=%s uid=%s",
                choice, interaction.user.id,
            )
            await interaction.response.send_message(
                f"Use failed: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        # Rebuild the room embed (HP / room / dropdown counts).
        try:
            await view._redraw(interaction)
        except Exception:
            log.debug(
                "delve room re-render after consumable failed",
                exc_info=True,
            )
        # Surface the consumable receipt as an ephemeral so the player
        # sees "Healed for X HP" without spamming the main panel.
        try:
            await interaction.followup.send(
                f"\U0001F9EA Used **{choice}**: "
                f"{getattr(res, 'detail', None) or 'ok'}",
                ephemeral=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Delve shop view (categorised dropdown browser)
# ---------------------------------------------------------------------------

# Category metadata. Each value is (label, emoji, source_dict, formatter,
# class_filter) where:
#   * label / emoji feed the SelectOption
#   * source_dict is the catalog (WEAPONS / ARMOR / CONSUMABLES) to draw from
#   * formatter is a callable(item_meta) -> str that renders one line
#   * class_filter applies an additional filter against the player's class
#     ("class" -> only items the player's class can equip; "" -> no filter)
# Sub-categories of CONSUMABLES are filtered by ``kind``.

def _fmt_affix_tail(meta: dict) -> str:
    """Suffix string ``  ·  +10% phys, +15% vs undead`` for an item.

    Returns ``""`` when the item has no affixes -- common gear stays
    visually clean. Used by both the weapon and armor catalog lines so
    the shop / inventory render affixes the same way.
    """
    affixes = dc.item_affixes(meta)
    parts = dc.affix_summary_lines(affixes)
    if not parts:
        return ""
    return "  ·  " + ", ".join(parts)


def _fmt_weapon_line(w: dict) -> str:
    """Render a weapon catalog line: rarity, name, type, ATK, tier, price, blurb.

    Pulled out of ``_DelveShopView._build_embed`` so the new dropdown
    panel and any future inspector reuse the same formatting. The ammo
    label only appears for ranged weapons (bow / crossbow) so melee
    rows aren't padded with an empty 'no ammo' tail. Rarity dot + label
    surface here so the shop and the bag share one visual contract.
    """
    em   = str(w.get("emoji") or "")
    name = str(w.get("name") or w.get("key") or "?")
    wt   = str(w.get("weapon_type") or "?")
    atk  = dc.effective_atk_bonus(w)
    tier = int(w.get("tier") or 0)
    price = float(w.get("price_rune") or 0.0)
    ammo = str(w.get("ammo_key") or "")
    ammo_tag = (
        f"  ·  draws **{ammo.replace('_', ' ')}**"
        if ammo else ""
    )
    blurb = str(w.get("blurb") or "")
    rarity = dc.item_rarity(w)
    rdot = dc.rarity_dot(rarity)
    rlbl = dc.rarity_label(rarity)
    affix_tail = _fmt_affix_tail(w)
    return (
        f"{rdot} {em} **{name}**  ·  *{rlbl}*  ·  T{tier}  ·  *{wt}*  ·  "
        f"**+{atk} ATK**  ·  {_fmt_rune(price)}{ammo_tag}{affix_tail}\n"
        f"-# {blurb}"
    )


def _fmt_armor_line(a: dict) -> str:
    """Render an armor catalog line: rarity, name, type, DEF, tier, price, blurb."""
    em   = str(a.get("emoji") or "")
    name = str(a.get("name") or a.get("key") or "?")
    at   = str(a.get("armor_type") or "?")
    df   = dc.effective_def_bonus(a)
    tier = int(a.get("tier") or 0)
    price = float(a.get("price_rune") or 0.0)
    blurb = str(a.get("blurb") or "")
    rarity = dc.item_rarity(a)
    rdot = dc.rarity_dot(rarity)
    rlbl = dc.rarity_label(rarity)
    affix_tail = _fmt_affix_tail(a)
    return (
        f"{rdot} {em} **{name}**  ·  *{rlbl}*  ·  T{tier}  ·  *{at}*  ·  "
        f"**+{df} DEF**  ·  {_fmt_rune(price)}{affix_tail}\n"
        f"-# {blurb}"
    )


def _fmt_consumable_line(c: dict) -> str:
    """Render a consumable catalog line: name, kind-specific stat, price, blurb.

    Each ``kind`` gets its own stat label so a healing potion shows
    ``+25% HP`` while a damage scroll shows ``5x ATK`` and an ammo bundle
    shows ``20 shots`` -- no more guessing what ``value: 0.25`` means in
    the raw catalog.
    """
    em    = str(c.get("emoji") or "")
    name  = str(c.get("name") or c.get("key") or "?")
    kind  = str(c.get("kind") or "")
    val   = float(c.get("value") or 0.0)
    price = float(c.get("price_rune") or 0.0)
    blurb = str(c.get("blurb") or "")
    if kind == "heal":
        stat = f"**+{int(val * 100)}% HP**"
    elif kind == "revive":
        stat = f"**revive @ {int(val * 100)}% HP**"
    elif kind == "charm":
        stat = f"**+{int(val * 100)}% capture**"
    elif kind == "mine_boost":
        stat = f"**+{int(val * 100)}% ore**"
    elif kind == "lure":
        stat = f"**{int(val * 100)}% bonus mob**"
    elif kind == "damage":
        stat = f"**{val:g}x ATK damage**"
    elif kind == "buff":
        rounds = int(c.get("duration_rounds") or 1)
        stat = f"**{val:g}x  ·  {rounds}r**"
    elif kind == "regen":
        rounds = int(c.get("duration_rounds") or 1)
        stat = f"**+{int(val * 100)}% HP/turn  ·  {rounds}r**"
    elif kind == "skill_reset":
        stat = "**reset skill CD**"
    elif kind == "ammo":
        pack = int(c.get("pack_size") or 0)
        mult = float(c.get("ammo_dmg_mult") or 1.0)
        bonus = f" (+{int((mult - 1) * 100)}% dmg)" if mult > 1.0 else ""
        stat = f"**{pack} shots**{bonus}"
    elif kind == "escape":
        stat = "**ends combat**"
    else:
        stat = f"`{kind}: {val:g}`"
    return (
        f"{em} **{name}**  ·  *{kind}*  ·  {stat}  ·  {_fmt_rune(price)}\n"
        f"-# {blurb}"
    )


# Categories shown in the delve shop dropdown. Each entry is keyed by
# (value, label, emoji) and carries a fetcher that returns the formatted
# lines for that page given the player's class.
_DELVE_SHOP_CATEGORIES: list[tuple[str, str, str]] = [
    ("weapons_class",  "Weapons (your class)", "\U00002694"),
    ("weapons_all",    "Weapons (all)",        "\U0001F5E1"),
    ("armor_class",    "Armor (your class)",   "\U0001F6E1"),
    ("armor_all",      "Armor (all)",          "\U0001F455"),
    ("cons_heal",      "Consumables  -  Healing",  "\U0001F9EA"),
    ("cons_buff",      "Consumables  -  Buffs",    "\U00002728"),
    ("cons_damage",    "Consumables  -  Spells",   "\U0001F4DC"),
    ("cons_ammo",      "Consumables  -  Ammo",     "\U0001F3F9"),
    ("cons_utility",   "Consumables  -  Utility",  "\U0001FA99"),
]


class _DelveShopPageButton(discord.ui.Button):
    """Prev / Next paginator. ``direction = -1`` for prev, ``+1`` for next."""

    def __init__(self, *, direction: int, label: str) -> None:
        super().__init__(
            label=label,
            emoji=("\U00002B05" if direction < 0 else "\U000027A1"),
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DelveShopView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your shop.", ephemeral=True,
            )
            return
        view.page = max(
            0, min(view.page + self.direction, view._total_pages() - 1),
        )
        embed = await view._build_embed()  # also re-runs _rebuild_select()
        await interaction.response.edit_message(embed=embed, view=view)


class _DelveShopCategorySelect(discord.ui.Select):
    def __init__(self, current: str) -> None:
        super().__init__(
            placeholder="\U0001F6D2 Pick a category...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label=label, value=value, emoji=emoji,
                    default=(value == current),
                )
                for value, label, emoji in _DELVE_SHOP_CATEGORIES
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DelveShopView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your shop. Run `,delve shop` to open your own.",
                ephemeral=True,
            )
            return
        view.category = self.values[0]
        view.page = 0
        embed = await view._build_embed()  # also re-runs _rebuild_select()
        await interaction.response.edit_message(embed=embed, view=view)


class _DelveShopView(discord.ui.View):
    """Categorised browser for the surface shop.

    Owner-locked; 5 min timeout. The category dropdown swaps the embed
    in place so players can scan weapons / armor / consumables without
    flooding the channel. Items are pre-filtered to the player's class
    on the default tabs, with explicit "all" tabs for shopping ahead of
    a future class reroll.
    """

    # Cap per-page items so the rendered embed stays under Discord's
    # 6000-char total limit. With each line averaging ~150 chars (name +
    # type + stat + price + blurb), 8 items per page leaves comfortable
    # headroom for the description, class label, and footer.
    _PAGE_SIZE: int = 8

    def __init__(self, ctx: DiscoContext) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.category: str = "weapons_class"
        self.page: int = 0
        self.message: discord.Message | None = None
        self._rebuild_select()
        # Quick Buy button is currency-locked to RUNE -- the only thing
        # the delve shop spends. Lives on its own row so the category
        # dropdown + pagination row stay clean.
        self.add_item(QuickBuyButton(
            ctx=ctx,
            command_template="delve buy {item}",
            accepted_currency=dc.RUNE_SYMBOL,
            item_label="What to buy",
            item_placeholder="weapon iron_shortsword | armor padded | consumable potion_minor",
            modal_title=f"Delve Quick Buy ({dc.RUNE_SYMBOL})",
            owner_id=int(ctx.author.id),
            row=2,
        ))

    def _rebuild_select(self) -> None:
        for child in list(self.children):
            if isinstance(child, (_DelveShopCategorySelect, _DelveShopPageButton)):
                self.remove_item(child)
        self.add_item(_DelveShopCategorySelect(self.category))
        # Pagination buttons -- always present so the layout doesn't
        # shift between pages; their disabled state reflects the cursor.
        prev_btn = _DelveShopPageButton(direction=-1, label="Prev")
        next_btn = _DelveShopPageButton(direction=+1, label="Next")
        prev_btn.disabled = self.page <= 0
        next_btn.disabled = self.page >= max(0, self._total_pages() - 1)
        self.add_item(prev_btn)
        self.add_item(next_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your shop.", ephemeral=True,
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

    async def _player_class_key(self) -> str:
        try:
            state = await dsvc.list_state(
                self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
            )
            return str(state.get("class_key") or "")
        except Exception:
            return ""

    def _category_lines(self, class_key: str) -> tuple[list[str], str]:
        """Return (lines, title_suffix) for the current category.

        Pure-function on the in-memory catalogs + class_key argument so
        ``_total_pages()`` can call it without an extra DB roundtrip.
        ``class_key`` empty disables the class filter (treats the page
        as if every type is allowed). Items flagged ``delve_only=True``
        never appear in the shop -- they only enter circulation through
        boss / mini-boss / chest drops.
        """
        cat = self.category
        lines: list[str] = []
        title_suffix = ""
        if cat == "weapons_class":
            title_suffix = " -- Weapons (your class)"
            for w in dc.WEAPONS.values():
                if w.get("delve_only"):
                    continue
                if class_key and not dc.weapon_allowed_for_class(w["key"], class_key):
                    continue
                lines.append(_fmt_weapon_line(w))
            if not lines:
                lines.append("_No weapons available for this class._")
        elif cat == "weapons_all":
            title_suffix = " -- Weapons (all)"
            lines = [
                _fmt_weapon_line(w) for w in dc.WEAPONS.values()
                if not w.get("delve_only")
            ]
        elif cat == "armor_class":
            title_suffix = " -- Armor (your class)"
            for a in dc.ARMOR.values():
                if a.get("delve_only"):
                    continue
                if class_key and not dc.armor_allowed_for_class(a["key"], class_key):
                    continue
                lines.append(_fmt_armor_line(a))
            if not lines:
                lines.append("_No armor available for this class._")
        elif cat == "armor_all":
            title_suffix = " -- Armor (all)"
            lines = [
                _fmt_armor_line(a) for a in dc.ARMOR.values()
                if not a.get("delve_only")
            ]
        elif cat == "cons_heal":
            title_suffix = " -- Healing"
            lines = [
                _fmt_consumable_line(c) for c in dc.CONSUMABLES.values()
                if c.get("kind") in ("heal", "revive", "regen")
            ]
        elif cat == "cons_buff":
            title_suffix = " -- Buffs / Brews"
            lines = [
                _fmt_consumable_line(c) for c in dc.CONSUMABLES.values()
                if c.get("kind") == "buff"
            ]
        elif cat == "cons_damage":
            title_suffix = " -- Damage Scrolls"
            lines = [
                _fmt_consumable_line(c) for c in dc.CONSUMABLES.values()
                if c.get("kind") == "damage"
            ]
        elif cat == "cons_ammo":
            title_suffix = " -- Ammo"
            lines = [
                _fmt_consumable_line(c) for c in dc.CONSUMABLES.values()
                if c.get("kind") == "ammo"
            ]
        elif cat == "cons_utility":
            title_suffix = " -- Utility"
            lines = [
                _fmt_consumable_line(c) for c in dc.CONSUMABLES.values()
                if c.get("kind") in ("charm", "mine_boost", "lure", "escape", "skill_reset")
            ]
        if not lines:
            lines = ["_(empty)_"]
        return lines, title_suffix

    def _total_pages(self, lines: list[str] | None = None) -> int:
        """How many pages the current category needs at ``_PAGE_SIZE``.

        Re-computes the line list synchronously when not provided -- the
        prev/next button enable check needs this without an awaitable.
        """
        if lines is None:
            # Use the most recent class_key snapshot if cached on the
            # view; fall back to "" so the count matches the broadest
            # version of the page.
            lines, _ = self._category_lines(self._cached_class_key or "")
        n = len(lines or [])
        return max(1, (n + self._PAGE_SIZE - 1) // self._PAGE_SIZE)

    _cached_class_key: str = ""

    async def _build_embed(self) -> discord.Embed:
        rune_balance = to_human(int(
            await dsvc.get_rune_wallet_raw(
                self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
            ) or 0
        ))
        oracles = await _oracles(self.ctx)
        rune_oracle = oracles.get(dc.RUNE_SYMBOL, 0.0)
        bal_line = f"You have **{_fmt_rune(rune_balance)}**"
        if rune_oracle > 0 and rune_balance > 0:
            bal_line += f" ≈ **{fmt_usd(rune_balance * rune_oracle)}**"
        bal_line += " to spend."

        class_key = await self._player_class_key()
        self._cached_class_key = class_key
        cmeta = dc.class_meta(class_key) or {}
        class_label = (
            f"Class: **{cmeta.get('name', class_key)}** -- "
            f"weapons: {', '.join(cmeta.get('weapon_types', ()) or ()) or '-'}  ·  "
            f"armor: {', '.join(cmeta.get('armor_types', ()) or ()) or '-'}"
            if class_key else "_No class picked yet -- ,delve class <name>._"
        )

        lines, title_suffix = self._category_lines(class_key)
        total = self._total_pages(lines)
        # Snap the cursor in case the category change shrank the page count.
        self.page = max(0, min(self.page, total - 1))

        start = self.page * self._PAGE_SIZE
        end = start + self._PAGE_SIZE
        page_lines = lines[start:end]

        embed = (
            card(
                f"\U0001F3EA  Surface Shop{title_suffix}  "
                f"(page {self.page + 1}/{total})",
                color=C_INFO,
            )
            .description(
                f"{bal_line}\n{class_label}\n"
                "Buy with `,delve buy weapon|armor|consumable <key>`."
            )
        )

        # Chunk into 1024-char fields per Discord's per-field cap. With
        # ``_PAGE_SIZE = 8`` items / page the total embed body stays
        # well under the 6000-char overall limit.
        chunk: list[str] = []
        chunk_len = 0
        first = True
        for ln in page_lines:
            ln_len = len(ln) + 1
            if chunk and chunk_len + ln_len > 1024:
                embed.field(
                    "Items" if first else "Items (cont.)",
                    "\n".join(chunk), False,
                )
                first = False
                chunk = [ln]
                chunk_len = ln_len
            else:
                chunk.append(ln)
                chunk_len += ln_len
        if chunk:
            embed.field(
                "Items" if first else "Items (cont.)",
                "\n".join(chunk), False,
            )
        embed.footer(
            f"Showing {start + 1}-{min(end, len(lines))} of {len(lines)} -- "
            f"category dropdown swaps page; \U00002B05 \U000027A1 paginate."
        )
        # Rebuild the button row so prev/next disabled-state reflects
        # the current cursor + the freshly-cached class key.
        self._rebuild_select()
        return embed.build()


class _DelveRoomView(discord.ui.View):
    """Persistent room view: buttons for the current room's actions + Next.

    Edits the same message in place instead of spamming the channel with
    one reply per ,delve next / mine / open / rest / descend. Button
    layout adapts per room type:
        * mob / boss rooms -- handled by ``_DelveBattleView`` instead;
          this view only takes over once combat ends.
        * empty / corridor   -- Next, Rest
        * ore                -- Mine, Next, Rest
        * chest              -- Open, Next, Rest
        * shrine             -- Rest (full heal, ends run), Next
        * stairs             -- Descend, Rest

    Owner-locked: only the player whose run is in progress can press the
    buttons. The class is stateless beyond the cog + ctx + message
    handle; every render path re-reads state from the DB so concurrent
    edits (e.g. an admin ,reset) never leave the view stale.
    """

    def __init__(self, cog: "Dungeon", ctx: DiscoContext) -> None:
        # No timeout: the user-facing requirement is that the panel
        # persists. Stale views still get cleaned up when a new
        # ,delve start spawns a fresh message that takes over.
        super().__init__(timeout=None)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Not your delve. Run `,delve` to start your own.",
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
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _redraw(self, interaction: discord.Interaction | None = None) -> None:
        # Renamed from ``_refresh`` -- discord.ui.View has a private sync
        # ``_refresh(components)`` that the framework calls during
        # component rehydration; shadowing it triggered
        # ``RuntimeWarning: coroutine '_DelveRoomView._refresh' was never
        # awaited`` on every interaction.
        """Re-render the room embed + button set on the same message."""
        if interaction is not None:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
        await self.cog._rebuild_room_message(self.ctx, self)

    # ── Action buttons ─────────────────────────────────────────────────────
    # All buttons share the same shape: defer the interaction (so Discord
    # gets its 3s ack), call the underlying service helper via the cog,
    # then re-render the room embed on the same message. Service-level
    # ValueErrors bubble up as ephemeral toasts so they don't trash the
    # main view layout.

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, emoji="\U0001F463", row=0)
    async def btn_next(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            await dsvc.advance_room(self.ctx.db, self.ctx.guild_id, self.owner_id)
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_AMBER).build(),
                ephemeral=True,
            )
            return
        await self.cog._rebuild_room_message(self.ctx, self, replace_view=True)

    @discord.ui.button(label="Mine", style=discord.ButtonStyle.success, emoji="\U000026CF", row=0)
    async def btn_mine(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            res = await dsvc.mine_ore(self.ctx.db, self.ctx.guild_id, self.owner_id)
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_AMBER).build(),
                ephemeral=True,
            )
            return
        # Pretty receipt for the swing -- public so the player (and the
        # channel) sees what was mined. Send the receipt FIRST so a bus
        # listener exception in _fan_out never eats the user-facing
        # reply (the original bug: a fan_out crash would silently
        # swallow the receipt, leaving the player wondering whether
        # the mine even happened).
        usd_value = res.qty_human * res.oracle_after
        usd_tag = f"  ~ **{fmt_usd(usd_value)}**" if usd_value > 0 else ""
        junk_meta = dc.junk_meta(res.junk_drop_key) if res.junk_drop_key else None
        junk_line = (
            f"\n+{junk_meta['emoji']} **{junk_meta['name']}** "
            f"({str(junk_meta.get('kind', '')).title()})"
            if junk_meta else ""
        )
        receipt = card(
            "\U000026CF \U0001F4AB Pickaxe Strike",
            color=C_GOLD,
        ).description(
            f"Mined **{_fmt_ore(res.ore_symbol, res.qty_human)}**{usd_tag}\n"
            f"-# Oracle: ${res.oracle_before:,.6f} -> ${res.oracle_after:,.6f}"
            f"  (slippage {res.impact_pct * 100:.2f}%)"
            f"{junk_line}"
        ).build()
        # Attach Use / Sell / Bag quick-actions when a junk item drops.
        # Pass ``view`` to followup.send only when one exists -- discord.py
        # 2.3 raises ``TypeError`` on ``view=None``, which would skip the
        # receipt AND the embed rebuild below since the surrounding
        # handler only catches HTTPException.
        #
        # Receipts are ephemeral so they no longer spam the play channel
        # alongside the room embed everyone is already watching. Going
        # ephemeral also sidesteps the "embed posts in parent channel
        # instead of the thread" routing quirk: ephemeral followups are
        # delivered to the player privately regardless of where the
        # interaction originated.
        drop_view: discord.ui.View | None = None
        if res.junk_drop_key:
            drop_view = _JunkDropView(self.cog, self.ctx, res.junk_drop_key)
        try:
            if drop_view is not None:
                sent = await interaction.followup.send(
                    embed=receipt, view=drop_view, ephemeral=True,
                )
                drop_view.message = sent
            else:
                await interaction.followup.send(embed=receipt, ephemeral=True)
        except discord.HTTPException:
            log.exception(
                "delve mine: receipt send failed uid=%s gid=%s",
                self.owner_id, self.ctx.guild_id,
            )
        # Bus events run AFTER the receipt so a misbehaving listener
        # can't suppress the player-facing reply.
        try:
            await self.cog._fan_out(
                self.owner_id, self.ctx.guild_id, "delve_mine",
            )
            sym_trigger = {
                dc.COPPER_SYMBOL: "delve_mined_copper",
                dc.SILVER_SYMBOL: "delve_mined_silver",
                dc.GOLD_SYMBOL:   "delve_mined_gold",
            }.get(res.ore_symbol)
            if sym_trigger:
                await self.cog._fan_out(
                    self.owner_id, self.ctx.guild_id, sym_trigger,
                    amount=max(1, int(res.qty_human)),
                )
        except Exception:
            log.debug(
                "delve mine: fan_out failed uid=%s gid=%s",
                self.owner_id, self.ctx.guild_id, exc_info=True,
            )
        # Auto-advance to the next room so the player doesn't have to
        # tap Next after every successful mine. mine_ore already cleared
        # the room to 'empty'; advance_room rolls the next room type.
        try:
            await dsvc.advance_room(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
            )
        except ValueError:
            log.debug(
                "delve mine: advance_room raised after mining "
                "uid=%s gid=%s", self.owner_id, self.ctx.guild_id,
                exc_info=True,
            )
        await self.cog._rebuild_room_message(self.ctx, self, replace_view=True)

    @discord.ui.button(label="Open", style=discord.ButtonStyle.success, emoji="\U0001F4B0", row=0)
    async def btn_open(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            chest = await dsvc.open_chest(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
            )
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_AMBER).build(),
                ephemeral=True,
            )
            return
        oracles = await _oracles(self.ctx)
        rune_amt = float(chest.rune_amount)
        relic_meta = dc.relic_meta(chest.relic_key) if chest.relic_key else None
        relic_line = (
            f"\n{relic_meta['emoji']} **Relic dropped!** {relic_meta['name']} "
            f"({str(relic_meta.get('rarity', 'common')).title()}) -- "
            f"{relic_meta.get('blurb', '')}"
            if relic_meta else ""
        )
        junk_meta = dc.junk_meta(chest.junk_drop_key) if chest.junk_drop_key else None
        junk_line = (
            f"\n{junk_meta['emoji']} +**{junk_meta['name']}** "
            f"({str(junk_meta.get('kind', '')).title()})"
            if junk_meta else ""
        )
        debt_line = (
            f"\n\U0001F64F **Shrine debt paid off!** Rune payout x{chest.shrine_debt_mult:g}."
            if chest.shrine_debt_mult and chest.shrine_debt_mult > 1.0 else ""
        )
        receipt = card(
            "\U0001F4B0 \U00002728 Chest cracked!", color=C_GOLD,
        ).description(
            f"+**{_fmt_rune(rune_amt)}**"
            f"{_with_usd(rune_amt, oracles.get(dc.RUNE_SYMBOL, 0.0))}"
            f"{debt_line}{relic_line}{junk_line}"
        ).build()
        # If a junk item dropped, attach a Use / Sell / Bag quick-action
        # view to the receipt. Skip ``view=`` entirely when no drop -- on
        # discord.py 2.3 ``view=None`` raises TypeError which would kill
        # the handler before _rebuild_room_message runs, leaving the embed
        # frozen on the chest room.
        drop_view: discord.ui.View | None = None
        if chest.junk_drop_key:
            drop_view = _JunkDropView(self.cog, self.ctx, chest.junk_drop_key)
        try:
            if drop_view is not None:
                sent = await interaction.followup.send(
                    embed=receipt, view=drop_view, ephemeral=True,
                )
                drop_view.message = sent
            else:
                await interaction.followup.send(embed=receipt, ephemeral=True)
        except discord.HTTPException:
            pass
        await self.cog._rebuild_room_message(self.ctx, self, replace_view=True)

    @discord.ui.button(label="Pray", style=discord.ButtonStyle.success, emoji="\U0001F64F", row=0)
    async def btn_pray(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        """Pray at the shrine in the current room. Mirrors ``,delve pray``.

        The room-view shipped without this button -- shrine rooms could
        only be activated via the slash command and the shrine sat
        inert when discovered. Now Pray appears whenever the room type
        is ``shrine`` (toggled via _delve_set_button_visibility).
        """
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            res = await dsvc.pray_at_shrine(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
            )
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_AMBER).build(),
                ephemeral=True,
            )
            return
        oracles = await _oracles(self.ctx)
        if res.outcome_key == "shrine_curse":
            frame = dc.FRAMES.get("shrine_curse", "")
            color = C_PURPLE
        else:
            frame = dc.FRAMES.get("shrine_blessing", "")
            color = C_GOLD if res.outcome_key == "relic_gift" else C_TEAL
        detail_lines: list[str] = [f"_{res.blurb}_"]
        if res.hp_delta > 0:
            detail_lines.append(f"\U00002764\U0000FE0F **+{res.hp_delta}** HP restored.")
        elif res.hp_delta < 0:
            detail_lines.append(f"\U0001F494 **{res.hp_delta}** HP (a debt accrues).")
        if res.rune_credited > 0:
            detail_lines.append(
                f"\U0001F4B0 +**{_fmt_rune(res.rune_credited)}**"
                f"{_with_usd(res.rune_credited, oracles.get(dc.RUNE_SYMBOL, 0.0))}"
            )
        if res.buff_key:
            label_tmpl = {
                "shrine_atk": "Smiting Blessing (+%d%% ATK, %d rounds)",
                "shrine_spd": "Swift Blessing (+%d%% SPD, %d rounds)",
            }.get(res.buff_key, res.buff_key + " (+%d%%, %d rounds)")
            detail_lines.append(
                "\U0001F4AB " + label_tmpl % (
                    int(res.buff_value * 100), res.buff_duration,
                )
            )
        if res.relic_key:
            rmeta = dc.relic_meta(res.relic_key) or {}
            detail_lines.append(
                f"{rmeta.get('emoji', '')} **Relic gift:** "
                f"{rmeta.get('name', res.relic_key)} "
                f"({str(rmeta.get('rarity', 'common')).title()}) -- "
                f"_{rmeta.get('blurb', '')}_"
            )
        title = f"\U0001F64F {res.boon_name}"
        desc = f"```\n{frame}\n```\n" + "\n".join(detail_lines)
        try:
            await interaction.followup.send(
                embed=card(title, description=desc, color=color).build(),
                ephemeral=True,
            )
        except discord.HTTPException:
            log.exception(
                "delve pray (button): receipt send failed uid=%s gid=%s",
                self.owner_id, self.ctx.guild_id,
            )
        await self.cog._rebuild_room_message(self.ctx, self, replace_view=True)

    @discord.ui.button(label="Descend", style=discord.ButtonStyle.danger, emoji="\U0001F53D", row=0)
    async def btn_descend(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            await dsvc.descend(self.ctx.db, self.ctx.guild_id, self.owner_id)
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_AMBER).build(),
                ephemeral=True,
            )
            return
        state_after = await dsvc.list_state(
            self.ctx.db, self.ctx.guild_id, self.owner_id,
        )
        await self.cog._fan_out(
            self.owner_id, self.ctx.guild_id, "delve_floor_reached",
            amount=int(state_after.get("current_floor") or 0),
        )
        await self.cog._rebuild_room_message(self.ctx, self, replace_view=True)

    @discord.ui.button(label="Rest", style=discord.ButtonStyle.secondary, emoji="\U0001F6CC", row=1)
    async def btn_rest(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        state = await dsvc.list_state(
            self.ctx.db, self.ctx.guild_id, self.owner_id,
        )
        if state.get("current_mob_state"):
            await interaction.followup.send(
                embed=card(
                    description="Can't rest mid-combat. Flee or finish the fight first.",
                    color=C_AMBER,
                ).build(),
                ephemeral=True,
            )
            return
        if not state.get("run_id"):
            await interaction.followup.send(
                embed=card(
                    description="You're already on the surface!",
                    color=C_NEUTRAL,
                ).build(),
                ephemeral=True,
            )
            return
        await dsvc.end_run(self.ctx.db, self.ctx.guild_id, self.owner_id, "rest")
        rested = card(
            "\U0001F6CC \U0001F4AB Run ended -- full heal!",
            color=C_SUCCESS,
        ).description(
            "You retreat to the surface. HP fully restored.\n"
            "`,delve start` whenever you're ready for another run."
        ).build()
        if self.message is not None:
            try:
                await self.message.edit(embed=rested, view=None)
            except discord.HTTPException:
                pass
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        self.stop()

    @discord.ui.button(
        label="Junk", style=discord.ButtonStyle.secondary,
        emoji="\U0001F392", row=1,
    )
    async def btn_junk(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        """Open the player's junk inventory inline.

        Renders an ephemeral panel with the same shape as ``,delve junk``
        plus per-row Use / Sell quick-action buttons via
        ``_JunkInventoryView`` so the player can drain salvage / pop a
        Healing Herb / Smoke Bomb out of a fight without leaving the
        room view.
        """
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        state = await dsvc.ensure_state(
            self.ctx.db, self.ctx.guild_id, self.owner_id,
        )
        junk_inv = state.get("junk_inventory") or {}
        if isinstance(junk_inv, str):
            try:
                import json as _json
                junk_inv = _json.loads(junk_inv) if junk_inv else {}
            except Exception:
                junk_inv = {}
        embed = _junk_panel_embed(self.ctx.author, junk_inv)
        view = _JunkInventoryView(self.cog, self.ctx, junk_inv)
        try:
            sent = await interaction.followup.send(
                embed=embed, view=view, ephemeral=True,
            )
            view.message = sent
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Bump", style=discord.ButtonStyle.secondary, emoji="\U0001F53C", row=4)
    async def btn_bump(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        """Re-post this room panel at the end of the channel.

        Mirrors the BumpButton in core/framework/persistent_embeds.py but
        is declared inline here because the room view also needs to
        rebind ``self.message`` to the new copy so subsequent action
        buttons keep editing the right message. Sits alone on row 4
        per the project-wide refresh/bump bottom-row convention.
        """
        await _bump_panel(self, interaction)


class _JunkDropView(discord.ui.View):
    """Quick-action buttons attached to a chest / mine / kill receipt
    when a junk item drops.

    Renders Use (only if usable), Sell (one-shot for that drop), and
    Open Bag (full junk panel). Owner-locked, ephemeral, 5-min timeout.
    The ``Refresh`` -free convention applies: this short-lived view
    only carries action buttons + a single bottom-row Bag opener, so
    no refresh is needed.
    """

    def __init__(
        self, cog: "Dungeon", ctx: DiscoContext, junk_key: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.junk_key = str(junk_key)
        self.message: discord.Message | None = None
        meta = dc.junk_meta(junk_key) or {}
        kind = str(meta.get("kind") or "salvage")
        is_usable = kind == "usable"
        # Use button only renders for usable kinds; salvage / mat skip
        # straight to the Sell / Bag pair.
        if is_usable:
            self.add_item(_JunkUseQuickButton(junk_key, row=0))
        self.add_item(_JunkSellQuickButton(junk_key, row=0))
        self.add_item(_JunkBagQuickButton(row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Not your drop.", ephemeral=True,
            )
            return False
        return True


class _JunkUseQuickButton(discord.ui.Button):
    """``Use`` button on a junk-drop receipt -- pops the dropped item."""

    def __init__(self, junk_key: str, *, row: int = 0) -> None:
        meta = dc.junk_meta(junk_key) or {}
        super().__init__(
            label=f"Use {meta.get('name', junk_key)}",
            emoji=meta.get("emoji"),
            style=discord.ButtonStyle.success,
            row=row,
        )
        self.junk_key = junk_key

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_JunkDropView" = self.view  # type: ignore[assignment]
        try:
            res = await dsvc.use_junk_item(
                view.ctx.db, view.ctx.guild_id, view.owner_id, self.junk_key,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        meta = dc.junk_meta(res.key) or {}
        await interaction.response.send_message(
            f"{meta.get('emoji', '')} **{meta.get('name', res.key)}** -- {res.detail}",
            ephemeral=True,
        )
        # Disable so the player can't double-pop a one-shot drop.
        for child in view.children:
            try:
                if isinstance(child, _JunkUseQuickButton):
                    child.disabled = True
            except Exception:
                pass
        if view.message is not None:
            try:
                await view.message.edit(view=view)
            except discord.HTTPException:
                pass


class _JunkSellQuickButton(discord.ui.Button):
    """``Sell`` button on a junk-drop receipt -- cashes that one stack."""

    def __init__(self, junk_key: str, *, row: int = 0) -> None:
        super().__init__(
            label="Sell",
            emoji="\U0001F4B0",
            style=discord.ButtonStyle.secondary,
            row=row,
        )
        self.junk_key = junk_key

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_JunkDropView" = self.view  # type: ignore[assignment]
        try:
            rune_h, sold = await dsvc.sell_junk(
                view.ctx.db, view.ctx.guild_id, view.owner_id, self.junk_key,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        oracles = await _oracles(view.ctx)
        bits = []
        for k, qty in sold.items():
            m = dc.junk_meta(k) or {}
            bits.append(f"{m.get('emoji', '')} **{m.get('name', k)}** x{qty}")
        await interaction.response.send_message(
            "\n".join(bits)
            + f"\n\n+**{_fmt_rune(rune_h)}**"
            + _with_usd(rune_h, oracles.get(dc.RUNE_SYMBOL, 0.0)),
            ephemeral=True,
        )
        for child in view.children:
            try:
                if isinstance(child, (_JunkUseQuickButton, _JunkSellQuickButton)):
                    child.disabled = True
            except Exception:
                pass
        if view.message is not None:
            try:
                await view.message.edit(view=view)
            except discord.HTTPException:
                pass


class _JunkBagQuickButton(discord.ui.Button):
    """``Bag`` button -- opens the full junk inventory panel ephemerally."""

    def __init__(self, *, row: int = 0) -> None:
        super().__init__(
            label="Bag",
            emoji="\U0001F392",
            style=discord.ButtonStyle.primary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_JunkDropView" = self.view  # type: ignore[assignment]
        state = await dsvc.ensure_state(
            view.ctx.db, view.ctx.guild_id, view.owner_id,
        )
        junk_inv = state.get("junk_inventory") or {}
        if isinstance(junk_inv, str):
            try:
                import json as _json
                junk_inv = _json.loads(junk_inv) if junk_inv else {}
            except Exception:
                junk_inv = {}
        embed = _junk_panel_embed(view.ctx.author, junk_inv)
        bag_view = _JunkInventoryView(view.cog, view.ctx, junk_inv)
        await interaction.response.send_message(
            embed=embed, view=bag_view, ephemeral=True,
        )


class _JunkInventoryView(discord.ui.View):
    """Full ``,delve junk`` panel with per-kind action buttons.

    Builds a compact action surface from the current junk inventory:
    one Use button per usable, one Sell-All button, one Sell-Salvage
    button. Owner-locked, ephemeral, 5-min timeout. Action rows fill
    rows 0/1; the Refresh + Bag-bump pair sits alone on row 4 per the
    project-wide bottom-row convention.
    """

    def __init__(
        self, cog: "Dungeon", ctx: DiscoContext, junk_inv: dict,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.message: discord.Message | None = None
        # Action layout: surface up to 4 usable Use buttons (one per
        # owned usable) on row 0, then Sell-Salvage / Sell-Mats /
        # Sell-All on row 1.
        usable_keys: list[str] = []
        for k, qty in (junk_inv or {}).items():
            if int(qty or 0) <= 0:
                continue
            m = dc.junk_meta(k) or {}
            if str(m.get("kind") or "") == "usable":
                usable_keys.append(k)
        for i, k in enumerate(usable_keys[:4]):
            self.add_item(_JunkUseQuickButton(k, row=0))
        self.add_item(_JunkBulkSellButton(kind_filter="salvage", row=1))
        self.add_item(_JunkBulkSellButton(kind_filter="mat", row=1))
        self.add_item(_JunkBulkSellButton(kind_filter=None, row=1))
        self.add_item(_JunkRefreshButton(row=4))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Not your bag.", ephemeral=True,
            )
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction) -> None:
        """Re-fetch junk inventory and rebuild the view in place."""
        state = await dsvc.ensure_state(
            self.ctx.db, self.ctx.guild_id, self.owner_id,
        )
        junk_inv = state.get("junk_inventory") or {}
        if isinstance(junk_inv, str):
            try:
                import json as _json
                junk_inv = _json.loads(junk_inv) if junk_inv else {}
            except Exception:
                junk_inv = {}
        embed = _junk_panel_embed(self.ctx.author, junk_inv)
        # Replace items with a fresh layout (use buttons can change as
        # usables get consumed).
        self.clear_items()
        usable_keys: list[str] = []
        for k, qty in (junk_inv or {}).items():
            if int(qty or 0) <= 0:
                continue
            m = dc.junk_meta(k) or {}
            if str(m.get("kind") or "") == "usable":
                usable_keys.append(k)
        for k in usable_keys[:4]:
            self.add_item(_JunkUseQuickButton(k, row=0))
        self.add_item(_JunkBulkSellButton(kind_filter="salvage", row=1))
        self.add_item(_JunkBulkSellButton(kind_filter="mat", row=1))
        self.add_item(_JunkBulkSellButton(kind_filter=None, row=1))
        self.add_item(_JunkRefreshButton(row=4))
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass


class _JunkBulkSellButton(discord.ui.Button):
    """Bulk-sell button for the junk panel.

    ``kind_filter`` of "salvage" or "mat" sells everything of that
    kind. ``None`` sells the whole bag (usables + salvage + mats).
    """

    def __init__(
        self, *, kind_filter: str | None, row: int = 1,
    ) -> None:
        if kind_filter == "salvage":
            label = "Sell Salvage"
            emoji = "\U0001F5D1"
            style = discord.ButtonStyle.secondary
        elif kind_filter == "mat":
            label = "Sell Mats"
            emoji = "\U00002692\U0000FE0F"
            style = discord.ButtonStyle.secondary
        else:
            label = "Sell All"
            emoji = "\U0001F4B0"
            style = discord.ButtonStyle.danger
        super().__init__(label=label, emoji=emoji, style=style, row=row)
        self.kind_filter = kind_filter

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_JunkInventoryView" = self.view  # type: ignore[assignment]
        # Pull junk and filter by kind on the cog side so the service
        # ``sell_junk`` (which only takes a single key or "all") still
        # works without a new service path.
        state = await dsvc.ensure_state(
            view.ctx.db, view.ctx.guild_id, view.owner_id,
        )
        junk_inv = state.get("junk_inventory") or {}
        if isinstance(junk_inv, str):
            try:
                import json as _json
                junk_inv = _json.loads(junk_inv) if junk_inv else {}
            except Exception:
                junk_inv = {}
        if not junk_inv:
            await interaction.response.send_message(
                "Junk bag is empty.", ephemeral=True,
            )
            return
        if self.kind_filter is None:
            try:
                rune_h, sold = await dsvc.sell_junk(
                    view.ctx.db, view.ctx.guild_id, view.owner_id, None,
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        else:
            # Sell each filter-matching key in succession.
            sold: dict[str, int] = {}
            rune_h = 0.0
            for k in list(junk_inv.keys()):
                m = dc.junk_meta(k) or {}
                if str(m.get("kind") or "") != self.kind_filter:
                    continue
                try:
                    r, s = await dsvc.sell_junk(
                        view.ctx.db, view.ctx.guild_id, view.owner_id, k,
                    )
                    rune_h += r
                    for sk, sq in s.items():
                        sold[sk] = sold.get(sk, 0) + sq
                except ValueError:
                    continue
            if not sold:
                await interaction.response.send_message(
                    f"No {self.kind_filter} junk to sell.", ephemeral=True,
                )
                return
        oracles = await _oracles(view.ctx)
        bits = []
        for k, qty in sold.items():
            m = dc.junk_meta(k) or {}
            bits.append(f"{m.get('emoji', '')} **{m.get('name', k)}** x{qty}")
        await interaction.response.send_message(
            "\n".join(bits)
            + f"\n\n+**{_fmt_rune(rune_h)}**"
            + _with_usd(rune_h, oracles.get(dc.RUNE_SYMBOL, 0.0)),
            ephemeral=True,
        )
        # Refresh the junk panel in place so the items the player just
        # sold disappear from the view. Without this the next bulk-sell
        # click hit a now-empty inventory and replied "Junk inventory
        # is empty.", which the player read as the buttons being broken.
        try:
            state_after = await dsvc.ensure_state(
                view.ctx.db, view.ctx.guild_id, view.owner_id,
            )
            junk_after = state_after.get("junk_inventory") or {}
            if isinstance(junk_after, str):
                try:
                    import json as _json
                    junk_after = _json.loads(junk_after) if junk_after else {}
                except Exception:
                    junk_after = {}
            view.clear_items()
            usable_keys: list[str] = []
            for k, qty in (junk_after or {}).items():
                if int(qty or 0) <= 0:
                    continue
                m = dc.junk_meta(k) or {}
                if str(m.get("kind") or "") == "usable":
                    usable_keys.append(k)
            for k in usable_keys[:4]:
                view.add_item(_JunkUseQuickButton(k, row=0))
            view.add_item(_JunkBulkSellButton(kind_filter="salvage", row=1))
            view.add_item(_JunkBulkSellButton(kind_filter="mat", row=1))
            view.add_item(_JunkBulkSellButton(kind_filter=None, row=1))
            view.add_item(_JunkRefreshButton(row=4))
            embed = _junk_panel_embed(view.ctx.author, junk_after)
            # The panel may have been opened either as a normal
            # message (view.message set) or ephemerally from a Bag
            # quick-action (view.message None; the message lives on
            # the originating interaction). Try both surfaces.
            target_msg = view.message or interaction.message
            if target_msg is not None:
                try:
                    await target_msg.edit(embed=embed, view=view)
                except discord.HTTPException:
                    pass
        except Exception:
            log.debug(
                "delve junk panel refresh after bulk sell failed",
                exc_info=True,
            )


class _JunkRefreshButton(discord.ui.Button):
    """Bottom-row refresh for the junk inventory view.

    Lives alone on row 4 per the project-wide refresh-row convention.
    """

    def __init__(self, *, row: int = 4) -> None:
        super().__init__(
            label="Refresh",
            emoji="\U0001F504",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_JunkInventoryView" = self.view  # type: ignore[assignment]
        await view._redraw(interaction)


class _DelveAbilityButton(discord.ui.Button):
    """One slot in the in-combat ability picker.

    Bound to a specific ability_key at construction. Label / emoji /
    style come from ``dc.ABILITIES``; the disabled state and the "(CDn)"
    suffix on the label get refreshed by ``_DelveBattleView._refresh_ability_buttons``
    every time the parent view re-renders so the picker always reflects
    live cooldowns.
    """

    _STYLE_BY_KIND: dict[str, discord.ButtonStyle] = {
        "melee":  discord.ButtonStyle.success,
        "ranged": discord.ButtonStyle.success,
        "spell":  discord.ButtonStyle.primary,
    }

    def __init__(self, ability_key: str, *, row: int = 0) -> None:
        ameta = dc.ability_meta(ability_key) or {}
        kind = str(ameta.get("kind") or "melee")
        super().__init__(
            label=str(ameta.get("name") or ability_key),
            emoji=str(ameta.get("emoji") or "\U00002728"),
            style=self._STYLE_BY_KIND.get(kind, discord.ButtonStyle.success),
            row=row,
            custom_id=f"delve_ability_{ability_key}",
        )
        self.ability_key = ability_key

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DelveBattleView" = self.view  # type: ignore[assignment]
        await view._act(interaction, "ability", ability_key=self.ability_key)


class _DelveBattleView(discord.ui.View):
    """Buttons that drive an interactive combat embed in-place."""

    def __init__(
        self, cog: "Dungeon", ctx: DiscoContext, *,
        class_key: str = "",
    ) -> None:
        # No timeout: combat embeds persist until KO / flee / room change.
        super().__init__(timeout=None)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.message: discord.Message | None = None
        self.wild_buddy: dict | None = None  # set if this is a wild buddy battle
        self.over = False
        self.class_key = str(class_key or "")
        self._ability_buttons: list[_DelveAbilityButton] = []
        self._add_ability_buttons()

    def _add_ability_buttons(self) -> None:
        """Append one button per class ability in row 0.

        Falls back silently when ``class_key`` is empty -- the player
        will see Strike + Potion in row 0 only, without the ability
        picker. They can still use the bot via the legacy
        ``,delve skill`` prefix command.
        """
        keys = dc.class_abilities(self.class_key)
        if not keys:
            return
        for key in keys:
            btn = _DelveAbilityButton(key, row=0)
            self._ability_buttons.append(btn)
            self.add_item(btn)

    def _refresh_ability_buttons(self, state: dict) -> None:
        """Update each ability button's label suffix + disabled state.

        Reads the player's per-ability cooldowns from
        ``state.player_buffs`` (the new ``_ability_cd_<key>`` entries)
        plus the legacy ``skill_cd_remaining`` column for the primary
        ability. Disables ranged abilities when the player isn't
        wielding a ranged weapon, and any ability still on cooldown.
        """
        if not self._ability_buttons:
            return
        buffs = dict(state.get("player_buffs") or {})
        skill_cd = int(state.get("skill_cd_remaining") or 0)
        weapon = dc.weapon_meta(state.get("equipped_weapon") or "") or {}
        is_ranged = str(weapon.get("attack_kind") or "melee") == "ranged"
        prim = (dc.class_abilities(self.class_key) or ("",))[0]
        for btn in self._ability_buttons:
            ameta = dc.ability_meta(btn.ability_key) or {}
            base_label = str(ameta.get("name") or btn.ability_key)
            cd = 0
            if btn.ability_key == prim:
                cd = max(cd, skill_cd)
            payload = buffs.get("_ability_cd_" + btn.ability_key)
            if isinstance(payload, dict):
                # Subtract 1 since the buff hasn't ticked for THIS round yet.
                cd = max(cd, max(0, int(payload.get("duration") or 0) - 1))
            wrong_weapon = (
                str(ameta.get("kind") or "") == "ranged" and not is_ranged
            )
            btn.disabled = (cd > 0) or wrong_weapon
            if cd > 0:
                btn.label = f"{base_label} ({cd})"
            elif wrong_weapon:
                btn.label = f"{base_label} (need bow)"
            else:
                btn.label = base_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Not your fight. Run `,delve` to start your own.",
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
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _disable_all(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass

    async def _act(
        self, interaction: discord.Interaction, mode: str,
        *, ability_key: str | None = None,
    ) -> None:
        """Resolve a swing or flee and edit the embed in-place."""
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            if mode == "flee":
                res = await dsvc.resolve_flee(
                    self.ctx.db, self.ctx.guild_id, self.owner_id,
                )
            elif mode == "ability":
                res = await dsvc.resolve_attack(
                    self.ctx.db, self.ctx.guild_id, self.owner_id,
                    mode="ability", ability_key=ability_key,
                )
            else:
                res = await dsvc.resolve_attack(
                    self.ctx.db, self.ctx.guild_id, self.owner_id, mode=mode,
                )
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_ERROR).build(),
                ephemeral=True,
            )
            return

        # Drops + lifecycle hooks (mirrors the legacy command paths).
        granted_badges: list[str] = []
        if res.outcome == "mob_dead":
            await dsvc.credit_combat_drops(
                self.ctx.db, self.ctx.guild_id, self.owner_id, res,
            )
            granted_badges += await self.cog._fan_out(
                self.owner_id, self.ctx.guild_id, "delve_kill",
            )
            if res.boss_kill:
                granted_badges += await self.cog._fan_out(
                    self.owner_id, self.ctx.guild_id, "delve_boss_kill",
                )
            if res.rune_drop_human > 0:
                granted_badges += await self.cog._fan_out(
                    self.owner_id, self.ctx.guild_id,
                    "delve_rune_earned", amount=int(res.rune_drop_human),
                )
            # Buddy egg drop chance on a kill -- routes through the
            # fishing held-egg system since that table already exists.
            try:
                await self._maybe_drop_buddy_egg()
            except Exception:
                log.debug("delve battle: buddy egg drop failed", exc_info=True)
        if res.outcome == "player_dead":
            await dsvc.end_run(self.ctx.db, self.ctx.guild_id, self.owner_id, "died")

        # Render the new state on the same message.
        embed, scene_file = await self.cog._battle_embed_from_result(
            self.ctx, res, self.wild_buddy,
        )
        edit_kwargs: dict = {"embed": embed}
        if scene_file is not None:
            edit_kwargs["attachments"] = [scene_file]
        if res.outcome in ("mob_dead", "player_dead", "fled"):
            await self._disable_all()
            self.over = True
            self.stop()

            # Hand off to the room view so the player can press Next /
            # Rest from the same chat slot instead of having to type
            # ,delve next manually after every kill.
            if self.message is not None and res.outcome in ("mob_dead", "fled"):
                state_after = await dsvc.list_state(
                    self.ctx.db, self.ctx.guild_id, self.owner_id,
                )
                if state_after.get("run_id"):
                    room_view = _DelveRoomView(self.cog, self.ctx)
                    rt_after = str(state_after.get("current_room_type") or "empty")
                    _delve_set_button_visibility(room_view, rt_after)
                    try:
                        await self.message.edit(
                            view=room_view, **edit_kwargs,
                        )
                        room_view.message = self.message
                    except discord.HTTPException:
                        log.debug("delve battle handoff: edit failed", exc_info=True)
                    # Junk drop quick-action followup: if the kill rolled
                    # a junk item, drop an ephemeral Use / Sell / Bag
                    # view so the player can act on it without typing.
                    if res.outcome == "mob_dead" and res.junk_drop_key:
                        try:
                            jmeta = dc.junk_meta(res.junk_drop_key) or {}
                            drop_view = _JunkDropView(
                                self.cog, self.ctx, res.junk_drop_key,
                            )
                            note = card(
                                f"\U0001F392 Loot: {jmeta.get('emoji', '')} "
                                f"{jmeta.get('name', res.junk_drop_key)}",
                                color=C_NEUTRAL,
                                description=(
                                    f"_{jmeta.get('blurb', '')}_\n"
                                    f"({str(jmeta.get('kind', '')).title()})"
                                ),
                            ).build()
                            sent = await interaction.followup.send(
                                embed=note, view=drop_view, ephemeral=True,
                            )
                            drop_view.message = sent
                        except Exception:
                            log.debug(
                                "delve battle: junk drop view send failed",
                                exc_info=True,
                            )
                    if granted_badges:
                        await self.cog._announce_badges(
                            interaction.channel, granted_badges,
                        )
                    return
        if self.message is not None:
            # Refresh ability button labels (cooldown indicators) before
            # the in-place edit so the next round shows accurate state.
            try:
                live_state = await dsvc.list_state(
                    self.ctx.db, self.ctx.guild_id, self.owner_id,
                )
                self._refresh_ability_buttons(live_state)
            except Exception:
                log.debug("delve battle: ability button refresh failed", exc_info=True)
            try:
                await self.message.edit(view=self, **edit_kwargs)
            except discord.HTTPException:
                log.debug("delve battle: edit failed", exc_info=True)
        if granted_badges:
            await self.cog._announce_badges(
                interaction.channel, granted_badges,
            )

    async def _maybe_drop_buddy_egg(self) -> None:
        """8% chance: drop a buddy egg into the player's held-egg slot."""
        if _random.random() >= _DELVE_BUDDY_EGG_DROP_CHANCE:
            return
        try:
            from services import fishing as fish_svc
            await fish_svc.hatch_fishing_buddy(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
                source="delve",
            )
            log.info(
                "[delve egg] guild=%d uid=%d dropped buddy egg from kill",
                self.ctx.guild_id, self.owner_id,
            )
        except Exception:
            log.debug("delve battle: egg hatch service failed", exc_info=True)

    @discord.ui.button(label="Strike", style=discord.ButtonStyle.primary, emoji="\U00002694", row=0)
    async def btn_strike(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self._act(interaction, "attack")

    @discord.ui.button(label="Potion", style=discord.ButtonStyle.secondary, emoji="\U0001F9EA", row=1)
    async def btn_potion(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        """Drink the strongest potion in your bag for a mid-battle heal.

        Scans every ``kind == "heal"`` consumable in the catalog and picks
        the highest-value one the player owns, so any new healing potion
        added to ``CONSUMABLES`` is auto-eligible without code changes.
        """
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        # Read consumables off the live state.
        state = await dsvc.list_state(self.ctx.db, self.ctx.guild_id, self.owner_id)
        cons = dict(state.get("consumables") or {})
        # Catalog-driven: every heal-kind consumable is eligible, ranked by
        # heal value descending so a new "potion_supreme" between major
        # and elixir slots in correctly without touching this button.
        heal_keys = sorted(
            (
                k for k, meta in dc.CONSUMABLES.items()
                if str(meta.get("kind") or "") == "heal"
            ),
            key=lambda k: float(dc.CONSUMABLES[k].get("value") or 0.0),
            reverse=True,
        )
        chosen = next((k for k in heal_keys if int(cons.get(k) or 0) > 0), "")
        if not chosen:
            await interaction.followup.send(
                embed=card(
                    description=(
                        "\U0001F62F Your bag is empty! "
                        "Buy a potion at `,delve shop`."
                    ),
                    color=C_AMBER,
                ).build(),
                ephemeral=True,
            )
            return
        try:
            heal_res = await dsvc.use_consumable(
                self.ctx.db, self.ctx.guild_id, self.owner_id, chosen,
            )
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_ERROR).build(),
                ephemeral=True,
            )
            return
        meta = dc.consumable_meta(chosen) or {}
        heal_embed = card(
            "\U0001F49A Yum! \U00002728",
            color=C_SUCCESS,
        ).description(
            f"{meta.get('emoji', '')} **{meta.get('name', chosen)}** -- "
            f"{heal_res.detail}\n"
            f"`{_cute_hp_bar(heal_res.player_hp, heal_res.player_max_hp)}`  "
            f"`{heal_res.player_hp}/{heal_res.player_max_hp} HP`"
        ).build()
        await interaction.followup.send(embed=heal_embed, ephemeral=True)

    @discord.ui.button(label="Capture", style=discord.ButtonStyle.secondary, emoji="\U0001F9F2", row=1)
    async def btn_capture(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            cap = await dsvc.attempt_capture(
                self.ctx.db, self.ctx.guild_id, self.owner_id, charm=False,
            )
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_ERROR).build(),
                ephemeral=True,
            )
            return
        if cap.success and self.wild_buddy:
            # Wild buddy capture also writes to cc_buddies so the player
            # can use them as a regular pet -- mirrors fishing wild capture.
            #
            # Translate the dungeon mob into a real buddies_config species
            # so the resulting cc_buddies row inherits a portrait,
            # ability_key, name pool, and rarity-scaled stats. The mob's
            # tier picks the species pool; rarity is rolled fresh via
            # ``roll_rarity()`` so the same species can show up at any
            # rarity (mirrors the fishing wild-capture path).
            capture_dest: str | None = None
            try:
                from services.buddy_economy import (
                    capture_destination as _capture_destination,
                )
                capture_dest = await _capture_destination(
                    self.ctx.db, self.ctx.guild_id, self.owner_id,
                )
                if capture_dest is None:
                    log.info(
                        "delve wild buddy: battle + storage full uid=%s gid=%s "
                        "-- captured into dungeon_party only",
                        self.owner_id, self.ctx.guild_id,
                    )
                else:
                    capture_status = (
                        "owned" if capture_dest == "battle" else "stored"
                    )
                    mob_tier = int(self.wild_buddy.get("rarity_tier") or 1)
                    species = _pick_dungeon_buddy_species(mob_tier)
                    rolled_tier = roll_rarity()
                    sp_meta = SPECIES.get(species, {})
                    name_pool = sp_meta.get("name_pool") or [species.title()]
                    name = (
                        str(self.wild_buddy.get("name"))
                        if self.wild_buddy.get("name")
                        else _random.choice(name_pool)
                    )
                    from configs.buddies_config import (
                        roll_gender as _roll_gender,
                        xp_for_level as _xp_for_level,
                    )
                    _cap_lvl = int(self.wild_buddy.get("level") or 1)
                    await self.ctx.db.fetch_one(
                        """
                        INSERT INTO cc_buddies
                            (guild_id, owner_user_id, species, name,
                             status, is_active, rarity_tier, level, xp, gender)
                        VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7, $8, $9)
                        RETURNING id
                        """,
                        self.ctx.guild_id, self.owner_id,
                        species, name, str(capture_status),
                        int(rolled_tier),
                        _cap_lvl,
                        int(_xp_for_level(_cap_lvl)),
                        _roll_gender(),
                    )
            except Exception:
                log.debug("delve wild buddy: cc_buddies insert failed", exc_info=True)
        if cap.success:
            verdict = "\U0001F4AB \U00002728 Captured! \U00002728 \U0001F4AB"
            color = C_PURPLE
            if self.wild_buddy and capture_dest == "storage":
                footer = (
                    "Active slots full -- buddy went to your **storage** "
                    "(`,buddy storage`). Withdraw it whenever you free a "
                    "battle slot."
                )
            elif self.wild_buddy and capture_dest is None:
                footer = (
                    "Battle + storage both full! Buddy joined your delve "
                    "party only (see `,delve party`). Free a slot via "
                    "`,buddy store`, surrender, or buy more from `,buddy shop`."
                )
            else:
                footer = (
                    f"\U0001F49E Joins your active roster "
                    f"(rolled {int(cap.chance * 100)}%)"
                )
        else:
            verdict = "\U0001F4A8 Slipped free!"
            color = C_AMBER
            footer = f"Try again at lower HP! (rolled {int(cap.chance * 100)}%)"
        desc = (
            _frame_block("capture") + "\n"
            if cap.success else ""
        ) + "\n".join(cap.log)
        embed = card(verdict, color=color).description(desc).footer(footer).build()
        if cap.success:
            await self._disable_all()
            self.over = True
            self.stop()
        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                log.debug("delve capture: edit failed", exc_info=True)

    @discord.ui.button(label="Flee", style=discord.ButtonStyle.danger, emoji="\U0001F45F", row=1)
    async def btn_flee(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self._act(interaction, "flee")

    @discord.ui.button(label="Bump", style=discord.ButtonStyle.secondary, emoji="\U0001F53C", row=4)
    async def btn_bump(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        """Re-post the battle panel at the end of the channel.

        Sits alone on row 4 per the project-wide refresh/bump
        bottom-row convention -- never shares a row with combat actions."""
        await _bump_panel(self, interaction)


# ============================================================================
# Interactive wild-buddy battle view (delve)
# ============================================================================
# Mirrors cogs/fishing.py _WildBattleView. The wild_battle room used to be
# resolved by typing ,delve battle, which auto-ran the engine and printed a
# log dump. Now the room embed gets a prominent Challenge / Skip pair, and
# Challenge swaps in turn-by-turn Strike / Special / Brace / Risky buttons
# that share the same engine cogs/fishing.py uses (services.buddy_battle's
# LiveBattle + apply_player_action + enemy_ai_turn).

from services.buddy_battle import (
    INTERACTIVE_PLAYER_STAMINA_MAX as _DELVE_PLAYER_STAMINA_MAX,
    INTERACTIVE_SPECIAL_STAMINA_COST as _DELVE_SPECIAL_STAMINA_COST,
    Fighter as _DelveFighter,
    LiveBattle as _DelveLiveBattle,
    apply_player_action as _delve_apply_player_action,
    compute_battle_bonus as _delve_compute_battle_bonus,
    enemy_ai_turn as _delve_enemy_ai_turn,
    hp_bar as _delve_hp_bar,
)


_TIER_WORDS: tuple[str, ...] = (
    "Common", "Uncommon", "Rare", "Epic", "Legendary",
)


def _tier_word(tier: int) -> str:
    return _TIER_WORDS[max(0, min(len(_TIER_WORDS) - 1, int(tier) - 1))]


class _DelveWildBuddyView(discord.ui.View):
    """Interactive wild-buddy battle view for delve wild_battle rooms.

    The room embed attaches this view in place of the old text instruction
    that asked the player to type ``,delve battle``. Layout:

    Initial state:
        Challenge (red ⚔️)  -- enters interactive combat
        Skip / Next (gray)  -- walks past, no counter bump

    On Challenge press, the view rebuilds itself with the four action
    buttons and an in-place round embed; clicks resolve player + AI turns
    locally, only writing to the DB on the final outcome via
    ``services.dungeon.resolve_wild_battle``.
    """

    def __init__(self, cog: "Dungeon", ctx: DiscoContext, wild_buddy: dict) -> None:
        super().__init__(timeout=_BATTLE_VIEW_TIMEOUT_S)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.wild_buddy = dict(wild_buddy or {})
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._resolved = False
        self._battle: _DelveLiveBattle | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Not your delve. Run `,delve` to start your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self._resolved or self.message is None:
            return
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Challenge", emoji="\U00002694",
        style=discord.ButtonStyle.danger, row=0,
    )
    async def btn_challenge(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if self._lock.locked() or self._resolved or self._battle is not None:
            await interaction.response.defer()
            return
        async with self._lock:
            if self._resolved or self._battle is not None:
                return

            active = await self.ctx.db.fetch_one(
                """
                SELECT b.*,
                       (e.expedition_id IS NOT NULL) AS on_expedition,
                       e.ends_at                    AS expedition_ends_at,
                       e.destination                AS expedition_destination
                  FROM cc_buddies b
                  LEFT JOIN buddy_expeditions e
                         ON e.buddy_id = b.id
                        AND e.status   = 'running'
                 WHERE b.guild_id = $1 AND b.owner_user_id = $2
                   AND b.status = 'owned' AND b.is_active = TRUE
                 LIMIT 1
                """,
                self.ctx.guild_id, self.owner_id,
            )
            if not active:
                # No active buddy at all -- check whether the player has
                # ANY owned buddies that aren't on expedition. If every
                # owned buddy is currently deployed the engage button
                # surfaces a tailored 'all buddies are away' message
                # instead of the generic 'activate one' nudge.
                roster = await self.ctx.db.fetch_one(
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
                    self.ctx.guild_id, self.owner_id,
                )
                total = int((roster or {}).get("total") or 0)
                away  = int((roster or {}).get("away")  or 0)
                if total > 0 and total == away:
                    msg = (
                        f"All **{total}** of your buddies are out on "
                        f"expeditions -- there's no one home to fight. "
                        f"`,expedition` to track them."
                    )
                else:
                    msg = (
                        "You need an active CC buddy to fight a wild one. "
                        "Activate one with `,buddy panel`, then come back."
                    )
                await interaction.response.send_message(
                    embed=card(
                        "\U0001F436 No Active Buddy",
                        color=C_AMBER,
                    ).description(msg).build(),
                    ephemeral=True,
                )
                return
            if active.get("on_expedition"):
                ends = active.get("expedition_ends_at")
                when = f" (back {fmt_rel(ends)})" if ends is not None else ""
                await interaction.response.send_message(
                    embed=card(
                        "\U0001F392 Buddy on Expedition",
                        color=C_AMBER,
                    ).description(
                        f"**{active.get('name') or 'Your buddy'}** is away on "
                        f"**{(active.get('expedition_destination') or 'an expedition').replace('_', ' ').title()}**{when}. "
                        f"Send a different buddy on expedition and swap them "
                        f"active, or wait for them to return."
                    ).build(),
                    ephemeral=True,
                )
                return

            try:
                player_f = _DelveFighter.from_row(dict(active))
                enemy_f = _DelveFighter.from_row(dict(self.wild_buddy))
            except Exception:
                log.exception(
                    "delve wild battle: Fighter.from_row failed gid=%s uid=%s",
                    self.ctx.guild_id, self.owner_id,
                )
                self._resolved = True
                try:
                    await interaction.response.edit_message(
                        embed=card(
                            "\U0001F4A5 Wild buddy slipped away",
                            color=C_ERROR,
                        ).description(
                            "Something went wrong setting up the fight. "
                            "No counters bumped."
                        ).build(),
                        view=None,
                    )
                except discord.HTTPException:
                    pass
                return

            self._battle = _DelveLiveBattle(player=player_f, enemy=enemy_f)

            # Surface a slot warning at fight-start so the player knows
            # up front that a successful capture won't drop into their
            # shelter when it's full.
            try:
                from core.framework.slot_warning import maybe_warn_full_slots
                await maybe_warn_full_slots(
                    self.ctx, surface="delve", phase="fight_start",
                )
            except Exception:
                log.debug("delve slot warning failed", exc_info=True)

            self.clear_items()
            self.add_item(self._make_action_button(
                "Strike", "\U00002694", "strike",
                discord.ButtonStyle.primary, row=0,
            ))
            # Special label = the buddy's named ability (e.g. "Pack
            # Howl") so the player can read what they're casting
            # without bouncing to ,buddy info.
            self.add_item(self._make_action_button(
                str(player_f.ability_name or "Special")[:20] or "Special",
                "\U0001F4A5", "special",
                discord.ButtonStyle.success, row=0,
            ))
            self.add_item(self._make_action_button(
                "Brace", "\U0001F6E1️", "brace",
                discord.ButtonStyle.secondary, row=0,
            ))
            self.add_item(self._make_action_button(
                "Risky", "\U0001F3AF", "risky",
                discord.ButtonStyle.danger, row=0,
            ))
            # Pokemon-style capture affordance for the wild-buddy fight.
            # Disabled until enemy HP drops under CAPTURE_HP_THRESHOLD;
            # _refresh_action_button_state flips it on each round.
            cap_btn = discord.ui.Button(
                label="Capture", emoji="\U0001F9F2",
                style=discord.ButtonStyle.secondary,
                disabled=True, row=1,
            )
            cap_btn.callback = self._capture_callback
            self.add_item(cap_btn)
            self._refresh_action_button_state()
            _embed, _file = self._round_embed(opening=True)
            try:
                _kw: dict = {"embed": _embed, "view": self}
                if _file is not None:
                    _kw["attachments"] = [_file]
                await interaction.response.edit_message(**_kw)
            except discord.HTTPException:
                log.debug("delve wild battle: opening edit failed", exc_info=True)

    @discord.ui.button(
        label="Skip", emoji="\U0001F45F",
        style=discord.ButtonStyle.secondary, row=0,
    )
    async def btn_skip(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        """Walk past the wild buddy with no counter bump.

        Functionally the same as ,delve next on a wild_battle room: the
        room advances, no win/loss/capture counter changes.
        """
        if self._resolved or self._battle is not None:
            await interaction.response.defer()
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            await dsvc.advance_room(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
            )
        except ValueError as exc:
            await interaction.followup.send(
                embed=card(description=str(exc), color=C_AMBER).build(),
                ephemeral=True,
            )
            return
        self._resolved = True
        self.stop()
        # Hand off to the room view on the same message so the player
        # keeps walking from the same chat slot.
        if self.message is None:
            return
        room_view = _DelveRoomView(self.cog, self.ctx)
        state_after = await dsvc.list_state(
            self.ctx.db, self.ctx.guild_id, self.owner_id,
        )
        rt_after = str(state_after.get("current_room_type") or "empty")
        # If the next room is a mob/boss, swap directly into the battle
        # view instead -- mirrors _rebuild_room_message's behaviour.
        if rt_after in ("mob", "boss"):
            battle_view = _DelveBattleView(
                self.cog, self.ctx,
                class_key=str(state_after.get("class_key") or ""),
            )
            battle_view._refresh_ability_buttons(state_after)
            embed = await self.cog._build_room_embed(self.ctx, state_after)
            try:
                await self.message.edit(embed=embed, view=battle_view)
                battle_view.message = self.message
            except discord.HTTPException:
                pass
            return
        if rt_after == "wild_battle":
            payload_after = state_after.get("current_room_payload") or {}
            if isinstance(payload_after, str):
                import json as _json
                try:
                    payload_after = _json.loads(payload_after)
                except Exception:
                    payload_after = {}
            wb_after = (payload_after or {}).get("wild_buddy") or {}
            if wb_after:
                next_view = _DelveWildBuddyView(self.cog, self.ctx, wb_after)
                embed = await self.cog._build_room_embed(self.ctx, state_after)
                try:
                    await self.message.edit(embed=embed, view=next_view)
                    next_view.message = self.message
                except discord.HTTPException:
                    pass
                return
        _delve_set_button_visibility(room_view, rt_after)
        embed = await self.cog._build_room_embed(self.ctx, state_after)
        try:
            await self.message.edit(embed=embed, view=room_view)
            room_view.message = self.message
        except discord.HTTPException:
            pass

    def _make_action_button(
        self, label: str, emoji: str, action_key: str,
        style: discord.ButtonStyle, *, row: int = 0,
    ) -> discord.ui.Button:
        btn = discord.ui.Button(
            label=label, emoji=emoji, style=style, disabled=False, row=row,
        )
        # Stamp action_key onto the button so refresh logic can find
        # the Special button by intent rather than by label (the label
        # is now the buddy's named ability).
        btn.action_key = action_key  # type: ignore[attr-defined]

        async def _cb(interaction: discord.Interaction) -> None:
            await self._handle_action(interaction, action_key)

        btn.callback = _cb
        return btn

    def _refresh_action_button_state(self) -> None:
        if not self._battle:
            return
        b = self._battle
        # Enemy HP fraction gates the Capture button -- can't chuck a
        # charm at a buddy still at full health, just like the regular
        # mob fight (cogs/dungeon.py:btn_capture).
        hp_frac = float(b.enemy.hp) / max(1, float(b.enemy.max_hp))
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if getattr(child, "action_key", None) == "special":
                child.disabled = (
                    b.player_stamina < _DELVE_SPECIAL_STAMINA_COST
                )
            elif child.label == "Capture" or child.label.startswith("Capture"):
                child.disabled = hp_frac > dc.CAPTURE_HP_THRESHOLD
                # Live-update the label with the current chance so the
                # player can see whether to throw now or wear the wild
                # buddy down further.
                if child.disabled:
                    child.label = "Capture"
                else:
                    pct = int(self._capture_chance() * 100)
                    child.label = f"Capture ({pct}%)"

    def _capture_chance(self) -> float:
        """Compute the wild-buddy capture chance for the current state.

        Mirrors the regular mob ``capture_chance`` shape: tier penalty
        baked in, then a HP-based bonus (lower HP = much higher odds)
        so the capture-low-then-throw flow players know from Pokemon
        actually pays off here.
        """
        b = self._battle
        if not b:
            return 0.0
        hp_frac = float(b.enemy.hp) / max(1, float(b.enemy.max_hp))
        if hp_frac > dc.CAPTURE_HP_THRESHOLD:
            return 0.0
        rarity_tier = int(self.wild_buddy.get("rarity_tier") or 1)
        base = max(
            0.0,
            dc.CAPTURE_BASE_CHANCE
            - max(0, rarity_tier - 1) * dc.CAPTURE_PER_TIER_PENALTY,
        )
        # +up to 50% as HP -> 0 below the threshold, scaled linearly.
        bonus = (1.0 - hp_frac / dc.CAPTURE_HP_THRESHOLD) * 0.50
        return max(0.05, min(0.95, base + bonus))

    async def _capture_callback(
        self, interaction: discord.Interaction,
    ) -> None:
        """Capture the wild buddy.

        Gated to enemy HP under CAPTURE_HP_THRESHOLD by the button's
        own ``disabled`` flag (refreshed each round). On success the
        battle resolves immediately as a captured win; on failure the
        wild buddy gets a free turn and the fight continues.
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
            chance = self._capture_chance()
            if chance <= 0:
                await interaction.response.send_message(
                    f"Get the wild buddy below "
                    f"**{int(dc.CAPTURE_HP_THRESHOLD * 100)}%** HP first.",
                    ephemeral=True,
                )
                return
            roll = _random.random()
            if roll <= chance:
                # KO the enemy + finalise as a won battle. ``resolve_wild_battle``
                # rolls capture itself, but we ALSO insert a cc_buddies row
                # directly so the explicit-capture-on-button flow always
                # awards the buddy (the auto post-win roll has its own dice).
                b.enemy.hp = 0
                b.log_lines.append(
                    f"\U0001F4AB You hurl a charm. "
                    f"It works! ({int(chance * 100)}% rolled {int(roll * 100)})"
                )
                # Pre-flight the cc_buddies insert so the player gets the
                # captured pet regardless of resolve_wild_battle's own dice.
                try:
                    from services.buddy_economy import (
                        capture_destination as _capture_destination,
                    )
                    capture_dest = await _capture_destination(
                        self.ctx.db, self.ctx.guild_id, self.owner_id,
                    )
                    if capture_dest is not None:
                        capture_status = (
                            "owned" if capture_dest == "battle" else "stored"
                        )
                        species = str(self.wild_buddy.get("species") or "")
                        sp_meta = SPECIES.get(species, {})
                        name_pool = sp_meta.get("name_pool") or [species.title()]
                        name = (
                            str(self.wild_buddy.get("name"))
                            if self.wild_buddy.get("name")
                            else _random.choice(name_pool)
                        )
                        rolled_tier = int(
                            self.wild_buddy.get("rarity_tier") or roll_rarity()
                        )
                        from configs.buddies_config import (
                            roll_gender as _roll_gender2,
                            xp_for_level as _xp_for_level2,
                        )
                        _cap_lvl = int(self.wild_buddy.get("level") or 1)
                        await self.ctx.db.fetch_one(
                            """
                            INSERT INTO cc_buddies
                                (guild_id, owner_user_id, species, name,
                                 status, is_active, rarity_tier, level, xp,
                                 gender)
                            VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7, $8, $9)
                            RETURNING id
                            """,
                            self.ctx.guild_id, self.owner_id,
                            species, name, str(capture_status),
                            int(rolled_tier),
                            _cap_lvl,
                            int(_xp_for_level2(_cap_lvl)),
                            _roll_gender2(),
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
                        log.info(
                            "delve wild capture: battle + storage full uid=%s gid=%s",
                            self.owner_id, self.ctx.guild_id,
                        )
                        try:
                            await interaction.followup.send(
                                "Battle + storage both full -- buddy joined "
                                "your delve party only. Free a slot via "
                                "`,buddy store` or surrender.",
                                ephemeral=True,
                            )
                        except discord.HTTPException:
                            pass
                except Exception:
                    log.debug(
                        "delve wild capture: cc_buddies insert failed",
                        exc_info=True,
                    )
                # Tell _finalize to skip the resolver's auto-capture
                # roll AND to publish the wild-buddy-captured event so
                # achievements / quests are still credited.
                self._manual_capture_done = True
                await self._finalize(interaction)
                return
            # Capture failed -- enemy gets a free turn.
            b.log_lines.append(
                f"\U0001F4A8 The {self.wild_buddy.get('species', 'wild buddy')} "
                f"slipped the charm! ({int(chance * 100)}% rolled "
                f"{int(roll * 100)})"
            )
            ai_lines = _delve_enemy_ai_turn(b)
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
                    "delve wild capture: round edit failed",
                    exc_info=True,
                )

    async def _handle_action(
        self, interaction: discord.Interaction, action_key: str,
    ) -> None:
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

            # Defer up front -- bursts will run past Discord's 3s
            # interaction response window.
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                log.debug("delve wild battle: defer failed", exc_info=True)

            # Player swing burst.
            from services.buddy_battle_scene import play_battle_action_burst
            await play_battle_action_burst(
                self, b.player, b.enemy,
                actor_side="p1",
                action=str(action_key),
                round_num=int(b.round_num),
                max_rounds=25,
                ability_name=str(getattr(b.player, "ability_name", "") or ""),
            )

            new_lines = _delve_apply_player_action(b, action_key)
            b.log_lines.extend(new_lines)
            if b.is_over():
                b.log_lines.append("")
                await self._finalize(interaction)
                return

            # Enemy AI burst (always a strike).
            await play_battle_action_burst(
                self, b.player, b.enemy,
                actor_side="p2", action="strike",
                round_num=int(b.round_num),
                max_rounds=25,
            )

            ai_lines = _delve_enemy_ai_turn(b)
            b.log_lines.extend(ai_lines)
            b.round_num += 1
            if b.is_over():
                b.log_lines.append("")
                await self._finalize(interaction)
                return

            self._refresh_action_button_state()
            _embed, _file = self._round_embed()
            try:
                _kw: dict = {"embed": _embed, "view": self}
                if _file is not None:
                    _kw["attachments"] = [_file]
                if self.message is not None:
                    await self.message.edit(**_kw)
            except discord.HTTPException:
                log.debug("delve wild battle: round edit failed", exc_info=True)

    def _round_embed(
        self, *, opening: bool = False, action_banner: str = "",
    ) -> tuple[discord.Embed, "discord.File | None"]:
        """Return ``(embed, scene_file)`` -- the embed references the
        unified battle scene PNG via attachment://battle.png so delve
        wild-buddy fights share visuals with every other buddy battle."""
        b = self._battle
        assert b is not None
        p, e = b.player, b.enemy
        p_emoji = p.emoji or "\U0001F436"
        e_emoji = e.emoji or "\U0001F47E"

        tail_lines = [ln for ln in b.log_lines[-6:] if ln.strip()]
        if opening or not tail_lines:
            tail = "_Choose your move..._"
        else:
            tail = "\n".join(tail_lines)

        species_name = (
            str(self.wild_buddy.get("species") or e.name or "wild buddy")
            .title()
        )
        title = f"\U00002694 Round {b.round_num}  -  Wild {species_name}"
        stamina_pips = (
            "●" * b.player_stamina
            + "○" * (_DELVE_PLAYER_STAMINA_MAX - b.player_stamina)
        )
        desc_lines = [
            f"{p_emoji} **{p.name}**  Lv.{p.level} {p.tier_name}",
            f"  HP `{_delve_hp_bar(p.hp, p.max_hp)}`  -  ATK {int(p.atk)}",
            f"  Stamina `{stamina_pips}` ({b.player_stamina}/{_DELVE_PLAYER_STAMINA_MAX})",
            "",
            f"{e_emoji} **Wild {e.name}**  Lv.{e.level} {e.tier_name}",
            f"  HP `{_delve_hp_bar(e.hp, e.max_hp)}`  -  ATK {int(e.atk)}",
            "",
            tail,
        ]
        if opening:
            desc_lines.append(
                f"-# Strike (+1 stamina)  •  Special "
                f"({_DELVE_SPECIAL_STAMINA_COST} stamina)  •  "
                f"Brace (heal + halve next hit)  •  Risky "
                f"(60% huge / 25% miss / 15% backfire)"
            )

        scene_file = None
        try:
            from services.buddy_battle_scene import (
                fighters_to_scene_state, render_battle_frame,
            )
            import io as _io
            state = fighters_to_scene_state(
                p, e,
                round_num=b.round_num,
                max_rounds=25,
                action_banner=action_banner or ("FIGHT!" if opening else ""),
                is_player_turn=True,
            )
            png = render_battle_frame(state)
            scene_file = discord.File(_io.BytesIO(png), filename="battle.png")
        except Exception:
            log.debug("delve wild battle: scene render failed", exc_info=True)

        builder = card(title, color=C_PURPLE).description("\n".join(desc_lines))
        if scene_file is not None:
            builder = builder.image("attachment://battle.png")
        return builder.build(), scene_file

    async def _finalize(self, interaction: discord.Interaction) -> None:
        self._resolved = True
        b = self._battle
        assert b is not None
        won = b.player_won()
        bonus_pct = _delve_compute_battle_bonus(b) if won else 0.0
        # ``_manual_capture_done`` is set by the explicit Capture button
        # right before it calls _finalize. When True we skip the
        # resolver's auto-capture roll (already inserted cc_buddies in
        # the button path) and still publish the captured event so
        # achievements / quests / challenges that watch for that
        # trigger get bumped.
        manual_captured = bool(getattr(self, "_manual_capture_done", False))

        # _handle_action defers up-front for the burst frames; only
        # defer here when nothing has responded yet.
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass

        state_now = await dsvc.list_state(
            self.ctx.db, self.ctx.guild_id, self.owner_id,
        )
        floor = int(state_now.get("current_floor") or 1)

        try:
            res = await dsvc.resolve_wild_battle(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
                won=won,
                floor=floor,
                opponent_species=str(self.wild_buddy.get("species") or ""),
                opponent_level=int(self.wild_buddy.get("level") or 1),
                opponent_rarity_tier=int(self.wild_buddy.get("rarity_tier") or 1),
                bonus_pct=bonus_pct,
                skip_capture_roll=manual_captured,
            )
        except Exception:
            log.exception(
                "delve wild battle: resolve failed uid=%s gid=%s",
                self.owner_id, self.ctx.guild_id,
            )
            res = None

        # Fan-out triggers. Mirrors the legacy ,delve battle command.
        await self.cog._fan_out(
            self.owner_id, self.ctx.guild_id, "delve_wild_battle_spawn",
        )
        if won:
            await self.cog._fan_out(
                self.owner_id, self.ctx.guild_id, "delve_wild_battle_won",
            )
        # Manual capture path -- always fire the captured event so
        # achievements / quests pick it up. The auto-roll path below
        # already publishes it on `res.captured`, but resolve_wild_battle
        # didn't roll for us this time, so bump it here.
        if manual_captured:
            await self.cog._fan_out(
                self.owner_id, self.ctx.guild_id,
                "delve_wild_buddy_captured",
            )
        else:
            await self.cog._fan_out(
                self.owner_id, self.ctx.guild_id, "delve_wild_battle_lost",
            )
        if res and getattr(res, "captured", False):
            await self.cog._fan_out(
                self.owner_id, self.ctx.guild_id, "delve_wild_buddy_captured",
            )

        # Unified buddy_battle_win / _loss event so cross-surface
        # achievements / quests / challenges (Buddy Champion etc.) and
        # cc_buddies.wins/losses bookkeeping pick up delve wild battles
        # the same way they pick up PvP / arena / fish wild battles.
        try:
            from services.buddy_battle import (
                record_pve_battle_result as _rec_pve,
            )
            player_buddy_id = int(getattr(b.player, "id", 0) or 0) or None
            await _rec_pve(
                self.ctx.db,
                player_buddy_id=player_buddy_id,
                won=bool(won),
                rounds=int(b.round_num),
            )
            bus = getattr(self.cog.bot, "bus", None)
            if bus is not None:
                if won:
                    await bus.publish(
                        "buddy_battle_win",
                        guild=self.ctx.guild_id, user_id=self.owner_id,
                        winner_buddy_id=player_buddy_id,
                        loser_buddy_id=None,
                        source="delve_wild",
                    )
                else:
                    await bus.publish(
                        "buddy_battle_loss",
                        guild=self.ctx.guild_id, user_id=self.owner_id,
                        winner_buddy_id=None,
                        loser_buddy_id=player_buddy_id,
                        source="delve_wild",
                    )
        except Exception:
            log.debug(
                "delve wild battle: unified buddy_battle event failed",
                exc_info=True,
            )

        embed = self._render_final_embed(b, res, bonus_pct)
        if self.message is None:
            return

        # Auto-advance: don't make the player type ,delve next after a wild
        # battle. After resolving (and only if the run is still alive), hand
        # off to the same room view a normal ,delve next would land on.
        try:
            await dsvc.advance_room(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
            )
        except ValueError:
            # Run ended (death / completion). Just show the result embed
            # with no view -- the player can ,delve again from scratch.
            try:
                await self.message.edit(embed=embed, view=None)
            except discord.HTTPException:
                log.debug("delve wild battle: final edit failed", exc_info=True)
            return
        except Exception:
            log.exception(
                "delve wild battle: advance_room failed uid=%s gid=%s",
                self.owner_id, self.ctx.guild_id,
            )
            try:
                await self.message.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass
            return

        state_after = await dsvc.list_state(
            self.ctx.db, self.ctx.guild_id, self.owner_id,
        )
        if not state_after.get("run_id"):
            try:
                await self.message.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass
            return

        rt_after = str(state_after.get("current_room_type") or "empty")
        # If the next room is another fight, swap directly into the
        # appropriate battle view (mirrors the Skip-button handoff).
        if rt_after in ("mob", "boss"):
            battle_view = _DelveBattleView(
                self.cog, self.ctx,
                class_key=str(state_after.get("class_key") or ""),
            )
            battle_view._refresh_ability_buttons(state_after)
            try:
                next_embed = await self.cog._build_room_embed(
                    self.ctx, state_after,
                )
                await self.message.edit(embed=next_embed, view=battle_view)
                battle_view.message = self.message
            except discord.HTTPException:
                log.debug(
                    "delve wild battle: handoff to mob view failed",
                    exc_info=True,
                )
            return
        if rt_after == "wild_battle":
            payload_after = state_after.get("current_room_payload") or {}
            if isinstance(payload_after, str):
                import json as _json
                try:
                    payload_after = _json.loads(payload_after)
                except Exception:
                    payload_after = {}
            wb_after = (payload_after or {}).get("wild_buddy") or {}
            if wb_after:
                next_view = _DelveWildBuddyView(self.cog, self.ctx, wb_after)
                try:
                    next_embed = await self.cog._build_room_embed(
                        self.ctx, state_after,
                    )
                    await self.message.edit(embed=next_embed, view=next_view)
                    next_view.message = self.message
                except discord.HTTPException:
                    log.debug(
                        "delve wild battle: handoff to wild view failed",
                        exc_info=True,
                    )
                return

        # Empty / loot / shop / etc. -- show the result embed in this slot
        # with the room view's Next/Rest buttons so the player keeps walking
        # without typing ,delve next.
        room_view = _DelveRoomView(self.cog, self.ctx)
        _delve_set_button_visibility(room_view, rt_after)
        try:
            await self.message.edit(embed=embed, view=room_view)
            room_view.message = self.message
        except discord.HTTPException:
            log.debug("delve wild battle: room view handoff failed", exc_info=True)

    def _render_final_embed(
        self,
        b: _DelveLiveBattle,
        res: "Any | None",
        bonus_pct: float,
    ) -> discord.Embed:
        species = str(self.wild_buddy.get("species") or "?").title()
        won = b.player_won()
        rounds = b.round_num

        if won and res is not None:
            rune_h = to_human(int(res.rune_reward_raw))
            ore_h = to_human(int(res.ore_reward_raw))
            lines = [
                f"\U0001F4AB You beat the wild **{species}** in {rounds} rounds.",
                f"+{_fmt_rune(rune_h)}",
            ]
            if res.ore_symbol and ore_h > 0:
                lines.append(f"+{_fmt_ore(res.ore_symbol, ore_h)}")
            # Surface the buddy XP credited for this win so players see
            # the same hit they'd get from a buddy-vs-buddy battle.
            if int(getattr(res, "buddy_xp_awarded", 0) or 0) > 0:
                fighter_id = getattr(res, "fighter_buddy_id", None)
                tag = f" (#{int(fighter_id)})" if fighter_id else ""
                lines.append(
                    f"\U0001F436 Your buddy{tag} earns "
                    f"**+{int(res.buddy_xp_awarded):,}** XP."
                )
            if bonus_pct > 0:
                lines.append(
                    f"-# Performance bonus: **+{bonus_pct * 100:.0f}%** "
                    f"(rounds / HP remaining / action variety)"
                )
            if res.captured and res.captured_buddy_row:
                cap_status = str(res.captured_buddy_row.get("status") or "owned")
                where = (
                    "went to your **storage** (active slots full)."
                    if cap_status == "stored"
                    else "joins your active roster."
                )
                lines.append(
                    f"\U00002728 **Captured!** "
                    f"**{res.captured_buddy_row.get('name')}** "
                    f"(Lv {int(res.captured_buddy_row.get('level') or 1)}) "
                    f"{where}"
                )
            lines.append(
                f"-# Wild battles: **{res.new_won_total}** won / "
                f"**{res.new_lost_total}** lost  -  "
                f"**{res.new_captured_total}** captured."
            )
            color = C_GOLD
            title = f"\U0001F3C6 Wild {species} Defeated"
        elif res is not None:
            lines = [
                f"\U0001F480 The wild **{species}** beat your buddy "
                f"in {rounds} rounds.",
                "No penalty -- but no reward either. "
                "Try again with a tougher buddy.",
                f"-# Wild battles: **{res.new_won_total}** won / "
                f"**{res.new_lost_total}** lost  -  "
                f"**{res.new_captured_total}** captured.",
            ]
            color = C_AMBER
            title = f"\U0001F4A8 The {species} got away"
        else:
            lines = [
                f"\U00002694 Battle vs wild **{species}** ended in {rounds} "
                f"rounds.",
                "Could not persist the result -- counters not updated.",
            ]
            color = C_NEUTRAL
            title = f"\U00002694 Wild {species}"

        builder = card(title, color=color).description("\n".join(lines))

        # Append a tail of the in-memory log so the player sees the swing
        # they just won/lost on, capped to a single embed field.
        tail = [ln for ln in b.log_lines if ln.strip()][-12:]
        if tail:
            text = "\n".join(tail)
            if len(text) > 1020:
                text = text[-1020:]
            builder = builder.field("Battle Log", text, False)
        return builder.build()


# ============================================================================
# Cog
# ============================================================================


class Dungeon(commands.Cog, name="Delve"):
    """ASCII dungeon crawler with mob captures, ore tiers, and a RUNE economy."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_check(self, ctx) -> bool:
        """Module + premium gate. Delves are paid; admins do NOT bypass."""
        if not await module_cog_check(self.bot, ctx, "dungeon"):
            return False
        from services import entitlements
        from core.framework.premium import PremiumGateFailure
        if not await entitlements.is_premium(ctx.guild_id, ctx.db):
            raise PremiumGateFailure("dungeon")
        return True

    @staticmethod
    async def _announce_badges(
        send_channel, granted_ids: list[str],
    ) -> None:
        """Drop a public 'Achievement Unlocked!' embed in ``send_channel``.

        ``send_channel`` is anything that exposes ``.send(embed=...)`` --
        typically ``ctx.channel`` or a ``discord.Message`` for follow-up
        replies. The DM that ``services.achievements.grant`` already
        sends is great for archival, but the player asked for an
        in-channel banner so peers can see the kill earned a badge.
        Silently no-ops on empty lists or send failures.
        """
        if not granted_ids:
            return
        try:
            import configs.achievements_config as _ach_cat
            parts: list[str] = []
            total_reward = 0.0
            for bid in granted_ids:
                entry = _ach_cat.get(bid)
                if not entry:
                    continue
                reward = float(entry.get("reward_usd", 0.0) or 0.0)
                total_reward += reward
                line = (
                    f"{entry.get('icon', '')} **{entry['name']}**  -  "
                    f"_{entry.get('description', '')}_"
                )
                if reward > 0:
                    line += f"  +**${reward:,.0f}**"
                parts.append(line)
            if not parts:
                return
            footer = (
                f"+${total_reward:,.0f} paid to your wallet"
                if total_reward > 0 else
                "View all your badges with `,achievements`"
            )
            embed = (
                card(
                    "\U0001F3C5 Achievement Unlocked!",
                    description="\n".join(parts),
                    color=C_GOLD,
                )
                .footer(footer)
                .build()
            )
            await send_channel.send(embed=embed)
        except Exception:
            log.debug("dungeon: badge announcement failed", exc_info=True)

    async def _fan_out(
        self, uid: int, gid: int, trigger: str, amount: int = 1,
    ) -> list[str]:
        """Fan one trigger into achievements / quests / challenges.

        Mirrors services.fishing._fan_out so the dungeon participates in
        the same per-user counter machinery as every other minigame.
        Each downstream call is wrapped because a bookkeeping failure
        must never abort the player's action. Returns the list of newly-
        granted ``badge_id`` values from ``achievements.bump`` so the
        caller can surface them in the result embed (the bare DM was
        easy to miss).
        """
        granted: list[str] = []
        try:
            from services import achievements as _ach
            granted = await _ach.bump(self.bot, uid, gid, trigger, amount=amount) or []
        except Exception:
            log.debug("dungeon: achievements.bump %s failed", trigger, exc_info=True)
        try:
            from services import quests as _quests
            await _quests.progress_trigger(self.bot.db, uid, gid, trigger, amount=amount)
        except Exception:
            log.debug("dungeon: quests.progress_trigger %s failed", trigger, exc_info=True)
        try:
            from services import challenges as _ch
            await _ch.progress_trigger(self.bot, uid, gid, trigger, amount=amount)
        except Exception:
            log.debug("dungeon: challenges.progress_trigger %s failed", trigger, exc_info=True)
        return granted

    # -- Group --
    @commands.hybrid_group(
        name="delve", aliases=["dungeon"],
        invoke_without_command=True, with_app_command=False,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def delve(self, ctx: DiscoContext) -> None:
        """Show the current room or surface panel."""
        from services.onboarding import maybe_send_intro
        from core.framework.slot_warning import maybe_warn_full_slots
        await maybe_send_intro(ctx, "delve")
        await maybe_warn_full_slots(ctx, surface="delve", phase="game_start")
        await self._show_panel(ctx)

    # ── Panel rendering ────────────────────────────────────────────────────

    async def _show_panel(self, ctx: DiscoContext) -> None:
        uid = ctx.author.id
        state = await dsvc.ensure_state(ctx.db, ctx.guild_id, uid)
        oracles = await _oracles(ctx)
        holdings = await _gather_holdings(ctx, uid)
        if not state.get("class_key"):
            embed = (
                card(
                    "\U0001F3F0 The Crypt Tavern  -  Whispers from the Deep",
                    color=C_NAVY,
                )
                .description(_frame_block("town"))
                .field(
                    "Choose your class",
                    "\n".join(
                        f"{cm['emoji']} **{cm['name']}** -- {cm['blurb']}"
                        for cm in dc.CLASSES.values()
                    ),
                    False,
                )
                .field(
                    "Begin",
                    "Run `,delve class warrior|mage|rogue` to commit, "
                    "then `,delve start` to enter the dungeon.",
                    False,
                )
                .footer(
                    "COPPER / SILVER / GOLD ore mined deep -- "
                    "burn-swap to RUNE -- cash RUNE for USD."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        if state.get("run_id") and state.get("current_room_type"):
            await self._render_room_embed(ctx, state, oracles, holdings)
            return

        await self._render_stats_embed(ctx, state, oracles, holdings)

    async def _render_room_embed(
        self, ctx: DiscoContext, state: dict,
        oracles: dict[str, float], holdings: dict[str, int],
    ) -> None:
        floor = int(state.get("current_floor") or 1)
        room = int(state.get("current_room") or 0)
        fmeta = dc.floor_meta(floor)
        rt = str(state.get("current_room_type") or "empty")
        color = _floor_color(state)

        title = f"\U0001F5FA  Floor {floor}: {fmeta.get('name', '?')}  -  Room {room}"
        if rt == "mob" or rt == "boss":
            mob = dict(state.get("current_mob_state") or {})
            mob_meta = dc.mob_meta(str(mob.get("key") or "")) or {}
            glyph = mob_meta.get("emoji") or "?"
            is_mini_boss = bool(mob.get("mini_boss"))
            frame = _frame_block(
                "boss_room" if (rt == "boss" or is_mini_boss) else "mob_room",
                glyph=glyph,
            )
            mob_hp = int(mob.get("hp", 0))
            mob_max = max(1, int(mob.get("max_hp", 1)))
            tier_n = int(mob.get("tier") or mob_meta.get("tier", 1) or 1)
            tier_stars = "\U00002B50" * max(1, min(5, tier_n))
            if rt == "boss":
                opener = "\U0001F47B \U00002728 *A boss appears!* \U00002728 \U0001F47B"
                badge = ""
            elif is_mini_boss:
                opener = "\U0001F38F \U00002728 *A mini-boss blocks the way!* \U00002728"
                badge = " \U0001F38F **Mini-Boss**"
            else:
                opener = "\U0001F4AB *A wild encounter!*"
                badge = ""
            desc = (
                f"{frame}\n"
                f"{opener}\n"
                f"{mob_meta.get('emoji', '')} **{mob_meta.get('name', mob.get('key'))}**{badge} {tier_stars}\n"
                f"`{_cute_hp_bar(mob_hp, mob_max)}`  `{mob_hp}/{mob_max} HP`\n"
                f"_{mob_meta.get('blurb', '')}_"
            )
        elif rt == "ore":
            payload = dict(state.get("current_room_payload") or {})
            ore_sym = str(payload.get("ore_symbol") or dc.COPPER_SYMBOL)
            ore_qty = float(payload.get("ore_qty") or 0.0)
            ore_glyph = {
                dc.COPPER_SYMBOL: "c", dc.SILVER_SYMBOL: "s", dc.GOLD_SYMBOL: "g",
            }.get(ore_sym, "*")
            frame = _frame_block("ore_room", ore_glyph=ore_glyph)
            est_usd = ore_qty * oracles.get(ore_sym, 0.0)
            est_tag = f"  ~ **{fmt_usd(est_usd)}**" if est_usd > 0 else ""
            desc = (
                f"{frame}\n"
                f"A vein of **{ore_sym}** glints in the wall "
                f"({ore_qty:,.2f} units{est_tag}). "
                f"`,delve mine` to harvest."
            )
        elif rt == "shrine":
            desc = _frame_block("shrine") + (
                "\nA glowing shrine pulses softly. "
                "`,delve rest` to channel it and end your run with a full heal."
            )
        elif rt == "stairs":
            desc = _frame_block("stairs") + (
                "\nA descending staircase yawns ahead. "
                "`,delve descend` to go deeper, or `,delve rest` to retreat."
            )
        elif rt == "chest":
            payload = dict(state.get("current_room_payload") or {})
            rune_amt = float(payload.get("rune_amount") or 0.0)
            desc = _frame_block("chest") + (
                f"\nA dusty chest. `,delve open` to crack it "
                f"(roughly **{rune_amt:.2f}** RUNE inside)."
            )
        elif rt == "wild_battle":
            payload = dict(state.get("current_room_payload") or {})
            wb = dict(payload.get("wild_buddy") or {})
            sp = str(wb.get("species") or "wild buddy").title()
            lvl = int(wb.get("level") or 1)
            tier = int(wb.get("rarity_tier") or 1)
            sp_meta = SPECIES.get(str(wb.get("species") or "").lower(), {})
            sp_emoji = sp_meta.get("emoji") or "\U0001F436"
            tier_stars = "\U00002B50" * max(1, min(5, tier))
            desc = _frame_block("mob_room", glyph=sp_emoji) + (
                f"\n\U0001F436 \U00002728 *A wild buddy challenges you!* "
                f"\U00002728 \U0001F436\n"
                f"{sp_emoji} **Wild {sp}** {tier_stars}\n"
                f"_Lv {lvl}, {_tier_word(tier)}_\n"
                f"\nTap **Challenge** to send your active CC buddy in, "
                f"or **Skip** to walk past with no penalty."
            )
        else:
            desc = _frame_block("corridor") + (
                "\nThe corridor stretches on. `,delve next` to keep going."
            )

        hp = int(state.get("current_hp") or 0)
        hp_max = int(state.get("hp_max") or 1)
        cmeta = dc.class_meta(state.get("class_key") or "warrior") or {}
        embed = (
            card(title, color=color)
            .description(desc)
            .field(
                f"{cmeta.get('emoji', '')} HP",
                f"`{_hp_bar(hp, hp_max)}` **{hp}/{hp_max}**",
                True,
            )
            .field(
                "Lv.",
                f"**{int(state.get('level') or 1)}**  ({int(state.get('xp') or 0):,} XP)",
                True,
            )
            .field(
                "Skill",
                self._skill_status(state, cmeta),
                True,
            )
        )
        embed = embed.footer(
            f"Class: {cmeta.get('name', '?')}  -  "
            f"Run total: F{int(state.get('current_floor') or 0)}  -  "
            f"Deepest: F{int(state.get('deepest_floor') or 0)}"
        )

        # Combat rooms: attach the interactive battle view so the player
        # fights via buttons that edit this same message instead of
        # spamming new replies per swing.
        if rt in ("mob", "boss"):
            view = _DelveBattleView(
                self, ctx,
                class_key=str(state.get("class_key") or ""),
            )
            view._refresh_ability_buttons(state)
            # 5% of regular mob encounters become a wild buddy battle --
            # a captureable buddy that lands in cc_buddies on success.
            if rt == "mob" and _random.random() < _DELVE_WILD_BUDDY_CHANCE:
                mob_state = dict(state.get("current_mob_state") or {})
                view.wild_buddy = {
                    "species": str(mob_state.get("key") or "wolf"),
                    "name": "Wild " + str(mob_state.get("key") or "Buddy").title(),
                    "rarity_tier": int(mob_state.get("tier") or 1),
                    "level": int(mob_state.get("level") or 1),
                }
                embed = embed.field(
                    "\U0001F436 Wild Buddy!",
                    "A wild buddy appears! Tap **Capture** when its HP is "
                    "low to add it to your shelter instead of slaying it.",
                    False,
                )
            sent = await ctx.reply(embed=embed.build(), view=view, mention_author=False)
            view.message = sent
            return

        # Wild-buddy battle rooms: interactive Challenge/Skip view that
        # mirrors fishing's wild battle UX. The Challenge button swaps in
        # turn-based action buttons; Skip just advances the room.
        if rt == "wild_battle":
            payload = dict(state.get("current_room_payload") or {})
            wb = dict(payload.get("wild_buddy") or {})
            if wb:
                if bool(wb.get("attractor_pulled")):
                    embed = embed.field(
                        "\U0001F9F2 Battle Attractor",
                        "Your active attractor lured this fight.",
                        inline=False,
                    )
                wb_view = _DelveWildBuddyView(self, ctx, wb)
                sent = await ctx.reply(
                    embed=embed.build(), view=wb_view, mention_author=False,
                )
                wb_view.message = sent
                return

        # Non-combat rooms: persistent room view with action+next buttons
        # that edit this same message instead of spamming the channel.
        # Owner-locked so other players can't drive someone else's run.
        room_view = _DelveRoomView(self, ctx)
        _delve_set_button_visibility(room_view, rt)
        sent = await ctx.reply(
            embed=embed.build(), view=room_view, mention_author=False,
        )
        room_view.message = sent

    async def _rebuild_room_message(
        self,
        ctx: DiscoContext,
        view: "_DelveRoomView",
        *,
        replace_view: bool = False,
    ) -> None:
        """Re-render the room embed onto ``view.message`` after an action.

        Reads the latest run state, builds the same room embed
        ``_render_room_embed`` builds, and edits the existing message in
        place. When ``replace_view`` is True the button set is rebuilt so
        the layout matches the new room type (e.g. after Mining the room
        becomes empty, so the Mine button hides and Next is the only
        action).

        Falls back to a "you're back at the surface" embed when the run
        ends mid-flight (e.g. ``Rest`` button or boss kill on F20).
        """
        state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        if not state.get("run_id"):
            if view.message is not None:
                surface = (
                    card(
                        "\U0001F3F0 Back at the Surface",
                        color=C_NAVY,
                    )
                    .description(
                        "Run ended. HP restored.\n"
                        "`,delve start` to dive again whenever you're ready."
                    )
                    .build()
                )
                try:
                    await view.message.edit(embed=surface, view=None)
                except discord.HTTPException:
                    pass
            view.stop()
            return

        rt = str(state.get("current_room_type") or "empty")

        # If the player just stepped into a mob/boss room via Next, the
        # room view doesn't fit -- swap in the battle view on the same
        # message so the player keeps fighting from the same chat slot.
        if rt in ("mob", "boss"):
            battle_view = _DelveBattleView(
                self, ctx,
                class_key=str(state.get("class_key") or ""),
            )
            battle_view._refresh_ability_buttons(state)
            if rt == "mob" and _random.random() < _DELVE_WILD_BUDDY_CHANCE:
                mob_state = dict(state.get("current_mob_state") or {})
                battle_view.wild_buddy = {
                    "species": str(mob_state.get("key") or "wolf"),
                    "name": "Wild " + str(mob_state.get("key") or "Buddy").title(),
                    "rarity_tier": int(mob_state.get("tier") or 1),
                    "level": int(mob_state.get("level") or 1),
                }
            embed = await self._build_room_embed(ctx, state)
            if view.message is not None:
                try:
                    await view.message.edit(embed=embed, view=battle_view)
                    battle_view.message = view.message
                except discord.HTTPException:
                    pass
            view.stop()
            return

        # Wild-buddy battle rooms get the interactive Challenge / Skip
        # view -- same treatment as mob rooms above so the player never
        # has to type ,delve battle.
        if rt == "wild_battle":
            payload = state.get("current_room_payload") or {}
            if isinstance(payload, str):
                import json as _json
                try:
                    payload = _json.loads(payload)
                except Exception:
                    payload = {}
            wb = (payload or {}).get("wild_buddy") or {}
            if wb:
                wb_view = _DelveWildBuddyView(self, ctx, wb)
                embed = await self._build_room_embed(ctx, state)
                if view.message is not None:
                    try:
                        await view.message.edit(embed=embed, view=wb_view)
                        wb_view.message = view.message
                    except discord.HTTPException:
                        pass
                view.stop()
                return

        embed = await self._build_room_embed(ctx, state)
        if replace_view:
            _delve_set_button_visibility(view, rt)
        # Rebuild the consumables dropdown from current inventory so
        # the qty counts + any new buys land immediately. Selects
        # can't have options mutated post-construction so we drop +
        # re-add the row-2 dropdown on every refresh. Skip the dropdown
        # entirely when the player owns no consumables -- an empty
        # disabled select takes a whole row and adds nothing.
        try:
            cons = dict(state.get("consumables") or {})
            for child in list(view.children):
                if isinstance(child, _DelveConsumableSelect):
                    view.remove_item(child)
            if any(int(v or 0) > 0 for v in cons.values()):
                view.add_item(_DelveConsumableSelect(cons))
        except Exception:
            log.debug("delve: consumables dropdown rebuild failed",
                      exc_info=True)
        if view.message is not None:
            try:
                await view.message.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass

    async def _build_room_embed(
        self, ctx: DiscoContext, state: dict,
    ) -> discord.Embed:
        """Build the same embed ``_render_room_embed`` posts, in isolation.

        Used by the room view's _rebuild path so we don't have to reply
        with a new message every action -- we just edit the existing one.
        """
        oracles = await _oracles(ctx)
        floor = int(state.get("current_floor") or 1)
        room = int(state.get("current_room") or 0)
        fmeta = dc.floor_meta(floor)
        rt = str(state.get("current_room_type") or "empty")
        color = _floor_color(state)
        title = f"\U0001F5FA  Floor {floor}: {fmeta.get('name', '?')}  -  Room {room}"

        if rt == "mob" or rt == "boss":
            # Mob / boss embed: matches what ``_render_room_embed``'s
            # mob branch builds. Without this case ``_rebuild_room_message``
            # would fall through to the corridor description but attach
            # the battle view, leaving the player staring at "Tap Next
            # to keep going" with nothing but Strike / Skill / Flee
            # buttons -- the bug image1 / image2 demonstrated.
            mob = dict(state.get("current_mob_state") or {})
            mob_meta = dc.mob_meta(str(mob.get("key") or "")) or {}
            glyph = mob_meta.get("emoji") or "?"
            frame = _frame_block(
                "boss_room" if rt == "boss" else "mob_room",
                glyph=glyph,
            )
            mob_hp = int(mob.get("hp", 0))
            mob_max = max(1, int(mob.get("max_hp", 1)))
            tier_n = int(mob.get("tier") or mob_meta.get("tier", 1) or 1)
            tier_stars = "\U00002B50" * max(1, min(5, tier_n))
            opener = (
                "\U0001F47B \U00002728 *A boss appears!* \U00002728 \U0001F47B"
                if rt == "boss"
                else "\U0001F4AB *A wild encounter!*"
            )
            desc = (
                f"{frame}\n"
                f"{opener}\n"
                f"{mob_meta.get('emoji', '')} **{mob_meta.get('name', mob.get('key'))}** {tier_stars}\n"
                f"`{_cute_hp_bar(mob_hp, mob_max)}`  `{mob_hp}/{mob_max} HP`\n"
                f"_{mob_meta.get('blurb', '')}_"
            )
        elif rt == "ore":
            payload = dict(state.get("current_room_payload") or {})
            ore_sym = str(payload.get("ore_symbol") or dc.COPPER_SYMBOL)
            ore_qty = float(payload.get("ore_qty") or 0.0)
            ore_glyph = {
                dc.COPPER_SYMBOL: "c", dc.SILVER_SYMBOL: "s", dc.GOLD_SYMBOL: "g",
            }.get(ore_sym, "*")
            frame = _frame_block("ore_room", ore_glyph=ore_glyph)
            est_usd = ore_qty * oracles.get(ore_sym, 0.0)
            est_tag = f"  ~ **{fmt_usd(est_usd)}**" if est_usd > 0 else ""
            desc = (
                f"{frame}\n"
                f"\U00002728 A vein of **{ore_sym}** glints in the wall "
                f"({ore_qty:,.2f} units{est_tag}).\n"
                "Tap **Mine** to harvest, or **Next** to move on."
            )
        elif rt == "shrine":
            desc = (
                _frame_block("shrine")
                + "\nA glowing shrine pulses softly. **Rest** to channel it "
                "and end your run with a full heal, or push deeper with **Next**."
            )
        elif rt == "stairs":
            desc = (
                _frame_block("stairs")
                + "\nA descending staircase yawns ahead.\n"
                "**Descend** to go deeper, or **Rest** to retreat."
            )
        elif rt == "chest":
            payload = dict(state.get("current_room_payload") or {})
            rune_amt = float(payload.get("rune_amount") or 0.0)
            desc = (
                _frame_block("chest")
                + f"\n\U00002728 A dusty chest. **Open** to crack it "
                f"(roughly **{rune_amt:.2f}** RUNE inside)."
            )
        elif rt == "wild_battle":
            payload = dict(state.get("current_room_payload") or {})
            wb = dict(payload.get("wild_buddy") or {})
            sp = str(wb.get("species") or "wild buddy").title()
            lvl = int(wb.get("level") or 1)
            tier = int(wb.get("rarity_tier") or 1)
            sp_meta = SPECIES.get(str(wb.get("species") or "").lower(), {})
            sp_emoji = sp_meta.get("emoji") or "\U0001F436"
            tier_stars = "\U00002B50" * max(1, min(5, tier))
            desc = (
                _frame_block("mob_room", glyph=sp_emoji)
                + f"\n\U0001F436 \U00002728 *A wild buddy challenges you!* "
                f"\U00002728 \U0001F436\n"
                f"{sp_emoji} **Wild {sp}** {tier_stars}\n"
                f"_Lv {lvl}, {_tier_word(tier)}_\n"
                f"\nTap **Challenge** to send your active CC buddy in, "
                f"or **Skip** to walk past with no penalty."
            )
        else:
            desc = (
                _frame_block("corridor")
                + "\nThe corridor stretches on. Tap **Next** to keep going."
            )

        hp = int(state.get("current_hp") or 0)
        hp_max = int(state.get("hp_max") or 1)
        cmeta = dc.class_meta(state.get("class_key") or "warrior") or {}
        builder = (
            card(title, color=color)
            .description(desc)
            .field(
                f"{cmeta.get('emoji', '')} HP",
                f"`{_cute_hp_bar(hp, hp_max)}`  `{hp}/{hp_max}`",
                True,
            )
            .field(
                "Lv.",
                f"**{int(state.get('level') or 1)}**  ({int(state.get('xp') or 0):,} XP)",
                True,
            )
            .field(
                "Skill",
                self._skill_status(state, cmeta),
                True,
            )
            .footer(
                f"Class: {cmeta.get('name', '?')}  -  "
                f"Run total: F{int(state.get('current_floor') or 0)}  -  "
                f"Deepest: F{int(state.get('deepest_floor') or 0)}"
            )
        )
        return builder.build()

    async def _render_stats_embed(
        self, ctx: DiscoContext, state: dict,
        oracles: dict[str, float], holdings: dict[str, int],
    ) -> None:
        cmeta = dc.class_meta(state.get("class_key") or "warrior") or {}
        hp = int(state.get("current_hp") or 0)
        hp_max = int(state.get("hp_max") or 1)
        xp = int(state.get("xp") or 0)
        lvl = int(state.get("level") or 1)
        into, span = dc.xp_to_next(xp)
        bar = FormatKit.bar(into, max(span, 1), width=10, show_pct=False)
        pstats = dsvc.player_combat_stats(state)
        pending = await dsvc.accrued_stake_yield(ctx.db, ctx.guild_id, ctx.author.id)

        # Allocations + unspent stat points -- need both numbers so the
        # panel reflects the same source of truth the spend command uses.
        hp_alloc  = int(pstats.get("hp_alloc")  or 0)
        atk_alloc = int(pstats.get("atk_alloc") or 0)
        spd_alloc = int(pstats.get("spd_alloc") or 0)
        int_alloc = int(pstats.get("int_alloc") or 0)
        unspent_pts = dc.stat_points_available(
            lvl, hp_alloc, atk_alloc, spd_alloc, int_alloc,
        )

        # Equipped weapon + armor metadata. We render the type tag, the
        # raw stat bonus, and (for ranged weapons) the ammo key + how
        # many rounds the player has loaded.
        weapon_key = str(state.get("equipped_weapon") or "")
        armor_key  = str(state.get("equipped_armor")  or "")
        wmeta = dc.weapon_meta(weapon_key) or {}
        ameta = dc.armor_meta(armor_key)   or {}
        attack_kind = str(pstats.get("attack_kind") or "melee")
        weapon_type = str(wmeta.get("weapon_type") or "?")
        ammo_key = pstats.get("weapon_ammo_key")
        consumables = state.get("consumables") or {}
        if not isinstance(consumables, dict):
            consumables = {}
        ammo_have = int(consumables.get(ammo_key) or 0) if ammo_key else 0

        wrarity = dc.item_rarity(wmeta)
        arity = dc.item_rarity(ameta)
        weapon_line = (
            f"{dc.rarity_dot(wrarity)} \U0001F5E1 "
            f"**{wmeta.get('name', weapon_key) or '_unequipped_'}**"
            f" *({dc.rarity_label(wrarity)})*"
            f"  -- {weapon_type}, {attack_kind}"
            f"  +{dc.effective_atk_bonus(wmeta)} ATK"
            f"{_fmt_affix_tail(wmeta)}"
        )
        if ammo_key:
            weapon_line += f"  -- ammo: {ammo_have} {str(ammo_key).replace('_bundle','')}"
        armor_line = (
            f"{dc.rarity_dot(arity)} \U0001F6E1 "
            f"**{ameta.get('name', armor_key) or '_unequipped_'}**"
            f" *({dc.rarity_label(arity)})*"
            f"  -- {ameta.get('armor_type', '?')}"
            f"  +{dc.effective_def_bonus(ameta)} DEF"
            f"{_fmt_affix_tail(ameta)}"
        )

        # Skill panel: name, multiplier, cooldown ready/remaining,
        # plus the auto-crit / kind tags so a Mage knows their Fireball
        # scales with INT and a Rogue sees the auto-crit affordance.
        skill_cd_remaining = int(state.get("skill_cd_remaining") or 0)
        skill_cd_max = int(pstats.get("skill_cd") or cmeta.get("skill_cd") or 0)
        if skill_cd_remaining <= 0:
            cd_line = "**Ready**"
        else:
            cd_line = f"On cooldown ({skill_cd_remaining}/{skill_cd_max} r)"
        skill_tags = []
        if pstats.get("skill_auto_crit"):
            skill_tags.append("auto-crit")
        sk_kind = str(pstats.get("skill_kind") or "")
        if sk_kind:
            skill_tags.append(sk_kind)
        skill_tag_line = f" -- {', '.join(skill_tags)}" if skill_tags else ""
        skill_block = (
            f"**{pstats.get('skill_name', '?')}**  "
            f"({pstats.get('skill_mult', 1.0):.2f}x dmg, "
            f"{skill_cd_max}r cd){skill_tag_line}\n"
            f"-# {cmeta.get('skill_desc', '')}\n"
            f"{cd_line}"
        )

        # Active player buffs (thorn aura, wildshape, regen, sanctuary,
        # marked target, volley_charged). Buff keys are config-defined
        # in dungeon_config.CONSUMABLES via the ``buff`` field; we map
        # known keys to clean labels and fall back to title-cased raw.
        buffs = state.get("player_buffs") or {}
        if not isinstance(buffs, dict):
            buffs = {}
        buff_labels = {
            "thorn_aura":     ("\U0001F33F", "Thorn Aura",     "reflect %"),
            "wildshape":      ("\U0001F43B", "Wildshape",      "+ATK / regen"),
            "sanctuary":      ("\U0001F4DC", "Sanctuary",      "halve incoming"),
            "marked_target":  ("\U0001F3AF", "Marked Target",  "auto-crit"),
            "volley_charged": ("\U0001F3F9", "Volley Charged", "next swing x3"),
            "regen":          ("\U0001F33A", "Regen",          "heal %/r"),
        }
        buff_lines: list[str] = []
        for name, payload in buffs.items():
            if not isinstance(payload, dict):
                continue
            # Per-ability cooldown markers live in player_buffs but are
            # rendered in the skill row, not the buffs row.
            if str(name).startswith("_ability_cd_"):
                continue
            dur = int(payload.get("duration") or 0)
            if dur <= 0:
                continue
            emoji_lbl, label, _hint = buff_labels.get(
                name, ("\U00002728", str(name).replace("_", " ").title(), ""),
            )
            val = float(payload.get("value") or 0.0)
            val_tag = f" (+{val:g})" if val and abs(val) > 0.0001 else ""
            buff_lines.append(f"{emoji_lbl} **{label}**{val_tag} -- {dur}r left")

        # Compact 2-column "core stats" string. INT only matters for
        # caster classes (mage / druid) but we always show it -- a flat
        # 0 is information too if the player just rolled a martial.
        core_stats_block = (
            f"ATK **{pstats['atk']:.1f}**   DEF **{pstats['def']:.1f}**\n"
            f"SPD **{pstats['spd']:.2f}**   INT **{pstats['int']:.1f}**"
        )
        alloc_block = (
            f"HP +{hp_alloc}   ATK +{atk_alloc}\n"
            f"SPD +{spd_alloc}   INT +{int_alloc}\n"
            + (
                f"-# **{unspent_pts}** unspent (`{ctx.prefix or ','}delve upgrade`)"
                if unspent_pts > 0
                else "-# (all points spent)"
            )
        )

        emoji_default = "\U0001F5FA"
        emoji = cmeta.get("emoji") or emoji_default
        class_name = cmeta.get("name") or "Adventurer"
        # Themed title -- the Crypt Network is the bot's PvE dungeon-crawl
        # surface, so the panel reads as a tavern check-in for a returning
        # delver rather than the previous dry "X's Adventurer" string.
        embed = (
            card(
                f"\U0001F3F0 The Crypt Tavern  -  "
                f"{emoji} {ctx.author.display_name} the Lv{lvl} {class_name}",
                color=C_NAVY,
            )
            .description(_frame_block("town"))
            .field(
                f"Lv. {lvl}  -  {xp:,} XP",
                f"`{bar}` {into:,}/{span:,}" if span else "`##########` MAX",
                False,
            )
            .field("HP", f"`{_hp_bar(hp, hp_max)}` {hp}/{hp_max}", True)
            .field("Class", f"**{cmeta.get('name', '?')}**", True)
            .field("Deepest", f"**F{int(state.get('deepest_floor') or 0)}**", True)
            .field("Core stats", core_stats_block, True)
            .field("Allocations", alloc_block, True)
            .field("Skill", skill_block, True)
            .field("Equipped", f"{weapon_line}\n{armor_line}", False)
            .field_if(
                bool(buff_lines),
                "Active buffs",
                "\n".join(buff_lines) if buff_lines else "_(none)_",
                False,
            )
            .field("Wallet", "\n".join(_balance_lines(holdings, oracles)), True)
            .field(
                "Stake",
                self._stake_summary(state, oracles, pending),
                True,
            )
            .field(
                "Lifetime",
                f"Kills **{int(state.get('total_kills') or 0):,}**  "
                f"-  Tames **{int(state.get('total_captures') or 0):,}**  "
                f"-  Bosses **{int(state.get('bosses_slain') or 0):,}**\n"
                f"Runs **{int(state.get('total_runs') or 0):,}**  "
                f"-  Cashout "
                f"**{fmt_usd(to_human(int(state.get('total_usd_cashout_raw') or 0)))}**",
                False,
            )
        )
        if state.get("last_action_at"):
            embed = embed.footer(
                f"Last action {fmt_ts(state['last_action_at'])}  -  "
                f"`,delve start` to dive again."
            )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @staticmethod
    def _skill_status(state: dict, cmeta: dict) -> str:
        """Compact one-liner listing all 3 class abilities with CD tags.

        Reads the legacy ``skill_cd_remaining`` for the primary ability
        plus the new ``_ability_cd_<key>`` entries in ``player_buffs``
        for the rest, so the stats panel matches the in-combat picker.
        """
        cd_primary = int(state.get("skill_cd_remaining") or 0)
        buffs = dict(state.get("player_buffs") or {})
        keys = dc.class_abilities(cmeta.get("key") or "")
        if not keys:
            tag = "ready" if cd_primary <= 0 else f"{cd_primary}r"
            return f"{cmeta.get('skill_name', '?')}  ({tag})"
        parts: list[str] = []
        for i, k in enumerate(keys):
            ameta = dc.ability_meta(k) or {}
            cd = 0
            if i == 0:
                cd = max(cd, cd_primary)
            payload = buffs.get("_ability_cd_" + k)
            if isinstance(payload, dict):
                cd = max(cd, max(0, int(payload.get("duration") or 0) - 1))
            tag = "ready" if cd <= 0 else f"{cd}r"
            parts.append(f"{ameta.get('emoji', '')} **{ameta.get('name', k)}** ({tag})")
        return "  ·  ".join(parts)

    @staticmethod
    def _stake_summary(state: dict, oracles: dict[str, float], pending_raw: int) -> str:
        rune_oracle = oracles.get(dc.RUNE_SYMBOL, 0.0)
        pending_h = to_human(int(pending_raw or 0))
        lines: list[str] = []
        for sym, col in (
            (dc.COPPER_SYMBOL, "copper_staked_raw"),
            (dc.SILVER_SYMBOL, "silver_staked_raw"),
            (dc.GOLD_SYMBOL,   "gold_staked_raw"),
        ):
            staked_raw = int(state.get(col) or 0)
            if staked_raw <= 0:
                continue
            staked_h = to_human(staked_raw)
            lines.append(
                f"{_fmt_ore(sym, staked_h)}"
                f"{_with_usd(staked_h, oracles.get(sym, 0.0))}"
            )
        if pending_h > 0:
            lines.append(
                f"Pending {_fmt_rune(pending_h)}"
                f"{_with_usd(pending_h, rune_oracle)}"
            )
        return "\n".join(lines) if lines else "_(no stake)_"


    # ── Class selection ────────────────────────────────────────────────────

    @delve.command(name="class", aliases=["pick", "choose"])
    async def delve_class(self, ctx: DiscoContext, class_key: str = "") -> None:
        """Pick warrior / mage / rogue / archer / druid.

        Initial pick is free. Use ``,delve reroll <class>`` to switch
        later (cost ramps geometrically per reroll).
        """
        key = (class_key or "").strip().lower()
        if key not in dc.CLASSES:
            lines = []
            for m in dc.CLASSES.values():
                wts = ", ".join(m.get("weapon_types") or ()) or "-"
                ats = ", ".join(m.get("armor_types") or ()) or "-"
                lines.append(
                    f"{m['emoji']} `{m['key']}` -- {m['blurb']}\n"
                    f"  -# Weapons: {wts}  ·  Armor: {ats}"
                )
            menu = "\n".join(lines)
            await ctx.reply_error_hint(
                f"Pick one of: {', '.join(dc.CLASSES)}.",
                hint="delve class warrior",
            )
            await ctx.reply(
                embed=card("Classes", description=menu, color=C_INFO).build(),
                mention_author=False,
            )
            return
        try:
            await dsvc.set_class(ctx.db, ctx.guild_id, ctx.author.id, key)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # Generic + per-class trigger so quests / achievements can count
        # both "you picked any class" and "you picked archer specifically".
        await self._fan_out(ctx.author.id, ctx.guild_id, "delve_class_picked")
        await self._fan_out(
            ctx.author.id, ctx.guild_id, f"delve_class_picked_{key}",
        )
        meta = dc.CLASSES[key]
        starter_w = dc.weapon_meta(meta.get("starter_weapon") or "") or {}
        starter_a = dc.armor_meta(meta.get("starter_armor") or "") or {}
        await ctx.reply_success(
            f"You are now a **{meta['name']}**.\n"
            f"Starter kit: **{starter_w.get('name', '?')}** "
            f"+ **{starter_a.get('name', '?')}**.\n"
            f"`,delve start` to enter the dungeon.",
            title="Class chosen",
        )

    # ── Class reroll ──────────────────────────────────────────────────────

    @delve.command(name="reroll", aliases=["respec_class", "switch"])
    async def delve_reroll(self, ctx: DiscoContext, new_class: str = "") -> None:
        """Switch your delve class. Cost ramps geometrically per reroll.

        Usage: ``,delve reroll archer``. Inventory + level + XP +
        captures + stat-point allocations are preserved; only the class
        identity, equipped weapon/armor, skill cooldown, and active
        buffs change. Refused mid-run -- ``,delve rest`` first.
        """
        key = (new_class or "").strip().lower()
        if not key:
            state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
            rerolls = int(state.get("class_rerolls_used") or 0)
            cost = dc.class_reroll_cost_usd(rerolls)
            menu_rows = []
            for m in dc.CLASSES.values():
                if str(m["key"]) == str(state.get("class_key") or ""):
                    continue
                menu_rows.append(f"{m['emoji']} `{m['key']}` -- {m['blurb']}")
            await ctx.reply(
                embed=card(
                    "Class reroll",
                    description=(
                        f"Current class: **{state.get('class_key') or '-'}**\n"
                        f"Reroll #{rerolls + 1} costs **${cost:,.2f}** "
                        f"(wallet + bank).\n\n"
                        f"Pick a target:\n" + "\n".join(menu_rows) +
                        f"\n\nUsage: `,delve reroll <class>`."
                    ),
                    color=C_INFO,
                ).build(),
                mention_author=False,
            )
            return
        if key not in dc.CLASSES:
            await ctx.reply_error(
                f"Unknown class. Pick one of: {', '.join(dc.CLASSES)}."
            )
            return
        try:
            new_state = await dsvc.reroll_class(
                ctx.db, ctx.guild_id, ctx.author.id, key,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        meta = dc.CLASSES[key]
        await ctx.bot.bus.publish(
            "delve_class_reroll",
            guild=ctx.guild, user=ctx.author,
            new_class=key,
            rerolls_used=int(new_state.get("class_rerolls_used") or 0),
        )
        await ctx.reply_success(
            f"You are now a **{meta['name']}**.\n"
            f"Starter kit equipped: **{dc.weapon_meta(meta['starter_weapon']).get('name')}** "
            f"+ **{dc.armor_meta(meta['starter_armor']).get('name')}**.\n"
            f"Stat-point allocations preserved. Skill cooldown reset.",
            title="Class rerolled",
        )

    # ── Stat-point upgrade ────────────────────────────────────────────────

    @delve.command(name="upgrade", aliases=["spend_pts", "alloc_delve"])
    async def delve_upgrade(
        self, ctx: DiscoContext, *, spec: str = "",
    ) -> None:
        """Spend stat points across Hardiness / Power / Vigor / Wisdom.

        Three accepted forms (you don't have to remember all of them):
          * ``,delve upgrade 2 1 0 0``        -- positional HP / ATK / SPD / INT
          * ``,delve upgrade atk 5``          -- named, one stat at a time
          * ``,delve upgrade hp 3 atk 2``     -- named, multi-stat in one line

        Each level grants 1 point. Bonuses (per point):
          * **Hardiness** -- +4 max HP
          * **Power**     -- +0.6 ATK (scales melee + ranged swings)
          * **Vigor**     -- +0.005 SPD (scales crit + first-strike chance)
          * **Wisdom**    -- +0.6 INT (scales spell damage; Mage / Druid)

        Pass no args to see your current allocations + a quick-spend button.
        """
        state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        if not state.get("class_key"):
            await ctx.reply_error_action(
                "Pick a class first.",
                button_label="Pick a class",
                command="delve class warrior",
            )
            return
        level = int(state.get("level") or 1)
        hp_a = int(state.get("hp_alloc")  or 0)
        at_a = int(state.get("atk_alloc") or 0)
        sp_a = int(state.get("spd_alloc") or 0)
        in_a = int(state.get("int_alloc") or 0)
        avail = dc.stat_points_available(level, hp_a, at_a, sp_a, in_a)

        try:
            hardiness, power, vigor, wisdom = _parse_delve_upgrade_args(
                tuple((spec or "").split()),
            )
        except ValueError as exc:
            await ctx.reply_error_hint(
                str(exc),
                hint=(
                    "Try `,delve upgrade 1 1 0 0` (HP / ATK / SPD / INT) "
                    "or `,delve upgrade atk 2` for one stat at a time."
                ),
                command_name="delve upgrade",
            )
            return
        total_request = hardiness + power + vigor + wisdom

        if total_request <= 0:
            view = _DelveUpgradeView(self, ctx.author.id, available=avail)
            respecs_used = int(state.get("stat_respecs_used") or 0)
            respec_cost = dc.respec_cost_usd(respecs_used)
            await ctx.reply(
                embed=card(
                    f"\U0001F4CA  Stat upgrade -- Lv. {level}",
                    color=C_INFO,
                    description=(
                        f"**How points work**\n"
                        f"Every dungeon level grants "
                        f"**{dc.STAT_POINTS_PER_LEVEL}** stat point. Points "
                        f"are SHARED across all four lanes -- spend wherever "
                        f"you want, in any order. Allocations are sticky: "
                        f"they survive class rerolls, gear swaps, and run "
                        f"resets. Only `,delve respec` (currently "
                        f"**${respec_cost:,.0f}**) refunds them.\n\n"
                        f"**Current allocations**\n"
                        f"❤️  Hardiness **{hp_a}**  ·  +{int(hp_a * dc.STAT_POINT_HP_BONUS)} max HP\n"
                        f"⚔️  Power **{at_a}**  ·  +{at_a * dc.STAT_POINT_ATK_BONUS:g} ATK\n"
                        f"\U0001F4A8  Vigor **{sp_a}**  ·  +{sp_a * dc.STAT_POINT_SPD_BONUS:.3f} SPD\n"
                        f"✨  Wisdom **{in_a}**  ·  +{in_a * dc.STAT_POINT_INT_BONUS:g} INT (spell dmg)\n\n"
                        f"**Per-point payoff**\n"
                        f"❤️ +{dc.STAT_POINT_HP_BONUS:g} max HP  ·  "
                        f"⚔️ +{dc.STAT_POINT_ATK_BONUS:g} ATK (melee + ranged swings)\n"
                        f"\U0001F4A8 +{dc.STAT_POINT_SPD_BONUS * 100:g}% SPD "
                        f"(boosts crit chance + first-strike odds)  ·  "
                        f"✨ +{dc.STAT_POINT_INT_BONUS:g} INT "
                        f"(spell damage -- Mage / Druid)\n\n"
                        f"**Available: {avail}** unspent point(s).\n"
                        f"Tap a button to spend a point, or run "
                        f"`,delve upgrade hp 3 atk 2` to spend several at once.\n"
                        f"Made a wrong call? `,delve respec` refunds every "
                        f"point for a USD fee (doubles each respec)."
                    ),
                ).build(),
                view=view if avail > 0 else None,
                mention_author=False,
            )
            return
        try:
            await dsvc.spend_stat_points(
                ctx.db, ctx.guild_id, ctx.author.id,
                hp=hardiness, atk=power,
                spd=vigor, int_=wisdom,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await self._fan_out(
            ctx.author.id, ctx.guild_id, "delve_stat_spent",
            amount=int(total_request),
        )
        await ctx.reply_success(
            f"Spent **{total_request}** point(s): "
            f"+{hardiness} HP, +{power} ATK, +{vigor} SPD, +{wisdom} INT.\n"
            f"Run `,delve upgrade` to see the new totals.",
            title="Stat points spent",
        )

    # ── Stat-point respec ─────────────────────────────────────────────────

    @delve.command(name="respec", aliases=["restat", "reset_points", "resetpoints"])
    async def delve_respec(self, ctx: DiscoContext) -> None:
        """Refund every spent stat point for a USD fee.

        Zeroes Hardiness / Power / Vigor / Wisdom back to 0 so you can
        rebuild from scratch via ``,delve upgrade``. Cost doubles every
        time you respec the same delver:

          * respec #1 -- $10,000
          * respec #2 -- $20,000
          * respec #3 -- $40,000
          * ... (doubles each time)

        Refused mid-run -- ``,delve rest`` first. Class + level + XP +
        gear + captures + ore + RUNE all stay; only the four stat-point
        allocations are wiped.
        """
        state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        if not state.get("class_key"):
            await ctx.reply_error_action(
                "Pick a class first.",
                button_label="Pick a class",
                command="delve class warrior",
            )
            return

        level = int(state.get("level") or 1)
        hp_a = int(state.get("hp_alloc")  or 0)
        at_a = int(state.get("atk_alloc") or 0)
        sp_a = int(state.get("spd_alloc") or 0)
        in_a = int(state.get("int_alloc") or 0)
        spent = hp_a + at_a + sp_a + in_a
        respecs_used = int(state.get("stat_respecs_used") or 0)
        cost_usd = dc.respec_cost_usd(respecs_used)
        next_cost = dc.respec_cost_usd(respecs_used + 1)

        if spent <= 0:
            await ctx.reply_error(
                "You have no spent stat points to refund yet. "
                "Earn some by levelling, then `,delve upgrade` to spend "
                f"them. (You'd save **${cost_usd:,.2f}** by waiting.)"
            )
            return
        if state.get("run_id"):
            await ctx.reply_error(
                "Can't respec mid-run -- `,delve rest` first."
            )
            return

        confirmed = await ctx.confirm(
            f"Respec for **${cost_usd:,.2f}**?\n\n"
            f"Refunds **{spent}** spent point(s):\n"
            f"  -  ❤️  Hardiness **{hp_a}**\n"
            f"  -  ⚔️  Power **{at_a}**\n"
            f"  -  \U0001F4A8  Vigor **{sp_a}**\n"
            f"  -  ✨  Wisdom **{in_a}**\n\n"
            f"After respec you'll have **{level * dc.STAT_POINTS_PER_LEVEL}** "
            f"points available to reallocate via `,delve upgrade`.\n"
            f"Your next respec will cost **${next_cost:,.2f}**.",
        )
        if not confirmed:
            return

        try:
            _, paid_usd, refunded = await dsvc.respec_stat_points(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        new_available = level * dc.STAT_POINTS_PER_LEVEL
        await ctx.bot.bus.publish(
            "delve_stat_respec",
            guild=ctx.guild, user=ctx.author,
            refunded=int(refunded), cost_usd=float(paid_usd),
            respecs_used=respecs_used + 1,
        )
        await ctx.reply_success(
            f"Refunded **{refunded}** point(s). You now have "
            f"**{new_available}** available to spend with `,delve upgrade`.\n"
            f"Paid: **${paid_usd:,.2f}**. Next respec on this delver: "
            f"**${next_cost:,.2f}**.",
            title="Stat points refunded",
        )

    # ── Run lifecycle ──────────────────────────────────────────────────────

    @delve.command(name="start", aliases=["enter", "go"])
    @user_cooldown(dc.RUN_COOLDOWN_S)
    async def delve_start(self, ctx: DiscoContext) -> None:
        """Begin a new dungeon run. Spawns you on Floor 1, Room 0."""
        try:
            res = await dsvc.start_run(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await self._fan_out(ctx.author.id, ctx.guild_id, "delve_run_start")
        # Land directly into a button-driven room view -- the player can
        # tap Next / Rest from the entry message instead of having to
        # type ,delve next.
        embed = (
            card(
                f"\U0001F6AA  Floor {res.floor}: {dc.floor_meta(res.floor).get('name')}",
                color=_floor_color(res.state),
            )
            .description(
                _frame_block("corridor")
                + "\n\U00002728 You enter the dungeon. Tap **Next** to advance."
            )
            .footer(
                f"Run #{res.run_id}  -  HP "
                f"{res.state.get('current_hp')}/{res.state.get('hp_max')}"
            )
            .build()
        )
        room_view = _DelveRoomView(self, ctx)
        # Floor entry is an empty corridor: only Next + Rest enabled.
        _delve_set_button_visibility(room_view, "empty")
        sent = await ctx.reply(embed=embed, view=room_view, mention_author=False)
        room_view.message = sent

    @delve.command(name="next", aliases=["advance", "explore"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_next(self, ctx: DiscoContext) -> None:
        """Advance to the next room of the current floor."""
        try:
            await dsvc.advance_room(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await self._show_panel(ctx)

    @delve.command(name="descend", aliases=["down"])
    async def delve_descend(self, ctx: DiscoContext) -> None:
        """Take the stairs to the next floor."""
        try:
            await dsvc.descend(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        state_after = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        await self._fan_out(
            ctx.author.id, ctx.guild_id, "delve_floor_reached",
            amount=int(state_after.get("current_floor") or 0),
        )
        await self._show_panel(ctx)

    @delve.command(name="rest", aliases=["leave", "exit"])
    async def delve_rest(self, ctx: DiscoContext) -> None:
        """End the current run. Returns to the surface and full-heals."""
        state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        if not state.get("run_id"):
            await ctx.reply_error("You're not currently delving.")
            return
        if state.get("current_mob_state"):
            await ctx.reply_error(
                "You can't rest mid-combat. `,delve flee` first."
            )
            return
        outcome = "rest"
        await dsvc.end_run(ctx.db, ctx.guild_id, ctx.author.id, outcome)
        await ctx.reply_success(
            "You retreat to the surface. Full HP, ready for another run.",
            title="Run ended",
        )

    # ── Combat ─────────────────────────────────────────────────────────────

    def _delve_battle_text_block(
        self,
        res: "dsvc.CombatResult",
        *,
        state: dict | None = None,
        class_key: str = "",
        class_name: str = "Adventurer",
        action_banner: str = "",
        is_player_turn: bool = True,
    ) -> str:
        """Build the ASCII battle-frame block for a delve **mob** fight.

        Mob fights are rendered as plain ASCII inside the embed code
        fence (no PNG). Wild-buddy encounters in the delve keep the
        ``services.buddy_battle_scene`` PNG renderer -- that path lives
        in ``_DelveWildBuddyView`` and is unaffected here.

        Returns an empty string on any failure so the caller can fall
        back to the text-only summary lines.
        """
        try:
            from services.delve_battle_render import (
                MobView,
                PlayerView,
                render_mob_battle_frame,
            )
            mob_state = dict(res.mob_state or {})
            mob_key = str(mob_state.get("key") or "")
            mob_meta = dc.mob_meta(mob_key) or {}
            tags = tuple(mob_meta.get("tags") or ())
            weapon = dc.weapon_meta((state or {}).get("equipped_weapon") or "") or {}
            wk = str(weapon.get("attack_kind") or "melee")
            cmeta = dc.class_meta(class_key) or {}
            player = PlayerView(
                name="You",
                class_key=str(class_key or ""),
                class_name=str(class_name or cmeta.get("name") or "Adventurer"),
                level=int((state or {}).get("level") or 1),
                hp=int(res.player_hp),
                max_hp=max(1, int(res.player_max_hp)),
                atk=int((state or {}).get("atk") or cmeta.get("atk_base") or 5),
                defense=int((state or {}).get("defense") or cmeta.get("def_base") or 2),
                spd=float((state or {}).get("spd") or cmeta.get("spd_base") or 0.5),
                stamina=int((state or {}).get("stamina") or 0),
                stamina_max=int((state or {}).get("stamina_max") or 5),
                skill_cd=int((state or {}).get("skill_cd_remaining") or 0),
                weapon_kind=wk,
            )
            mob_tier = int(mob_state.get("tier") or mob_meta.get("tier") or 1)
            mob_lvl = int(mob_state.get("level") or mob_meta.get("level") or mob_tier)
            mob = MobView(
                key=mob_key,
                name=str(mob_meta.get("name") or mob_key.title() or "Mob"),
                tier=mob_tier,
                level=mob_lvl,
                hp=int(mob_state.get("hp") or 0),
                max_hp=max(1, int(mob_state.get("max_hp") or 1)),
                atk=int(mob_state.get("atk") or mob_meta.get("atk_base") or 1),
                defense=int(mob_state.get("def") or mob_meta.get("def_base") or 0),
                spd=float(mob_state.get("spd") or mob_meta.get("spd_base") or 0.5),
                is_boss=bool(mob_meta.get("boss") or mob_state.get("boss")),
                is_undead="undead" in tags,
                status=str(mob_state.get("status") or ""),
            )
            return render_mob_battle_frame(
                player=player,
                mob=mob,
                round_num=int(mob_state.get("round") or 1),
                max_rounds=int(dc.BATTLE_MAX_ROUNDS),
                floor=int((state or {}).get("current_floor") or 1),
                action_banner=action_banner,
                is_player_turn=is_player_turn,
            )
        except Exception:
            log.debug("delve mob battle text block: render failed", exc_info=True)
            return ""

    async def _battle_embed_from_result(
        self, ctx: DiscoContext, res: dsvc.CombatResult,
        wild_buddy: dict | None = None,
    ) -> tuple[discord.Embed, discord.File | None]:
        """Build the embed shown by ``_DelveBattleView`` for a mob fight.

        Mob fights render the new ASCII battle frame inside the embed
        description; no scene PNG is attached. The function still
        returns ``(embed, None)`` so callers that already expect the
        ``(embed, file)`` tuple keep working -- wild-buddy encounters
        live in ``_DelveWildBuddyView`` and keep the PNG renderer.
        """
        oracles = await _oracles(ctx)
        state_now: dict = {}
        try:
            state_now = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        except Exception:
            log.debug("delve battle embed: state lookup failed", exc_info=True)

        buddy_tag = ""
        if wild_buddy:
            buddy_tag = (
                f"\U0001F4AB \U0001FA77 *A wild buddy appeared!* "
                f"`{wild_buddy.get('species', '?')}` \U0001FA77 \U0001F4AB\n\n"
            )

        class_emoji = "\U0001F5E1"
        class_name = "Adventurer"
        class_key = str(state_now.get("class_key") or "")
        if hasattr(res, "_class_emoji"):
            class_emoji = getattr(res, "_class_emoji") or class_emoji
        if hasattr(res, "_class_name"):
            class_name = getattr(res, "_class_name") or class_name

        def _scene(banner: str, *, turn: bool = True) -> str:
            if wild_buddy:
                return ""
            block = self._delve_battle_text_block(
                res, state=state_now, class_key=class_key,
                class_name=class_name, action_banner=banner,
                is_player_turn=turn,
            )
            return f"```\n{block}\n```\n" if block else ""

        if res.outcome == "mob_dead":
            extras: list[str] = []
            if res.ore_drop_symbol and res.ore_drop_qty_human > 0:
                extras.append(
                    f"\U0001F381  + {_fmt_ore(res.ore_drop_symbol, res.ore_drop_qty_human)}"
                    f"{_with_usd(res.ore_drop_qty_human, oracles.get(res.ore_drop_symbol, 0.0))}"
                )
            if res.rune_drop_human > 0:
                extras.append(
                    f"\U0001F381  + {_fmt_rune(res.rune_drop_human)}"
                    f"{_with_usd(res.rune_drop_human, oracles.get(dc.RUNE_SYMBOL, 0.0))}"
                )
            if res.leveled_up:
                extras.append(
                    f"\U0001F31F \U00002728 **Level up! Lv. {res.new_level}** "
                    f"\U00002728 \U0001F31F"
                )
            desc = (
                _scene("VICTORY!")
                + buddy_tag
                + "\n".join(res.log[-6:])
                + (("\n\n" + "\n".join(extras)) if extras else "")
                + f"\n\n\U0001F4AB **+{res.mob_xp} XP**"
            )
            if res.boss_kill:
                title = "\U0001F451 \U00002728 Boss slain! \U00002728"
                color = C_GOLD
            elif res.mini_boss_kill:
                title = "\U0001F38F \U00002728 Mini-boss slain! \U00002728"
                color = C_PURPLE
            else:
                title = "\U0001F4AB \U00002694 Victory! \U00002694 \U0001F4AB"
                color = C_SUCCESS
            embed = card(title, color=color).description(desc).footer(
                f"Rounds played: {(res.mob_state or {}).get('round', '?')}"
            ).build()
            return embed, None
        if res.outcome == "player_dead":
            desc = (
                _scene("K.O.", turn=False)
                + buddy_tag
                + "\n".join(res.log[-6:])
                + "\n\n*You wake up at the surface, bruised but alive...*"
            )
            embed = card(
                "\U0001F480  You fainted", color=C_CRIMSON,
            ).description(desc).footer("Try again! \U0001F49E").build()
            return embed, None
        if res.outcome == "fled":
            desc = (
                _scene("FLED")
                + buddy_tag
                + "\U0001F45F  *You scamper away from the fight!*\n\n"
                + "\n".join(res.log[-4:])
            )
            embed = (
                card("\U0001F4A8  Fled the fight", color=C_AMBER)
                .description(desc)
                .footer("Still in the dungeon -- ,delve rest to exit and restore HP.")
                .build()
            )
            return embed, None
        if res.outcome == "failed_flee":
            desc = (
                _scene("MISS")
                + buddy_tag
                + "\U0001F633  *Couldn't escape! The fight continues...*\n\n"
                + "\n".join(res.log[-4:])
            )
            embed = card(
                "\U000026A0  Failed to flee", color=C_AMBER,
            ).description(desc).build()
            return embed, None

        # "continue" outcome -- the in-fight battle panel.
        return self._cute_battle_panel(
            ctx, res, wild_buddy=wild_buddy, buddy_tag=buddy_tag,
            state_now=state_now,
            class_key=class_key,
            class_emoji=class_emoji, class_name=class_name,
        )

    def _cute_battle_panel(
        self, ctx: DiscoContext, res: dsvc.CombatResult, *,
        wild_buddy: dict | None = None, buddy_tag: str = "",
        state_now: dict | None = None,
        class_key: str = "",
        class_emoji: str = "\U0001F5E1", class_name: str = "Adventurer",
    ) -> tuple[discord.Embed, discord.File | None]:
        """In-fight battle panel for the ``continue`` outcome.

        For mob fights, embeds the new ASCII battle frame directly in
        the description and returns ``(embed, None)``. For wild-buddy
        encounters (still routed through this panel via
        ``_battle_embed_from_result``), the renderer side handles the
        PNG path -- here we keep the legacy two-line header so the
        wild-buddy code path stays visually consistent.
        """
        state_now = state_now or {}
        player_level = int(state_now.get("level") or 1)

        # Last two log lines render below the ASCII frame.
        recent_log = "\n".join(f"\U0001F4AC  {ln}" for ln in res.log[-2:]) or "_..._"

        if wild_buddy:
            # Legacy two-line header preserved for wild-buddy fights;
            # the PNG scene is composed by the wild-buddy view, not us.
            you_line = (
                f"{class_emoji} **You** -- Lv {player_level} {class_name}\n"
                f"`{_cute_hp_bar(res.player_hp, res.player_max_hp)}`  "
                f"`{res.player_hp}/{res.player_max_hp} HP`"
            )
            mob_line = "_(no opponent)_"
            if res.mob_state:
                mob_key = str(res.mob_state.get("key") or "")
                mob_meta = dc.mob_meta(mob_key) or {}
                mob_emoji = mob_meta.get("emoji", "\U0001F47E")
                mob_name = mob_meta.get("name", "Mystery Mob")
                tier_n = int(res.mob_state.get("tier") or mob_meta.get("tier", 1) or 1)
                tier_stars = "\U00002B50" * max(1, min(5, tier_n))
                mob_lvl = int(res.mob_state.get("level") or mob_meta.get("level") or tier_n)
                mob_line = (
                    f"{mob_emoji} **{mob_name}** -- Lv {mob_lvl} {tier_stars}\n"
                    f"`{_cute_hp_bar(int(res.mob_state.get('hp', 0)), int(res.mob_state.get('max_hp', 1)))}`  "
                    f"`{res.mob_state.get('hp')}/{res.mob_state.get('max_hp')} HP`"
                )
            desc = (
                buddy_tag
                + you_line
                + "\n\n*~~~~ vs ~~~~*\n\n"
                + mob_line
                + "\n\n"
                + recent_log
            )
            embed = (
                card(
                    "\U00002694 \U00002728 Battle! \U00002728 \U00002694",
                    color=C_PURPLE,
                )
                .description(desc)
                .footer(
                    "\U00002694 Strike  -  \U0001F4A5 Skill  -  "
                    "\U0001F9F2 Capture  -  \U0001F45F Flee"
                )
                .build()
            )
            return embed, None

        # Mob fight -- render the ASCII battle frame inline.
        block = self._delve_battle_text_block(
            res, state=state_now, class_key=class_key,
            class_name=class_name, action_banner="FIGHT!",
        )
        scene_md = f"```\n{block}\n```\n" if block else ""
        desc = scene_md + recent_log

        embed = (
            card(
                "\U00002694 \U00002728 Battle! \U00002728 \U00002694",
                color=C_INFO,
            )
            .description(desc)
            .footer(
                "\U00002694 Strike  -  \U0001F4A5 Skill  -  "
                "\U0001F45F Flee"
            )
            .build()
        )
        return embed, None

    async def _resolve_swing(
        self, ctx: DiscoContext, mode: str,
        *, ability_key: str | None = None,
    ) -> None:
        # Snapshot the class BEFORE the swing so a level-up that fires
        # delve_class_lv10 can compare old vs new level cleanly.
        pre_state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        class_key = str(pre_state.get("class_key") or "")
        pre_level = int(pre_state.get("level") or 1)
        # ``skill_key`` is the legacy fan-out signal; for the new
        # ability path we report the actual ability_key fired so quests
        # / achievements can target Frostbolt / Aimed Shot specifically.
        if mode == "skill":
            fired_ability = (dc.class_meta(class_key) or {}).get("skill_key") or ""
        elif mode == "ability":
            fired_ability = str(ability_key or "")
        else:
            fired_ability = ""

        try:
            res = await dsvc.resolve_attack(
                ctx.db, ctx.guild_id, ctx.author.id,
                mode=mode, ability_key=ability_key,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        # Ability fan-out so quests / achievements can count usage by
        # specific ability key (Volley / Wildshape / etc.) without
        # sniffing the log.
        if fired_ability:
            await self._fan_out(
                ctx.author.id, ctx.guild_id, f"delve_skill_{fired_ability}",
            )

        # Ore + RUNE drops on a kill. Side-effects on oracles handled inside.
        granted_badges: list[str] = []
        if res.outcome == "mob_dead":
            await dsvc.credit_combat_drops(
                ctx.db, ctx.guild_id, ctx.author.id, res,
            )
            granted_badges += await self._fan_out(
                ctx.author.id, ctx.guild_id, "delve_kill",
            )
            if class_key:
                granted_badges += await self._fan_out(
                    ctx.author.id, ctx.guild_id, f"delve_kill_{class_key}",
                )
            if res.boss_kill:
                granted_badges += await self._fan_out(
                    ctx.author.id, ctx.guild_id, "delve_boss_kill",
                )
            if res.rune_drop_human > 0:
                granted_badges += await self._fan_out(
                    ctx.author.id, ctx.guild_id, "delve_rune_earned",
                    amount=int(res.rune_drop_human),
                )
            # Lv 10 milestone -- counts each class only once via a per-
            # class trigger so the pentafecta achievement (5 distinct
            # classes at Lv 10) tallies cleanly.
            if (
                class_key
                and pre_level < 10
                and int(res.new_level or pre_level) >= 10
            ):
                granted_badges += await self._fan_out(
                    ctx.author.id, ctx.guild_id, f"delve_class_lv10_{class_key}",
                )
            # Auto-end run on a death-cap or boss kill on F20 (game cleared).
            state_after = await dsvc.list_state(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if (
                res.boss_kill
                and int(state_after.get("current_floor") or 0) >= dc.MAX_FLOOR
            ):
                await dsvc.end_run(
                    ctx.db, ctx.guild_id, ctx.author.id, "cleared",
                )
                granted_badges += await self._fan_out(
                    ctx.author.id, ctx.guild_id, "delve_clear_run",
                )

        if res.outcome == "player_dead":
            await dsvc.end_run(ctx.db, ctx.guild_id, ctx.author.id, "died")

        await self._render_combat_result(ctx, res)
        if granted_badges:
            await self._announce_badges(ctx.channel, granted_badges)

    async def _render_combat_result(
        self, ctx: DiscoContext, res: dsvc.CombatResult,
    ) -> None:
        oracles = await _oracles(ctx)
        if res.outcome == "mob_dead":
            extras: list[str] = []
            if res.ore_drop_symbol and res.ore_drop_qty_human > 0:
                extras.append(
                    f"+ {_fmt_ore(res.ore_drop_symbol, res.ore_drop_qty_human)}"
                    f"{_with_usd(res.ore_drop_qty_human, oracles.get(res.ore_drop_symbol, 0.0))}"
                )
            if res.rune_drop_human > 0:
                extras.append(
                    f"+ {_fmt_rune(res.rune_drop_human)}"
                    f"{_with_usd(res.rune_drop_human, oracles.get(dc.RUNE_SYMBOL, 0.0))}"
                )
            if res.leveled_up:
                extras.append(f"\U0001F31F **Level up! Lv. {res.new_level}**")
            desc = (
                _frame_block("victory")
                + "\n"
                + "\n".join(res.log[-8:])
                + (("\n" + "\n".join(extras)) if extras else "")
                + f"\n\n+**{res.mob_xp} XP**"
            )
            embed = card(
                ("\U0001F451 Boss slain!" if res.boss_kill else "\U00002694 Victory"),
                color=(C_GOLD if res.boss_kill else C_SUCCESS),
            ).description(desc).build()
            # Quick-action junk drop view if a kill rolled an item.
            drop_view: discord.ui.View | None = None
            if res.junk_drop_key:
                drop_view = _JunkDropView(self, ctx, res.junk_drop_key)
            sent = await ctx.reply(
                embed=embed, view=drop_view, mention_author=False,
            )
            if drop_view is not None:
                drop_view.message = sent
            return
        if res.outcome == "player_dead":
            desc = _frame_block("defeat") + "\n" + "\n".join(res.log[-8:])
            embed = card("\U0001F480  You died", color=C_CRIMSON).description(desc).build()
            await ctx.reply(embed=embed, mention_author=False)
            return
        if res.outcome == "fled":
            desc = "\n".join(res.log[-6:])
            embed = (
                card("\U0001F45F  Fled the fight", color=C_AMBER)
                .description(desc)
                .footer(
                    f"Still inside the dungeon -- "
                    f"{ctx.prefix}delve rest to exit to the surface and restore HP."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return
        if res.outcome == "failed_flee":
            desc = "\n".join(res.log[-6:])
            embed = card(
                "\U000026A0  Failed to flee", color=C_AMBER,
            ).description(desc).build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        # continue: render the live mob HP + log
        desc = "\n".join(res.log[-8:])
        embed = (
            card("\U00002694 Round resolved", color=C_INFO)
            .description(desc)
            .field(
                "Your HP",
                f"`{_hp_bar(res.player_hp, res.player_max_hp)}` "
                f"{res.player_hp}/{res.player_max_hp}",
                False,
            )
        )
        if res.mob_state:
            mob_meta = dc.mob_meta(str(res.mob_state.get("key") or "")) or {}
            embed = embed.field(
                f"{mob_meta.get('emoji', '')} {mob_meta.get('name', '?')}",
                f"`{_hp_bar(int(res.mob_state.get('hp', 0)), int(res.mob_state.get('max_hp', 1)))}` "
                f"{res.mob_state.get('hp')}/{res.mob_state.get('max_hp')}",
                False,
            )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @delve.command(name="attack", aliases=["a", "fight", "swing"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_attack(self, ctx: DiscoContext) -> None:
        """Basic swing in combat."""
        await self._resolve_swing(ctx, "attack")

    @delve.command(name="skill", aliases=["s", "cast", "ability"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_skill(
        self, ctx: DiscoContext, *, ability: str = "",
    ) -> None:
        """Cast a class ability. Defaults to your primary ability when no
        argument is given (legacy single-skill behaviour).

        Usage:
          ,delve skill                    -- cast primary ability
          ,delve skill cleave             -- cast a specific ability
          ,delve skill shieldbash         -- cast another ability
        """
        key = (ability or "").strip().lower().replace(" ", "_").replace("-", "_")
        if not key:
            await self._resolve_swing(ctx, "skill")
            return
        # Validate against the player's class abilities so a wrong key
        # gets a friendly hint instead of a generic combat error.
        state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        class_key = str(state.get("class_key") or "")
        keys = dc.class_abilities(class_key)
        if key not in keys:
            avail = ", ".join(f"`{k}`" for k in keys) or "_(set a class first)_"
            await ctx.reply_error(
                f"Unknown ability `{key}`. Available: {avail}"
            )
            return
        await self._resolve_swing(ctx, "ability", ability_key=key)

    @delve.command(name="flee", aliases=["run"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_flee(self, ctx: DiscoContext) -> None:
        """Escape the current mob fight (55% chance, costs HP). Still in the dungeon after -- use ,delve rest to exit."""
        try:
            res = await dsvc.resolve_flee(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await self._render_combat_result(ctx, res)

    @delve.command(name="capture", aliases=["tame", "catch"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_capture(self, ctx: DiscoContext) -> None:
        """Attempt to tame the active mob (must be at low HP)."""
        # If the player has a charm primed, consume it on the attempt.
        state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        cons = dict(state.get("consumables") or {})
        charm_primed = int(cons.get("tame_charm") or 0) > 0
        try:
            res = await dsvc.attempt_capture(
                ctx.db, ctx.guild_id, ctx.author.id, charm=charm_primed,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if charm_primed:
            cons["tame_charm"] = max(0, int(cons["tame_charm"]) - 1)
            if cons["tame_charm"] <= 0:
                cons.pop("tame_charm", None)
            await ctx.db.execute(
                "UPDATE user_dungeon SET consumables = $3::jsonb, updated_at = NOW() "
                "WHERE guild_id = $1 AND user_id = $2",
                ctx.guild_id, ctx.author.id, __import__("json").dumps(cons),
            )
        if res.success:
            await self._fan_out(ctx.author.id, ctx.guild_id, "delve_capture")
            # Per-class capture trigger so quests like "Speak To The Wild"
            # (Druid 3 captures) can count cleanly without bus filters.
            cur_state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
            if cur_state.get("class_key"):
                await self._fan_out(
                    ctx.author.id, ctx.guild_id,
                    f"delve_capture_{cur_state['class_key']}",
                )
            meta = dc.mob_meta(res.mob_key or "") or {}
            desc = (
                _frame_block("capture")
                + "\n"
                + "\n".join(res.log)
                + f"\n\n{meta.get('emoji', '')} **{meta.get('name', res.mob_key)}** is now in your party "
                f"(id #{res.party_id})."
            )
            embed = card(
                "\U0001F9F2  Capture!", color=C_PURPLE,
            ).description(desc).footer(
                f"Roll: {int(res.chance * 100)}% success"
            ).build()
            await ctx.reply(embed=embed, mention_author=False)
            return
        embed = card(
            "\U0001F4A8  It thrashes free", color=C_AMBER,
        ).description("\n".join(res.log)).footer(
            f"Roll: {int(res.chance * 100)}% chance"
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @delve.command(name="use", aliases=["drink", "consume"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_use(self, ctx: DiscoContext, item: str = "") -> None:
        """Use a consumable (e.g. `,delve use potion_minor`)."""
        key = (item or "").strip().lower()
        if not key or not dc.consumable_meta(key):
            options = ", ".join(f"`{k}`" for k in dc.CONSUMABLES)
            await ctx.reply_error_hint(
                "Pick a consumable.",
                hint=f"delve use potion_minor",
            )
            await ctx.reply(
                embed=card(
                    "Consumables", description=options, color=C_INFO,
                ).build(),
                mention_author=False,
            )
            return
        try:
            res = await dsvc.use_consumable(ctx.db, ctx.guild_id, ctx.author.id, key)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        meta = dc.consumable_meta(key) or {}
        await ctx.reply_success(
            f"{meta.get('emoji', '')} **{meta.get('name', key)}** -- {res.detail}",
            title="Used",
        )

    # ── Mining + chest ─────────────────────────────────────────────────────

    @delve.command(name="mine", aliases=["dig", "harvest"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_mine(self, ctx: DiscoContext) -> None:
        """Mine the ore vein in this room."""
        try:
            res = await dsvc.mine_ore(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await self._fan_out(ctx.author.id, ctx.guild_id, "delve_mine")
        sym_trigger = {
            dc.COPPER_SYMBOL: "delve_mined_copper",
            dc.SILVER_SYMBOL: "delve_mined_silver",
            dc.GOLD_SYMBOL:   "delve_mined_gold",
        }.get(res.ore_symbol)
        if sym_trigger:
            await self._fan_out(
                ctx.author.id, ctx.guild_id, sym_trigger,
                amount=max(1, int(res.qty_human)),
            )
        oracles = await _oracles(ctx)
        usd_value = res.qty_human * res.oracle_after
        usd_tag = f"  ~ **{fmt_usd(usd_value)}**" if usd_value > 0 else ""
        impact_line = ""
        if res.impact_pct > 0:
            impact_line = (
                f"\n-# {res.ore_symbol} oracle: "
                f"**${res.oracle_before:,.6f} -> ${res.oracle_after:,.6f}** "
                f"(slippage **{res.impact_pct * 100:.2f}%**)"
            )
        junk_meta = dc.junk_meta(res.junk_drop_key) if res.junk_drop_key else None
        junk_line = (
            f"\n+{junk_meta['emoji']} **{junk_meta['name']}** "
            f"({str(junk_meta.get('kind', '')).title()}) -- "
            f"_{junk_meta.get('blurb', '')}_"
            if junk_meta else ""
        )
        desc = (
            _frame_block("mining")
            + f"\nMined **{_fmt_ore(res.ore_symbol, res.qty_human)}**{usd_tag}.\n"
            + "-# Mint event -- mined ore expanded supply, oracle drops."
            + impact_line
            + junk_line
        )
        embed = card("\U000026CF  Pickaxe Strike", color=C_GOLD).description(desc).build()
        await ctx.reply(embed=embed, mention_author=False)

    @delve.command(name="open", aliases=["loot", "chest"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_open(self, ctx: DiscoContext) -> None:
        """Open the chest in this room."""
        try:
            chest = await dsvc.open_chest(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        oracles = await _oracles(ctx)
        rune_amt = float(chest.rune_amount)
        relic_meta = dc.relic_meta(chest.relic_key) if chest.relic_key else None
        # Use the dedicated relic_drop frame if a relic actually surfaced
        # so the receipt visibly upgrades from "chest" to "relic moment".
        frame_key = "relic_drop" if relic_meta else "chest"
        relic_line = (
            f"\n{relic_meta['emoji']} **Relic dropped!** "
            f"{relic_meta['name']} ({str(relic_meta.get('rarity', 'common')).title()}) -- "
            f"{relic_meta.get('blurb', '')}"
            if relic_meta else ""
        )
        junk_meta = dc.junk_meta(chest.junk_drop_key) if chest.junk_drop_key else None
        junk_line = (
            f"\n{junk_meta['emoji']} +**{junk_meta['name']}** "
            f"({str(junk_meta.get('kind', '')).title()}) -- "
            f"_{junk_meta.get('blurb', '')}_"
            if junk_meta else ""
        )
        debt_line = (
            f"\n\U0001F64F **Shrine debt paid off!** Rune payout x{chest.shrine_debt_mult:g}."
            if chest.shrine_debt_mult and chest.shrine_debt_mult > 1.0 else ""
        )
        desc = (
            _frame_block(frame_key)
            + f"\nThe chest opens. **{_fmt_rune(rune_amt)}**"
            + _with_usd(rune_amt, oracles.get(dc.RUNE_SYMBOL, 0.0))
            + debt_line
            + relic_line
            + junk_line
        )
        # Bonus: 12% chance the chest also coughs up a buddy egg.  Goes
        # straight into the player's held-egg slot via the fishing service
        # so there's only one egg system to maintain.
        egg_note = ""
        if _random.random() < 0.12:
            try:
                from services import fishing as fish_svc
                await fish_svc.hatch_fishing_buddy(
                    ctx.db, ctx.guild_id, ctx.author.id, source="delve_chest",
                )
                egg_note = "\n\U0001F95A *A buddy egg tumbles out of the chest!*"
            except Exception:
                log.debug("delve open: egg hatch failed", exc_info=True)
        embed = card(
            "\U0001F4B0  Chest cracked", color=C_GOLD,
        ).description(desc + egg_note).build()
        await ctx.reply(embed=embed, mention_author=False)

    # -- ,delve scavenge -------------------------------------------------

    @delve.command(name="scavenge", aliases=["forage", "wander", "rummage"])
    async def delve_scavenge(self, ctx: DiscoContext) -> None:
        """Wander the surface ruins for a randomized payout.

        Free roll every 10 minutes (only available outside an active
        delve). Drops range from a small RUNE / ORE purse up to a
        cache of consumables, an Escape Scroll, or the rare Relic
        Shard jackpot which goes straight into your relic bag.
        Mirrors ``,farm forage`` and ``,fish beachcomb`` in shape.
        """
        try:
            res = await dsvc.scavenge(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        # Pre-frame so the reveal lands with a beat -- same send-then-edit
        # cadence the chest, dig, and forage views use.
        pre_msg = None
        try:
            pre_msg = await ctx.reply(embed=card(
                "\U0001F9F1 Picking through the rubble...",
                description=f"```\n{dc.FRAMES['scavenge_start']}\n```",
                color=C_AMBER,
            ).build(), mention_author=False)
            await asyncio.sleep(0.8)
        except Exception:
            log.debug("delve scavenge pre-frame send failed", exc_info=True)

        oracles = await _oracles(ctx)

        frame_key = {
            "rune_purse_small": "scavenge_rune",
            "rune_purse_big":   "scavenge_rune",
            "ore_pile_small":   "scavenge_ore",
            "ore_pile_big":     "scavenge_ore",
            "consumable_cache": "scavenge_consumable",
            "scroll_find":      "scavenge_scroll",
            "relic_shard":      "scavenge_relic",
            "empty":            "scavenge_empty",
        }.get(res.outcome_key, "scavenge_empty")
        frame = dc.FRAMES.get(frame_key, "")

        detail_lines: list[str] = []
        if res.rune_credited > 0:
            detail_lines.append(
                f"\U0001F4B0 **{_fmt_rune(res.rune_credited)}**"
                f"{_with_usd(res.rune_credited, oracles.get(dc.RUNE_SYMBOL, 0.0))}"
            )
        if res.ore_credited > 0 and res.ore_symbol:
            detail_lines.append(
                f"\U000026CF **{_fmt_ore(res.ore_symbol, res.ore_credited)}**"
                f"{_with_usd(res.ore_credited, oracles.get(res.ore_symbol, 0.0))}"
            )
        for key, qty in res.consumables_added:
            meta = dc.consumable_meta(key) or {}
            detail_lines.append(
                f"{meta.get('emoji', '')} **{qty}× {meta.get('name', key)}** "
                f"-- _{meta.get('blurb', '')}_"
            )
        scroll_default = "\U0001F4DC"
        for key, qty in res.scrolls_added:
            meta = dc.consumable_meta(key) or {}
            emoji = meta.get("emoji", "") or scroll_default
            detail_lines.append(
                f"{emoji} **{qty}× {meta.get('name', key)}** "
                f"-- _{meta.get('blurb', '')}_"
            )
        if res.relic_added:
            rk, qty = res.relic_added
            rmeta = dc.relic_meta(rk) or {}
            detail_lines.append(
                f"\U00002728 {rmeta.get('emoji', '')} "
                f"**{qty}× {rmeta.get('name', rk)}** "
                f"({str(rmeta.get('rarity', '?')).title()})  -- "
                f"_{rmeta.get('blurb', '')}_"
            )

        title = "\U0001F9F1 " + res.label
        color = (
            C_GOLD     if res.outcome_key == "relic_shard"
            else C_TEAL if res.outcome_key in ("consumable_cache", "scroll_find")
            else C_AMBER if res.outcome_key.startswith(("rune_", "ore_"))
            else C_NEUTRAL
        )

        desc = (
            f"```\n{frame}\n```\n"
            + ("\n".join(detail_lines) if detail_lines
               else "_The ruins only had dust and bones for you._")
        )
        desc += f"\n-# Next scavenge in {dc.SCAVENGE_COOLDOWN_S // 60}m."
        final_embed = card(title, description=desc, color=color).build()
        if pre_msg is not None:
            try:
                await pre_msg.edit(embed=final_embed)
                return
            except Exception:
                log.debug("delve scavenge final edit failed", exc_info=True)
        await ctx.reply(embed=final_embed, mention_author=False)

    @delve.command(name="battle", aliases=["fight_wild", "wild"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_battle(self, ctx: DiscoContext) -> None:
        """Re-post the interactive wild-buddy battle view in this room.

        The room embed already includes a Challenge button -- this command
        is a fallback for players who'd rather type, and for repositioning
        the panel at the bottom of a busy channel. Posts a fresh embed +
        ``_DelveWildBuddyView`` (Challenge / Skip), same as walking into
        the room.

        Single-fight gate: acquires the per-user fight lock so the player
        can't have a buddy PvP, farm wild, and delve panel queued at the
        same time. The lock auto-clears after 8 minutes if the battle
        stalls; the Challenge button doesn't currently re-acquire so a
        Challenge press inside an already-stale window will just proceed.
        """
        # Single-fight blocker. We acquire here even though the actual
        # fight starts on Challenge-button press -- holding the lock
        # while the panel is posted prevents the player from opening a
        # second fight surface in another channel while this panel is
        # still up. TTL takes care of the case where the panel times
        # out without a Challenge press.
        from services.fight_lock import acquire as _fl_acquire, FightLockBusy
        _fl_res = await _fl_acquire(
            ctx.db, ctx.guild_id, ctx.author.id, "delve_wild",
        )
        if not _fl_res.acquired:
            await ctx.reply_error(str(FightLockBusy(_fl_res)))
            return

        state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        if str(state.get("current_room_type") or "") != "wild_battle":
            await ctx.reply_error(
                "No wild buddy in this room. Use `,delve next` to keep delving."
            )
            return
        payload = state.get("current_room_payload") or {}
        if isinstance(payload, str):
            import json as _json
            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        wild_buddy = (payload or {}).get("wild_buddy") or {}
        if not wild_buddy:
            await ctx.reply_error("Room state is corrupt. Try `,delve next`.")
            return

        embed = await self._build_room_embed(ctx, state)
        wb_view = _DelveWildBuddyView(self, ctx, wild_buddy)
        sent = await ctx.reply(embed=embed, view=wb_view, mention_author=False)
        wb_view.message = sent

    # ── Token economy: ORE -> RUNE burn-swap, RUNE -> USD cashout ──────────

    @staticmethod
    def _normalise_ore(arg: str) -> str | None:
        a = (arg or "").strip().upper()
        return a if a in dc.ORE_SYMBOLS else None

    @staticmethod
    def _parse_amount(arg: str) -> float:
        s = (arg or "").strip().lower().replace(",", "")
        if s in ("all", "max", "*"):
            return -1.0
        try:
            return float(s)
        except ValueError:
            raise ValueError(f"Bad amount: {arg!r}")

    @delve.command(name="swap", aliases=["burnswap"])
    async def delve_swap(
        self, ctx: DiscoContext, ore: str = "", amount: str = "",
    ) -> None:
        """Burn ore -> mint RUNE. Slippage applies to both oracles."""
        sym = self._normalise_ore(ore)
        if not sym:
            await ctx.reply_error_hint(
                "Pick COPPER, SILVER, or GOLD.",
                hint="delve swap copper 100",
            )
            return
        try:
            amt = self._parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if amt < 0:
            held_raw = await dsvc.get_ore_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id, sym,
            )
            if held_raw <= 0:
                await ctx.reply_error(f"You have no {sym} to swap.")
                return
            amt_raw = held_raw
        else:
            amt_raw = to_raw(amt)
        try:
            res = await dsvc.burn_ore_for_rune(
                ctx.db, ctx.guild_id, ctx.author.id, sym, amt_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        oracles = await _oracles(ctx)
        rune_h = to_human(res.rune_minted_raw)
        ore_h  = to_human(res.ore_burned_raw)
        desc = (
            f"Burned **{_fmt_ore(sym, ore_h)}** "
            f"-> minted **{_fmt_rune(rune_h)}**"
            f"{_with_usd(rune_h, oracles.get(dc.RUNE_SYMBOL, 0.0))}\n"
            f"-# {sym} oracle: ${res.ore_oracle_before:,.6f} -> ${res.ore_oracle_after:,.6f}\n"
            f"-# RUNE oracle: ${res.rune_oracle_before:,.6f} -> ${res.rune_oracle_after:,.6f}\n"
            f"-# Slippage: **{res.price_impact_pct * 100:.2f}%**"
        )
        if res.lp_reward_usd > 0:
            desc += f"\n-# Paid **{fmt_usd(res.lp_reward_usd)}** to LP holders."
        embed = card("\U0001F525  Burn-Swap", color=C_AMBER).description(desc).build()
        await ctx.reply(embed=embed, mention_author=False)

    @delve.command(name="cashout", aliases=["sellrune", "withdraw"])
    async def delve_cashout(self, ctx: DiscoContext, amount: str = "") -> None:
        """Burn RUNE -> credit your USD wallet at the live oracle minus impact."""
        try:
            amt = self._parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if amt < 0:
            held_raw = await dsvc.get_rune_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if held_raw <= 0:
                await ctx.reply_error("You have no RUNE to cash out.")
                return
            amt_raw = held_raw
        else:
            amt_raw = to_raw(amt)
        try:
            res = await dsvc.cashout_rune(
                ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # V3 Pillar 2: delver mastery XP scales with USD cashed out.
        try:
            from services import mastery as _mastery
            _xp = _mastery.xp_for_action(to_human(int(res.usd_credited_raw)))
            await _mastery.add_mastery(
                ctx.db, ctx.author.id, ctx.guild_id, "delver", _xp,
            )
        except Exception:
            pass
        from core.framework.staking import cashout_receipt
        await ctx.reply(
            embed=cashout_receipt(
                burned_symbol=dc.RUNE_SYMBOL, burned_emoji=dc.RUNE_EMOJI,
                burned_h=to_human(int(res.rune_burned_raw)),
                usd_credited_h=to_human(int(res.usd_credited_raw)),
                oracle_before=float(res.rune_oracle_before),
                oracle_after=float(res.rune_oracle_after),
                impact_pct=float(res.price_impact_pct),
                revenue_usd=float(res.revenue_usd or 0.0),
                lp_reward_usd=float(res.lp_reward_usd or 0.0),
            ),
            mention_author=False,
        )

    @delve.command(name="sell", aliases=["sellgear", "unequip_sell"])
    async def delve_sell(self, ctx: DiscoContext, *, item: str = "") -> None:
        """Sell delve gear, or bulk-dump salvage with ``sell all``.

        Usage:
          ,delve sell <weapon_key>   -- e.g.  ,delve sell iron_sword
          ,delve sell <armor_key>    -- e.g.  ,delve sell leather_vest
          ,delve sell all            -- bulk-sells JUNK SALVAGE only

        ``sell all`` will NOT sell crafting mats, usables, consumables,
        or weapons / armor. Those have to be sold individually so a
        bulk-sell never accidentally dumps a useful drop. Mats and
        usables: ``,delve junk sell <key>``. Gear: ``,delve sell <key>``.
        """
        key = (item or "").strip().lower().replace(" ", "_")
        if key in ("all", "*"):
            await self._delve_sell_all(ctx)
            return
        if not key:
            prefix = ctx.prefix or "."
            lines = []
            for k, meta in dc.WEAPONS.items():
                refund = float(dc.gear_sell_value(meta))
                if refund <= 0:
                    continue
                rdot = dc.rarity_dot(dc.item_rarity(meta))
                tag = " *(delve-only)*" if meta.get("delve_only") else ""
                lines.append(
                    f"{rdot} {meta.get('emoji', '')} `{k}` -- "
                    f"{meta['name']}{tag}  ({refund:,.2f} RUNE back)"
                )
            for k, meta in dc.ARMOR.items():
                refund = float(dc.gear_sell_value(meta))
                if refund <= 0:
                    continue
                rdot = dc.rarity_dot(dc.item_rarity(meta))
                tag = " *(delve-only)*" if meta.get("delve_only") else ""
                lines.append(
                    f"{rdot} {meta.get('emoji', '')} `{k}` -- "
                    f"{meta['name']}{tag}  ({refund:,.2f} RUNE back)"
                )
            await ctx.reply(
                embed=card(
                    f"{dc.RUNE_EMOJI} Sell Gear",
                    description=(
                        "Sell owned (unequipped) weapons or armor.\n"
                        "Shop-bought refunds 50% of price. Delve-only drops "
                        "refund a tier+rarity-derived value.\n\n"
                        + "\n".join(lines)
                    ),
                    color=C_AMBER,
                ).footer(f"Usage: {prefix}delve sell <key>").build(),
                mention_author=False,
            )
            return

        kind = None
        if key in dc.WEAPONS:
            kind = "weapon"
        elif key in dc.ARMOR:
            kind = "armor"
        else:
            all_keys = list(dc.WEAPONS) + list(dc.ARMOR)
            await ctx.reply_error(
                f"Unknown item `{key}`.\n"
                f"Run `,delve sell` (no args) to see sellable gear."
            )
            return

        try:
            refund, name = await dsvc.sell_gear(
                ctx.db, ctx.guild_id, ctx.author.id, kind, key,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        await ctx.reply_success(
            f"Sold **{name}** for **{refund:,.4f} {dc.RUNE_SYMBOL}** (50% back).",
            title=f"{dc.RUNE_EMOJI} Gear Sold",
        )

    @delve.command(name="stake", aliases=["lock", "stakes", "stakeinfo"])
    async def delve_stake(
        self, ctx: DiscoContext, ore: str = "", amount: str = "",
    ) -> None:
        """Stake ore for RUNE yield, or show the stake panel.

        ``,delve stake``                  -- show the unified stake panel
                                             (Stake / Unstake / Claim /
                                             Refresh buttons -- same shape
                                             as ,farm stake / ,craft stake
                                             / ,fish stake / ,buddy stake)
        ``,delve stake <ore> <amt|all>``  -- lock ore for passive RUNE yield
        ``,delve stake all``              -- stake the entire wallet of every ore
        """
        if not ore:
            await self._open_stake_panel(ctx)
            return
        if ore.strip().lower() in ("all", "*"):
            await self._delve_stake_all(ctx)
            return
        sym = self._normalise_ore(ore)
        if not sym:
            await ctx.reply_error_hint(
                "Pick COPPER, SILVER, or GOLD.",
                hint="delve stake copper 50",
            )
            return
        try:
            amt = self._parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if amt < 0:
            held_raw = await dsvc.get_ore_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id, sym,
            )
            if held_raw <= 0:
                await ctx.reply_error(f"You have no {sym} to stake.")
                return
            amt_raw = held_raw
        else:
            amt_raw = to_raw(amt)
        try:
            res = await dsvc.stake_ore(
                ctx.db, ctx.guild_id, ctx.author.id, sym, amt_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        from core.framework.staking import stake_receipt
        oracles = await _oracles(ctx)
        ore_emoji = {
            dc.COPPER_SYMBOL: dc.COPPER_EMOJI,
            dc.SILVER_SYMBOL: dc.SILVER_EMOJI,
            dc.GOLD_SYMBOL:   dc.GOLD_EMOJI,
        }.get(sym, "")
        await ctx.reply(
            embed=stake_receipt(
                action="Staked",
                stake_symbol=sym, stake_emoji=ore_emoji,
                delta_h=to_human(int(res.delta_raw)),
                total_h=to_human(int(res.staked_raw)),
                stake_oracle=oracles.get(sym, 0.0),
                note=(
                    f"Earns {dc.ORE_STAKE_RUNE_PER_DAY[sym]:g} RUNE per "
                    f"{sym} per day."
                ),
            ),
            mention_author=False,
        )

    @delve.command(name="unstake", aliases=["unlock"])
    async def delve_unstake(
        self, ctx: DiscoContext, ore: str = "", amount: str = "",
    ) -> None:
        """Unlock staked ore back to your wallet (also pays accrued RUNE).

        ``,delve unstake all`` pulls every staked ore type at once.
        """
        if ore.strip().lower() in ("all", "*"):
            await self._delve_unstake_all(ctx)
            return
        sym = self._normalise_ore(ore)
        if not sym:
            await ctx.reply_error_hint(
                "Pick COPPER, SILVER, or GOLD.",
                hint="delve unstake copper all",
            )
            return
        try:
            amt = self._parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        amt_raw = (2 ** 62) if amt < 0 else to_raw(amt)
        try:
            res = await dsvc.unstake_ore(
                ctx.db, ctx.guild_id, ctx.author.id, sym, amt_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        from core.framework.staking import stake_receipt
        oracles = await _oracles(ctx)
        ore_emoji = {
            dc.COPPER_SYMBOL: dc.COPPER_EMOJI,
            dc.SILVER_SYMBOL: dc.SILVER_EMOJI,
            dc.GOLD_SYMBOL:   dc.GOLD_EMOJI,
        }.get(sym, "")
        await ctx.reply(
            embed=stake_receipt(
                action="Unstaked",
                stake_symbol=sym, stake_emoji=ore_emoji,
                delta_h=to_human(abs(int(res.delta_raw))),
                total_h=to_human(int(res.staked_raw)),
                stake_oracle=oracles.get(sym, 0.0),
                yield_symbol=dc.RUNE_SYMBOL, yield_emoji=dc.RUNE_EMOJI,
                yield_paid_h=to_human(int(res.rune_yield_paid_raw)),
                yield_oracle=oracles.get(dc.RUNE_SYMBOL, 0.0),
            ),
            mention_author=False,
        )

    async def _delve_stake_all(self, ctx: DiscoContext) -> None:
        """Stake the entire wallet of every ore the player holds."""
        gid, uid = ctx.guild_id, ctx.author.id
        rows: list[str] = []
        staked_any = False
        for sym in dc.ORE_SYMBOLS:
            held_raw = int(
                await dsvc.get_ore_wallet_raw(ctx.db, gid, uid, sym) or 0
            )
            if held_raw <= 0:
                continue
            try:
                res = await dsvc.stake_ore(ctx.db, gid, uid, sym, held_raw)
            except ValueError as exc:
                rows.append(f"{sym} -- {exc}")
                continue
            staked_any = True
            rows.append(
                f"+{_fmt_ore(sym, to_human(int(res.delta_raw)))} staked  "
                f"·  total {_fmt_ore(sym, to_human(int(res.staked_raw)))}"
            )
        if not staked_any:
            await ctx.reply_error_hint(
                "Nothing to stake -- your COPPER / SILVER / GOLD wallet is empty.",
                hint="delve mine",
            )
            return
        await ctx.reply(
            embed=card(
                f"{dc.RUNE_EMOJI} Staked Everything",
                description="\n".join(rows),
                color=C_SUCCESS,
            ).footer(
                "Earns RUNE per ore per day. Use ,delve claim to collect."
            ).build(),
            mention_author=False,
        )

    async def _delve_unstake_all(self, ctx: DiscoContext) -> None:
        """Unstake every ore type at once. Pays accrued RUNE on the first hit."""
        gid, uid = ctx.guild_id, ctx.author.id
        rows: list[str] = []
        unstaked_any = False
        rune_paid_total = 0
        for sym in dc.ORE_SYMBOLS:
            try:
                res = await dsvc.unstake_ore(ctx.db, gid, uid, sym, 2 ** 62)
            except ValueError:
                continue
            unstaked_any = True
            rune_paid_total += int(res.rune_yield_paid_raw or 0)
            rows.append(
                f"+{_fmt_ore(sym, to_human(abs(int(res.delta_raw))))} returned to wallet"
            )
        if not unstaked_any:
            await ctx.reply_error("You have no ore staked.")
            return
        if rune_paid_total > 0:
            rows.append(f"+{_fmt_rune(to_human(rune_paid_total))} yield paid")
        await ctx.reply(
            embed=card(
                f"{dc.RUNE_EMOJI} Unstaked Everything",
                description="\n".join(rows),
                color=C_SUCCESS,
            ).build(),
            mention_author=False,
        )

    async def _delve_sell_all(self, ctx: DiscoContext) -> None:
        """Sell only the player's salvage-tier junk for RUNE.

        Crafting mats, usables, consumables, weapons and armor are
        deliberately NOT touched -- those are useful items the player
        chose to keep. ``,delve sell <key>`` sells gear individually;
        ``,delve junk sell <key>`` sells one stack of junk at a time.
        """
        gid, uid = ctx.guild_id, ctx.author.id
        try:
            total_rune, sold = await dsvc.sell_junk(
                ctx.db, gid, uid, salvage_only=True,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        # One line per stack so the player sees what got dumped.
        lines: list[str] = []
        for k, qty in sorted(
            sold.items(),
            key=lambda kv: -int(kv[1]),
        ):
            meta = dc.junk_meta(k) or {}
            rune_per = float(dc.effective_salvage_rune(meta))
            stack_rune = rune_per * int(qty)
            rdot = dc.rarity_dot(dc.item_rarity(meta))
            lines.append(
                f"{rdot} {meta.get('emoji', '')} {meta.get('name', k)}  "
                f"x{int(qty)}  +{_fmt_rune(stack_rune)}"
            )

        embed = (
            card(
                f"{dc.RUNE_EMOJI} Sold {len(sold)} Salvage Stacks",
                description=(
                    f"Total: **{_fmt_rune(total_rune)}**\n\n"
                    + "\n".join(lines)
                ),
                color=C_SUCCESS,
            )
            .footer(
                "Mats / usables / consumables / gear stay -- sell those "
                "individually with `,delve junk sell <key>` or `,delve sell <key>`."
            )
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    async def _open_stake_panel(self, ctx: DiscoContext) -> None:
        """Open the unified stake panel for COPPER/SILVER/GOLD -> RUNE."""
        from core.framework.staking import StakeAdapter, StakePanelView, StakeToken

        async def _state(c: DiscoContext) -> dict:
            uid, gid = c.author.id, c.guild_id
            state = await dsvc.ensure_state(c.db, gid, uid)
            pending_raw = int(
                await dsvc.accrued_stake_yield(c.db, gid, uid) or 0
            )
            oracles = await _oracles(c)
            wallets = {
                sym: int(
                    await dsvc.get_ore_wallet_raw(c.db, gid, uid, sym) or 0
                )
                for sym in dc.ORE_SYMBOLS
            }
            staked_by_sym = {
                dc.COPPER_SYMBOL: int(state.get("copper_staked_raw") or 0),
                dc.SILVER_SYMBOL: int(state.get("silver_staked_raw") or 0),
                dc.GOLD_SYMBOL:   int(state.get("gold_staked_raw")   or 0),
            }
            # Daily RUNE drip = sum(qty * rate_per_day) across the three
            # ores so the panel header matches what unstake/claim pay out.
            daily_rune_h = 0.0
            for sym, raw in staked_by_sym.items():
                daily_rune_h += (
                    to_human(raw) * float(dc.ORE_STAKE_RUNE_PER_DAY[sym])
                )
            return {
                "staked_by_sym": staked_by_sym,
                "wallet_by_sym": wallets,
                "stake_oracle_by_sym": {
                    sym: oracles.get(sym, 0.0) for sym in dc.ORE_SYMBOLS
                },
                "yield_oracle": oracles.get(dc.RUNE_SYMBOL, 0.0),
                "pending_raw": pending_raw,
                "daily_rate_raw": int(to_raw(daily_rune_h)),
            }

        async def _stake(c: DiscoContext, raw: int, sym: str) -> int:
            res = await dsvc.stake_ore(
                c.db, c.guild_id, c.author.id, sym, int(raw),
            )
            return int(res.staked_raw)

        async def _unstake(c: DiscoContext, raw: int, sym: str) -> int:
            res = await dsvc.unstake_ore(
                c.db, c.guild_id, c.author.id, sym, int(raw),
            )
            return int(res.staked_raw)

        async def _claim(c: DiscoContext) -> int:
            res = await dsvc.claim_stake_yield(
                c.db, c.guild_id, c.author.id,
            )
            return int(getattr(res, "rune_yield_paid_raw", 0) or 0)

        adapter = StakeAdapter(
            title="\U0001F5FA Delve Stakes (COPPER/SILVER/GOLD -> RUNE)",
            color=C_GOLD,
            stake_tokens=[
                StakeToken(dc.COPPER_SYMBOL, dc.COPPER_EMOJI),
                StakeToken(dc.SILVER_SYMBOL, dc.SILVER_EMOJI),
                StakeToken(dc.GOLD_SYMBOL,   dc.GOLD_EMOJI),
            ],
            yield_symbol=dc.RUNE_SYMBOL, yield_emoji=dc.RUNE_EMOJI,
            get_state=_state, do_stake=_stake,
            do_unstake=_unstake, do_claim=_claim,
            note=(
                f"Stake ore to drip RUNE. Yield rates: "
                f"COPPER {dc.ORE_STAKE_RUNE_PER_DAY[dc.COPPER_SYMBOL]:g} / "
                f"SILVER {dc.ORE_STAKE_RUNE_PER_DAY[dc.SILVER_SYMBOL]:g} / "
                f"GOLD {dc.ORE_STAKE_RUNE_PER_DAY[dc.GOLD_SYMBOL]:g} RUNE "
                f"per ore per day."
            ),
        )
        await StakePanelView.send(ctx, adapter)

    @delve.command(name="claim", aliases=["yield"])
    async def delve_claim(self, ctx: DiscoContext) -> None:
        """Pay out accrued RUNE yield. Stake stays locked."""
        try:
            res = await dsvc.claim_stake_yield(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        oracles = await _oracles(ctx)
        from core.framework.staking import claim_receipt
        await ctx.reply(
            embed=claim_receipt(
                yield_symbol=dc.RUNE_SYMBOL, yield_emoji=dc.RUNE_EMOJI,
                yield_paid_h=to_human(int(res.rune_yield_paid_raw)),
                yield_oracle=oracles.get(dc.RUNE_SYMBOL, 0.0),
                stake_symbol="ore",
                total_staked_h=to_human(int(res.staked_raw)),
            ),
            mention_author=False,
        )

    # ── Shop / inventory / party ───────────────────────────────────────────

    @delve.command(name="shop", aliases=["wares"])
    async def delve_shop(self, ctx: DiscoContext) -> None:
        """Browse the surface shop -- categorised dropdowns, clean stats.

        Defaults to weapons your class can equip, but a category dropdown
        switches the page to any of: Weapons (yours / all), Armor (yours
        / all), Healing, Buffs / Brews, Damage Scrolls, Ammo, or
        Utility consumables. Each entry shows tier, slot type, the raw
        ATK / DEF / heal % stat plus its blurb, all priced in RUNE.
        """
        view = _DelveShopView(ctx)
        embed = await view._build_embed()
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    @delve.command(name="buy", aliases=["purchase"])
    async def delve_buy(
        self, ctx: DiscoContext, kind: str = "", key: str = "",
        qty: int = 1,
    ) -> None:
        """Buy a ``weapon``, ``armor``, or ``consumable``.

        Consumables accept an optional quantity:
            ``,delve buy consumable arrow_bundle 5`` -- five bundles
        Weapons + armor are unique-owned and always qty=1.
        """
        kind = (kind or "").strip().lower()
        key = (key or "").strip().lower()
        try:
            qty = max(1, int(qty or 1))
        except (TypeError, ValueError):
            qty = 1
        if kind not in ("weapon", "armor", "consumable") or not key:
            await ctx.reply_error_hint(
                "Use `,delve buy weapon|armor|consumable <key> [qty]`.",
                hint="delve buy consumable arrow_bundle 5",
            )
            return
        try:
            res = await dsvc.buy_item(
                ctx.db, ctx.guild_id, ctx.author.id, kind, key, qty=qty,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        meta = (
            dc.weapon_meta(key) if kind == "weapon"
            else dc.armor_meta(key) if kind == "armor"
            else dc.consumable_meta(key)
        ) or {}
        slip = (
            f"\n-# RUNE oracle: ${res.oracle_before:,.6f} -> "
            f"${res.oracle_after:,.6f} (slippage {res.impact_pct * 100:.2f}%)"
            if res.impact_pct > 0 else ""
        )
        if kind == "consumable":
            pack_size = max(1, int((meta.get("pack_size") or 1)))
            granted = pack_size * qty
            qty_tag = (
                f" x{qty}" if qty > 1 else ""
            )
            unit_tag = (
                f" ({granted} units)" if pack_size > 1 else
                (f" ({granted} units)" if qty > 1 else "")
            )
            await ctx.reply_success(
                f"{meta.get('emoji', '')} **{meta.get('name', key)}**"
                f"{qty_tag}{unit_tag} acquired for "
                f"{_fmt_rune(res.price_rune_human)}.{slip}",
                title="Bought",
            )
        else:
            await ctx.reply_success(
                f"{meta.get('emoji', '')} **{meta.get('name', key)}** acquired for "
                f"{_fmt_rune(res.price_rune_human)}.{slip}",
                title="Bought",
            )

    @delve.command(name="equip", aliases=["wear"])
    async def delve_equip(
        self, ctx: DiscoContext, kind: str = "", key: str = "",
    ) -> None:
        """Equip a weapon or armor you own."""
        kind = (kind or "").strip().lower()
        key = (key or "").strip().lower()
        if kind not in ("weapon", "armor") or not key:
            await ctx.reply_error_hint(
                "Use `,delve equip weapon|armor <key>`.",
                hint="delve equip weapon iron_shortsword",
            )
            return
        try:
            await dsvc.equip_item(
                ctx.db, ctx.guild_id, ctx.author.id, kind, key,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        meta = (
            dc.weapon_meta(key) if kind == "weapon"
            else dc.armor_meta(key)
        ) or {}
        await ctx.reply_success(
            f"Equipped {meta.get('emoji', '')} **{meta.get('name', key)}**.",
            title="Geared up",
        )

    @delve.command(name="inv", aliases=["inventory", "bag"])
    async def delve_inv(self, ctx: DiscoContext) -> None:
        """Unified bag panel: weapons, armor, consumables, junk, ore balances.

        Sorted by rarity (legendary at the top, common at the bottom)
        within each category so the player's best items always read
        first. Mirrors the standalone ``,delve junk`` view -- that
        command still works for a junk-only deep-dive.
        """
        state = await dsvc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        oracles = await _oracles(ctx)
        holdings = await _gather_holdings(ctx, ctx.author.id)
        weapons = state.get("weapons_owned") or {}
        armor = state.get("armor_owned") or {}
        cons = state.get("consumables") or {}
        junk = state.get("junk_inventory") or {}
        eq_w = state.get("equipped_weapon") or ""
        eq_a = state.get("equipped_armor") or ""

        def _wline(k: str) -> str:
            m = dc.weapon_meta(k) or {}
            star = " *(equipped)*" if k == eq_w else ""
            atk = dc.effective_atk_bonus(m)
            rdot = dc.rarity_dot(dc.item_rarity(m))
            rlbl = dc.rarity_label(dc.item_rarity(m))
            tail = _fmt_affix_tail(m)
            return (
                f"{rdot} `{k}` -- *{rlbl}*  T{m.get('tier', 0)}  "
                f"+{atk} ATK{tail}{star}"
            )

        def _aline(k: str) -> str:
            m = dc.armor_meta(k) or {}
            star = " *(equipped)*" if k == eq_a else ""
            df = dc.effective_def_bonus(m)
            rdot = dc.rarity_dot(dc.item_rarity(m))
            rlbl = dc.rarity_label(dc.item_rarity(m))
            tail = _fmt_affix_tail(m)
            return (
                f"{rdot} `{k}` -- *{rlbl}*  T{m.get('tier', 0)}  "
                f"+{df} DEF{tail}{star}"
            )

        # Sort gear by rarity (legendaries first), then by tier desc.
        def _gear_sort_key(k: str, catalog: dict) -> tuple:
            m = catalog.get(k) or {}
            return (
                -dc.RARITY_RANK.get(dc.item_rarity(m), 0),
                -int(m.get("tier") or 0),
                k,
            )

        weapons_sorted = sorted(
            (k for k in weapons),
            key=lambda k: _gear_sort_key(k, dc.WEAPONS),
        )
        armor_sorted = sorted(
            (k for k in armor),
            key=lambda k: _gear_sort_key(k, dc.ARMOR),
        )

        cons_lines = [
            f"x{int(qty):>3}  {(dc.consumable_meta(k) or {}).get('emoji', '')} "
            f"`{k}` -- {(dc.consumable_meta(k) or {}).get('blurb', '')}"
            for k, qty in cons.items() if int(qty) > 0
        ] or ["_(empty)_"]

        # Junk section: rarity-sorted, with per-stack salvage value so
        # the player can decide what to dump vs keep.
        junk_lines: list[str] = []
        total_junk_rune = 0.0
        for k, qty in sorted(
            ((k, int(q)) for k, q in junk.items() if int(q or 0) > 0),
            key=lambda kv: (
                -dc.RARITY_RANK.get(dc.item_rarity(dc.junk_meta(kv[0])), 0),
                -float(dc.effective_salvage_rune(dc.junk_meta(kv[0]))),
                kv[0],
            ),
        ):
            m = dc.junk_meta(k) or {}
            rune_per = float(dc.effective_salvage_rune(m))
            stack = rune_per * qty
            total_junk_rune += stack
            kind_tag = str(m.get("kind") or "salvage").title()
            rdot = dc.rarity_dot(dc.item_rarity(m))
            junk_lines.append(
                f"{rdot} `{k}` -- *{kind_tag}*  x{qty}  "
                f"({_fmt_rune(stack)})"
            )

        builder = (
            card("\U0001F392  Bag", color=C_NAVY)
            .field(
                "Weapons",
                "\n".join(_wline(k) for k in weapons_sorted) or "_(none)_",
                False,
            )
            .field(
                "Armor",
                "\n".join(_aline(k) for k in armor_sorted) or "_(none)_",
                False,
            )
            .field("Consumables", "\n".join(cons_lines), False)
        )
        if junk_lines:
            # Chunk junk lines into 1024-cap-safe sub-fields so a deep
            # delver's overflow doesn't silently truncate.
            buf = ""
            idx = 0
            for ln in junk_lines:
                sep = "\n" if buf else ""
                if buf and len(buf) + len(sep) + len(ln) > 1000:
                    title = (
                        f"Junk -- bulk sell {_fmt_rune(total_junk_rune)}"
                        if idx == 0 else "Junk (cont)"
                    )
                    builder.field(title, buf, False)
                    buf = ln
                    idx += 1
                else:
                    buf += sep + ln
            if buf:
                title = (
                    f"Junk -- bulk sell {_fmt_rune(total_junk_rune)}"
                    if idx == 0 else "Junk (cont)"
                )
                builder.field(title, buf, False)
        else:
            builder.field("Junk", "_(none)_", False)
        builder.field(
            "Wallet", "\n".join(_balance_lines(holdings, oracles)), False,
        ).footer(
            "`,delve sell all` dumps salvage only -- mats / usables / gear "
            "stay. `,delve junk sell <key>` for stacks."
        )
        await ctx.reply(embed=builder.build(), mention_author=False)

    @delve.command(name="party", aliases=["pets", "buddies"])
    async def delve_party(self, ctx: DiscoContext) -> None:
        """Show your captured buddies."""
        roster = await dsvc.list_party(ctx.db, ctx.guild_id, ctx.author.id)
        state = await dsvc.list_state(ctx.db, ctx.guild_id, ctx.author.id)
        active_id = state.get("active_buddy_id")
        if not roster:
            await ctx.reply(
                embed=card(
                    "\U0001F43E  Party",
                    description="You haven't captured any buddies yet. "
                                "Bring a mob below 30% HP and run `,delve capture`.",
                    color=C_NEUTRAL,
                ).build(),
                mention_author=False,
            )
            return
        lines = []
        for r in roster:
            sm = dc.mob_meta(r.get("species_key") or "") or {}
            star = " *(active)*" if r.get("party_id") == active_id else ""
            lines.append(
                f"`#{r.get('party_id')}` {sm.get('emoji', '')} "
                f"**{r.get('name') or sm.get('name', r.get('species_key'))}** "
                f"-- T{sm.get('tier', '?')}  Lv.{r.get('level', 1)}  "
                f"W{r.get('wins', 0)}/L{r.get('losses', 0)}{star}"
            )
        embed = (
            card("\U0001F43E  Party", color=C_PURPLE)
            .description("\n".join(lines))
            .footer(
                f"{len(roster)}/{dc.MAX_PARTY_SIZE} slots used.  "
                f"`,delve summon <id>` to set active."
            )
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @delve.command(name="summon", aliases=["activate"])
    async def delve_summon(self, ctx: DiscoContext, party_id: str = "") -> None:
        """Set a captured buddy as your active assist (or `none` to clear)."""
        s = (party_id or "").strip().lower()
        target: int | None = None
        if s in ("none", "off", "clear", ""):
            target = None
        else:
            try:
                target = int(s.lstrip("#"))
            except ValueError:
                await ctx.reply_error("Pass a numeric party id, or `none`.")
                return
        try:
            row = await dsvc.set_active_buddy(
                ctx.db, ctx.guild_id, ctx.author.id, target,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if target is None:
            await ctx.reply_success("Cleared active buddy.", title="Buddy")
            return
        sm = dc.mob_meta((row or {}).get("species_key") or "") or {}
        await ctx.reply_success(
            f"{sm.get('emoji', '')} **{(row or {}).get('name') or sm.get('name', '?')}** is now your active assist.",
            title="Buddy summoned",
        )

    @delve.command(name="release", aliases=["free"])
    async def delve_release(self, ctx: DiscoContext, party_id: str = "") -> None:
        """Release a captured buddy. Slot becomes free."""
        try:
            pid = int((party_id or "").lstrip("#"))
        except ValueError:
            await ctx.reply_error("Pass a numeric party id.")
            return
        ok = await dsvc.release_buddy(ctx.db, ctx.guild_id, ctx.author.id, pid)
        if not ok:
            await ctx.reply_error("That buddy isn't yours, or already released.")
            return
        await ctx.reply_success(f"Released party #{pid}.", title="Released")

    @delve.command(name="pray", aliases=["shrine", "kneel", "offer"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_pray(self, ctx: DiscoContext) -> None:
        """Activate the shrine in this room. Random boon: heal, RUNE,
        ATK / SPD blessing, free relic, or a small curse with a debt
        that pays off on the next chest. One-shot per shrine."""
        gid, uid = ctx.guild_id, ctx.author.id
        # Pre-frame for the kneeling beat, edited in place after the
        # outcome resolves. Same send-then-edit pattern as ,fish dig.
        pre_msg = None
        try:
            pre_msg = await ctx.reply(
                embed=card(
                    "\U0001F64F Approaching the shrine...",
                    description=f"```\n{dc.FRAMES.get('shrine_pray', '')}\n```",
                    color=C_TEAL,
                ).build(),
                mention_author=False,
            )
            await asyncio.sleep(0.8)
        except Exception:
            log.debug("delve pray pre-frame send failed", exc_info=True)
        try:
            res = await dsvc.pray_at_shrine(ctx.db, gid, uid)
        except ValueError as exc:
            if pre_msg is not None:
                try:
                    await pre_msg.edit(
                        embed=card(description=str(exc), color=C_AMBER).build(),
                    )
                    return
                except Exception:
                    pass
            await ctx.reply_error(str(exc))
            return
        oracles = await _oracles(ctx)
        # Frame + color depend on whether the shrine blessed or bit.
        if res.outcome_key == "shrine_curse":
            frame = dc.FRAMES.get("shrine_curse", "")
            color = C_PURPLE
        else:
            frame = dc.FRAMES.get("shrine_blessing", "")
            color = C_GOLD if res.outcome_key == "relic_gift" else C_TEAL
        # Build receipt bullets. Single switch on outcome -- matches the
        # forage / dig render style.
        detail_lines: list[str] = [f"_{res.blurb}_"]
        if res.hp_delta > 0:
            detail_lines.append(f"\U00002764\U0000FE0F **+{res.hp_delta}** HP restored.")
        elif res.hp_delta < 0:
            detail_lines.append(f"\U0001F494 **{res.hp_delta}** HP (a debt accrues).")
        if res.rune_credited > 0:
            detail_lines.append(
                f"\U0001F4B0 +**{_fmt_rune(res.rune_credited)}**"
                f"{_with_usd(res.rune_credited, oracles.get(dc.RUNE_SYMBOL, 0.0))}"
            )
        if res.buff_key:
            label = {
                "shrine_atk": "Smiting Blessing (+%d%% ATK, %d rounds)",
                "shrine_spd": "Swift Blessing (+%d%% SPD, %d rounds)",
            }.get(res.buff_key, res.buff_key + " (+%d%%, %d rounds)")
            detail_lines.append(
                f"\U0001F4AB " + label % (int(res.buff_value * 100), res.buff_duration)
            )
        if res.relic_key:
            rmeta = dc.relic_meta(res.relic_key) or {}
            detail_lines.append(
                f"{rmeta.get('emoji', '')} **Relic gift:** {rmeta.get('name', res.relic_key)} "
                f"({str(rmeta.get('rarity', 'common')).title()}) -- _{rmeta.get('blurb', '')}_"
            )
        title = f"\U0001F64F {res.boon_name}"
        desc = f"```\n{frame}\n```\n" + "\n".join(detail_lines)
        final_embed = card(title, description=desc, color=color).build()
        if pre_msg is not None:
            try:
                await pre_msg.edit(embed=final_embed)
                return
            except Exception:
                log.debug("delve pray final edit failed", exc_info=True)
        await ctx.reply(embed=final_embed, mention_author=False)

    @delve.command(name="junk", aliases=["salvage", "scraps", "trash"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_junk(
        self, ctx: DiscoContext, action: str | None = None, key: str | None = None,
    ) -> None:
        """View, use, or sell salvage / craft mat / usable drops.

        ``,delve junk``                -- list everything you've collected
        ``,delve junk use <key>``      -- consume a usable junk item
        ``,delve junk sell <key>``     -- sell one specific junk type for RUNE
        ``,delve junk sell all``       -- dump the whole pile for RUNE

        Junk drops fire on combat wins, chest opens, and mining --
        salvage trash, craft mats, and the occasional usable item like
        Healing Herb or Smoke Bomb. Sells go straight to your RUNE
        wallet at the per-type ``salvage_rune`` price.
        """
        gid, uid = ctx.guild_id, ctx.author.id
        act = (action or "").strip().lower()
        if act in ("use", "consume", "drink"):
            if not key:
                await ctx.reply_error("Tell me which junk. `,delve junk use <key>`.")
                return
            try:
                res = await dsvc.use_junk_item(ctx.db, gid, uid, key.lower())
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            meta = dc.junk_meta(res.key) or {}
            await ctx.reply_success(
                f"{meta.get('emoji', '')} **{meta.get('name', res.key)}** used.\n"
                f"_{res.detail}_",
                title="\U0001F392 Junk used",
            )
            return
        if act in ("sell", "dump", "burn"):
            sell_key: str | None = None
            sell_salvage_only = False
            if key and key.lower() not in ("all", "everything", "every"):
                sell_key = key.lower()
            else:
                # Bulk sell only dumps salvage; mats and usables are
                # protected so the player keeps anything craftable or
                # in-run useful. Single-key sells still let the player
                # off-load specific stacks (mats included).
                sell_salvage_only = True
            try:
                rune_h, sold = await dsvc.sell_junk(
                    ctx.db, gid, uid, sell_key,
                    salvage_only=sell_salvage_only,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            oracles = await _oracles(ctx)
            lines = []
            for k, qty in sold.items():
                m = dc.junk_meta(k) or {}
                lines.append(
                    f"{m.get('emoji', '')} **{m.get('name', k)}** x{qty}"
                )
            await ctx.reply_success(
                "\n".join(lines)
                + f"\n\n+**{_fmt_rune(rune_h)}**"
                + _with_usd(rune_h, oracles.get(dc.RUNE_SYMBOL, 0.0)),
                title="\U0001F392 Junk sold",
            )
            return
        # Default panel: list everything owned
        state = await dsvc.ensure_state(ctx.db, gid, uid)
        junk_inv = state.get("junk_inventory") or {}
        if isinstance(junk_inv, str):
            try:
                import json as _json
                junk_inv = _json.loads(junk_inv) if junk_inv else {}
            except Exception:
                junk_inv = {}
        await ctx.reply(
            embed=_junk_panel_embed(ctx.author, junk_inv),
            mention_author=False,
        )

    @delve.command(name="relic", aliases=["relics"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_relic(
        self, ctx: DiscoContext, action: str | None = None, key: str | None = None,
    ) -> None:
        """View, equip, or unequip a relic.

        ``,delve relic``                 -- list owned relics + the equipped one
        ``,delve relic equip <key>``     -- equip an owned relic
        ``,delve relic unequip``         -- clear the equipped relic
        ``,delve relic info <key>``      -- look up any relic in the catalog
        """
        gid, uid = ctx.guild_id, ctx.author.id
        act = (action or "").strip().lower()
        if act in ("equip", "wear", "use"):
            if not key:
                await ctx.reply_error("Tell me which relic. `,delve relic equip <key>`.")
                return
            try:
                res = await dsvc.equip_relic(ctx.db, gid, uid, key.lower())
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            meta = dc.relic_meta(res.equipped_key) or {}
            await ctx.reply_success(
                f"{meta.get('emoji', '')} **{meta.get('name', res.equipped_key)}** equipped.\n"
                f"_{meta.get('blurb', '')}_",
                title="\U0001F48E Relic equipped",
            )
            return
        if act in ("unequip", "remove", "off", "clear", "none"):
            res = await dsvc.equip_relic(ctx.db, gid, uid, None)
            await ctx.reply_success(
                "Relic unequipped.", title="\U0001F48E Relic cleared",
            )
            return
        if act == "info":
            if not key:
                await ctx.reply_error("Which relic? `,delve relic info <key>`.")
                return
            meta = dc.relic_meta(key.lower())
            if not meta:
                await ctx.reply_error(f"Unknown relic: `{key}`.")
                return
            await ctx.reply(
                embed=_relic_info_embed(meta),
                mention_author=False,
            )
            return
        # Default panel: own + equipped
        owned, equipped = await dsvc.list_relics(ctx.db, gid, uid)
        await ctx.reply(
            embed=_relics_panel_embed(ctx.author, owned, equipped),
            mention_author=False,
        )

    @delve.command(name="curse", aliases=["curses"])
    @user_cooldown(dc.ACTION_COOLDOWN_S)
    async def delve_curse(
        self, ctx: DiscoContext, action: str | None = None, key: str | None = None,
    ) -> None:
        """List, set, or clear an opt-in run curse modifier.

        ``,delve curse``                -- list available curses + your active one
        ``,delve curse set <key>``      -- arm a curse for your next delve
        ``,delve curse clear``          -- drop the active curse before starting
        """
        gid, uid = ctx.guild_id, ctx.author.id
        act = (action or "").strip().lower()
        if act in ("set", "use", "arm"):
            if not key:
                await ctx.reply_error("Tell me which curse. `,delve curse set <key>`.")
                return
            try:
                res = await dsvc.set_run_curse(ctx.db, gid, uid, key.lower())
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            meta = dc.curse_meta(res.curse_key) or {}
            rune_pct  = (float(meta.get("rune_mult", 1.0))  - 1.0) * 100
            chest_pct = (float(meta.get("chest_mult", 1.0)) - 1.0) * 100
            ore_pct   = (float(meta.get("ore_mult", 1.0))   - 1.0) * 100
            embed = card(
                f"\U0001F480 {meta.get('emoji', '')} {meta.get('name', res.curse_key)} armed",
                color=C_PURPLE,
                description=(
                    _frame_block("curse_armed")
                    + f"\n_{meta.get('blurb', '')}_\n\n"
                    + f"+{rune_pct:.0f}% RUNE, +{ore_pct:.0f}% ore, +{chest_pct:.0f}% chests "
                    + "for the next run.\n"
                    + "Run `,delve start` to enter the cursed delve."
                ),
            ).build()
            await ctx.reply(embed=embed, mention_author=False)
            return
        if act in ("clear", "none", "off", "remove", "drop"):
            try:
                await dsvc.set_run_curse(ctx.db, gid, uid, None)
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            await ctx.reply_success(
                "Curse cleared. Next delve runs at standard difficulty.",
                title="\U0001F480 Curse cleared",
            )
            return
        # Default panel: list curses + active
        state = await dsvc.ensure_state(ctx.db, gid, uid)
        active = state.get("run_curse") or None
        await ctx.reply(
            embed=_curses_panel_embed(ctx.author, active),
            mention_author=False,
        )

    @delve.command(name="stats", aliases=["panel"])
    async def delve_stats(
        self, ctx: DiscoContext, member: discord.Member | None = None,
    ) -> None:
        """Show your delver panel (or another member's)."""
        target = member or ctx.author
        state = await dsvc.ensure_state(ctx.db, ctx.guild_id, target.id)
        oracles = await _oracles(ctx)
        holdings = await _gather_holdings(ctx, target.id)
        # ,delve stats is the panel command. Bare ,delve already renders
        # the active room when there is a run, so this surface stays
        # purely the stats panel even mid-delve.
        await self._render_stats_embed(ctx, state, oracles, holdings)

    @delve.command(name="lb", aliases=["leaderboard", "top"])
    async def delve_lb(self, ctx: DiscoContext) -> None:
        """Top delvers in this server, sorted by deepest floor."""
        rows = await dsvc.get_top_delvers(ctx.db, ctx.guild_id, limit=50)
        if rows:
            from core.framework.leaderboard import filter_lb_user_ids
            keep = await filter_lb_user_ids(
                ctx, [int(r.get("user_id") or 0) for r in rows],
            )
            rows = [r for r in rows if int(r.get("user_id") or 0) in keep][:10]
        if not rows:
            await ctx.reply(
                embed=card(
                    "\U0001F3C6  Delver Board",
                    description="Nobody has reached a floor yet. Be the first.",
                    color=C_NEUTRAL,
                ).build(),
                mention_author=False,
            )
            return
        lines = []
        for i, r in enumerate(rows, start=1):
            cmeta = dc.class_meta(r.get("class_key") or "") or {}
            uid = int(r.get("user_id") or 0)
            lines.append(
                f"`#{i:>2}`  {cmeta.get('emoji', '')} <@{uid}>  "
                f"-- F**{r.get('deepest_floor', 0)}**  Lv.{r.get('level', 1)}  "
                f"K{r.get('total_kills', 0)} / Tame{r.get('total_captures', 0)} "
                f"/ Boss{r.get('bosses_slain', 0)}"
            )
        embed = card(
            "\U0001F3C6  Deepest Delvers", color=C_GOLD,
        ).description("\n".join(lines)).build()
        await ctx.reply(embed=embed, mention_author=False)

    @delve.command(name="help", aliases=["commands"])
    async def delve_help(self, ctx: DiscoContext) -> None:
        """Quick command reference."""
        body = (
            "**Run**\n"
            "`,delve start` `,delve next` `,delve descend` `,delve rest`\n"
            "**Combat**\n"
            "`,delve attack` `,delve skill` `,delve flee` `,delve capture` "
            "`,delve use <item>`\n"
            "**Mining + chest + shrine**\n"
            "`,delve mine` `,delve open` `,delve pray`\n"
            "**Scavenge** (free roll outside a delve)\n"
            "`,delve scavenge` -- wander surface ruins for RUNE/ORE, "
            "consumables, an Escape Scroll, or a rare Relic Shard "
            f"({dc.SCAVENGE_COOLDOWN_S // 60}m cooldown)\n"
            "**Junk drops**\n"
            "`,delve junk` -- list  ·  `,delve junk use <key>` / `,delve junk sell [key|all]`\n"
            "**Relics + curses**\n"
            "`,delve relic` / `,delve relic equip <key>`\n"
            "`,delve curse` / `,delve curse set <key>` (arm before `,delve start`)\n"
            "**Shop + gear**\n"
            "`,delve shop` `,delve buy <kind> <key>` `,delve equip <kind> <key>`\n"
            "**Party**\n"
            "`,delve party` `,delve summon <id|none>` `,delve release <id>`\n"
            "**Token economy**\n"
            "`,delve swap <ore> <amt>`  `,delve stake <ore> <amt>`  "
            "`,delve unstake <ore> <amt>`\n"
            "`,delve claim`  `,delve cashout <amt>`\n"
            "**Info**\n"
            "`,delve` `,delve stats [@u]` `,delve inv` `,delve lb`\n"
            "**Class + stats**\n"
            "`,delve class warrior|mage|rogue` (one-time)  ·  "
            "`,delve reroll <class>` (paid switch)\n"
            "`,delve upgrade` -- spend earned stat points  ·  "
            "`,delve respec` -- refund every spent point (USD fee, doubles per respec)"
        )
        embed = card(
            "\U0001F5FA  Delve Commands", color=C_INFO,
        ).description(body).build()
        await ctx.reply(embed=embed, mention_author=False)


    # ── Delve Arena PvP ────────────────────────────────────────────────────
    #
    # New `,delve arena` sub-group: async ranked matchmaking + live duels
    # against another player. Combat reads each player's existing delve
    # combat profile (class / abilities / weapon / armor / relic /
    # allocs), so there's no separate "arena gear" to tune. Rewards land
    # in copper / silver / gold ore or RUNE depending on the winner's
    # rank band, with division scaling on top of player level.

    @delve.group(
        name="arena",
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def delve_arena(self, ctx: DiscoContext) -> None:
        """Show the arena panel: rank, ELO, season window, leaderboard hook."""
        from services import delve_arena as da
        try:
            season = await da.current_season(ctx.db)
            rank = await da.get_rank(ctx.db, ctx.guild_id, ctx.author.id)
        except Exception:
            log.exception("delve arena: panel lookup failed")
            await ctx.reply_error("Arena couldn't load right now. Try again in a moment.")
            return
        title_band = rank.rank_key.title()
        roman = ("I", "II", "III", "IV", "V")[max(0, min(4, rank.division - 1))]
        body = (
            f"**Rank**: {title_band} {roman}  ({rank.elo} ELO, peak {rank.peak_elo})\n"
            f"**Record**: {rank.wins}-{rank.losses}  ·  streak {rank.streak}\n"
            f"**Season**: ends {fmt_ts(season.get('end_ts'))}\n\n"
            "`,delve arena fight` -- queue a ranked async match\n"
            "`,delve arena duel @user [unranked]` -- challenge a player live\n"
            "`,delve arena leaderboard` -- top 25\n"
            "`,delve arena profile [@user]` -- inspect a player\n"
        )
        embed = card(
            f"\U0001F3DF Delve Arena", color=C_PURPLE,
        ).description(body).footer(
            f"Win to climb. Copper -> Silver -> Gold -> Rune. Cooldown "
            f"{da.ASYNC_COOLDOWN_S // 60}m between ranked fights."
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @delve_arena.command(name="fight")
    async def delve_arena_fight(self, ctx: DiscoContext) -> None:
        """Queue a ranked async match. Combat resolves and replays in place."""
        from services import delve_arena as da
        try:
            p1, p2, replay, settlement = await da.queue_match(
                ctx.db, ctx.guild_id, ctx.author.id,
                name=str(ctx.author.display_name)[:32],
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        except Exception:
            log.exception("delve arena: queue_match failed")
            await ctx.reply_error("Arena matchmaking broke. Try again shortly.")
            return
        await self._render_arena_replay(ctx, p1, p2, replay, settlement)

    async def _fan_out_arena(
        self, ctx: DiscoContext, settlement, *, perspective_uid: int,
    ) -> None:
        """Fire achievement / quest / challenge triggers for an arena
        match from ``perspective_uid``'s side."""
        if not settlement.ranked:
            return
        is_win = (settlement.winner_uid == perspective_uid)
        triggers: list[tuple[str, int]] = []
        if is_win:
            triggers.append(("delve_arena_win", 1))
            if settlement.flawless:
                triggers.append(("delve_arena_flawless", 1))
            new_rank = (
                settlement.p1_rank_after if perspective_uid == settlement.winner_uid
                else settlement.p2_rank_after
            )
            old_rank = (
                settlement.p1_rank_before if perspective_uid == settlement.winner_uid
                else settlement.p2_rank_before
            )
            if new_rank.rank_key != old_rank.rank_key:
                triggers.append((f"delve_arena_rank_{new_rank.rank_key}", 1))
            # Streak triggers -- read live so we don't double-fire.
            try:
                from services import delve_arena as da
                rank_now = await da.get_rank(ctx.db, ctx.guild_id, perspective_uid)
                if rank_now.streak >= 10:
                    triggers.append(("delve_arena_streak_10", 1))
                if rank_now.streak >= 3:
                    triggers.append(("delve_arena_streak_3", 1))
            except Exception:
                log.debug("arena: streak lookup failed", exc_info=True)
        else:
            triggers.append(("delve_arena_loss", 1))
        for trig, amt in triggers:
            try:
                await self._fan_out(perspective_uid, ctx.guild_id, trig, amount=amt)
            except Exception:
                log.debug("arena: fan_out %s failed", trig, exc_info=True)

    async def _render_arena_replay(
        self, ctx: DiscoContext, p1, p2, replay, settlement,
    ) -> None:
        """Send a final frame for an arena async replay."""
        from services.delve_arena_render import render_arena_frame
        await self._fan_out_arena(ctx, settlement, perspective_uid=p1.uid)
        if replay.rounds:
            last = replay.rounds[-1]
            p1_hp = last.p1_hp_after
            p2_hp = last.p2_hp_after
        else:
            p1_hp = p1.hp_max
            p2_hp = p2.hp_max
        winner_name = (
            p1.name if settlement.winner_uid == p1.uid
            else (p2.name if settlement.winner_uid == p2.uid else "Draw")
        )
        banner = f"WIN: {winner_name}"
        if settlement.flawless:
            banner += "  FLAWLESS"
        frame = render_arena_frame(
            p1=p1, p2=p2, p1_hp=p1_hp, p2_hp=p2_hp,
            round_num=len(replay.rounds), max_rounds=25,
            action_banner=banner,
            rank_key=settlement.p1_rank_after.rank_key,
            division=settlement.p1_rank_after.division,
            elo=settlement.p1_rank_after.elo,
        )
        # Last few log lines from the replay so the player sees the
        # decisive blows without us streaming every round.
        log_tail: list[str] = []
        for ev in replay.rounds[-4:]:
            for ln in (ev.p1_log + ev.p2_log)[-2:]:
                log_tail.append(ln)
        reward_line = "_No rewards this match._"
        if settlement.reward_symbol and settlement.reward_qty_human > 0:
            reward_line = (
                f"\U0001F381 +{settlement.reward_qty_human:.4f} "
                f"{settlement.reward_symbol}"
            )
        delta = settlement.p1_elo_after - settlement.p1_elo_before
        delta_str = f"{'+' if delta >= 0 else ''}{delta}"
        body = (
            f"```\n{frame}\n```\n"
            + ("\n".join(log_tail) if log_tail else "_No log_")
            + f"\n\n**ELO**: {settlement.p1_elo_before} -> "
              f"{settlement.p1_elo_after} ({delta_str})\n"
            + reward_line
        )
        color = (
            C_SUCCESS if settlement.winner_uid == p1.uid
            else (C_AMBER if settlement.winner_uid is None else C_CRIMSON)
        )
        embed = card(
            f"\U0001F3DF Arena vs {p2.name}", color=color,
        ).description(body).footer(
            f"Match #{settlement.match_id}  ·  Rounds {settlement.rounds}"
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @delve_arena.command(name="leaderboard")
    async def delve_arena_leaderboard(self, ctx: DiscoContext) -> None:
        from services import delve_arena as da
        try:
            rows = await da.list_leaderboard(ctx.db, ctx.guild_id, limit=25)
        except Exception:
            log.exception("delve arena: leaderboard fetch failed")
            await ctx.reply_error("Leaderboard unavailable right now.")
            return
        if not rows:
            await ctx.reply_error("No arena fights yet this season.")
            return
        body_lines: list[str] = []
        for i, r in enumerate(rows, start=1):
            elo = int(r.get("elo") or 0)
            rk = da.rank_from_elo(elo)
            roman = ("I", "II", "III", "IV", "V")[max(0, min(4, rk.division - 1))]
            body_lines.append(
                f"**{i}.** <@{int(r['user_id'])}>  ·  "
                f"{rk.rank_key.title()} {roman}  ·  {elo} ELO  ·  "
                f"{int(r.get('wins') or 0)}-{int(r.get('losses') or 0)}"
            )
        embed = card(
            "\U0001F3C6 Delve Arena Leaderboard", color=C_GOLD,
        ).description("\n".join(body_lines[:25])).build()
        await ctx.reply(embed=embed, mention_author=False)

    @delve_arena.command(name="profile")
    async def delve_arena_profile(
        self, ctx: DiscoContext, target: discord.Member | None = None,
    ) -> None:
        from services import delve_arena as da
        who = target or ctx.author
        try:
            rank = await da.get_rank(ctx.db, ctx.guild_id, who.id)
        except Exception:
            log.exception("delve arena: profile fetch failed")
            await ctx.reply_error("Profile unavailable.")
            return
        roman = ("I", "II", "III", "IV", "V")[max(0, min(4, rank.division - 1))]
        body = (
            f"**Rank**: {rank.rank_key.title()} {roman}\n"
            f"**ELO**: {rank.elo}  (peak {rank.peak_elo})\n"
            f"**Record**: {rank.wins}-{rank.losses}\n"
            f"**Streak**: {rank.streak} (best {rank.streak if rank.streak else 0})"
        )
        embed = card(
            f"\U0001F3DF Arena Profile -- {who.display_name}",
            color=C_PURPLE,
        ).description(body).build()
        await ctx.reply(embed=embed, mention_author=False)

    @delve_arena.command(name="duel")
    async def delve_arena_duel(
        self,
        ctx: DiscoContext,
        target: discord.Member,
        ranked_flag: str = "",
    ) -> None:
        """Challenge a player to a live duel. Ranked unless 'unranked' is given."""
        from services import delve_arena as da
        if target.bot or target.id == ctx.author.id:
            await ctx.reply_error("Pick a real human opponent.")
            return
        ranked = (str(ranked_flag).lower() != "unranked")
        try:
            invite = await da.open_duel(
                ctx.db, ctx.guild_id, ctx.author.id, target.id, ranked=ranked,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        except Exception:
            log.exception("delve arena: open_duel failed")
            await ctx.reply_error("Couldn't send the invite. Try again shortly.")
            return
        view = _ArenaDuelInviteView(self, ctx, invite, target)
        flavor = "Ranked duel" if ranked else "Unranked duel"
        body = (
            f"{ctx.author.mention} challenges {target.mention} to a "
            f"{flavor}! {target.mention} has "
            f"{da.DUEL_INVITE_TTL_S}s to accept."
        )
        embed = card(
            "\U00002694 Arena Duel Invite", color=C_PURPLE,
        ).description(body).footer(
            "The challenger's gear is locked at invite time."
        ).build()
        sent = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = sent


# ============================================================================
# Arena duel views
# ============================================================================

class _ArenaDuelInviteView(discord.ui.View):
    """Accept / Decline buttons for a delve arena duel invite."""

    def __init__(
        self, cog: "Dungeon", ctx: DiscoContext,
        invite, target: discord.Member,
    ) -> None:
        from services import delve_arena as da
        super().__init__(timeout=float(da.DUEL_INVITE_TTL_S))
        self.cog = cog
        self.ctx = ctx
        self.invite = invite
        self.target = target
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message(
                "Only the challenged player can answer this invite.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        from services import delve_arena as da
        try:
            await da.mark_invite(self.ctx.db, self.invite.invite_id, "expired")
        except Exception:
            log.debug("arena duel: expire mark failed", exc_info=True)
        for c in self.children:
            try:
                c.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message:
            try:
                await self.message.edit(
                    embed=card(
                        "\U00002694 Arena Duel -- Expired", color=C_NEUTRAL,
                    ).description(
                        "Invite expired without an answer."
                    ).build(),
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        from services import delve_arena as da
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            inv = await da.fetch_invite(self.ctx.db, self.invite.invite_id)
            if inv is None or inv.expired or inv.accepted or inv.declined:
                await interaction.followup.send(
                    "This invite is no longer pending.", ephemeral=True,
                )
                self.stop()
                return
            p1, p2, duel = await da.begin_duel(
                self.ctx.db, self.ctx.guild_id, inv,
                names={
                    inv.challenger_uid: self.ctx.author.display_name[:32],
                    inv.target_uid: self.target.display_name[:32],
                },
            )
        except Exception:
            log.exception("arena duel: accept failed")
            await interaction.followup.send(
                "Couldn't start the duel.", ephemeral=True,
            )
            self.stop()
            return
        # Disable invite buttons.
        for c in self.children:
            try:
                c.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            await self.message.edit(view=self)  # type: ignore[union-attr]
        except (AttributeError, discord.HTTPException):
            pass
        self.stop()
        # Run the duel in-place via a fresh battle view.
        battle_view = _ArenaDuelBattleView(self.cog, self.ctx, inv, p1, p2, duel)
        await battle_view.start(interaction.channel)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        from services import delve_arena as da
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            await da.mark_invite(self.ctx.db, self.invite.invite_id, "declined")
        except Exception:
            log.debug("arena duel: decline mark failed", exc_info=True)
        for c in self.children:
            try:
                c.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message:
            try:
                await self.message.edit(
                    embed=card(
                        "\U00002694 Arena Duel -- Declined", color=C_NEUTRAL,
                    ).description(
                        f"{self.target.display_name} declined the duel."
                    ).build(),
                    view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()


class _ArenaDuelBattleView(discord.ui.View):
    """Interactive arena duel -- both players submit actions each round."""

    def __init__(
        self, cog: "Dungeon", ctx: DiscoContext,
        invite, p1, p2, duel,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.ctx = ctx
        self.invite = invite
        self.p1 = p1
        self.p2 = p2
        self.duel = duel
        self.message: discord.Message | None = None
        self._pending: dict[int, tuple[str, str | None]] = {}

    async def start(self, channel: discord.abc.Messageable) -> None:
        from services.delve_arena_render import render_arena_frame
        from services import delve_arena as da
        rk1 = await da.get_rank(self.ctx.db, self.ctx.guild_id, self.invite.challenger_uid)
        frame = render_arena_frame(
            p1=self.p1, p2=self.p2,
            p1_hp=self.duel.f1.hp, p2_hp=self.duel.f2.hp,
            round_num=1, max_rounds=25,
            action_banner="FIGHT!",
            rank_key=rk1.rank_key, division=rk1.division, elo=rk1.elo,
        )
        embed = card(
            "\U00002694 Arena Duel -- LIVE", color=C_PURPLE,
        ).description(
            f"```\n{frame}\n```\n"
            f"<@{self.invite.challenger_uid}> vs <@{self.invite.target_uid}>"
        ).footer(
            "Each round both players click an action. The engine ticks "
            "once both submit (Strike fallback on round timeout)."
        ).build()
        sent = await channel.send(embed=embed, view=self)
        self.message = sent

    def _action_for(self, uid: int) -> tuple[str, str | None] | None:
        return self._pending.get(int(uid))

    async def _submit(
        self, interaction: discord.Interaction, action: str,
        ability_key: str | None = None,
    ) -> None:
        uid = interaction.user.id
        if uid not in (self.invite.challenger_uid, self.invite.target_uid):
            await interaction.response.send_message(
                "Not your duel.", ephemeral=True,
            )
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        self._pending[uid] = (action, ability_key)
        # Ack the action via a quiet followup so the player sees their click registered.
        try:
            await interaction.followup.send(
                f"Action queued: {action}{(' ' + ability_key) if ability_key else ''}",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        if (self.invite.challenger_uid in self._pending
                and self.invite.target_uid in self._pending):
            await self._tick_round()

    async def _tick_round(self) -> None:
        a1 = self._action_for(self.invite.challenger_uid)
        a2 = self._action_for(self.invite.target_uid)
        self._pending.clear()
        ev = self.duel.step(a1, a2)
        await self._refresh(ev)
        if self.duel.over:
            await self._finish()

    async def _refresh(self, ev) -> None:
        from services.delve_arena_render import render_arena_frame
        from services import delve_arena as da
        rk = await da.get_rank(self.ctx.db, self.ctx.guild_id, self.invite.challenger_uid)
        frame = render_arena_frame(
            p1=self.p1, p2=self.p2,
            p1_hp=ev.p1_hp_after, p2_hp=ev.p2_hp_after,
            round_num=ev.round_num, max_rounds=25,
            action_banner=f"{ev.p1_action} vs {ev.p2_action}",
            rank_key=rk.rank_key, division=rk.division, elo=rk.elo,
        )
        log_tail = "\n".join((ev.p1_log + ev.p2_log)[-4:])
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=card(
                        "\U00002694 Arena Duel -- LIVE", color=C_PURPLE,
                    ).description(f"```\n{frame}\n```\n{log_tail}").build(),
                    view=self,
                )
            except discord.HTTPException:
                log.debug("arena duel: refresh failed", exc_info=True)

    async def _finish(self) -> None:
        from services import delve_arena as da
        from services.delve_arena_render import render_arena_frame
        replay = self.duel.finalize()
        try:
            settlement = await da.settle_duel(
                self.ctx.db, self.ctx.guild_id, self.invite, replay,
            )
        except Exception:
            log.exception("arena duel: settle failed")
            return
        # Fan out triggers for both duelists -- both are real players in
        # a live duel so both can earn arena achievements/quests.
        for uid_p in (self.invite.challenger_uid, self.invite.target_uid):
            try:
                await self.cog._fan_out_arena(
                    self.ctx, settlement, perspective_uid=uid_p,
                )
            except Exception:
                log.debug("arena duel: fan_out failed", exc_info=True)
        winner_name = (
            self.p1.name if settlement.winner_uid == self.p1.uid
            else (self.p2.name if settlement.winner_uid == self.p2.uid else "Draw")
        )
        banner = f"WIN: {winner_name}"
        if settlement.flawless:
            banner += "  FLAWLESS"
        last = replay.rounds[-1] if replay.rounds else None
        p1_hp = last.p1_hp_after if last else self.p1.hp_max
        p2_hp = last.p2_hp_after if last else self.p2.hp_max
        rk = await da.get_rank(self.ctx.db, self.ctx.guild_id, self.invite.challenger_uid)
        frame = render_arena_frame(
            p1=self.p1, p2=self.p2,
            p1_hp=p1_hp, p2_hp=p2_hp,
            round_num=len(replay.rounds), max_rounds=25,
            action_banner=banner,
            rank_key=rk.rank_key, division=rk.division, elo=rk.elo,
        )
        reward_line = "Unranked match -- no rewards."
        if settlement.ranked and settlement.reward_symbol:
            reward_line = (
                f"\U0001F381 +{settlement.reward_qty_human:.4f} "
                f"{settlement.reward_symbol}"
            )
        for c in self.children:
            try:
                c.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=card(
                        f"\U00002694 Arena Duel -- {winner_name}",
                        color=(
                            C_SUCCESS if settlement.winner_uid else C_AMBER
                        ),
                    ).description(
                        f"```\n{frame}\n```\n"
                        f"**Rounds**: {settlement.rounds}\n"
                        f"{reward_line}"
                    ).build(),
                    view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()

    @discord.ui.button(label="Strike", style=discord.ButtonStyle.primary, row=0)
    async def btn_strike(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        from services.delve_arena_battle import ACTION_STRIKE
        await self._submit(interaction, ACTION_STRIKE)

    @discord.ui.button(label="Skill", style=discord.ButtonStyle.secondary, row=0)
    async def btn_skill(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        from services.delve_arena_battle import ACTION_ABILITY
        # Use player's primary ability (matches the existing delve skill UX).
        uid = interaction.user.id
        prof = self.p1 if uid == self.invite.challenger_uid else self.p2
        prim = prof.abilities[0] if prof.abilities else None
        await self._submit(interaction, ACTION_ABILITY, prim)

    @discord.ui.button(label="Brace", style=discord.ButtonStyle.secondary, row=0)
    async def btn_brace(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        from services.delve_arena_battle import ACTION_BRACE
        await self._submit(interaction, ACTION_BRACE)

    @discord.ui.button(label="Flee", style=discord.ButtonStyle.danger, row=0)
    async def btn_flee(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        from services.delve_arena_battle import ACTION_FLEE
        await self._submit(interaction, ACTION_FLEE)


# ============================================================================
# Setup
# ============================================================================

async def setup(bot: Discoin) -> None:
    await bot.add_cog(Dungeon(bot))
