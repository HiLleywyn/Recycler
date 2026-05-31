"""
cogs/buddy.py  -  CC Buddy system (Phase 1).

A per-user ASCII companion. Players get HATCH_FREE_COUNT free hatches and
then pay a doubling fee per hatch (resets after 7 idle days; see
buddies_config.HATCH_BASE_PRICE_USD). Buddies gain XP from chat activity
and live in a single live-edited embed panel driven by core/framework/live.py.
Phase 1 ships:

    ,buddy hatch          -- hatch a buddy (free for the first few, then paid)
    ,buddy                -- show your buddy's live panel
    ,buddy stats          -- same as above (explicit alias)
    ,buddy rename         -- modal-based rename (7 day cooldown)
    ,buddy help           -- command help

The panel is the ONLY interaction surface: Feed / Pet / Talk / Refresh
buttons mutate state and re-render the same message in place. No extra
embeds are ever sent for actions; core/framework/live.py handles the edits.

Shelter, decay, run-away, and bonus integration land in Phase 2/3.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import discord
from discord.ext import commands

from configs.buddies_config import (
    ACTION_OVERRIDE_S,
    ADOPT_MOOD,
    BATTLE_CHALLENGE_TIMEOUT_S,
    BATTLE_COOLDOWN_S,
    BATTLE_STAKE_MAX,
    BATTLE_STAKE_MIN,
    CHAT_XP_COOLDOWN_S,
    CHAT_XP_MAX,
    CHAT_XP_MIN,
    DECAY_TICK_INTERVAL_S,
    FEED_COOLDOWN_S,
    FEED_ENERGY_DELTA,
    FEED_HAPPINESS_DELTA,
    FEED_HUNGER_DELTA,
    HATCH_BASE_PRICE_USD,
    HATCH_FREE_COUNT,
    HATCH_STREAK_RESET_SECONDS,
    MAX_LEVEL,
    MAX_OWNED_BUDDIES,
    NAME_MAX_LEN,
    PANEL_LIFETIME_S,
    PANEL_TICK_INTERVAL_S,
    PET_COOLDOWN_S,
    PET_ENERGY_DELTA,
    PET_HAPPINESS_DELTA,
    RARITY_TIERS,
    RENAME_PRICE_USD,
    BUDDY_GIFT_FEE_USD,
    REROLL_MAX,
    RESPEC_BASE_PRICE_USD,
    SPECIES,
    STAT_POINT_ATK_BONUS,
    STAT_POINT_HP_BONUS,
    STAT_POINT_SPD_BONUS,
    STAT_POINTS_PER_LEVEL,
    SWAP_BASE_PRICE_USD,
    TALK_COOLDOWN_S,
    TALK_ENERGY_DELTA,
    TALK_HAPPINESS_DELTA,
    effective_level,
    frame_key_for_mood,
    mood_label,
    rarity_meta,
    roll_rarity,
    xp_to_next,
)
from configs.buddy_gear_config import gear_display
from services.fishing import _as_dict as _json_dict
from core.config import Config
from core.framework.scale import to_human, to_raw
from services.buddy_ai import (
    generate_reply,
    owner_label_for,
    record_event,
)
from services.buddy_battle import (
    BattleResult,
    award_battle_xp,
    record_battle_result,
    record_pve_battle_result,
)
from services.buddy_lifecycle import (
    count_shelter,
    count_storage,
    from_storage,
    list_shelter,
    list_storage,
    set_active_buddy,
    sweep_decay,
    sweep_runaway,
    to_shelter,
    to_storage,
    try_adopt,
    try_reclaim,
)
from services import buddy_breeding
from services.buddy_world import (
    adopt_escaped,
    banish_defeated,
    mark_escaped,
    pick_escape_candidate,
    reclaim_to_shelter,
)
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.live import LiveState
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.premium import premium_required
from core.framework.quick_buy import QuickBuyView
from core.framework.ui import (
    C_AMBER,
    C_ERROR,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_NEUTRAL,
    C_PURPLE,
    C_SUCCESS,
    C_TEAL,
    FormatKit,
    fmt_pct,
    fmt_rel,
    fmt_token,
    fmt_ts,
    fmt_usd,
)
from services.buddy_names import (
    generate_name,
    pick_hatch_species,
    validate_rename,
)

log = logging.getLogger(__name__)

# Messages starting with any of these are treated as commands and skipped
# by the buddy XP listener. Mirrors cogs/chat_leveling.py.
_COMMAND_PREFIXES = (",", ".", "/", "$", "!", "?")

# World event cadence. The loop fires once per interval and rolls the
# chance for EACH guild independently, so a 20-guild bot with a 5%
# chance averages ~1 escape event per interval globally. Tuned so the
# bot never spams a single guild: the CHANCE roll is the main lever.
ESCAPED_EVENT_INTERVAL_S: int = 30 * 60          # 30 min between rolls
ESCAPED_EVENT_CHANCE: float = 0.15               # 15% per guild per roll
ESCAPED_EVENT_TIMEOUT_S: int = 10 * 60           # 10 min prompt lifetime

# (guild_id, user_id) -> unix seconds of last chat-XP grant. Per-process,
# matches the leveling cog's approach so buddies and chat XP stay independent.
_last_buddy_xp: dict[tuple[int, int], float] = {}

# state_id -> "feed" | "pet" | "talk"; set when a button is pressed and
# cleared after ACTION_OVERRIDE_S seconds so the idle animation resumes.
_action_override: dict[str, tuple[str, float]] = {}

# state_id -> {"feed": ts, "pet": ts, "talk": ts} per-action rate limits.
_action_last: dict[str, dict[str, float]] = {}


# =============================================================================
# Rendering
# =============================================================================

def _panel_state_id(user_id: int, channel_id: int) -> str:
    """Panel state is keyed by user + channel now that a single panel can
    page across multiple buddies."""
    return f"buddy:{user_id}:{channel_id}"


def _fighter_field(row: dict, *, owner_name: str | None = None) -> tuple[str, str]:
    """Render a (name, value) embed field describing ONE buddy in combat shape.

    Used by the challenge embed, escape-event embed, and battle-result
    embed so every buddy-vs-buddy visual shares a format. The returned
    value fits inside Discord's 1024-char field limit with room to spare.
    """
    species = str(row.get("species") or "")
    meta = SPECIES.get(species, {})
    emoji = str(meta.get("emoji") or "")
    name = str(row.get("name") or species.title() or "Buddy")
    tier = int(row.get("rarity_tier") or 1)
    tmeta = rarity_meta(tier)
    level = effective_level(row)
    hunger = max(0, min(100, int(row.get("hunger") or 0)))
    happiness = max(0, min(100, int(row.get("happiness") or 0)))
    energy = max(0, min(100, int(row.get("energy") or 0)))

    hp_alloc  = max(0, int(row.get("hp_alloc")  or 0))
    atk_alloc = max(0, int(row.get("atk_alloc") or 0))
    spd_alloc = max(0, int(row.get("spd_alloc") or 0))

    hp_mult = 0.5 + 0.5 * (hunger / 100.0)
    atk_mood = 0.5 + 0.5 * (happiness / 100.0)
    hp_base  = int(tmeta["hp_base"])  + level * 3   + hp_alloc  * STAT_POINT_HP_BONUS
    atk_base = int(tmeta["atk_base"]) + level * 0.8 + atk_alloc * STAT_POINT_ATK_BONUS
    max_hp = max(1, int(round(hp_base * hp_mult)))
    atk = float(atk_base) * atk_mood
    spd = min(1.0, 0.5 + 0.5 * (energy / 100.0) + spd_alloc * STAT_POINT_SPD_BONUS)

    ability = str(meta.get("ability_name") or "-")
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)

    # Always surface the species under the renamed display name. Without
    # this, a wolf renamed "Shrek" reads as just "🐺 Shrek" in the
    # challenge prompt and the wolf-only Pack Howl trigger looks like a
    # bug to the player. Showing "the Wolf" makes the ability lineage
    # obvious.
    species_label = species.title() if species else ""
    header = f"{emoji} **{name}**"
    if species_label and species_label.lower() != name.lower():
        header += f" the {species_label}"
    if owner_name:
        header += f"\n-# owned by {owner_name}"

    # Surface unlocked secondary / tertiary abilities inline with the
    # primary. Locked slots are skipped here to keep the card compact;
    # the buddy panel shows the full roadmap with lock icons.
    ability_parts: list[str] = [f"✨ {ability}"]
    prog = _species_ability_progression_safe(species)
    for slot in ("secondary", "tertiary"):
        entry = prog.get(slot) if prog else None
        if not entry:
            continue
        if level >= int(entry.get("unlock_level") or 1) and entry.get("name"):
            ability_parts.append(f"+ {entry['name']}")
    ability_line = "  ·  ".join(ability_parts)

    lines = [
        header,
        f"{tmeta.get('name') or 'Common'} -- Lv. **{level}**",
        f"❤️ HP {max_hp}  -  ⚔️ ATK {int(atk)}  -  💨 SPD {spd:.2f}",
        ability_line,
        f"`{FormatKit.bar(happiness, 100, width=6)}` mood",
    ]
    if wins + losses > 0:
        wr = 100.0 * wins / max(1, wins + losses)
        lines.append(f"Record: **{wins}W / {losses}L** ({wr:.0f}%)")
    return (f"{emoji} {name}", "\n".join(lines))


def _final_hp_field(
    fighter_name: str, fighter_emoji: str, hp: int, max_hp: int, *, is_winner: bool,
) -> tuple[str, str]:
    """Post-battle 'final HP' block. Bar turns red on a KO."""
    badge = "🏆" if is_winner else "💀"
    pct = (hp / max_hp * 100.0) if max_hp > 0 else 0.0
    bar = FormatKit.bar(max(0, hp), max(1, max_hp), width=10)
    status = "Victorious" if is_winner else ("KO" if hp <= 0 else "Standing")
    return (
        f"{badge} {fighter_emoji} {fighter_name}",
        f"`{bar}`\n**{max(0, hp)}/{max_hp} HP** ({pct:.0f}%)  -  {status}",
    )


def _current_frame(row: dict, state_id: str) -> str:
    """Pick the ASCII frame: action override wins, else mood-based idle.

    Boss-tamed buddies (row.boss_zone_id set) check the per-boss frame
    table FIRST so a captured Meadow King shows up with his crown on
    the `,buddy` panel instead of inheriting the generic wolf art.
    """
    species = str(row.get("species") or "")
    boss_zid = str(row.get("boss_zone_id") or "").strip()
    meta = SPECIES.get(species)
    if not meta:
        return "(no frame)"
    species_frames = meta.get("frames") or {}
    boss_frames: dict[str, str] = {}
    if boss_zid:
        from configs.buddies_config import BOSS_ASCII_FRAMES
        boss_frames = BOSS_ASCII_FRAMES.get(boss_zid) or {}

    def _pick(key: str) -> str | None:
        return boss_frames.get(key) or species_frames.get(key)

    override = _action_override.get(state_id)
    if override:
        label, until = override
        if time.time() < until:
            frame = _pick(label)
            if frame:
                return frame
        else:
            _action_override.pop(state_id, None)

    key = frame_key_for_mood(
        int(row.get("hunger") or 0),
        int(row.get("happiness") or 0),
        int(row.get("energy") or 0),
    )
    return _pick(key) or _pick("neutral") or "(no frame)"


def _build_panel_embed(
    row: dict,
    state_id: str,
    *,
    page_idx: int = 0,
    page_total: int = 1,
    action_line: str = "",
    owner_progression: str = "",
) -> discord.Embed:
    """Render the live buddy panel embed for ONE buddy page.

    Multi-pet collections get a ``[n/m] - Active``-style header above the
    ASCII frame so the user knows which of their buddies they're looking
    at and which one is passively gaining XP / mood shifts. The embed
    color tracks the buddy's rarity tier so rarer buddies visibly stand
    out in the channel.
    """
    species = str(row.get("species") or "unknown")
    meta = SPECIES.get(species, {})
    emoji = str(meta.get("emoji") or "")
    tagline = str(meta.get("tagline") or "")
    bonus_label = str(meta.get("bonus_label") or "")
    ability_name = str(meta.get("ability_name") or "")
    ability_progression = _species_ability_progression_safe(species)

    name = str(row.get("name") or "Buddy")
    xp = int(row.get("xp") or 0)
    lvl = effective_level(row)
    into, needed = xp_to_next(xp)

    hunger    = max(0, min(100, int(row.get("hunger") or 0)))
    happiness = max(0, min(100, int(row.get("happiness") or 0)))
    energy    = max(0, min(100, int(row.get("energy") or 0)))

    is_active = bool(row.get("is_active"))
    tier      = int(row.get("rarity_tier") or 1)
    tier_meta = rarity_meta(tier)
    color     = int(tier_meta.get("color_hex") or C_PURPLE)

    frame = _current_frame(row, state_id)

    # Spent stat points -- folded into the displayed combat stats below so
    # the panel matches services.buddy_battle.Fighter.from_row exactly.
    hp_alloc  = max(0, int(row.get("hp_alloc")  or 0))
    atk_alloc = max(0, int(row.get("atk_alloc") or 0))
    spd_alloc = max(0, int(row.get("spd_alloc") or 0))
    spent     = hp_alloc + atk_alloc + spd_alloc
    available = max(0, lvl * STAT_POINTS_PER_LEVEL - spent)

    # Combat stats, derived from level + mood + tier + upgrades. Kept in
    # lock-step with services.buddy_battle.Fighter.from_row so the panel
    # never lies about what the buddy would do in a fight.
    hp_mult    = 0.5 + 0.5 * (hunger / 100.0)
    atk_mood   = 0.5 + 0.5 * (happiness / 100.0)
    hp_base    = int(tier_meta["hp_base"]) + lvl * 3 + hp_alloc * STAT_POINT_HP_BONUS
    atk_base   = float(int(tier_meta["atk_base"]) + lvl * 0.8 + atk_alloc * STAT_POINT_ATK_BONUS)
    combat_hp  = max(1, int(round(hp_base * hp_mult)))
    combat_atk = int(round(atk_base * atk_mood))
    combat_spd = min(1.0, 0.5 + 0.5 * (energy / 100.0) + spd_alloc * STAT_POINT_SPD_BONUS)

    header_bits: list[str] = []
    if page_total > 1:
        header_bits.append(f"**[{page_idx + 1}/{page_total}]**")
    header_bits.append(f"Rarity: **{tier_meta['name']}**")
    header_bits.append("⚡ Active" if is_active else "💤 Resting")
    header = "  -  ".join(header_bits)

    desc_lines: list[str] = [header]
    if tagline:
        desc_lines.append(f"*{tagline}*")
    desc_lines.append(f"```{frame}```")
    if action_line:
        desc_lines.append(f"> {action_line}")

    xp_field = (
        f"**Lv. {lvl}** (MAX)  -  {mood_label(hunger, happiness, energy)}"
        if lvl >= MAX_LEVEL
        else (
            f"**Lv. {lvl}**  -  `{FormatKit.bar(into, max(1, needed), width=10)}`  "
            f"{into:,} / {needed:,} XP"
        )
    )

    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    battle_count = int(row.get("battle_count") or wins + losses)
    record_line = ""
    if battle_count > 0:
        if wins + losses > 0:
            wr = 100.0 * wins / max(1, wins + losses)
            record_line = f"**{wins}W  -  {losses}L**  ({wr:.0f}% win rate, {battle_count} fought)"
        else:
            record_line = f"{battle_count} battle(s), no decisive result yet"

    # Append a "+N from upgrades" tag per stat the player has invested
    # in so the visible HP/ATK/SPD numbers come with their own provenance.
    hp_bonus  = int(round(hp_alloc  * STAT_POINT_HP_BONUS))
    atk_bonus = int(round(atk_alloc * STAT_POINT_ATK_BONUS))
    spd_bonus = spd_alloc * STAT_POINT_SPD_BONUS
    hp_tag  = f" *(+{hp_bonus} upg)*"  if hp_bonus  > 0 else ""
    atk_tag = f" *(+{atk_bonus} upg)*" if atk_bonus > 0 else ""
    spd_tag = f" *(+{spd_bonus:.2f} upg)*" if spd_bonus > 0 else ""
    combat_line = (
        f"❤️ HP **{combat_hp}**{hp_tag}  -  "
        f"⚔️ ATK **{combat_atk}**{atk_tag}  -  "
        f"💨 SPD **{combat_spd:.2f}**{spd_tag}"
    )
    if ability_name:
        combat_line += f"\n✨ Ability: **{ability_name}**"
    # Surface secondary + tertiary unlocks. Locked entries show the
    # required level so players know what's coming; unlocked entries
    # show a check + the ability blurb.
    progression_lines = _format_ability_progression(ability_progression, lvl)
    if progression_lines:
        combat_line += "\n" + "\n".join(progression_lines)

    # Age based on hatched_at (datetime or epoch float via PgRow coerce).
    # Fall back to "Just hatched" for freshly adopted / migrated rows.
    hatched_at = row.get("hatched_at")
    age_tail = ""
    try:
        if hatched_at:
            ts = hatched_at.timestamp() if hasattr(hatched_at, "timestamp") else float(hatched_at)
            age_s = int(max(0, time.time() - ts))
            age_tail = f"  -  Age: {FormatKit.time_ago(age_s).replace(' ago', '')}"
    except Exception:
        age_tail = ""

    footer_tail = (
        f"Feed, pet, or talk to keep {name} happy"
        if is_active
        else f"{name} is resting. Set Active to bring them back in."
    )

    # Surface the cc_buddies id + gender glyph in the title so the
    # player can quote the id directly in ,ah list buddy <id> <price>
    # / ,buddy store <id> / etc. and see whether they need an opposite
    # partner for ,buddy nest deposit at a glance.
    bid = int(row.get("id") or 0)
    from configs.buddies_config import gender_glyph as _gender_glyph
    glyph = _gender_glyph(row.get("gender"))
    title_bits: list[str] = [f"{emoji}  {name}"]
    if glyph:
        title_bits.append(glyph)
    if bid:
        title_bits.append(f"`#{bid}`")
    upgrade_line = _upgrades_field_value(
        hp_alloc, atk_alloc, spd_alloc, available,
    )

    builder = (
        card("  ·  ".join(title_bits), color=color)
        .description("\n".join(desc_lines))
        .field("📈 Progress", xp_field, inline=False)
        .field("🍖 Hunger",    f"`{FormatKit.bar(hunger,    100, width=10)}`  {hunger}/100",    inline=True)
        .field("😊 Happiness", f"`{FormatKit.bar(happiness, 100, width=10)}`  {happiness}/100", inline=True)
        .field("⚡ Energy",    f"`{FormatKit.bar(energy,    100, width=10)}`  {energy}/100",    inline=True)
        .field("⚔️ Combat", combat_line, inline=False)
        .field("🛠 Upgrades", upgrade_line, inline=False)
        .field_if(bool(record_line), "🏆 Record", record_line, inline=False)
        .field_if(bool(bonus_label), "💰 Bonus", bonus_label, inline=False)
        .field_if(bool(_passive_effects_field(species, lvl, tier)),
                  "✨ Passive effects",
                  _passive_effects_field(species, lvl, tier),
                  inline=False)
        .field_if(bool(owner_progression), "⭐ Owner", owner_progression, inline=False)
        .field_if(
            bool(_json_dict(row.get("gear"))),
            "\U0001F9E3 Equipped",
            gear_display(_json_dict(row.get("gear"))),
            inline=False,
        )
        .footer(f"{footer_tail}{age_tail}  -  {fmt_ts(time.time())}")
    )
    return builder.build()


def _upgrades_field_value(
    hp_alloc: int, atk_alloc: int, spd_alloc: int, available: int,
) -> str:
    """Render the buddy panel's ``Upgrades`` field.

    Shows what the player has bought with stat points, what each point
    grants in raw battle units (so the bonuses on the Combat line above
    are explained), and how many points are still waiting to be spent.
    Matches the per-point constants used in
    ``services.buddy_battle.Fighter.from_row`` exactly so the panel never
    over- or under-promises what an investment will do.
    """
    spent = hp_alloc + atk_alloc + spd_alloc
    lines = [
        f"❤️ Hardiness **{hp_alloc}**  ·  +{int(hp_alloc * STAT_POINT_HP_BONUS)} max HP",
        f"⚔️ Power **{atk_alloc}**  ·  +{atk_alloc * STAT_POINT_ATK_BONUS:g} ATK",
        f"💨 Vigor **{spd_alloc}**  ·  +{spd_alloc * STAT_POINT_SPD_BONUS:.3f} SPD",
        f"-# +{STAT_POINT_HP_BONUS:g} HP / +{STAT_POINT_ATK_BONUS:g} ATK / "
        f"+{STAT_POINT_SPD_BONUS * 100:g}% SPD per point  ·  "
        f"{spent} spent",
    ]
    if available > 0:
        lines.append(
            f"✨ **{available} unspent point(s)** -- run "
            f"`,buddy upgrade` to allocate."
        )
    return "\n".join(lines)


def _species_ability_progression_safe(species: str) -> dict[str, dict]:
    """Wrapper around buddies_config.species_ability_progression that
    never raises (returns {} on any import / lookup error)."""
    try:
        from configs.buddies_config import species_ability_progression as _sap
        return _sap(species) or {}
    except Exception:
        return {}


def _format_ability_progression(progression: dict[str, dict], level: int) -> list[str]:
    """Render the secondary + tertiary unlock lines for the buddy panel.

    Skips the ``primary`` entry (already shown above as ``✨ Ability:``).
    Locked entries display the required level and a lock icon; unlocked
    entries display the ability name + blurb.
    """
    if not progression:
        return []
    lines: list[str] = []
    for slot in ("secondary", "tertiary"):
        entry = progression.get(slot)
        if not entry:
            continue
        unlock = int(entry.get("unlock_level") or 1)
        name = str(entry.get("name") or "")
        desc = str(entry.get("desc") or "")
        if not name:
            continue
        if level >= unlock:
            icon = "🟢"
            tag = f"Lv {unlock}"
            lines.append(f"{icon} **{name}** *(unlocked at {tag})* -- {desc}")
        else:
            icon = "🔒"
            lines.append(
                f"{icon} _{name}_ -- unlocks at **Lv {unlock}**. {desc}"
            )
    return lines


def _passive_effects_field(species: str, level: int, rarity_tier: int) -> str:
    """Render the buddy's species + rarity passive effects.

    Pulls the buddy's signature lanes via ``buddy_bonus_lanes_for`` and
    formats each as ``Lane: +X.X%``. Common buddies surface their
    species lane only; Rare / Epic / Legendary surface 2 / 3 / 4 lanes
    so players can see their rarer buddies actively help across more
    of the bot.

    Numbers match ``services/buddy_bonus.py:buddy_bonus`` exactly so the
    panel never lies about what the buff actually grants.
    """
    try:
        from configs.buddies_config import (
            BONUS_LANE_LABELS,
            BONUS_SIG_PER_LEVEL,
            MAX_LEVEL,
            buddy_bonus_lanes_for,
            rarity_meta as _rmeta,
        )
    except Exception:
        return ""
    lanes = buddy_bonus_lanes_for(str(species or ""), int(rarity_tier or 1))
    if not lanes:
        return ""
    bonus_mult = float(_rmeta(int(rarity_tier or 1)).get("bonus_mult", 1.0))
    lvl = max(1, min(int(MAX_LEVEL), int(level or 1)))
    per_lvl_pct = BONUS_SIG_PER_LEVEL * bonus_mult * lvl * 100.0
    species_lane = lanes[0]
    rarity_lanes = lanes[1:]
    lines: list[str] = [
        f"**Species** ({BONUS_LANE_LABELS.get(species_lane, species_lane)}): "
        f"+{per_lvl_pct:.1f}%",
    ]
    if rarity_lanes:
        rarity_bits = ", ".join(
            f"{BONUS_LANE_LABELS.get(l, l)}" for l in rarity_lanes
        )
        lines.append(
            f"**Rarity** ({rarity_bits}): +{per_lvl_pct:.1f}% each"
        )
    return "\n".join(lines)


def _format_species_avail(soft_cap: int = 800) -> str:
    """Comma-joined list of every species key, capped to fit a Discord
    field value. The full SPECIES catalog now spans enough names that
    naive ", ".join can creep up on the 1024 char field limit when
    nested inside a hint embed; truncating with a "+N more" tail and
    pointing at the panel keeps the hint usable.
    """
    keys = list(SPECIES.keys())
    full = ", ".join(f"`{k}`" for k in keys)
    if len(full) <= soft_cap:
        return full
    parts: list[str] = []
    used = 0
    for i, k in enumerate(keys):
        chunk = f"`{k}`"
        sep = ", " if parts else ""
        if used + len(sep) + len(chunk) > soft_cap:
            remaining = len(keys) - i
            parts.append(f"+{remaining} more  (use `,buddy species` for the full list)")
            break
        parts.append(chunk)
        used += len(sep) + len(chunk)
    return ", ".join(parts)


async def _fetch_active(db, guild_id: int, user_id: int) -> dict | None:
    """Return the user's currently ACTIVE buddy row, or None.

    Used by chat XP, buddy_bonus callers, reroll, swap, surrender, and
    rename -- every place that previously assumed "the single buddy".

    The result is left-joined against ``buddy_expeditions`` so callers
    can read ``row["on_expedition"]`` (bool) + ``row["expedition_ends_at"]``
    without a second round-trip. Surfaces an expedition's deadline so
    "your buddy is busy until X" hints can quote a time.
    """
    return await db.fetch_one(
        """
        SELECT b.*,
               (e.expedition_id IS NOT NULL) AS on_expedition,
               e.ends_at                     AS expedition_ends_at,
               e.destination                 AS expedition_destination
          FROM cc_buddies b
          LEFT JOIN buddy_expeditions e
                 ON e.buddy_id = b.id
                AND e.status   = 'running'
         WHERE b.guild_id = $1 AND b.owner_user_id = $2
           AND b.status = 'owned' AND b.is_active
         LIMIT 1
        """,
        guild_id, user_id,
    )


def _expedition_busy_message(row: dict) -> str | None:
    """Return a user-facing 'buddy is on expedition' refusal, or None.

    Centralises the message + the busy check so feed/pet/talk/battle
    paths just call this and bail when it returns a string.
    """
    if not row or not row.get("on_expedition"):
        return None
    name = str(row.get("name") or "Your buddy")
    dest = str(row.get("expedition_destination") or "an expedition")
    ends = row.get("expedition_ends_at")
    when = f" (back {fmt_rel(ends)})" if ends is not None else ""
    return (
        f"**{name}** is away on **{dest.replace('_', ' ').title()}**{when}. "
        f"Buddies on expeditions can't be fed, petted, talked to, or "
        f"sent into combat until they return. `,expedition` to track."
    )


async def _fetch_all_owned(db, guild_id: int, user_id: int) -> list[dict]:
    """Return every owned buddy for the user, active first."""
    rows = await db.fetch_all(
        "SELECT * FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned' "
        "ORDER BY is_active DESC, id ASC",
        guild_id, user_id,
    )
    return list(rows or [])


async def _count_owned(db, guild_id: int, user_id: int) -> int:
    val = await db.fetch_val(
        "SELECT COUNT(*) FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned'",
        guild_id, user_id,
    )
    return int(val or 0)


# =============================================================================
# Rename helper (shared by command + modal)
# =============================================================================

async def _charge_and_rename(
    db: Any, *, owner_id: int, guild_id: int, buddy_id: int, new_name: str,
) -> tuple[bool, str]:
    """Atomically deduct the rename price and update the buddy's name.

    Returns ``(ok, user_facing_message)``. Wraps the payment + update in a
    single transaction so a failed buddy lookup rolls the charge back.
    The buddy is locked via SELECT FOR UPDATE to block a concurrent
    surrender / reroll from racing the rename.
    """
    raw_price = to_raw(RENAME_PRICE_USD)
    try:
        async with db.transaction() as conn:
            target = await conn.fetchrow(
                "SELECT id FROM cc_buddies "
                "WHERE id = $1 AND owner_user_id = $2 AND status = 'owned' "
                "FOR UPDATE",
                buddy_id, owner_id,
            )
            if not target:
                return False, "Rename failed -- buddy not found."
            paid = await conn.fetchrow(
                "UPDATE users SET "
                "  wallet = GREATEST(0, wallet - $1), "
                "  bank   = bank - GREATEST(0, $1 - wallet) "
                "WHERE user_id = $2 AND guild_id = $3 "
                "  AND (wallet + bank) >= $1 "
                "RETURNING 1",
                raw_price, owner_id, guild_id,
            )
            if not paid:
                return False, (
                    f"Rename costs **${RENAME_PRICE_USD:,}** (wallet + bank combined). "
                    f"You don't have enough."
                )
            await conn.execute(
                "UPDATE cc_buddies SET "
                "  name = $3, last_rename_at = NOW(), "
                "  rename_count = rename_count + 1, updated_at = NOW() "
                "WHERE id = $1 AND owner_user_id = $2",
                buddy_id, owner_id, new_name,
            )
    except Exception:
        log.exception(
            "rename transaction failed uid=%s buddy_id=%s", owner_id, buddy_id,
        )
        return False, "Rename failed. Please try again."
    return True, f"Renamed to **{new_name}** for **${RENAME_PRICE_USD:,}**."


# =============================================================================
# Rename modal
# =============================================================================

class RenameModal(discord.ui.Modal):
    """Modal popup for renaming a buddy.

    Lives here rather than using core.framework.ui.InputModal because it needs
    to run validation + DB writes + refresh the caller's live panel.
    """

    def __init__(
        self, cog: "Buddy", owner_id: int, buddy_id: int, current_name: str,
    ) -> None:
        super().__init__(title="Rename your buddy")
        self._cog = cog
        self._owner_id = owner_id
        self._buddy_id = buddy_id
        self._input = discord.ui.TextInput(
            label="New name",
            placeholder=current_name or "e.g. Zenny",
            required=True,
            max_length=NAME_MAX_LEN,
            default=current_name,
        )
        self.add_item(self._input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = (self._input.value or "").strip()
        ok, err = validate_rename(new_name)
        if not ok:
            await interaction.response.send_message(f"Rename rejected: {err}", ephemeral=True)
            return

        db = self._cog.bot.db
        ok, msg = await _charge_and_rename(
            db,
            owner_id=self._owner_id,
            guild_id=interaction.guild_id,
            buddy_id=self._buddy_id,
            new_name=new_name,
        )
        await interaction.response.send_message(msg, ephemeral=True)


# =============================================================================
# Upgrade modal + view
# =============================================================================
# Stat-point allocation flow:
#   ,buddy upgrade  ->  embed showing current allocs + Spend button
#   button click    ->  modal with three numeric inputs (HP / ATK / SPD)
#   modal submit    ->  validates, applies the allocation atomically
#
# Available points are computed live from level minus current allocations,
# so a user who levels up after opening the panel still gets credited if
# they hit Spend before re-rendering. The DB UPDATE re-checks the cap so
# concurrent submits can't exceed the cap.

def _parse_alloc_input(raw: str) -> int:
    """Parse a modal text input as a non-negative integer. Empty == 0."""
    s = (raw or "").strip()
    if not s:
        return 0
    if not s.isdigit():
        raise ValueError("must be a non-negative whole number")
    return int(s)


async def _apply_alloc(
    db,
    owner_id: int,
    guild_id: int,
    buddy_id: int,
    add_hp: int,
    add_atk: int,
    add_spd: int,
) -> tuple[bool, str, dict | None]:
    """Apply an allocation delta atomically, re-checking the cap DB-side.

    Returns ``(ok, message, updated_row)``. ``updated_row`` is ``None`` on
    failure. The UPDATE only commits if ``hp_alloc + atk_alloc + spd_alloc
    + delta <= level``, so two concurrent submits can't double-spend.
    """
    if add_hp < 0 or add_atk < 0 or add_spd < 0:
        return False, "Each stat must be 0 or greater.", None
    delta = add_hp + add_atk + add_spd
    if delta == 0:
        return False, "You didn't allocate anything.", None

    # Cap check uses level_from_xp(xp) so a buddy that just leveled up
    # via battle / expedition / craft can immediately spend the new
    # points -- even if the stored ``level`` column hasn't caught up yet.
    row = await db.fetch_one(
        "UPDATE cc_buddies SET "
        "  hp_alloc  = hp_alloc  + $1, "
        "  atk_alloc = atk_alloc + $2, "
        "  spd_alloc = spd_alloc + $3, "
        "  level     = GREATEST(level, LEAST(50, GREATEST(1, "
        "      FLOOR((1.0 + SQRT(1.0 + 8.0 * xp::double precision / 120.0)) / 2.0)::int "
        "  ))), "
        "  updated_at = NOW() "
        "WHERE id = $4 AND owner_user_id = $5 AND guild_id = $6 "
        "  AND status = 'owned' "
        "  AND (hp_alloc + atk_alloc + spd_alloc + $7) <= "
        "      GREATEST(level, LEAST(50, GREATEST(1, "
        "          FLOOR((1.0 + SQRT(1.0 + 8.0 * xp::double precision / 120.0)) / 2.0)::int "
        "      ))) "
        "RETURNING id, name, species, level, xp, hp_alloc, atk_alloc, spd_alloc",
        add_hp, add_atk, add_spd, buddy_id, owner_id, guild_id, delta,
    )
    if not row:
        return False, (
            f"Not enough points -- requested {delta}, but you don't have "
            f"that many available. Run the command again to see your "
            f"current totals."
        ), None
    return True, "", row


def _alloc_summary(row: dict) -> tuple[int, int, int, int, int]:
    """Return (level, hp_alloc, atk_alloc, spd_alloc, available).

    ``level`` is XP-derived so the unspent-points count tracks the buddy's
    actual rank even when the stored ``level`` column lags behind a recent
    XP grant (battle / expedition / craft).
    """
    level     = effective_level(row)
    hp_alloc  = max(0, int(row.get("hp_alloc")  or 0))
    atk_alloc = max(0, int(row.get("atk_alloc") or 0))
    spd_alloc = max(0, int(row.get("spd_alloc") or 0))
    available = max(0, level * STAT_POINTS_PER_LEVEL - (hp_alloc + atk_alloc + spd_alloc))
    return level, hp_alloc, atk_alloc, spd_alloc, available


class UpgradeModal(discord.ui.Modal):
    """Modal that takes per-stat point increments and applies them."""

    def __init__(self, cog: "Buddy", owner_id: int, buddy_id: int, available: int) -> None:
        super().__init__(title=f"Upgrade Buddy ({available} pts available)")
        self._cog = cog
        self._owner_id = owner_id
        self._buddy_id = buddy_id
        self._available = available

        self._hp = discord.ui.TextInput(
            label=f"Hardiness (+{STAT_POINT_HP_BONUS:g} max HP each)",
            placeholder="0", required=False, default="0", max_length=4,
        )
        self._atk = discord.ui.TextInput(
            label=f"Power (+{STAT_POINT_ATK_BONUS:g} ATK each)",
            placeholder="0", required=False, default="0", max_length=4,
        )
        self._spd = discord.ui.TextInput(
            label=f"Vigor (+{STAT_POINT_SPD_BONUS * 100:g}% SPD each, cap 1.0)",
            placeholder="0", required=False, default="0", max_length=4,
        )
        self.add_item(self._hp)
        self.add_item(self._atk)
        self.add_item(self._spd)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            add_hp  = _parse_alloc_input(self._hp.value)
            add_atk = _parse_alloc_input(self._atk.value)
            add_spd = _parse_alloc_input(self._spd.value)
        except ValueError as e:
            await interaction.response.send_message(
                f"Invalid input: {e}.", ephemeral=True,
            )
            return

        delta = add_hp + add_atk + add_spd
        if delta > self._available:
            await interaction.response.send_message(
                f"You only have **{self._available}** points available, "
                f"but you tried to spend **{delta}**.",
                ephemeral=True,
            )
            return

        ok, msg, row = await _apply_alloc(
            self._cog.bot.db,
            owner_id=self._owner_id,
            guild_id=interaction.guild_id,
            buddy_id=self._buddy_id,
            add_hp=add_hp, add_atk=add_atk, add_spd=add_spd,
        )
        if not ok or row is None:
            await interaction.response.send_message(msg, ephemeral=True)
            return

        level, hp_alloc, atk_alloc, spd_alloc, available = _alloc_summary(row)
        emoji = str(SPECIES.get(str(row.get("species") or ""), {}).get("emoji") or "")
        name = str(row.get("name") or "your buddy")
        await interaction.response.send_message(
            f"{emoji} **{name}** spent **{delta}** points.\n"
            f"Hardiness: **{hp_alloc}**  -  Power: **{atk_alloc}**  -  "
            f"Vigor: **{spd_alloc}**\n"
            f"Available: **{available}** / {level}.",
            ephemeral=True,
        )


class UpgradeView(discord.ui.View):
    """Single-button view that opens the UpgradeModal for the buddy owner."""

    def __init__(self, cog: "Buddy", owner_id: int, buddy_id: int) -> None:
        super().__init__(timeout=PANEL_LIFETIME_S)
        self.cog = cog
        self.owner_id = owner_id
        self.buddy_id = buddy_id

    @discord.ui.button(label="Spend points", emoji="\U00002B06", style=discord.ButtonStyle.primary)
    async def spend_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the buddy's owner can spend its points.", ephemeral=True,
            )
            return
        # Re-read the row so we open the modal with up-to-date "available".
        row = await self.cog.bot.db.fetch_one(
            "SELECT id, level, hp_alloc, atk_alloc, spd_alloc "
            "FROM cc_buddies "
            "WHERE id = $1 AND owner_user_id = $2 AND status = 'owned'",
            self.buddy_id, self.owner_id,
        )
        if not row:
            await interaction.response.send_message(
                "That buddy is no longer yours.", ephemeral=True,
            )
            return
        _, _, _, _, available = _alloc_summary(row)
        if available <= 0:
            await interaction.response.send_message(
                "No points available right now -- level up to earn more.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            UpgradeModal(self.cog, self.owner_id, self.buddy_id, available),
        )


# =============================================================================
# Panel View (Feed / Pet / Talk / Rename / Refresh)
# =============================================================================

class _BuddyListAHModal(discord.ui.Modal, title="List Buddy on Auction House"):
    """Modal: ask for a price (and optional currency) to list the
    panel's CURRENT-page buddy on the AH. Submission routes through
    ``services.auction.create_listing_by_token`` so all the safeguards
    (gas, escrow, event log) stay in place.
    """

    price = discord.ui.TextInput(
        label="Price",
        placeholder="e.g. 50000",
        required=True,
        max_length=20,
    )
    currency = discord.ui.TextInput(
        label="Currency (optional)",
        placeholder="leave blank for the network's default (BUD)",
        required=False,
        max_length=10,
    )

    def __init__(self, view: "BuddyPanelView", buddy_id: int) -> None:
        super().__init__()
        self.view = view
        self.buddy_id = int(buddy_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from services import auction as _auc
        try:
            price_v = float(str(self.price.value).strip())
            if price_v <= 0:
                raise ValueError("Price must be positive.")
        except ValueError as e:
            await interaction.response.send_message(
                f"Bad price: {e}", ephemeral=True,
            )
            return
        cur = (str(self.currency.value or "").strip().upper() or None)
        try:
            tok_id = await _auc.find_owned_buddy_token(
                self.view.cog.bot.db,
                guild_id=int(interaction.guild_id or 0),
                seller_user_id=interaction.user.id,
                buddy_id=self.buddy_id,
            )
        except Exception:
            tok_id = None
        if not tok_id:
            await interaction.response.send_message(
                f"Couldn't find buddy `#{self.buddy_id}`'s NFT (or it's "
                f"escrowed / wrong owner).",
                ephemeral=True,
            )
            return
        try:
            listing_id, _tok, msg = await _auc.create_listing_by_token(
                self.view.cog.bot.db,
                guild_id=int(interaction.guild_id or 0),
                seller_user_id=interaction.user.id,
                token_id=tok_id,
                price=price_v,
                currency=cur,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            log.exception(
                "buddy panel List click failed buddy=%s",
                self.buddy_id,
            )
            await interaction.response.send_message(
                f"Could not list: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"\U0001F3DB Listed buddy `#{self.buddy_id}` as listing "
            f"#{int(listing_id)}. {msg}",
            ephemeral=True,
        )


class _BuddySelect(discord.ui.Select):
    """Jump-to-buddy dropdown on the live panel.

    Lists every owned buddy with rarity / level / status badges and
    lets the user jump directly to any one of them instead of clicking
    Prev/Next through a long collection. The select is rebuilt on every
    re-render so newly hatched / sold / gifted buddies show up
    immediately. Discord caps a single select at 25 options; the
    dropdown is disabled past that point and the user falls back to
    Prev/Next pagination.
    """

    def __init__(self, parent: "BuddyPanelView", pages: list[dict]) -> None:
        opts: list[discord.SelectOption] = []
        for i, b in enumerate(pages[:25]):
            try:
                from configs.buddies_config import (
                    SPECIES as _SPECIES,
                    rarity_meta as _b_rarity,
                )
                emoji = (
                    str((_SPECIES.get(str(b.get("species") or "")) or {}).get("emoji") or "")
                    or "\U0001F436"
                )
                tier_name = str(
                    _b_rarity(int(b.get("rarity_tier") or 1)).get("name")
                    or "Common"
                )
            except Exception:
                emoji, tier_name = "\U0001F436", "Common"
            name = str(b.get("name") or "Buddy")
            level = effective_level(b)
            active_tag = " (active)" if b.get("is_active") else ""
            label = f"{name} -- L{level} {tier_name}{active_tag}"
            opts.append(discord.SelectOption(
                label=label[:100],
                value=str(i),
                emoji=emoji,
                default=(i == parent.page_idx),
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no buddies)", value="_none_", default=True,
            )]
        super().__init__(
            placeholder="Jump to a buddy...",
            options=opts,
            min_values=1, max_values=1, row=3,
            disabled=(len(opts) == 1 and opts[0].value == "_none_"),
            # Stable custom_id so the parent BuddyPanelView (timeout=None,
            # registered via bot.add_view in cog_load) routes post-restart
            # interactions to its rehydrated stub instead of dropping them
            # with "this interaction failed".
            custom_id="buddy_panel:select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        view: "BuddyPanelView" = self.view  # type: ignore[assignment]
        try:
            view.page_idx = int(v)
        except ValueError:
            await interaction.response.defer()
            return
        # Wrap the rebuild so a failure in _load_pages / _build_panel_embed
        # / edit_message doesn't leave the user with Discord's generic
        # "this interaction failed" -- surface the real exception via
        # ephemeral followup so we can diagnose.
        try:
            await view._re_render(interaction)
        except Exception as e:
            log.exception(
                "buddy panel select-rebuild failed uid=%s page=%s",
                view.owner_id, view.page_idx,
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        f"Couldn't rebuild buddy panel: "
                        f"`{type(e).__name__}: {e}`",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        f"Couldn't rebuild buddy panel: "
                        f"`{type(e).__name__}: {e}`",
                        ephemeral=True,
                    )
            except Exception:
                log.debug(
                    "buddy panel select error-followup failed",
                    exc_info=True,
                )


class BuddyPanelView(discord.ui.View):
    """Persistent button row for the live buddy panel.

    A single panel pages across every owned buddy in the user's collection
    (``MAX_OWNED_BUDDIES`` max). The passive live tick always re-renders
    the CURRENT PAGE; Feed / Pet / Talk always operate on the user's
    ACTIVE buddy. If the user is viewing a resting (inactive) page the
    mood buttons are disabled -- Set Active promotes that page's buddy
    first, then the mood buttons light up.

    Row 0: Feed, Pet, Talk               (mood interactions; active only)
    Row 1: Rename, Refresh, List on AH   (meta; current page)
    Row 2: Prev, Next, Set Active        (navigation)
    Row 3: Jump-to-buddy dropdown        (skip the Prev/Next clickfest)
    """

    def __init__(
        self,
        cog: "Buddy | None" = None,
        owner_id: int | None = None,
        state_id: str | None = None,
    ) -> None:
        # ``timeout=None`` makes the view persistent: discord.py keeps
        # routing interactions to it across bot restarts so clicks on a
        # panel opened before a redeploy land cleanly instead of
        # producing "this interaction failed" / "unknown view, discarding".
        # All component custom_ids below are explicit + stable for the
        # same reason.
        #
        # Constructor args are optional. Fresh creation passes them all
        # (cog + owner_id + state_id) so the panel works fully -- live
        # tick, gated owner-only interactions, in-flight page state.
        # Rehydration (the persistent registration via ``bot.add_view``)
        # passes none; in that mode each callback derives the cog via
        # ``interaction.client.get_cog("Buddy")`` and degrades gracefully
        # when state isn't available (responds with a "panel expired,
        # run ,buddy" hint).
        super().__init__(timeout=None)
        self.cog = cog
        self.owner_id = owner_id
        self.state_id = state_id
        # Index into the ordered list of owned buddies. Re-read from DB on
        # every interaction, so "page 0" is meaningful only relative to the
        # current owned-buddy ordering (active first, then by id).
        self.page_idx: int = 0
        # Pre-rendered owner progression line (achievements / streak / pass).
        # Computed once at ,buddy entry and reused across every re-render
        # so the live tick doesn't pay a DB query on every frame.
        self.owner_progression: str = ""

        # Persistent rehydration needs every component the live view ever
        # uses to be present at registration time so discord.py can match
        # post-restart interactions by custom_id. The 8 buttons are
        # declared via @discord.ui.button decorators above (already
        # present on every instance). The select is added dynamically in
        # _rebuild_buddy_select on the live tick path -- but on the
        # persistent-view stub (no constructor args) that path never
        # fires, so we add a placeholder copy here to keep the custom_id
        # in the registry. The placeholder's options are dummy (the stub
        # interaction_check rejects all clicks with a "panel expired"
        # hint anyway); the real options get rebuilt on each render of
        # a fresh, in-memory view via _rebuild_buddy_select.
        if cog is None and owner_id is None:
            self._add_persistent_stub_select()

    def _add_persistent_stub_select(self) -> None:
        """Attach a dummy ``_BuddySelect`` to the persistent-stub view so
        discord.py can route post-restart dropdown interactions by
        custom_id. The instance never sees real callbacks (the
        interaction_check rejects all clicks on the stub) so the option
        list is just a placeholder.
        """
        try:
            self.add_item(_BuddySelect(self, []))
        except Exception:
            log.debug("BuddyPanelView stub-select attach failed", exc_info=True)

    def _is_rehydrated(self) -> bool:
        """True when the view was rebuilt by discord.py's persistent-
        view registration (no constructor args). In that mode we don't
        have the original opener's id or the live-tick state handle, so
        interactions get a friendly "re-open the panel" hint.
        """
        return self.owner_id is None or self.cog is None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Rehydrated stub (panel was opened in a previous bot session,
        # the in-memory state is gone): the buttons would fail with no
        # cog reference / no owner_id, so short-circuit with a clean
        # ephemeral hint instead of producing "this interaction failed".
        if self._is_rehydrated():
            try:
                await interaction.response.send_message(
                    "This buddy panel was opened in a previous session "
                    "and has expired. Run `,buddy` to open a fresh one.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return False
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This buddy isn't yours.", ephemeral=True,
            )
            return False
        return True

    async def _load_pages(
        self, guild_id: int,
    ) -> tuple[list[dict], int, dict | None]:
        """Fetch the owned buddies and snap page_idx into range.

        Returns (pages, page_idx, current_row). ``current_row`` is None
        only if the user owns nothing.
        """
        pages = await _fetch_all_owned(self.cog.bot.db, guild_id, self.owner_id)
        if not pages:
            self.page_idx = 0
            return [], 0, None
        self.page_idx = max(0, min(self.page_idx, len(pages) - 1))
        return pages, self.page_idx, pages[self.page_idx]

    def _apply_button_states(self, current: dict | None, page_total: int) -> None:
        """Enable / disable buttons based on what page is showing."""
        is_active = bool(current and current.get("is_active"))
        # Mood interactions only work on the active buddy.
        self.feed_btn.disabled = not is_active
        self.pet_btn.disabled  = not is_active
        self.talk_btn.disabled = not is_active
        # Navigation needs at least two pages.
        self.prev_btn.disabled = page_total <= 1
        self.next_btn.disabled = page_total <= 1
        # Set Active is off when the page is already active or the user has no pet.
        self.set_active_btn.disabled = current is None or is_active

    def _rebuild_buddy_select(self, pages: list[dict]) -> None:
        """Drop any prior _BuddySelect and add a fresh one for ``pages``.

        Called from every re-render so the dropdown matches the current
        owned-buddy list. New buddies show up immediately; sold / gifted
        buddies disappear without a stale entry.
        """
        for child in list(self.children):
            if isinstance(child, _BuddySelect):
                self.remove_item(child)
        self.add_item(_BuddySelect(self, pages))

    async def _re_render(
        self,
        interaction: discord.Interaction,
        *,
        action_line: str = "",
    ) -> None:
        pages, idx, current = await self._load_pages(interaction.guild_id)
        if not current:
            self._apply_button_states(None, 0)
            self._rebuild_buddy_select([])
            await interaction.response.edit_message(
                embed=card("Buddy gone", color=C_AMBER).description(
                    "You don't have any buddies right now.",
                ).build(),
                view=self,
            )
            return
        self._apply_button_states(current, len(pages))
        self._rebuild_buddy_select(pages)
        embed = _build_panel_embed(
            current, self.state_id,
            page_idx=idx, page_total=len(pages), action_line=action_line,
            owner_progression=self.owner_progression,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def _action(
        self,
        interaction: discord.Interaction,
        action: str,
        cooldown_s: int,
        hunger_delta: int,
        happiness_delta: int,
        energy_delta: int,
        flavor: str,
    ) -> None:
        """Feed/Pet/Talk: operate on the ACTIVE buddy regardless of which
        page is currently shown (the button is disabled on inactive pages,
        so we only land here when the page IS the active one, but the
        WHERE clause is still active-scoped for safety).

        Talk routes through the AI reply service so the buddy sounds like
        itself; feed / pet use the caller-supplied canned ``flavor`` line.
        All three append an event to the buddy's ai_memory so future
        replies recall recent interactions.
        """
        now = time.time()
        per_action = _action_last.setdefault(self.state_id, {})
        last = per_action.get(action, 0.0)
        if now - last < cooldown_s:
            wait = int(cooldown_s - (now - last))
            await interaction.response.send_message(
                f"{action.title()} is on cooldown. Try again in **{wait}s**.",
                ephemeral=True,
            )
            return

        db = self.cog.bot.db
        # Buddies on expedition are unreachable -- block feed/pet/talk
        # until they're back. The cooldown stamp is INTENTIONALLY skipped
        # so the player isn't punished by retry attempts.
        active_check = await _fetch_active(db, interaction.guild_id, self.owner_id)
        busy_msg = _expedition_busy_message(active_check or {})
        if busy_msg:
            await interaction.response.send_message(busy_msg, ephemeral=True)
            return

        per_action[action] = now

        # Apex Mastery: Hearty Meals (utility.feed_efficiency) scales
        # the FEED action's hunger delta. Pet and talk don't restore
        # hunger, so the passive only affects feed.
        if action == "feed" and hunger_delta > 0:
            try:
                from services import mastery as _mastery
                _mp = await _mastery.passives(
                    db, self.owner_id, interaction.guild_id,
                )
                _bonus = float(_mp.get("utility.feed_efficiency") or 0.0)
                if _bonus > 0:
                    hunger_delta = int(round(hunger_delta * (1.0 + _bonus)))
            except Exception:
                log.debug(
                    "utility.feed_efficiency passive read failed",
                    exc_info=True,
                )

        updated = await db.fetch_one(
            "UPDATE cc_buddies SET "
            "  hunger    = GREATEST(0, LEAST(100, hunger    + $3)), "
            "  happiness = GREATEST(0, LEAST(100, happiness + $4)), "
            "  energy    = GREATEST(0, LEAST(100, energy    + $5)), "
            "  last_interacted_at = NOW(), "
            "  updated_at = NOW() "
            "WHERE guild_id = $1 AND owner_user_id = $2 "
            "  AND status = 'owned' AND is_active "
            "RETURNING *",
            interaction.guild_id, self.owner_id,
            hunger_delta, happiness_delta, energy_delta,
        )
        if not updated:
            await interaction.response.send_message(
                "Your active buddy is no longer here.", ephemeral=True,
            )
            return

        frame_label = {"feed": "eating", "pet": "petted", "talk": "talking"}[action]
        _action_override[self.state_id] = (frame_label, now + ACTION_OVERRIDE_S)
        self.cog.bot.live.pause(self.state_id, seconds=ACTION_OVERRIDE_S)

        # The Talk BUTTON stays on canned dialogue so the re-render fits
        # inside Discord's 3s interaction deadline. The AI-backed version
        # lives on the `,buddy talk [message]` prefix command, which
        # shares this cooldown so users can't stack the two. Feed / pet
        # use the caller-supplied canned flavor.
        try:
            await record_event(
                db, int(updated["id"]), action,
                summary={
                    "feed": "owner fed me",
                    "pet":  "owner pet me",
                    "talk": "owner talked via the panel button",
                }[action],
            )
        except Exception:
            log.debug("buddy action: record_event failed", exc_info=True)

        # FREN drop on every interaction. Amount scales with the buddy's
        # level, rarity tier, and CURRENT happiness (so a sad buddy drops
        # less). 50% chance per interaction with the full payout; on the
        # other 50% a tiny consolation drop fires so the player ALWAYS
        # sees feedback that talk/feed/pet feeds their FREN balance.
        # Best-effort: any failure here is silent so the user-visible
        # interaction still completes normally.
        fren_drop_msg = ""
        try:
            import random as _r
            lvl_b   = effective_level(updated)
            tier_b  = int(updated.get("rarity_tier") or 1)
            happy_b = int(updated.get("happiness") or 0)
            # Base 0.5 FREN; +10% per level; rarity multiplier
            # (1.0 / 1.3 / 1.6 / 2.0 / 2.5 for tiers 1-5);
            # happiness scales 0..1 so a 0-mood buddy gives nothing
            # extra and a 100-mood buddy gives full reward.
            rarity_mult = (1.0, 1.3, 1.6, 2.0, 2.5)[max(0, min(4, tier_b - 1))]
            happy_mult  = max(0.0, min(1.0, happy_b / 100.0))
            full_amount = (
                0.5
                * (1.0 + 0.10 * max(0, lvl_b - 1))
                * rarity_mult
                * (0.25 + 0.75 * happy_mult)
            )
            # 50% chance for the full drop; otherwise a 10% consolation
            # so feedback is always visible without spamming the high end.
            roll = _r.random()
            if roll < 0.5:
                fren_amount = full_amount
                drop_kind = "lucky"
            else:
                fren_amount = full_amount * 0.10
                drop_kind = "tip"
            fren_amount = round(fren_amount, 4)
            if fren_amount > 0:
                from services import buddy_economy as _bes
                raw = to_raw(fren_amount)
                await db.update_wallet_holding(
                    int(self.owner_id), int(interaction.guild_id),
                    _bes.BUD_NETWORK_SHORT, _bes.FREN_SYMBOL, int(raw),
                )
                fren_meta = Config.TOKENS.get("FREN", {}) or {}
                fren_emoji = fren_meta.get("emoji") or "\U0001F49E"
                tag = "lucky drop" if drop_kind == "lucky" else "tip"
                fren_drop_msg = (
                    f"\n{fren_emoji} **+{fren_amount:,.4f} FREN** "
                    f"({tag} -- Lv{lvl_b} buddy, "
                    f"happiness {happy_b}/100)"
                )
        except Exception:
            log.debug("buddy action: FREN drop failed", exc_info=True)

        await self._re_render(interaction, action_line=flavor + fren_drop_msg)

    @discord.ui.button(
        label="Feed", style=discord.ButtonStyle.success, row=0,
        custom_id="buddy_panel:feed",
    )
    async def feed_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._action(
            interaction, "feed", FEED_COOLDOWN_S,
            FEED_HUNGER_DELTA, FEED_HAPPINESS_DELTA, FEED_ENERGY_DELTA,
            "You fed your buddy. It looks satisfied and a bit perkier.",
        )

    @discord.ui.button(
        label="Pet", style=discord.ButtonStyle.primary, row=0,
        custom_id="buddy_panel:pet",
    )
    async def pet_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._action(
            interaction, "pet", PET_COOLDOWN_S,
            0, PET_HAPPINESS_DELTA, PET_ENERGY_DELTA,
            "You pet your buddy. Little happy noises.",
        )

    @discord.ui.button(
        label="Talk", style=discord.ButtonStyle.primary, row=0,
        custom_id="buddy_panel:talk",
    )
    async def talk_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        db = self.cog.bot.db
        row = await _fetch_active(db, interaction.guild_id, self.owner_id)
        species = str(row.get("species") or "") if row else ""
        lines = list(SPECIES.get(species, {}).get("dialogue") or [])
        flavor = random.choice(lines) if lines else "Your buddy looks at you expectantly."
        await self._action(
            interaction, "talk", TALK_COOLDOWN_S,
            0, TALK_HAPPINESS_DELTA, TALK_ENERGY_DELTA,
            flavor,
        )

    @discord.ui.button(
        label="Rename", style=discord.ButtonStyle.secondary, row=1,
        custom_id="buddy_panel:rename",
    )
    async def rename_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        pages, _idx, current = await self._load_pages(interaction.guild_id)
        if not current:
            await interaction.response.send_message(
                "You don't have any buddies.", ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RenameModal(
                self.cog, self.owner_id,
                int(current["id"]),
                str(current.get("name") or ""),
            ),
        )

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=1,
        custom_id="buddy_panel:refresh",
    )
    async def refresh_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._re_render(interaction)

    @discord.ui.button(label="List on AH", emoji="\U0001F3DB",
                       style=discord.ButtonStyle.primary, row=1,
                       custom_id="buddy_panel:list_ah")
    async def list_ah_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        # Pop a price modal for the panel's CURRENT-page buddy. Routes
        # through the same NFT-driven create_listing_by_token flow as
        # ,items inspect "List on AH" so safeguards stay in place.
        pages, _idx, current = await self._load_pages(interaction.guild_id)
        if not current:
            await interaction.response.send_message(
                "No buddy on this page.", ephemeral=True,
            )
            return
        buddy_id = int(current.get("id") or 0)
        if buddy_id <= 0:
            await interaction.response.send_message(
                "Buddy id missing.", ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            _BuddyListAHModal(self, buddy_id),
        )

    @discord.ui.button(
        label="Prev", emoji="◀",
        style=discord.ButtonStyle.secondary, row=2,
        custom_id="buddy_panel:prev",
    )
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        pages, _idx, _cur = await self._load_pages(interaction.guild_id)
        if len(pages) >= 2:
            self.page_idx = (self.page_idx - 1) % len(pages)
        await self._re_render(interaction)

    @discord.ui.button(
        label="Next", emoji="▶",
        style=discord.ButtonStyle.secondary, row=2,
        custom_id="buddy_panel:next",
    )
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        pages, _idx, _cur = await self._load_pages(interaction.guild_id)
        if len(pages) >= 2:
            self.page_idx = (self.page_idx + 1) % len(pages)
        await self._re_render(interaction)

    @discord.ui.button(
        label="Set Active", style=discord.ButtonStyle.success, row=2,
        custom_id="buddy_panel:set_active",
    )
    async def set_active_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        pages, _idx, current = await self._load_pages(interaction.guild_id)
        if not current:
            await interaction.response.send_message(
                "You don't have any buddies.", ephemeral=True,
            )
            return
        ok, err, _new_row = await set_active_buddy(
            self.cog.bot.db,
            interaction.guild_id,
            self.owner_id,
            int(current["id"]),
        )
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await self._re_render(
            interaction,
            action_line=f"**{current.get('name') or 'Your buddy'}** is now active. Chat XP and mood now flow into them.",
        )


# =============================================================================
# Interactive PvP buddy battle
# =============================================================================
# ,buddy battle used to auto-resolve via services.buddy_battle.run_battle the
# moment the opponent accepted. The new Pokemon-style flow lets BOTH players
# pick an action per round (Strike / Special / Brace / Risky) and resolves
# in speed order.
#
#   1. Both players see the same round embed (HP bars, stamina, last log).
#   2. Each clicks ONE of four action buttons. interaction_check allows both
#      challenger + opponent; per-side action is locked in until the round
#      resolves.
#   3. When BOTH are locked in, the round resolves in Fighter.spd order
#      (faster goes first; the slower's action is wasted if the faster's
#      hit drops them).
#   4. Brace is one-shot: it halves the next incoming hit AND heals 8% HP.
#      Risky is high-variance: 60% big hit / 25% miss / 15% self-damage.
#      Special costs 2 stamina, gained +1 per Strike or Brace (cap 5).
#   5. First to <=0 HP loses. ROUND_CAP forces a verdict on remaining-HP-%.
#
# Returns a services.buddy_battle.BattleResult so the existing escrow /
# stake-settle / XP-award wrapper in buddy_battle drops in unchanged.

import dataclasses as _dc

# Action damage windows are owned by services/buddy_battle.py so PvP and
# PvE (map / boss / tournament) share a single source of truth -- a change
# to Strike/Special/Risky/Brace tuning applies to every battle type.
from services.buddy_battle import (
    PVE_BRACE_HEAL    as _PVP_BRACE_HEAL,
    PVE_RISKY_HIT     as _PVP_RISKY_HIT,
    PVE_RISKY_MISS    as _PVP_RISKY_MISS,
    PVE_RISKY_RANGE   as _PVP_RISKY_RANGE,
    PVE_SPECIAL_COST  as _PVP_SPECIAL_COST,
    PVE_SPECIAL_RANGE as _PVP_SPECIAL_RANGE,
    PVE_STAMINA_MAX   as _PVP_STAMINA_MAX,
    PVE_STRIKE_RANGE  as _PVP_STRIKE_RANGE,
)
_PVP_BATTLE_MAX_ROUNDS = 25

# Per-round timeout. Generous because two humans + decision time.
_PVP_ROUND_TIMEOUT_S = 90


@_dc.dataclass
class _PvpBattle:
    """In-memory turn-state for an interactive PvP buddy battle."""
    p1: "Any"   # services.buddy_battle.Fighter
    p2: "Any"
    p1_user_id: int
    p2_user_id: int
    p1_stamina: int = 0
    p2_stamina: int = 0
    p1_brace_next: bool = False
    p2_brace_next: bool = False
    # Actions locked in for the CURRENT round; cleared after resolve.
    p1_action: str | None = None
    p2_action: str | None = None
    # Lifetime per-side action history (used for any future variety bonus).
    p1_actions_used: set = _dc.field(default_factory=set)
    p2_actions_used: set = _dc.field(default_factory=set)
    round_num: int = 1
    log_lines: list = _dc.field(default_factory=list)

    def __post_init__(self) -> None:
        # Endurance Charm bonus: each fighter's gear-derived
        # start_stamina applies once at battle start.
        for slot, fighter in (("p1", self.p1), ("p2", self.p2)):
            bonus = int(getattr(fighter, "start_stamina_bonus", 0) or 0)
            if bonus > 0:
                cur = self.p1_stamina if slot == "p1" else self.p2_stamina
                new = min(_PVP_STAMINA_MAX, cur + bonus)
                if slot == "p1":
                    self.p1_stamina = new
                else:
                    self.p2_stamina = new

    def both_locked(self) -> bool:
        return self.p1_action is not None and self.p2_action is not None

    def is_over(self) -> bool:
        return (self.p1.hp <= 0 or self.p2.hp <= 0
                or self.round_num > _PVP_BATTLE_MAX_ROUNDS)

    def winner(self) -> "Any | None":
        """Return the winning Fighter, or None for a draw / mutual KO."""
        if self.p1.hp <= 0 and self.p2.hp <= 0:
            return None
        if self.p1.hp <= 0:
            return self.p2
        if self.p2.hp <= 0:
            return self.p1
        # Round cap timeout: winner = higher HP%.
        p1_pct = self.p1.hp / max(1, self.p1.max_hp)
        p2_pct = self.p2.hp / max(1, self.p2.max_hp)
        if p1_pct > p2_pct:
            return self.p1
        if p2_pct > p1_pct:
            return self.p2
        return None

    def loser(self) -> "Any | None":
        w = self.winner()
        if w is None:
            return None
        return self.p2 if w is self.p1 else self.p1


def _pvp_hp_bar(cur: int, mx: int, width: int = 12) -> str:
    """ASCII HP bar with percentage."""
    if mx <= 0:
        return f"[{'░' * width}]   0%"
    cur = max(0, min(cur, mx))
    pct = cur / mx
    filled = int(round(width * pct))
    return f"[{'█' * filled}{'░' * (width - filled)}] {int(pct * 100):3d}%"


def _pvp_heal_with_cap(f: "Any", amount: int) -> int:
    """PvP-side mirror of services.buddy_battle._heal_fighter.

    Honors the per-fighter heal_total soft cap so a healer can't
    repeatedly out-sustain damage in a long PvP. Returns actual HP
    healed (0 if the fighter is at full or KO'd).
    """
    if amount <= 0 or f.hp <= 0:
        return 0
    try:
        from configs.buddies_config import BATTLE_HEAL_SOFT_CAP_PCT
    except Exception:
        BATTLE_HEAL_SOFT_CAP_PCT = 1.20  # type: ignore
    cap = int(BATTLE_HEAL_SOFT_CAP_PCT * f.max_hp)
    cur_total = int(getattr(f, "heal_total", 0) or 0)
    if cur_total >= cap:
        amount = max(1, amount // 2)
    old = f.hp
    f.hp = min(f.max_hp, f.hp + amount)
    actual = f.hp - old
    setattr(f, "heal_total", cur_total + actual)
    return actual


def _pvp_post_damage_triggers(
    attacker: "Any", defender: "Any", lines: list[str],
) -> None:
    """Run the engine's post-damage status triggers on the defender.

    Mirrors the wolf low_hp_rage, wecco preen_heal, berserker, and
    second_wind hooks from services.buddy_battle._apply_hit. Called
    manually from _pvp_apply after every damage event so PvP fights
    honour the same triggers the auto-run engine fires.
    """
    a_emoji = attacker.emoji or "\U0001F436"  # noqa: F841 (kept for symmetry)
    d_emoji = defender.emoji or "\U0001F436"

    # Wolf: low-HP rage arms the ATK buff once, permanently.
    if (
        defender.low_hp_rage_pending
        and defender.hp > 0
        and defender.hp * 2 < defender.max_hp
    ):
        defender.atk_mult *= 1.0 + defender.low_hp_rage_bonus
        defender.low_hp_rage_pending = False
        bonus_pct = int(round(defender.low_hp_rage_bonus * 100))
        lines.append(
            f"\U0001F43A {d_emoji} {defender.name} howls -- **Pack Howl** "
            f"flips on (ATK +{bonus_pct}%)."
        )
    # Berserker (level-gated tertiary): triggers earlier than Pack Howl
    # and stacks with it. Mirrors services.buddy_battle._apply_hit.
    if (
        getattr(defender, "berserker_pending", False)
        and defender.hp > 0
        and defender.hp / max(1, defender.max_hp)
            < float(getattr(defender, "berserker_thresh", 0.40))
    ):
        bonus = float(getattr(defender, "berserker_bonus", 0.25))
        defender.atk_mult *= 1.0 + bonus
        defender.berserker_pending = False
        lines.append(
            f"\U0001F525 {d_emoji} {defender.name} goes **berserk** "
            f"(ATK +{int(round(bonus * 100))}%)."
        )
    # Wecco: preen heals once when pushed below 30% HP, also buffs ATK.
    # Threshold matches the auto-run engine after the healer rebalance.
    if (
        defender.ability_key == "preen_heal"
        and not defender.preen_used
        and defender.hp > 0
        and defender.hp * 100 < defender.max_hp * 30
    ):
        defender.preen_used = True
        heal_to = int(round(defender.max_hp * defender.preen_heal_pct))
        if heal_to > defender.hp:
            _pvp_heal_with_cap(defender, heal_to - defender.hp)
        defender.atk_mult *= 1.0 + defender.preen_atk_bonus
        bonus_pct = int(round(defender.preen_atk_bonus * 100))
        lines.append(
            f"\U0001FAB6 {d_emoji} {defender.name} preens -- **Preen** "
            f"triggers, healed to {defender.hp}/{defender.max_hp} "
            f"and ATK +{bonus_pct}%."
        )
    # Second Wind (Lv 30 tertiary on some species): one-shot 25% heal
    # the first time the defender drops below 30% HP.
    if (
        getattr(defender, "second_wind_pending", False)
        and not getattr(defender, "second_wind_used", False)
        and defender.hp > 0
        and defender.hp / max(1, defender.max_hp) < 0.30
    ):
        defender.second_wind_used = True
        actual = _pvp_heal_with_cap(
            defender, max(1, int(round(defender.max_hp * 0.25))),
        )
        lines.append(
            f"\U0001F4A8 {d_emoji} **Second Wind** -- {defender.name} "
            f"recovers **{actual}** HP "
            f"({defender.hp}/{defender.max_hp})."
        )


def _pvp_check_proc_on_hit(
    attacker: "Any", defender: "Any", lines: list[str],
    *, dmg_dealt: int = 0,
) -> None:
    """Roll attacker's on-hit proc abilities (poison, stun, static, reflect)
    onto defender, plus the defender's reactive abilities (counter).

    Mirrors services.buddy_battle._maybe_proc_on_hit + the reflect /
    static_shock branches in _apply_hit. ``dmg_dealt`` is the actual
    HP delivered (after mitigation + brace) so reflect / static scale
    against real damage.
    """
    import random as _r
    a_emoji = attacker.emoji or "\U0001F436"
    d_emoji = defender.emoji or "\U0001F436"

    # Jolt static_shock: bonus damage proc on hit.
    if (getattr(attacker, "static_proc_chance", 0.0) > 0
            and defender.hp > 0 and dmg_dealt > 0):
        if _r.random() < attacker.static_proc_chance:
            mult = float(getattr(attacker, "static_bonus_mult", 0.50) or 0.50)
            bonus = max(1, int(round(dmg_dealt * mult)))
            defender.hp = max(0, defender.hp - bonus)
            lines.append(
                f"⚡ {a_emoji} **Static Shock** arcs for **{bonus}** bonus "
                f"dmg ({defender.name}: {defender.hp}/{defender.max_hp})."
            )

    # Tortuga / Phantom reflect: defender bounces a fraction of the
    # delivered damage back at the attacker.
    if (getattr(defender, "reflect_pct", 0.0) > 0
            and defender.hp > 0 and attacker.hp > 0 and dmg_dealt > 0):
        reflected = max(1, int(round(dmg_dealt * defender.reflect_pct)))
        attacker.hp = max(0, attacker.hp - reflected)
        lines.append(
            f"  \U0001F6E1️ {d_emoji} **{defender.name}** reflects "
            f"**{reflected}** dmg back ({attacker.name}: "
            f"{attacker.hp}/{attacker.max_hp})."
        )

    if (getattr(attacker, "poison_proc_chance", 0.0) > 0
            and defender.poison_turns == 0):
        if _r.random() < attacker.poison_proc_chance:
            defender.poison_turns = 3
            lines.append(
                f"\U0001F9EA {d_emoji} {defender.name} is **poisoned** "
                f"(3 turns)."
            )
    if (getattr(attacker, "stun_proc_chance", 0.0) > 0
            and defender.stunned_turns == 0):
        if _r.random() < attacker.stun_proc_chance:
            defender.stunned_turns = 1
            lines.append(
                f"\U0001F4AB {d_emoji} {defender.name} is **stunned** "
                f"(1 turn)."
            )
    # Thornling counter: defender retaliates when hit.
    if (getattr(defender, "counter_chance", 0.0) > 0
            and defender.hp > 0 and attacker.hp > 0):
        if _r.random() < defender.counter_chance:
            c_pct = float(getattr(defender, "counter_pct", 0.50))
            atk_mult = float(getattr(defender, "atk_mult", 1.0) or 1.0)
            c_dmg = max(1, int(round(defender.atk * atk_mult * c_pct)))
            attacker.hp = max(0, attacker.hp - c_dmg)
            lines.append(
                f"  {d_emoji} **Prickle Back** -- {defender.name} "
                f"retaliates for **{c_dmg}** dmg "
                f"({attacker.name}: {attacker.hp}/{attacker.max_hp})."
            )


def _pvp_apply(b: _PvpBattle, slot: int, action: str) -> list[str]:
    """Apply ``action`` from ``slot`` (1 or 2) onto the opposing side.

    Engine integration points (mirrors services.buddy_battle):
      * pre-turn one-shot abilities fire BEFORE the action
        (ink_atk_debuff_20 cuts opponent ATK, rain_skip_2 stuns opponent)
      * stun_turns on the attacker skips their action this round
      * crit chance applies on every hit (Strike / Special / Risky) via
        the engine's BATTLE_CRIT_BASE + spd-scaled formula
      * defender.dmg_taken_mult mitigates the damage delivered (shell
        species etc.)
      * post-damage hooks: wolf low_hp_rage and wecco preen_heal
      * on-hit procs: poison + stun chance

    Brace remains a PvP-only action (not in the engine). Risky is
    PvP-only too. Strike + Special honour every engine ability for
    consistent species power across PvP and wild fights.

    Returns log lines for THIS action. Mutates ``b`` (and the Fighter
    objects on it) for HP / stamina / brace / status / action history.
    """
    import random as _r
    if slot == 1:
        atk, defn = b.p1, b.p2
        atk_stam = b.p1_stamina
        atk_actions = b.p1_actions_used
        defn_brace = b.p2_brace_next
    else:
        atk, defn = b.p2, b.p1
        atk_stam = b.p2_stamina
        atk_actions = b.p2_actions_used
        defn_brace = b.p1_brace_next

    atk_actions.add(action)
    a_emoji = atk.emoji or "\U0001F436"
    d_emoji = defn.emoji or "\U0001F436"
    lines: list[str] = []

    def _consume_defender_brace() -> None:
        if slot == 1:
            b.p2_brace_next = False
        else:
            b.p1_brace_next = False

    def _set_attacker_brace(v: bool) -> None:
        if slot == 1:
            b.p1_brace_next = v
        else:
            b.p2_brace_next = v

    def _commit_attacker_stamina(s: int) -> None:
        if slot == 1:
            b.p1_stamina = s
        else:
            b.p2_stamina = s

    # Pre-turn one-shot abilities: shrimp ink debuff (atk_mult cut on
    # defender) + nimbus rain skip (defender stunned 2 turns). These
    # only fire ONCE per battle per fighter; the engine's flag
    # (ink_used / rain_used) gates that.
    try:
        from services.buddy_battle import _maybe_fire_pre_turn_ability
        _maybe_fire_pre_turn_ability(atk, defn, lines)
    except Exception:
        log.debug("pvp: pre-turn ability hook failed", exc_info=True)

    # Stunned attacker can't act. Decrement counter so the next round
    # they can resume. Stamina + brace don't tick during a stun.
    if getattr(atk, "stunned_turns", 0) > 0:
        atk.stunned_turns -= 1
        lines.append(
            f"\U0001F4AB {a_emoji} **{atk.name}** is stunned -- skips "
            f"this turn."
        )
        return lines

    def _delivered_damage(raw: int) -> int:
        """Apply defender mitigation (shell etc.) + brace halving."""
        m = float(getattr(defn, "dmg_taken_mult", 1.0) or 1.0)
        out = max(1, int(round(raw * m)))
        if defn_brace:
            out = max(1, out // 2)
            lines.append(f"{d_emoji} braced -- damage halved.")
            _consume_defender_brace()
        return out

    def _maybe_crit_mult() -> tuple[float, bool]:
        """Engine-style crit roll: BATTLE_CRIT_BASE + spd scaling +
        any level-gated lucky_crit / battle_focus bonuses on the attacker.
        """
        try:
            from services.buddy_battle import _crit_chance, _crit_mult
            chance = float(_crit_chance(atk))
            if _r.random() < chance:
                return float(_crit_mult(atk)), True
        except Exception:
            pass
        return 1.0, False

    def _killing_blow(raw_dmg: float) -> float:
        """Apply level-gated killing_blow bonus when defender HP is low.

        Stacks ADDITIVELY with draclet's execute_30 -- a Lv 30 draclet
        with the killing_blow tertiary lights up both bonuses below
        25% HP.
        """
        bonus_pct = float(getattr(atk, "killing_blow_bonus_pct", 0.0))
        thresh = float(getattr(atk, "killing_blow_thresh", 0.0))
        if bonus_pct > 0 and thresh > 0 and defn.hp > 0:
            if defn.hp / max(1, defn.max_hp) < thresh:
                bonus = max(1, int(round(raw_dmg * bonus_pct)))
                lines.append(
                    f"  {a_emoji} **Killing Blow** -- +{bonus} bonus dmg!"
                )
                return raw_dmg + bonus
        return raw_dmg

    def _pvp_execute(raw_dmg: float) -> float:
        """Apply draclet execute bonus when defender HP is low."""
        if getattr(atk, "execute_thresh", 0.0) > 0 and defn.hp > 0:
            if defn.hp / max(1, defn.max_hp) < atk.execute_thresh:
                bonus_pct = float(getattr(atk, "execute_bonus_pct", 0.0))
                bonus = max(1, int(round(raw_dmg * bonus_pct)))
                lines.append(
                    f"  {a_emoji} **Death Grip** execute -- "
                    f"+{bonus} bonus!"
                )
                return raw_dmg + bonus
        return raw_dmg

    def _pvp_lifesteal(dmg_dealt: int) -> None:
        """Heal attacker for % of damage dealt (blazer). Honors the
        heal soft-cap so the buddy can't out-sustain damage forever.
        """
        ls = float(getattr(atk, "lifesteal_pct", 0.0))
        if ls > 0 and dmg_dealt > 0 and atk.hp > 0:
            steal = max(1, int(round(dmg_dealt * ls)))
            actual = _pvp_heal_with_cap(atk, steal)
            if actual > 0:
                lines.append(
                    f"  {a_emoji} **Flame Drain** siphons **{actual}** HP "
                    f"({atk.hp}/{atk.max_hp})."
                )

    if action == "strike":
        lo, hi = _PVP_STRIKE_RANGE
        # atk_mult is the engine's persistent ATK modifier (preen +
        # low-HP rage stack into it). Honor it on every hit.
        atk_mult = float(getattr(atk, "atk_mult", 1.0) or 1.0)
        crit_mult, is_crit = _maybe_crit_mult()
        raw = atk.atk * atk_mult * _r.uniform(lo, hi) * crit_mult
        raw = _pvp_execute(raw)
        raw = _killing_blow(raw)
        dmg = _delivered_damage(max(1, int(round(raw))))
        defn.hp = max(0, defn.hp - dmg)
        atk_stam = min(_PVP_STAMINA_MAX, atk_stam + 1)
        crit_tag = "  **CRIT!**" if is_crit else ""
        lines.append(
            f"{a_emoji} **{atk.name}** strikes for **{dmg}** dmg{crit_tag}  "
            f"({defn.name}: {defn.hp}/{defn.max_hp})"
        )
        _pvp_lifesteal(dmg)
        _pvp_check_proc_on_hit(atk, defn, lines, dmg_dealt=dmg)
        _pvp_post_damage_triggers(atk, defn, lines)

    elif action == "special":
        if atk_stam < _PVP_SPECIAL_COST:
            lines.append(
                f"{a_emoji} {atk.name} tries Special but lacks stamina!"
            )
        else:
            atk_stam -= _PVP_SPECIAL_COST
            lo, hi = _PVP_SPECIAL_RANGE
            atk_mult = float(getattr(atk, "atk_mult", 1.0) or 1.0)
            crit_mult, is_crit = _maybe_crit_mult()
            raw = atk.atk * atk_mult * _r.uniform(lo, hi) * crit_mult
            raw = _pvp_execute(raw)
            raw = _killing_blow(raw)
            dmg = _delivered_damage(max(1, int(round(raw))))
            defn.hp = max(0, defn.hp - dmg)
            ability = (atk.ability_name or "Special").strip() or "Special"
            crit_tag = "  **CRIT!**" if is_crit else ""
            lines.append(
                f"\U0001F4A5 {a_emoji} **{atk.name}** unleashes **{ability}** "
                f"for **{dmg}** dmg{crit_tag}  "
                f"({defn.name}: {defn.hp}/{defn.max_hp})"
            )
            _pvp_lifesteal(dmg)
            _pvp_check_proc_on_hit(atk, defn, lines, dmg_dealt=dmg)
            _pvp_post_damage_triggers(atk, defn, lines)

    elif action == "brace":
        _set_attacker_brace(True)
        heal = max(1, int(round(atk.max_hp * _PVP_BRACE_HEAL)))
        actual = _pvp_heal_with_cap(atk, heal)
        atk_stam = min(_PVP_STAMINA_MAX, atk_stam + 1)
        lines.append(
            f"\U0001F6E1 {a_emoji} **{atk.name}** braces, healing **{actual}** "
            f"HP  ({atk.hp}/{atk.max_hp}); next hit halved."
        )

    elif action == "risky":
        roll = _r.random()
        if roll < _PVP_RISKY_HIT:
            lo, hi = _PVP_RISKY_RANGE
            atk_mult = float(getattr(atk, "atk_mult", 1.0) or 1.0)
            crit_mult, is_crit = _maybe_crit_mult()
            raw = atk.atk * atk_mult * _r.uniform(lo, hi) * crit_mult
            raw = _pvp_execute(raw)
            raw = _killing_blow(raw)
            dmg = _delivered_damage(max(1, int(round(raw))))
            defn.hp = max(0, defn.hp - dmg)
            crit_tag = "  **CRIT!**" if is_crit else ""
            lines.append(
                f"\U0001F3AF {a_emoji} **{atk.name}** lands a RISKY hit for "
                f"**{dmg}** dmg{crit_tag}  "
                f"({defn.name}: {defn.hp}/{defn.max_hp})"
            )
            _pvp_lifesteal(dmg)
            _pvp_check_proc_on_hit(atk, defn, lines, dmg_dealt=dmg)
            _pvp_post_damage_triggers(atk, defn, lines)
        elif roll < _PVP_RISKY_HIT + _PVP_RISKY_MISS:
            lines.append(
                f"\U0001F4A8 {a_emoji} {atk.name} tries a Risky -- whiff!"
            )
        else:
            recoil = max(1, int(round(atk.atk * 0.45)))
            atk.hp = max(0, atk.hp - recoil)
            lines.append(
                f"\U0001F4A2 {a_emoji} {atk.name}'s Risky backfires for "
                f"**{recoil}** self-dmg  ({atk.hp}/{atk.max_hp})"
            )

    _commit_attacker_stamina(atk_stam)
    return lines


def _pvp_tick_poison(b: _PvpBattle) -> list[str]:
    """End-of-round poison DOT on both fighters.

    Mirrors services.buddy_battle._tick_poison. Called from
    _PvpBattleView._resolve_round AFTER both actions have landed so a
    fighter who poisoned their opponent THIS round still sees the
    first tick the same round.
    """
    lines: list[str] = []
    for f in (b.p1, b.p2):
        if f.hp <= 0 or getattr(f, "poison_turns", 0) <= 0:
            continue
        dmg = max(1, int(round(f.max_hp * 0.05)))
        f.hp = max(0, f.hp - dmg)
        f.poison_turns -= 1
        emoji = f.emoji or "\U0001F436"
        lines.append(
            f"\U0001F9EA {emoji} {f.name} takes **{dmg}** poison dmg  "
            f"({f.hp}/{f.max_hp}, {f.poison_turns} turn(s) left)"
        )
    return lines


def _pvp_tick_round_effects(b: _PvpBattle) -> list[str]:
    """Apply per-round passive effects for both fighters.

    Mirrors services.buddy_battle.apply_round_effects but operates on
    _PvpBattle / Fighter objects. Call after _pvp_tick_poison each round.

    Regen is capped at BATTLE_REGEN_HP_CAP_PCT of max HP so healers
    can't sit at 100% topping up between hits. Overclock cadence is
    species-specific (verdant ramps every round, robo every 3rd).
    """
    try:
        from configs.buddies_config import BATTLE_REGEN_HP_CAP_PCT as _REGEN_CAP
    except Exception:
        _REGEN_CAP = 0.75  # type: ignore
    lines: list[str] = []
    for f in (b.p1, b.p2):
        if f.hp <= 0:
            continue
        emoji = f.emoji or "\U0001F436"
        # Per-round regen (gloomer Lunar Regen, verdant Photo Synth,
        # swift_recovery secondary). Capped at the regen-HP threshold.
        regen = float(getattr(f, "regen_pct", 0.0))
        if regen > 0 and (f.hp / max(1, f.max_hp)) < _REGEN_CAP:
            heal = max(1, int(round(f.max_hp * regen)))
            actual = _pvp_heal_with_cap(f, heal)
            if actual > 0:
                label = ("Lunar Regen" if f.ability_key == "regen_3pct"
                         else "Photo Synth" if f.ability_key == "photo_synth"
                         else "Swift Recovery")
                lines.append(
                    f"  {emoji} **{label}** restores **{actual}** HP "
                    f"({f.hp}/{f.max_hp})."
                )
        # Per-round ATK ramp. Cadence lives on Fighter.atk_up_every_n_rounds.
        atk_per = float(getattr(f, "atk_up_per_stack", 0.0))
        max_stacks = int(getattr(f, "atk_up_max_stacks", 3))
        cadence = max(1, int(getattr(f, "atk_up_every_n_rounds", 3) or 3))
        if atk_per > 0:
            cur_stacks = int(getattr(f, "atk_up_stacks", 0))
            if b.round_num % cadence == 0 and cur_stacks < max_stacks:
                cur_stacks += 1
                f.atk_up_stacks = cur_stacks
                gain = f.atk * atk_per
                f.atk += gain
                setattr(f, "atk_mult", float(getattr(f, "atk_mult", 1.0)) + atk_per)
                label = "Overclock" if f.ability_key == "atk_up_3rounds" else "Photo Synth"
                lines.append(
                    f"  {emoji} **{label}** stack {cur_stacks}/{max_stacks} -- "
                    f"ATK +{int(round(gain))}!"
                )
    return lines


async def _play_pvp_action_burst(
    view: "discord.ui.View",
    b: _PvpBattle,
    p1_user: "discord.abc.User",
    p2_user: "discord.abc.User",
    *,
    actor_slot: int,
    action: str,
) -> None:
    """Play the per-move attack burst on a PvP / wild battle view.

    Thin wrapper around the shared ``play_battle_action_burst`` helper
    in ``services.buddy_battle_scene`` so PvP, wild buddy, fishing,
    delve and arena map all share one burst implementation.
    """
    from services.buddy_battle_scene import play_battle_action_burst
    actor_side = "p1" if actor_slot == 1 else "p2"
    actor_fighter = b.p1 if actor_slot == 1 else b.p2
    ability_name = str(getattr(actor_fighter, "ability_name", "") or "")
    await play_battle_action_burst(
        view, b.p1, b.p2,
        actor_side=actor_side,
        action=str(action),
        round_num=int(b.round_num),
        max_rounds=_PVP_BATTLE_MAX_ROUNDS,
        ability_name=ability_name,
    )


def _pvp_battle_scene_state(
    b: _PvpBattle, p1_user: "discord.abc.User", p2_user: "discord.abc.User",
    *, action_banner: str = "",
) -> dict:
    """Adapter: convert a _PvpBattle into the dict shape that
    services.buddy_battle_scene.render_battle_frame expects.

    Delegates to the shared ``fighters_to_scene_state`` helper so PvP +
    wild buddy fights paint the same Pokemon-Stadium-style scene PNG
    used by arena map, tournament, fishing, delve, and farming -- one
    adapter across the entire game.
    """
    from services.buddy_battle_scene import fighters_to_scene_state
    return fighters_to_scene_state(
        b.p1, b.p2,
        round_num=int(b.round_num),
        max_rounds=_PVP_BATTLE_MAX_ROUNDS,
        action_banner=action_banner,
        is_player_turn=bool(getattr(b, "p1_action", None) is None),
    )


def _pvp_round_embed(
    b: _PvpBattle, p1_user: "discord.abc.User", p2_user: "discord.abc.User",
    *, opening: bool = False,
) -> tuple["discord.Embed", "discord.File"]:
    """Render the per-round combat panel showing both fighters.

    Returns ``(embed, scene_file)`` -- the embed references the scene PNG
    via attachment://battle.png so every battle entry point ships the
    same visual. Callers must pass the file via ``file=`` (or
    ``attachments=[file]`` for an edit) along with the embed.
    """
    p1_emoji = b.p1.emoji or "\U0001F436"
    p2_emoji = b.p2.emoji or "\U0001F436"
    p1_lock = "\U0001F512 *picked*" if b.p1_action else "\U0001F4A4 *picking...*"
    p2_lock = "\U0001F512 *picked*" if b.p2_action else "\U0001F4A4 *picking...*"

    tail_lines = [ln for ln in b.log_lines[-6:] if ln.strip()]
    if opening or not tail_lines:
        tail = "_Both players: pick your move..._"
    else:
        tail = "\n".join(tail_lines)

    p1_stam = ("●" * b.p1_stamina
               + "○" * (_PVP_STAMINA_MAX - b.p1_stamina))
    p2_stam = ("●" * b.p2_stamina
               + "○" * (_PVP_STAMINA_MAX - b.p2_stamina))

    desc_lines = [
        f"{p1_emoji} **{b.p1.name}**  Lv.{b.p1.level} {b.p1.tier_name}  "
        f"({p1_user.display_name})  {p1_lock}",
        f"  HP `{_pvp_hp_bar(b.p1.hp, b.p1.max_hp)}`  -  ATK {int(b.p1.atk)}",
        f"  Stamina `{p1_stam}` ({b.p1_stamina}/{_PVP_STAMINA_MAX})",
        "",
        f"{p2_emoji} **{b.p2.name}**  Lv.{b.p2.level} {b.p2.tier_name}  "
        f"({p2_user.display_name})  {p2_lock}",
        f"  HP `{_pvp_hp_bar(b.p2.hp, b.p2.max_hp)}`  -  ATK {int(b.p2.atk)}",
        f"  Stamina `{p2_stam}` ({b.p2_stamina}/{_PVP_STAMINA_MAX})",
        "",
        tail,
    ]
    if opening:
        desc_lines.append(
            f"-# Strike (+1 stam)  •  Special ({_PVP_SPECIAL_COST} stam)  "
            f"•  Brace (heal + halve next)  •  Risky (60% big / 25% miss / 15% backfire)"
        )

    # Build the battle scene PNG so PvP / wild / arena / map / delve all
    # share the same visual.
    import io as _io
    from services.buddy_battle_scene import render_battle_frame
    banner = "FIGHT!" if opening else ""
    state = _pvp_battle_scene_state(
        b, p1_user, p2_user, action_banner=banner,
    )
    png = render_battle_frame(state)
    scene_file = discord.File(_io.BytesIO(png), filename="battle.png")

    embed = (
        card(
            f"⚔️ Round {b.round_num}  -  {b.p1.name} vs {b.p2.name}",
            color=C_GOLD,
        )
        .description("\n".join(desc_lines))
        .image("attachment://battle.png")
        .build()
    )
    return embed, scene_file


class _PvpBattleView(discord.ui.View):
    """Pokemon-style turn-based PvP battle view.

    Both players are owners (interaction_check accepts either). Each
    has 4 action buttons; clicking locks in their action for the
    current round. When BOTH are locked, the view resolves the round
    in Fighter.spd order, updates the embed, and unlocks for the next.
    """

    def __init__(
        self, *, ctx: DiscoContext, p1_user: discord.abc.User,
        p2_user: discord.abc.User, p1: "Any", p2: "Any",
    ) -> None:
        super().__init__(timeout=_PVP_ROUND_TIMEOUT_S)
        self.ctx = ctx
        self.p1_user = p1_user
        self.p2_user = p2_user
        self.battle = _PvpBattle(
            p1=p1, p2=p2,
            p1_user_id=int(p1_user.id),
            p2_user_id=int(p2_user.id),
        )
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._done = asyncio.Event()
        # Add the four action buttons.
        for label, emoji, key, style in (
            ("Strike",  "⚔️", "strike",  discord.ButtonStyle.primary),
            ("Special", "\U0001F4A5",   "special", discord.ButtonStyle.success),
            ("Brace",   "\U0001F6E1",   "brace",   discord.ButtonStyle.secondary),
            ("Risky",   "\U0001F3AF",   "risky",   discord.ButtonStyle.danger),
        ):
            btn = discord.ui.Button(
                label=label, emoji=emoji, style=style,
            )
            btn.callback = self._make_cb(key)
            self.add_item(btn)
        # Bump button so the duel doesn't get buried by chat. Either
        # combatant can press it (the existing interaction_check
        # already restricts who counts as "owner") so require_owner=False.
        # Lives alone on the bottom row per project convention.
        from core.framework.persistent_embeds import BumpButton as _BumpButton
        self.add_item(_BumpButton(
            int(p1_user.id), label="Bump", row=4, require_owner=False,
        ))

    def _make_cb(self, action_key: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self._on_action(interaction, action_key)
        return _cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.p1_user.id, self.p2_user.id):
            await interaction.response.send_message(
                "Only the two combatants can act in this battle.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_action(
        self, interaction: discord.Interaction, action_key: str,
    ) -> None:
        b = self.battle
        async with self._lock:
            if b.is_over():
                await interaction.response.defer()
                return

            slot = 1 if interaction.user.id == self.p1_user.id else 2
            already = (b.p1_action if slot == 1 else b.p2_action)
            if already is not None:
                # Allow re-pick before the OTHER side has locked, so a
                # player can change their mind. Once both are locked,
                # the lock disengages on the next round automatically.
                if slot == 1:
                    b.p1_action = action_key
                else:
                    b.p2_action = action_key
                await interaction.response.defer()
                return

            if slot == 1:
                b.p1_action = action_key
            else:
                b.p2_action = action_key

            # Special with no stamina: refuse the lock-in so the player
            # picks again. Caller's interaction is still acknowledged
            # via an ephemeral hint.
            atk_stam = b.p1_stamina if slot == 1 else b.p2_stamina
            if action_key == "special" and atk_stam < _PVP_SPECIAL_COST:
                if slot == 1:
                    b.p1_action = None
                else:
                    b.p2_action = None
                await interaction.response.send_message(
                    f"Need {_PVP_SPECIAL_COST} stamina for Special "
                    f"(you have {atk_stam}). Pick something else.",
                    ephemeral=True,
                )
                return

            if not b.both_locked():
                # Refresh the panel so the OTHER player sees the lock.
                try:
                    _embed, _file = _pvp_round_embed(
                        b, self.p1_user, self.p2_user,
                    )
                    await interaction.response.edit_message(
                        embed=_embed, attachments=[_file],
                    )
                except discord.HTTPException:
                    log.debug("pvp battle: lock edit failed", exc_info=True)
                return

            # Both locked -- resolve the round.
            await self._resolve_round(interaction)

    async def _resolve_round(
        self, interaction: discord.Interaction,
    ) -> None:
        b = self.battle
        # Speed order. Fighter.spd is a float in [0,1]; ties favour P1.
        p1_first = float(b.p1.spd) >= float(b.p2.spd)
        first_slot = 1 if p1_first else 2
        second_slot = 2 if p1_first else 1
        first_action = b.p1_action if p1_first else b.p2_action
        second_action = b.p2_action if p1_first else b.p1_action

        # Acknowledge the click before kicking off the (potentially
        # multi-second) burst animations so Discord doesn't fail the
        # interaction for taking too long.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.debug("pvp battle: defer failed", exc_info=True)

        # Per-move animation burst BEFORE the first fighter's action
        # resolves (so the player sees the swing before HP updates).
        await _play_pvp_action_burst(
            self, b, self.p1_user, self.p2_user,
            actor_slot=first_slot, action=str(first_action),
        )

        # Round header. Helps the log read like a Pokemon battle log.
        b.log_lines.append(f"__**Round {b.round_num}**__")
        b.log_lines.extend(_pvp_apply(b, first_slot, str(first_action)))
        if not b.is_over():
            # Second fighter's animation burst before their swing.
            await _play_pvp_action_burst(
                self, b, self.p1_user, self.p2_user,
                actor_slot=second_slot, action=str(second_action),
            )
            b.log_lines.extend(
                _pvp_apply(b, second_slot, str(second_action))
            )
        # End-of-round poison DOT. Mirrors the engine's _tick_poison
        # ordering -- happens AFTER both fighters have acted so a
        # poison applied this round still ticks before next round.
        if not b.is_over():
            b.log_lines.extend(_pvp_tick_poison(b))
            b.log_lines.extend(_pvp_tick_round_effects(b))
        b.log_lines.append("")  # blank separator

        # Clear locks for next round.
        b.p1_action = None
        b.p2_action = None
        b.round_num += 1

        if b.is_over():
            self._done.set()
            try:
                # Disable buttons before final edit so a stale click
                # mid-network can't double-resolve. We already deferred
                # the interaction at the top of _resolve_round to keep
                # the burst animations from timing it out, so edit the
                # message directly instead of going through the
                # interaction response API again.
                for child in self.children:
                    child.disabled = True  # type: ignore[attr-defined]
                _embed, _file = _pvp_round_embed(b, self.p1_user, self.p2_user)
                if self.message is not None:
                    await self.message.edit(
                        embed=_embed, attachments=[_file], view=self,
                    )
            except discord.HTTPException:
                log.debug("pvp battle: final edit failed", exc_info=True)
            self.stop()
            return

        # Refresh + reset timer for next round.
        try:
            _embed, _file = _pvp_round_embed(b, self.p1_user, self.p2_user)
            if self.message is not None:
                await self.message.edit(
                    embed=_embed, attachments=[_file],
                )
        except discord.HTTPException:
            log.debug("pvp battle: round edit failed", exc_info=True)

    async def on_timeout(self) -> None:
        # Player who hadn't locked in this round forfeits via auto-Strike.
        # If both still hadn't picked, the slower player loses on tie-break
        # so a stalled battle resolves cleanly rather than hanging the view.
        b = self.battle
        if self._done.is_set() or b.is_over():
            return
        if b.p1_action is None:
            b.p1_action = "strike"
        if b.p2_action is None:
            b.p2_action = "strike"
        # Fake an interaction-less resolve via direct message edit.
        try:
            p1_first = float(b.p1.spd) >= float(b.p2.spd)
            first_slot = 1 if p1_first else 2
            second_slot = 2 if p1_first else 1
            first_action = b.p1_action if p1_first else b.p2_action
            second_action = b.p2_action if p1_first else b.p1_action
            b.log_lines.append(
                f"__**Round {b.round_num}**__  *(timeout -> auto-strike)*"
            )
            b.log_lines.extend(
                _pvp_apply(b, first_slot, str(first_action))
            )
            if not b.is_over():
                b.log_lines.extend(
                    _pvp_apply(b, second_slot, str(second_action))
                )
            if not b.is_over():
                b.log_lines.extend(_pvp_tick_poison(b))
                b.log_lines.extend(_pvp_tick_round_effects(b))
            b.p1_action = None
            b.p2_action = None
            b.round_num += 1
            if self.message is not None:
                for child in self.children:
                    child.disabled = True  # type: ignore[attr-defined]
                _embed, _file = _pvp_round_embed(b, self.p1_user, self.p2_user)
                await self.message.edit(
                    embed=_embed, attachments=[_file], view=self,
                )
        except Exception:
            log.debug("pvp battle on_timeout edit failed", exc_info=True)
        self._done.set()
        self.stop()


# =============================================================================
# Interactive WILD battle (escaped shelter buddy event)
# =============================================================================
# Same Pokemon-style cadence as _PvpBattleView, but only the challenger
# clicks buttons. The wild side picks an action via _wild_pick_action's
# heuristic, the round resolves immediately on each player click, and
# the embed updates in place between rounds.
#
# Mirrors the action mechanics exactly: Strike (+1 stam), Special (cost 2),
# Brace (heal 8% + halve next), Risky (60/25/15). Reuses _pvp_apply +
# _pvp_round_embed so wild fights share every species ability + crit
# rule with PvP.

def _wild_pick_action(b: "_PvpBattle") -> str:
    """Heuristic AI picker for the wild side.

    Plays toward the most threatening action available given current HP +
    stamina. The wild side gets the same 4 actions the player has.
    """
    import random as _r
    wild = b.p2
    hp_pct = wild.hp / max(1, wild.max_hp)
    stam = b.p2_stamina

    if stam >= _PVP_SPECIAL_COST and _r.random() < 0.40:
        return "special"
    if hp_pct < 0.30 and not b.p2_brace_next and _r.random() < 0.55:
        return "brace"
    if _r.random() < 0.18:
        return "risky"
    return "strike"


class _SpeciesTypeSelect(discord.ui.Select):
    """Type filter on the ,buddy species roster panel."""

    _TYPES: tuple[tuple[str, str, str], ...] = (
        # (value, label, emoji)
        ("__all__", "All Species",     "\U0001F436"),
        ("forest",  "Forest Affinity", "\U0001F332"),
        ("reef",    "Reef Affinity",   "\U0001FAB8"),
        ("mine",    "Mine Affinity",   "\U000026CF"),
        ("ruins",   "Ruins Affinity",  "\U0001F3DB"),
        ("neutral", "Neutral",         "\U0001F535"),
    )

    def __init__(self, current: str = "__all__") -> None:
        opts = [
            discord.SelectOption(
                label=label, value=val, emoji=emoji,
                default=(val == current),
            )
            for val, label, emoji in self._TYPES
        ]
        super().__init__(
            placeholder="Filter by affinity type...",
            options=opts, min_values=1, max_values=1, row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_SpeciesRosterView" = self.view  # type: ignore[assignment]
        view.affinity = str(self.values[0])
        embed = await view._build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class _SpeciesRosterView(discord.ui.View):
    """Interactive ,buddy species panel.

    Owner-locked, 5-min timeout. Single dropdown filters the roster
    by ``expeditions_config.SPECIES_AFFINITY`` -- All / Forest / Reef /
    Mine / Ruins / Neutral. The embed shows each species' hatch
    chance, full signature-lane breakdown (species + rarity extras),
    ability copy, and base combat stats so a player evaluating
    species choices for a destination / lane has everything in one
    place.
    """

    def __init__(self, ctx: DiscoContext) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.affinity = "__all__"
        self.message: discord.Message | None = None
        self.add_item(_SpeciesTypeSelect(self.affinity))

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your roster panel. Run `,buddy species` to "
                "open your own.",
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

    def _filter_species(self) -> list[tuple[str, dict]]:
        """Return (species_key, meta) pairs matching the current filter,
        sorted by rarity then alphabetic so the embed is stable.
        """
        try:
            from configs.expeditions_config import species_affinity as _aff
        except Exception:
            _aff = lambda _: "neutral"  # type: ignore
        out: list[tuple[str, dict]] = []
        for key, meta in SPECIES.items():
            if self.affinity != "__all__":
                if _aff(key) != self.affinity:
                    continue
            out.append((key, meta))
        out.sort(key=lambda kv: (
            int(kv[1].get("rarity", 1)),
            str(kv[0]),
        ))
        return out

    async def _build_embed(self) -> discord.Embed:
        from configs.buddies_config import (
            BONUS_LANE_LABELS,
            RARITY_EXTRA_SIGNATURE_LANES,
            buddy_bonus_lanes_for,
        )
        try:
            from configs.expeditions_config import (
                DESTINATIONS as _DESTS,
                species_affinity as _aff,
            )
        except Exception:
            _DESTS, _aff = {}, (lambda _: "neutral")  # type: ignore
        species_rows = self._filter_species()
        total_weight = sum(int(m.get("weight", 0)) for m in SPECIES.values()) or 1

        title_label = next(
            (l for v, l, _ in _SpeciesTypeSelect._TYPES if v == self.affinity),
            "All Species",
        )
        builder = card(
            f"\U0001F436 Buddy Roster  ·  {title_label}  ·  "
            f"{len(species_rows)} species",
            color=C_NAVY,
        )
        if self.affinity != "__all__":
            dest_meta = _DESTS.get(self.affinity, {})
            blurb = str(dest_meta.get("blurb") or "")
            if blurb:
                builder = builder.description(
                    f"_{blurb}_  ·  Affinity buddies on `,expedition` "
                    f"to **{dest_meta.get('name', self.affinity.title())}** "
                    f"earn +25% loot quantity."
                )
        if not species_rows:
            return builder.field(
                "Nothing here",
                "No species in this affinity bucket. Try **All Species** "
                "or pick another type.",
                False,
            ).build()

        # Group by rarity, render per-species detail.
        by_tier: dict[int, list[tuple[str, dict]]] = {}
        for key, meta in species_rows:
            by_tier.setdefault(int(meta.get("rarity", 1)), []).append((key, meta))

        for tier in sorted(by_tier.keys()):
            tier_rows = by_tier[tier]
            tmeta = RARITY_TIERS.get(tier, {})
            extras_n = int(RARITY_EXTRA_SIGNATURE_LANES.get(int(tier), 0))
            extras_part = (
                f"  ·  +{extras_n} extra signature lane"
                f"{'s' if extras_n != 1 else ''}"
                if extras_n > 0 else ""
            )
            header = (
                f"__**{tmeta.get('name', f'Tier {tier}')}**__  ·  "
                f"bonus x{tmeta.get('bonus_mult', 1.0):.2f}{extras_part}"
            )
            lines: list[str] = []
            for key, meta in tier_rows:
                emoji = str(meta.get("emoji") or "")
                weight = int(meta.get("weight", 0))
                pct = 100.0 * weight / total_weight
                ability_name = str(meta.get("ability_name") or "-")
                ability_desc = str(meta.get("ability_desc") or "")
                hp_base = int(tmeta.get("hp_base") or 100)
                atk_base = int(tmeta.get("atk_base") or 10)
                lanes = buddy_bonus_lanes_for(str(key), int(tier))
                lane_display = " · ".join(
                    BONUS_LANE_LABELS.get(l, l) for l in lanes
                ) if lanes else "-"
                affinity = _aff(key)
                # Secondary + tertiary unlock summary so the roster
                # makes the level-gated kit visible at a glance.
                prog = _species_ability_progression_safe(key)
                sec = prog.get("secondary", {}) if prog else {}
                ter = prog.get("tertiary", {}) if prog else {}
                unlock_bits: list[str] = []
                if sec.get("name"):
                    unlock_bits.append(
                        f"Lv {int(sec.get('unlock_level') or 15)}: "
                        f"**{sec['name']}**"
                    )
                if ter.get("name"):
                    unlock_bits.append(
                        f"Lv {int(ter.get('unlock_level') or 30)}: "
                        f"**{ter['name']}**"
                    )
                unlock_line = (
                    "-# Unlocks: " + " · ".join(unlock_bits)
                    if unlock_bits else ""
                )
                line_chunks = [
                    f"{emoji}  **{key.title()}**  ·  {pct:.1f}% hatch  ·  "
                    f"affinity: `{affinity}`",
                    f"-# Buffs: {lane_display}",
                    f"-# Ability: **{ability_name}** -- {ability_desc}",
                ]
                if unlock_line:
                    line_chunks.append(unlock_line)
                line_chunks.append(
                    f"-# Base: **{hp_base}** HP · **{atk_base}** ATK"
                )
                lines.append("\n".join(line_chunks))
            value = "\n\n".join(lines)
            # Discord limits: a single field value is 1024 chars, the
            # whole embed is 6000. Keep a margin under both so a future
            # ability_desc / lane label growth never throws "exceeds
            # char max value".
            _FIELD_CAP = 950
            _EMBED_CAP = 5500

            def _embed_len() -> int:
                # CardBuilder wraps a discord.Embed; __len__ on Embed
                # already sums title + description + every field name +
                # value + footer + author so this is the running total
                # we need to budget against.
                try:
                    return len(builder._embed)  # type: ignore[attr-defined]
                except Exception:
                    return 0

            def _safe_field(b, name: str, val: str):
                # Hard-cap every field value at 1024 (the Discord limit)
                # as defence-in-depth, in case the chunk math ever
                # disagrees with reality.
                return b.field(name[:256], (val or " ")[:1024], False)

            chunks: list[str] = []
            if len(value) <= _FIELD_CAP:
                chunks = [value]
            else:
                buf = ""
                for ln in lines:
                    sep = "\n\n" if buf else ""
                    if buf and len(buf) + len(sep) + len(ln) > _FIELD_CAP:
                        chunks.append(buf)
                        buf = ln
                    else:
                        # Single line longer than the cap: still has to
                        # ship -- truncate so Discord doesn't reject it.
                        ln_safe = ln if len(ln) <= 1024 else ln[:1020] + "..."
                        buf += sep + ln_safe
                if buf:
                    chunks.append(buf)

            for i, ck in enumerate(chunks):
                fname = header if i == 0 else f"{header} (cont)"
                # Drop chunks once the embed is approaching its 6000
                # cap. Surfaces a one-line "+N more" hint so the player
                # knows to use the affinity filter instead.
                if _embed_len() + len(fname) + len(ck) >= _EMBED_CAP:
                    remaining = sum(
                        1 for _ in chunks[i:]
                    ) + (len(species_rows) - len(lines))
                    builder = _safe_field(
                        builder,
                        "+ more species",
                        f"_{remaining} more species hidden -- pick a more "
                        f"specific affinity filter from the dropdown to see them._",
                    )
                    break
                builder = _safe_field(builder, fname, ck)

        prefix = await self.ctx.get_guild_prefix()
        return builder.footer(
            f"`{prefix}buddy hatch` to roll  ·  "
            f"`{prefix}buddy reroll` for free re-rolls (limit "
            f"{REROLL_MAX})  ·  "
            f"`{prefix}buddy swap <species>` paid swap"
        ).build()


class _WildBattleView(discord.ui.View):
    """Single-player Pokemon-style wild fight against an escaped buddy.

    The challenger clicks one of four action buttons; the wild side
    locks in its action via :func:`_wild_pick_action` immediately, the
    round resolves in speed order, and the embed refreshes. The view
    stops when either fighter hits 0 HP or _PVP_BATTLE_MAX_ROUNDS is
    reached, at which point ``self.battle.winner()`` reflects the
    outcome.
    """

    def __init__(
        self, *, ctx: DiscoContext, challenger: discord.abc.User,
        p1: "Any", p2: "Any",
    ) -> None:
        super().__init__(timeout=_PVP_ROUND_TIMEOUT_S)
        self.ctx = ctx
        self.challenger = challenger
        self.battle = _PvpBattle(
            p1=p1, p2=p2,
            p1_user_id=int(challenger.id),
            p2_user_id=0,  # wild side has no owner
        )
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._done = asyncio.Event()
        for label, emoji, key, style in (
            ("Strike",  "\U00002694", "strike",  discord.ButtonStyle.primary),
            ("Special", "\U0001F4A5", "special", discord.ButtonStyle.success),
            ("Brace",   "\U0001F6E1", "brace",   discord.ButtonStyle.secondary),
            ("Risky",   "\U0001F3AF", "risky",   discord.ButtonStyle.danger),
        ):
            btn = discord.ui.Button(label=label, emoji=emoji, style=style)
            btn.callback = self._make_cb(key)
            self.add_item(btn)
        # Bump for the wild fight too -- single-player owner-locked.
        # Lives alone on the bottom row per project convention.
        from core.framework.persistent_embeds import BumpButton as _BumpButton
        self.add_item(_BumpButton(int(challenger.id), label="Bump", row=4))

    def _make_cb(self, action_key: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self._on_action(interaction, action_key)
        return _cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.challenger.id:
            await interaction.response.send_message(
                "Only the challenger can act in this fight.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_action(
        self, interaction: discord.Interaction, action_key: str,
    ) -> None:
        b = self.battle
        async with self._lock:
            if b.is_over():
                await interaction.response.defer()
                return

            # Special with no stamina: bounce the click.
            if action_key == "special" and b.p1_stamina < _PVP_SPECIAL_COST:
                await interaction.response.send_message(
                    f"Need {_PVP_SPECIAL_COST} stamina for Special "
                    f"(you have {b.p1_stamina}). Pick something else.",
                    ephemeral=True,
                )
                return

            b.p1_action = action_key
            b.p2_action = _wild_pick_action(b)
            await self._resolve_round(interaction)

    async def _resolve_round(
        self, interaction: discord.Interaction,
    ) -> None:
        b = self.battle
        # Speed order. ``Fighter.spd`` is a float in [0,1]; ties favour P1
        # so the player wins simultaneous-hit ties (good vibes vs the wild).
        p1_first = float(b.p1.spd) >= float(b.p2.spd)
        first_slot = 1 if p1_first else 2
        second_slot = 2 if p1_first else 1
        first_action = b.p1_action if p1_first else b.p2_action
        second_action = b.p2_action if p1_first else b.p1_action

        # Defer up front so the burst frames don't run past Discord's
        # 3s interaction window.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.debug("wild battle: defer failed", exc_info=True)

        wild_stub = _wild_user_stub(b)
        await _play_pvp_action_burst(
            self, b, self.challenger, wild_stub,
            actor_slot=first_slot, action=str(first_action),
        )

        b.log_lines.append(f"__**Round {b.round_num}**__")
        b.log_lines.extend(_pvp_apply(b, first_slot, str(first_action)))
        if not b.is_over():
            await _play_pvp_action_burst(
                self, b, self.challenger, wild_stub,
                actor_slot=second_slot, action=str(second_action),
            )
            b.log_lines.extend(
                _pvp_apply(b, second_slot, str(second_action))
            )
        if not b.is_over():
            b.log_lines.extend(_pvp_tick_poison(b))
            b.log_lines.extend(_pvp_tick_round_effects(b))
        b.log_lines.append("")

        b.p1_action = None
        b.p2_action = None
        b.round_num += 1

        if b.is_over():
            self._done.set()
            try:
                for child in self.children:
                    child.disabled = True  # type: ignore[attr-defined]
                _embed, _file = _pvp_round_embed(
                    b, self.challenger, wild_stub,
                )
                if self.message is not None:
                    await self.message.edit(
                        embed=_embed, attachments=[_file], view=self,
                    )
            except discord.HTTPException:
                log.debug("wild battle: final edit failed", exc_info=True)
            self.stop()
            return

        try:
            _embed, _file = _pvp_round_embed(
                b, self.challenger, wild_stub,
            )
            if self.message is not None:
                await self.message.edit(
                    embed=_embed, attachments=[_file],
                )
        except discord.HTTPException:
            log.debug("wild battle: round edit failed", exc_info=True)

    async def on_timeout(self) -> None:
        # Player walked away mid-round. Treat as a forfeit by auto-Strike
        # so the view resolves cleanly instead of hanging.
        b = self.battle
        if self._done.is_set() or b.is_over():
            return
        if b.p1_action is None:
            b.p1_action = "strike"
        if b.p2_action is None:
            b.p2_action = _wild_pick_action(b)
        try:
            p1_first = float(b.p1.spd) >= float(b.p2.spd)
            first_slot = 1 if p1_first else 2
            second_slot = 2 if p1_first else 1
            first_action = b.p1_action if p1_first else b.p2_action
            second_action = b.p2_action if p1_first else b.p1_action
            b.log_lines.append(
                f"__**Round {b.round_num}**__  *(timeout -> auto-strike)*"
            )
            b.log_lines.extend(
                _pvp_apply(b, first_slot, str(first_action))
            )
            if not b.is_over():
                b.log_lines.extend(
                    _pvp_apply(b, second_slot, str(second_action))
                )
            if not b.is_over():
                b.log_lines.extend(_pvp_tick_poison(b))
                b.log_lines.extend(_pvp_tick_round_effects(b))
            b.p1_action = None
            b.p2_action = None
            b.round_num += 1
            if self.message is not None:
                for child in self.children:
                    child.disabled = True  # type: ignore[attr-defined]
                _embed, _file = _pvp_round_embed(
                    b, self.challenger, _wild_user_stub(b),
                )
                await self.message.edit(
                    embed=_embed, attachments=[_file], view=self,
                )
        except Exception:
            log.debug("wild battle on_timeout edit failed", exc_info=True)
        self._done.set()
        self.stop()


class _WildOwnerStub:
    """Tiny shim so _pvp_round_embed sees a labeled 'opponent' on the wild side."""
    __slots__ = ("display_name", "id")

    def __init__(self, display_name: str) -> None:
        self.display_name = display_name
        self.id = 0


def _wild_user_stub(b: "_PvpBattle") -> _WildOwnerStub:
    return _WildOwnerStub(f"Wild {b.p2.name}")


# =============================================================================
# ,buddy market filter parser
# =============================================================================
# Parses free-form positional tokens into a dict the cog passes straight
# into bm.browse_listings kwargs. Tokens can appear in any order and are
# classified by shape (kind keyword, species name, rarity name, lvl/price
# range, page). Unknown tokens are silently dropped so a typo doesn't
# nuke the whole query.

import re as _re_mkt

_MARKET_RARITY_NAME_TO_TIER: dict[str, int] = {
    "common":    1,
    "uncommon":  2,
    "rare":      3,
    "epic":      4,
    "legendary": 5,
    # Short forms players might type quickly:
    "leg":       5,
    "rar":       3,
    "epi":       4,
    "unc":       2,
    "com":       1,
}


def _parse_money_token(tok: str) -> int | None:
    """Convert ``50k``, ``1.5m``, ``250000`` etc. into a USD raw amount.

    Returns ``None`` if the token doesn't look money-like, so the parser
    can fall through to other classifiers without false positives.
    """
    s = str(tok).strip().lower().replace(",", "").replace("$", "").replace("_", "")
    mult = 1.0
    if s.endswith("k"):
        mult = 1_000.0; s = s[:-1]
    elif s.endswith("m"):
        mult = 1_000_000.0; s = s[:-1]
    elif s.endswith("b"):
        mult = 1_000_000_000.0; s = s[:-1]
    try:
        val = float(s) * mult
    except ValueError:
        return None
    if val <= 0:
        return None
    # Use the canonical to_raw scaler so a "<50k" filter compares
    # bit-for-bit identically against listing.asking_price_raw values
    # written by the rest of the marketplace path.
    return int(to_raw(val))


def _parse_market_filters(args: tuple[str, ...]) -> dict:
    """Token-bag parser for ``,buddy market`` filter args.

    Returns a kwargs-shaped dict the cog hands to ``bm.browse_listings``.
    Recognised tokens:
        eggs / egg / buddies / buddy / all     -> kind
        common / uncommon / rare / epic / legendary -> rarity_tier
        any species in buddies_config.SPECIES -> species
        lvl5 / lvl5+ / lvl5- / lvl3-12 / lv5+ -> min_level / max_level
        <50000 / >100000 / <50k / >1.5m       -> max_price_raw / min_price_raw
        page=N / p2 / 2 (bare int)            -> page
    Unrecognised tokens are dropped silently.
    """
    out: dict = {"_raw_tokens": list(args)}
    species_keys = {k.lower() for k in SPECIES.keys()}

    for raw in args:
        tok = str(raw).strip().lower()
        if not tok:
            continue

        # ---- kind ------------------------------------------------------
        if tok in ("eggs", "egg"):
            out["kind"] = "egg"
            continue
        if tok in ("buddies", "buddy"):
            out["kind"] = "buddy"
            continue
        if tok in ("all", "any", "*"):
            out["kind"] = None
            continue

        # ---- rarity ----------------------------------------------------
        if tok in _MARKET_RARITY_NAME_TO_TIER:
            out["rarity_tier"] = int(_MARKET_RARITY_NAME_TO_TIER[tok])
            continue

        # ---- species ---------------------------------------------------
        if tok in species_keys:
            out["species"] = tok
            continue

        # ---- level: lvl5 / lvl5+ / lvl5-12 / lv3- ---------------------
        m = _re_mkt.match(r"^(?:lvl|lv|level)(\d+)([+\-]?)(?:-(\d+))?$", tok)
        if m:
            lo = int(m.group(1))
            tail = m.group(2) or ""
            hi_explicit = m.group(3)
            if hi_explicit:
                out["min_level"] = lo
                out["max_level"] = int(hi_explicit)
            elif tail == "+":
                out["min_level"] = lo
            elif tail == "-":
                out["max_level"] = lo
            else:
                # Exact level: treat as min == max
                out["min_level"] = lo
                out["max_level"] = lo
            continue

        # ---- price: <50000 / >100000 / <50k / >1.5m -------------------
        if tok.startswith("<"):
            v = _parse_money_token(tok[1:])
            if v is not None:
                out["max_price_raw"] = v
                continue
        if tok.startswith(">"):
            v = _parse_money_token(tok[1:])
            if v is not None:
                out["min_price_raw"] = v
                continue
        # Bare price like "50000" / "50k" -- only counts as price when
        # it has a multiplier suffix (k/m/b) to avoid swallowing the
        # bare-int "page=N" case.
        if tok and tok[-1] in ("k", "m", "b"):
            v = _parse_money_token(tok)
            if v is not None:
                # Treat bare suffix-money as a CEILING (most common
                # filter intent: "show me listings under $50k").
                out["max_price_raw"] = v
                continue

        # ---- page: page=N / p=N / pN / bare integer -------------------
        m = _re_mkt.match(r"^(?:page|p)\s*=?\s*(\d+)$", tok)
        if m:
            out["page"] = max(1, int(m.group(1)))
            continue
        if tok.isdigit():
            out["page"] = max(1, int(tok))
            continue

        # Unknown -- drop silently. The chip line below won't show it,
        # so the user just sees their recognised filters and can adjust.

    return out


def _market_filter_chips(filters: dict) -> str:
    """Render the active-filter line for the market embed header.

    Returns an empty string when no filters are set so the panel
    description doesn't grow a useless 'Filters:' row.
    """
    parts: list[str] = []
    kind = filters.get("kind")
    if kind == "buddy":
        parts.append("buddies only")
    elif kind == "egg":
        parts.append("eggs only")
    if filters.get("species"):
        parts.append(f"species: `{filters['species']}`")
    rt = filters.get("rarity_tier")
    if rt is not None:
        try:
            name = rarity_meta(int(rt)).get("name", f"T{rt}")
        except Exception:
            name = f"T{rt}"
        parts.append(f"rarity: {name}")
    mn, mx = filters.get("min_level"), filters.get("max_level")
    if mn is not None and mx is not None and mn == mx:
        parts.append(f"level: {mn}")
    elif mn is not None and mx is not None:
        parts.append(f"level: {mn}-{mx}")
    elif mn is not None:
        parts.append(f"level: {mn}+")
    elif mx is not None:
        parts.append(f"level: <={mx}")
    if filters.get("min_price_raw"):
        parts.append(f"price: >= {fmt_token(to_human(filters['min_price_raw']), 'BUD')}")
    if filters.get("max_price_raw"):
        parts.append(f"price: <= {fmt_token(to_human(filters['max_price_raw']), 'BUD')}")
    return "  -  ".join(parts) if parts else ""


# =============================================================================
# Buddy storage view -- Withdraw / Deposit buttons for ,buddy storage
# =============================================================================


def _format_storage_line(r: dict) -> str:
    """One stored-buddy row line for the storage embed.

    Mirrors the in-cog formatter so the inline list and the dropdown
    descriptions stay consistent.
    """
    from configs.buddies_config import (
        GENDER_LABEL as _GENDER_LABEL,
        gender_glyph as _gender_glyph,
        rarity_meta as _rarity_meta,
    )
    rid = int(r["id"])
    species = str(r.get("species") or "")
    emoji = str(SPECIES.get(species, {}).get("emoji") or "")
    gender_raw = str(r.get("gender") or "").upper()
    glyph = _gender_glyph(r.get("gender"))
    gender_word = _GENDER_LABEL.get(gender_raw, "Unknown")
    gender_part = f"{glyph} {gender_word}" if glyph else gender_word
    name = str(r.get("name") or "Unnamed")
    lvl = effective_level(r)
    tier_n = int(r.get("rarity_tier") or 1)
    tier_label = str(_rarity_meta(tier_n).get("name") or "Common")
    wins = int(r.get("wins") or 0)
    losses = int(r.get("losses") or 0)
    record = f"  -  {wins}W-{losses}L" if (wins or losses) else ""
    return (
        f"`#{rid}`  {emoji} **{name}**  -  Lv. {lvl}\n"
        f"-# Rarity: **{tier_label}**  -  Gender: **{gender_part}**{record}"
    )


class _BuddyStorageWithdrawSelect(discord.ui.Select):
    """Dropdown listing stored buddies. Picking one withdraws it.

    Limited to 25 options because Discord caps a single Select. When the
    player has more than 25 stored buddies they should still see the
    paginated text list to find ids; falling back to ``,buddy retrieve
    <id>`` always works.
    """

    def __init__(self, parent: "_BuddyStorageView", rows: list[dict]) -> None:
        opts: list[discord.SelectOption] = []
        from configs.buddies_config import rarity_meta as _b_rarity
        for r in rows[:25]:
            rid = int(r["id"])
            species = str(r.get("species") or "")
            emoji_str = str(SPECIES.get(species, {}).get("emoji") or "\U0001F436")
            tier_name = str(_b_rarity(int(r.get("rarity_tier") or 1)).get("name") or "Common")
            lvl = effective_level(r)
            name = str(r.get("name") or "Unnamed")
            opts.append(discord.SelectOption(
                label=f"#{rid} {name} -- L{lvl} {tier_name}"[:100],
                value=str(rid),
                emoji=emoji_str,
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no stored buddies)", value="_none_", default=True,
            )]
        super().__init__(
            placeholder="Pick a stored buddy to withdraw...",
            options=opts, min_values=1, max_values=1, row=0,
            disabled=(len(opts) == 1 and opts[0].value == "_none_"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your storage panel.", ephemeral=True,
            )
            return
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        try:
            bid = int(v)
        except ValueError:
            await interaction.response.defer()
            return
        ok, err, row = await from_storage(
            view.cog.bot.db, view.ctx.guild_id, view.ctx.author.id, bid,
        )
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            await view._refresh(interaction)
            return
        species = str((row or {}).get("species") or "")
        emoji_str = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str((row or {}).get("name") or "Your buddy")
        lvl = effective_level(row or {})
        await interaction.response.send_message(
            f"{emoji_str} **{name}** (Lv. {lvl}) is back with you.",
            ephemeral=True,
        )
        await view._refresh(interaction)


class _BuddyStorageDepositSelect(discord.ui.Select):
    """Dropdown listing owned buddies. Picking one deposits it to storage.

    Hidden by default behind the Deposit button so the panel doesn't
    surface eight components on first paint.
    """

    def __init__(self, parent: "_BuddyStorageView", rows: list[dict]) -> None:
        opts: list[discord.SelectOption] = []
        from configs.buddies_config import rarity_meta as _b_rarity
        for r in rows[:25]:
            rid = int(r["id"])
            species = str(r.get("species") or "")
            emoji_str = str(SPECIES.get(species, {}).get("emoji") or "\U0001F436")
            tier_name = str(_b_rarity(int(r.get("rarity_tier") or 1)).get("name") or "Common")
            lvl = effective_level(r)
            name = str(r.get("name") or "Unnamed")
            active_tag = " · active" if r.get("is_active") else ""
            opts.append(discord.SelectOption(
                label=f"#{rid} {name} -- L{lvl} {tier_name}"[:100],
                value=str(rid),
                description=f"{tier_name}{active_tag}"[:100],
                emoji=emoji_str,
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no owned buddies)", value="_none_", default=True,
            )]
        super().__init__(
            placeholder="Pick an owned buddy to deposit...",
            options=opts, min_values=1, max_values=1, row=0,
            disabled=(len(opts) == 1 and opts[0].value == "_none_"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your storage panel.", ephemeral=True,
            )
            return
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        try:
            bid = int(v)
        except ValueError:
            await interaction.response.defer()
            return
        ok, err, row = await to_storage(
            view.cog.bot.db, view.ctx.guild_id, view.ctx.author.id, bid,
        )
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            await view._refresh(interaction)
            return
        species = str((row or {}).get("species") or "")
        emoji_str = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str((row or {}).get("name") or "Your buddy")
        lvl = effective_level(row or {})
        try:
            stored_count = await count_storage(
                view.cog.bot.db, view.ctx.guild_id, view.ctx.author.id,
            )
            await view.cog.bot.bus.publish(
                "buddy_stored",
                guild=view.ctx.guild, user=view.ctx.author,
                buddy_id=bid, stored_count=int(stored_count),
            )
        except Exception:
            log.debug("buddy_stored event publish failed", exc_info=True)
        await interaction.response.send_message(
            f"{emoji_str} **{name}** (Lv. {lvl}) is now in storage.",
            ephemeral=True,
        )
        await view._refresh(interaction)


class _SurrenderConfirmView(discord.ui.View):
    """Ephemeral yes/no confirmation before surrendering a stored buddy."""

    def __init__(
        self,
        parent: "_BuddyStorageView",
        buddy_id: int,
        name: str,
        species: str,
    ) -> None:
        super().__init__(timeout=30)
        self._parent = parent
        self._buddy_id = buddy_id
        self._name = name
        self._species = species

    @discord.ui.button(label="Yes, surrender", style=discord.ButtonStyle.danger)
    async def _yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        emoji_str = str(SPECIES.get(self._species, {}).get("emoji") or "")
        shelter_rows = await to_shelter(
            self._parent.cog.bot.db,
            self._parent.ctx.guild_id,
            self._parent.ctx.author.id,
            "surrendered",
            buddy_id=self._buddy_id,
            display_name=(
                getattr(self._parent.ctx.author, "display_name", None)
                or self._parent.ctx.author.name
            ),
        )
        if not shelter_rows:
            await interaction.response.edit_message(
                content="Surrender failed. Please try again.", view=None,
            )
            return
        await interaction.response.edit_message(
            content=f"{emoji_str} **{self._name}** is now at the shelter.",
            view=None,
        )
        await self._parent._refresh(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


class _BuddyStorageSurrenderSelect(discord.ui.Select):
    """Dropdown listing stored buddies for surrender. Picking one sends an
    ephemeral confirmation before handing the buddy to the shelter.
    """

    def __init__(self, parent: "_BuddyStorageView", rows: list[dict]) -> None:
        opts: list[discord.SelectOption] = []
        from configs.buddies_config import rarity_meta as _b_rarity
        for r in rows[:25]:
            rid = int(r["id"])
            species = str(r.get("species") or "")
            emoji_str = str(SPECIES.get(species, {}).get("emoji") or "\U0001F436")
            tier_name = str(_b_rarity(int(r.get("rarity_tier") or 1)).get("name") or "Common")
            lvl = effective_level(r)
            name = str(r.get("name") or "Unnamed")
            opts.append(discord.SelectOption(
                label=f"#{rid} {name} -- L{lvl} {tier_name}"[:100],
                value=str(rid),
                emoji=emoji_str,
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no stored buddies)", value="_none_", default=True,
            )]
        super().__init__(
            placeholder="Pick a stored buddy to surrender...",
            options=opts, min_values=1, max_values=1, row=0,
            disabled=(len(opts) == 1 and opts[0].value == "_none_"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your storage panel.", ephemeral=True,
            )
            return
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        try:
            bid = int(v)
        except ValueError:
            await interaction.response.defer()
            return
        target = next((r for r in view._stored if int(r["id"]) == bid), None)
        name = str((target or {}).get("name") or "Unnamed")
        species = str((target or {}).get("species") or "")
        emoji_str = str(SPECIES.get(species, {}).get("emoji") or "")
        confirm_view = _SurrenderConfirmView(view, bid, name, species)
        await interaction.response.send_message(
            f"Surrender {emoji_str} **{name}** to the shelter?\n"
            f"-# This is permanent -- anyone can adopt them afterward.",
            view=confirm_view,
            ephemeral=True,
        )


class _BuddyStorageView(discord.ui.View):
    """Owner-locked storage panel. Five-minute timeout matches the rest of
    the buddy surface. Withdraw / Deposit toggle the dropdown on row 0
    so the panel never exceeds Discord's component-row budget.
    """

    def __init__(self, cog: "Buddy", ctx: DiscoContext) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.message: discord.Message | None = None
        # "withdraw" | "deposit" | "surrender" | "list" | "gift"
        self._mode: str = "withdraw"
        self._stored: list[dict] = []
        self._owned: list[dict] = []

    async def _load(self) -> None:
        self._stored = list(await list_storage(
            self.cog.bot.db, self.ctx.guild_id, self.ctx.author.id,
            limit=25, offset=0,
        ) or [])
        self._owned = await _fetch_all_owned(
            self.cog.bot.db, self.ctx.guild_id, self.ctx.author.id,
        )

    def _rebuild(self) -> None:
        self.clear_items()
        if self._mode == "surrender":
            self.add_item(_BuddyStorageSurrenderSelect(self, self._stored))
        elif self._mode == "withdraw":
            self.add_item(_BuddyStorageWithdrawSelect(self, self._stored))
        elif self._mode == "list":
            self.add_item(_BuddyStorageListAHSelect(self, self._owned))
        elif self._mode == "gift":
            self.add_item(_BuddyStorageGiftSelect(self, self._owned))
        else:
            self.add_item(_BuddyStorageDepositSelect(self, self._owned))
        self.add_item(_BuddyStorageWithdrawButton())
        self.add_item(_BuddyStorageDepositButton())
        self.add_item(_BuddyStorageEggsButton())
        self.add_item(_BuddyStorageRefreshButton())
        self.add_item(_BuddyStorageListAHButton())
        self.add_item(_BuddyStorageGiftButton())
        self.add_item(_BuddyStorageSurrenderButton())

    async def _build_embed(self) -> discord.Embed:
        await self._load()
        self._rebuild()
        prefix = await self.ctx.get_guild_prefix()
        total_stored = len(self._stored)
        if total_stored:
            lines = [_format_storage_line(r) for r in self._stored]
            description = "\n".join(lines)
        else:
            description = (
                "_(no stored buddies)_\n\n"
                "Tap **Deposit** to stash one of your owned buddies. "
                "Stored buddies don't count against your owned cap, don't "
                "decay, and aren't usable in arena / delve until you "
                "withdraw them."
            )
        return (
            card(
                f"Buddy Storage  -  {self.ctx.author.display_name}",
                color=C_NAVY,
            )
            .description(description)
            .footer(
                f"{total_stored} stored  -  Eggs button opens "
                f"{prefix}fish egg for held-egg ops"
            )
            .build()
        )

    async def _refresh(self, interaction: discord.Interaction) -> None:
        embed = await self._build_embed()
        if self.message is None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.edit_message(
                        message_id=interaction.message.id,  # type: ignore[union-attr]
                        embed=embed, view=self,
                    )
                else:
                    await interaction.response.edit_message(embed=embed, view=self)
            except discord.HTTPException:
                log.debug("buddy storage refresh failed", exc_info=True)
            return
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            log.debug("buddy storage refresh failed", exc_info=True)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your storage panel.", ephemeral=True,
            )
            return False
        return True


class _BuddyStorageWithdrawButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Withdraw", emoji="\U0001F4E4",
            style=discord.ButtonStyle.success, row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        view._mode = "withdraw"
        await view._refresh(interaction)


class _BuddyStorageDepositButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Deposit", emoji="\U0001F4E5",
            style=discord.ButtonStyle.primary, row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        view._mode = "deposit"
        await view._refresh(interaction)


class _BuddyStorageEggsButton(discord.ui.Button):
    """Pivot in-place from the buddy storage panel to the egg picker.

    The egg picker (``_EggPickerView``) carries a matching "Buddies"
    button so the player can ping-pong between buddies + eggs without
    losing the message slot, mirroring how the storage panel's
    Withdraw / Deposit buttons just edit the same embed.
    """

    def __init__(self) -> None:
        super().__init__(
            label="Eggs", emoji="\U0001F95A",
            style=discord.ButtonStyle.secondary, row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        # Lazy import to dodge the cogs/fishing <- cogs/buddy circular.
        from cogs.fishing import (
            _EggPickerView,
            _egg_status_embed,
            _oracle_pair,
        )
        from services import fishing as _fish
        summary = await _fish.list_held_eggs(
            view.ctx.db, view.ctx.guild_id, view.ctx.author.id,
        )
        lure_oracle, _ = await _oracle_pair(view.ctx)
        try:
            fren_row = await view.ctx.db.get_price("FREN", view.ctx.guild_id)
            fren_oracle = float(fren_row["price"]) if fren_row else 0.0
        except Exception:
            fren_oracle = 0.0
        embed = _egg_status_embed(
            view.ctx.author, summary,
            lure_oracle=lure_oracle, fren_oracle=fren_oracle,
        )
        new_view = _EggPickerView(
            view.ctx, summary,
            lure_oracle=lure_oracle, fren_oracle=fren_oracle,
        )
        # Hand the message reference over so the egg picker's own
        # Buddies / Refresh buttons keep editing the same chat slot.
        new_view.message = view.message
        try:
            await interaction.response.edit_message(embed=embed, view=new_view)
        except discord.HTTPException:
            log.debug("buddy storage -> eggs swap failed", exc_info=True)
        view.stop()


class _BuddyStorageRefreshButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Refresh", emoji="\U0001F504",
            style=discord.ButtonStyle.secondary, row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        await view._refresh(interaction)


class _BuddyStorageSurrenderButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Surrender", emoji="\U0001F3F3",
            style=discord.ButtonStyle.danger, row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        view._mode = "surrender"
        await view._refresh(interaction)


class _BuddyStorageListAHButton(discord.ui.Button):
    """Pivot the dropdown to OWNED buddies for AH listing.

    Both ``services.auction.find_owned_buddy_token`` and
    ``services.buddy_market.gift_buddy`` require ``status='owned'``, so
    the storage panel can't operate on stored buddies directly. The
    list / gift dropdowns therefore surface owned buddies and delegate
    through the same flows the buddy panel uses, keeping the storage
    section as a unified hub for buddy-side commerce.
    """

    def __init__(self) -> None:
        super().__init__(
            label="List on AH", emoji="\U0001F3DB",
            style=discord.ButtonStyle.primary, row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        view._mode = "list"
        await view._refresh(interaction)


class _BuddyStorageGiftButton(discord.ui.Button):
    """Pivot the dropdown to OWNED buddies for gifting."""

    def __init__(self) -> None:
        super().__init__(
            label="Gift", emoji="\U0001F381",
            style=discord.ButtonStyle.primary, row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        view._mode = "gift"
        await view._refresh(interaction)


class _BuddyStorageListAHSelect(discord.ui.Select):
    """Dropdown listing OWNED buddies for AH-list. Picking one pops the
    same price modal the buddy panel's "List on AH" button uses, which
    routes through the auction service end-to-end.
    """

    def __init__(self, parent: "_BuddyStorageView", rows: list[dict]) -> None:
        opts: list[discord.SelectOption] = []
        from configs.buddies_config import rarity_meta as _b_rarity
        for r in rows[:25]:
            rid = int(r["id"])
            species = str(r.get("species") or "")
            emoji_str = str(SPECIES.get(species, {}).get("emoji") or "\U0001F436")
            tier_name = str(_b_rarity(int(r.get("rarity_tier") or 1)).get("name") or "Common")
            lvl = effective_level(r)
            name = str(r.get("name") or "Unnamed")
            opts.append(discord.SelectOption(
                label=f"#{rid} {name} -- L{lvl} {tier_name}"[:100],
                value=str(rid),
                emoji=emoji_str,
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no owned buddies)", value="_none_", default=True,
            )]
        super().__init__(
            placeholder="Pick an owned buddy to list on the AH...",
            options=opts, min_values=1, max_values=1, row=0,
            disabled=(len(opts) == 1 and opts[0].value == "_none_"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your storage panel.", ephemeral=True,
            )
            return
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        try:
            bid = int(v)
        except ValueError:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            _BuddyStorageListAHModal(view, bid),
        )


class _BuddyStorageListAHModal(discord.ui.Modal, title="List Buddy on Auction House"):
    """Storage-panel variant of the buddy panel's list modal.

    Shares the same ``services.auction.find_owned_buddy_token`` +
    ``create_listing_by_token`` pipeline so safeguards (gas, escrow,
    event log) stay identical. The owning view is refreshed on success
    so the listed buddy disappears from the OWNED pool immediately.
    """

    price = discord.ui.TextInput(
        label="Price",
        placeholder="e.g. 50000",
        required=True,
        max_length=20,
    )
    currency = discord.ui.TextInput(
        label="Currency (optional)",
        placeholder="leave blank for the network's default (BUD)",
        required=False,
        max_length=10,
    )

    def __init__(self, view: "_BuddyStorageView", buddy_id: int) -> None:
        super().__init__()
        self._view = view
        self.buddy_id = int(buddy_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from services import auction as _auc
        try:
            price_v = float(str(self.price.value).strip())
            if price_v <= 0:
                raise ValueError("Price must be positive.")
        except ValueError as e:
            await interaction.response.send_message(
                f"Bad price: {e}", ephemeral=True,
            )
            return
        cur = (str(self.currency.value or "").strip().upper() or None)
        try:
            tok_id = await _auc.find_owned_buddy_token(
                self._view.cog.bot.db,
                guild_id=int(interaction.guild_id or 0),
                seller_user_id=interaction.user.id,
                buddy_id=self.buddy_id,
            )
        except Exception:
            tok_id = None
        if not tok_id:
            await interaction.response.send_message(
                f"Couldn't find buddy `#{self.buddy_id}`'s NFT (or it's "
                f"escrowed / wrong owner).",
                ephemeral=True,
            )
            return
        try:
            listing_id, _tok, msg = await _auc.create_listing_by_token(
                self._view.cog.bot.db,
                guild_id=int(interaction.guild_id or 0),
                seller_user_id=interaction.user.id,
                token_id=tok_id,
                price=price_v,
                currency=cur,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            log.exception(
                "buddy storage List click failed buddy=%s",
                self.buddy_id,
            )
            await interaction.response.send_message(
                f"Could not list: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"\U0001F3DB Listed buddy `#{self.buddy_id}` as listing "
            f"#{int(listing_id)}. {msg}",
            ephemeral=True,
        )
        try:
            await self._view._refresh(interaction)
        except Exception:
            log.debug("buddy storage refresh after list failed", exc_info=True)


class _BuddyStorageGiftSelect(discord.ui.Select):
    """Dropdown listing OWNED buddies for gifting. Picking one pops a
    modal asking for the recipient (id or @mention) since modals can't
    embed a UserSelect.
    """

    def __init__(self, parent: "_BuddyStorageView", rows: list[dict]) -> None:
        opts: list[discord.SelectOption] = []
        from configs.buddies_config import rarity_meta as _b_rarity
        for r in rows[:25]:
            rid = int(r["id"])
            species = str(r.get("species") or "")
            emoji_str = str(SPECIES.get(species, {}).get("emoji") or "\U0001F436")
            tier_name = str(_b_rarity(int(r.get("rarity_tier") or 1)).get("name") or "Common")
            lvl = effective_level(r)
            name = str(r.get("name") or "Unnamed")
            opts.append(discord.SelectOption(
                label=f"#{rid} {name} -- L{lvl} {tier_name}"[:100],
                value=str(rid),
                emoji=emoji_str,
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no owned buddies)", value="_none_", default=True,
            )]
        super().__init__(
            placeholder="Pick an owned buddy to gift...",
            options=opts, min_values=1, max_values=1, row=0,
            disabled=(len(opts) == 1 and opts[0].value == "_none_"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_BuddyStorageView" = self.view  # type: ignore[assignment]
        if interaction.user.id != view.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your storage panel.", ephemeral=True,
            )
            return
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        try:
            bid = int(v)
        except ValueError:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            _BuddyStorageGiftModal(view, bid),
        )


class _BuddyStorageGiftModal(discord.ui.Modal, title="Gift Buddy"):
    """Modal to capture the recipient for a gift action. The fee is
    fixed at ``BUDDY_GIFT_FEE_USD`` so the modal only needs the target
    user reference. Resolves the recipient via ``ctx.guild.get_member``
    so it accepts a raw id or a ``<@id>`` mention.
    """

    recipient = discord.ui.TextInput(
        label="Recipient (user id or @mention)",
        placeholder="e.g. 123456789012345678",
        required=True,
        max_length=64,
    )

    def __init__(self, view: "_BuddyStorageView", buddy_id: int) -> None:
        super().__init__()
        self._view = view
        self.buddy_id = int(buddy_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.recipient.value or "").strip()
        # Strip mention wrappers <@id>, <@!id>, plain id
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            await interaction.response.send_message(
                "Couldn't parse a user id from that input.", ephemeral=True,
            )
            return
        try:
            recipient_id = int(digits)
        except ValueError:
            await interaction.response.send_message(
                "Couldn't parse a user id from that input.", ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This must be used in a guild.", ephemeral=True,
            )
            return
        member = guild.get_member(recipient_id)
        if member is None:
            try:
                member = await guild.fetch_member(recipient_id)
            except Exception:
                member = None
        if member is None or member.bot:
            await interaction.response.send_message(
                "Recipient must be a guild member (and not a bot).",
                ephemeral=True,
            )
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "Pick another player to gift to.", ephemeral=True,
            )
            return
        from services import buddy_market as bm
        try:
            res = await bm.gift_buddy(
                self._view.cog.bot.db,
                int(interaction.guild_id or 0),
                int(interaction.user.id),
                int(member.id),
                int(self.buddy_id),
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception as exc:
            log.exception(
                "buddy storage Gift click failed buddy=%s",
                self.buddy_id,
            )
            await interaction.response.send_message(
                f"Could not gift: `{type(exc).__name__}: {exc}`.",
                ephemeral=True,
            )
            return
        emoji_str = str(SPECIES.get(res.species, {}).get("emoji") or "")
        await interaction.response.send_message(
            f"{emoji_str} **{res.buddy_name}** has been gifted to "
            f"{member.mention}.\n"
            f"Paid fee: **${to_human(res.fee_paid_raw):,.0f}**.\n"
            f"-# Transfer #{res.transfer_id} logged for both of you.",
            ephemeral=True,
        )
        try:
            await self._view._refresh(interaction)
        except Exception:
            log.debug("buddy storage refresh after gift failed", exc_info=True)


# =============================================================================
# Cog
# =============================================================================

class Buddy(commands.Cog):
    """CC Buddy commands + chat-XP listener."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._sweep_task: asyncio.Task | None = None
        self._world_task: asyncio.Task | None = None
        # Per-challenger battle cooldown: (guild_id, user_id) -> unix epoch.
        # Per-process only; matches the chat-XP cooldown's design choice.
        self._last_battle_at: dict[tuple[int, int], float] = {}

    # -- Autodelete helpers --------------------------------------------------
    #
    # Admins can set guild_settings.buddy_message_delete_after (seconds) via
    # ,buddy admin autodelete. NULL / <= 0 disables cleanup; any positive
    # integer schedules Message.delete() after that many seconds on every
    # buddy embed this cog sends. Battle challenges, battle results,
    # escape events -- all honour the same knob so admins get one lever
    # to control buddy-channel clutter.
    async def _buddy_delete_after(self, guild_id: int | None) -> int | None:
        if not guild_id:
            return None
        try:
            val = await self.bot.db.fetch_val(
                "SELECT buddy_message_delete_after FROM guild_settings "
                "WHERE guild_id = $1",
                int(guild_id),
            )
        except Exception:
            return None
        if val is None:
            return None
        try:
            s = int(val)
        except (TypeError, ValueError):
            return None
        return s if s > 0 else None

    async def _schedule_autodelete(
        self, message: discord.Message | None, seconds: int | None,
    ) -> None:
        """Schedule a deferred delete on ``message`` after ``seconds``.

        ``Message.edit`` doesn't accept delete_after, so for edited
        messages (battle results, etc.) we schedule a task by hand.
        Silent on NotFound / Forbidden -- autodelete is best-effort.
        """
        if not message or not seconds or seconds <= 0:
            return

        async def _runner() -> None:
            try:
                await asyncio.sleep(seconds)
                await message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug(
                    "buddy autodelete: delete failed mid=%s", message.id,
                    exc_info=True,
                )

        asyncio.create_task(_runner())

    # -- Background sweep ----------------------------------------------------

    async def cog_load(self) -> None:
        """Start the mood-decay + runaway sweep when the cog loads."""
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop())
        if self._world_task is None or self._world_task.done():
            self._world_task = asyncio.create_task(self._world_loop())
        # Register a persistent stub of BuddyPanelView. discord.py routes
        # post-restart interactions on any panel opened in a previous
        # session to this stub instead of dropping them with the generic
        # "this interaction failed" + "View interaction referencing
        # unknown view for item ... Discarding" warning. The stub's
        # interaction_check sends an ephemeral hint asking the player
        # to re-run ,buddy for a fresh panel.
        try:
            self.bot.add_view(BuddyPanelView())
        except Exception:
            log.debug("BuddyPanelView persistent registration failed", exc_info=True)

    async def cog_unload(self) -> None:
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
        if self._world_task and not self._world_task.done():
            self._world_task.cancel()

    async def _sweep_loop(self) -> None:
        """Run decay + runaway every DECAY_TICK_INTERVAL_S seconds."""
        while True:
            try:
                await asyncio.sleep(DECAY_TICK_INTERVAL_S)
                await sweep_decay(self.bot.db)
                fled = await sweep_runaway(self.bot.db)
                for row in fled:
                    await self._dm_runaway(row)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("buddy sweep loop: unhandled error")

    async def _dm_runaway(self, row: dict) -> None:
        """DM the former owner that their buddy ran away."""
        uid = int(row.get("former_owner_id") or 0)
        if uid <= 0:
            return
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
        except Exception:
            return
        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "Your buddy")
        lvl = int(row.get("level") or 1)
        embed = (
            card(f"{emoji}  {name} ran away", color=C_ERROR)
            .description(
                f"After days of hunger and loneliness, **{name}** (Lv. {lvl}) "
                f"left for the shelter. You can adopt again from `,buddy shelter` "
                f"if another adopts them first.",
            )
            .footer(f"{fmt_ts(time.time())}")
            .build()
        )
        try:
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # -- Guild leave / ban listeners -----------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Fired on leave or kick. Move any active buddy to shelter with grace."""
        if not member or member.bot:
            return
        try:
            await to_shelter(
                self.bot.db, member.guild.id, member.id, "left_guild",
                display_name=getattr(member, "display_name", None) or member.name,
            )
        except Exception:
            log.debug("on_member_remove: to_shelter failed gid=%s uid=%s",
                      member.guild.id, member.id, exc_info=True)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        """Fired on ban. Same 24h grace as a leave."""
        if not guild or not user or user.bot:
            return
        try:
            await to_shelter(
                self.bot.db, guild.id, user.id, "banned",
                display_name=getattr(user, "display_name", None) or user.name,
            )
        except Exception:
            log.debug("on_member_ban: to_shelter failed gid=%s uid=%s",
                      guild.id, user.id, exc_info=True)

    # -- Chat XP listener ----------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Grant a chat-XP roll to the sender's buddy, once per cooldown window."""
        if not message.guild or message.author.bot:
            return
        if message.webhook_id:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        content = (message.content or "").strip()
        if not content or content.startswith(_COMMAND_PREFIXES):
            return

        gid = message.guild.id
        uid = message.author.id

        now = time.time()
        key = (gid, uid)
        last = _last_buddy_xp.get(key, 0.0)
        if now - last < CHAT_XP_COOLDOWN_S:
            return
        _last_buddy_xp[key] = now

        # Rarity scales the buddy's own chat-XP gain. Lookup the rarity
        # tier in the same UPDATE via a CASE over RARITY_TIERS so we don't
        # pay an extra round-trip per message. Only the ACTIVE buddy earns
        # XP -- inactive collection members are frozen.
        rarity_xp_case = " ".join(
            f"WHEN {tier} THEN {meta['xp_mult']:.4f}"
            for tier, meta in RARITY_TIERS.items()
        )
        roll = random.randint(CHAT_XP_MIN, CHAT_XP_MAX)

        try:
            # CTE captures the pre-update level so the level-up announcement
            # path can compare new vs. old without a second SELECT. The
            # UPDATE itself recomputes level from the new xp so every XP
            # path keeps the level column in lock-step (see migration
            # 0198 for the backfill rationale).
            row = await self.bot.db.fetch_one(
                f"""
                WITH _prev AS (
                    SELECT id, level AS old_level
                      FROM cc_buddies
                     WHERE guild_id = $1 AND owner_user_id = $2
                       AND status = 'owned' AND is_active
                )
                UPDATE cc_buddies AS b SET
                    xp = b.xp + GREATEST(
                        1,
                        ROUND($3 * (CASE b.rarity_tier {rarity_xp_case} ELSE 1.0 END))::int
                    ),
                    level = GREATEST(
                        b.level,
                        LEAST(50, GREATEST(1,
                            FLOOR((1.0 + SQRT(
                                1.0 + 8.0 * (b.xp + GREATEST(
                                    1,
                                    ROUND($3 * (CASE b.rarity_tier {rarity_xp_case} ELSE 1.0 END))::int
                                ))::double precision / 120.0
                            )) / 2.0)::int
                        ))
                    ),
                    last_xp_at = NOW(),
                    updated_at = NOW()
                FROM _prev
                WHERE b.id = _prev.id
                RETURNING b.id, b.name, b.species, b.level, b.xp,
                          b.rarity_tier, _prev.old_level
                """,
                gid, uid, roll,
            )
        except Exception:
            log.debug("buddy on_message: xp update failed gid=%s uid=%s", gid, uid, exc_info=True)
            return

        if not row:
            return

        old_level = int(row.get("old_level") or 1)
        new_level = int(row.get("level") or 1)
        leveled_up = new_level > old_level
        if leveled_up:
            await self._announce_level_up(message, row, new_level)

        # Themed Heartstone XP: each buddy chat tick (and a chunky bonus on
        # level-up) levels the owner's Heartstone if they have one.
        # ``bot`` + ``guild`` opt the grant into auto-levelup + ready DM
        # the same way the legacy hashstone path does it.
        try:
            from services import themed_stones as _ts
            await _ts.grant_heartstone_xp(
                self.bot.db, uid, gid,
                chat_ticks=1, leveled_up=leveled_up,
                bot=self.bot, guild=message.guild,
            )
        except Exception:
            log.debug(
                "buddy on_message: themed_stones.grant_heartstone_xp failed",
                exc_info=True,
            )

    async def _announce_level_up(
        self, message: discord.Message, row: dict, new_level: int,
    ) -> None:
        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "Your buddy")
        embed = (
            card(f"{emoji}  {name} leveled up!", color=C_GOLD)
            .description(f"Reached **Lv. {new_level}**.")
            .footer(f"Keep chatting to level up further  -  {fmt_ts(time.time())}")
            .build()
        )
        try:
            await message.channel.send(embed=embed)
        except discord.HTTPException:
            log.debug("buddy level-up: send failed gid=%s uid=%s",
                      message.guild.id, message.author.id)

    # -- Group ---------------------------------------------------------------

    @commands.group(name="buddy", invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy(self, ctx: DiscoContext) -> None:
        """Show your buddy's live panel, or hatch one if you have none."""
        from services.onboarding import maybe_send_intro
        await maybe_send_intro(ctx, "buddy")
        await self._show_panel(ctx)

    @buddy.command(name="stats", aliases=["info", "me"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_stats(self, ctx: DiscoContext) -> None:
        """Show your buddy's live panel."""
        await self._show_panel(ctx)

    @buddy.command(name="help")
    @guild_only
    async def buddy_help(self, ctx: DiscoContext) -> None:
        """List all buddy subcommands."""
        await ctx.send_group_help(self.buddy, title="CC Buddy Commands", color=C_NAVY)

    # -- Buddy Network economy + shop ---------------------------------------
    # All ,buddy bud / ,buddy shop / ,buddy stake / ,buddy claim /
    # ,buddy convert / ,buddy cashout / ,buddy attractor commands route
    # through services/buddy_economy.py. Burn impacts, slippage, and
    # LP-reward fan-out come for free (mirrors ore/RUNE behavior).

    @staticmethod
    def _bud_parse_amount(arg: str) -> float:
        s = (arg or "").strip().lower().replace(",", "")
        if s in ("all", "max", "*"):
            return -1.0
        try:
            return float(s)
        except ValueError:
            raise ValueError(f"Bad amount: {arg!r}")

    @buddy.command(name="shop", aliases=["bshop", "bud_shop"])
    async def buddy_shop(self, ctx: DiscoContext) -> None:
        """Show the Buddy Shop (slot upgrades + attractor, priced in BUD)."""
        from services import buddy_economy as bes
        from configs.buddies_config import (
            BATTLE_SLOTS_BASE, BATTLE_SLOTS_HARD_CAP,
            BATTLE_SLOTS_MAX_PURCHASED,
            STORAGE_SLOTS_BASE, STORAGE_SLOTS_HARD_CAP,
            STORAGE_SLOTS_MAX_PURCHASED, STORAGE_SLOTS_PER_UPGRADE,
            EGG_HELD_HARD_CAP, EGG_STORAGE_BASE, EGG_STORAGE_HARD_CAP,
            EGG_STORAGE_MAX_PURCHASED, EGG_STORAGE_PER_UPGRADE,
            NEST_SLOTS_BASE, NEST_SLOTS_HARD_CAP, NEST_SLOTS_MAX_PURCHASED,
        )
        state = await bes.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        held_raw = await bes.get_bud_wallet_raw(ctx.db, ctx.guild_id, ctx.author.id)
        battle_cap   = await bes.user_max_battle_slots(ctx.db, ctx.guild_id, ctx.author.id)
        storage_cap  = await bes.user_max_storage_slots(ctx.db, ctx.guild_id, ctx.author.id)
        egg_cap      = await bes.user_max_egg_storage(ctx.db, ctx.guild_id, ctx.author.id)
        nest_cap     = await bes.user_max_nest_slots(ctx.db, ctx.guild_id, ctx.author.id)
        battle_extra  = int(state.get("battle_slots_purchased") or 0)
        storage_extra = int(state.get("storage_slots_purchased") or 0)
        egg_extra     = int(state.get("egg_storage_slots_purchased") or 0)
        nest_extra    = int(state.get("nest_slots_purchased") or 0)
        attractor_until = state.get("attractor_until")
        active = await bes.attractor_active(ctx.db, ctx.guild_id, ctx.author.id)

        # Flat BUD prices -- the upgrade ladder never drifts under the
        # player as the BUD oracle moves. The USD tag is only a hint
        # off the live oracle so a player can eyeball the dollar cost.
        battle_cost_bud  = float(bes.BATTLE_SLOT_PRICE_BUD)
        storage_cost_bud = float(bes.STORAGE_SLOT_PRICE_BUD)
        egg_cost_bud     = float(bes.EGG_STORAGE_PRICE_BUD)
        nest_cost_bud    = float(bes.NEST_SLOT_PRICE_BUD)
        attr_cost_bud    = float(bes.ATTRACTOR_PRICE_BUD)
        bud_oracle_row = await ctx.db.get_price("BUD", ctx.guild_id)
        bud_oracle = float(bud_oracle_row["price"]) if bud_oracle_row else 0.0

        def _usd_tag(cost: float) -> str:
            return (
                f"  ~ **{fmt_usd(cost * bud_oracle)}**"
                if bud_oracle > 0 else ""
            )

        # Each row carries the shop ITEM ID (left column, the literal
        # token a player passes to the Quick Buy modal or types as a
        # subcommand), the human label, and the flat BUD price. This
        # keeps the embed copy/paste-friendly: the id and the price
        # are visible without scrolling, and the cap progression is
        # the trailing detail line.
        battle_status = (
            f"+{battle_extra}/{BATTLE_SLOTS_MAX_PURCHASED} purchased  -  "
            f"now **{battle_cap}** active "
            f"(base {BATTLE_SLOTS_BASE}, hard cap {BATTLE_SLOTS_HARD_CAP})"
        )
        storage_status = (
            f"+{storage_extra}/{STORAGE_SLOTS_MAX_PURCHASED} purchased  -  "
            f"now **{storage_cap}** stored "
            f"(base {STORAGE_SLOTS_BASE}, "
            f"+{STORAGE_SLOTS_PER_UPGRADE} per upgrade, "
            f"hard cap {STORAGE_SLOTS_HARD_CAP})"
        )
        egg_status = (
            f"+{egg_extra}/{EGG_STORAGE_MAX_PURCHASED} purchased  -  "
            f"now **{egg_cap}** eggs banked "
            f"(base {EGG_STORAGE_BASE}, "
            f"+{EGG_STORAGE_PER_UPGRADE} per upgrade, "
            f"hard cap {EGG_STORAGE_HARD_CAP}; "
            f"held with you stays at {EGG_HELD_HARD_CAP})"
        )
        nest_status = (
            f"+{nest_extra}/{NEST_SLOTS_MAX_PURCHASED} purchased  -  "
            f"now **{nest_cap}** simultaneous nests "
            f"(base {NEST_SLOTS_BASE}, hard cap {NEST_SLOTS_HARD_CAP})"
        )
        if active and attractor_until:
            attr_status = (
                f"\U00002728 Active until **{fmt_ts(attractor_until)}** "
                f"(buff x{bes.ATTRACTOR_BUFF_MULT:.1f}) -- buying again "
                f"extends the timer."
            )
        else:
            attr_status = (
                "_(no attractor active)_  -  buff x"
                f"{bes.ATTRACTOR_BUFF_MULT:.1f} for 1 hour, stacks by "
                "extending expiry."
            )

        def _item_line(name: str, cost: float, status: str, command: str) -> str:
            return (
                f"**{name}**  -  **{fmt_token(cost, 'BUD')}**{_usd_tag(cost)}\n"
                f"-# {status}\n"
                f"-# Buy: `{command}`"
            )

        embed = (
            card("\U0001F436 Buddy Shop", color=C_PURPLE)
            .description(
                "Each row shows `id` -- name -- price (BUD). Quick Buy "
                "below accepts the id (`battle` / `storage` / `eggs` / "
                "`attractor`); slash commands are listed per item. "
                "All purchases burn BUD with the standard slippage / "
                "LP fan-out the rest of the economy uses. Earn BUD via "
                "`,buddy stake fren <amt>` + `,buddy claim`, or "
                "burn-swap with `,buddy convert`."
            )
            .field(
                "`battle`  \U0001F5E1️ Battle Slot Upgrade",
                _item_line(
                    "Battle Slot Upgrade",
                    battle_cost_bud, battle_status, ",buddy slot battle buy",
                ),
                False,
            )
            .field(
                "`storage`  \U0001F4E6 Storage Slot Upgrade",
                _item_line(
                    "Storage Slot Upgrade",
                    storage_cost_bud, storage_status, ",buddy slot storage buy",
                ),
                False,
            )
            .field(
                "`eggs`  \U0001F95A Egg Storage Upgrade",
                _item_line(
                    "Egg Storage Upgrade",
                    egg_cost_bud, egg_status, ",buddy slot eggs buy",
                ),
                False,
            )
            .field(
                "`nest`  \U0001FAB9 Nest Slot Upgrade",
                _item_line(
                    "Nest Slot Upgrade",
                    nest_cost_bud, nest_status, ",buddy slot nest buy",
                ),
                False,
            )
            .field(
                "`attractor`  \U0001F9F2 Battle Attractor (1h)",
                _item_line(
                    "Battle Attractor (+1h)",
                    attr_cost_bud, attr_status, ",buddy attractor buy",
                ),
                False,
            )
            .field(
                "Your BUD wallet",
                f"**{fmt_token(to_human(held_raw), 'BUD')}**",
                True,
            )
            .footer(
                f"Battle: max {BATTLE_SLOTS_HARD_CAP}  -  "
                f"Storage: max {STORAGE_SLOTS_HARD_CAP}  -  "
                f"Eggs banked: max {EGG_STORAGE_HARD_CAP}  -  "
                f"Nests: max {NEST_SLOTS_HARD_CAP}  -  "
                "Attractor: stacks."
            )
        )
        # Quick Buy modal accepts the full item path. Templated as
        # "buddy {item} buy" so a player typing ``slot battle`` lands
        # on ``,buddy slot battle buy`` and ``attractor`` lands on
        # ``,buddy attractor buy`` -- both routes already exist and
        # carry the standard ConfirmView flow.
        view = QuickBuyView(
            ctx=ctx,
            command_template="buddy {item} buy",
            accepted_currency="BUD",
            item_label="What to buy",
            item_placeholder="slot battle | slot storage | slot eggs | slot nest | attractor",
            modal_title="Buddy Quick Buy (BUD)",
        )
        sent = await ctx.reply(
            embed=embed.build(), view=view, mention_author=False,
        )
        view.message = sent

    # ═══════════════════════════════════════════════════════════════════
    # Buddy Arena Map + Champion Tournament (Buddy Battles expansion)
    # ═══════════════════════════════════════════════════════════════════
    #
    # Note: ``,buddy arena`` is already taken by the legacy BUD-mint
    # single-fight queue (defined further down in this file). The
    # branching-zone "Pokemon Stadium" feature shipped with this
    # expansion lives under ``,buddy map`` instead -- it's the actual
    # map of arenas, so the name reads cleanly.
    #
    # ``,buddy map``              -- render the arena map PNG (current
    #                                 zone highlighted, neighbours,
    #                                 region progress).
    # ``,buddy map travel <zone>``-- move the travel cursor.
    # ``,buddy map battle``       -- fight tier-matched AI in current zone
    #                                 with an in-battle consumable
    #                                 drop-down and FPS attack-burst
    #                                 animation.
    # ``,buddy map boss``         -- fight the region boss (boss zones
    #                                 only).
    # ``,buddy map items``        -- list battle consumable inventory.
    # ``,buddy tourney``          -- show the championship bracket.
    # ``,buddy tourney start``    -- begin / resume the bracket.
    # ``,buddy tourney fight``    -- play the current bracket round.
    #
    # All implementations live in services/buddy_arena_view -- this cog
    # just wires the subcommands so they sit under the existing ``,buddy``
    # group like everything else buddy-related.

    @buddy.group(name="map", aliases=["world", "journey", "zones"],
                 invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_map(self, ctx: DiscoContext) -> None:
        """Show the Buddy Arena Map -- your current zone + neighbours."""
        from services.buddy_arena_view import show_arena_map
        await show_arena_map(ctx)

    @buddy_map.command(name="travel", aliases=["go", "move"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_map_travel(self, ctx: DiscoContext, *, zone_id: str) -> None:
        """Travel to a neighbouring zone. ``,buddy map travel <zone_id>``"""
        from services.buddy_arena_view import do_travel
        await do_travel(ctx, zone_id)

    @buddy_map.command(name="battle", aliases=["fight", "duel"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_map_battle(self, ctx: DiscoContext) -> None:
        """Fight a tier-matched AI buddy in your current zone."""
        from services.buddy_arena_view import do_zone_battle
        await do_zone_battle(ctx, is_boss=False)

    @buddy_map.command(name="boss")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_map_boss(self, ctx: DiscoContext) -> None:
        """Fight the boss of your current zone (boss zones only)."""
        from services.buddy_arena_view import do_zone_battle
        await do_zone_battle(ctx, is_boss=True)

    @buddy_map.command(name="items", aliases=["bag", "inventory", "inv"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_map_items(self, ctx: DiscoContext) -> None:
        """List your battle consumable inventory + craft hints."""
        from services.buddy_arena_view import show_items
        await show_items(ctx)

    @buddy_map.command(
        name="visit",
        aliases=["shop", "heal", "dig", "trader"],
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_map_visit(self, ctx: DiscoContext) -> None:
        """Interact with a special location at your current zone.

        ``,buddy map visit`` runs whichever flow fits the special you
        are standing on -- Mossy Market for the item shop, Ash Springs
        for a heal, Smith's Camp for a daily dig, or the Caravan
        Clearing for the rotating trader. Plain combat zones aren't
        supported here.
        """
        from services.buddy_arena_view import do_visit_special
        await do_visit_special(ctx)

    @buddy_map.command(name="buy", aliases=["purchase"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_map_buy(
        self, ctx: DiscoContext, item_key: str, qty: int = 1,
    ) -> None:
        """Buy an item from the Mossy Market (BUD)."""
        from services.buddy_arena_view import do_shop_buy
        await do_shop_buy(ctx, item_key, qty=max(1, int(qty or 1)))

    @buddy_map.command(name="trade", aliases=["redeem"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_map_trade(self, ctx: DiscoContext, slot: int) -> None:
        """Redeem one of the trader's three rotating offers (1, 2, or 3)."""
        from services.buddy_arena_view import do_trader_redeem
        await do_trader_redeem(ctx, int(slot))

    @buddy.group(name="tourney", aliases=["tournament", "champ"],
                 invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_tourney(self, ctx: DiscoContext) -> None:
        """Show the Champion Tournament bracket."""
        from services.buddy_arena_view import show_tournament
        await show_tournament(ctx)

    @buddy_tourney.command(name="start", aliases=["begin", "enter"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_tourney_start(self, ctx: DiscoContext) -> None:
        """Begin / resume the Champion Tournament bracket."""
        from services.buddy_arena_view import tourney_start_cmd
        await tourney_start_cmd(ctx)

    @buddy_tourney.command(name="fight", aliases=["play", "round"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_tourney_fight(self, ctx: DiscoContext) -> None:
        """Play the current bracket round vs scaling AI."""
        from services.buddy_arena_view import tourney_fight_cmd
        await tourney_fight_cmd(ctx)

    @buddy.group(name="slot", aliases=["slots"], invoke_without_command=True)
    async def buddy_slot(self, ctx: DiscoContext) -> None:
        """Manage buddy capacity upgrades.

        Four independent ladders, each priced in flat BUD:
          ``,buddy slot battle buy``   -- +1 active slot, max 10 total
          ``,buddy slot storage buy``  -- +10 storage rows, max 100 total
          ``,buddy slot eggs buy``     -- +50 banked-egg rows, max 1000 total
          ``,buddy slot nest buy``     -- +1 simultaneous nest, max 10 total

        With no subcommand, shows the current state of all four caps.
        """
        from services import buddy_economy as bes
        from configs.buddies_config import (
            BATTLE_SLOTS_BASE, BATTLE_SLOTS_HARD_CAP,
            BATTLE_SLOTS_MAX_PURCHASED,
            STORAGE_SLOTS_BASE, STORAGE_SLOTS_HARD_CAP,
            STORAGE_SLOTS_MAX_PURCHASED, STORAGE_SLOTS_PER_UPGRADE,
            EGG_STORAGE_BASE, EGG_STORAGE_HARD_CAP,
            EGG_STORAGE_MAX_PURCHASED, EGG_STORAGE_PER_UPGRADE,
            NEST_SLOTS_BASE, NEST_SLOTS_HARD_CAP, NEST_SLOTS_MAX_PURCHASED,
        )
        state = await bes.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        battle_cap  = await bes.user_max_battle_slots(ctx.db, ctx.guild_id, ctx.author.id)
        storage_cap = await bes.user_max_storage_slots(ctx.db, ctx.guild_id, ctx.author.id)
        egg_cap     = await bes.user_max_egg_storage(ctx.db, ctx.guild_id, ctx.author.id)
        nest_cap    = await bes.user_max_nest_slots(ctx.db, ctx.guild_id, ctx.author.id)
        battle_extra  = int(state.get("battle_slots_purchased") or 0)
        storage_extra = int(state.get("storage_slots_purchased") or 0)
        egg_extra     = int(state.get("egg_storage_slots_purchased") or 0)
        nest_extra    = int(state.get("nest_slots_purchased") or 0)

        desc = (
            f"\U0001F5E1️ **Battle slots**: {battle_cap}/"
            f"{BATTLE_SLOTS_HARD_CAP} active "
            f"(+{battle_extra}/{BATTLE_SLOTS_MAX_PURCHASED} bought, "
            f"base {BATTLE_SLOTS_BASE}). "
            f"**{fmt_token(bes.BATTLE_SLOT_PRICE_BUD, 'BUD')}** per +1.\n"
            f"`,buddy slot battle buy`\n\n"
            f"\U0001F4E6 **Storage slots**: {storage_cap}/"
            f"{STORAGE_SLOTS_HARD_CAP} stored "
            f"(+{storage_extra}/{STORAGE_SLOTS_MAX_PURCHASED} bought, "
            f"base {STORAGE_SLOTS_BASE}, "
            f"+{STORAGE_SLOTS_PER_UPGRADE} per upgrade). "
            f"**{fmt_token(bes.STORAGE_SLOT_PRICE_BUD, 'BUD')}** per upgrade.\n"
            f"`,buddy slot storage buy`\n\n"
            f"\U0001F95A **Egg storage**: {egg_cap}/"
            f"{EGG_STORAGE_HARD_CAP} eggs banked "
            f"(+{egg_extra}/{EGG_STORAGE_MAX_PURCHASED} bought, "
            f"base {EGG_STORAGE_BASE}, "
            f"+{EGG_STORAGE_PER_UPGRADE} per upgrade). "
            f"**{fmt_token(bes.EGG_STORAGE_PRICE_BUD, 'BUD')}** per upgrade.\n"
            f"`,buddy slot eggs buy`\n\n"
            f"\U0001FAB9 **Nest slots**: {nest_cap}/"
            f"{NEST_SLOTS_HARD_CAP} simultaneous "
            f"(+{nest_extra}/{NEST_SLOTS_MAX_PURCHASED} bought, "
            f"base {NEST_SLOTS_BASE}). "
            f"**{fmt_token(bes.NEST_SLOT_PRICE_BUD, 'BUD')}** per +1.\n"
            f"`,buddy slot nest buy`"
        )
        await ctx.reply(
            embed=card(
                "\U0001FAA8 Buddy Slots",
                description=desc,
                color=C_PURPLE,
            ).build(),
            mention_author=False,
        )

    async def _slot_buy_receipt(
        self, ctx: DiscoContext, *, label: str, kind: str,
        res, total_cap: int, hard_cap: int,
    ) -> None:
        """Render the success receipt for a slot upgrade purchase.

        Shared formatting between battle / storage / egg buys so all
        three receipts read the same -- title + new cap, BUD burn,
        USD value at the average pre/post oracle, and slippage line.
        """
        bud_h = to_human(int(res.bud_burned_raw))
        avg_oracle = (
            (float(res.bud_oracle_before) + float(res.bud_oracle_after)) / 2.0
            if (res.bud_oracle_before > 0 and res.bud_oracle_after > 0) else 0.0
        )
        usd_tag = f"  ~ **{fmt_usd(bud_h * avg_oracle)}**" if avg_oracle > 0 else ""
        msg = (
            f"+1 {label} (now **{int(res.new_slot_count)}** purchased, "
            f"effective cap **{int(total_cap)}**/{int(hard_cap)}).\n"
            f"Burned **{fmt_token(bud_h, 'BUD')}**{usd_tag}.\n"
            f"-# BUD oracle: ${res.bud_oracle_before:,.6f} -> "
            f"${res.bud_oracle_after:,.6f}  "
            f"(slippage {res.price_impact_pct * 100:.2f}%)"
        )
        await ctx.reply_success(msg, title=f"{label.title()} acquired")

    @buddy_slot.group(name="battle", aliases=["b", "active", "fight"], invoke_without_command=True)
    async def buddy_slot_battle(self, ctx: DiscoContext) -> None:
        """Battle slot upgrade group. Use ``,buddy slot battle buy``."""
        await self.buddy_slot(ctx)

    @buddy_slot_battle.command(name="buy", aliases=["purchase"])
    async def buddy_slot_battle_buy(self, ctx: DiscoContext) -> None:
        """Burn BUD for one extra battle (active) slot. Max 10 total."""
        from services import buddy_economy as bes
        from configs.buddies_config import BATTLE_SLOTS_HARD_CAP
        try:
            res = await bes.purchase_battle_slot(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        cap = await bes.user_max_battle_slots(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        await self._slot_buy_receipt(
            ctx, label="battle slot", kind="battle", res=res,
            total_cap=cap, hard_cap=BATTLE_SLOTS_HARD_CAP,
        )

    @buddy_slot.group(name="storage", aliases=["s", "stored", "box"], invoke_without_command=True)
    async def buddy_slot_storage(self, ctx: DiscoContext) -> None:
        """Storage slot upgrade group. Use ``,buddy slot storage buy``."""
        await self.buddy_slot(ctx)

    @buddy_slot_storage.command(name="buy", aliases=["purchase"])
    async def buddy_slot_storage_buy(self, ctx: DiscoContext) -> None:
        """Burn BUD for one storage upgrade (+10 stored buddies). Max 100."""
        from services import buddy_economy as bes
        from configs.buddies_config import STORAGE_SLOTS_HARD_CAP
        try:
            res = await bes.purchase_storage_slot(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        cap = await bes.user_max_storage_slots(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        await self._slot_buy_receipt(
            ctx, label="storage slot", kind="storage", res=res,
            total_cap=cap, hard_cap=STORAGE_SLOTS_HARD_CAP,
        )

    @buddy_slot.group(name="eggs", aliases=["egg", "e"], invoke_without_command=True)
    async def buddy_slot_eggs(self, ctx: DiscoContext) -> None:
        """Egg storage upgrade group. Use ``,buddy slot eggs buy``."""
        await self.buddy_slot(ctx)

    @buddy_slot_eggs.command(name="buy", aliases=["purchase"])
    async def buddy_slot_eggs_buy(self, ctx: DiscoContext) -> None:
        """Burn BUD for one egg-storage upgrade (+50 eggs). Max 1000."""
        from services import buddy_economy as bes
        from configs.buddies_config import EGG_STORAGE_HARD_CAP
        try:
            res = await bes.purchase_egg_storage(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        cap = await bes.user_max_egg_storage(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        await self._slot_buy_receipt(
            ctx, label="egg storage", kind="eggs", res=res,
            total_cap=cap, hard_cap=EGG_STORAGE_HARD_CAP,
        )

    @buddy_slot.group(
        name="nest",
        aliases=["nests", "n", "incubator", "incubators"],
        invoke_without_command=True,
    )
    async def buddy_slot_nest(self, ctx: DiscoContext) -> None:
        """Nest slot upgrade group. Use ``,buddy slot nest buy``."""
        await self.buddy_slot(ctx)

    @buddy_slot_nest.command(name="buy", aliases=["purchase"])
    async def buddy_slot_nest_buy(self, ctx: DiscoContext) -> None:
        """Burn BUD for one extra nest slot (+1 simultaneous incubation). Max 10."""
        from services import buddy_economy as bes
        from configs.buddies_config import NEST_SLOTS_HARD_CAP
        try:
            res = await bes.purchase_nest_slot(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        cap = await bes.user_max_nest_slots(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        await self._slot_buy_receipt(
            ctx, label="nest slot", kind="nest", res=res,
            total_cap=cap, hard_cap=NEST_SLOTS_HARD_CAP,
        )

    @buddy.group(name="attractor", aliases=["lure", "attract"], invoke_without_command=True)
    async def buddy_attractor(self, ctx: DiscoContext) -> None:
        """Show the buddy battle attractor status (`,buddy attractor buy`)."""
        from services import buddy_economy as bes
        state = await bes.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        active = await bes.attractor_active(ctx.db, ctx.guild_id, ctx.author.id)
        until = state.get("attractor_until")
        if active and until:
            from core.framework.ui import fmt_ts
            desc = (
                f"\U00002728 Active until **{fmt_ts(until)}** "
                f"(buff x{bes.ATTRACTOR_BUFF_MULT:.1f})\n"
                "`,buddy attractor buy` adds another hour."
            )
            color = C_SUCCESS
        else:
            desc = (
                "_(no attractor active)_\n"
                "`,buddy attractor buy` -- burn BUD for a 1-hour buff."
            )
            color = C_NEUTRAL
        await ctx.reply(
            embed=card("\U0001F9F2 Buddy Battle Attractor", description=desc, color=color).build(),
            mention_author=False,
        )

    @buddy_attractor.command(name="buy", aliases=["purchase", "topup"])
    async def buddy_attractor_buy(self, ctx: DiscoContext) -> None:
        """Burn BUD for a 1-hour buddy battle attractor (stacks)."""
        from services import buddy_economy as bes
        from core.framework.ui import fmt_ts
        try:
            res = await bes.purchase_attractor(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        bud_h = to_human(res.bud_burned_raw)
        avg_oracle = (
            (float(res.bud_oracle_before) + float(res.bud_oracle_after)) / 2.0
            if (res.bud_oracle_before > 0 and res.bud_oracle_after > 0) else 0.0
        )
        usd_tag = f"  ~ **{fmt_usd(bud_h * avg_oracle)}**" if avg_oracle > 0 else ""
        msg = (
            f"\U00002728 Attractor active until **{fmt_ts(res.expires_at)}** "
            f"(buff x{bes.ATTRACTOR_BUFF_MULT:.1f}).\n"
            f"Burned **{fmt_token(bud_h, 'BUD')}**{usd_tag}.\n"
            f"-# BUD oracle: ${res.bud_oracle_before:,.6f} -> "
            f"${res.bud_oracle_after:,.6f}  "
            f"(slippage {res.price_impact_pct * 100:.2f}%)"
        )
        await ctx.reply_success(msg, title="Attractor acquired")

    @buddy.command(name="stake", aliases=["lock", "stakes", "stakeinfo"])
    async def buddy_stake(
        self, ctx: DiscoContext, sym: str = "", amount: str = "",
    ) -> None:
        """Stake FREN or BBT to earn BUD passively.

        ``,buddy stake``                -- show your current stake panel
        ``,buddy stake fren <amt|all>`` -- lock FREN
        ``,buddy stake bbt  <amt|all>`` -- lock BBT
        ``,buddy stake everything``     -- lock all FREN + BBT in one shot
        """
        from services import buddy_economy as bes
        # No-args / no-symbol: open the unified stake panel (Stake /
        # Unstake / Claim / Refresh buttons -- same shape as ,farm stake
        # / ,craft stake / ,fish stake / ,delve stake).
        if not sym:
            await self._open_stake_panel(ctx)
            return

        sym_up = sym.upper().strip()
        # ,buddy stake everything -- lock all FREN AND BBT in one shot.
        if sym_up in ("EVERYTHING", "ALL", "MAX"):
            fren_held = await bes.get_fren_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            bbt_held = await bes.get_bbt_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if fren_held <= 0 and bbt_held <= 0:
                await ctx.reply_error("You have no FREN or BBT to stake.")
                return
            results: list[tuple[str, Any]] = []
            try:
                if fren_held > 0:
                    r1 = await bes.stake_fren(
                        ctx.db, ctx.guild_id, ctx.author.id, int(fren_held),
                    )
                    results.append(("FREN", r1))
                if bbt_held > 0:
                    r2 = await bes.stake_bbt(
                        ctx.db, ctx.guild_id, ctx.author.id, int(bbt_held),
                    )
                    results.append(("BBT", r2))
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            try:
                fren_p = await ctx.db.get_price("FREN", ctx.guild_id)
                bbt_p  = await ctx.db.get_price("BBT",  ctx.guild_id)
                fren_oracle = float((fren_p or {}).get("price") or 0.0)
                bbt_oracle  = float((bbt_p  or {}).get("price") or 0.0)
            except Exception:
                fren_oracle = bbt_oracle = 0.0
            sections: list[str] = []
            for token_sym, res in results:
                staked_total_raw = int(getattr(
                    res,
                    "fren_staked_raw" if token_sym == "FREN" else "bbt_staked_raw",
                    0,
                ) or 0)
                delta_raw = (
                    int(fren_held) if token_sym == "FREN" else int(bbt_held)
                )
                tok_oracle = fren_oracle if token_sym == "FREN" else bbt_oracle
                dh = to_human(delta_raw)
                th = to_human(staked_total_raw)
                usd_d = f"  ·  {fmt_usd(dh * tok_oracle)}" if tok_oracle > 0 else ""
                usd_t = f"  ·  {fmt_usd(th * tok_oracle)}" if tok_oracle > 0 else ""
                sections.append(
                    f"**{token_sym}**\n"
                    f"Staked: **{fmt_token(dh, token_sym)}**{usd_d}\n"
                    f"Total staked: **{fmt_token(th, token_sym)}**{usd_t}"
                )
            desc = "\n\n".join(sections)
            desc += (
                f"\n-# Earns {bes.FREN_STAKE_BUD_PER_DAY:.4f} BUD per token"
                f" per day (combined FREN+BBT clock)."
            )
            await ctx.reply(
                embed=card(
                    "\U0001F512 Buddy Stake All", color=C_SUCCESS,
                ).description(desc).build(),
                mention_author=False,
            )
            return

        if sym_up not in ("FREN", "BBT"):
            await ctx.reply_error_hint(
                "Only FREN or BBT can be staked on the Buddy Network.",
                hint="buddy stake fren 1000  /  buddy stake bbt all  /  buddy stake everything",
            )
            return

        try:
            amt = self._bud_parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if amt < 0:
            if sym_up == "FREN":
                held = await bes.get_fren_wallet_raw(
                    ctx.db, ctx.guild_id, ctx.author.id,
                )
            else:
                held = await bes.get_bbt_wallet_raw(
                    ctx.db, ctx.guild_id, ctx.author.id,
                )
            if held <= 0:
                await ctx.reply_error(f"You have no {sym_up} to stake.")
                return
            amt_raw = held
        else:
            amt_raw = to_raw(amt)
        try:
            if sym_up == "FREN":
                res = await bes.stake_fren(
                    ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
                )
            else:
                res = await bes.stake_bbt(
                    ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
                )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        try:
            sym_p  = await ctx.db.get_price(sym_up, ctx.guild_id)
            sym_oracle = float((sym_p or {}).get("price") or 0.0)
        except Exception:
            sym_oracle = 0.0
        if sym_up == "FREN":
            delta_h   = to_human(int(res.fren_delta_raw))
            staked_h  = to_human(int(res.fren_staked_raw))
        else:
            delta_h   = to_human(int(getattr(res, "bbt_delta_raw", 0) or 0))
            staked_h  = to_human(int(getattr(res, "bbt_staked_raw", 0) or 0))
        from core.framework.staking import stake_receipt
        await ctx.reply(
            embed=stake_receipt(
                action="Staked",
                stake_symbol=sym_up,
                delta_h=delta_h, total_h=staked_h,
                stake_oracle=sym_oracle,
                note=(
                    f"Earns {bes.FREN_STAKE_BUD_PER_DAY:.4f} BUD per "
                    f"{sym_up} per day (combined FREN+BBT clock)."
                ),
            ),
            mention_author=False,
        )

    @buddy.command(name="unstake", aliases=["unlock"])
    async def buddy_unstake(
        self, ctx: DiscoContext, sym: str = "", amount: str = "",
    ) -> None:
        """Unstake FREN or BBT (also pays accrued BUD).

        ``,buddy unstake fren <amt|all>``
        ``,buddy unstake bbt  <amt|all>``
        ``,buddy unstake everything``  -- unlock all FREN + BBT in one shot
        """
        from services import buddy_economy as bes
        sym_up = (sym or "").upper().strip()

        if sym_up in ("EVERYTHING", "ALL", "MAX"):
            state = await bes.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
            fren_staked = int(state.get("fren_staked_raw") or 0)
            bbt_staked  = int(state.get("bbt_staked_raw") or 0)
            if fren_staked <= 0 and bbt_staked <= 0:
                await ctx.reply_error("You have nothing staked.")
                return
            results: list[Any] = []
            try:
                if fren_staked > 0:
                    results.append(await bes.unstake_fren(
                        ctx.db, ctx.guild_id, ctx.author.id, int(fren_staked),
                    ))
                if bbt_staked > 0:
                    results.append(await bes.unstake_bbt(
                        ctx.db, ctx.guild_id, ctx.author.id, int(bbt_staked),
                    ))
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            try:
                fren_p = await ctx.db.get_price("FREN", ctx.guild_id)
                bbt_p  = await ctx.db.get_price("BBT",  ctx.guild_id)
                bud_p  = await ctx.db.get_price("BUD",  ctx.guild_id)
                fren_oracle = float((fren_p or {}).get("price") or 0.0)
                bbt_oracle  = float((bbt_p  or {}).get("price") or 0.0)
                bud_oracle  = float((bud_p  or {}).get("price") or 0.0)
            except Exception:
                fren_oracle = bbt_oracle = bud_oracle = 0.0
            total_yield_raw = sum(int(r.bud_yield_paid_raw) for r in results)
            from core.framework.staking import stake_receipt
            # Per-token unstake receipts so the everything-branch produces
            # the exact same embed shape as a single-token unstake.
            if int(fren_staked) > 0:
                await ctx.reply(
                    embed=stake_receipt(
                        action="Unstaked",
                        stake_symbol="FREN",
                        delta_h=to_human(int(fren_staked)),
                        total_h=0.0,
                        stake_oracle=fren_oracle,
                    ),
                    mention_author=False,
                )
            if int(bbt_staked) > 0:
                await ctx.reply(
                    embed=stake_receipt(
                        action="Unstaked",
                        stake_symbol="BBT",
                        delta_h=to_human(int(bbt_staked)),
                        total_h=0.0,
                        stake_oracle=bbt_oracle,
                    ),
                    mention_author=False,
                )
            if total_yield_raw > 0:
                from core.framework.staking import claim_receipt
                await ctx.reply(
                    embed=claim_receipt(
                        yield_symbol="BUD",
                        yield_paid_h=to_human(int(total_yield_raw)),
                        yield_oracle=bud_oracle,
                    ),
                    mention_author=False,
                )
            return

        if sym_up not in ("FREN", "BBT"):
            await ctx.reply_error_hint(
                "Only FREN or BBT can be unstaked on the Buddy Network.",
                hint="buddy unstake fren all  /  buddy unstake bbt all  /  buddy unstake everything",
            )
            return
        try:
            amt = self._bud_parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        amt_raw = (2 ** 62) if amt < 0 else to_raw(amt)
        try:
            if sym_up == "FREN":
                res = await bes.unstake_fren(
                    ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
                )
            else:
                res = await bes.unstake_bbt(
                    ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
                )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        try:
            sym_p = await ctx.db.get_price(sym_up, ctx.guild_id)
            bud_p = await ctx.db.get_price("BUD",  ctx.guild_id)
            sym_oracle = float((sym_p or {}).get("price") or 0.0)
            bud_oracle = float((bud_p or {}).get("price") or 0.0)
        except Exception:
            sym_oracle = bud_oracle = 0.0
        if sym_up == "FREN":
            delta_raw  = abs(int(res.fren_delta_raw))
            staked_raw = int(res.fren_staked_raw)
        else:
            delta_raw  = abs(int(getattr(res, "bbt_delta_raw", 0) or 0))
            staked_raw = int(getattr(res, "bbt_staked_raw", 0) or 0)
        from core.framework.staking import stake_receipt
        await ctx.reply(
            embed=stake_receipt(
                action="Unstaked",
                stake_symbol=sym_up,
                delta_h=to_human(delta_raw),
                total_h=to_human(staked_raw),
                stake_oracle=sym_oracle,
                yield_symbol="BUD",
                yield_paid_h=to_human(int(res.bud_yield_paid_raw)),
                yield_oracle=bud_oracle,
            ),
            mention_author=False,
        )

    @buddy.command(name="claim", aliases=["yield", "harvest"])
    async def buddy_claim(self, ctx: DiscoContext) -> None:
        """Claim accrued BUD yield. Stake stays locked."""
        from services import buddy_economy as bes
        try:
            res = await bes.claim_yield(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        try:
            bud_p = await ctx.db.get_price("BUD", ctx.guild_id)
            bud_oracle = float((bud_p or {}).get("price") or 0.0)
        except Exception:
            bud_oracle = 0.0
        from core.framework.staking import claim_receipt
        # Combined FREN + BBT stake powers one BUD clock; show the
        # aggregate "stake" line so the receipt mirrors farm/craft/fish.
        total_staked_h = to_human(
            int(res.fren_staked_raw) + int(res.bbt_staked_raw)
        )
        await ctx.reply(
            embed=claim_receipt(
                yield_symbol="BUD",
                yield_paid_h=to_human(int(res.bud_yield_paid_raw)),
                yield_oracle=bud_oracle,
                stake_symbol="FREN+BBT",
                total_staked_h=total_staked_h,
            ),
            mention_author=False,
        )

    async def _open_stake_panel(self, ctx: DiscoContext) -> None:
        """Open the unified stake panel for FREN/BBT -> BUD."""
        from services import buddy_economy as bes
        from core.framework.staking import StakeAdapter, StakePanelView, StakeToken

        async def _state(c: DiscoContext) -> dict:
            state = await bes.ensure_state(c.db, c.guild_id, c.author.id)
            fren_staked = int(state.get("fren_staked_raw") or 0)
            bbt_staked  = int(state.get("bbt_staked_raw") or 0)
            pending_raw = int(
                await bes.accrued_yield(c.db, c.guild_id, c.author.id) or 0
            )
            fren_held = int(
                await bes.get_fren_wallet_raw(c.db, c.guild_id, c.author.id) or 0
            )
            bbt_held = int(
                await bes.get_bbt_wallet_raw(c.db, c.guild_id, c.author.id) or 0
            )
            try:
                fren_p = await c.db.get_price("FREN", c.guild_id)
                bbt_p  = await c.db.get_price("BBT",  c.guild_id)
                bud_p  = await c.db.get_price("BUD",  c.guild_id)
                fren_oracle = float((fren_p or {}).get("price") or 0.0)
                bbt_oracle  = float((bbt_p  or {}).get("price") or 0.0)
                bud_oracle  = float((bud_p  or {}).get("price") or 0.0)
            except Exception:
                fren_oracle = bbt_oracle = bud_oracle = 0.0
            total_staked_h = to_human(fren_staked + bbt_staked)
            daily_h = total_staked_h * float(bes.FREN_STAKE_BUD_PER_DAY)
            return {
                "staked_by_sym": {"FREN": fren_staked, "BBT": bbt_staked},
                "wallet_by_sym": {"FREN": fren_held,   "BBT": bbt_held},
                "stake_oracle_by_sym": {"FREN": fren_oracle, "BBT": bbt_oracle},
                "yield_oracle": bud_oracle,
                "pending_raw": pending_raw,
                "daily_rate_raw": int(to_raw(daily_h)),
            }

        async def _stake(c: DiscoContext, raw: int, sym: str) -> int:
            res = await bes.stake_token(
                c.db, c.guild_id, c.author.id,
                symbol=sym, amount_raw=int(raw),
            )
            return (
                int(res.fren_staked_raw) if sym == "FREN"
                else int(res.bbt_staked_raw)
            )

        async def _unstake(c: DiscoContext, raw: int, sym: str) -> int:
            res = await bes.unstake_token(
                c.db, c.guild_id, c.author.id,
                symbol=sym, amount_raw=int(raw),
            )
            return (
                int(res.fren_staked_raw) if sym == "FREN"
                else int(res.bbt_staked_raw)
            )

        async def _claim(c: DiscoContext) -> int:
            res = await bes.claim_yield(c.db, c.guild_id, c.author.id)
            return int(getattr(res, "bud_yield_paid_raw", 0) or 0)

        adapter = StakeAdapter(
            title="\U0001F4CC Buddy Stake (FREN + BBT -> BUD)",
            color=C_PURPLE,
            stake_tokens=[StakeToken("FREN"), StakeToken("BBT")],
            yield_symbol="BUD",
            get_state=_state, do_stake=_stake,
            do_unstake=_unstake, do_claim=_claim,
            note=(
                f"Stake FREN or BBT to drip BUD. Yield: "
                f"{bes.FREN_STAKE_BUD_PER_DAY:.4f} BUD per token per day "
                f"(combined FREN+BBT clock)."
            ),
        )
        await StakePanelView.send(ctx, adapter)

    @buddy.command(name="convert", aliases=["burnswap", "econswap", "budswap"])
    async def buddy_convert(
        self, ctx: DiscoContext, sym_in: str = "", sym_out: str = "",
        amount: str = "", max_slip: str = "",
    ) -> None:
        """Burn-swap BUD against any registered partner.

        ``,buddy swap`` is the species-change command (changes which
        species your active buddy is). The token burn-swap lives here
        as ``,buddy convert`` to avoid the collision.

        Carve-outs allowed (all bidirectional, BUD must be on one side):
          BUD <-> FREN / REEL / RUNE / MOON / HRV / BBT / INGOT / SAGE
          BUD <-> GBC / GAMBIT / CROWN / VEIN / PIP / EDGE / ACE / NOIR / CHERRY

        The Gamba Network leg (GBC + the eight game tokens) closes the
        circular buddy <-> gamba loop: burn BUD for any game token,
        stake it on the gamba surface for GBC drip, then convert GBC
        back to BUD whenever you want. No USD round-trip required.

        SAGE follows the same earn-only firewall pattern as GBC / INGOT:
        BUD <-> SAGE is bidirectional, but EDU (the Sage game token)
        stays out so the stake-yield loop is not collapsed into a direct
        sell path.

        Slippage applies on both oracles. Use ``all`` to dump the full
        wallet of the input token. Use ``,buddy quote`` to preview a
        swap (rate / pool depth / impact / slippage) without executing.

        Optional 4th arg ``max_slip`` is a max-slippage gate in percent
        (e.g. ``,buddy convert reel bud 100 5`` aborts if the predicted
        slippage exceeds 5%). Without it the swap fills at whatever the
        oracle says. ``max_slip 0`` disables the gate explicitly.
        """
        from services import buddy_economy as bes
        si, so = (sym_in or "").upper(), (sym_out or "").upper()
        legal_out = frozenset(Config.BUD_SWAPPABLE_TOKENS) | {"BUD"}
        legal_in  = legal_out | frozenset(Config.BUD_ONEWAY_IN_TOKENS)
        partners_bi  = " / ".join(sorted(legal_out - {"BUD"}))
        if si not in legal_in or so not in legal_out or si == so:
            await ctx.reply_error_hint(
                f"Pair must include BUD on at least one side: BUD <-> {partners_bi}.",
                hint="buddy convert fren bud 100",
            )
            return
        if "BUD" not in (si, so):
            await ctx.reply_error("Buddy swaps must touch BUD on at least one side.")
            return
        try:
            amt = self._bud_parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        # Optional slippage cap. Strips a trailing ``%`` so both ``5`` and
        # ``5%`` work. ``max_slip 0`` is treated as "explicitly no gate".
        max_slip_pct: float | None = None
        if max_slip:
            _ms = max_slip.strip().rstrip("%").strip()
            try:
                max_slip_pct = float(_ms)
            except ValueError:
                await ctx.reply_error(f"max_slip must be a number, got `{max_slip}`.")
                return
            if max_slip_pct < 0.0 or max_slip_pct > 100.0:
                await ctx.reply_error("max_slip must be between 0 and 100 (percent).")
                return

        if amt < 0:
            held = await bes._wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id, si,
            )
            if held <= 0:
                await ctx.reply_error(f"You have no {si} to swap.")
                return
            amt_raw = held
        else:
            amt_raw = to_raw(amt)

        # Pre-flight quote when a slip gate was supplied. We still hand the
        # actual swap off to ``_generic_burn_swap`` afterwards -- the quote
        # is purely a guard, not a settlement.
        if max_slip_pct is not None and max_slip_pct > 0.0:
            try:
                _quote = await bes.quote_burn_swap(
                    ctx.db, ctx.guild_id, ctx.author.id, si, so, amt_raw,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            _slip_pct = _quote.slippage_pct * 100.0
            if _slip_pct > max_slip_pct:
                await ctx.reply_error_hint(
                    f"Predicted slippage **{_slip_pct:.2f}%** exceeds your cap "
                    f"of **{max_slip_pct:.2f}%**. Trade aborted.",
                    hint=f"buddy quote {si.lower()} {so.lower()} {amount}  "
                         f"-- inspect, then raise max_slip or split into smaller "
                         f"swaps.",
                    command_name="buddy convert",
                )
                return

        try:
            if si == "BUD":
                res = await bes.burn_bud_for(
                    ctx.db, ctx.guild_id, ctx.author.id, so, amt_raw,
                )
            else:
                res = await bes.burn_for_bud(
                    ctx.db, ctx.guild_id, ctx.author.id, si, amt_raw,
                )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        in_h  = to_human(res.amount_in_raw)
        out_h = to_human(res.amount_out_raw)
        desc = (
            f"Burned **{fmt_token(in_h, res.sym_in)}** "
            f"-> minted **{fmt_token(out_h, res.sym_out)}**\n"
            f"-# {res.sym_in} oracle: ${res.in_oracle_before:,.6f} -> "
            f"${res.in_oracle_after:,.6f}\n"
            f"-# {res.sym_out} oracle: ${res.out_oracle_before:,.6f} -> "
            f"${res.out_oracle_after:,.6f}\n"
            f"-# Slippage: **{res.price_impact_pct * 100:.2f}%**"
        )
        if res.lp_reward_usd > 0:
            desc += f"\n-# Paid **{fmt_usd(res.lp_reward_usd)}** to LP holders."
        await ctx.reply(
            embed=card("\U0001F525 Buddy Burn-Swap", color=C_AMBER).description(desc).build(),
            mention_author=False,
        )

    @buddy.command(name="quote", aliases=["preview", "rate", "swapquote"])
    async def buddy_quote(
        self, ctx: DiscoContext, sym_in: str = "", sym_out: str = "", amount: str = "",
    ) -> None:
        """Preview a ``,buddy convert`` burn-swap without executing it.

        Shows the spot vs. effective exchange rate, synthetic-pool depth on
        each side (oracle x circulating supply), per-side oracle price
        impact, headline slippage, the LP-reward fee, and the estimated
        output amount. Same legal pairs as ``,buddy convert``.
        """
        from services import buddy_economy as bes
        si, so = (sym_in or "").upper(), (sym_out or "").upper()
        legal_out = frozenset(Config.BUD_SWAPPABLE_TOKENS) | {"BUD"}
        legal_in  = legal_out | frozenset(Config.BUD_ONEWAY_IN_TOKENS)
        partners_bi  = " / ".join(sorted(legal_out - {"BUD"}))
        if not si or not so or not amount:
            await ctx.reply_error_hint(
                "Usage: `,buddy quote <in> <out> <amount>`.",
                hint=f"buddy quote bud reel 100  (legal: BUD <-> {partners_bi})",
                command_name="buddy quote",
            )
            return
        if si not in legal_in or so not in legal_out or si == so:
            await ctx.reply_error_hint(
                f"Pair must include BUD on at least one side: BUD <-> {partners_bi}.",
                hint="buddy quote fren bud 100",
            )
            return
        if "BUD" not in (si, so):
            await ctx.reply_error("Buddy swaps must touch BUD on at least one side.")
            return
        try:
            amt = self._bud_parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if amt < 0:
            held = await bes._wallet_raw(ctx.db, ctx.guild_id, ctx.author.id, si)
            if held <= 0:
                await ctx.reply_error(f"You have no {si} to swap.")
                return
            amt_raw = held
        else:
            amt_raw = to_raw(amt)
        try:
            q = await bes.quote_burn_swap(
                ctx.db, ctx.guild_id, ctx.author.id, si, so, amt_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        in_h, out_h = q.amount_in_human, q.amount_out_human
        spot_line = (
            f"Spot: 1 {q.sym_in} = **{q.spot_rate:,.6f} {q.sym_out}**\n"
            f"Effective: 1 {q.sym_in} = **{q.effective_rate:,.6f} {q.sym_out}**"
        )
        pool_line = (
            f"{q.sym_in}: **{fmt_usd(q.in_pool_usd)}**\n"
            f"{q.sym_out}: **{fmt_usd(q.out_pool_usd)}**"
        )
        impact_line = (
            f"{q.sym_in}: **{fmt_pct(-q.in_impact_pct * 100.0)}**\n"
            f"{q.sym_out}: **{fmt_pct(q.out_impact_pct * 100.0)}**"
        )
        slip_line = (
            f"**{q.slippage_pct * 100.0:.2f}%** vs spot\n"
            f"-# max-side oracle impact **{q.price_impact_pct * 100.0:.2f}%**"
        )
        oracle_line = (
            f"{q.sym_in}: ${q.in_oracle:,.6f} -> ${q.in_oracle_after:,.6f}\n"
            f"{q.sym_out}: ${q.out_oracle:,.6f} -> ${q.out_oracle_after:,.6f}"
        )
        embed = (
            card(f"\U0001F50D Buddy Swap Quote -- {q.sym_in} -> {q.sym_out}", color=C_INFO)
            .description(
                f"Burn **{fmt_token(in_h, q.sym_in)}** "
                f"-> receive ~**{fmt_token(out_h, q.sym_out)}** "
                f"(USD value **{fmt_usd(q.usd_value)}**)"
            )
            .field("Rate", spot_line, True)
            .field("Pool depth (oracle x supply)", pool_line, True)
            .field("Price impact", impact_line, True)
            .field("Slippage", slip_line, True)
            .field("Oracle move (post-swap)", oracle_line, False)
            .footer(
                f"LP fee: {fmt_usd(q.lp_reward_usd)}  |  "
                f"Run ,buddy convert {q.sym_in.lower()} {q.sym_out.lower()} "
                f"{amount} to execute."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @buddy.command(name="pools", aliases=["pairs", "swaps", "markets"])
    async def buddy_pools(self, ctx: DiscoContext, sample: str = "100") -> None:
        """List every BUD swap pair with rate / depth / sample-impact.

        For each legal pair (all bidirectional now) shows the current
        spot rate (BUD <-> partner at oracle), synthetic-pool depth on
        each side (oracle x circulating supply), and the slippage a
        sample swap of the given size would feel in each direction.
        ``sample`` defaults to **100 BUD-equivalent USD** -- pass any
        number to rescale (e.g. ``,buddy pools 1000`` for a $1k probe).
        """
        from services import buddy_economy as bes

        try:
            sample_usd = float(sample.strip().rstrip("$").replace(",", "")) if sample else 100.0
        except ValueError:
            await ctx.reply_error(f"Bad sample size `{sample}` -- pass a USD number.")
            return
        if sample_usd <= 0:
            await ctx.reply_error("sample must be positive.")
            return

        legal_out = frozenset(Config.BUD_SWAPPABLE_TOKENS) | {"BUD"}
        # legal_in still unions the (now-empty) one-way set so future
        # carve-outs slot back in without touching this command.
        legal_in  = legal_out | frozenset(Config.BUD_ONEWAY_IN_TOKENS)
        partners_bi = sorted(legal_out - {"BUD"})

        bud_oracle = await bes._oracle_price(ctx.db, ctx.guild_id, "BUD")
        if bud_oracle <= 0:
            await ctx.reply_error("BUD oracle price is currently zero -- try again later.")
            return
        # Sample size in raw BUD so we can quote both directions consistently.
        bud_sample_raw = to_raw(sample_usd / bud_oracle)

        # Aggregate LP exposure per token so we can mark each pair with a
        # 🟢 dot when the user is earning burn-fee rewards on it.
        # ``_distribute_burn_lp_reward`` (services/buddy_economy.py) pays
        # USD to LP holders of any non-vault pool containing the burned
        # symbol on each side -- so for a pair BUD<->X the player is on
        # the receiving end as long as they hold LP in any pool that
        # touches X (BUD itself is earn-only and has no auto-seeded pool).
        # One bulk query keeps the per-pair loop cheap.
        try:
            lp_rows = await ctx.db.fetch_all(
                "SELECT lp.lp_shares::float / NULLIF(p.total_lp, 0) AS pct, "
                "       p.token_a, p.token_b "
                "  FROM lp_positions lp "
                "  JOIN pools p "
                "    ON p.pool_id = lp.pool_id "
                "   AND p.guild_id = lp.guild_id "
                " WHERE lp.guild_id = $1 "
                "   AND lp.user_id = $2 "
                "   AND lp.lp_shares > 0 "
                "   AND COALESCE(p.vault_locked, FALSE) = FALSE",
                ctx.guild_id, ctx.author.id,
            ) or []
        except Exception:
            lp_rows = []
        user_lp_by_sym: dict[str, float] = {}
        for r in lp_rows:
            try:
                pct = float(r.get("pct") or 0.0) * 100.0
            except (TypeError, ValueError):
                pct = 0.0
            if pct <= 0:
                continue
            for tok in (r.get("token_a"), r.get("token_b")):
                if not tok:
                    continue
                user_lp_by_sym[str(tok).upper()] = (
                    user_lp_by_sym.get(str(tok).upper(), 0.0) + pct
                )

        rows: list[str] = []
        for sym in partners_bi:
            try:
                # Direction 1: BUD -> partner
                q_out = await bes.quote_burn_swap(
                    ctx.db, ctx.guild_id, ctx.author.id, "BUD", sym, bud_sample_raw,
                )
                # Direction 2: partner -> BUD, sized to match the same USD
                partner_oracle = q_out.out_oracle
                if partner_oracle <= 0:
                    continue
                partner_sample_raw = to_raw(sample_usd / partner_oracle)
                q_in = await bes.quote_burn_swap(
                    ctx.db, ctx.guild_id, ctx.author.id, sym, "BUD", partner_sample_raw,
                )
            except ValueError:
                continue
            # Aggregate LP share across every pool containing the partner
            # token; same green-dot convention as ,trade pool list.
            partner_share = user_lp_by_sym.get(sym, 0.0)
            lp_mark = f" 🟢{partner_share:.1f}%" if partner_share > 0 else ""
            rows.append(
                f"**BUD <-> {sym}**{lp_mark}  `1 BUD = {q_out.spot_rate:,.4f} {sym}`\n"
                f"-# depth: BUD **{fmt_usd(q_out.in_pool_usd)}** / "
                f"{sym} **{fmt_usd(q_out.out_pool_usd)}**  |  "
                f"slip @ {fmt_usd(sample_usd)}: "
                f"BUD->{sym} **{q_out.slippage_pct * 100.0:.2f}%**, "
                f"{sym}->BUD **{q_in.slippage_pct * 100.0:.2f}%**"
            )

        if not rows:
            await ctx.reply_error("No buddy-swap pools have live oracle prices yet.")
            return

        builder = (
            card("\U0001F300 Buddy Swap Pools", color=C_TEAL)
            .description(
                f"Synthetic pool depth = oracle x circulating supply. "
                f"Slippage probed at **{fmt_usd(sample_usd)}** per direction "
                f"(BUD oracle: ${bud_oracle:,.6f})."
            )
        )
        # Chunk pair rows so we never blow Discord's 1024-char field cap.
        # Each row is a 2-line block joined with a blank line, so we pack
        # rows into successive "Pairs" fields, flushing whenever the next
        # row would push the buffer over 1000 chars.
        idx = 0
        buf = ""
        for row in rows:
            sep = "\n\n" if buf else ""
            if len(buf) + len(sep) + len(row) > 1000 and buf:
                builder.field(
                    "Pairs" if idx == 0 else f"Pairs (cont)",
                    buf, False,
                )
                buf = row
                idx += 1
            else:
                buf += sep + row
        if buf:
            builder.field(
                "Pairs" if idx == 0 else f"Pairs (cont)",
                buf, False,
            )
        embed = (
            builder
            .footer(
                "🟢 = you have LP earning burn-fee rewards on this pair  ·  "
                ",buddy quote <in> <out> <amt> for a real-size quote  ·  "
                ",buddy convert <in> <out> <amt> [max_slip%] to execute."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @buddy.command(name="cashout", aliases=["sellbud", "withdraw"])
    async def buddy_cashout(self, ctx: DiscoContext, amount: str = "") -> None:
        """Burn BUD -> credit USD wallet at oracle minus impact."""
        from services import buddy_economy as bes
        try:
            amt = self._bud_parse_amount(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if amt < 0:
            held = await bes.get_bud_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if held <= 0:
                await ctx.reply_error("You have no BUD to cash out.")
                return
            amt_raw = held
        else:
            amt_raw = to_raw(amt)
        try:
            res = await bes.cashout_bud(
                ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        from core.framework.staking import cashout_receipt
        await ctx.reply(
            embed=cashout_receipt(
                burned_symbol="BUD",
                burned_h=to_human(int(res.bud_burned_raw)),
                usd_credited_h=to_human(int(res.usd_credited_raw)),
                oracle_before=float(res.bud_oracle_before),
                oracle_after=float(res.bud_oracle_after),
                impact_pct=float(res.price_impact_pct),
                revenue_usd=float(res.revenue_usd or 0.0),
                lp_reward_usd=float(res.lp_reward_usd or 0.0),
            ),
            mention_author=False,
        )

    # -- Hatch ---------------------------------------------------------------

    @buddy.command(name="hatch")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_hatch(self, ctx: DiscoContext) -> None:
        """Hatch a new buddy. First HATCH_FREE_COUNT lifetime hatches are
        free; after that the price starts at HATCH_BASE_PRICE_USD and
        doubles per hatch (resets to base after 7 days idle). You can hold
        up to MAX_OWNED_BUDDIES; the first hatch is your active buddy,
        subsequent hatches slot in as resting collection members (promote
        via the panel)."""
        gid = ctx.guild_id
        uid = ctx.author.id

        count = await _count_owned(ctx.db, gid, uid)
        # Honour any extra slots the user bought via `,buddy slot buy`
        # (services/buddy_economy.user_max_buddies). Falls back cleanly
        # to the base cap if the row hasn't been seeded yet.
        try:
            from services import buddy_economy as _bes
            max_owned = await _bes.user_max_buddies(ctx.db, gid, uid)
        except Exception:
            max_owned = MAX_OWNED_BUDDIES
        if count >= max_owned:
            await ctx.reply_error(
                f"You already have the max of **{max_owned}** buddies. "
                f"Surrender one (with `,buddy surrender`), buy a slot "
                f"(`,buddy slot buy`, priced in BUD), or pay for a hatch.",
            )
            return

        # Hatch pricing state lives on the same row that the lifetime hatch
        # log uses, so we read it in one query. paid_streak / last_paid_hatch_at
        # drive the doubling-cost curve added in migration 0137.
        state_row = await ctx.db.fetch_one(
            "SELECT "
            "  COALESCE(hatch_count, 0)  AS hatch_count, "
            "  COALESCE(paid_streak, 0)  AS paid_streak, "
            "  EXTRACT(EPOCH FROM (NOW() - last_paid_hatch_at))::bigint AS paid_age_s "
            "FROM cc_buddy_hatches WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )

        # Compute the hatch fee. First HATCH_FREE_COUNT lifetime hatches are
        # free; after that the price doubles each hatch in the current paid
        # streak. The streak resets when the gap since the last paid hatch
        # exceeds HATCH_STREAK_RESET_SECONDS, so a long break drops the
        # next hatch back to the base price.
        lifetime = int((state_row or {}).get("hatch_count") or 0)
        prev_streak = int((state_row or {}).get("paid_streak") or 0)
        paid_age_s = (state_row or {}).get("paid_age_s")
        streak_for_pricing = (
            0
            if paid_age_s is None or int(paid_age_s) >= HATCH_STREAK_RESET_SECONDS
            else prev_streak
        )
        if lifetime < HATCH_FREE_COUNT:
            cost_usd = 0
            free_left_after = HATCH_FREE_COUNT - lifetime - 1
        else:
            cost_usd = HATCH_BASE_PRICE_USD * (2 ** streak_for_pricing)
            free_left_after = 0

        # Confirm before charging real money. Free hatches skip the prompt
        # so casual onboarding stays one-click.
        if cost_usd > 0:
            next_cost = HATCH_BASE_PRICE_USD * (2 ** (streak_for_pricing + 1))
            confirmed = await ctx.confirm(
                f"Hatch a new buddy for **{fmt_usd(cost_usd)}**?\n\n"
                f"You've used your {HATCH_FREE_COUNT} free hatches. The next "
                f"hatch after this one will cost **{fmt_usd(next_cost)}**, "
                f"and the price keeps doubling. Wait **7 days** without "
                f"hatching and the cost resets back to "
                f"**{fmt_usd(HATCH_BASE_PRICE_USD)}**.",
            )
            if not confirmed:
                return

        species = pick_hatch_species()
        name = await generate_name(species, ctx.db, gid)
        be_active = count == 0
        # Rarity is rolled independently of species: any species can land
        # at any tier. Stored rarity_tier becomes the buddy's permanent
        # identity (swap preserves it; reroll replaces the buddy entirely).
        tier = roll_rarity()

        # Single transaction: charge the user, upsert the lifetime hatch
        # log (creating it for first-time hatchers), and create the buddy
        # row. If any step raises, the whole thing rolls back so we never
        # take money without delivering a buddy or vice versa.
        try:
            async with ctx.db.transaction() as conn:
                if cost_usd > 0:
                    # deduct_liquid_in_conn participates in this transaction,
                    # so a downstream INSERT failure rolls back the deduction
                    # too. Raises ValueError on insufficient balance.
                    await ctx.db.deduct_liquid_in_conn(
                        conn, uid, gid, to_raw(cost_usd)
                    )
                    new_streak = streak_for_pricing + 1
                    await conn.execute(
                        "INSERT INTO cc_buddy_hatches "
                        "  (guild_id, user_id, first_species, hatch_count, "
                        "   paid_streak, last_paid_hatch_at) "
                        "VALUES ($1, $2, $3, 1, $4, NOW()) "
                        "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
                        "  hatch_count = cc_buddy_hatches.hatch_count + 1, "
                        "  paid_streak = $4, "
                        "  last_paid_hatch_at = NOW()",
                        gid, uid, species, new_streak,
                    )
                else:
                    await conn.execute(
                        "INSERT INTO cc_buddy_hatches "
                        "  (guild_id, user_id, first_species, hatch_count) "
                        "VALUES ($1, $2, $3, 1) "
                        "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
                        "  hatch_count = cc_buddy_hatches.hatch_count + 1",
                        gid, uid, species,
                    )
                from configs.buddies_config import roll_gender as _roll_gender
                await conn.execute(
                    "INSERT INTO cc_buddies ("
                    "  guild_id, owner_user_id, species, name, status, "
                    "  is_active, rarity_tier, gender"
                    ") VALUES ($1, $2, $3, $4, 'owned', $5, $6, $7)",
                    gid, uid, species, name, be_active, tier,
                    _roll_gender(),
                )
        except ValueError as exc:
            # Insufficient balance from deduct_liquid. The transaction was
            # rolled back, so no buddy was created and no money moved.
            await ctx.reply_error(
                f"Insufficient funds for this hatch. Cost: **{fmt_usd(cost_usd)}** "
                f"(wallet + bank). {exc}"
            )
            return
        except Exception:
            log.exception("buddy hatch: txn failed gid=%s uid=%s", gid, uid)
            await ctx.reply_error("Hatch failed. Please try again.")
            return

        meta = SPECIES.get(species, {})
        tier_name = rarity_meta(tier).get("name", "Common")
        slot_note = "" if be_active else " (resting -- promote from the panel to activate)"
        if cost_usd > 0:
            next_cost = HATCH_BASE_PRICE_USD * (2 ** (streak_for_pricing + 1))
            cost_note = (
                f"\nPaid: **{fmt_usd(cost_usd)}**. "
                f"Next hatch: **{fmt_usd(next_cost)}** "
                f"(resets to {fmt_usd(HATCH_BASE_PRICE_USD)} after 7 days idle)."
            )
        elif free_left_after > 0:
            cost_note = f"\nFree hatches remaining: **{free_left_after}** of {HATCH_FREE_COUNT}."
        else:
            cost_note = (
                f"\nThat was your last free hatch. The next one costs "
                f"**{fmt_usd(HATCH_BASE_PRICE_USD)}**."
            )
        await ctx.reply_success(
            f"{meta.get('emoji', '')}  You hatched **{name}** (a {tier_name} {species}){slot_note}.\n"
            f"*{meta.get('tagline', '')}*\n"
            f"Bonus: **{meta.get('bonus_label', '-')}**"
            f"{cost_note}\n\n"
            f"Use `{await ctx.get_guild_prefix()}buddy` to open the panel.",
            title="A new buddy!",
        )

    # -- Talk (standalone command form; panel has its own button) -----------

    @buddy.command(name="talk", aliases=["chat", "say"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_talk")
    async def buddy_talk(self, ctx: DiscoContext, *, message: str | None = None) -> None:
        """Talk to your active buddy. They remember the conversation.

        Passes your optional ``message`` to the AI so the buddy can react
        to what you actually said. With no message, the buddy just says
        something in character. Shares the Talk-button cooldown so a user
        can't spam this to bypass the in-panel cooldown.
        """
        gid = ctx.guild_id
        uid = ctx.author.id

        row = await _fetch_active(ctx.db, gid, uid)
        if not row:
            await ctx.reply_error_action(
                "You don't have an active buddy to talk to.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return

        # Reuse the panel's per-state talk cooldown so button + command
        # share one limiter. State id is per (user, channel) so it's safe
        # to call from any channel the user is in.
        state_id = _panel_state_id(uid, ctx.channel.id)
        per_action = _action_last.setdefault(state_id, {})
        now = time.time()
        last = per_action.get("talk", 0.0)
        if now - last < TALK_COOLDOWN_S:
            await ctx.reply_cooldown(TALK_COOLDOWN_S - (now - last))
            return
        per_action["talk"] = now

        owner_label = owner_label_for(
            getattr(ctx.author, "display_name", None) or ctx.author.name,
            uid,
        )
        extra = message.strip() if message else None
        # Pass owner progression so the buddy can reference streak / pass
        # tier / achievements in its reply when it feels natural.
        owner_prog: str | None = None
        try:
            from services.progression import (
                user_snapshot as _prog_snapshot,
                ai_context_line as _prog_ai_line,
            )
            _snap = await _prog_snapshot(ctx.db, uid, gid)
            owner_prog = _prog_ai_line(_snap)
        except Exception:
            log.debug("buddy talk: owner progression snapshot failed", exc_info=True)
        line = await generate_reply(
            dict(row), owner_label, "talk",
            extra=(f"the owner said: {extra}" if extra else None),
            owner_progression=owner_prog,
        )

        # Bump interaction stats so the active buddy gets a bit of love
        # from the standalone command too. Mirrors the Talk button.
        updated = await ctx.db.fetch_one(
            "UPDATE cc_buddies SET "
            "  happiness = GREATEST(0, LEAST(100, happiness + $3)), "
            "  energy    = GREATEST(0, LEAST(100, energy    + $4)), "
            "  last_interacted_at = NOW(), "
            "  updated_at = NOW() "
            "WHERE guild_id = $1 AND owner_user_id = $2 "
            "  AND status = 'owned' AND is_active "
            "RETURNING id",
            gid, uid, TALK_HAPPINESS_DELTA, TALK_ENERGY_DELTA,
        )
        if updated:
            summary = (
                f"owner said: {extra} | I said: {line}"
                if extra else f"owner talked; I said: {line}"
            )
            await record_event(ctx.db, int(updated["id"]), "talk", summary)

        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "Your buddy")
        embed = (
            card(f"{emoji}  {name}", color=int(
                rarity_meta(int(row.get("rarity_tier") or 1))
                .get("color_hex") or C_NAVY
            ))
            .description(f"> {line}")
            .footer(f"{fmt_ts(time.time())}")
            .build()
        )
        await ctx.send_embed(embed)


    # -- Rename (command form; panel uses the modal directly) ----------------

    @buddy.command(name="rename")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_rename(self, ctx: DiscoContext, *, new_name: str) -> None:
        """Rename your active buddy. Costs ${RENAME_PRICE_USD:,} per rename."""
        gid = ctx.guild_id
        uid = ctx.author.id

        row = await _fetch_active(ctx.db, gid, uid)
        if not row:
            await ctx.reply_error("You don't have an active buddy. Try `buddy hatch` first.")
            return

        ok, err = validate_rename(new_name)
        if not ok:
            await ctx.reply_error(err)
            return

        ok, msg = await _charge_and_rename(
            ctx.db,
            owner_id=uid,
            guild_id=gid,
            buddy_id=int(row["id"]),
            new_name=new_name.strip(),
        )
        if ok:
            await ctx.reply_success(msg, title="Renamed")
        else:
            await ctx.reply_error(msg)

    # -- Panel renderer ------------------------------------------------------

    async def _show_panel(self, ctx: DiscoContext) -> None:
        gid = ctx.guild_id
        uid = ctx.author.id

        pages = await _fetch_all_owned(ctx.db, gid, uid)
        if not pages:
            await ctx.reply_error_action(
                "You don't have a buddy yet.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return

        state_id = _panel_state_id(uid, ctx.channel.id)
        view = BuddyPanelView(self, uid, state_id)
        # Pre-render the owner's progression strip once. Computed here
        # instead of inside _build_panel_embed so the live-tick re-render
        # doesn't pay a DB query on every frame. Stale-by-seconds is
        # fine -- achievements + pass tier move slowly.
        try:
            from services.progression import (
                user_snapshot as _prog_snapshot,
                format_inline as _prog_inline,
            )
            _snap = await _prog_snapshot(self.bot.db, uid, gid)
            view.owner_progression = _prog_inline(_snap)
        except Exception:
            log.debug("buddy panel: owner progression snapshot failed", exc_info=True)
        # Initial paint: page 0 (view._load_pages re-sorts and snaps index).
        view._apply_button_states(pages[0], len(pages))
        view._rebuild_buddy_select(pages)
        embed = _build_panel_embed(
            pages[0], state_id, page_idx=0, page_total=len(pages),
            owner_progression=view.owner_progression,
        )

        msg = await ctx.reply(embed=embed, view=view, mention_author=False)

        # The live tick closes over the view so pagination state survives
        # across background re-renders. get_data returns the current page
        # row; render formats it with the current page index.
        async def _get_data() -> dict | None:
            live_pages = await _fetch_all_owned(self.bot.db, gid, uid)
            if not live_pages:
                return None
            view.page_idx = max(0, min(view.page_idx, len(live_pages) - 1))
            view._apply_button_states(live_pages[view.page_idx], len(live_pages))
            # NOTE: do NOT rebuild the _BuddySelect here. The live tick
            # only edits the embed via ``msg.edit(embed=...)`` and does
            # NOT push the view to Discord. If we mutated self.children
            # here, Discord would still show the stale dropdown component
            # while our in-memory view holds a new (different-id) one --
            # discord.py then logs "View interaction referencing unknown
            # view for item <_BuddySelect ... id=14>. Discarding" on the
            # very next click, and the player sees "this interaction
            # failed". Select rebuilds happen only on the explicit
            # _redraw path (button-driven), which DOES push view=self
            # in edit_message so the components stay in sync.
            return {
                "__pages_total__": len(live_pages),
                "__page_idx__":    view.page_idx,
                **dict(live_pages[view.page_idx]),
            }

        def _render(data: dict | None) -> discord.Embed:
            if not data:
                return card("Buddy gone", color=C_AMBER).description(
                    "You don't have any buddies right now.",
                ).build()
            return _build_panel_embed(
                data, state_id,
                page_idx=int(data.get("__page_idx__", 0)),
                page_total=int(data.get("__pages_total__", 1)),
                owner_progression=view.owner_progression,
            )

        self.bot.live.register(LiveState(
            id=state_id,
            message_id=msg.id,
            channel_id=msg.channel.id,
            interval=PANEL_TICK_INTERVAL_S,
            expires_at=time.time() + PANEL_LIFETIME_S,
            get_data=_get_data,
            render=_render,
        ))


    # -- Surrender -----------------------------------------------------------

    @buddy.command(name="surrender")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_surrender(self, ctx: DiscoContext) -> None:
        """Surrender your ACTIVE buddy to the shelter. Irreversible.

        Multi-pet aware: only surrenders the currently-active buddy, so the
        user can keep their other collection members. To surrender a
        resting buddy, first promote it via the panel and then run this.
        """
        row = await _fetch_active(ctx.db, ctx.guild_id, ctx.author.id)
        if not row:
            await ctx.reply_error(
                "You don't have an active buddy to surrender. "
                "If you have resting buddies, promote one first from the panel.",
            )
            return

        buddy_id = int(row["id"])
        name = str(row.get("name") or "your buddy")
        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")

        confirmed = await ctx.confirm(
            f"Surrender your active buddy {emoji} **{name}**?\n\n"
            f"They will go to the shelter and anyone else can adopt them. "
            f"Your other resting buddies (if any) stay put.",
        )
        if not confirmed:
            return

        shelter_rows = await to_shelter(
            ctx.db, ctx.guild_id, ctx.author.id, "surrendered",
            buddy_id=buddy_id,
            display_name=getattr(ctx.author, "display_name", None) or ctx.author.name,
        )
        if not shelter_rows:
            await ctx.reply_error("Surrender failed. Please try again.")
            return

        await ctx.reply_success(
            f"{emoji} **{name}** is now at the shelter.\n\n"
            f"-# Hatch a new buddy any time with `,buddy hatch`, or "
            f"adopt from the shelter with `,buddy shelter`.",
            title="Surrendered",
        )

    # -- Shelter browse ------------------------------------------------------

    @buddy.command(name="shelter")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_shelter(self, ctx: DiscoContext) -> None:
        """Browse adoptable buddies at this server's shelter."""
        rows = await list_shelter(ctx.db, ctx.guild_id, limit=50, offset=0)
        total = await count_shelter(ctx.db, ctx.guild_id)

        if not rows:
            await ctx.reply_error("The shelter is empty. No buddies up for adoption.")
            return

        from configs.buddies_config import gender_glyph as _gender_glyph
        prefix = await ctx.get_guild_prefix()
        per_page = 8
        pages: list[discord.Embed] = []
        for start in range(0, len(rows), per_page):
            chunk = rows[start:start + per_page]
            lines: list[str] = []
            for r in chunk:
                rid = int(r["id"])
                species = str(r.get("species") or "")
                emoji = str(SPECIES.get(species, {}).get("emoji") or "")
                lvl = int(r.get("level") or 1)
                name = str(r.get("name") or "Unnamed")
                tier = int(r.get("rarity_tier") or 1)
                tier_name = str(rarity_meta(tier).get("name") or "Common")
                glyph = _gender_glyph(r.get("gender"))
                gender_tag = f" {glyph}" if glyph else ""
                reason = str(r.get("abandoned_reason") or " - ").replace("_", " ")
                lines.append(
                    f"`#{rid}`  {emoji} **{name}**{gender_tag}  -  "
                    f"**{tier_name}** Lv. {lvl}  -  *{reason}*"
                )
            embed = (
                card(f"Shelter  -  {ctx.guild.name}", color=C_NAVY)
                .description("\n".join(lines))
                .footer(
                    f"{total} buddy/buddies adoptable  -  "
                    f"Use {prefix}buddy adopt <id> to adopt"
                )
                .build()
            )
            pages.append(embed)
        await ctx.paginate(pages)

    # -- Adopt ---------------------------------------------------------------

    @buddy.command(name="adopt")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_adopt(self, ctx: DiscoContext, buddy_id: str) -> None:
        """Adopt a shelter buddy by its id (shown in ,buddy shelter)."""
        # Shelter listings display the id as `#5`, so strip a leading `#`
        # (and any surrounding whitespace) before parsing. Accepting a str
        # here also gives us a friendlier error than the default
        # "Converting to int failed for parameter buddy_id".
        raw = (buddy_id or "").strip().lstrip("#").strip()
        try:
            buddy_id_int = int(raw)
        except (TypeError, ValueError):
            prefix = await ctx.get_guild_prefix()
            await ctx.reply_error(
                f"Pass a numeric buddy id from `{prefix}buddy shelter`, "
                f"like `{prefix}buddy adopt 5`.",
            )
            return
        if buddy_id_int <= 0:
            await ctx.reply_error("Pass a positive buddy id from `,buddy shelter`.")
            return

        ok, err, row = await try_adopt(ctx.db, ctx.guild_id, ctx.author.id, buddy_id_int)
        if not ok:
            await ctx.reply_error(err)
            return

        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "Your new buddy")
        lvl = int(row.get("level") or 1)

        owner_label = owner_label_for(
            getattr(ctx.author, "display_name", None) or ctx.author.name,
            ctx.author.id,
        )
        greeting = await generate_reply(dict(row), owner_label, "adopt")
        await record_event(
            ctx.db, int(row["id"]), "adopt",
            f"adopted by {owner_label}; said: {greeting}",
        )

        await ctx.reply_success(
            f"{emoji} **{name}** (Lv. {lvl}) is yours now.\n"
            f"> {greeting}\n"
            f"Use `{await ctx.get_guild_prefix()}buddy` to open the panel.",
            title="Adopted!",
        )
        await ctx.bot.bus.publish(
            "buddy_adopted",
            guild=ctx.guild, user=ctx.author,
            buddy_id=int(row["id"]), species=species, name=name,
        )

    # -- Reclaim -------------------------------------------------------------

    @buddy.command(name="reclaim")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_reclaim(self, ctx: DiscoContext) -> None:
        """Reclaim a buddy you lost to a leave or ban (within 24h)."""
        ok, err, row = await try_reclaim(ctx.db, ctx.guild_id, ctx.author.id)
        if not ok:
            await ctx.reply_error(err)
            return

        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "Your buddy")
        lvl = int(row.get("level") or 1)

        owner_label = owner_label_for(
            getattr(ctx.author, "display_name", None) or ctx.author.name,
            ctx.author.id,
        )
        greeting = await generate_reply(dict(row), owner_label, "reclaim")
        await record_event(
            ctx.db, int(row["id"]), "reclaim",
            f"reclaimed by original owner {owner_label}; said: {greeting}",
        )

        await ctx.reply_success(
            f"{emoji} **{name}** (Lv. {lvl}) came back to you.\n"
            f"> {greeting}",
            title="Reclaimed",
        )

    # -- Gear (equipment slots) ----------------------------------------------

    @buddy.group(
        name="gear",
        aliases=["equip", "equipment"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_gear(self, ctx: DiscoContext) -> None:
        """Show your active buddy's equipped gear."""
        uid = ctx.author.id
        gid = ctx.guild_id
        row = await ctx.db.fetch_one(
            "SELECT id, name, species, gear FROM cc_buddies"
            " WHERE owner_user_id=$1 AND guild_id=$2 AND is_active=TRUE",
            uid, gid,
        )
        if not row:
            await ctx.reply_error("No active buddy. Use `,buddy` to open the panel.")
            return

        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "\U0001F43E")
        name = str(row.get("name") or "Your Buddy")
        gear: dict = _json_dict(row.get("gear"))
        from configs.buddy_gear_config import BUDDY_GEAR

        acc_key = gear.get("accessory")
        charm_key = gear.get("charm")

        lines = []
        for slot in ("accessory", "charm"):
            k = gear.get(slot)
            if k:
                meta = BUDDY_GEAR.get(str(k) or "")
                if meta:
                    lines.append(
                        f"{meta['emoji']} **{meta['name']}** ({slot}) -- {meta['blurb']}"
                    )
            else:
                lines.append(f"_{slot.capitalize()}: empty_")

        prefix = await ctx.get_guild_prefix()
        embed = (
            card(f"{emoji} {name} -- Gear", color=C_NAVY)
            .description("\n".join(lines) or "_(no gear equipped)_")
            .footer(
                f"Use {prefix}buddy gear equip <item> to equip  |  "
                f"{prefix}buddy gear unequip <slot> to remove"
            )
            .build()
        )
        await ctx.reply(embed=embed)

    @buddy_gear.command(name="equip")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_gear_equip(self, ctx: DiscoContext, *, item_key: str) -> None:
        """Equip a gear item (accessory or charm) onto your active buddy."""
        from configs.buddy_gear_config import BUDDY_GEAR
        key = (item_key or "").strip().lower().replace(" ", "_")
        meta = BUDDY_GEAR.get(key)
        if not meta:
            names = ", ".join(f"`{k}`" for k in BUDDY_GEAR)
            await ctx.reply_error(
                f"Unknown gear item `{key}`. Valid items: {names}",
            )
            return

        uid = ctx.author.id
        gid = ctx.guild_id
        buddy_row = await ctx.db.fetch_one(
            "SELECT id, name, species, gear FROM cc_buddies"
            " WHERE owner_user_id=$1 AND guild_id=$2 AND is_active=TRUE",
            uid, gid,
        )
        if not buddy_row:
            await ctx.reply_error("No active buddy. Use `,buddy` to open the panel.")
            return

        slot: str = meta["slot"]
        buddy_id = int(buddy_row["id"])
        current_gear: dict = _json_dict(buddy_row.get("gear"))
        currently_equipped = current_gear.get(slot)

        if currently_equipped == key:
            await ctx.reply_error(
                f"{meta['emoji']} **{meta['name']}** is already equipped in the {slot} slot.\n"
                f"Use `,buddy gear unequip {slot}` to remove it."
            )
            return

        await ctx.db.execute(
            "UPDATE cc_buddies"
            " SET gear = jsonb_set(COALESCE(gear, '{}'), ARRAY[$1], to_jsonb($2::text))"
            " WHERE id=$3",
            slot, key, buddy_id,
        )

        species = str(buddy_row.get("species") or "")
        bud_emoji = str(SPECIES.get(species, {}).get("emoji") or "\U0001F43E")
        bud_name = str(buddy_row.get("name") or "Your Buddy")

        msg_parts = [f"{meta['emoji']} **{meta['name']}** equipped in the **{slot}** slot."]
        if currently_equipped:
            old_meta = BUDDY_GEAR.get(str(currently_equipped) or "")
            if old_meta:
                msg_parts.append(
                    f"Replaced {old_meta['emoji']} **{old_meta['name']}**."
                )
        if meta["stat_bonus"]:
            bonuses = ", ".join(
                f"+{int(v*100)}% {k.replace('_', ' ')}"
                for k, v in meta["stat_bonus"].items()
            )
            msg_parts.append(f"Bonus: {bonuses}")

        await ctx.reply_success(
            "\n".join(msg_parts),
            title=f"{bud_emoji} {bud_name} -- Gear Updated",
        )

    @buddy_gear.command(name="shop", aliases=["store", "buy_list"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_gear_shop(self, ctx: DiscoContext) -> None:
        """Browse starter buddy gear sold for DSD.

        Three tiers (Apprentice / Initiate / Adept) of basic gear that
        any player can buy outright -- weaker than the crafted items
        but always in stock. Each row shows the item id (left column,
        the literal token to pass to ``,buddy gear buy``), name, slot,
        bonus, and DSD price.
        """
        from configs.buddy_gear_config import (
            STARTER_TIER_LABELS,
            starter_gear_by_tier,
        )
        tiers = starter_gear_by_tier()
        prefix = await ctx.get_guild_prefix()

        builder = (
            card("\U0001F6CD Buddy Gear -- Starter Shop", color=C_NAVY)
            .description(
                "Basic kit, paid in DSD (wallet+bank). Buying equips "
                "the item on your active buddy and replaces anything "
                "currently in that slot. Crafted gear is stronger -- "
                "see `,craft list` for the full ladder."
            )
        )
        for tier in (1, 2, 3):
            entries = tiers.get(tier, [])
            if not entries:
                continue
            lines: list[str] = []
            for key, meta in entries:
                cost = float(meta.get("shop_cost_dsd") or 0.0)
                slot = str(meta.get("slot") or "")
                emoji = str(meta.get("emoji") or "")
                name = str(meta.get("name") or key)
                blurb = str(meta.get("blurb") or "")
                lines.append(
                    f"`{key}`  {emoji} **{name}**  ({slot})  -  "
                    f"**{fmt_usd(cost)}**\n-# {blurb}"
                )
            builder = builder.field(
                STARTER_TIER_LABELS.get(tier, f"Tier {tier}"),
                "\n".join(lines),
                False,
            )
        builder = builder.footer(
            f"Buy: {prefix}buddy gear buy <item>  ·  "
            f"Equip from elsewhere: {prefix}buddy gear equip <item>"
        )
        await ctx.reply(embed=builder.build(), mention_author=False)

    @buddy_gear.command(name="buy", aliases=["purchase"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_gear_buy(self, ctx: DiscoContext, *, item_key: str) -> None:
        """Buy a starter gear item with DSD and equip it on your active buddy.

        The buddy gear shop is buy-and-equip: the item lands directly in
        the matching slot on your active buddy, replacing whatever is
        there. There's no inventory -- if you replace gear, the old
        item is gone (no refund). Confirms before deducting.
        """
        from configs.buddy_gear_config import BUDDY_GEAR
        key = (item_key or "").strip().lower().replace(" ", "_")
        meta = BUDDY_GEAR.get(key)
        if not meta or not meta.get("starter_tier"):
            await ctx.reply_error_hint(
                f"Unknown starter gear `{key}`.",
                hint="See `,buddy gear shop` for the full list.",
                command_name="buddy gear shop",
            )
            return
        cost = float(meta.get("shop_cost_dsd") or 0.0)
        if cost <= 0:
            await ctx.reply_error(
                f"`{key}` isn't sold in the starter shop right now.",
            )
            return

        uid = ctx.author.id
        gid = ctx.guild_id
        buddy_row = await ctx.db.fetch_one(
            "SELECT id, name, species, gear FROM cc_buddies"
            " WHERE owner_user_id=$1 AND guild_id=$2 AND is_active=TRUE",
            uid, gid,
        )
        if not buddy_row:
            await ctx.reply_error("No active buddy. Use `,buddy` to open the panel.")
            return

        slot = str(meta["slot"])
        bid = int(buddy_row["id"])
        bud_name = str(buddy_row.get("name") or "Your Buddy")
        species = str(buddy_row.get("species") or "")
        bud_emoji = str(SPECIES.get(species, {}).get("emoji") or "\U0001F43E")
        current_gear: dict = _json_dict(buddy_row.get("gear"))
        currently_equipped = current_gear.get(slot)

        # Refuse to charge if the buddy already has THIS item -- nothing
        # to gain, no surprise wallet hit.
        if currently_equipped == key:
            await ctx.reply_error(
                f"{meta['emoji']} **{meta['name']}** is already equipped "
                f"on **{bud_name}**.",
            )
            return

        # Confirm replacement when there's already gear in that slot,
        # since buying overwrites it with no refund.
        replace_warning = ""
        if currently_equipped:
            old_meta = BUDDY_GEAR.get(str(currently_equipped) or "")
            old_label = (
                f"{old_meta['emoji']} **{old_meta['name']}**"
                if old_meta else f"`{currently_equipped}`"
            )
            replace_warning = (
                f"\n\n⚠️ This will **replace** the {old_label} "
                f"currently in the {slot} slot. The old item is **lost** "
                f"(no refund)."
            )

        confirmed = await ctx.confirm(
            f"Buy {meta['emoji']} **{meta['name']}** "
            f"({slot}, Tier {int(meta.get('starter_tier') or 0)}) "
            f"for **{fmt_usd(cost)}** and equip it on "
            f"{bud_emoji} **{bud_name}**?{replace_warning}",
        )
        if not confirmed:
            return

        # Pull cost from wallet+bank. ValueError on insufficient funds.
        try:
            await ctx.db.deduct_liquid(uid, gid, to_raw(cost))
        except ValueError:
            await ctx.reply_error(
                f"Need **{fmt_usd(cost)}** (wallet + bank combined). "
                f"You don't have enough.",
            )
            return

        await ctx.db.execute(
            "UPDATE cc_buddies"
            " SET gear = jsonb_set(COALESCE(gear, '{}'), ARRAY[$1], to_jsonb($2::text))"
            " WHERE id=$3",
            slot, key, bid,
        )

        msg_parts = [
            f"{meta['emoji']} **{meta['name']}** purchased and equipped "
            f"in the **{slot}** slot for **{fmt_usd(cost)}**.",
        ]
        if meta["stat_bonus"]:
            bonuses = ", ".join(
                f"+{int(v*100)}% {k.replace('_', ' ')}"
                if isinstance(v, float) and 0 < v < 1
                else f"+{v} {k.replace('_', ' ')}"
                for k, v in meta["stat_bonus"].items()
            )
            msg_parts.append(f"Bonus: {bonuses}")
        await ctx.reply_success(
            "\n".join(msg_parts),
            title=f"{bud_emoji} {bud_name} -- Gear Updated",
        )

    @buddy_gear.command(name="unequip", aliases=["remove"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_gear_unequip(self, ctx: DiscoContext, *, slot: str) -> None:
        """Remove gear from the accessory or charm slot."""
        slot = (slot or "").strip().lower()
        if slot not in ("accessory", "charm"):
            await ctx.reply_error(
                "Slot must be `accessory` or `charm`."
            )
            return

        uid = ctx.author.id
        gid = ctx.guild_id
        buddy_row = await ctx.db.fetch_one(
            "SELECT id, name, species, gear FROM cc_buddies"
            " WHERE owner_user_id=$1 AND guild_id=$2 AND is_active=TRUE",
            uid, gid,
        )
        if not buddy_row:
            await ctx.reply_error("No active buddy. Use `,buddy` to open the panel.")
            return

        current_gear: dict = _json_dict(buddy_row.get("gear"))
        if not current_gear.get(slot):
            await ctx.reply_error(f"Nothing equipped in the **{slot}** slot.")
            return

        buddy_id = int(buddy_row["id"])
        await ctx.db.execute(
            "UPDATE cc_buddies"
            " SET gear = gear - $1"
            " WHERE id=$2",
            slot, buddy_id,
        )

        species = str(buddy_row.get("species") or "")
        bud_emoji = str(SPECIES.get(species, {}).get("emoji") or "\U0001F43E")
        bud_name = str(buddy_row.get("name") or "Your Buddy")
        await ctx.reply_success(
            f"**{slot.capitalize()}** slot cleared.",
            title=f"{bud_emoji} {bud_name} -- Gear Updated",
        )

    # -- Storage (the "buddy computer") --------------------------------------

    @buddy.group(
        name="storage",
        aliases=["box", "computer", "pc", "stored"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_storage(self, ctx: DiscoContext) -> None:
        """Browse buddies you have stashed in storage. Pokemon-PC style.

        ``,buddy storage``       -- button-driven panel (default)
        ``,buddy storage eggs``  -- list banked eggs (held + buddy storage)

        Stored buddies don't count against your battle slot cap, don't
        decay, and aren't usable in arena / delve until you withdraw
        them. The view ships Withdraw / Deposit / Eggs buttons so the
        player never has to remember the underlying ``,buddy store`` /
        ``,buddy retrieve`` commands. Storage capacity is upgraded in
        `,buddy shop` (storage slot upgrade).
        """
        view = _BuddyStorageView(self, ctx)
        embed = await view._build_embed()
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    @buddy_storage.command(
        name="eggs",
        aliases=["egg", "incubator", "bank"],
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_storage_eggs(self, ctx: DiscoContext) -> None:
        """Browse banked eggs (held with you + buddy egg storage).

        Two distinct egg containers consolidated under one panel:

          * **Held** -- on-person eggs from fishing / wild battle.
                       Capped at 10, fixed (not upgradable). The first
                       cap an egg overflows.
          * **Banked** -- buddy egg storage on the buddy network.
                          Capped at 50 base, +50 per ``,buddy slot
                          eggs buy`` upgrade, max 1000.

        New eggs land in held first; once held is full, overflow auto-
        deposits into banked. When BOTH are full, an egg roll falls
        back to a mystery-box LURE payout (existing fishing behaviour).
        """
        from services import buddy_economy as bes
        from services import buddy_storage_eggs as bse
        from services import fishing as _fish
        from configs.buddies_config import EGG_HELD_HARD_CAP, rarity_meta as _rarity_meta

        held_summary = await _fish.list_held_eggs(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        banked = await bse.list_storage(ctx.db, ctx.guild_id, ctx.author.id)
        banked_cap = await bes.user_max_egg_storage(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        held_count = int(held_summary.get("total") or 0)

        def _bucket_lines(eggs: list[dict]) -> list[str]:
            """Compact ``species/tier x N`` rows for the embed."""
            buckets: dict[tuple[str, int], int] = {}
            for e in eggs:
                key = (
                    str(e.get("species") or "").lower(),
                    int(e.get("rarity_tier") or 1),
                )
                buckets[key] = buckets.get(key, 0) + 1
            out: list[str] = []
            for (species, tier), count in sorted(buckets.items()):
                emoji = str(SPECIES.get(species, {}).get("emoji") or "\U0001F95A")
                tier_label = str(_rarity_meta(tier).get("name") or "Common")
                pretty = species.title() if species else "Unknown"
                out.append(
                    f"{emoji} **{pretty}** ({tier_label})  -  x**{count}**"
                )
            return out

        # Held buckets come from list_held_eggs (keyed as
        # by_species_tier in fishing service). Iterate sorted so the
        # ordering is stable across renders.
        held_by_st: dict = held_summary.get("by_species_tier") or {}
        held_lines: list[str] = []
        for (species_key, tier), count in sorted(
            held_by_st.items(), key=lambda kv: (kv[0][0], kv[0][1]),
        ):
            emoji = str(SPECIES.get(species_key, {}).get("emoji") or "\U0001F95A")
            tier_label = str(_rarity_meta(int(tier)).get("name") or "Common")
            pretty = str(species_key).title()
            held_lines.append(
                f"{emoji} **{pretty}** ({tier_label})  -  x**{count}**"
            )
        held_value = "\n".join(held_lines) or "_(empty)_"
        banked_lines = _bucket_lines(banked)
        banked_value = "\n".join(banked_lines) or "_(empty)_"

        prefix = await ctx.get_guild_prefix()
        embed = (
            card(
                f"\U0001F95A Egg Storage  -  {ctx.author.display_name}",
                color=C_NAVY,
            )
            .description(
                f"Held: **{held_count}**/{EGG_HELD_HARD_CAP}  -  "
                f"Banked: **{len(banked)}**/{banked_cap}\n"
                f"Held overflows into banked automatically. Both full = "
                f"the egg falls back to a LURE payout."
            )
            .field(
                f"Held (with you)  -  {held_count}/{EGG_HELD_HARD_CAP}",
                held_value[:1024],
                False,
            )
            .field(
                f"Banked (buddy storage)  -  {len(banked)}/{banked_cap}",
                banked_value[:1024],
                False,
            )
            .footer(
                f"{prefix}buddy egg deposit <n>  -  move held to banked   "
                f"{prefix}buddy egg withdraw <n>  -  move banked to held   "
                f"{prefix}buddy slot eggs buy  -  +50 banked rows"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @buddy.command(name="store", aliases=["box-in", "deposit"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_store(self, ctx: DiscoContext, buddy_id: str) -> None:
        """Stash one of your owned buddies in storage.

        Storage doesn't count against your owned cap, doesn't decay, and
        can't fight. Useful for keeping spare collectible buddies without
        having to surrender them. Requires you have at least one other
        owned buddy left after the move.
        """
        raw = (buddy_id or "").strip().lstrip("#").strip()
        try:
            bid = int(raw)
        except (TypeError, ValueError):
            prefix = await ctx.get_guild_prefix()
            await ctx.reply_error(
                f"Pass a numeric buddy id, like `{prefix}buddy store 12`. "
                f"Get ids from `{prefix}buddy species` or `{prefix}buddy stats`.",
            )
            return
        if bid <= 0:
            await ctx.reply_error("Pass a positive buddy id.")
            return

        ok, err, row = await to_storage(
            ctx.db, ctx.guild_id, ctx.author.id, bid,
        )
        if not ok:
            await ctx.reply_error(err)
            return
        species = str(row.get("species") or "") if row else ""
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "Your buddy") if row else "Your buddy"
        lvl = int(row.get("level") or 1) if row else 1
        # Bus event so the achievements + quests services can react. Carries
        # the running stored count so the "Buddy Collector" milestone has
        # the value it needs to threshold-check without re-querying.
        try:
            stored_count = await count_storage(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            await ctx.bot.bus.publish(
                "buddy_stored",
                guild=ctx.guild, user=ctx.author,
                buddy_id=bid, stored_count=int(stored_count),
            )
        except Exception:
            log.debug("buddy_stored event publish failed", exc_info=True)
        await ctx.reply_success(
            f"{emoji} **{name}** (Lv. {lvl}) is now in storage. "
            f"Use `,buddy retrieve {bid}` when you want them back.",
            title="Buddy Stored",
        )

    @buddy.command(
        name="retrieve",
        aliases=["box-out", "unstore", "take", "fetch"],
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_retrieve(self, ctx: DiscoContext, buddy_id: str) -> None:
        """Withdraw a stored buddy back into your owned pool.

        Refused if you're already at the owned cap. The withdrawn buddy
        lands inactive (use `,buddy panel` to promote it) unless you have
        no other owned buddy, in which case it auto-promotes.
        """
        raw = (buddy_id or "").strip().lstrip("#").strip()
        try:
            bid = int(raw)
        except (TypeError, ValueError):
            prefix = await ctx.get_guild_prefix()
            await ctx.reply_error(
                f"Pass a numeric buddy id from `{prefix}buddy storage`, "
                f"like `{prefix}buddy retrieve 12`.",
            )
            return
        if bid <= 0:
            await ctx.reply_error("Pass a positive buddy id.")
            return

        ok, err, row = await from_storage(
            ctx.db, ctx.guild_id, ctx.author.id, bid,
        )
        if not ok:
            await ctx.reply_error(err)
            return
        species = str(row.get("species") or "") if row else ""
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "Your buddy") if row else "Your buddy"
        lvl = int(row.get("level") or 1) if row else 1
        await ctx.reply_success(
            f"{emoji} **{name}** (Lv. {lvl}) is back with you.",
            title="Buddy Retrieved",
        )

    @buddy.command(name="find", aliases=["search", "locate", "where"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_find(
        self, ctx: DiscoContext, *, query: str | None = None,
    ) -> None:
        """Locate one of your buddies by id, name, or species.

        Searches every status (owned, stored, shelter) so a wild capture
        you can't see in `,buddy stats` because it's inactive, or a buddy
        that ran away to the shelter, still surfaces here. Pass a buddy
        id (`,buddy find 47`), a name (`,buddy find scampi`), or a species
        (`,buddy find octopus`). With no query, lists every buddy you have.
        """
        gid = ctx.guild_id
        uid = ctx.author.id
        q = (query or "").strip().lstrip("#").strip()
        prefix = await ctx.get_guild_prefix()

        rows: list[dict] = []
        if not q:
            rows = await ctx.db.fetch_all(
                "SELECT id, species, name, status, level, rarity_tier, "
                "       gender, is_active, hatched_at "
                "FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 "
                "  AND status IN ('owned', 'stored', 'shelter') "
                "ORDER BY status ASC, is_active DESC, id DESC "
                "LIMIT 50",
                gid, uid,
            )
        else:
            try:
                bid = int(q)
            except (TypeError, ValueError):
                bid = 0
            if bid > 0:
                rows = await ctx.db.fetch_all(
                    "SELECT id, species, name, status, level, rarity_tier, "
                    "       gender, is_active, hatched_at "
                    "FROM cc_buddies "
                    "WHERE guild_id = $1 AND owner_user_id = $2 AND id = $3",
                    gid, uid, bid,
                )
            else:
                like = f"%{q.lower()}%"
                rows = await ctx.db.fetch_all(
                    "SELECT id, species, name, status, level, rarity_tier, "
                    "       gender, is_active, hatched_at "
                    "FROM cc_buddies "
                    "WHERE guild_id = $1 AND owner_user_id = $2 "
                    "  AND status IN ('owned', 'stored', 'shelter') "
                    "  AND (LOWER(name) LIKE $3 OR LOWER(species) LIKE $3) "
                    "ORDER BY status ASC, is_active DESC, id DESC "
                    "LIMIT 50",
                    gid, uid, like,
                )

        if not rows:
            if q:
                await ctx.reply_error(
                    f"No buddies match `{q}`. Try `{prefix}buddy find` (no "
                    f"args) to list every buddy you own."
                )
            else:
                await ctx.reply_error(
                    f"You don't have any buddies. `{prefix}buddy hatch` to "
                    f"start, or `{prefix}fish` and beat a wild buddy to "
                    f"capture one."
                )
            return

        from configs.buddies_config import (
            gender_glyph as _gender_glyph,
            rarity_meta as _rarity_meta,
        )
        _STATUS_HINTS = {
            "owned":   ("Owned (active)", f"`{prefix}buddy stats`"),
            "owned_i": ("Owned (inactive)",
                       f"`{prefix}buddy stats` -> page to it -> Set Active"),
            "stored":  ("Storage",
                       f"`{prefix}buddy retrieve <id>` to withdraw"),
            "shelter": ("Shelter",
                       f"`{prefix}buddy adopt <id>` to bring home"),
        }
        groups: dict[str, list[str]] = {}
        for r in rows:
            rid = int(r["id"])
            status = str(r.get("status") or "")
            is_active = bool(r.get("is_active"))
            key = "owned" if (status == "owned" and is_active) else (
                "owned_i" if status == "owned" else status
            )
            species = str(r.get("species") or "")
            emoji = str(SPECIES.get(species, {}).get("emoji") or "")
            glyph = _gender_glyph(r.get("gender"))
            glyph_part = f" {glyph}" if glyph else ""
            name = str(r.get("name") or "Unnamed")
            lvl = int(r.get("level") or 1)
            tier_n = int(r.get("rarity_tier") or 1)
            tier_label = str(_rarity_meta(tier_n).get("name") or "Common")
            line = (
                f"`#{rid}`  {emoji} **{name}**{glyph_part}  -  "
                f"Lv. {lvl} {tier_label} {species}"
            )
            groups.setdefault(key, []).append(line)

        embed = card(
            f"Buddy Search  -  {ctx.author.display_name}",
            color=C_NAVY,
        )
        if q:
            embed.description(f"Matches for `{q}`:")
        for key, (label, hint) in _STATUS_HINTS.items():
            lines = groups.get(key)
            if not lines:
                continue
            value = "\n".join(lines[:10])
            if len(lines) > 10:
                value += f"\n-# +{len(lines) - 10} more"
            value += f"\n-# {hint}"
            embed.field(f"{label}  ({len(lines)})", value, False)
        embed.footer(
            f"Total: {sum(len(v) for v in groups.values())}  -  "
            f"{prefix}buddy find <id|name|species>"
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    # -- Nest / Breeding -----------------------------------------------------
    # Internally the table is still ``cc_buddy_daycare`` and the bus event is
    # ``daycare_egg_collected`` (renaming the schema would invalidate every
    # historical event row). Player-facing copy uses "nest" -- the command
    # accepts both spellings via aliases.

    @buddy.group(
        name="nest",
        aliases=["daycare", "breed", "breeding", "incubator"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_daycare(self, ctx: DiscoContext) -> None:
        """Show the status of every nest slot you own.

        Subcommands:
            ``,buddy nest deposit <id1> <id2>`` -- start incubating an egg
            ``,buddy nest collect [slot_id]``   -- pick up a ready egg
            ``,buddy nest cancel  [slot_id]``   -- abandon (no fee refund)

        Slot capacity comes from ``,buddy slot nest`` (base 1, up to 10
        with purchased upgrades). Rarity is hidden until an egg is ready
        to collect, so the species is the only hint while it incubates.
        """
        from configs.buddies_config import (
            DAYCARE_FEE_BUD, DAYCARE_INCUBATION_S,
            DAYCARE_MIN_PARENT_LEVEL, rarity_meta,
        )
        from services import buddy_economy as bes
        nests = await buddy_breeding.list_nests(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        cap = await bes.user_max_nest_slots(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        prefix = await ctx.get_guild_prefix()
        if not nests:
            await ctx.send_embed(
                card("\U0001FAB9 Buddy Nest", color=C_INFO)
                .description(
                    f"No buddies in your nest (0/{cap} slots used). Drop "
                    "two parents in to incubate an egg over time.\n\n"
                    f"`{prefix}buddy nest deposit <id1> <id2>` to start.\n"
                    f"Fee: **{DAYCARE_FEE_BUD:,.0f} BUD** (burned).\n"
                    f"Incubation: **{DAYCARE_INCUBATION_S // 3600} hours**.\n"
                    f"Each parent must be at least Lv. "
                    f"{DAYCARE_MIN_PARENT_LEVEL}.\n"
                    f"-# Parents leave your active inventory while in "
                    f"the nest and return on collect/cancel (active first, "
                    f"else storage). They don't count toward your battle "
                    f"or storage caps while incubating.\n"
                    f"`{prefix}buddy slot nest buy` to widen your cap."
                )
                .build()
            )
            return

        ready_count = sum(1 for n in nests if n.get("ready"))
        embed = card("\U0001FAB9 Buddy Nest", color=(
            C_SUCCESS if ready_count > 0 else C_NAVY
        )).description(
            f"**{len(nests)}/{cap}** slots in use  -  "
            f"**{ready_count}** egg{'s' if ready_count != 1 else ''} ready.\n"
            f"`{prefix}buddy nest collect [slot_id]`  -  "
            f"`{prefix}buddy nest cancel [slot_id]`"
        )
        for nest in nests:
            slot_id = int(nest.get("id") or 0)
            species = str(nest.get("egg_species") or "")
            emoji = str(SPECIES.get(species, {}).get("emoji") or "\U0001F95A")
            secs = int(nest.get("seconds_remaining") or 0)
            hours = secs // 3600
            mins = (secs % 3600) // 60
            if nest.get("ready"):
                rarity = int(nest.get("egg_rarity_tier") or 1)
                rarity_name = str(rarity_meta(rarity).get("name") or "Common")
                # Rarity stays hidden until the egg is actually ready,
                # so an incubating slot reads as a surprise pull.
                timer_line = (
                    f"\U0001F423 **{rarity_name} {species.title()} egg "
                    f"ready** -- `{prefix}buddy nest collect {slot_id}`."
                )
            else:
                timer_line = (
                    f"\U000023F3 Incubating a **{species.title()}** egg  -  "
                    f"about **{hours}h {mins}m** to go.\n"
                    f"-# Rarity will be revealed when it hatches."
                )
            embed = embed.field(
                f"{emoji} Slot `#{slot_id}`",
                f"Parents: `#{int(nest['parent1_id'])}` + "
                f"`#{int(nest['parent2_id'])}`\n"
                f"{timer_line}",
                False,
            )
        await ctx.send_embed(embed.build())

    @buddy_daycare.command(name="deposit", aliases=["start", "drop", "in"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_daycare_deposit(
        self, ctx: DiscoContext, parent1: str, parent2: str,
    ) -> None:
        """Deposit two of your owned buddies as breeding parents.

        Both parents must be at least Lv. ``DAYCARE_MIN_PARENT_LEVEL``,
        not currently listed for sale, and **opposite genders** (one
        male and one female). The buddy panel (``,buddy stats``) and
        ``,buddy storage`` both show the ``♂`` / ``♀`` glyph next to
        each buddy so you can pick a valid pair. The egg's rarity is
        rolled at deposit time but stays hidden until the egg hatches.
        """

        def _parse_id(s: str) -> int:
            raw = (s or "").strip().lstrip("#").strip()
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0

        p1, p2 = _parse_id(parent1), _parse_id(parent2)
        if p1 <= 0 or p2 <= 0:
            await ctx.reply_error(
                "Pass two numeric buddy ids, like "
                "`,buddy nest deposit 12 19`.",
            )
            return
        ok, err, row = await buddy_breeding.deposit(
            ctx.db, ctx.guild_id, ctx.author.id, p1, p2,
        )
        if not ok:
            await ctx.reply_error(err)
            return
        species = str(row.get("egg_species") or "") if row else ""
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        slot_id = int(row.get("id") or 0) if row else 0
        await ctx.reply_success(
            f"{emoji} A **{species.title()}** egg is now incubating in "
            f"slot `#{slot_id}`. Rarity will be revealed when it hatches.\n"
            f"-# Both parents have left your active inventory and won't "
            f"count toward your battle / storage caps while incubating. "
            f"They come home when you collect or cancel.\n"
            f"Check progress with `,buddy nest`.",
            title="Buddies Deposited",
        )

    @buddy_daycare.command(name="collect", aliases=["claim", "pickup", "out"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_daycare_collect(
        self, ctx: DiscoContext, slot_id: str | None = None,
    ) -> None:
        """Collect a ready egg. Without a slot id, picks the next-ready one."""
        from configs.buddies_config import rarity_meta
        from services.fishing import give_held_egg

        target: int | None = None
        if slot_id is not None:
            raw = (slot_id or "").strip().lstrip("#").strip()
            try:
                target = int(raw)
            except (TypeError, ValueError):
                await ctx.reply_error(
                    "Slot id must be numeric -- see `,buddy nest`."
                )
                return
            if target <= 0:
                await ctx.reply_error("Pass a positive slot id.")
                return
        ok, err, egg = await buddy_breeding.collect(
            ctx.db, ctx.guild_id, ctx.author.id, slot_id=target,
        )
        if not ok:
            await ctx.reply_error(err)
            return

        species = str((egg or {}).get("species") or "")
        rarity = int((egg or {}).get("rarity_tier") or 1)
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        rarity_name = str(rarity_meta(rarity).get("name") or "Common")
        placement = (egg or {}).get("parent_placement") or {}

        gave_ok, give_err = await give_held_egg(
            ctx.db, ctx.guild_id, ctx.author.id,
            species=species, rarity_tier=rarity,
            source="daycare",
        )
        if not gave_ok:
            await ctx.reply_error(
                give_err or
                "Could not deposit the egg in your held-egg slot. "
                "Try `,buddy hatch` to make room and retry."
            )
            return
        try:
            await ctx.bot.bus.publish(
                "daycare_egg_collected",
                guild=ctx.guild, user=ctx.author,
                species=species, rarity_tier=rarity,
            )
        except Exception:
            log.debug(
                "daycare_egg_collected event publish failed", exc_info=True,
            )
        active_back = [pid for pid, s in placement.items() if s == "owned"]
        stored_back = [pid for pid, s in placement.items() if s == "stored"]
        bits: list[str] = []
        if active_back:
            bits.append(
                "Active: " + ", ".join(f"`#{pid}`" for pid in active_back),
            )
        if stored_back:
            bits.append(
                "Storage: " + ", ".join(f"`#{pid}`" for pid in stored_back),
            )
        parents_line = (
            f"\n-# Parents returned -- {' | '.join(bits)}." if bits else ""
        )
        await ctx.reply_success(
            f"{emoji} **{rarity_name} {species.title()} Egg** "
            f"added to your held-eggs. Hatch it with `,buddy hatch` "
            f"(gender will be rolled when it hatches).{parents_line}",
            title="Egg Collected",
        )

    @buddy_daycare.command(
        name="cancel",
        aliases=["abandon", "stop", "release"],
    )
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_daycare_cancel(
        self, ctx: DiscoContext, slot_id: str | None = None,
    ) -> None:
        """Abandon a nest slot. Without a slot id, the oldest one is dropped."""
        target: int | None = None
        if slot_id is not None:
            raw = (slot_id or "").strip().lstrip("#").strip()
            try:
                target = int(raw)
            except (TypeError, ValueError):
                await ctx.reply_error(
                    "Slot id must be numeric -- see `,buddy nest`."
                )
                return
            if target <= 0:
                await ctx.reply_error("Pass a positive slot id.")
                return
        ok, err, parents, placement = await buddy_breeding.cancel(
            ctx.db, ctx.guild_id, ctx.author.id, slot_id=target,
        )
        if not ok:
            await ctx.reply_error(err)
            return
        active_back = [pid for pid, s in placement.items() if s == "owned"]
        stored_back = [pid for pid, s in placement.items() if s == "stored"]
        bits: list[str] = []
        if active_back:
            bits.append(
                "Active: " + ", ".join(f"`#{pid}`" for pid in active_back),
            )
        if stored_back:
            bits.append(
                "Storage: " + ", ".join(f"`#{pid}`" for pid in stored_back),
            )
        where_line = (
            f"\n-# {' | '.join(bits)}." if bits else ""
        )
        await ctx.reply_success(
            f"Nest cancelled. Buddies `#{parents[0]}` + `#{parents[1]}` "
            f"are free again. The fee is gone.{where_line}",
            title="Nest Abandoned",
        )


    # -- Leaderboard ---------------------------------------------------------

    @buddy.command(name="leaderboard", aliases=["lb", "top"])
    @guild_only
    @no_bots
    async def buddy_leaderboard(self, ctx: DiscoContext) -> None:
        """Top buddies in this server, sorted by level then XP.

        For the wins-based ranking, see ``,buddy battles``.
        """
        # Pull a wider slice than we render so the post-membership
        # filter (drop bots / left members / user 0) still has 10
        # rows to display after culling.
        rows = await ctx.db.fetch_all(
            "SELECT id, owner_user_id, species, name, level, xp, "
            "       hunger, happiness, wins, losses "
            "FROM cc_buddies "
            "WHERE guild_id = $1 AND status = 'owned' "
            "ORDER BY level DESC, xp DESC "
            "LIMIT 50",
            ctx.guild_id,
        )
        if rows:
            from core.framework.leaderboard import filter_lb_user_ids
            keep = await filter_lb_user_ids(
                ctx, [int(r["owner_user_id"]) for r in rows],
            )
            rows = [r for r in rows if int(r["owner_user_id"]) in keep][:10]
        if not rows:
            await ctx.reply_error(
                "No buddies in this server yet. Be the first to `,buddy hatch`!",
            )
            return

        medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
        lines: list[str] = []
        for idx, r in enumerate(rows):
            rank_glyph = medals[idx] if idx < 3 else f"`#{idx + 1:>2}`"
            species = str(r.get("species") or "")
            emoji = str(SPECIES.get(species, {}).get("emoji") or "")
            name = str(r.get("name") or "Unnamed")
            lvl = int(r.get("level") or 1)
            xp = int(r.get("xp") or 0)
            wins = int(r.get("wins") or 0)
            losses = int(r.get("losses") or 0)
            uid = int(r.get("owner_user_id") or 0)
            member = ctx.guild.get_member(uid) if ctx.guild else None
            owner = member.display_name if member else f"User {uid}"
            mood_hint = ""
            if int(r.get("hunger") or 0) == 0 or int(r.get("happiness") or 0) == 0:
                mood_hint = "  *(mood broken)*"
            record_hint = f"  -  {wins}W-{losses}L" if (wins or losses) else ""
            lines.append(
                f"{rank_glyph}  {emoji} **{name}**  -  Lv. **{lvl}**  -  "
                f"{xp:,} XP{record_hint}  -  *owned by {owner}*{mood_hint}"
            )

        prefix = await ctx.get_guild_prefix()
        embed = (
            card(f"Buddy Leaderboard  -  {ctx.guild.name}", color=C_GOLD)
            .description("\n".join(lines))
            .footer(
                f"Top 10 by level  -  see {prefix}buddy battles for the wins board  -  "
                f"{fmt_ts(time.time())}"
            )
            .build()
        )
        if ctx.guild and ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)
        await ctx.send_embed(embed)


    # -- Battle leaderboard --------------------------------------------------

    @buddy.command(name="battles", aliases=["battleboard", "blb"])
    @guild_only
    @no_bots
    @premium_required("buddy_battle")
    async def buddy_battles(self, ctx: DiscoContext) -> None:
        """Top buddies in this server ranked by battle wins.

        Ties break on win rate, then on battle_count asc (a 10-2 record
        edges a 10-20 record). Buddies that have never fought are hidden.
        """
        rows = await ctx.db.fetch_all(
            "SELECT id, owner_user_id, species, name, level, "
            "       wins, losses, battle_count "
            "FROM cc_buddies "
            "WHERE guild_id = $1 AND status = 'owned' AND battle_count > 0 "
            "ORDER BY wins DESC, "
            "         (wins::float / GREATEST(1, wins + losses)) DESC, "
            "         battle_count ASC, id ASC "
            "LIMIT 50",
            ctx.guild_id,
        )
        if rows:
            from core.framework.leaderboard import filter_lb_user_ids
            keep = await filter_lb_user_ids(
                ctx, [int(r["owner_user_id"]) for r in rows],
            )
            rows = [r for r in rows if int(r["owner_user_id"]) in keep][:10]
        if not rows:
            await ctx.reply_error(
                "No battles have been fought in this server yet. "
                "Challenge someone with `,buddy battle fight @rival`.",
            )
            return

        medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
        lines: list[str] = []
        for idx, r in enumerate(rows):
            rank_glyph = medals[idx] if idx < 3 else f"`#{idx + 1:>2}`"
            species = str(r.get("species") or "")
            emoji = str(SPECIES.get(species, {}).get("emoji") or "")
            name = str(r.get("name") or "Unnamed")
            lvl = int(r.get("level") or 1)
            wins = int(r.get("wins") or 0)
            losses = int(r.get("losses") or 0)
            fought = int(r.get("battle_count") or 0)
            wr = 100.0 * wins / max(1, wins + losses) if (wins + losses) else 0.0
            uid = int(r.get("owner_user_id") or 0)
            member = ctx.guild.get_member(uid) if ctx.guild else None
            owner = member.display_name if member else f"User {uid}"
            lines.append(
                f"{rank_glyph}  {emoji} **{name}**  -  Lv. {lvl}  -  "
                f"**{wins}W - {losses}L** ({wr:.0f}%, {fought} fought)  -  "
                f"*owned by {owner}*"
            )

        embed = (
            card(f"Battle Leaderboard  -  {ctx.guild.name}", color=C_GOLD)
            .description("\n".join(lines))
            .footer(f"Top 10 by wins  -  {fmt_ts(time.time())}")
            .build()
        )
        if ctx.guild and ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)
        await ctx.send_embed(embed)


    # -- Reroll --------------------------------------------------------------

    @buddy.command(name="reroll")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_reroll(self, ctx: DiscoContext) -> None:
        """Reroll your hatch. Free, up to 3 total. Old buddy is discarded
        (NOT sent to shelter)."""
        gid = ctx.guild_id
        uid = ctx.author.id

        row = await _fetch_active(ctx.db, gid, uid)
        if not row:
            await ctx.reply_error_action(
                "You need a buddy to reroll.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return

        hatch_row = await ctx.db.fetch_one(
            "SELECT reroll_count FROM cc_buddy_hatches "
            "WHERE guild_id = $1 AND user_id = $2",
            gid, uid,
        )
        used = int((hatch_row or {}).get("reroll_count") or 0)
        remaining = REROLL_MAX - used
        if remaining <= 0:
            await ctx.reply_error(
                f"You've already used all **{REROLL_MAX}** of your rerolls. "
                f"For a paid species change, use `,buddy swap <species>`.",
            )
            return

        cur_species = str(row.get("species") or "")
        cur_emoji = str(SPECIES.get(cur_species, {}).get("emoji") or "")
        cur_name = str(row.get("name") or "your buddy")

        confirmed = await ctx.confirm(
            f"Reroll {cur_emoji} **{cur_name}** for a freshly-rolled buddy?\n\n"
            f"**{cur_name}** will be **permanently discarded** (not sent to "
            f"shelter) and you'll get a new random species + name with fresh "
            f"stats and 0 XP.\n\n"
            f"You have **{remaining}** reroll(s) left (of {REROLL_MAX} total).",
        )
        if not confirmed:
            return

        # Pick + name the new buddy before any destructive write. Species
        # and rarity are rolled independently -- a reroll is a fresh buddy
        # in every sense.
        new_species = pick_hatch_species()
        new_name = await generate_name(new_species, ctx.db, gid)
        new_tier = roll_rarity()
        h, hp, e = ADOPT_MOOD
        active_id = int(row["id"])

        # Single transaction so we never end up with a consumed reroll and no
        # replacement buddy, or vice-versa. The reroll replaces ONLY the
        # active buddy; resting collection members are untouched.
        async with ctx.db.transaction() as conn:
            del_status = await conn.execute(
                "DELETE FROM cc_buddies "
                "WHERE id = $1 AND owner_user_id = $2 AND status = 'owned'",
                active_id, uid,
            )
            if not str(del_status).startswith("DELETE 1"):
                # Raced with surrender / runaway; abort cleanly.
                raise RuntimeError("reroll: active buddy disappeared mid-transaction")
            from configs.buddies_config import roll_gender as _roll_gender
            await conn.execute(
                "INSERT INTO cc_buddies ("
                "  guild_id, owner_user_id, species, name, status, "
                "  is_active, rarity_tier, hunger, happiness, energy, "
                "  gender"
                ") VALUES ($1, $2, $3, $4, 'owned', TRUE, $5, $6, $7, $8, $9)",
                gid, uid, new_species, new_name, new_tier, h, hp, e,
                _roll_gender(),
            )
            await conn.execute(
                "UPDATE cc_buddy_hatches SET reroll_count = reroll_count + 1 "
                "WHERE guild_id = $1 AND user_id = $2",
                gid, uid,
            )

        meta = SPECIES.get(new_species, {})
        bonus = str(meta.get("bonus_label") or "")
        tier_name = rarity_meta(new_tier).get("name", "Common")
        left = remaining - 1
        await ctx.reply_success(
            f"{meta.get('emoji', '')}  You rerolled into **{new_name}** "
            f"(a {tier_name} {new_species}).\n"
            f"*{meta.get('tagline', '')}*\n"
            f"Bonus: **{bonus}**\n\n"
            f"Rerolls remaining: **{left}/{REROLL_MAX}**.\n"
            f"Use `{await ctx.get_guild_prefix()}buddy` to open the panel.",
            title="Rerolled!",
        )

    # -- Swap ----------------------------------------------------------------

    @buddy.command(name="swap")
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_swap(self, ctx: DiscoContext, species: str | None = None) -> None:
        """Pay to change your buddy's species. Price doubles each swap."""
        gid = ctx.guild_id
        uid = ctx.author.id

        row = await _fetch_active(ctx.db, gid, uid)
        if not row:
            await ctx.reply_error_action(
                "You need a buddy to swap.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return

        swap_count = int(row.get("swap_count") or 0)
        price_usd = SWAP_BASE_PRICE_USD * (2 ** swap_count)

        if species is None:
            prefix = await ctx.get_guild_prefix()
            avail = _format_species_avail()
            await ctx.reply_error_hint(
                f"Pick a species. Next swap costs **${price_usd:,}**.\n"
                f"Available: {avail}",
                hint=f"{prefix}buddy swap fox",
                command_name="buddy swap",
            )
            return

        target = species.strip().lower()
        if target not in SPECIES:
            avail = _format_species_avail()
            await ctx.reply_error(
                f"Unknown species `{species}`. Available: {avail}",
            )
            return

        cur_species = str(row.get("species") or "")
        if target == cur_species:
            await ctx.reply_error(
                f"Your buddy is already a **{cur_species}**. Pick a different "
                f"species to swap into.",
            )
            return

        # Confirm cost before deducting.
        cur_emoji = str(SPECIES.get(cur_species, {}).get("emoji") or "")
        new_emoji = str(SPECIES.get(target, {}).get("emoji") or "")
        new_bonus = str(SPECIES.get(target, {}).get("bonus_label") or "")
        cur_name = str(row.get("name") or "your buddy")
        next_price = SWAP_BASE_PRICE_USD * (2 ** (swap_count + 1))

        cur_tier = int(row.get("rarity_tier") or 1)
        tier_name = rarity_meta(cur_tier).get("name", "Common")
        confirmed = await ctx.confirm(
            f"Swap {cur_emoji} **{cur_name}** ({cur_species}) into a "
            f"{new_emoji} **{target}** for **${price_usd:,}**?\n\n"
            f"Stats, XP, level, stat allocations, and **rarity ({tier_name})** "
            f"are preserved -- only species + name change. "
            f"New bonus: **{new_bonus}**.\n\n"
            f"Your next swap will cost **${next_price:,}**.",
        )
        if not confirmed:
            return

        # Pull cost from wallet first, then bank. Raises ValueError if short.
        try:
            await ctx.db.deduct_liquid(uid, gid, to_raw(price_usd))
        except ValueError:
            await ctx.reply_error(
                f"Swap costs **${price_usd:,}** (wallet + bank combined). "
                f"You don't have enough.",
            )
            return

        new_name = await generate_name(target, ctx.db, gid)
        # Swap only reskins the buddy (species + name + swap_count). Rarity
        # tier and stat allocations (hp_alloc/atk_alloc/spd_alloc) are the
        # buddy's identity and are never touched here -- reroll is the
        # command for replacing the whole buddy.
        await ctx.db.execute(
            "UPDATE cc_buddies SET "
            "  species = $1, name = $2, "
            "  swap_count = swap_count + 1, "
            "  last_interacted_at = NOW(), "
            "  updated_at = NOW() "
            "WHERE id = $3 AND owner_user_id = $4 AND status = 'owned'",
            target, new_name, int(row["id"]), uid,
        )


        await ctx.reply_success(
            f"{cur_emoji} **{cur_name}** is now {new_emoji} **{new_name}** "
            f"(a {tier_name} {target}).\n"
            f"Bonus: **{new_bonus}**\n"
            f"Paid: **${price_usd:,}**. Next swap: **${next_price:,}**.",
            title="Swapped!",
        )

    # -- Direct gift (P2P transfer) ------------------------------------------

    @buddy.command(name="gift", aliases=["give", "send"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_gift(
        self, ctx: DiscoContext, member: discord.Member,
        identifier: str | None = None,
    ) -> None:
        """Gift one of your buddies to another player.

        ``identifier`` resolves the buddy: numeric = exact id, string =
        case-insensitive name (or unique prefix). Omit to gift your
        currently-active buddy. Costs ``BUDDY_GIFT_FEE_USD`` paid by
        the sender out of wallet+bank.

        Refuses to gift a buddy that's currently listed on the market
        (delist first), or to a recipient at the buddy cap.
        """
        if member.bot:
            await ctx.reply_error("Can't gift to a bot.")
            return
        if member.id == ctx.author.id:
            await ctx.reply_error("Pick another player to gift to.")
            return

        from services import buddy_market as bm
        owned = await bm.get_owned_buddies(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if not owned:
            await ctx.reply_error_action(
                "You don't have any buddies to gift.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return

        target = bm.find_buddy_by_id_or_name(owned, identifier)
        if target is None:
            # Either no match or ambiguous name -- show the roster.
            roster = "\n".join(
                f"  -  `{r['id']}` {SPECIES.get(r['species'], {}).get('emoji', '')} "
                f"**{r['name']}** (Lv.{r['level']} "
                f"{rarity_meta(int(r.get('rarity_tier') or 1)).get('name', 'Common')})"
                f"{'  *for sale*' if r.get('for_sale') else ''}"
                for r in owned
            )
            await ctx.reply_error_hint(
                f"Couldn't find buddy `{identifier or '(active)'}`.",
                hint=("Try one of:\n" + roster +
                      "\n\nUse the buddy id (e.g. `,buddy gift @user 42`) "
                      "or its name."),
                command_name="buddy gift",
            )
            return

        species = str(target.get("species") or "")
        spec_meta = SPECIES.get(species, {})
        emoji = str(spec_meta.get("emoji") or "")
        tier_name = rarity_meta(int(target.get("rarity_tier") or 1)).get(
            "name", "Common",
        )
        name = str(target.get("name") or "your buddy")

        confirmed = await ctx.confirm(
            f"Gift {emoji} **{name}** ({tier_name} {species}, Lv.{target['level']}) "
            f"to {member.mention} for **${BUDDY_GIFT_FEE_USD:,}**?\n\n"
            f"-# The buddy keeps its level, stats, allocations, and "
            f"rarity. The recipient will need to promote it to active "
            f"via `,buddy active <id>`.",
        )
        if not confirmed:
            return

        try:
            res = await bm.gift_buddy(
                ctx.db, ctx.guild_id, ctx.author.id, member.id,
                int(target["id"]),
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        await ctx.reply_success(
            f"{emoji} **{res.buddy_name}** has been gifted to "
            f"{member.mention}.\n"
            f"Paid fee: **${to_human(res.fee_paid_raw):,.0f}**.\n"
            f"-# Transfer #{res.transfer_id} logged for both of you.",
            title="Gifted!",
        )

    # -- Marketplace --------------------------------------------------------

    @buddy.command(name="list", aliases=["sell"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_list(
        self, ctx: DiscoContext, identifier: str | None = None,
        price: str | None = None,
    ) -> None:
        """Deprecated: buddy listings now live on the auction house.

        Use ``,ah list buddy <id_or_name> <price>`` instead. The auction
        house supports buddies, eggs, fish, crops, ore, weapons, armor,
        consumables, and crafted items in one place with cross-currency
        purchases and a stable token id per listing.
        """
        prefix = await ctx.get_guild_prefix()
        ref = (identifier or "<id_or_name>").strip() or "<id_or_name>"
        price_part = (price or "<price>").strip() or "<price>"
        await ctx.reply(
            embed=card(
                "\U0001F3DB Moved to the Auction House",
                color=C_INFO,
                description=(
                    "`,buddy list` has been consolidated into the new "
                    "auction house, which handles every item kind in "
                    "one place.\n\n"
                    f"**List a buddy:** `{prefix}ah list buddy {ref} {price_part}`\n"
                    f"**Browse:** `{prefix}ah` (categorised, with dropdown filter)\n"
                    f"**Cancel:** `{prefix}ah cancel <listing_id>`\n"
                    f"**Your listings:** `{prefix}ah mine`\n"
                    f"**Help:** `{prefix}ah help`"
                ),
            ).build(),
            mention_author=False,
        )

    @buddy.command(name="delist", aliases=["unlist", "cancel"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_delist(
        self, ctx: DiscoContext, listing_id: int,
    ) -> None:
        """Cancel a legacy buddy/egg listing.

        Kept around to drain the old ``cc_buddy_listings`` table -- for
        anything listed via the new auction house, use ``,ah cancel <id>``.
        """
        from services import buddy_market as bm
        listing = await bm.get_listing_by_id(
            ctx.db, ctx.guild_id, int(listing_id),
        )
        if listing is None:
            prefix = await ctx.get_guild_prefix()
            await ctx.reply_error(
                f"No legacy listing with id `{listing_id}`. "
                f"Auction-house listings: `{prefix}ah cancel {listing_id}`."
            )
            return
        try:
            if listing.get("kind") == "egg":
                res = await bm.cancel_egg_listing(
                    ctx.db, ctx.guild_id, ctx.author.id, int(listing_id),
                )
                tail = "Egg returned to your held inventory."
            else:
                res = await bm.delist_buddy(
                    ctx.db, ctx.guild_id, ctx.author.id, int(listing_id),
                )
                tail = "Buddy is back in your shelter."
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await ctx.reply_success(
            f"Listing #{res.listing_id} ({res.label}) cancelled. {tail}",
            title="Delisted",
        )

    @buddy.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_buy(
        self, ctx: DiscoContext, listing_id: int,
    ) -> None:
        """Buy a legacy buddy/egg listing.

        Kept around to drain the old ``cc_buddy_listings`` table -- new
        buddy/egg listings live on ``,ah``. Use ``,ah buy <id>`` for
        anything you find via ``,ah browse``.
        """
        from services import buddy_market as bm
        listing = await bm.get_listing_by_id(
            ctx.db, ctx.guild_id, int(listing_id),
        )
        if listing is None:
            prefix = await ctx.get_guild_prefix()
            await ctx.reply_error(
                f"No legacy listing with id `{listing_id}`. "
                f"Auction-house listings: `{prefix}ah buy {listing_id}`."
            )
            return
        seller_id = int(listing["seller_user_id"])
        if seller_id == ctx.author.id:
            await ctx.reply_error("That's your own listing.")
            return
        price_human = to_human(int(listing["asking_price_raw"]))
        kind = str(listing.get("kind") or "")
        head = listing["label"]
        if kind == "buddy":
            head = f"{head} (Lv.{listing.get('level', 1)})"

        confirmed = await ctx.confirm(
            f"Buy **{head}** from <@{seller_id}> for **{fmt_token(price_human, 'BUD')}**?\n"
            f"-# Pulled from wallet+bank combined."
        )
        if not confirmed:
            return
        try:
            if kind == "egg":
                res = await bm.buy_listed_egg(
                    ctx.db, ctx.guild_id, ctx.author.id, int(listing_id),
                )
                tail = (
                    "Egg added to your held inventory. Hatch with "
                    "`,fish egg hatch` once your shelter has room."
                )
            else:
                res = await bm.buy_listed_buddy(
                    ctx.db, ctx.guild_id, ctx.author.id, int(listing_id),
                )
                tail = (
                    f"Buddy #{res.buddy_id} is now in your shelter -- "
                    f"promote with `,buddy active {res.buddy_id}`."
                )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await ctx.reply_success(
            f"Bought **{res.label}** for "
            f"**{fmt_token(to_human(res.price_paid_raw), 'BUD')}**.\n"
            f"-# {tail}\n"
            f"-# Transfer #{res.transfer_id} logged.",
            title="Purchased!",
        )

    @buddy.command(name="market", aliases=["browse"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_market(
        self, ctx: DiscoContext, *args: str,
    ) -> None:
        """Deprecated: the buddy market has moved to the auction house.

        Buddies and eggs now live alongside fish, crops, ore, weapons,
        armor, consumables, and crafted items in a single ``,ah``
        browser with categorised listings, dropdown filters, and
        cross-currency purchases.
        """
        prefix = await ctx.get_guild_prefix()
        await ctx.reply(
            embed=card(
                "\U0001F3DB Moved to the Auction House",
                color=C_INFO,
                description=(
                    "`,buddy market` has been consolidated into the new "
                    "auction house. Same listings, more filters, all "
                    "item kinds in one place.\n\n"
                    f"**Browse all:** `{prefix}ah`\n"
                    f"**Just buddies:** `{prefix}ah browse buddy`\n"
                    f"**Just eggs:** `{prefix}ah browse egg`\n"
                    f"**Search:** `{prefix}ah search <text>`\n"
                    f"**Buy:** `{prefix}ah buy <listing_id>`\n"
                    f"**Help:** `{prefix}ah help`"
                ),
            ).build(),
            mention_author=False,
        )

    @buddy.command(name="mylistings", aliases=["mylist", "listings"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_mylistings(self, ctx: DiscoContext) -> None:
        """Deprecated: see ``,ah mine`` for your active listings."""
        prefix = await ctx.get_guild_prefix()
        await ctx.reply(
            embed=card(
                "\U0001F3DB Moved to the Auction House",
                color=C_INFO,
                description=(
                    "`,buddy mylistings` has moved.\n\n"
                    f"**Your active listings:** `{prefix}ah mine`\n"
                    f"**Sold history:** `{prefix}ah sold`  ·  "
                    f"**Trade log:** `{prefix}ah history`\n"
                    f"**Cancel:** `{prefix}ah cancel <listing_id>`"
                ),
            ).build(),
            mention_author=False,
        )

    # -- Buddy-egg market subgroup -----------------------------------------

    @buddy.group(
        name="egg", aliases=["eggs"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_egg(self, ctx: DiscoContext) -> None:
        """Buddy-egg root: opens the egg picker (held + banked).

        Lists every held egg in an interactive panel with Hatch / Sell /
        Gift / List-on-AH buttons, plus deposit / withdraw between held
        and banked storage. ``,fish egg`` redirects here -- buddy eggs
        consolidated under the buddy surface.
        """
        from services import fishing as _fish
        from cogs.fishing import (
            _EggPickerView,
            _egg_status_embed,
            _oracle_pair,
        )
        summary = await _fish.list_held_eggs(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        lure_oracle, _ = await _oracle_pair(ctx)
        try:
            fren_row = await ctx.db.get_price("FREN", ctx.guild_id)
            fren_oracle = float(fren_row["price"]) if fren_row else 0.0
        except Exception:
            fren_oracle = 0.0
        embed = _egg_status_embed(
            ctx.author, summary,
            lure_oracle=lure_oracle, fren_oracle=fren_oracle,
        )
        view = _EggPickerView(
            ctx, summary,
            lure_oracle=lure_oracle, fren_oracle=fren_oracle,
        )
        msg = await ctx.reply(
            embed=embed, view=view, mention_author=False,
        )
        view.message = msg

    @buddy_egg.command(name="hatch")
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_egg_hatch(
        self, ctx: DiscoContext, species: str | None = None,
    ) -> None:
        """Hatch the oldest held egg (optionally filter by species).

        If active slots are full, the buddy lands in storage; if both
        active and storage are full, the hatch is refused with an error.
        """
        from services import fishing as _fish
        try:
            row = await _fish.hatch_held_egg(
                ctx.db, ctx.guild_id, ctx.author.id, species=species,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        sp = str(row.get("species") or "?")
        nm = str(row.get("name") or "?")
        tier = int(row.get("rarity_tier") or 1)
        emoji = str((SPECIES.get(sp) or {}).get("emoji") or "\U0001F95A")
        tier_name = str(rarity_meta(tier).get("name") or "Common")
        dest = str(row.get("_hatch_destination") or "owned")
        if dest == "stored":
            tail = (
                "Active slots were full -- they went to your **storage**. "
                "View / withdraw via `,buddy storage`."
            )
        else:
            tail = "_Promote it from `,buddy` to set it active._"
        await ctx.reply_success(
            f"{emoji} **{nm}** the {tier_name} {sp} hatched!\n{tail}",
            title="Egg Hatched",
        )

    @buddy_egg.command(name="sell")
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_egg_sell(
        self, ctx: DiscoContext, target: str = "1",
    ) -> None:
        """Sell held eggs for LURE.

        Examples:
            ``,buddy egg sell``         -- sells one (oldest)
            ``,buddy egg sell 5``       -- sells five (oldest first)
            ``,buddy egg sell all``     -- sells every held egg
            ``,buddy egg sell wecco 2`` -- two wecco eggs
        """
        from services import fishing as _fish
        species: str | None = None
        count: int | None = 1
        s = (target or "1").strip().lower()
        try:
            tail = (ctx.message.content or "").split(None, 4)
            extra = tail[4].strip().lower() if len(tail) >= 5 else ""
        except Exception:
            extra = ""
        if s in ("all", "everything"):
            count = None
        elif s.isdigit():
            count = int(s)
        elif s in SPECIES:
            species = s
            if extra in ("all", "everything"):
                count = None
            elif extra.isdigit():
                count = int(extra)
            else:
                count = None
        else:
            await ctx.reply_error_hint(
                f"Don't know how to sell `{target}`.",
                hint=(
                    "buddy egg sell  -  buddy egg sell 5  -  "
                    "buddy egg sell all  -  buddy egg sell wecco 2"
                ),
                command_name="buddy egg sell",
            )
            return
        try:
            res = await _fish.sell_held_eggs(
                ctx.db, ctx.guild_id, ctx.author.id,
                species=species, count=count,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # FREN oracle for the USD ≈ tag, mirroring mint_bbt_reward
        # receipts. Falls back gracefully when the price isn't seeded.
        try:
            fren_row = await ctx.db.get_price("FREN", ctx.guild_id)
            fren_oracle = float(fren_row["price"]) if fren_row else 0.0
        except Exception:
            fren_oracle = 0.0
        usd_tag = (
            f" ≈ {fmt_usd(res.lure_paid * fren_oracle)}"
            if fren_oracle > 0 else ""
        )
        tier_lines = [
            f"-# {rarity_meta(t).get('name', f'Tier {t}')} x{n}"
            for t, n in sorted(res.by_tier.items())
        ]
        msg = (
            f"Sold **{res.sold_count}** egg(s) for "
            f"**{res.lure_paid:,.2f} FREN**{usd_tag}.\n"
            + "\n".join(tier_lines) +
            f"\n-# **{res.leftover}** held egg(s) remaining."
        )
        await ctx.reply_success(msg, title="Eggs Sold")

    @buddy_egg.command(name="gift", aliases=["give", "send"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_egg_gift(
        self, ctx: DiscoContext, member: discord.Member,
        species_or_count: str = "1", count_arg: str | None = None,
    ) -> None:
        """Gift held eggs to another player.

        Examples:
            ``,buddy egg gift @user``         -- 1 egg
            ``,buddy egg gift @user 3``       -- 3 eggs (oldest first)
            ``,buddy egg gift @user wecco``   -- 1 wecco egg
            ``,buddy egg gift @user wecco 2`` -- 2 wecco eggs
        """
        from services import fishing as _fish
        if member is None or member.id == ctx.author.id:
            await ctx.reply_error("Pick another player to gift eggs to.")
            return
        species: str | None = None
        count = 1
        s = (species_or_count or "1").strip().lower()
        if s.isdigit():
            count = int(s)
        elif s in SPECIES:
            species = s
            if count_arg and count_arg.strip().isdigit():
                count = int(count_arg.strip())
        else:
            await ctx.reply_error_hint(
                f"Don't know how to gift `{species_or_count}`.",
                hint=(
                    "buddy egg gift @user  -  buddy egg gift @user 3  -  "
                    "buddy egg gift @user wecco 2"
                ),
                command_name="buddy egg gift",
            )
            return
        try:
            res = await _fish.gift_held_eggs(
                ctx.db, ctx.guild_id, ctx.author.id, member.id,
                species=species, count=count,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        tier_lines = [
            f"-# {rarity_meta(t).get('name', f'Tier {t}')} x{n}"
            for t, n in sorted(res.by_tier.items())
        ]
        await ctx.reply_success(
            f"Gifted **{res.moved_count}** egg(s) to "
            f"{member.mention}.\n" + "\n".join(tier_lines),
            title="Eggs Gifted",
        )

    @buddy_egg.command(name="panel", aliases=["banked"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_egg_panel(self, ctx: DiscoContext) -> None:
        """Show the consolidated held + banked egg panel.

        ``,buddy egg`` opens the held-egg picker (with action buttons);
        this subcommand opens the storage-style summary panel that lists
        held + banked side by side.
        """
        await self.buddy_storage_eggs(ctx)

    @buddy_egg.command(name="deposit", aliases=["bank"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_egg_deposit(
        self, ctx: DiscoContext, count: str = "all", species: str | None = None,
    ) -> None:
        """Move eggs from held inventory into banked egg storage.

        ``,buddy egg deposit``         -- moves every held egg that fits
        ``,buddy egg deposit 5``       -- moves up to 5 held eggs
        ``,buddy egg deposit 5 wecco`` -- only wecco eggs

        FIFO selection (oldest first), respects banked cap.
        """
        from services import buddy_storage_eggs as bse
        from services import fishing as _fish
        n_raw = (count or "").strip().lower()
        if n_raw in ("", "all", "max"):
            n = 10**6  # arbitrary high; deposit() clips by cap anyway
        else:
            try:
                n = int(n_raw)
            except (TypeError, ValueError):
                await ctx.reply_error(
                    f"Pass a count or `all`. Example: "
                    f"`,buddy egg deposit 5 wecco`."
                )
                return
            if n <= 0:
                await ctx.reply_error("Pass a positive count.")
                return
        sp = (species or "").strip().lower() or None
        # Pop from held, then deposit. If deposit can't accept all, the
        # leftover gets re-pushed onto held so the player doesn't lose
        # eggs to a half-failed move.
        popped = await _fish.pop_held_eggs(
            ctx.db, ctx.guild_id, ctx.author.id, n=n, species=sp,
        )
        if not popped:
            target = f" {sp}" if sp else ""
            await ctx.reply_error(
                f"You have no held{target} eggs to deposit. Catch some "
                f"with `,fish` or check `,buddy storage eggs`."
            )
            return
        accepted = await bse.deposit(
            ctx.db, ctx.guild_id, ctx.author.id, popped, from_="deposit",
        )
        if accepted < len(popped):
            leftovers = popped[accepted:]
            await _fish.push_held_eggs(
                ctx.db, ctx.guild_id, ctx.author.id, leftovers,
            )
        if accepted == 0:
            await ctx.reply_error(
                "Banked egg storage is full. Upgrade with "
                "`,buddy slot eggs buy` (50 rows per upgrade)."
            )
            return
        await ctx.reply_success(
            f"Banked **{accepted}** egg(s) into buddy storage.",
            title="Eggs deposited",
        )

    @buddy_egg.command(name="withdraw", aliases=["unbank", "take"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_breeding")
    async def buddy_egg_withdraw(
        self, ctx: DiscoContext, count: str = "1", species: str | None = None,
    ) -> None:
        """Move eggs from banked storage back into held inventory.

        ``,buddy egg withdraw 1``        -- pull one egg out of banked
        ``,buddy egg withdraw 3 wecco``  -- pull up to 3 wecco eggs

        Refuses if held is at its 10-egg cap.
        """
        from services import buddy_storage_eggs as bse
        from services import fishing as _fish
        from configs.buddies_config import EGG_HELD_HARD_CAP
        n_raw = (count or "").strip().lower()
        if n_raw in ("", "all", "max"):
            n = EGG_HELD_HARD_CAP
        else:
            try:
                n = int(n_raw)
            except (TypeError, ValueError):
                await ctx.reply_error(
                    "Pass a count, like `,buddy egg withdraw 1`."
                )
                return
            if n <= 0:
                await ctx.reply_error("Pass a positive count.")
                return
        sp = (species or "").strip().lower() or None
        held = await _fish.list_held_eggs(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        held_room = max(0, EGG_HELD_HARD_CAP - int(held.get("total") or 0))
        if held_room <= 0:
            await ctx.reply_error(
                f"Held egg slot is already at the cap "
                f"({EGG_HELD_HARD_CAP}). Hatch some with "
                f"`,fish egg hatch <species>` first."
            )
            return
        n = min(n, held_room)
        pulled = await bse.withdraw(
            ctx.db, ctx.guild_id, ctx.author.id, n=n, species=sp,
        )
        if not pulled:
            target = f" {sp}" if sp else ""
            await ctx.reply_error(
                f"No matching{target} eggs in banked storage. "
                f"Check `,buddy storage eggs`."
            )
            return
        await _fish.push_held_eggs(
            ctx.db, ctx.guild_id, ctx.author.id, pulled,
        )
        await ctx.reply_success(
            f"Withdrew **{len(pulled)}** egg(s) into held inventory.",
            title="Eggs withdrawn",
        )

    @buddy_egg.command(name="list")
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_egg_list(
        self, ctx: DiscoContext, species: str, price: str,
    ) -> None:
        """Deprecated: list eggs on the auction house with ``,ah list egg``."""
        prefix = await ctx.get_guild_prefix()
        sp = (species or "<species>").strip() or "<species>"
        pr = (price or "<price>").strip() or "<price>"
        await ctx.reply(
            embed=card(
                "\U0001F3DB Moved to the Auction House",
                color=C_INFO,
                description=(
                    "`,buddy egg list` has moved. Eggs sell on the "
                    "auction house alongside everything else now -- "
                    "and the listing flow accepts a species name OR a "
                    "numeric index.\n\n"
                    f"**List by species:** `{prefix}ah list egg {sp} {pr}`\n"
                    f"**List by index:** `{prefix}ah list egg 0 {pr}`  "
                    f"(see `{prefix}fish egg` for what you hold)\n"
                    f"**Browse eggs:** `{prefix}ah browse egg`\n"
                    f"**Cancel:** `{prefix}ah cancel <listing_id>`"
                ),
            ).build(),
            mention_author=False,
        )

    @buddy_egg.command(name="market", aliases=["browse"])
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_market")
    async def buddy_egg_market(
        self, ctx: DiscoContext, *args: str,
    ) -> None:
        """Deprecated: see ``,ah browse egg`` for egg listings."""
        prefix = await ctx.get_guild_prefix()
        await ctx.reply(
            embed=card(
                "\U0001F3DB Moved to the Auction House",
                color=C_INFO,
                description=(
                    "`,buddy egg market` has been replaced by the "
                    "categorised auction-house browser.\n\n"
                    f"**Eggs only:** `{prefix}ah browse egg`\n"
                    f"**All listings:** `{prefix}ah`\n"
                    f"**Search:** `{prefix}ah search <species>`\n"
                    f"**Buy:** `{prefix}ah buy <listing_id>`"
                ),
            ).build(),
            mention_author=False,
        )

    # -- Stat respec ---------------------------------------------------------

    @buddy.command(name="respec", aliases=["restat", "reroll-stats"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_respec(self, ctx: DiscoContext) -> None:
        """Refund all spent stat points on the active buddy for USD.

        Returns ``hp_alloc / atk_alloc / spd_alloc`` to 0 so the player
        can reallocate from scratch via ``,buddy upgrade``. Cost
        doubles per respec on the same buddy. Pulls from wallet+bank
        via ``deduct_liquid`` (same path as ``,buddy swap``).
        """
        gid = ctx.guild_id
        uid = ctx.author.id

        row = await _fetch_active(ctx.db, gid, uid)
        if not row:
            await ctx.reply_error_action(
                "You need a buddy to respec.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return

        respec_count = int(row.get("respec_count") or 0)
        price_usd = RESPEC_BASE_PRICE_USD * (2 ** respec_count)
        next_price = RESPEC_BASE_PRICE_USD * (2 ** (respec_count + 1))

        level, hp_alloc, atk_alloc, spd_alloc, available = _alloc_summary(row)
        spent = hp_alloc + atk_alloc + spd_alloc
        if spent <= 0:
            await ctx.reply_error(
                "Nothing to refund -- this buddy has no spent stat points. "
                f"Earn some by levelling, then `,buddy upgrade` to spend "
                f"them. (You'd save **${price_usd:,}** by waiting.)"
            )
            return

        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "your buddy")
        tier_name = rarity_meta(int(row.get("rarity_tier") or 1)).get("name", "Common")

        confirmed = await ctx.confirm(
            f"Respec {emoji} **{name}** ({tier_name} {species}) for "
            f"**${price_usd:,}**?\n\n"
            f"Refunds **{spent}** spent point(s):\n"
            f"  -  Hardiness **{hp_alloc}**\n"
            f"  -  Power **{atk_alloc}**\n"
            f"  -  Vigor **{spd_alloc}**\n\n"
            f"After respec you'll have **{level * STAT_POINTS_PER_LEVEL}** "
            f"points available to reallocate via `,buddy upgrade`.\n"
            f"Your next respec on this buddy will cost **${next_price:,}**.",
        )
        if not confirmed:
            return

        # Pull cost from wallet+bank. ``deduct_liquid`` raises ValueError
        # on insufficient balance; rewrap so the user sees the price.
        try:
            await ctx.db.deduct_liquid(uid, gid, to_raw(price_usd))
        except ValueError:
            await ctx.reply_error(
                f"Respec costs **${price_usd:,}** (wallet + bank combined). "
                f"You don't have enough."
            )
            return

        # Single UPDATE: zero allocations + bump respec_count. WHERE
        # guards on owner_user_id + status='owned' so a stale row from
        # a swapped-out buddy can't accidentally take effect.
        await ctx.db.execute(
            "UPDATE cc_buddies SET "
            "  hp_alloc = 0, atk_alloc = 0, spd_alloc = 0, "
            "  respec_count = respec_count + 1, "
            "  updated_at = NOW() "
            "WHERE id = $1 AND owner_user_id = $2 AND status = 'owned'",
            int(row["id"]), uid,
        )

        new_available = level * STAT_POINTS_PER_LEVEL
        await ctx.reply_success(
            f"{emoji} **{name}**'s stat allocations have been wiped.\n"
            f"You now have **{new_available}** points to spend with "
            f"`,buddy upgrade`.\n"
            f"Paid: **${price_usd:,}**. Next respec on this buddy: "
            f"**${next_price:,}**.",
            title="Respec'd!",
        )

    # -- Stat-point upgrade --------------------------------------------------

    @buddy.command(name="upgrade", aliases=["spend", "alloc", "points"])
    @guild_only
    @no_bots
    @ensure_registered
    async def buddy_upgrade(self, ctx: DiscoContext) -> None:
        """Spend earned stat points across Hardiness / Power / Vigor."""
        gid = ctx.guild_id
        uid = ctx.author.id

        row = await _fetch_active(ctx.db, gid, uid)
        if not row:
            await ctx.reply_error_action(
                "You need a buddy to upgrade.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return

        level, hp_alloc, atk_alloc, spd_alloc, available = _alloc_summary(row)
        species = str(row.get("species") or "")
        emoji = str(SPECIES.get(species, {}).get("emoji") or "")
        name = str(row.get("name") or "your buddy")

        embed = (
            card(f"{emoji} {name} - Upgrade", color=C_INFO)
            .description(
                f"Each level grants **{STAT_POINTS_PER_LEVEL}** stat point. "
                f"Press **Spend points** to allocate. Allocations stick "
                f"across swap and level changes."
            )
            .field(
                "Hardiness",
                f"+**{hp_alloc}** spent\n+{STAT_POINT_HP_BONUS:g} max HP / pt",
                inline=True,
            )
            .field(
                "Power",
                f"+**{atk_alloc}** spent\n+{STAT_POINT_ATK_BONUS:g} ATK / pt",
                inline=True,
            )
            .field(
                "Vigor",
                f"+**{spd_alloc}** spent\n+{STAT_POINT_SPD_BONUS * 100:g}% SPD / pt",
                inline=True,
            )
            .field(
                "Available",
                f"**{available}** / {level} earned",
                inline=False,
            )
            .build()
        )
        view = UpgradeView(self, uid, int(row["id"]))
        await ctx.send(embed=embed, view=view)

    # -- Species roster ------------------------------------------------------

    @buddy.command(name="species", aliases=["roster"])
    @guild_only
    async def buddy_species(self, ctx: DiscoContext) -> None:
        """Interactive roster of every species, filterable by affinity type.

        Opens an embed with a type-select dropdown -- pick All / Forest /
        Reef / Mine / Ruins / Neutral to see species with that
        ``expeditions_config.SPECIES_AFFINITY`` along with their hatch
        chance, signature lanes (species + rarity extras), ability,
        and base HP / ATK.
        """
        view = _SpeciesRosterView(ctx)
        embed = await view._build_embed()
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg


    # -- Pet battle ----------------------------------------------------------

    @buddy.group(name="battle", invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_battle")
    async def buddy_battle(self, ctx: DiscoContext) -> None:
        """Buddy battle hub -- show what the surface does + how to fight.

        Bare ``,buddy battle`` lands here and renders a help embed.
        Use ``,buddy battle fight @rival [amount]`` to actually start
        a duel.
        """
        if ctx.invoked_subcommand is not None:
            return
        embed = (
            card("\U00002694 Buddy Battles", color=C_PURPLE)
            .description(
                "Turn-based PvP between your active buddy and another "
                "player's. Stats + level + species ability decide the "
                "winner; the play-by-play renders as a unified battle "
                "scene with HP bars and action buttons.\n\n"
                "Winner takes XP + USD; staked battles ante up the same "
                "amount on both sides and the winner takes the pot. "
                "Draws refund both stakes."
            )
            .field(
                "Commands",
                "`,buddy battle fight @rival` -- friendly duel\n"
                "`,buddy battle fight @rival 500` -- staked duel "
                "(both ante $500, winner takes the pot)",
                False,
            )
            .field(
                "Rules",
                f"- One fight at a time per player (the fight lock blocks "
                f"queueing while a wild/PvE battle is open).\n"
                f"- {int(BATTLE_COOLDOWN_S // 60)} min cooldown per challenger.\n"
                "- Opponent must accept within the prompt timeout.\n"
                "- Both sides need an active buddy.",
                False,
            )
            .footer(
                "Looking for PvE BUD farming? Try ,buddy arena fight instead."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @buddy_battle.command(name="fight", aliases=["challenge", "duel"])
    @premium_required("buddy_battle")
    async def buddy_battle_fight(
        self,
        ctx: DiscoContext,
        opponent: discord.Member | None = None,
        amount: float | None = None,
    ) -> None:
        """Challenge another player's active buddy to a battle.

        Single-fight gate: this command refuses if the challenger
        already has a fight in flight (any combination of buddy PvP,
        fish wild, delve wild, farm wild, or escape event). The
        opponent's lock is checked when they accept, not here, so a
        challenge can still be issued while their previous fight is
        wrapping up; if they're still busy at accept-time the accept
        button itself will refuse.

        Pass an optional ``amount`` to make it a staked bet: both sides
        ante up the same USD amount, winner takes both stakes on top of
        the usual level-ratio prize. Draws refund both stakes.

        The opponent sees an Accept / Decline prompt. On accept the engine
        runs a turn-based duel (stats + level + species ability), posts
        the play-by-play, and awards the winner's buddy XP + USD.
        5 min cooldown per user.
        """
        # One-fight-at-a-time gate. Acquire the challenger's lock
        # before posting the challenge embed so a player can't queue
        # up multiple challenges. The lock is released when the
        # opponent declines / times out / the battle resolves; the
        # accept-button handler has its own opponent-side acquire.
        from services.fight_lock import acquire as _fl_acquire, release as _fl_release
        _fl_result = await _fl_acquire(
            ctx.db, ctx.guild_id, ctx.author.id, "buddy_pvp",
        )
        if not _fl_result.acquired:
            from services.fight_lock import FightLockBusy
            await ctx.reply_error(str(FightLockBusy(_fl_result)))
            return

        async def _release_lock() -> None:
            try:
                await _fl_release(
                    ctx.db, ctx.guild_id, ctx.author.id, kind="buddy_pvp",
                )
            except Exception:
                log.debug("buddy_pvp release failed", exc_info=True)

        if opponent is None or opponent.id == ctx.author.id or opponent.bot:
            await _release_lock()
            await ctx.reply_error(
                "Ping another (non-bot) player to battle, e.g. "
                "`,buddy battle fight @rival` or `,buddy battle fight @rival 500`.",
            )
            return

        gid = ctx.guild_id
        uid = ctx.author.id

        # Per-user challenger cooldown (both directions).
        now = time.time()
        last_c = self._last_battle_at.get((gid, uid), 0.0)
        if now - last_c < BATTLE_COOLDOWN_S:
            await ctx.reply_cooldown(BATTLE_COOLDOWN_S - (now - last_c))
            return

        # Validate the stake. None / 0 / negative -> no stake (friendly battle).
        stake: float = 0.0
        if amount is not None:
            try:
                stake = float(amount)
            except (TypeError, ValueError):
                await ctx.reply_error(
                    "Stake must be a number, e.g. `,buddy battle fight @rival 500`.",
                )
                return
            if stake < 0:
                await ctx.reply_error("Stake cannot be negative.")
                return
            if 0 < stake < BATTLE_STAKE_MIN:
                await ctx.reply_error(
                    f"Minimum stake is **${BATTLE_STAKE_MIN:,.2f}**. "
                    f"Drop the amount for a friendly no-stakes battle.",
                )
                return
            if stake > BATTLE_STAKE_MAX:
                await ctx.reply_error(
                    f"Max stake is **${BATTLE_STAKE_MAX:,.2f}**.",
                )
                return

        p1 = await _fetch_active(ctx.db, gid, uid)
        if not p1:
            await ctx.reply_error_action(
                "You need an active buddy to battle.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return
        busy_msg = _expedition_busy_message(dict(p1))
        if busy_msg:
            await ctx.reply_error(busy_msg)
            return
        p2 = await _fetch_active(ctx.db, gid, opponent.id)
        if not p2:
            await ctx.reply_error(
                f"**{opponent.display_name}** doesn't have an active buddy.",
            )
            return
        opp_busy_msg = _expedition_busy_message(dict(p2))
        if opp_busy_msg:
            await ctx.reply_error(
                f"**{opponent.display_name}**'s active buddy is on an "
                f"expedition. They can't battle right now."
            )
            return

        # Pre-flight balance check on the CHALLENGER (stake only -- the
        # opponent's balance is checked at accept time, since they might
        # spend between challenge-sent and accept-clicked). No debit yet;
        # escrow happens inside the accept branch so a decline / timeout
        # doesn't need to refund anything.
        if stake > 0:
            challenger_row = await ctx.db.get_user(uid, gid)
            challenger_wallet = int((challenger_row or {}).get("wallet") or 0)
            if challenger_wallet < to_raw(stake):
                await ctx.reply_error(
                    f"You don't have **${stake:,.2f}** in your wallet. "
                    f"Move some over from bank with `,move <amount>` first.",
                )
                return

        # Send the challenge. Only the opponent can accept / decline.
        challenge_view = _BattleChallengeView(opponent_id=opponent.id)
        p1_name, p1_block = _fighter_field(dict(p1), owner_name=ctx.author.display_name)
        p2_name, p2_block = _fighter_field(dict(p2), owner_name=opponent.display_name)

        # Stakes line: shown on both the challenge and result embeds so
        # both players know what they're risking before accepting. Draws
        # refund; accepting confirms the opponent agrees to ante up.
        if stake > 0:
            stake_blurb = (
                f"💰 **Stake: {fmt_usd(stake)} each**  -  winner takes "
                f"{fmt_usd(stake * 2)} plus the base prize. Draws refund."
            )
        else:
            stake_blurb = (
                f"-# Winner's buddy gains XP and the owner pockets USD "
                f"(both scale with the level ratio -- punching up pays more). "
                f"Losing is cosmetic."
            )

        # Title color leans gold so the prompt pops even when neither
        # buddy is a rare tier; tier colors return on the result embed.
        challenge_embed = (
            card(
                f"⚔️ Battle Challenge",
                color=C_GOLD,
            )
            .description(
                f"**{ctx.author.display_name}** vs **{opponent.display_name}**\n"
                f"{stake_blurb}\n\n"
                f"**{opponent.display_name}**, accept or decline below."
            )
            .field(p1_name, p1_block, True)
            .field(p2_name, p2_block, True)
            .footer(
                f"Expires in {BATTLE_CHALLENGE_TIMEOUT_S}s  -  "
                f"only {opponent.display_name} can respond."
            )
            .build()
        )
        # Autodelete budget: every buddy embed in this flow honours the
        # same per-guild setting. Fetched once up front so a mid-battle
        # DB blip can't make one message self-delete and another stick.
        _ad_secs = await self._buddy_delete_after(ctx.guild_id)

        challenge_msg = await ctx.reply(
            embed=challenge_embed, view=challenge_view, mention_author=False,
        )
        await challenge_view.wait()

        # The challenge message can be gone by the time the view wakes up
        # (user deleted it, mod cleaned the channel, Discord cache TTL on
        # a slow guild). `Message.edit` on a missing message throws
        # discord.NotFound (HTTP 404, error code 10008), which bubbles up
        # as a "Unknown Message" error embed instead of the intended
        # "challenge timed out" notice. Wrap every terminal-state edit so
        # the battle flow never leaks a 404 into chat.
        async def _safe_edit(**kwargs) -> None:
            try:
                await challenge_msg.edit(**kwargs)
            except discord.NotFound:
                pass  # message deleted while we waited -- nothing to update
            except discord.HTTPException:
                log.debug(
                    "buddy_battle: challenge_msg.edit failed",
                    exc_info=True,
                )

        if challenge_view.accepted is None:
            # Timed out.
            await _safe_edit(
                embed=card(
                    "Challenge timed out", color=C_NEUTRAL,
                ).description(
                    f"{opponent.display_name} didn't respond in time.",
                ).build(),
                view=None,
            )
            await self._schedule_autodelete(challenge_msg, _ad_secs)
            return
        if challenge_view.accepted is False:
            await _safe_edit(
                embed=card(
                    "Challenge declined", color=C_NEUTRAL,
                ).description(
                    f"{opponent.display_name} declined the battle.",
                ).build(),
                view=None,
            )
            await self._schedule_autodelete(challenge_msg, _ad_secs)
            return

        # Consent given -- lock in the challenger's cooldown and re-fetch
        # buddies in case anyone swapped/surrendered mid-prompt.
        self._last_battle_at[(gid, uid)] = time.time()

        p1 = await _fetch_active(ctx.db, gid, uid)
        p2 = await _fetch_active(ctx.db, gid, opponent.id)
        if not p1 or not p2:
            await _safe_edit(
                embed=card(
                    "Battle aborted", color=C_ERROR,
                ).description(
                    "One of the buddies is no longer available.",
                ).build(),
                view=None,
            )
            return

        # Escrow both stakes before the fight runs. update_wallet returns
        # None when the debit would take the wallet negative, so we can
        # tell which side actually covered the stake and roll back the
        # other. No DB writes for no-stake battles.
        stake_raw = to_raw(stake) if stake > 0 else 0
        if stake_raw > 0:
            chal_ok = await ctx.db.update_wallet(uid, gid, -stake_raw)
            if chal_ok is None:
                await _safe_edit(
                    embed=card(
                        "Battle aborted -- challenger short on funds",
                        color=C_ERROR,
                    ).description(
                        f"{ctx.author.display_name} no longer has "
                        f"**{fmt_usd(stake)}** in their wallet. "
                        f"Nothing was charged."
                    ).build(),
                    view=None,
                )
                return
            opp_ok = await ctx.db.update_wallet(opponent.id, gid, -stake_raw)
            if opp_ok is None:
                # Refund the challenger -- the opponent couldn't cover.
                await ctx.db.update_wallet(uid, gid, stake_raw)
                await _safe_edit(
                    embed=card(
                        "Battle aborted -- opponent short on funds",
                        color=C_ERROR,
                    ).description(
                        f"{opponent.display_name} doesn't have "
                        f"**{fmt_usd(stake)}** in their wallet. "
                        f"Stakes refunded."
                    ).build(),
                    view=None,
                )
                return

        # Run the fight INTERACTIVELY: both players pick actions per
        # round Pokemon-style. The view drives ten-ish round trips; we
        # then convert its final state into a BattleResult so the rest
        # of this command (stake settle + XP + result embed) drops in
        # unchanged. Any exception here triggers escrow refund.
        try:
            from services.buddy_battle import (
                Fighter as _Fighter,
                _xp_reward as _xp_calc,
                _usd_reward as _usd_calc,
            )
            p1_fighter = _Fighter.from_row(dict(p1))
            p2_fighter = _Fighter.from_row(dict(p2))

            pvp_view = _PvpBattleView(
                ctx=ctx,
                p1_user=ctx.author, p2_user=opponent,
                p1=p1_fighter, p2=p2_fighter,
            )
            opening_embed, opening_file = _pvp_round_embed(
                pvp_view.battle, ctx.author, opponent, opening=True,
            )
            await _safe_edit(
                embed=opening_embed,
                attachments=[opening_file],
                view=pvp_view,
            )
            pvp_view.message = challenge_msg
            await pvp_view.wait()

            b = pvp_view.battle
            winner_f = b.winner()
            loser_f = b.loser()
            xp = int(_xp_calc(winner_f, loser_f)) if (winner_f and loser_f) else 0
            usd = float(_usd_calc(winner_f, loser_f)) if (winner_f and loser_f) else 0.0
            result = BattleResult(
                winner=winner_f, loser=loser_f,
                rounds=int(min(b.round_num - 1, _PVP_BATTLE_MAX_ROUNDS)),
                xp_award=xp, usd_award=usd,
                log=list(b.log_lines),
            )
        except Exception:
            if stake_raw > 0:
                await ctx.db.update_wallet(uid, gid, stake_raw)
                await ctx.db.update_wallet(opponent.id, gid, stake_raw)
            log.exception("buddy_battle: pvp view raised; stakes refunded")
            await _safe_edit(
                embed=card(
                    "Battle aborted -- engine error",
                    color=C_ERROR,
                ).description(
                    "Something broke mid-fight. Stakes (if any) were refunded."
                ).build(),
                view=None,
            )
            return

        # Settle the stake. Winner takes both; draw refunds both.
        if stake_raw > 0:
            if result.winner is None:
                # Draw: refund both sides their original stake.
                await ctx.db.update_wallet(uid, gid, stake_raw)
                await ctx.db.update_wallet(opponent.id, gid, stake_raw)
            else:
                # Winner gets the full pot (their stake + opponent's).
                await ctx.db.update_wallet(
                    result.winner.owner_id, gid, 2 * stake_raw,
                )

        # Award XP to the winner's buddy (no level persist -- chat XP tick handles that).
        if result.winner and result.xp_award > 0:
            await award_battle_xp(
                self.bot.db, gid,
                winner_owner_id=result.winner.owner_id,
                winner_buddy_id=result.winner.id,
                xp=result.xp_award,
            )

        # USD prize to the winning OWNER's wallet. Scaled by level ratio
        # (see buddy_battle._usd_reward) so grinding low-level targets
        # can't print money. update_wallet takes a raw int; convert from
        # the float dollars the engine returned.
        if result.winner and result.usd_award > 0:
            try:
                await self.bot.db.update_wallet(
                    result.winner.owner_id, gid, to_raw(result.usd_award),
                )
            except Exception:
                log.exception(
                    "buddy_battle: USD credit failed uid=%s gid=%s amt=%s",
                    result.winner.owner_id, gid, result.usd_award,
                )

        # Persist wins / losses / battle_count / last_battle_at.
        await record_battle_result(
            self.bot.db,
            winner_buddy_id=result.winner.id if result.winner else None,
            loser_buddy_id=result.loser.id  if result.loser  else None,
        )
        if result.winner:
            await self.bot.bus.publish(
                "buddy_battle_win",
                guild=ctx.guild,
                user_id=int(result.winner.owner_id),
                winner_buddy_id=int(result.winner.id),
                loser_buddy_id=int(result.loser.id) if result.loser else None,
                source="pvp",
            )
        # Loss event for the loser so unified buddy_battle_loss tracking
        # picks up PvP defeats the same way it picks up arena / wild
        # battle losses.
        if result.loser:
            await self.bot.bus.publish(
                "buddy_battle_loss",
                guild=ctx.guild,
                user_id=int(result.loser.owner_id),
                winner_buddy_id=int(result.winner.id) if result.winner else None,
                loser_buddy_id=int(result.loser.id),
                source="pvp",
            )

        # AI commentary from winner + loser. Re-fetch their rows so the
        # AI sees the fresh W-L numbers (the generated line will reference
        # the current record).
        winner_line = ""
        loser_line  = ""
        if result.winner and result.loser:
            winner_row = await ctx.db.fetch_one(
                "SELECT * FROM cc_buddies WHERE id = $1", result.winner.id,
            )
            loser_row = await ctx.db.fetch_one(
                "SELECT * FROM cc_buddies WHERE id = $1", result.loser.id,
            )
            winner_member = ctx.guild.get_member(result.winner.owner_id) if ctx.guild else None
            loser_member  = ctx.guild.get_member(result.loser.owner_id)  if ctx.guild else None
            winner_owner = owner_label_for(
                getattr(winner_member, "display_name", None), result.winner.owner_id,
            )
            loser_owner = owner_label_for(
                getattr(loser_member, "display_name", None), result.loser.owner_id,
            )
            try:
                winner_line = await generate_reply(
                    dict(winner_row or {}), winner_owner, "battle_win",
                    extra=f"beat {result.loser.name} the {result.loser.species}",
                )
            except Exception:
                log.debug("battle: winner generate_reply failed", exc_info=True)
            try:
                loser_line = await generate_reply(
                    dict(loser_row or {}), loser_owner, "battle_loss",
                    extra=f"lost to {result.winner.name} the {result.winner.species}",
                )
            except Exception:
                log.debug("battle: loser generate_reply failed", exc_info=True)

            if winner_line:
                await record_event(
                    self.bot.db, int(result.winner.id), "battle_win",
                    f"beat {result.loser.name}; said: {winner_line}",
                )
            if loser_line:
                await record_event(
                    self.bot.db, int(result.loser.id), "battle_loss",
                    f"lost to {result.winner.name}; said: {loser_line}",
                )

        # Embed color = winner's rarity, or amber on draw.
        if result.winner:
            color = int(rarity_meta(result.winner.tier).get("color_hex") or C_GOLD)
        else:
            color = C_AMBER

        # Winner / loser header. Gives readers a one-line verdict at the
        # top without having to scan the whole log. Draw path falls through
        # to a neutral header so the embed always opens with something
        # human-readable.
        # Headline verdict. Kept short -- the final-HP fields below make
        # the outcome obvious at a glance; no need to repeat stat strings.
        if result.winner and result.loser:
            verdict_line = (
                f"🏆 **{result.winner.emoji} {result.winner.name}** "
                f"defeats "
                f"**{result.loser.emoji} {result.loser.name}**"
            )
        else:
            verdict_line = "⚖️ Draw -- both buddies limped away."

        # Log body. Strip the engine's intro preamble (fighter stats +
        # ability lines + leading blank) since those duplicate the
        # structured fields below. We walk forward until the first
        # `__**Round N**__` marker and drop everything before it.
        raw_lines = list(result.log)
        log_start = 0
        for idx, line in enumerate(raw_lines):
            if line.startswith("__**Round "):
                log_start = idx
                break
        log_text = "\n".join(raw_lines[log_start:]).strip()

        commentary_block = ""
        if winner_line or loser_line:
            commentary_block = "\n\n"
            if winner_line:
                commentary_block += (
                    f"> {result.winner.emoji} **{result.winner.name}:** {winner_line}"
                )
                if loser_line:
                    commentary_block += "\n"
            if loser_line:
                commentary_block += (
                    f"> {result.loser.emoji} **{result.loser.name}:** {loser_line}"
                )

        desc = f"{verdict_line}\n\n{log_text}{commentary_block}"
        if len(desc) > 3900:
            desc = desc[:3800] + "\n...  *(log truncated)*"

        builder = (
            card(
                f"⚔️ Pet Battle  -  {ctx.author.display_name} vs {opponent.display_name}",
                color=color,
            )
            .description(desc)
        )
        # Final-HP bars for each combatant. Displayed as inline fields so
        # the winner / loser sit side-by-side at the bottom of the embed.
        # Draw path falls through to a single "Both KO'd" summary.
        if result.winner and result.loser:
            wn, wv = _final_hp_field(
                result.winner.name, result.winner.emoji,
                result.winner.hp, result.winner.max_hp,
                is_winner=True,
            )
            ln, lv = _final_hp_field(
                result.loser.name, result.loser.emoji,
                result.loser.hp, result.loser.max_hp,
                is_winner=False,
            )
            builder = builder.field(wn, wv, True).field(ln, lv, True)
        # Summary strip: rounds + XP + USD prize + stake pot + cooldown.
        # Stake line only rendered for staked battles; draws note the
        # refund so both sides know their escrow came back.
        summary_bits = [
            f"Rounds: **{result.rounds}**",
            f"Winner XP: **+{result.xp_award}**",
            f"Prize: **{fmt_usd(result.usd_award)}**",
        ]
        if stake > 0:
            if result.winner is None:
                summary_bits.append(f"Stake: **{fmt_usd(stake)} refunded each**")
            else:
                summary_bits.append(f"Stake pot: **+{fmt_usd(stake * 2)}**")
        summary_bits.append(f"Cooldown: **{BATTLE_COOLDOWN_S // 60}m**")
        summary = "  -  ".join(summary_bits)
        builder = builder.field("📊 Summary", summary, False)
        battle_embed = builder.build()
        result_msg: discord.Message | None = None
        # Build a tiny persistent view with just a Bump button so the
        # result panel stays interactive + can be re-popped to the
        # bottom of the channel by either combatant. require_owner=False
        # because the inner _BattleResultView allows either user.
        from core.framework.persistent_embeds import BumpButton as _BumpButton
        result_view = discord.ui.View(timeout=None)
        # Bump alone on the bottom row per project convention.
        result_view.add_item(_BumpButton(
            int(uid), label="Bump", row=4, require_owner=False,
        ))
        # Override the view's interaction_check to allow the opponent too.
        async def _result_check(interaction: discord.Interaction) -> bool:
            if interaction.user.id in (int(uid), int(opponent.id)):
                return True
            await interaction.response.send_message(
                "Only the two combatants can bump this result.",
                ephemeral=True,
            )
            return False
        result_view.interaction_check = _result_check  # type: ignore[assignment]

        try:
            await challenge_msg.edit(embed=battle_embed, view=result_view)
            result_msg = challenge_msg
        except discord.NotFound:
            # Challenge message disappeared -- drop the battle result card
            # into the channel as a fresh message so the outcome still
            # reaches both players.
            try:
                result_msg = await ctx.reply(
                    embed=battle_embed, view=result_view, mention_author=False,
                )
            except discord.HTTPException:
                log.debug("buddy_battle: result reply failed", exc_info=True)
        except discord.HTTPException:
            log.debug("buddy_battle: result edit failed", exc_info=True)

        # Battle results are now persistent (Bump button instead of
        # autodelete) per the user-facing UX rule that interactive
        # embeds should not silently disappear. The autodelete path
        # still fires for declined / timed-out CHALLENGE prompts where
        # the battle never actually started -- handled in the
        # _BattleChallengeView path above.

    # ── Buddy Network arena ────────────────────────────────────────────────
    @buddy.group(
        name="arena", aliases=["bud_arena"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    @premium_required("buddy_battle")
    async def buddy_arena(self, ctx: DiscoContext) -> None:
        """Buddy arena hub -- bare invocation shows the help panel.

        Run ``,buddy arena fight`` to actually queue a PvE fight,
        ``,buddy arena boss`` for the daily boss, ``,buddy arena lb``
        for the leaderboard.
        """
        if ctx.invoked_subcommand is not None:
            return
        await self._render_buddy_arena_help(ctx)

    @buddy_arena.command(name="fight", aliases=["queue", "enter"])
    @premium_required("buddy_battle")
    async def buddy_arena_fight(self, ctx: DiscoContext) -> None:
        """Send your active buddy into the Buddy Network arena.

        Spawns a level-matched AI opponent. Win mints **BUD** into your
        Buddy Network wallet (the only PvE BUD-mint surface besides FREN
        stake yield) and applies the standard mint-impact oracle drop.
        Loss is a counter bump only -- no penalty, no payout.

        Cooldown is enforced DB-side via ``last_arena_at`` so container/
        DB clock skew can't fast-forward a player back into the queue.
        """
        from services import buddy_economy as _be

        gid = ctx.guild_id
        uid = ctx.author.id

        cd = await _be.arena_cooldown_remaining_s(ctx.db, gid, uid)
        if cd > 0:
            await ctx.reply_cooldown(cd)
            return

        active = await _fetch_active(ctx.db, gid, uid)
        if not active:
            await ctx.reply_error_action(
                "You need an active buddy to enter the arena.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return
        busy_msg = _expedition_busy_message(dict(active))
        if busy_msg:
            await ctx.reply_error(busy_msg)
            return

        # Pull the player's current arena tier so the intro embed can
        # display the active multiplier + the gap to the next promotion
        # ("4 wins to Silver"). Tiers come off lifetime arena_wins on
        # user_buddy_economy.
        state = await _be.ensure_state(ctx.db, gid, uid)
        cur_wins = int(state.get("arena_wins") or 0)
        cur_losses = int(state.get("arena_losses") or 0)
        cur_streak = int(state.get("arena_streak") or 0)
        best_streak = int(state.get("arena_best_streak") or 0)
        tier = _be.arena_tier_for_wins(cur_wins)
        next_tier = _be.arena_next_tier(cur_wins)

        opponent = _roll_arena_opponent(int(active.get("level") or 1))
        modifier = _roll_arena_modifier()
        embed = _build_arena_intro_embed(
            dict(active), opponent,
            tier=tier, next_tier=next_tier,
            arena_wins=cur_wins, arena_losses=cur_losses,
            streak=cur_streak, best_streak=best_streak,
            modifier=modifier, is_boss=False,
        )
        view = _BuddyArenaView(
            cog=self, ctx=ctx, active=dict(active), opponent=opponent,
            modifier=modifier, is_boss=False,
        )
        sent = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = sent

    @buddy_arena.command(name="help", aliases=["?", "info"])
    @premium_required("buddy_battle")
    async def buddy_arena_help(self, ctx: DiscoContext) -> None:
        """Show the Buddy Network arena mechanics + tier ladder."""
        await self._render_buddy_arena_help(ctx)

    async def _render_buddy_arena_help(self, ctx: DiscoContext) -> None:
        """Build + send the Buddy Network arena help embed.

        Shared between the bare ``,buddy arena`` group root (which now
        shows help instead of jumping straight into a fight) and the
        explicit ``,buddy arena help`` subcommand.
        """
        from services import buddy_economy as _be

        state = await _be.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        cur_wins = int(state.get("arena_wins") or 0)
        cur_losses = int(state.get("arena_losses") or 0)
        cur_tier = _be.arena_tier_for_wins(cur_wins)
        next_tier = _be.arena_next_tier(cur_wins)

        tier_lines: list[str] = []
        for tier in _be.ARENA_TIERS:
            key, label, emoji, min_wins, mult, _hex = tier
            marker = " \U0001F448 you" if key == cur_tier["key"] else ""
            tier_lines.append(
                f"{emoji} **{label}** -- {min_wins}+ wins, "
                f"`x{mult:.2f}` BUD reward{marker}"
            )

        bud_meta = Config.TOKENS.get("BUD", {}) or {}
        bud_emoji = bud_meta.get("emoji") or ""
        bbt_meta = Config.TOKENS.get("BBT", {}) or {}
        bbt_emoji = bbt_meta.get("emoji") or ""

        you_line = (
            f"{cur_tier['emoji']} **{cur_tier['label']}**  -  "
            f"`x{cur_tier['bud_mult']:.2f}` BUD multiplier  "
            f"({cur_wins} wins / {cur_losses} losses)"
        )
        if next_tier is not None:
            gap = max(0, int(next_tier["min_wins"]) - cur_wins)
            you_line += (
                f"\n-# {gap} win(s) to {next_tier['emoji']} "
                f"**{next_tier['label']}** "
                f"(`x{next_tier['bud_mult']:.2f}`)"
            )
        else:
            you_line += "\n-# Maxed out -- you're at the top tier."

        # Modifier table for the help panel. Each line is "emoji label --
        # flavor (+bonus)" so the player sees what every roll can do.
        mod_lines: list[str] = []
        for mod, _w in _ARENA_MODIFIERS:
            if mod.key == "none":
                continue
            mod_lines.append(
                f"{mod.emoji} **{mod.label}** -- {mod.flavor} "
                f"(+{int(mod.reward_bonus * 100)}% reward)"
            )

        # Streak surface: current + best + the cap so the player knows the
        # ladder. Also surfaces the milestone callouts so they can chase
        # the 3 / 5 / 10 / 20 / 50 toasts.
        cur_streak_val = int(state.get("arena_streak") or 0)
        best_streak_val = int(state.get("arena_best_streak") or 0)
        streak_lines = [
            f"\U0001F525 Current: **{cur_streak_val}**  -  "
            f"best **{best_streak_val}**",
            f"+{int(_be.ARENA_STREAK_BONUS_PER_WIN * 100)}% per consecutive "
            f"win, cap +{int(_be.ARENA_STREAK_BONUS_MAX * 100)}%. "
            "Loss resets the streak (best is permanent).",
            f"Milestones: {' · '.join(str(m) for m in _be.ARENA_STREAK_MILESTONES)} "
            "wins fire a special callout.",
        ]

        embed = (
            card(
                "\U0001F3DF️ Buddy Network Arena",
                color=int(cur_tier["color_hex"]),
            )
            .description(
                "Send your active CC buddy into the Buddy Network arena "
                "for a turn-based PvE fight. Win mints **BUD** + **BBT** "
                "directly into your wallet; loss is a counter bump that "
                "resets your streak.\n\n"
                "`,buddy arena fight` to queue a fight, "
                "`,buddy arena boss` once-a-day boss, "
                "`,buddy arena lb` wins board, "
                "`,buddy arena streaks` streak board."
            )
            .field("Your Standing", you_line, False)
            .field(
                f"{bbt_emoji} BBT Reward Formula (headline)",
                (
                    f"base = **{_be.ARENA_BBT_REWARD_BASE:.1f}** + "
                    f"**{_be.ARENA_BBT_REWARD_PER_LEVEL:.1f}** per buddy "
                    f"level (cap **{_be.ARENA_BBT_REWARD_MAX:.0f}**)\n"
                    "reward = base * "
                    "(1 + clean + streak + modifier) * tier mult * boss mult\n"
                    f"+ {bud_emoji} **BUD** drip "
                    f"(base **{_be.ARENA_BUD_REWARD_BASE:.1f}** + "
                    f"**{_be.ARENA_BUD_REWARD_PER_LEVEL:.2f}**/lv, "
                    f"cap **{_be.ARENA_BUD_REWARD_MAX:.0f}**) on top.\n"
                    "-# Bonuses stack additively inside the (1+...) factor; "
                    "tier multiplier + boss multiplier stack multiplicatively."
                ),
                False,
            )
            .field("Tier Ladder", "\n".join(tier_lines), False)
            .field("Streak", "\n".join(streak_lines), False)
            .field(
                "\U0001F47A Daily Boss",
                (
                    f"Lv +{_be.ARENA_BOSS_LEVEL_BUMP}, "
                    f"`x{_be.ARENA_BOSS_HP_MULT:.2f}` HP / "
                    f"`x{_be.ARENA_BOSS_ATK_MULT:.2f}` ATK.\n"
                    f"Win pays `x{_be.ARENA_BOSS_PAYOUT_MULT:.0f}` BUD + BBT "
                    f"plus +**{_be.ARENA_BOSS_BBT_BONUS:.0f}** BBT and "
                    f"+**{_be.ARENA_BOSS_BUD_BONUS:.0f}** BUD flat.\n"
                    "One attempt per 24h. `,buddy arena boss`."
                ),
                False,
            )
            .field(
                "Modifiers (random per fight)",
                "\n".join(mod_lines),
                False,
            )
            .field(
                "Cooldown",
                f"**{_be.ARENA_COOLDOWN_S}s** between fights "
                f"(boss has its own 24h gate).",
                True,
            )
            .field(
                "Combat",
                f"Strike (+1 stamina)\n"
                f"Special ({_ARENA_SPECIAL_STAMINA_COST} stamina cost)\n"
                f"Brace (heal + halve next hit)\n"
                f"Risky (60% huge / 25% miss / 15% backfire)",
                True,
            )
            .footer(
                "Arena W/L feeds achievements, quests, challenges, and "
                "the cross-surface buddy_battle_win counter."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @buddy_arena.command(name="lb", aliases=["leaderboard", "top"])
    @premium_required("buddy_battle")
    async def buddy_arena_lb(self, ctx: DiscoContext) -> None:
        """Top arena fighters in this guild, ranked by wins."""
        from services import buddy_economy as _be

        rows = await _be.list_arena_leaderboard(
            ctx.db, ctx.guild_id, limit=50,
        )
        if rows:
            from core.framework.leaderboard import filter_lb_user_ids
            keep = await filter_lb_user_ids(
                ctx, [int(r["user_id"]) for r in rows],
            )
            rows = [r for r in rows if int(r["user_id"]) in keep][:10]
        if not rows:
            embed = (
                card(
                    "\U0001F3DF️ Arena Leaderboard",
                    color=C_NEUTRAL,
                )
                .description(
                    "No arena fighters yet -- be the first! "
                    "Run `,buddy arena` to get on the board."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        bud_meta = Config.TOKENS.get("BUD", {}) or {}
        bud_emoji = bud_meta.get("emoji") or ""
        medals = ("\U0001F947", "\U0001F948", "\U0001F949")

        lines: list[str] = []
        for idx, row in enumerate(rows):
            uid = int(row.get("user_id") or 0)
            wins = int(row.get("arena_wins") or 0)
            losses = int(row.get("arena_losses") or 0)
            total_raw = int(row.get("arena_bud_earned_raw") or 0)
            total_h = to_human(total_raw)
            best_streak = int(row.get("arena_best_streak") or 0)
            cur_streak = int(row.get("arena_streak") or 0)
            boss_w = int(row.get("arena_boss_wins") or 0)
            tier = _be.arena_tier_for_wins(wins)
            member = ctx.guild.get_member(uid) if ctx.guild else None
            name = member.display_name if member else f"<@{uid}>"
            rank_tag = medals[idx] if idx < 3 else f"`#{idx + 1:>2}`"
            extras: list[str] = []
            if cur_streak > 0:
                extras.append(f"\U0001F525 {cur_streak}")
            if best_streak > cur_streak:
                extras.append(f"best **{best_streak}**")
            if boss_w > 0:
                extras.append(f"\U0001F47A {boss_w}")
            extras_tag = f"  -  {' / '.join(extras)}" if extras else ""
            lines.append(
                f"{rank_tag} **{name}**  -  "
                f"{tier['emoji']} {tier['label']}  -  "
                f"**{wins}**W / **{losses}**L  -  "
                f"{fmt_token(total_h, 'BUD', bud_emoji)}{extras_tag}"
            )

        top_tier = _be.arena_tier_for_wins(int(rows[0].get("arena_wins") or 0))
        embed = (
            card(
                "\U0001F3DF️ Buddy Network Arena - Leaderboard",
                color=int(top_tier["color_hex"]),
            )
            .description("\n".join(lines))
            .footer(
                "Ranked by lifetime arena wins. "
                "`,buddy arena streaks` for streak board, `,buddy arena boss` for daily."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @buddy_arena.command(name="streaks", aliases=["streak"])
    @premium_required("buddy_battle")
    async def buddy_arena_streaks(self, ctx: DiscoContext) -> None:
        """Top arena win-streak holders in this guild.

        Different ordering surface than ``,buddy arena lb`` so a player who
        stacks tight win streaks gets a separate spotlight from a pure-volume
        grinder. Ordered by best streak then current streak.
        """
        from services import buddy_economy as _be

        rows = await _be.list_arena_streak_leaderboard(
            ctx.db, ctx.guild_id, limit=10,
        )
        if not rows:
            embed = (
                card(
                    "\U0001F525 Arena Streak Leaderboard",
                    color=C_NEUTRAL,
                )
                .description(
                    "No streaks recorded yet -- string a few wins together "
                    "with `,buddy arena` to make the board."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        medals = ("\U0001F947", "\U0001F948", "\U0001F949")
        lines: list[str] = []
        for idx, row in enumerate(rows):
            uid = int(row.get("user_id") or 0)
            best = int(row.get("arena_best_streak") or 0)
            cur = int(row.get("arena_streak") or 0)
            wins = int(row.get("arena_wins") or 0)
            tier = _be.arena_tier_for_wins(wins)
            member = ctx.guild.get_member(uid) if ctx.guild else None
            name = member.display_name if member else f"<@{uid}>"
            rank_tag = medals[idx] if idx < 3 else f"`#{idx + 1:>2}`"
            cur_tag = f"  -  active \U0001F525 **{cur}**" if cur > 0 else ""
            lines.append(
                f"{rank_tag} **{name}**  -  "
                f"{tier['emoji']} {tier['label']}  -  "
                f"best \U0001F525 **{best}**{cur_tag}"
            )

        top_tier = _be.arena_tier_for_wins(int(rows[0].get("arena_wins") or 0))
        embed = (
            card(
                "\U0001F525 Buddy Network Arena - Streak Board",
                color=int(top_tier["color_hex"]),
            )
            .description("\n".join(lines))
            .footer(
                f"Streak grants +5%/win up to "
                f"+{int(_be.ARENA_STREAK_BONUS_MAX * 100)}%. "
                "Loss resets to 0; best is permanent."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @buddy_arena.command(name="boss", aliases=["daily"])
    @premium_required("buddy_battle")
    async def buddy_arena_boss(self, ctx: DiscoContext) -> None:
        """Once-per-day arena boss fight: high level, big payout.

        The boss is the player's active-buddy level + ARENA_BOSS_LEVEL_BUMP
        with HP / ATK scaled up. Win pays 4x BUD + BBT plus a flat boss
        cherry. One attempt every 24 hours, gated DB-side. Modifiers apply
        the same way they do in normal arena.
        """
        from services import buddy_economy as _be

        gid = ctx.guild_id
        uid = ctx.author.id

        # Per-day boss cooldown (24h) takes priority over the standard
        # arena cooldown so the user gets a clear "come back tomorrow"
        # message instead of a generic 60s wait.
        boss_cd = await _be.arena_boss_cooldown_remaining_s(ctx.db, gid, uid)
        if boss_cd > 0:
            await ctx.reply_cooldown(boss_cd)
            return
        cd = await _be.arena_cooldown_remaining_s(ctx.db, gid, uid)
        if cd > 0:
            await ctx.reply_cooldown(cd)
            return

        active = await _fetch_active(ctx.db, gid, uid)
        if not active:
            await ctx.reply_error_action(
                "You need an active buddy to challenge the daily boss.",
                button_label="Hatch one",
                command="buddy hatch",
            )
            return
        busy_msg = _expedition_busy_message(dict(active))
        if busy_msg:
            await ctx.reply_error(busy_msg)
            return

        state = await _be.ensure_state(ctx.db, gid, uid)
        cur_wins = int(state.get("arena_wins") or 0)
        cur_losses = int(state.get("arena_losses") or 0)
        cur_streak = int(state.get("arena_streak") or 0)
        best_streak = int(state.get("arena_best_streak") or 0)
        boss_wins = int(state.get("arena_boss_wins") or 0)
        tier = _be.arena_tier_for_wins(cur_wins)
        next_tier = _be.arena_next_tier(cur_wins)

        opponent = _roll_arena_opponent(
            int(active.get("level") or 1), is_boss=True,
        )
        modifier = _roll_arena_modifier()
        embed = _build_arena_intro_embed(
            dict(active), opponent,
            tier=tier, next_tier=next_tier,
            arena_wins=cur_wins, arena_losses=cur_losses,
            streak=cur_streak, best_streak=best_streak,
            modifier=modifier, is_boss=True, boss_wins=boss_wins,
        )
        view = _BuddyArenaView(
            cog=self, ctx=ctx, active=dict(active), opponent=opponent,
            modifier=modifier, is_boss=True,
        )
        sent = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = sent

    # -- World event: escaped shelter buddy ----------------------------------
    #
    # Every ESCAPED_EVENT_INTERVAL_S the background loop rolls a chance per
    # guild that has a bot channel configured. On a hit, a random shelter
    # buddy "escapes" and a public Battle prompt appears in one of the
    # guild's bot_channels. The first player to win the PvE fight adopts
    # the buddy for free; an unclaimed prompt returns the buddy to the
    # shelter.

    async def _world_loop(self) -> None:
        """Spawn escaped-buddy world events on a randomized cadence."""
        while True:
            try:
                await asyncio.sleep(ESCAPED_EVENT_INTERVAL_S)
                await self._spawn_escaped_events()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("buddy world loop: unhandled error")

    async def _spawn_escaped_events(self) -> None:
        """Walk every guild the bot is in, roll the spawn chance, post.

        Per-guild base roll uses ``ESCAPED_EVENT_CHANCE``. If any user in
        the guild has an active battle attractor (bought at ,buddy shop),
        the roll for THAT guild scales by ``ATTRACTOR_BUFF_MULT`` -- it's
        a guild-level boost so the buyer's whole server benefits, not
        just the buyer's DMs.
        """
        from services import buddy_economy as _bes
        for guild in list(self.bot.guilds):
            try:
                chance = ESCAPED_EVENT_CHANCE
                # Cheap one-row check: any active attractor in this guild?
                row = await self.bot.db.fetch_one(
                    """
                    SELECT 1 FROM user_buddy_economy
                     WHERE guild_id = $1
                       AND attractor_until IS NOT NULL
                       AND attractor_until > NOW()
                     LIMIT 1
                    """,
                    guild.id,
                )
                if row is not None:
                    chance = min(0.95, chance * float(_bes.ATTRACTOR_BUFF_MULT))
                if random.random() > chance:
                    continue
                await self._try_spawn_escape(guild)
            except Exception:
                log.debug(
                    "buddy world: spawn attempt failed gid=%s",
                    guild.id, exc_info=True,
                )

    async def _try_spawn_escape(self, guild: discord.Guild) -> None:
        channel_ids = await self.bot.db.get_bot_channels(guild.id)
        if not channel_ids:
            return
        channels: list[discord.TextChannel] = []
        for cid in channel_ids:
            ch = guild.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel):
                perms = ch.permissions_for(guild.me) if guild.me else None
                if perms and perms.send_messages and perms.embed_links:
                    channels.append(ch)
        if not channels:
            return

        candidate = await pick_escape_candidate(self.bot.db, guild.id)
        if not candidate:
            return
        row = await mark_escaped(self.bot.db, int(candidate["id"]))
        if not row:
            return  # raced: someone adopted it between pick and mark

        target = random.choice(channels)
        species = str(row.get("species") or "")
        meta = SPECIES.get(species, {})
        emoji = str(meta.get("emoji") or "")
        name = str(row.get("name") or species.title())
        tier = int(row.get("rarity_tier") or 1)
        tier_meta = rarity_meta(tier)
        tier_name = str(tier_meta.get("name") or "Common")

        # Structured stat block so challengers know what they're up against
        # before they click. Reuses _fighter_field for consistency with
        # the PvP challenge embed.
        wild_field_name, wild_field_value = _fighter_field(dict(row))

        embed = (
            card(
                f"🌲 A wild {tier_name} {species.title()} appears!",
                color=int(tier_meta.get("color_hex") or C_GOLD),
            )
            .description(
                f"{emoji} **{name}** broke loose from the shelter and is "
                f"looking for a fight.\n\n"
                f"Click ⚔️ **Challenge** to duel them. Winner adopts the "
                f"buddy for free; losing is free too -- the wild buddy "
                f"stays out and anyone else can try."
            )
            .field(wild_field_name, wild_field_value, False)
            .footer(
                f"Expires in {ESCAPED_EVENT_TIMEOUT_S // 60}m  -  "
                f"first to win claims them."
            )
            .build()
        )

        view = _EscapedBuddyView(
            cog=self,
            buddy_id=int(row["id"]),
            guild_id=guild.id,
            row=dict(row),
        )
        try:
            msg = await target.send(embed=embed, view=view)
        except discord.HTTPException:
            # Couldn't post -- return the buddy to the shelter so a future
            # tick can try again.
            await reclaim_to_shelter(self.bot.db, int(row["id"]))
            return
        view.message = msg

        _ad_secs = await self._buddy_delete_after(guild.id)
        await self._schedule_autodelete(msg, _ad_secs)


class _BattleChallengeView(discord.ui.View):
    """Two-button prompt (Accept / Decline) gated to a single opponent.

    Lives just long enough to collect consent for a battle. Sets
    ``self.accepted`` to True / False / None (timeout) before stopping.
    """

    def __init__(self, *, opponent_id: int) -> None:
        super().__init__(timeout=BATTLE_CHALLENGE_TIMEOUT_S)
        self.opponent_id = opponent_id
        self.accepted: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message(
                "Only the challenged player can accept or decline.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        self.accepted = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        self.accepted = False
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()


class _EscapedBuddyView(discord.ui.View):
    """Public 'Battle this escaped buddy' prompt.

    Single ⚔️ Challenge button. First user to click who has an active
    buddy runs a PvE battle against the escaped buddy using the shared
    run_battle engine. On victory the escaped buddy flips to 'owned'
    under the challenger; on a loss (or if the challenger has no buddy)
    the prompt stays open for the next hopeful.

    Timeout returns the buddy to the shelter so it can be adopted
    normally or picked up by a later world event.
    """

    def __init__(
        self,
        *,
        cog: "Buddy",
        buddy_id: int,
        guild_id: int,
        row: dict,
    ) -> None:
        super().__init__(timeout=ESCAPED_EVENT_TIMEOUT_S)
        self.cog = cog
        self.buddy_id = buddy_id
        self.guild_id = guild_id
        self.row = row
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._resolved = False

    async def on_timeout(self) -> None:
        if self._resolved:
            return
        try:
            await reclaim_to_shelter(self.cog.bot.db, self.buddy_id)
        except Exception:
            log.debug(
                "escape on_timeout: reclaim failed bid=%s",
                self.buddy_id, exc_info=True,
            )
        if self.message is not None:
            for child in self.children:
                child.disabled = True  # type: ignore[attr-defined]
            try:
                await self.message.edit(
                    embed=card(
                        "The wild buddy slipped away...",
                        color=C_NEUTRAL,
                    ).description(
                        f"No one took the challenge in time. "
                        f"**{self.row.get('name')}** returned to the "
                        f"shelter and can be adopted with `,buddy shelter`."
                    ).build(),
                    view=None,
                )
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(label="Challenge", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def challenge_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        # Serialize clicks so two simultaneous challengers don't both
        # fight the escaped buddy. The loser in a race gets an
        # ephemeral "someone else is fighting it" message.
        if self._lock.locked():
            await interaction.response.send_message(
                "Someone else just started a fight with this buddy. Hang on.",
                ephemeral=True,
            )
            return
        async with self._lock:
            if self._resolved:
                await interaction.response.send_message(
                    "This buddy already found an owner.", ephemeral=True,
                )
                return

            challenger = interaction.user
            db = self.cog.bot.db
            p1 = await _fetch_active(db, self.guild_id, challenger.id)
            if not p1:
                await interaction.response.send_message(
                    "You need an active buddy to fight. Try `,buddy hatch` or "
                    "`,buddy shelter`.",
                    ephemeral=True,
                )
                return

            # Defer the interaction; the fight + DB writes take longer
            # than Discord's 3s ack budget.
            await interaction.response.defer(thinking=False)

            # Build the PvE opponent row. owner_user_id=0 signals "wild" to
            # the engine; Fighter.from_row already defaults owner_id to 0.
            p2 = dict(self.row)
            p2["owner_user_id"] = 0

            # Interactive resolution: spawn _WildBattleView, let the
            # challenger pick actions Pokemon-style, then convert the
            # final state into a BattleResult so the existing
            # adopt / reward / record block below stays unchanged.
            from services.buddy_battle import (
                Fighter as _Fighter,
                _xp_reward as _xp_calc,
                _usd_reward as _usd_calc,
            )
            try:
                p1_fighter = _Fighter.from_row(dict(p1))
                p2_fighter = _Fighter.from_row(p2)
            except Exception:
                log.exception(
                    "wild battle: Fighter.from_row failed bid=%s",
                    self.buddy_id,
                )
                await interaction.followup.send(
                    "Couldn't start the fight (engine error). "
                    "The wild buddy is still on the loose.",
                    ephemeral=True,
                )
                return

            wild_view = _WildBattleView(
                ctx=self.cog, challenger=challenger,
                p1=p1_fighter, p2=p2_fighter,
            )
            try:
                _embed, _file = _pvp_round_embed(
                    wild_view.battle, challenger,
                    _wild_user_stub(wild_view.battle),
                    opening=True,
                )
                opening_msg = await interaction.followup.send(
                    embed=_embed,
                    file=_file,
                    view=wild_view,
                    ephemeral=True,
                )
                wild_view.message = opening_msg
            except discord.HTTPException:
                log.exception("wild battle: opening post failed")
                return
            await wild_view.wait()

            b = wild_view.battle
            winner_f = b.winner()
            loser_f = b.loser()
            xp_amt = int(_xp_calc(winner_f, loser_f)) if (winner_f and loser_f) else 0
            usd_amt = float(_usd_calc(winner_f, loser_f)) if (winner_f and loser_f) else 0.0
            result = BattleResult(
                winner=winner_f, loser=loser_f,
                rounds=int(min(b.round_num - 1, _PVP_BATTLE_MAX_ROUNDS)),
                xp_award=xp_amt, usd_award=usd_amt,
                log=list(b.log_lines),
            )

            winner_is_challenger = bool(
                result.winner and int(result.winner.id) == int(p1["id"])
            )

            # Strip the engine's intro preamble from the log -- fighter stats
            # are rendered as structured fields below, so the description only
            # needs the round-by-round text.
            raw_lines = list(result.log)
            _log_start = 0
            for _i, _line in enumerate(raw_lines):
                if _line.startswith("__**Round "):
                    _log_start = _i
                    break
            log_text = "\n".join(raw_lines[_log_start:]).strip()

            if winner_is_challenger:
                # Claim the lock immediately so a second click can't race
                # us between here and the result edit.
                self._resolved = True

                # Award XP + USD FIRST, before attempting adoption. The
                # fight was clean; the reward is earned regardless of
                # whether the adoption itself succeeds (the player might
                # be at the buddy cap, which isn't their fault in terms
                # of having won the battle). Old behavior silently ate
                # the battle result plus all rewards on adoption failure,
                # which looked like the battle never happened.
                if result.winner and result.xp_award > 0:
                    await award_battle_xp(
                        db, self.guild_id,
                        winner_owner_id=challenger.id,
                        winner_buddy_id=int(p1["id"]),
                        xp=result.xp_award,
                    )
                if result.usd_award > 0:
                    try:
                        await db.update_wallet(
                            challenger.id, self.guild_id, to_raw(result.usd_award),
                        )
                    except Exception:
                        log.exception(
                            "escape battle: USD credit failed uid=%s gid=%s amt=%s",
                            challenger.id, self.guild_id, result.usd_award,
                        )

                # Record W/L before the adoption attempt. The escapee's
                # loss persists on its row whether or not the challenger
                # ends up owning it.
                await record_battle_result(
                    db,
                    winner_buddy_id=int(p1["id"]),
                    loser_buddy_id=self.buddy_id,
                )
                await self.cog.bot.bus.publish(
                    "buddy_battle_win",
                    guild=interaction.guild,
                    user_id=int(challenger.id),
                    winner_buddy_id=int(p1["id"]),
                    loser_buddy_id=int(self.buddy_id),
                    source="escaped",
                )

                # Try the adoption. Failure (usually "at buddy cap") just
                # means the buddy can't move to the challenger. We
                # BANISH the row in that case -- the wild buddy was
                # defeated, and letting it slink back into the shelter
                # pool would just re-escape on the next world tick and
                # loop the same fight forever (user-reported bug). The
                # challenger still keeps their XP/USD.
                ok, err, adopted = await adopt_escaped(
                    db, self.guild_id, challenger.id, self.buddy_id,
                )
                adoption_failed_reason: str | None = None
                if not ok:
                    await banish_defeated(db, self.buddy_id)
                    adoption_failed_reason = err or "adoption failed"

                name = str(self.row.get("name") or "the buddy")
                species = str(self.row.get("species") or "")
                emoji = str(SPECIES.get(species, {}).get("emoji") or "")
                tier_meta = rarity_meta(int(self.row.get("rarity_tier") or 1))
                color_hex = int(tier_meta.get("color_hex") or C_SUCCESS)

                # Verdict shape depends on adoption outcome. Win rewards
                # are identical either way; only the "you now own this"
                # bit changes.
                if adoption_failed_reason is None:
                    title_line = f"⚔️ Wild Buddy Tamed  -  {challenger.display_name}"
                    verdict = (
                        f"🏆 **{challenger.display_name}** tamed "
                        f"**{emoji} {name}** the "
                        f"{tier_meta.get('name') or 'Common'} "
                        f"{species.title()}."
                    )
                    adopt_summary = f"Adopted: **{name}**"
                else:
                    title_line = f"⚔️ Wild Buddy Banished  -  {challenger.display_name}"
                    verdict = (
                        f"🏆 **{challenger.display_name}** beat "
                        f"**{emoji} {name}** fair and square.\n\n"
                        f"⚠️ Couldn't adopt: {adoption_failed_reason}\n"
                        f"{name} was defeated and is gone -- "
                        f"surrender a buddy first if you want to claim "
                        f"the next one. Rewards still paid out."
                    )
                    adopt_summary = "Adopted: *gone*"
                    color_hex = int(C_AMBER)  # yellow = "won but with caveat"

                desc = f"{verdict}\n\n{log_text}"
                if len(desc) > 3900:
                    desc = desc[:3800] + "\n...  *(log truncated)*"

                builder = card(title_line, color=color_hex).description(desc)
                if result.winner and result.loser:
                    wn, wv = _final_hp_field(
                        result.winner.name, result.winner.emoji,
                        result.winner.hp, result.winner.max_hp,
                        is_winner=True,
                    )
                    ln, lv = _final_hp_field(
                        result.loser.name, result.loser.emoji,
                        result.loser.hp, result.loser.max_hp,
                        is_winner=False,
                    )
                    builder = builder.field(wn, wv, True).field(ln, lv, True)
                builder = builder.field(
                    "📊 Summary",
                    f"Rounds: **{result.rounds}**  -  "
                    f"XP: **+{result.xp_award}**  -  "
                    f"Prize: **{fmt_usd(result.usd_award)}**  -  "
                    f"{adopt_summary}",
                    False,
                )
                result_embed = builder.build()

                # Always post the battle visual publicly. The fight
                # actually happened -- hiding it behind an ephemeral
                # error was the bug the user hit (post-adoption-fail,
                # from their perspective "no battle ever ran").
                edited = False
                for child in self.children:
                    child.disabled = True  # type: ignore[attr-defined]
                if self.message is not None:
                    try:
                        await self.message.edit(embed=result_embed, view=None)
                        edited = True
                    except (discord.NotFound, discord.HTTPException):
                        pass
                if not edited:
                    try:
                        await interaction.followup.send(
                            embed=result_embed, ephemeral=True,
                        )
                    except discord.HTTPException:
                        log.debug(
                            "escape battle: followup post failed",
                            exc_info=True,
                        )
                self.stop()
                return

            # Challenger lost (or drew). Leave the buddy escaped and the
            # prompt open so someone else can try.

            # Record the loss on the challenger's buddy AND the win on
            # the wild escapee (still referenceable by id even though it
            # has no owner right now). Kept consistent with PvP: every
            # resolved fight writes to both fighters' counters, so the
            # shelter buddy's record carries forward when it's eventually
            # adopted.
            if result.winner and result.loser:
                # result.loser is the challenger's buddy here.
                await record_battle_result(
                    db,
                    winner_buddy_id=int(result.winner.id),
                    loser_buddy_id=int(result.loser.id),
                )
                # Loss event so unified buddy_battle_loss tracking picks
                # this up alongside arena / wild battle losses.
                try:
                    await self.cog.bot.bus.publish(
                        "buddy_battle_loss",
                        guild=interaction.guild,
                        user_id=int(challenger.id),
                        winner_buddy_id=int(result.winner.id),
                        loser_buddy_id=int(result.loser.id),
                        source="escaped",
                    )
                except Exception:
                    log.debug(
                        "escape battle: buddy_battle_loss publish failed",
                        exc_info=True,
                    )
            log_snippet = log_text
            if len(log_snippet) > 1800:
                log_snippet = log_snippet[:1700] + "\n...  *(log truncated)*"
            wild_name = str(self.row.get("name") or "the wild buddy")
            wild_emoji = str(
                SPECIES.get(str(self.row.get("species") or ""), {}).get("emoji") or ""
            )
            builder = (
                card(
                    f"💀 {challenger.display_name} lost the fight",
                    color=C_NEUTRAL,
                )
                .description(
                    f"{wild_emoji} **{wild_name}** is still on the loose. "
                    f"Someone else can try.\n\n"
                    f"{log_snippet}"
                )
            )
            if result.winner and result.loser:
                wn, wv = _final_hp_field(
                    result.winner.name, result.winner.emoji,
                    result.winner.hp, result.winner.max_hp,
                    is_winner=True,
                )
                ln, lv = _final_hp_field(
                    result.loser.name, result.loser.emoji,
                    result.loser.hp, result.loser.max_hp,
                    is_winner=False,
                )
                builder = builder.field(wn, wv, True).field(ln, lv, True)
            try:
                await interaction.followup.send(
                    embed=builder.build(), ephemeral=True,
                )
            except discord.HTTPException:
                pass


# =============================================================================
# Buddy Network arena
# =============================================================================
# PvE arena built on the shared interactive battle helpers in
# services/buddy_battle.py. Same Strike/Special/Brace/Risky model as the
# fishing wild-battle and delve wild-buddy views, with the BUD reward
# wired through services.buddy_economy.resolve_arena_battle on win.

from services.buddy_battle import (
    INTERACTIVE_BATTLE_MAX_ROUNDS as _ARENA_BATTLE_MAX_ROUNDS,
    INTERACTIVE_PLAYER_STAMINA_MAX as _ARENA_PLAYER_STAMINA_MAX,
    INTERACTIVE_SPECIAL_STAMINA_COST as _ARENA_SPECIAL_STAMINA_COST,
    ARENA_MODIFIERS as _ARENA_MODIFIERS,
    ArenaModifier as _ArenaModifier,
    Fighter as _ArenaFighter,
    LiveBattle as _ArenaLiveBattle,
    apply_arena_modifier as _apply_arena_modifier,
    apply_player_action as _arena_apply_player_action,
    apply_round_effects as _arena_apply_round_effects,
    compute_battle_bonus as _arena_compute_battle_bonus,
    enemy_ai_turn as _arena_enemy_ai_turn,
    hp_bar as _arena_hp_bar,
    roll_arena_modifier as _roll_arena_modifier,
)


def _roll_arena_opponent(
    player_level: int, *, is_boss: bool = False,
) -> dict:
    """Build a level-matched AI opponent row for an arena fight.

    Returns a dict shaped like the cc_buddies row that
    ``services.buddy_battle.Fighter.from_row`` accepts. Mood is pinned
    at 100 (full HP / ATK / SPD floor) so the AI always fights at peak.
    Level jitter keeps the arena unpredictable; rarity follows the same
    weighted distribution the rest of the buddy system uses.

    Boss opponents (``is_boss=True``) sit ARENA_BOSS_LEVEL_BUMP levels
    above the player, always roll Legendary rarity, and carry a fixed
    "Boss" name prefix so the intro embed reads correctly.
    """
    from services.buddy_economy import (
        ARENA_BOSS_LEVEL_BUMP as _BLB,
    )
    pool = [s for s in SPECIES.keys() if isinstance(s, str) and s]
    species = random.choice(pool) if pool else "fox"
    base_level = max(1, int(player_level))
    if is_boss:
        level = base_level + int(_BLB)
        # Pin to Legendary so the boss feels distinct.
        rarity_tier = max(int(roll_rarity()), 5)
    else:
        level = max(1, base_level + random.randint(-2, 2))
        rarity_tier = roll_rarity()
    sp_meta = SPECIES.get(species, {})
    name_pool = sp_meta.get("name_pool") or [species.title()]
    base_name = random.choice(name_pool)
    name = f"Boss {base_name}" if is_boss else base_name
    return {
        "id": 0,
        "owner_user_id": 0,
        "species": species,
        "name": name,
        "rarity_tier": int(rarity_tier),
        "level": int(level),
        "hunger":    100,
        "happiness": 100,
        "energy":    100,
        "hp_alloc":  0,
        "atk_alloc": 0,
        "spd_alloc": 0,
    }


def _build_arena_intro_embed(
    active: dict,
    opponent: dict,
    *,
    tier: dict | None = None,
    next_tier: dict | None = None,
    arena_wins: int = 0,
    arena_losses: int = 0,
    streak: int = 0,
    best_streak: int = 0,
    modifier: "_ArenaModifier | None" = None,
    is_boss: bool = False,
    boss_wins: int = 0,
) -> discord.Embed:
    """Pre-fight embed with both fighters' stat blocks + tier panel.

    Surfaces the player's current win streak (so they know what they're
    risking on a loss) and the rolled arena modifier (so they can plan
    their opening). Boss fights flip the title + reward footer.
    """
    from services.buddy_economy import (
        ARENA_STREAK_BONUS_MAX as _SBM,
        ARENA_BOSS_LEVEL_BUMP as _BLB,
        ARENA_BOSS_PAYOUT_MULT as _BPM,
        arena_streak_bonus as _streak_bonus_fn,
    )
    p_name, p_block = _fighter_field(dict(active))
    op_meta = SPECIES.get(str(opponent.get("species") or ""), {})
    if is_boss:
        op_name = (
            f"\U0001F47A BOSS: {op_meta.get('emoji') or ''} "
            f"{opponent.get('name')}"
        )
    else:
        op_name = (
            f"\U0001F916 Arena Bot: {op_meta.get('emoji') or ''} "
            f"{opponent.get('name')}"
        )
    o_name, o_block = _fighter_field(
        dict(opponent),
        owner_name="Arena Boss" if is_boss else "Arena Bot",
    )
    color = int((tier or {}).get("color_hex") or C_PURPLE)
    if is_boss:
        # Boss fights override tier color with C_AMBER so they look distinct
        # in the channel feed even at low player tiers.
        color = C_AMBER
    title = (
        "\U0001F47A  Buddy Arena - Daily Boss" if is_boss
        else "\U0001F3DF️  Buddy Network Arena"
    )
    if is_boss:
        desc = (
            f"⚠ **DAILY BOSS** - opponent is +{int(_BLB)} levels and "
            f"hits harder. Win pays "
            f"`x{_BPM:.0f}` BUD + BBT plus a flat boss cherry. "
            "One attempt every 24h.\n"
            "Tap **Challenge** to commit, **Cancel** to back out "
            "(daily attempt is preserved)."
        )
    else:
        desc = (
            "Send your active buddy into the arena to mint **BUD** + **BBT**.\n"
            "Tap **Challenge** to enter the fight, or **Cancel** to back "
            "out (no cooldown spent on cancel)."
        )
    builder = (
        card(title, color=color)
        .description(desc)
        .field(p_name, p_block, True)
        .field(o_name, o_block, True)
    )
    if tier is not None:
        line = (
            f"{tier['emoji']} **{tier['label']}**  -  "
            f"`x{tier['bud_mult']:.2f}` BUD multiplier  "
            f"({int(arena_wins)}W / {int(arena_losses)}L)"
        )
        if next_tier is not None:
            gap = max(0, int(next_tier["min_wins"]) - int(arena_wins))
            line += (
                f"\n-# {gap} win(s) to {next_tier['emoji']} "
                f"**{next_tier['label']}** "
                f"(`x{next_tier['bud_mult']:.2f}`)"
            )
        else:
            line += "\n-# Top tier reached -- Diamond multiplier locked in."
        builder = builder.field("Your Tier", line, False)

    # Streak surface. Always visible (even at 0) so the player gets the
    # carrot/loss-aversion read at a glance. Boss fights show the boss
    # W/L instead since they don't touch the regular streak.
    if is_boss:
        builder = builder.field(
            "Boss Record",
            f"\U0001F480 **{int(boss_wins)}** lifetime boss kills.",
            True,
        )
    else:
        cur_bonus = _streak_bonus_fn(int(streak))
        if streak > 0:
            line = (
                f"\U0001F525 **{int(streak)}** win streak  -  "
                f"`+{cur_bonus * 100:.0f}%` reward bonus"
            )
            if cur_bonus < _SBM:
                line += f" _(cap +{_SBM * 100:.0f}%)_"
            else:
                line += " _(maxed)_"
            line += f"\n-# Best: **{int(best_streak)}**"
        else:
            line = (
                "\U0001F4A4 No active streak. Win to start one "
                f"(+5%/win, cap +{_SBM * 100:.0f}%)."
            )
            if best_streak > 0:
                line += f"\n-# Personal best: **{int(best_streak)}**"
        builder = builder.field("Streak", line, True)

    # Modifier surface. Always visible so players read the rules of
    # engagement before committing to the fight.
    if modifier is not None:
        mod_value = (
            f"{modifier.emoji} **{modifier.label}**\n{modifier.flavor}"
        )
        if modifier.reward_bonus > 0:
            mod_value += (
                f"\n-# Modifier reward bonus: "
                f"`+{modifier.reward_bonus * 100:.0f}%`"
            )
        builder = builder.field("Modifier", mod_value, False)

    if is_boss:
        footer = (
            "Win pays 4x BUD + BBT plus a flat boss cherry. "
            "Lose: counter bump only -- daily attempt is gone either way. "
            "`,buddy arena help`."
        )
    else:
        footer = (
            "Win pays BUD + BBT scaled by buddy level + clean-fight bonus + "
            "tier multiplier + streak + modifier. Loss resets your streak. "
            "`,buddy arena help`."
        )
    return builder.footer(footer).build()


class _BuddyArenaView(discord.ui.View):
    """Interactive arena fight: Challenge / Cancel -> Strike / Special / Brace / Risky."""

    def __init__(
        self,
        *,
        cog: "Buddy",
        ctx: DiscoContext,
        active: dict,
        opponent: dict,
        modifier: _ArenaModifier | None = None,
        is_boss: bool = False,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.active = dict(active)
        self.opponent = dict(opponent)
        self.modifier = modifier
        self.is_boss = bool(is_boss)
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._resolved = False
        self._battle: _ArenaLiveBattle | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This is someone else's arena run -- start your own with "
                "`,buddy arena`.",
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
            try:
                player_f = _ArenaFighter.from_row(dict(self.active))
                enemy_f = _ArenaFighter.from_row(dict(self.opponent))
            except Exception:
                log.exception(
                    "buddy arena: Fighter.from_row failed gid=%s uid=%s",
                    self.ctx.guild_id, self.owner_id,
                )
                self._resolved = True
                try:
                    await interaction.response.edit_message(
                        embed=card(
                            "\U0001F4A5 Arena offline",
                            color=C_ERROR,
                        ).description(
                            "Couldn't seed the fight. Try again in a moment "
                            "-- no cooldown spent."
                        ).build(),
                        view=None,
                    )
                except discord.HTTPException:
                    pass
                return

            # Boss fights scale the AI fighter's HP/ATK on top of the level
            # bump applied at roll time. Apply BEFORE building LiveBattle
            # so the modifier hooks (Glass Cannon halves HP, etc.) start
            # from the boss-scaled baseline.
            if self.is_boss:
                from services.buddy_economy import (
                    ARENA_BOSS_HP_MULT as _BHM,
                    ARENA_BOSS_ATK_MULT as _BAM,
                )
                enemy_f.max_hp = int(round(enemy_f.max_hp * float(_BHM)))
                enemy_f.hp = enemy_f.max_hp
                enemy_f.atk = enemy_f.atk * float(_BAM)

            self._battle = _ArenaLiveBattle(player=player_f, enemy=enemy_f)

            # Apply the rolled arena modifier (None == "standard"). Done
            # AFTER battle construction so the modifier reads max_hp /
            # atk off the freshly built fighters and can write the
            # brace_heal_pct override on the battle.
            if self.modifier is not None:
                try:
                    _apply_arena_modifier(self._battle, self.modifier.key)
                except Exception:
                    log.exception(
                        "buddy arena: modifier apply failed key=%s",
                        getattr(self.modifier, "key", "?"),
                    )
            self.clear_items()
            self.add_item(self._make_action_button(
                "Strike", "\U00002694", "strike",
                discord.ButtonStyle.primary,
            ))
            # Special button surfaces the player buddy's actual ability
            # name (e.g. "Pack Howl" for a wolf, "Hard Shell" for a
            # crab) so the player can see what they're casting -- same
            # treatment as ,buddy map battle.
            self.add_item(self._make_action_button(
                str(player_f.ability_name or "Special")[:20] or "Special",
                "\U0001F4A5", "special",
                discord.ButtonStyle.success,
            ))
            self.add_item(self._make_action_button(
                "Brace", "\U0001F6E1️", "brace",
                discord.ButtonStyle.secondary,
            ))
            self.add_item(self._make_action_button(
                "Risky", "\U0001F3AF", "risky",
                discord.ButtonStyle.danger,
            ))
            self._refresh_action_button_state()
            _embed, _file = self._round_embed(opening=True)
            try:
                _kw: dict = {"embed": _embed, "view": self}
                if _file is not None:
                    _kw["attachments"] = [_file]
                await interaction.response.edit_message(**_kw)
            except discord.HTTPException:
                log.debug("buddy arena: opening edit failed", exc_info=True)

    @discord.ui.button(
        label="Cancel", emoji="\U0000274C",
        style=discord.ButtonStyle.secondary, row=0,
    )
    async def btn_cancel(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if self._resolved or self._battle is not None:
            await interaction.response.defer()
            return
        self._resolved = True
        self.stop()
        try:
            await interaction.response.edit_message(
                embed=card(
                    "\U0001F300 Arena run cancelled",
                    color=C_NEUTRAL,
                ).description(
                    "No fight, no cooldown spent. Try `,buddy arena` again "
                    "whenever you're ready."
                ).build(),
                view=None,
            )
        except discord.HTTPException:
            log.debug("buddy arena: cancel edit failed", exc_info=True)

    def _make_action_button(
        self, label: str, emoji: str, action_key: str,
        style: discord.ButtonStyle,
    ) -> discord.ui.Button:
        btn = discord.ui.Button(
            label=label, emoji=emoji, style=style, disabled=False,
        )
        # Stamp the action_key onto the button so refresh logic can find
        # the Special button by intent, not by label (the label may now
        # be the buddy's named ability instead of the literal string
        # "Special").
        btn.action_key = action_key  # type: ignore[attr-defined]

        async def _cb(interaction: discord.Interaction) -> None:
            await self._handle_action(interaction, action_key)

        btn.callback = _cb
        return btn

    def _refresh_action_button_state(self) -> None:
        if not self._battle:
            return
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if getattr(child, "action_key", None) == "special":
                child.disabled = (
                    self._battle.player_stamina < _ARENA_SPECIAL_STAMINA_COST
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

            # Defer up front -- bursts will overrun the 3s interaction
            # window. Final edit goes through self.message directly.
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                log.debug("buddy arena: defer failed", exc_info=True)

            # Player swing burst.
            from services.buddy_battle_scene import play_battle_action_burst
            await play_battle_action_burst(
                self, b.player, b.enemy,
                actor_side="p1",
                action=str(action_key),
                round_num=int(b.round_num),
                max_rounds=_ARENA_BATTLE_MAX_ROUNDS,
                ability_name=str(getattr(b.player, "ability_name", "") or ""),
            )

            new_lines = _arena_apply_player_action(b, action_key)
            b.log_lines.extend(new_lines)
            if b.is_over():
                b.log_lines.append("")
                await self._finalize(interaction)
                return

            # Enemy AI swing burst (always strike-flavoured).
            await play_battle_action_burst(
                self, b.player, b.enemy,
                actor_side="p2", action="strike",
                round_num=int(b.round_num),
                max_rounds=_ARENA_BATTLE_MAX_ROUNDS,
            )

            ai_lines = _arena_enemy_ai_turn(b)
            b.log_lines.extend(ai_lines)
            if not b.is_over():
                b.log_lines.extend(_arena_apply_round_effects(b))
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
                log.debug("buddy arena: round edit failed", exc_info=True)

    def _round_embed(
        self, *, opening: bool = False, action_banner: str = "",
    ) -> tuple[discord.Embed, "discord.File | None"]:
        """Return ``(embed, scene_file)`` -- the embed references the
        unified battle scene PNG so ``,buddy arena`` shares visuals
        with every other buddy battle in the game."""
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
        if self.is_boss:
            title = f"\U0001F47A Round {b.round_num}  -  Arena Boss"
            color = C_AMBER
        else:
            title = f"\U0001F3DF️ Round {b.round_num}  -  Arena"
            color = C_PURPLE
        # Append the active modifier label to the title so the player sees
        # what's in effect on every round, not just the intro.
        if self.modifier is not None and self.modifier.key != "none":
            title += f"  -  {self.modifier.emoji} {self.modifier.label}"
        stamina_pips = (
            "●" * b.player_stamina
            + "○" * (_ARENA_PLAYER_STAMINA_MAX - b.player_stamina)
        )
        enemy_label = "BOSS" if self.is_boss else "Arena"
        desc_lines = [
            f"{p_emoji} **{p.name}**  Lv.{p.level} {p.tier_name}",
            f"  HP `{_arena_hp_bar(p.hp, p.max_hp)}`  -  ATK {int(p.atk)}",
            f"  Stamina `{stamina_pips}` "
            f"({b.player_stamina}/{_ARENA_PLAYER_STAMINA_MAX})",
            "",
            f"{e_emoji} **{enemy_label} {e.name}**  Lv.{e.level} {e.tier_name}",
            f"  HP `{_arena_hp_bar(e.hp, e.max_hp)}`  -  ATK {int(e.atk)}",
            "",
            tail,
        ]
        if opening:
            desc_lines.append(
                f"-# Strike (+1 stamina)  •  Special "
                f"({_ARENA_SPECIAL_STAMINA_COST} stamina)  •  "
                f"Brace (heal + halve next hit)  •  Risky "
                f"(60% huge / 25% miss / 15% backfire)"
            )

        # Battle scene PNG -- shared renderer across every battle view.
        scene_file = None
        try:
            from services.buddy_battle_scene import (
                fighters_to_scene_state, render_battle_frame,
            )
            import io as _io
            state = fighters_to_scene_state(
                p, e,
                round_num=int(b.round_num),
                max_rounds=_ARENA_BATTLE_MAX_ROUNDS,
                action_banner=action_banner or ("FIGHT!" if opening else ""),
                is_player_turn=True,
            )
            png = render_battle_frame(state)
            scene_file = discord.File(_io.BytesIO(png), filename="battle.png")
        except Exception:
            log.debug("buddy arena: scene render failed", exc_info=True)

        builder = card(title, color=color).description("\n".join(desc_lines))
        if scene_file is not None:
            builder = builder.image("attachment://battle.png")
        return builder.build(), scene_file

    async def _finalize(self, interaction: discord.Interaction) -> None:
        from services import buddy_economy as _be

        self._resolved = True
        b = self._battle
        assert b is not None
        won = b.player_won()
        bonus_pct = _arena_compute_battle_bonus(b) if won else 0.0

        # _handle_action already defers the interaction up front so the
        # per-move burst animations don't blow Discord's 3s response
        # window. Defer again only when nothing has responded yet (e.g.
        # the on_timeout path that bypasses _handle_action).
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

        mod = self.modifier
        try:
            res = await _be.resolve_arena_battle(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
                won=won,
                winner_level=int(self.active.get("level") or 1),
                bonus_pct=bonus_pct,
                is_boss=self.is_boss,
                modifier_key=str(getattr(mod, "key", "none")),
                modifier_reward_bonus=float(
                    getattr(mod, "reward_bonus", 0.0) or 0.0
                ),
            )
        except Exception:
            log.exception(
                "buddy arena: resolve failed uid=%s gid=%s",
                self.owner_id, self.ctx.guild_id,
            )
            res = None

        # Bump cc_buddies.wins/losses for the player's active buddy so
        # the per-buddy W/L surfaces (panel, ,buddy battles board) count
        # arena fights alongside PvP. Best-effort; never blocks the
        # result render.
        try:
            await record_pve_battle_result(
                self.ctx.db,
                player_buddy_id=int(self.active.get("id") or 0) or None,
                won=bool(won),
                rounds=int(b.round_num),
            )
        except Exception:
            log.debug(
                "buddy arena: record_pve_battle_result failed",
                exc_info=True,
            )

        # Bus fan-out. ``buddy_battle_win`` / ``_loss`` are the unified
        # cross-surface events that achievements / quests / challenges
        # subscribe to (PvP + escaped + fish + delve + arena all publish
        # them). The ``buddy_arena_*`` triplet fires alongside so a
        # per-arena quest / challenge can target this surface specifically.
        bus = getattr(self.cog.bot, "bus", None)
        if bus is not None:
            try:
                source = "arena_boss" if self.is_boss else "arena"
                await bus.publish(
                    "buddy_arena_spawn",
                    guild=self.ctx.guild_id, user_id=self.owner_id,
                    is_boss=bool(self.is_boss),
                )
                if won:
                    await bus.publish(
                        "buddy_battle_win",
                        guild=self.ctx.guild_id, user_id=self.owner_id,
                        winner_buddy_id=int(self.active.get("id") or 0),
                        loser_buddy_id=None,
                        source=source,
                    )
                    await bus.publish(
                        "buddy_arena_won",
                        guild=self.ctx.guild_id, user_id=self.owner_id,
                        is_boss=bool(self.is_boss),
                    )
                    if self.is_boss:
                        await bus.publish(
                            "buddy_arena_boss_won",
                            guild=self.ctx.guild_id, user_id=self.owner_id,
                        )
                else:
                    await bus.publish(
                        "buddy_battle_loss",
                        guild=self.ctx.guild_id, user_id=self.owner_id,
                        winner_buddy_id=None,
                        loser_buddy_id=int(self.active.get("id") or 0),
                        source=source,
                    )
                    await bus.publish(
                        "buddy_arena_lost",
                        guild=self.ctx.guild_id, user_id=self.owner_id,
                        is_boss=bool(self.is_boss),
                    )
                    if self.is_boss:
                        await bus.publish(
                            "buddy_arena_boss_lost",
                            guild=self.ctx.guild_id, user_id=self.owner_id,
                        )
            except Exception:
                log.debug("buddy arena: bus publish failed", exc_info=True)

        embed, scene_file = await self._render_final_embed(b, res, bonus_pct)
        if self.message is not None:
            try:
                _kw: dict = {"embed": embed, "view": None}
                if scene_file is not None:
                    _kw["attachments"] = [scene_file]
                await self.message.edit(**_kw)
            except discord.HTTPException:
                log.debug("buddy arena: final edit failed", exc_info=True)

    async def _render_final_embed(
        self,
        b: _ArenaLiveBattle,
        res: "Any | None",
        bonus_pct: float,
    ) -> tuple[discord.Embed, "discord.File | None"]:
        species = str(self.opponent.get("species") or "?").title()
        won = b.player_won()
        rounds = b.round_num
        is_boss = bool(self.is_boss)
        boss_label = "Boss " if is_boss else "Arena "
        mod = self.modifier

        if won and res is not None:
            bud_h = to_human(int(res.bud_reward_raw))
            bud_meta = Config.TOKENS.get("BUD", {}) or {}
            bud_emoji = bud_meta.get("emoji") or ""
            bbt_h = to_human(int(getattr(res, "bbt_reward_raw", 0) or 0))
            bbt_meta = Config.TOKENS.get("BBT", {}) or {}
            bbt_emoji = bbt_meta.get("emoji") or ""
            tier_after = res.tier_after or {}
            # USD-equivalent of the reward at oracle-after for BUD +
            # current BBT oracle. Surfaces to the player so they can
            # value the win without bouncing to ,balance.
            bud_oracle_after = float(getattr(res, "bud_oracle_after", 0.0) or 0.0)
            try:
                bbt_price_row = await self.ctx.db.get_price(
                    "BBT", self.ctx.guild_id,
                )
                bbt_oracle = float((bbt_price_row or {}).get("price") or 0.0)
            except Exception:
                bbt_oracle = 0.0
            usd_value = (bud_h * bud_oracle_after) + (bbt_h * bbt_oracle)
            usd_tag = f"  ~ **{fmt_usd(usd_value)}**" if usd_value > 0 else ""
            lines = [
                f"\U0001F3C6 Your **{b.player.name}** beat "
                f"**{boss_label}{species}** in {rounds} rounds.",
                (
                    f"+{fmt_token(bud_h, 'BUD', bud_emoji)} · "
                    f"+{fmt_token(bbt_h, 'BBT', bbt_emoji)}{usd_tag}"
                    if bbt_h > 0 else
                    f"+{fmt_token(bud_h, 'BUD', bud_emoji)}{usd_tag}"
                ),
            ]
            if bonus_pct > 0:
                lines.append(
                    f"-# Performance bonus: **+{bonus_pct * 100:.0f}%** "
                    f"(rounds / HP remaining / action variety)"
                )
            streak_bonus = float(getattr(res, "streak_bonus_applied", 0.0) or 0.0)
            if streak_bonus > 0:
                lines.append(
                    f"-# Streak bonus: **+{streak_bonus * 100:.0f}%** "
                    f"(carried into this win from your prior streak)"
                )
            mod_bonus = float(getattr(res, "modifier_reward_bonus", 0.0) or 0.0)
            if mod is not None and mod.key != "none" and mod_bonus > 0:
                lines.append(
                    f"-# Modifier bonus: {mod.emoji} **{mod.label}** "
                    f"`+{mod_bonus * 100:.0f}%`"
                )
            if is_boss:
                lines.append(
                    f"-# \U0001F47A Boss multiplier: "
                    f"`x{float(getattr(res, 'boss_payout_mult', 1.0) or 1.0):.0f}` "
                    f"on top of the standard arena math + flat boss cherry."
                )
            tier_mult = float(getattr(res, "tier_bud_mult_applied", 1.0) or 1.0)
            if tier_mult > 1.0:
                lines.append(
                    f"-# {tier_after.get('emoji') or ''} "
                    f"**{tier_after.get('label') or 'Bronze'}** tier "
                    f"multiplier: `x{tier_mult:.2f}`"
                )
            # Surface buddy XP credited for this win so the player sees
            # the same XP hit they'd get from a buddy-vs-buddy battle.
            if int(getattr(res, "buddy_xp_awarded", 0) or 0) > 0:
                fighter_id = getattr(res, "fighter_buddy_id", None)
                tag = f" (#{int(fighter_id)})" if fighter_id else ""
                lines.append(
                    f"\U0001F436 Your buddy{tag} earns "
                    f"**+{int(res.buddy_xp_awarded):,}** XP."
                )
            if getattr(res, "tier_promoted", False):
                lines.append(
                    f"\U0001F31F **Promoted to "
                    f"{tier_after.get('emoji') or ''} "
                    f"{tier_after.get('label') or '?'}!** "
                    f"(`x{float(tier_after.get('bud_mult') or 1.0):.2f}` "
                    f"on every future win)"
                )
            # Streak milestone surfaces a "you just hit X wins in a row!"
            # callout so the rare streak achievements feel weighty.
            milestone = getattr(res, "streak_milestone", None)
            if milestone:
                lines.append(
                    f"\U0001F525 **{int(milestone)}-win streak milestone!** "
                    f"Keep it alive."
                )
            if is_boss:
                lines.append(
                    f"-# Boss kills: **{getattr(res, 'new_arena_wins', '?')}** arena W "
                    f"-- next boss attempt unlocks in 24h."
                )
            else:
                lines.append(
                    f"-# Arena: **{res.new_arena_wins}** won / "
                    f"**{res.new_arena_losses}** lost  -  "
                    f"streak: **{int(getattr(res, 'streak_after', 0))}** "
                    f"(best **{int(getattr(res, 'best_streak_after', 0))}**)  -  "
                    f"total earned: "
                    f"**{fmt_token(to_human(int(res.new_total_bud_earned_raw)), 'BUD', bud_emoji)}**"
                )
            color = int(tier_after.get("color_hex") or C_GOLD)
            if is_boss:
                color = C_AMBER
            title = (
                f"\U0001F47A Boss Slain  -  "
                f"{tier_after.get('emoji') or ''} "
                f"{tier_after.get('label') or ''}" if is_boss else
                f"\U0001F3DF️ Arena Victory  -  "
                f"{tier_after.get('emoji') or ''} "
                f"{tier_after.get('label') or ''}"
            ).rstrip()
        elif res is not None:
            tier_after = res.tier_after or {}
            broke_streak = (
                not is_boss
                and int(getattr(res, "streak_before", 0) or 0) > 1
            )
            lines = [
                f"\U0001F480 **{boss_label}{species}** beat your buddy in "
                f"{rounds} rounds.",
                (
                    "Daily boss attempt is gone -- come back tomorrow."
                    if is_boss else
                    "No penalty -- come back stronger. Cooldown is on."
                ),
            ]
            if broke_streak:
                lines.append(
                    f"\U0001F494 Your **{int(res.streak_before)}**-win streak "
                    "is broken. (Best stays banked.)"
                )
            if is_boss:
                lines.append(
                    f"-# Boss W/L: "
                    f"**{int(getattr(res, 'new_arena_wins', 0))}** arena W "
                    f"-- next attempt in 24h."
                )
            else:
                lines.append(
                    f"-# Arena: **{res.new_arena_wins}** won / "
                    f"**{res.new_arena_losses}** lost  -  "
                    f"{tier_after.get('emoji') or ''} "
                    f"{tier_after.get('label') or 'Bronze'} tier  -  "
                    f"streak reset to **0** "
                    f"(best **{int(getattr(res, 'best_streak_after', 0))}**)."
                )
            color = C_AMBER
            title = "\U0001F47A Boss Defeat" if is_boss else "\U0001F4A8 Arena Defeat"
        else:
            lines = [
                f"\U00002694 Arena fight vs **{species}** ended in "
                f"{rounds} rounds.",
                "Could not persist the result -- counters not updated.",
            ]
            color = C_NEUTRAL
            title = "\U0001F3DF️ Arena"

        builder = card(title, color=color).description("\n".join(lines))
        if mod is not None and mod.key != "none":
            builder = builder.field(
                "Modifier",
                f"{mod.emoji} **{mod.label}** -- {mod.flavor}",
                False,
            )
        tail = [ln for ln in b.log_lines if ln.strip()][-12:]
        if tail:
            text = "\n".join(tail)
            if len(text) > 1020:
                text = text[-1020:]
            builder = builder.field("Battle Log", text, False)

        # Final scene PNG -- mirrors the post-fight visual every other
        # buddy battle ships so ,buddy arena gets a proper victory /
        # defeat screen instead of a text-only card.
        scene_file = None
        try:
            from services.buddy_battle_scene import (
                fighters_to_scene_state, render_battle_frame,
            )
            import io as _io
            banner = "VICTORY!" if won else "K.O."
            state = fighters_to_scene_state(
                b.player, b.enemy,
                round_num=int(b.round_num),
                max_rounds=_ARENA_BATTLE_MAX_ROUNDS,
                action_banner=banner,
                is_player_turn=False,
            )
            png = render_battle_frame(state)
            scene_file = discord.File(
                _io.BytesIO(png), filename="battle_final.png",
            )
            builder = builder.image("attachment://battle_final.png")
        except Exception:
            log.debug("buddy arena final scene render failed", exc_info=True)
        builder = builder.footer(f"Rounds played: {b.round_num}")
        return builder.build(), scene_file


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Buddy(bot))
