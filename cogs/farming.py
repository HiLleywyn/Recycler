"""cogs/farming.py -- Farming minigame commands.

Group: ,farm  (aliases: field, garden, crop, crops)

  ,farm              -- field view (plots, weather, zone, balances)
  ,farm plant <slot> <crop>
  ,farm water [slot]
  ,farm fertilize <slot|all>
  ,farm harvest [slot]
  ,farm zones / ,farm zone <key>
  ,farm crops
  ,farm shop / ,farm buy plot|fertilizer|seed ...
  ,farm equip <key|none>
  ,farm sell <crop|all>
  ,farm process <recipe>
  ,farm bag
  ,farm history
  ,farm forage           -- wander for randomized loot (10m cooldown)
  ,farm contract / ,farm contract turnin
  ,farm swap <amt|all>   (SEED -> HRV burn)
  ,farm stake / unstake / claim / cashout
  ,farm lb
  ,farm help

Heavy lifting lives in services.farming -- this module is presentation only.
"""
from __future__ import annotations

import asyncio
import datetime
import logging

import discord
from discord.ext import commands

import configs.farming_config as fc
from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.cooldowns import user_cooldown
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.persistent_embeds import BumpButton as _BumpButton, CallbackButton as _CallbackButton
from core.framework.quick_buy import QuickBuyView
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_AMBER, C_ERROR, C_GOLD, C_NAVY, C_NEUTRAL, C_SUCCESS,
    C_TEAL, C_WARNING, RARITY_COLORS, FormatKit,
    fmt_rel, fmt_token, fmt_usd,
)
from services import farming as farm_svc

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _fmt_hrv(amount: float) -> str:
    return fmt_token(amount, fc.HRV_SYMBOL, fc.HRV_EMOJI)


def _fmt_seed(amount: float) -> str:
    return fmt_token(amount, fc.SEED_SYMBOL, fc.SEED_EMOJI)


def _with_usd(amount: float, oracle: float) -> str:
    if amount <= 0 or oracle <= 0:
        return ""
    return f"  ~ **{fmt_usd(amount * oracle)}**"


async def _oracle_pair(ctx: DiscoContext) -> tuple[float, float]:
    """Return (hrv_oracle, seed_oracle) for ctx.guild_id."""
    hp = await ctx.db.get_price(fc.HRV_SYMBOL, ctx.guild_id)
    sp = await ctx.db.get_price(fc.SEED_SYMBOL, ctx.guild_id)
    return (float(hp["price"]) if hp else 0.0, float(sp["price"]) if sp else 0.0)


# ---------------------------------------------------------------------------
# Rarity colours -- single source of truth in constants/ui.py
# ---------------------------------------------------------------------------

_RARITY_COLOR = RARITY_COLORS


# ---------------------------------------------------------------------------
# Plot rendering helpers
# ---------------------------------------------------------------------------

def _time_remaining(ready_at_iso: str | None) -> str:
    if not ready_at_iso:
        return ""
    try:
        ready = datetime.datetime.fromisoformat(str(ready_at_iso).replace("Z", "+00:00"))
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        secs = int((ready - now).total_seconds())
        if secs <= 0:
            return "ready"
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return ""


def _plot_lines(plots: list[dict]) -> str:
    lines = []
    for p in plots:
        slot = int(p.get("slot", 0)) + 1
        state = str(p.get("state") or "empty")
        crop_key = p.get("crop_key")
        crop_meta = fc.crop_meta(crop_key) if crop_key else None
        emoji = (crop_meta or {}).get("emoji", "\U0001F7EB")
        name = (crop_meta or {}).get("name", "?")
        pest = p.get("pest_state")
        # Mutation badge: prefix the crop name so the player sees the
        # surprise the moment the plot view rebuilds. Mutation is set at
        # plant time and cleared at harvest, so it's only ever visible on
        # 'growing' / 'ready' rows.
        mut_meta = fc.mutation_meta(p.get("mutation"))
        mut_badge = f"{mut_meta['emoji']} **{mut_meta['name']}** " if mut_meta else ""

        if state == "empty":
            lines.append(f"`[{slot:>2}]` \U0001F7EB Empty")
        elif pest:
            pest_meta = fc.pest_meta(str(pest.get("key", ""))) or {}
            pe = pest_meta.get("emoji", "\U0001F41B")
            lines.append(f"`[{slot:>2}]` {pe} **PEST** on {mut_badge}{name}!")
        elif state == "growing":
            eta = _time_remaining(p.get("ready_at"))
            lines.append(f"`[{slot:>2}]` {emoji} {mut_badge}{name} - {eta}")
        elif state in ("ready", "ripe"):
            lines.append(f"`[{slot:>2}]` {emoji} {mut_badge}{name} - **RIPE!** \U00002705")
        elif state == "wilted":
            lines.append(f"`[{slot:>2}]` \U0001F343 {mut_badge}{name} - _wilted_")
        else:
            lines.append(f"`[{slot:>2}]` {emoji} {mut_badge}{name} - {state}")
    return "\n".join(lines) if lines else "_No plots yet._"


# ---------------------------------------------------------------------------
# Main field embed
# ---------------------------------------------------------------------------

def _field_embed(
    author: discord.Member,
    state: dict,
    weather: farm_svc.WeatherEvent,
    *,
    hrv_oracle: float,
    seed_oracle: float,
    bloomstone: dict | None = None,
    hrv_held: float = 0.0,
    seed_held: float = 0.0,
) -> discord.Embed:
    zone_key = str(state.get("current_zone") or fc.DEFAULT_ZONE)
    zone_meta = fc.zone_meta(zone_key) or {}
    w_meta = fc.weather_meta(weather.weather_key) or {}
    plot_tier = int(state.get("plot_tier") or 1)
    plot_meta = fc.plot_meta(plot_tier) or {}
    fert_key = state.get("equipped_fertilizer")
    fert_meta = fc.fertilizer_meta(fert_key) if fert_key else None

    farm_lvl = int(state.get("farm_level") or 1)
    farm_xp = float(state.get("farm_xp") or 0.0)
    lvl_into, lvl_total = fc.xp_to_next(farm_xp)
    lvl_pct = (lvl_into / lvl_total) if lvl_total > 0 else 1.0
    lvl_bar_len = 12
    filled = int(round(lvl_pct * lvl_bar_len))
    lvl_bar = "█" * filled + "░" * (lvl_bar_len - filled)
    lvl_pay_pct = (fc.level_payout_mult(farm_lvl) - 1.0) * 100

    frame_key = weather.weather_key if weather.weather_key in fc.FRAMES else "meadow_idle"
    frame = fc.FRAMES.get(frame_key, fc.FRAMES.get("meadow_idle", ""))

    plots = list(state.get("plots") or [])
    plot_text = _plot_lines(plots)

    fert_line = (
        f"{fert_meta['emoji']} {fert_meta['name']}"
        if fert_meta else "_None equipped_"
    )

    bm_cfg = Config.SHOP_ITEMS.get("bloomstone", {})
    bm_emoji = bm_cfg.get("emoji", "\U0001F33C")
    bm_name = bm_cfg.get("name", "Bloomstone")
    if bloomstone and bm_cfg:
        lvl = int(bloomstone.get("level") or 1)
        max_lvl = int(bm_cfg.get("max_level", 100))
        yield_pct = lvl * float(bm_cfg.get("stats", {}).get("farm_yield_bonus", 0.0)) * 100
        seed_pct = lvl * float(bm_cfg.get("stats", {}).get("farm_seed_drop_bonus", 0.0)) * 100
        bloom_line = (
            f"{bm_emoji} **Lv. {lvl}/{max_lvl}** "
            f"-- +{yield_pct:.1f}% yield · +{seed_pct:.1f}% SEED"
        )
    elif bm_cfg:
        bloom_line = f"_(no {bm_name} yet -- `,shop buy bloomstone`)_"
    else:
        bloom_line = ""

    if farm_lvl >= fc.FARM_MAX_LEVEL:
        lvl_value = (
            f"**Lv. {farm_lvl}** (max)\n"
            f"-# +{lvl_pay_pct:.0f}% HRV from crop sales"
        )
    else:
        lvl_value = (
            f"**Lv. {farm_lvl}**  `{lvl_bar}`  {int(lvl_into):,}/{int(lvl_total):,} XP\n"
            f"-# +{lvl_pay_pct:.0f}% HRV from crop sales"
        )

    b = (
        card(f"\U0001F33E {author.display_name}'s Farm", color=C_GOLD)
        .description(f"```\n{frame}\n```")
        .field(
            f"{zone_meta.get('emoji', '')} {zone_meta.get('name', zone_key)}  |  "
            f"{w_meta.get('emoji', '')} {w_meta.get('name', weather.weather_key)}",
            plot_text,
            False,
        )
        .field("\U0001F4D6 Farmer", lvl_value, False)
        .field(
            "\U0001F33E HRV",
            f"**{_fmt_hrv(hrv_held)}**{_with_usd(hrv_held, hrv_oracle)}",
            True,
        )
        .field(
            "\U0001F331 SEED",
            f"**{_fmt_seed(seed_held)}**{_with_usd(seed_held, seed_oracle)}",
            True,
        )
        .field(
            "\U0001F9EA Plot",
            f"Tier {plot_tier} - {plot_meta.get('name', '?')} ({len(plots)} slots)",
            True,
        )
        .field("\U0001F9F4 Fertilizer", fert_line, True)
    )
    if bloom_line:
        b = b.field("\U0001F33C Bloomstone", bloom_line, True)
    b = b.footer(f"Season: {fc.current_season().title()}  |  ,farm help for commands")
    return b.build()


# ---------------------------------------------------------------------------
# Zones embed
# ---------------------------------------------------------------------------

def _zones_embed() -> discord.Embed:
    b = card("\U0001F5FA Farming Zones", color=C_TEAL)
    lines = []
    for z in fc.ZONES.values():
        lines.append(
            f"{z['emoji']} **{z['name']}** (tier {z['zone_tier']}) - "
            f"Plot tier {z['plot_tier_required']}+ required\n"
            f"-# {z['blurb']}"
        )
    b.description("\n\n".join(lines))
    return b.build()


# ---------------------------------------------------------------------------
# Crops catalog embed (chunked to stay under 1024)
# ---------------------------------------------------------------------------

def _crops_embed() -> discord.Embed:
    b = card(
        "\U0001F33E Crop Catalog",
        description=(
            "**Seeds only** -- crops grow from seed packets, never bought pre-grown.\n"
            "Buy seeds: `,farm buy seed <key> <qty|all>`  -  "
            "Plant: `,farm plant <slot> <key>`"
        ),
        color=C_GOLD,
    )
    rarity_groups: dict[str, list[str]] = {
        "common": [], "uncommon": [], "rare": [], "epic": [], "legendary": [],
    }
    for c in fc.CROPS.values():
        r = str(c.get("rarity", "common"))
        secs = int(c.get("growth_seconds", 60))
        grow = f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h {(secs % 3600) // 60}m"
        rarity_groups.setdefault(r, []).append(
            f"{c['emoji']} **{c['name']}** (`{c['key']}`) - grow {grow}, "
            f"sell {_fmt_hrv(float(c['hrv_sell_price']))}"
        )
    for rarity, items in rarity_groups.items():
        if not items:
            continue
        label = rarity.title()
        text = "\n".join(items)
        if len(text) > 1020:
            text = text[:1017] + "..."
        b.field(label, text, False)
    return b.build()


# ---------------------------------------------------------------------------
# Shop embed
# ---------------------------------------------------------------------------

def _shop_embed(
    state: dict,
    *,
    hrv_balance: float = 0.0,
    hrv_oracle: float = 0.0,
) -> discord.Embed:
    plot_tier = int(state.get("plot_tier") or 1)
    next_tier = plot_tier + 1
    bal_line = f"You have **{_fmt_hrv(hrv_balance)}**"
    if hrv_oracle > 0 and hrv_balance > 0:
        bal_line += f" ≈ **{fmt_usd(hrv_balance * hrv_oracle)}**"
    bal_line += " to spend."
    b = card("\U0001F3EA Farm Shop", color=C_AMBER).description(bal_line)

    # Plot upgrade
    next_plot = fc.plot_meta(next_tier)
    if next_plot:
        b.field(
            "\U0001F7EB Plot Upgrade",
            f"**{next_plot['name']}** (tier {next_tier}) - "
            f"{_fmt_hrv(next_plot['price_hrv'])}\n"
            f"-# {next_plot['slots']} slots, {next_plot['blurb']}\n"
            f"-# `,farm buy plot`",
            False,
        )
    else:
        b.field("\U0001F7EB Plot", "_Max tier reached._", False)

    # Fertilizers
    fert_lines = []
    for f in fc.FERTILIZERS.values():
        fert_lines.append(
            f"{f['emoji']} **{f['name']}** - {_fmt_hrv(f['price_hrv'])} ea  "
            f"yield x{f['yield_mult']:.2f}"
        )
    text = "\n".join(fert_lines)
    if len(text) > 1020:
        text = text[:1017] + "..."
    b.field("\U0001F9F4 Fertilizer  `,farm buy fertilizer <key> <qty>`", text, False)

    # Seed packets -- chunk by rarity so the field stays readable as the
    # crop catalog grows. Seed price is ``crop.hrv_sell_price * 0.20``
    # (matches services.farming.buy_seed_packet); render that here so
    # players see the per-packet cost without bouncing to ,farm crops.
    seed_groups: dict[str, list[str]] = {
        "common": [], "uncommon": [], "rare": [], "epic": [], "legendary": [],
    }
    for c in fc.CROPS.values():
        rarity = str(c.get("rarity", "common"))
        seed_each = float(c.get("hrv_sell_price", 0.0)) * 0.20
        seed_groups.setdefault(rarity, []).append(
            f"{c['emoji']} **{c['name']}** (`{c['key']}`) -- "
            f"{_fmt_hrv(seed_each)} ea"
        )
    seed_label = "\U0001F331 Seed Packets  `,farm buy seed <key> <qty|all>`"
    seed_lines: list[str] = []
    for rarity in ("common", "uncommon", "rare", "epic", "legendary"):
        items = seed_groups.get(rarity) or []
        if not items:
            continue
        seed_lines.append(f"__{rarity.title()}__: " + "  ·  ".join(items))
    if seed_lines:
        # Chunk into 1024-char fields so a packed catalog doesn't 400.
        chunk: list[str] = []
        chunk_len = 0
        first = True
        for ln in seed_lines:
            ln_len = len(ln) + 1
            if chunk and chunk_len + ln_len > 1000:
                b.field(
                    seed_label if first else f"{seed_label} (cont.)",
                    "\n".join(chunk), False,
                )
                first = False
                chunk = [ln]
                chunk_len = ln_len
            else:
                chunk.append(ln)
                chunk_len += ln_len
        if chunk:
            b.field(
                seed_label if first else f"{seed_label} (cont.)",
                "\n".join(chunk), False,
            )

    b.footer("Seed packets: ,farm buy seed <crop> <qty>  |  ,farm crops for full details")
    return b.build()


# ---------------------------------------------------------------------------
# Bag / inventory embed
# ---------------------------------------------------------------------------

def _bag_embed(author: discord.Member, summary: dict) -> discord.Embed:
    b = card(f"\U0001F392 {author.display_name}'s Farm Bag", color=C_NAVY)

    crops = summary.get("crops") or []
    if crops:
        lines = [
            f"{c['emoji']} **{c['name']}** x{c['count']}"
            f"  _(sell: {_fmt_hrv(c['hrv_each'])} ea)_"
            for c in crops
        ]
        text = "\n".join(lines)
        if len(text) > 1020:
            text = text[:1017] + "..."
        b.field(f"\U0001F33E Crops ({summary.get('crops_total', 0)})", text, False)
    else:
        b.field("\U0001F33E Crops", "_None_", False)

    processed = summary.get("processed") or []
    if processed:
        lines = [
            f"{p['emoji']} **{p['name']}** x{p['count']}"
            for p in processed
        ]
        b.field("\U0001F372 Processed", "\n".join(lines), False)

    ferts = summary.get("fertilizer") or []
    if ferts:
        lines = [f"{f['emoji']} **{f['name']}** x{f['count']}" for f in ferts]
        b.field("\U0001F9F4 Fertilizer", "\n".join(lines), False)

    packets = summary.get("seed_packets") or []
    if packets:
        lines = [f"{p['emoji']} **{p['name']}** x{p['count']}" for p in packets]
        text = "\n".join(lines)
        if len(text) > 1020:
            text = text[:1017] + "..."
        b.field("\U0001F331 Seed Packets", text, False)

    return b.build()


# ---------------------------------------------------------------------------
# History embed
# ---------------------------------------------------------------------------

def _history_embed(author: discord.Member, rows: list[dict]) -> discord.Embed:
    b = card(f"\U0001F4DC {author.display_name}'s Harvest History", color=C_NAVY)
    if not rows:
        b.description("_No harvests yet._")
        return b.build()
    lines = []
    for r in rows:
        crop_key = str(r.get("crop_key") or "?")
        meta = fc.crop_meta(crop_key) or {}
        emoji = meta.get("emoji", "\U0001F33E")
        name = meta.get("name", crop_key.title())
        qty = int(r.get("qty") or 0)
        seed_raw = int(r.get("seed_earned_raw") or 0)
        seed_human = to_human(seed_raw)
        ts = r.get("harvested_at")
        ts_str = fmt_rel(ts, fallback="") if ts is not None else ""
        lines.append(
            f"{emoji} **{name}** x{qty} - {_fmt_seed(seed_human)} SEED  {ts_str}"
        )
    b.description("\n".join(lines))
    return b.build()


# ---------------------------------------------------------------------------
# Daily contract embed
# ---------------------------------------------------------------------------

def _contract_embed(
    author: discord.Member, view: farm_svc.ContractView,
) -> discord.Embed:
    c = view.contract
    crop_key = str(c.get("crop_key") or "")
    meta = fc.crop_meta(crop_key) or {}
    rarity = str(c.get("rarity") or "common")
    color = _RARITY_COLOR.get(rarity, C_NAVY)
    qty_required = int(c.get("qty_required") or 0)
    qty_delivered = int(c.get("qty_delivered") or 0)
    completed = bool(c.get("completed"))
    hrv_reward = float(c.get("hrv_reward_human") or 0.0)
    seed_reward = float(c.get("seed_reward_human") or 0.0)
    title_emoji = "\U0001F4E6"
    title = f"{title_emoji} Daily Contract"
    if completed:
        title += " - Complete!"
    elif view.fresh_today:
        title += " - New today!"
    b = card(title, color=color)
    b.field(
        "Order",
        f"{meta.get('emoji', '')} **{meta.get('name', crop_key.title())}** "
        f"({rarity.title()}) x{qty_required}",
        inline=False,
    )
    progress_bar = FormatKit.bar(qty_delivered, qty_required, width=12)
    b.field(
        "Progress",
        f"{progress_bar} {qty_delivered}/{qty_required}\n"
        f"In your bag: **{view.have}**",
        inline=False,
    )
    b.field(
        "Reward",
        f"{_fmt_hrv(hrv_reward)}"
        + (f"\n{_fmt_seed(seed_reward)} SEED" if seed_reward > 0 else ""),
        inline=False,
    )
    if completed:
        b.footer("Next contract rolls at UTC midnight.")
    elif view.can_turn_in:
        b.footer("Run `,farm contract turnin` to deliver what's in your bag.")
    else:
        b.footer("Harvest the listed crop, then `,farm contract turnin`.")
    return b.build()


# ---------------------------------------------------------------------------
# Leaderboard embed
# ---------------------------------------------------------------------------

def _lb_embed(rows: list[dict], title: str) -> discord.Embed:
    b = card(title, color=C_GOLD)
    if not rows:
        b.description("_No data yet._")
        return b.build()
    lines = []
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        uid = r.get("user_id")
        # The board sorts by total_hrv_earned_raw, so the headline number must
        # match: convert the raw NUMERIC(36,0) column to human scale and show
        # harvest count + biggest qty as supporting detail. get_top_farmers
        # returns plain dicts (PgRow is stripped at the service boundary), so
        # PgRow.h() is not available here -- use to_human() directly.
        hrv_h = to_human(int(r.get("total_hrv_earned_raw") or 0))
        harvests = int(r.get("total_harvested") or 0)
        biggest = int(r.get("biggest_harvest_qty") or 0)
        detail = f"{harvests:,} harvests" + (f" · biggest {biggest:,}" if biggest else "")
        lines.append(
            f"{medal} <@{uid}> -- {_fmt_hrv(hrv_h)} -# {detail}"
        )
    b.description("\n".join(lines))
    return b.build()


# ---------------------------------------------------------------------------
# Field view (interactive farming panel)
# ---------------------------------------------------------------------------

class _PlotTargetSelect(discord.ui.Select):
    """Pick which empty plot the next seed plant lands in.

    Defaults to "next empty" so the seed dropdown still works in one
    click when the player doesn't care which slot. Picking a specific
    plot stashes the choice on the parent FarmFieldView; the seed
    dropdown then plants into that plot on next use.
    """

    def __init__(self, plots: list[dict], chosen: int | None) -> None:
        opts: list[discord.SelectOption] = []
        empty_slot_default = next(
            (i for i, p in enumerate(plots)
             if str(p.get("state") or "") in ("empty", "")),
            None,
        )
        opts.append(discord.SelectOption(
            label=(
                "Next empty plot"
                + (
                    f" (#{empty_slot_default + 1})"
                    if empty_slot_default is not None else " (none free)"
                )
            ),
            value="__auto__",
            emoji="\U0001F500",
            default=(chosen is None),
        ))
        for i, p in enumerate(plots):
            state_s = str(p.get("state") or "").lower()
            crop = p.get("crop_key") or ""
            if state_s in ("empty", ""):
                desc = "empty"
                emoji = "\U0001F7EB"
            else:
                meta = fc.crop_meta(crop) if crop else None
                desc = (
                    f"{state_s}"
                    + (f" -- {(meta or {}).get('name', crop)}" if crop else "")
                )
                emoji = (
                    str((meta or {}).get("emoji") or "")[:1] or "\U0001F33E"
                )
            opts.append(discord.SelectOption(
                label=f"Plot #{i + 1}",
                value=str(i),
                description=desc[:100],
                emoji=emoji,
                default=(chosen == i),
            ))
        super().__init__(
            placeholder=(
                f"Plant target: Plot #{chosen + 1}"
                if chosen is not None else "Plant target: next empty plot"
            ),
            options=opts[:25],
            min_values=1, max_values=1,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "FarmFieldView" = self.view  # type: ignore
        choice = self.values[0]
        if choice == "__auto__":
            view._plant_target_slot = None
        else:
            try:
                view._plant_target_slot = int(choice)
            except ValueError:
                view._plant_target_slot = None
        # Rebuild both selects so the placeholder text updates and
        # the seed dropdown reflects whether the chosen plot is OK.
        await view._refresh_seed_select()
        if view.message:
            try:
                await interaction.response.edit_message(view=view)
            except Exception:
                await interaction.response.defer()


class _PlantSeedSelect(discord.ui.Select):
    """Dropdown of seed packets the player owns. Selecting a row
    plants ONE of that seed into the next empty plot via
    ``farm_svc.plant_seed``. Rebuilt every refresh so packet counts
    stay live.
    """

    def __init__(
        self, packets: dict, plots: list[dict],
        target_slot: int | None,
    ) -> None:
        opts: list[discord.SelectOption] = []
        for k, v in sorted(
            packets.items(), key=lambda kv: -int(kv[1] or 0),
        ):
            try:
                cnt = int(v or 0)
            except (TypeError, ValueError):
                cnt = 0
            if cnt <= 0:
                continue
            meta = fc.crop_meta(k) or {}
            label = f"{meta.get('name', k)} (x{cnt})"[:100]
            opts.append(discord.SelectOption(
                label=label,
                value=str(k),
                emoji=str(meta.get("emoji") or "")[:1] or None,
            ))
        # Target plot resolution: explicit pick wins, else first empty.
        if target_slot is not None and 0 <= target_slot < len(plots):
            slot_state = str(
                plots[target_slot].get("state") or ""
            ).lower()
            if slot_state in ("empty", ""):
                resolved_slot: int | None = int(target_slot)
            else:
                resolved_slot = None
        else:
            resolved_slot = next(
                (i for i, p in enumerate(plots)
                 if str(p.get("state") or "") in ("empty", "")),
                None,
            )
        disabled = (not opts) or resolved_slot is None
        if not opts:
            opts = [discord.SelectOption(
                label="(no seed packets)",
                value="__empty__", default=True,
            )]
        # Placeholder explains target choice.
        if resolved_slot is None:
            ph = (
                "Pick a different target -- chosen plot isn't empty"
                if target_slot is not None
                else "No empty plot to plant in"
            )
        elif target_slot is not None:
            ph = f"Plant seed into Plot #{resolved_slot + 1}..."
        else:
            ph = (
                f"Plant seed (next empty: Plot #{resolved_slot + 1})..."
            )
        super().__init__(
            placeholder=ph,
            options=opts[:25],
            min_values=1, max_values=1,
            row=2,
            disabled=disabled,
        )
        self._empty_slot = resolved_slot

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "FarmFieldView" = self.view  # type: ignore
        choice = self.values[0]
        if choice == "__empty__" or self._empty_slot is None:
            await interaction.response.send_message(
                "No seed / no empty plot.", ephemeral=True,
            )
            return
        try:
            res = await farm_svc.plant_seed(
                view.cog.bot.db,
                view.ctx.guild_id, interaction.user.id,
                int(self._empty_slot), str(choice),
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            log.exception(
                "farm view Plant click failed crop=%s slot=%s",
                choice, self._empty_slot,
            )
            await interaction.response.send_message(
                f"Plant failed: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        # Clear the explicit target so the next plant defaults to
        # next-empty again -- the user already used the chosen slot.
        view._plant_target_slot = None
        await view._on_refresh(interaction)


class FarmFieldView(discord.ui.View):
    """Harvest All / Water All / Sell All / Refresh buttons on the ,farm embed."""

    def __init__(self, cog: "Farming", ctx: DiscoContext) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.message: discord.Message | None = None

        self.add_item(_CallbackButton(
            self.owner_id, self._on_harvest_all,
            label="Harvest All", emoji="\U0001F33E",
            style=discord.ButtonStyle.success, row=0,
        ))
        self.add_item(_CallbackButton(
            self.owner_id, self._on_water_all,
            label="Water All", emoji="\U0001F4A7",
            style=discord.ButtonStyle.primary, row=0,
        ))
        self.add_item(_CallbackButton(
            self.owner_id, self._on_sell_all,
            label="Sell All", emoji="\U0001F4B0",
            style=discord.ButtonStyle.secondary, row=0,
        ))
        self.add_item(_CallbackButton(
            self.owner_id, self._on_plant_all,
            label="Plant All", emoji="\U0001F331",
            style=discord.ButtonStyle.success, row=1,
        ))
        self.add_item(_CallbackButton(
            self.owner_id, self._on_fertilize_all,
            label="Fertilize All", emoji="\U0001F9F4",
            style=discord.ButtonStyle.primary, row=1,
        ))
        # Refresh + Bump live alone on the bottom row, never sharing a
        # row with action buttons. Convention enforced across cogs so
        # the player always knows where these controls land.
        self.add_item(_CallbackButton(
            self.owner_id, self._on_refresh,
            label="Refresh", emoji="\U0001F504",
            style=discord.ButtonStyle.secondary, row=4,
        ))
        self.add_item(_BumpButton(self.owner_id, row=4))
        # Seed-pick (row 2) + plot-target (row 3) dropdowns. Populated
        # dynamically in _refresh_seed_select() called from every
        # refresh path. self._plant_target_slot is None = "next empty
        # plot" (default); explicit pick stashes the slot index so
        # the seed dropdown plants into THAT plot on next use.
        self._seed_select_attached = False
        self._plant_target_slot: int | None = None

    async def _rebuild_embed(self) -> discord.Embed:
        db = self.cog.bot.db
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        state = await farm_svc.ensure_state(db, gid, uid)
        weather = await farm_svc.get_or_roll_weather(db, gid, uid)
        hp = await db.get_price(fc.HRV_SYMBOL, gid)
        sp = await db.get_price(fc.SEED_SYMBOL, gid)
        hrv_oracle = float(hp["price"]) if hp else 0.0
        seed_oracle = float(sp["price"]) if sp else 0.0
        bloomstone = await db.get_bloomstone(uid, gid)
        hrv_wh = await db.get_wallet_holding(
            uid, gid, fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
        )
        seed_wh = await db.get_wallet_holding(
            uid, gid, fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
        )
        hrv_held = to_human(int(hrv_wh["amount"]) if hrv_wh else 0)
        seed_held = to_human(int(seed_wh["amount"]) if seed_wh else 0)
        return _field_embed(
            self.ctx.author, state, weather,
            hrv_oracle=hrv_oracle, seed_oracle=seed_oracle,
            bloomstone=bloomstone, hrv_held=hrv_held, seed_held=seed_held,
        )

    async def _on_harvest_all(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        db = self.cog.bot.db
        async with self.cog._plot_lock(uid, gid):
            state = await farm_svc.ensure_state(db, gid, uid)
            plots = list(state.get("plots") or [])
            now = datetime.datetime.now(tz=datetime.timezone.utc)
            results = []
            for p in plots:
                if p.get("state") not in ("growing", "ready", "ripe"):
                    continue
                ready_iso = p.get("ready_at")
                if ready_iso:
                    try:
                        ready_at = datetime.datetime.fromisoformat(str(ready_iso))
                        if ready_at.tzinfo is None:
                            ready_at = ready_at.replace(tzinfo=datetime.timezone.utc)
                    except ValueError:
                        ready_at = None
                    if ready_at and now < ready_at:
                        continue
                try:
                    r = await farm_svc.harvest_plot(db, gid, uid, int(p["slot"]))
                    results.append(r)
                except Exception:
                    pass
        if results:
            total_seed = sum(to_human(int(r.seed_raw)) for r in results)
            lines = []
            mutated = 0
            seed_returns: dict[str, int] = {}
            for r in results:
                m = fc.crop_meta(r.crop_key) or {}
                mm = fc.mutation_meta(r.mutation)
                rarity_tag = f" `{r.rarity.title()}`"
                if mm:
                    mutated += 1
                    lines.append(
                        f"{mm['emoji']} **{mm['name']}** {m.get('emoji', '')} "
                        f"{m.get('name', r.crop_key)} x{r.qty}{rarity_tag}"
                    )
                else:
                    lines.append(
                        f"{m.get('emoji', '')} **{m.get('name', r.crop_key)}** "
                        f"x{r.qty}{rarity_tag}"
                    )
                if int(r.seed_packets_returned or 0) > 0:
                    seed_returns[r.crop_key] = (
                        seed_returns.get(r.crop_key, 0)
                        + int(r.seed_packets_returned)
                    )
            tail = f"\n+{_fmt_seed(total_seed)} SEED"
            if mutated:
                tail += f"\n\U00002728 {mutated} mutation{'s' if mutated != 1 else ''}!"
            if seed_returns:
                bits = []
                for k, n in seed_returns.items():
                    cm = fc.crop_meta(k) or {}
                    bits.append(f"**{n}x** {cm.get('name', k)}")
                tail += "\n\U0001F331 Seeds returned: " + ", ".join(bits)
            await interaction.followup.send(
                f"\U0001F33E Harvested **{len(results)}** plot(s)!\n"
                + "\n".join(lines)
                + tail,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "No ripe plots to harvest right now.", ephemeral=True,
            )
        try:
            embed = await self._rebuild_embed()
            if self.message:
                await self.message.edit(embed=embed, view=self)
        except Exception:
            log.debug("FarmFieldView: harvest_all refresh failed", exc_info=True)

    async def _on_water_all(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        db = self.cog.bot.db
        async with self.cog._plot_lock(uid, gid):
            state = await farm_svc.ensure_state(db, gid, uid)
            plots = list(state.get("plots") or [])
            watered = 0
            for p in plots:
                if p.get("state") in ("growing", "ready"):
                    try:
                        res = await farm_svc.water_plot(db, gid, uid, int(p["slot"]))
                        if res.ok:
                            watered += 1
                    except Exception:
                        pass
        if watered:
            await interaction.followup.send(
                f"\U0001F4A7 Watered **{watered}** plot(s).", ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "No growing plots to water right now.", ephemeral=True,
            )
        try:
            embed = await self._rebuild_embed()
            if self.message:
                await self.message.edit(embed=embed, view=self)
        except Exception:
            log.debug("FarmFieldView: water_all refresh failed", exc_info=True)

    async def _on_sell_all(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        db = self.cog.bot.db
        hp = await db.get_price(fc.HRV_SYMBOL, gid)
        hrv_oracle = float(hp["price"]) if hp else 0.0
        try:
            res = await farm_svc.sell_crop(db, gid, uid, "all")
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        hrv_human = to_human(int(res.hrv_received_raw))
        await interaction.followup.send(
            f"\U0001F4B0 Sold **{res.qty_sold}** crop(s) for "
            f"**{_fmt_hrv(hrv_human)}**{_with_usd(hrv_human, hrv_oracle)}\n"
            f"-# Slippage: {res.slippage_pct * 100:.2f}%",
            ephemeral=True,
        )
        try:
            embed = await self._rebuild_embed()
            if self.message:
                await self.message.edit(embed=embed, view=self)
        except Exception:
            log.debug("FarmFieldView: sell_all refresh failed", exc_info=True)

    async def _on_fertilize_all(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        db = self.cog.bot.db
        try:
            async with self.cog._plot_lock(uid, gid):
                applied, fert_key = await farm_svc.apply_fertilizer_all(db, gid, uid)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        fmeta = fc.fertilizer_meta(fert_key) or {}
        if not applied:
            await interaction.followup.send(
                "No growing plots needed fertilizer right now.", ephemeral=True,
            )
        else:
            slot_list = ", ".join(f"#{n}" for n in applied)
            await interaction.followup.send(
                f"{fmeta.get('emoji', '')} **{fmeta.get('name', fert_key)}** applied to "
                f"**{len(applied)}** plot(s): {slot_list}\n"
                f"-# Yield x{float(fmeta.get('yield_mult', 1.0)):.2f}, "
                f"growth x{float(fmeta.get('growth_mult', 1.0)):.2f}",
                ephemeral=True,
            )
        try:
            embed = await self._rebuild_embed()
            if self.message:
                await self.message.edit(embed=embed, view=self)
        except Exception:
            log.debug("FarmFieldView: fertilize_all refresh failed", exc_info=True)

    async def _refresh_seed_select(self) -> None:
        """Drop + re-add the seed select with current packet counts +
        empty-plot detection. Selects can't have their options mutated
        after construction, so we rebuild on every refresh.
        """
        db = self.cog.bot.db
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        state = await farm_svc.ensure_state(db, gid, uid)
        packets_raw = state.get("seed_packets") or {} if state else {}
        if isinstance(packets_raw, str):
            try:
                import json as _json
                packets = _json.loads(packets_raw) or {}
            except Exception:
                packets = {}
        elif isinstance(packets_raw, dict):
            packets = packets_raw
        else:
            packets = {}
        plots = list(state.get("plots") or [])
        for child in list(self.children):
            if isinstance(child, (_PlantSeedSelect, _PlotTargetSelect)):
                self.remove_item(child)
        self.add_item(_PlantSeedSelect(
            packets, plots, self._plant_target_slot,
        ))
        self.add_item(_PlotTargetSelect(plots, self._plant_target_slot))
        self._seed_select_attached = True

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            await self._refresh_seed_select()
            embed = await self._rebuild_embed()
            if self.message:
                await self.message.edit(embed=embed, view=self)
        except Exception:
            log.debug("FarmFieldView: refresh failed", exc_info=True)

    async def _on_plant_all(self, interaction: discord.Interaction) -> None:
        """Plant the player's "best" seed packet into every empty plot.

        Best = highest-rarity packet first (legendary -> common); ties
        break alphabetically. Stops when seeds run out OR every plot is
        filled. Single transaction-per-plot (delegated to
        ``farm_svc.plant_seed`` which atomically pops the packet) so a
        partial run still records the plots that did get planted.
        Posts an ephemeral receipt summarising what got planted.
        """
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        db = self.cog.bot.db
        # Rarity order for the "best seed first" pick.
        rarity_rank = {
            "legendary": 0, "epic": 1, "rare": 2,
            "uncommon": 3, "common": 4,
        }

        async with self.cog._plot_lock(uid, gid):
            state = await farm_svc.ensure_state(db, gid, uid)
            packets_raw = state.get("seed_packets") or {}
            if isinstance(packets_raw, str):
                try:
                    import json as _json
                    packets = _json.loads(packets_raw) or {}
                except Exception:
                    packets = {}
            elif isinstance(packets_raw, dict):
                packets = dict(packets_raw)
            else:
                packets = {}
            plots = list(state.get("plots") or [])
            empty_slots = [
                i for i, p in enumerate(plots)
                if str(p.get("state") or "") in ("empty", "")
            ]
            if not empty_slots:
                try:
                    await interaction.followup.send(
                        "No empty plots -- harvest or clear something first.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return
            usable = []
            for k, n in packets.items():
                try:
                    cnt = int(n or 0)
                except (TypeError, ValueError):
                    cnt = 0
                if cnt <= 0:
                    continue
                meta = fc.crop_meta(k) or {}
                rar = str(meta.get("rarity") or "common").lower()
                usable.append((rarity_rank.get(rar, 5), k, cnt))
            if not usable:
                try:
                    await interaction.followup.send(
                        "No seed packets in your bag -- buy some via "
                        "`,farm shop` first.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return
            usable.sort(key=lambda t: (t[0], t[1]))
            # Walk best-first, plant into each empty slot in turn.
            planted: list[tuple[int, str]] = []
            errors: list[str] = []
            slot_iter = iter(empty_slots)
            cur_packet: tuple[str, int] | None = None
            packet_idx = 0
            for slot in slot_iter:
                # Find the next packet with stock left.
                while True:
                    if cur_packet is None or cur_packet[1] <= 0:
                        if packet_idx >= len(usable):
                            cur_packet = None
                            break
                        _, k, n = usable[packet_idx]
                        packet_idx += 1
                        cur_packet = (k, n)
                    if cur_packet[1] > 0:
                        break
                if cur_packet is None:
                    break
                key = cur_packet[0]
                try:
                    await farm_svc.plant_seed(db, gid, uid, slot, key)
                    planted.append((slot, key))
                    cur_packet = (cur_packet[0], cur_packet[1] - 1)
                except ValueError as e:
                    errors.append(f"plot #{slot + 1}: {e}")
                    continue
                except Exception:
                    log.exception(
                        "plant_all: plot=%s key=%s failed", slot, key,
                    )
                    errors.append(f"plot #{slot + 1}: internal error")
                    continue

        # Re-render the panel with the new plot states.
        try:
            await self._refresh_seed_select()
            embed = await self._rebuild_embed()
            if self.message:
                await self.message.edit(embed=embed, view=self)
        except Exception:
            log.debug("FarmFieldView: plant-all refresh failed",
                      exc_info=True)

        if not planted:
            err_part = (
                f"\nErrors: {'; '.join(errors[:3])}"
                if errors else ""
            )
            try:
                await interaction.followup.send(
                    f"Nothing planted.{err_part}", ephemeral=True,
                )
            except Exception:
                pass
            return

        # Group planted by crop key for the receipt.
        from collections import Counter as _Counter
        by_key: _Counter[str] = _Counter()
        for _slot, k in planted:
            by_key[k] += 1
        lines = []
        for k, n in by_key.most_common():
            meta = fc.crop_meta(k) or {}
            emoji = str(meta.get("emoji") or "")
            name = str(meta.get("name") or k.title())
            lines.append(f"{emoji} **{name}** x{n}")
        err_part = (
            f"\n-# {len(errors)} slot(s) errored"
            if errors else ""
        )
        try:
            await interaction.followup.send(
                f"\U0001F331 Planted **{len(planted)}** seed(s):\n"
                + "\n".join(lines)
                + err_part,
                ephemeral=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pest battle view
# ---------------------------------------------------------------------------

class PestBattleView(discord.ui.View):
    def __init__(self, cog: "Farming", ctx: DiscoContext, plot_slot: int) -> None:
        super().__init__(timeout=fc.SESSION_TIMEOUT_S)
        self.cog = cog
        self.ctx = ctx
        self.plot_slot = plot_slot

    async def _resolve(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Not your farm.", ephemeral=True)
            return
        # Capture the message reference so the per-move burst can edit
        # it directly (interaction.response.edit_message can only fire
        # once per interaction, so the burst has to ride self.message).
        if getattr(self, "message", None) is None:
            self.message = interaction.message
        try:
            res = await farm_svc.resolve_pest_battle(
                self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
                self.plot_slot, action,
            )
        except ValueError as exc:
            await interaction.response.edit_message(
                content=str(exc), embed=None, view=None,
            )
            return

        # Per-move animation burst BEFORE the panel re-renders. Only the
        # "attack" action plays a strike burst; capture/flee don't have
        # an attack visual to drive.
        if action == "attack" and getattr(self, "message", None) is not None:
            try:
                # Build synthetic fighters matching the embed below.
                pest_snap = res.pest_state or {}
                pest_key = str(pest_snap.get("key") or res.pest_key or "?")
                pest_meta_local = fc.pest_meta(pest_key) or {}
                pest_tier = int(
                    pest_snap.get("tier") or pest_meta_local.get("tier", 1) or 1
                )
                pest_name = pest_meta_local.get("name", pest_key.title())
                pest_hp = int(pest_snap.get("hp", 0))
                pest_max_hp = int(pest_snap.get("max_hp", max(1, pest_hp)))

                class _Syn:
                    def __init__(self, name, level, tier, hp, max_hp, species):
                        self.name = name; self.level = level; self.tier = tier
                        self.hp = hp; self.max_hp = max_hp; self.species = species
                        self.id = 0; self.boss_zone_id = ""

                player_syn = _Syn("You (Farmer)", 1, 1, 100, 100, "default")
                pest_syn = _Syn(
                    pest_name, pest_tier, pest_tier,
                    pest_hp, max(1, pest_max_hp), "default",
                )

                from services.buddy_battle_scene import play_battle_action_burst
                await play_battle_action_burst(
                    self, player_syn, pest_syn,
                    actor_side="p1",
                    action="strike",
                    round_num=int(pest_snap.get("round") or 1),
                    max_rounds=15,
                )
            except Exception:
                log.debug("pest battle: action burst failed", exc_info=True)

        log_text = "\n".join(res.log) if res.log else ""
        outcome = res.outcome

        if outcome == "pest_dead":
            seed_human = to_human(int(res.seed_drop_raw))
            desc = (
                f"```\n{fc.FRAMES.get('victory', '')}\n```\n"
                f"\U0001F3C6 Pest defeated! "
                + (f"Captured as a buddy!" if res.captured else "")
                + (f"\n+{_fmt_seed(seed_human)} SEED dropped." if seed_human > 0 else "")
                + (f"\n{log_text}" if log_text else "")
            )
            embed = card("\U0001F3C6 Victory!", color=C_SUCCESS).description(desc).build()
            self.stop()
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=None)
            elif self.message is not None:
                await self.message.edit(embed=embed, view=None)

        elif outcome == "player_fled":
            embed = card("\U0001F3C3 Fled!", color=C_WARNING).description(
                f"You ran away.\n{log_text}"
            ).build()
            self.stop()
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=None)
            elif self.message is not None:
                await self.message.edit(embed=embed, view=None)

        elif outcome == "player_killed":
            embed = card("\U0001F480 Defeated", color=C_ERROR).description(
                f"The pest overcame you. Plot lost.\n{log_text}"
            ).build()
            self.stop()
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=None)
            elif self.message is not None:
                await self.message.edit(embed=embed, view=None)

        else:
            pest = res.pest_state or {}
            hp = int(pest.get("hp", 0))
            max_hp = int(pest.get("max_hp", hp))
            bar = FormatKit.bar(hp, max(max_hp, 1), width=8, show_pct=False)
            pest_key = str(pest.get("key", res.pest_key or "?"))
            pest_meta = fc.pest_meta(pest_key) or {}
            emoji = pest_meta.get("emoji", "\U0001F41B")
            name = pest_meta.get("name", pest_key.title())
            pest_tier = int(pest.get("tier") or pest_meta.get("tier", 1) or 1)
            _TIER_NAMES = {
                1: "Common", 2: "Uncommon", 3: "Rare",
                4: "Epic", 5: "Legendary",
            }
            rarity_word = _TIER_NAMES.get(pest_tier, "Common")
            stars = "\U00002B50" * max(1, min(5, pest_tier))
            desc = (
                f"\U0001F33E **You** (Farmer)\n"
                f"vs\n"
                f"{emoji} **{name}** -- Lv {pest_tier} {rarity_word} {stars}\n"
                f"HP: `{bar}` {hp}/{max_hp}"
                + (f"\n\n{log_text}" if log_text else "")
            )

            # Battle scene PNG -- synthetic fighters so the farm pest
            # fight uses the same Pokemon-Stadium-style visual as every
            # other buddy battle in the game.
            scene_file = None
            try:
                from services.buddy_battle_scene import (
                    fighters_to_scene_state, render_battle_frame,
                )
                import io as _io

                class _PestSyn:
                    def __init__(self, name, level, tier, hp, max_hp, species):
                        self.name = name
                        self.level = level
                        self.tier = tier
                        self.hp = hp
                        self.max_hp = max_hp
                        self.species = species
                        self.id = 0
                        self.boss_zone_id = ""

                # Farming pest combat doesn't track player HP -- the
                # player always survives unless they fail to flee. Show
                # a full HP bar on the player side so the scene reads
                # cleanly and the focus stays on the pest's bar.
                player_f = _PestSyn(
                    "You (Farmer)", 1, 1,
                    100, 100, "default",
                )
                pest_f = _PestSyn(
                    name, pest_tier, pest_tier,
                    hp, max(1, max_hp),
                    "default",
                )
                state = fighters_to_scene_state(
                    player_f, pest_f,
                    round_num=int(pest.get("round") or 1),
                    max_rounds=15,
                    action_banner="",
                    is_player_turn=True,
                )
                png = render_battle_frame(state)
                scene_file = discord.File(_io.BytesIO(png), filename="battle.png")
            except Exception:
                log.debug("pest battle: scene render failed", exc_info=True)

            builder = card(
                f"{emoji} Pest Battle!", color=C_ERROR,
            ).description(desc)
            if scene_file is not None:
                builder = builder.image("attachment://battle.png")
            embed = builder.build()
            _kw: dict = {"embed": embed, "view": self}
            if scene_file is not None:
                _kw["attachments"] = [scene_file]
            if not interaction.response.is_done():
                await interaction.response.edit_message(**_kw)
            elif self.message is not None:
                await self.message.edit(**_kw)

    @discord.ui.button(label="Strike", style=discord.ButtonStyle.red, emoji="⚔️")
    async def btn_attack(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._resolve(interaction, "attack")

    @discord.ui.button(label="Capture", style=discord.ButtonStyle.blurple, emoji="\U0001F977")
    async def btn_capture(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._resolve(interaction, "capture")

    @discord.ui.button(label="Flee", style=discord.ButtonStyle.grey, emoji="\U0001F3C3")
    async def btn_flee(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._resolve(interaction, "flee")


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Farming(commands.Cog):
    """Farming minigame: plant, grow, harvest, sell, climb the leaderboard."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._plot_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def cog_check(self, ctx: DiscoContext) -> bool:
        """Module + premium gate. Farming is a buddy-game minigame; admins
        do NOT bypass premium -- they are the ones who pay."""
        if not await module_cog_check(self.bot, ctx, "farming"):
            return False
        from services import entitlements
        from core.framework.premium import PremiumGateFailure
        if not await entitlements.is_premium(ctx.guild_id, ctx.db):
            raise PremiumGateFailure("farming")
        return True

    def _plot_lock(self, uid: int, gid: int) -> asyncio.Lock:
        return self._plot_locks.setdefault((uid, gid), asyncio.Lock())

    async def _fan_out(
        self, uid: int, gid: int, trigger: str, amount: int = 1,
    ) -> None:
        """Fan a single trigger into achievements / quests / challenges.

        Mirrors the pattern in cogs/dungeon.py so farming participates
        in the same per-user counter machinery. Each downstream call is
        wrapped because a bookkeeping failure must never abort the
        player's action.
        """
        try:
            from services import achievements as _ach
            await _ach.bump(self.bot, uid, gid, trigger, amount=amount)
        except Exception:
            log.debug("farming: achievements.bump %s failed", trigger, exc_info=True)
        try:
            from services import quests as _quests
            await _quests.progress_trigger(self.bot.db, uid, gid, trigger, amount=amount)
        except Exception:
            log.debug("farming: quests.progress_trigger %s failed", trigger, exc_info=True)
        try:
            from services import challenges as _ch
            await _ch.progress_trigger(self.bot, uid, gid, trigger, amount=amount)
        except Exception:
            log.debug("farming: challenges.progress_trigger %s failed", trigger, exc_info=True)

    async def _fan_out_farm_harvest(
        self, ctx: DiscoContext, res: "farm_svc.HarvestResult",
    ) -> None:
        """Emit harvest-related triggers (combo, sunheart, etc.)."""
        gid, uid = ctx.guild_id, ctx.author.id
        await self._fan_out(uid, gid, "farm_harvest")
        if str(res.rarity) == "legendary":
            await self._fan_out(uid, gid, "farm_legendary_harvest")
        # Per-crop trigger so achievements can target specific harvests.
        if res.crop_key:
            await self._fan_out(uid, gid, f"farm_harvest_{res.crop_key}")
        step = int(getattr(res, "combo_step", 0) or 0)
        if step >= 3:
            await self._fan_out(uid, gid, "farm_combo_3")
        if step >= 5:
            await self._fan_out(uid, gid, "farm_combo_5")
        if step >= 6:
            await self._fan_out(uid, gid, "farm_combo_legend")

    async def _fan_out_farm_pest_kill(
        self, ctx: DiscoContext, pest_key: str, *, boss: bool = False,
    ) -> None:
        """Emit pest-kill triggers including per-boss variants."""
        gid, uid = ctx.guild_id, ctx.author.id
        await self._fan_out(uid, gid, "farm_pest_kill")
        if boss:
            await self._fan_out(uid, gid, "farm_boss_pest_kill")
        if pest_key:
            await self._fan_out(uid, gid, f"farm_pest_kill_{pest_key}")

    # -- Group root ----------------------------------------------------------

    @commands.hybrid_group(
        name="farm", aliases=["field", "garden", "crop", "crops"],
        invoke_without_command=True, with_app_command=False,
    )
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm(self, ctx: DiscoContext) -> None:
        """Open your field view."""
        from services.onboarding import maybe_send_intro
        from core.framework.slot_warning import maybe_warn_full_slots
        await maybe_send_intro(ctx, "farming")
        await maybe_warn_full_slots(ctx, surface="farming", phase="game_start")
        gid, uid = ctx.guild_id, ctx.author.id
        state = await farm_svc.ensure_state(ctx.db, gid, uid)
        weather = await farm_svc.get_or_roll_weather(ctx.db, gid, uid)
        hrv_oracle, seed_oracle = await _oracle_pair(ctx)
        bloomstone = await ctx.db.get_bloomstone(uid, gid)
        # The HRV / SEED balances live on wallet_holdings (Harvest Network),
        # not on user_farming. Reading them off ``state`` always returned 0.
        hrv_wh = await ctx.db.get_wallet_holding(
            uid, gid, fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
        )
        seed_wh = await ctx.db.get_wallet_holding(
            uid, gid, fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
        )
        hrv_held = to_human(int(hrv_wh["amount"]) if hrv_wh else 0)
        seed_held = to_human(int(seed_wh["amount"]) if seed_wh else 0)
        view = FarmFieldView(self, ctx)
        # Populate the seed-pick dropdown from current packets / plots.
        try:
            await view._refresh_seed_select()
        except Exception:
            log.debug("FarmFieldView: initial seed-select build failed",
                      exc_info=True)
        msg = await ctx.reply(
            embed=_field_embed(
                ctx.author, state, weather,
                hrv_oracle=hrv_oracle, seed_oracle=seed_oracle,
                bloomstone=bloomstone,
                hrv_held=hrv_held, seed_held=seed_held,
            ),
            view=view,
            mention_author=False,
        )
        view.message = msg

    # -- Plant ---------------------------------------------------------------

    @farm.command(name="plant", aliases=["sow", "seed"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.PLANT_COOLDOWN_S)
    async def farm_plant(self, ctx: DiscoContext, a: str = "", b: str = "") -> None:
        """Plant a seed packet (or every packet of one crop in one shot).

        Order-agnostic so any of these work:
            ``,farm plant 1 wheat``       -- slot 1, wheat
            ``,farm plant wheat 1``       -- slot 1, wheat
            ``,farm plant wheat``         -- auto-pick first empty plot
            ``,farm plant all wheat``     -- fill every empty plot with wheat
            ``,farm plant wheat all``     -- (same)
        ``all`` keeps planting until either every empty plot is full or the
        player's wheat seed packets run out, whichever happens first.
        """
        gid, uid = ctx.guild_id, ctx.author.id
        args = [x for x in (a.strip(), b.strip()) if x]
        if not args:
            await ctx.reply_error_hint(
                "Specify a crop. Try `,farm crops` to see what's available.",
                hint="farm plant 1 wheat  /  farm plant all wheat",
                command_name="farm plant",
            )
            return
        slot_n: int | None = None
        crop: str | None = None
        plant_all = False
        for x in args:
            xl = x.lower()
            if xl in ("all", "max", "everything"):
                plant_all = True
            elif x.lstrip("-").isdigit():
                try:
                    slot_n = int(x)
                except ValueError:
                    pass
            else:
                crop = xl
        if not crop:
            await ctx.reply_error_hint(
                f"`{' '.join(args)}` doesn't include a crop name.",
                hint="farm plant 1 wheat  /  farm plant all wheat",
                command_name="farm plant",
            )
            return

        # ── Bulk plant: fill every empty plot with the requested crop ──
        if plant_all:
            crop_meta = fc.crop_meta(crop) or {}
            crop_key = crop_meta.get("key") or crop
            async with self._plot_lock(uid, gid):
                state = await farm_svc.ensure_state(ctx.db, gid, uid)
                plots = list(state.get("plots") or [])
                empty_slots = [int(p["slot"]) for p in plots if p.get("state") == "empty"]
                if not empty_slots:
                    await ctx.reply_error(
                        "All your plot tiles are full -- harvest some first "
                        "or expand with `,farm buy plot`.",
                    )
                    return
                seed_packets = state.get("seed_packets") or {}
                if not isinstance(seed_packets, dict):
                    import json as _json
                    try:
                        seed_packets = _json.loads(seed_packets) if seed_packets else {}
                    except Exception:
                        seed_packets = {}
                seeds_have = int(seed_packets.get(crop_key, 0) or 0)
                if seeds_have <= 0:
                    await ctx.reply_error_hint(
                        f"You have no {crop_meta.get('name', crop_key)} "
                        f"seed packets to plant.",
                        hint=f"farm buy seed {crop_key} 10",
                        command_name="farm plant",
                    )
                    return
                planted_slots: list[int] = []
                last_res = None
                # Cap by min(empty plots, seeds) so we never over-plant.
                limit = min(len(empty_slots), seeds_have)
                for slot in empty_slots[:limit]:
                    try:
                        last_res = await farm_svc.plant_seed(
                            ctx.db, gid, uid, slot, crop,
                        )
                        if last_res.ok:
                            planted_slots.append(slot + 1)
                        else:
                            break
                    except ValueError as exc:
                        # Out of seeds mid-loop, or some other rejection.
                        if not planted_slots:
                            await ctx.reply_error(str(exc))
                            return
                        break
            if not planted_slots:
                await ctx.reply_error(
                    "Could not plant any plots -- check seed packets and "
                    "empty slots.",
                )
                return
            meta = fc.crop_meta(planted_slots and crop_key) or {}
            eta = _time_remaining(last_res.ready_at) if last_res else "?"
            slots_disp = ", ".join(str(s) for s in planted_slots)
            await ctx.reply_success(
                f"{meta.get('emoji', '')} Planted "
                f"**{len(planted_slots)} x {meta.get('name', crop_key)}** "
                f"across slots {slots_disp}.\n"
                f"Ready in ~**{eta}**.",
                title="\U0001F331 Planted (bulk)",
            )
            return

        if slot_n is None:
            state = await farm_svc.ensure_state(ctx.db, gid, uid)
            plots = list(state.get("plots") or [])
            for p in plots:
                if p.get("state") == "empty":
                    slot_n = int(p["slot"]) + 1
                    break
            if slot_n is None:
                await ctx.reply_error(
                    "All your plot tiles are full -- harvest some first or "
                    "expand with `,farm buy plot`.",
                )
                return
        async with self._plot_lock(uid, gid):
            try:
                res = await farm_svc.plant_seed(
                    ctx.db, gid, uid, slot_n - 1, crop,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
        if not res.ok:
            await ctx.reply_error(res.msg or "Could not plant.")
            return
        meta = fc.crop_meta(res.crop_key) or {}
        eta = _time_remaining(res.ready_at)
        await ctx.reply_success(
            f"{meta.get('emoji', '')} **{meta.get('name', res.crop_key)}** planted in slot {slot_n}.\n"
            f"Ready in **{eta}**.",
            title="\U0001F331 Planted",
        )

    # -- Water ---------------------------------------------------------------

    @farm.command(name="water")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.WATER_COOLDOWN_S)
    async def farm_water(self, ctx: DiscoContext, slot: int | None = None) -> None:
        """Water one plot (by slot) or all plots."""
        gid, uid = ctx.guild_id, ctx.author.id
        async with self._plot_lock(uid, gid):
            if slot is not None:
                try:
                    res = await farm_svc.water_plot(ctx.db, gid, uid, slot - 1)
                except ValueError as exc:
                    await ctx.reply_error(str(exc))
                    return
                if not res.ok:
                    await ctx.reply_error(res.msg or "Could not water.")
                    return
                await ctx.reply_success(
                    f"Slot {slot} watered. Growth +{res.growth_speedup_pct:.0%} faster.",
                    title="\U0001F4A7 Watered",
                )
            else:
                state = await farm_svc.ensure_state(ctx.db, gid, uid)
                plots = list(state.get("plots") or [])
                watered = 0
                for p in plots:
                    if p.get("state") in ("growing", "ready"):
                        try:
                            res = await farm_svc.water_plot(ctx.db, gid, uid, int(p["slot"]))
                            if res.ok:
                                watered += 1
                        except Exception:
                            pass
                await ctx.reply_success(
                    f"Watered **{watered}** plot(s).",
                    title="\U0001F4A7 Watered",
                )

    # -- Fertilize -----------------------------------------------------------

    @farm.command(name="fertilize")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_fertilize(self, ctx: DiscoContext, slot: str) -> None:
        """Apply equipped fertilizer to a plot, or to every eligible plot.

        ``,farm fertilize <slot>`` -- one specific plot (1-based).
        ``,farm fertilize all``    -- every growing plot that doesn't
        already have fertilizer applied, until the inventory runs out.
        """
        s = (slot or "").strip().lower()
        if s in ("all", "every", "everything"):
            async with self._plot_lock(ctx.author.id, ctx.guild_id):
                try:
                    applied, fert_key = await farm_svc.apply_fertilizer_all(
                        ctx.db, ctx.guild_id, ctx.author.id,
                    )
                except ValueError as exc:
                    await ctx.reply_error(str(exc))
                    return
            fmeta = fc.fertilizer_meta(fert_key) or {}
            if not applied:
                await ctx.reply_error(
                    "No growing plots needed fertilizer. Plant seeds or wait "
                    "for current plots to start growing first."
                )
                return
            slot_list = ", ".join(f"#{n}" for n in applied)
            await ctx.reply_success(
                f"{fmeta.get('emoji', '')} **{fmeta.get('name', fert_key)}** applied "
                f"to **{len(applied)}** plot(s): {slot_list}.\n"
                f"Yield x{float(fmeta.get('yield_mult', 1.0)):.2f}, "
                f"growth x{float(fmeta.get('growth_mult', 1.0)):.2f}.",
                title="\U0001F9F4 Fertilized All",
            )
            return
        try:
            slot_n = int(s)
        except ValueError:
            await ctx.reply_error("Slot must be a number or `all`.")
            return
        async with self._plot_lock(ctx.author.id, ctx.guild_id):
            try:
                res = await farm_svc.apply_fertilizer(
                    ctx.db, ctx.guild_id, ctx.author.id, slot_n - 1,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
        if not res.ok:
            await ctx.reply_error(res.msg or "Could not fertilize.")
            return
        fmeta = fc.fertilizer_meta(res.fertilizer_key) or {}
        await ctx.reply_success(
            f"{fmeta.get('emoji', '')} **{fmeta.get('name', res.fertilizer_key)}** applied to slot {slot_n}.\n"
            f"Yield x{res.yield_mult:.2f}, growth x{res.growth_mult:.2f}.",
            title="\U0001F9F4 Fertilized",
        )

    # -- Harvest -------------------------------------------------------------

    @farm.command(name="harvest", aliases=["reap", "pick"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.HARVEST_COOLDOWN_S)
    async def farm_harvest(self, ctx: DiscoContext, slot: str | None = None) -> None:
        """Harvest a ripe plot, or all ripe plots.

        ``,farm harvest``         -- harvest every ripe plot
        ``,farm harvest all``     -- same
        ``,farm harvest <slot>``  -- harvest a specific plot (1-based)
        """
        gid, uid = ctx.guild_id, ctx.author.id
        s = (slot or "").strip().lower()
        do_all = (not s) or s in ("all", "every", "everything")
        slot_n: int | None = None
        if not do_all:
            try:
                slot_n = int(s)
            except ValueError:
                await ctx.reply_error("Slot must be a number or `all`.")
                return
        async with self._plot_lock(uid, gid):
            if slot_n is not None:
                try:
                    res = await farm_svc.harvest_plot(ctx.db, gid, uid, slot_n - 1)
                except ValueError as exc:
                    await ctx.reply_error(str(exc))
                    return
                await self._fan_out_farm_harvest(ctx, res)
                meta = fc.crop_meta(res.crop_key) or {}
                seed_human = to_human(int(res.seed_raw))
                rarity_label = res.rarity.title()
                mut_meta = fc.mutation_meta(res.mutation)
                # Seed-return: small chance the harvested crop drops
                # packets of itself back into the bag. Tail line on
                # both the mutation embed and the plain success path.
                seed_return_line = ""
                if int(res.seed_packets_returned or 0) > 0:
                    seed_return_line = (
                        f"\n\U0001F331 **{res.seed_packets_returned}** "
                        f"{meta.get('name', res.crop_key)} seed packet"
                        f"{'s' if res.seed_packets_returned != 1 else ''} returned!"
                    )
                if mut_meta:
                    # Mutation hit -- big visible reveal with the
                    # dedicated burst frame so it's clear something
                    # special just happened.
                    body = (
                        f"```\n{fc.FRAMES.get('mutation_burst', '')}\n```\n"
                        f"{mut_meta['emoji']} **{mut_meta['name']}** "
                        f"{meta.get('emoji', '')} **{meta.get('name', res.crop_key)}** "
                        f"x{res.qty} ({rarity_label})\n"
                        f"+{_fmt_seed(seed_human)} SEED"
                        f"{seed_return_line}\n"
                        f"_{mut_meta['blurb']}_"
                    )
                    embed = card(
                        "\U00002728 Mutation Harvest!",
                        color=_RARITY_COLOR.get(res.rarity, C_GOLD),
                        description=body,
                    ).build()
                    await ctx.reply(embed=embed, mention_author=False)
                else:
                    await ctx.reply_success(
                        f"{meta.get('emoji', '')} **{meta.get('name', res.crop_key)}** x{res.qty} ({rarity_label})\n"
                        f"+{_fmt_seed(seed_human)} SEED"
                        f"{seed_return_line}",
                        title="\U0001F33E Harvested",
                    )
            else:
                state = await farm_svc.ensure_state(ctx.db, gid, uid)
                plots = list(state.get("plots") or [])
                # The plot state machine is empty -> growing -> harvested-back-to-empty.
                # There is no automatic 'ready' transition -- plots stay in
                # 'growing' until harvest_plot reads the ready_at timestamp.
                # Treat anything past its ready_at as harvestable, regardless of
                # what the state field says.
                now = datetime.datetime.now(tz=datetime.timezone.utc)
                results = []
                for p in plots:
                    if p.get("state") not in ("growing", "ready", "ripe"):
                        continue
                    ready_iso = p.get("ready_at")
                    if ready_iso:
                        try:
                            ready_at = datetime.datetime.fromisoformat(str(ready_iso))
                            if ready_at.tzinfo is None:
                                ready_at = ready_at.replace(tzinfo=datetime.timezone.utc)
                        except ValueError:
                            ready_at = None
                        if ready_at and now < ready_at:
                            continue
                    try:
                        r = await farm_svc.harvest_plot(ctx.db, gid, uid, int(p["slot"]))
                        results.append(r)
                    except Exception:
                        pass
                if not results:
                    await ctx.reply_error(
                        "No ripe plots to harvest. Check `,farm` for ready times.",
                    )
                    return
                total_seed = sum(to_human(int(r.seed_raw)) for r in results)
                lines = []
                mutated = 0
                seed_returns: dict[str, int] = {}
                for r in results:
                    m = fc.crop_meta(r.crop_key) or {}
                    mm = fc.mutation_meta(r.mutation)
                    rarity_tag = f" `{r.rarity.title()}`"
                    if mm:
                        mutated += 1
                        lines.append(
                            f"{mm['emoji']} **{mm['name']}** {m.get('emoji', '')} "
                            f"{m.get('name', r.crop_key)} x{r.qty}{rarity_tag}"
                        )
                    else:
                        lines.append(
                            f"{m.get('emoji', '')} **{m.get('name', r.crop_key)}** "
                            f"x{r.qty}{rarity_tag}"
                        )
                    if int(r.seed_packets_returned or 0) > 0:
                        seed_returns[r.crop_key] = (
                            seed_returns.get(r.crop_key, 0)
                            + int(r.seed_packets_returned)
                        )
                tail = f"\n\n+{_fmt_seed(total_seed)} SEED total"
                if mutated:
                    tail += f"\n\U00002728 {mutated} mutation{'s' if mutated != 1 else ''} this batch!"
                if seed_returns:
                    bits = []
                    for k, n in seed_returns.items():
                        cm = fc.crop_meta(k) or {}
                        bits.append(f"**{n}x** {cm.get('name', k)}")
                    tail += "\n\U0001F331 Seed packets returned: " + ", ".join(bits)
                await ctx.reply_success(
                    "\n".join(lines) + tail,
                    title=f"\U0001F33E Harvested {len(results)} plot(s)",
                )

            # Wild-buddy spawn + harvest-egg roll. Both are best-effort
            # additive surprises -- a failure here never aborts the
            # harvest reply the player already saw.
            try:
                state = await farm_svc.ensure_state(ctx.db, gid, uid)
                wild = await farm_svc.maybe_spawn_wild_battle(
                    ctx.db, gid, uid, str(state.get("current_zone") or "meadow"),
                )
                if wild:
                    sp = str(wild.get("species") or "wild buddy").title()
                    lvl = int(wild.get("level") or 1)
                    tier = int(wild.get("rarity_tier") or 1)
                    tier_word = ("Common", "Uncommon", "Rare", "Epic", "Legendary")[
                        max(0, min(4, tier - 1))
                    ]
                    notice = card(
                        "\U0001F4AB A wild buddy appears!",
                        color=C_WARNING,
                        description=(
                            f"A wild **{sp}** (Lv {lvl}, {tier_word}) "
                            f"emerges from the harvest. Send your active "
                            f"CC buddy after it with `,farm battle`, or "
                            f"keep farming and it'll wait. Win pays HRV "
                            f"and BBT; capture chance on win."
                        ),
                    ).build()
                    await ctx.send(embed=notice)
            except Exception:
                log.debug("farm wild-spawn check failed", exc_info=True)
            try:
                got_egg = await farm_svc.maybe_drop_harvest_egg(ctx.db, gid, uid)
                if got_egg:
                    await ctx.send(
                        embed=card(
                            "\U0001F95A Buddy egg!",
                            color=C_SUCCESS,
                            description=(
                                "A buddy egg tumbles out of the harvest. "
                                "Held in your egg slot -- hatch with "
                                "`,buddy hatch <species>`."
                            ),
                        ).build()
                    )
            except Exception:
                log.debug("farm harvest egg drop failed", exc_info=True)

    # -- Wild buddy battle ---------------------------------------------------

    @farm.command(name="battle", aliases=["fight", "wild"])
    @guild_only
    @no_bots
    @ensure_registered
    async def farm_battle(self, ctx: DiscoContext) -> None:
        """Send your active CC buddy after the wild buddy that spawned
        during the last harvest. Win pays HRV + BBT and rolls capture.
        Skipping is free: keep harvesting and the wild buddy waits.
        """
        gid, uid = ctx.guild_id, ctx.author.id
        # One-fight-at-a-time gate: refuse if the player already has a
        # buddy / fish / delve / farm / escape fight in flight.
        from services.fight_lock import fight_lock_guard, FightLockBusy
        try:
            async with fight_lock_guard(ctx, kind="farm_wild"):
                return await self._farm_battle_locked(ctx)
        except FightLockBusy as exc:
            await ctx.reply_error(str(exc))
            return

    async def _farm_battle_locked(self, ctx: DiscoContext) -> None:
        """Body of ``,farm battle`` -- runs inside the fight_lock_guard."""
        gid, uid = ctx.guild_id, ctx.author.id
        state = await farm_svc.ensure_state(ctx.db, gid, uid)
        wild_buddy = state.get("pending_wild_buddy") or None
        if isinstance(wild_buddy, str):
            try:
                import json as _json
                wild_buddy = _json.loads(wild_buddy)
            except Exception:
                wild_buddy = None
        if not wild_buddy:
            await ctx.reply_error(
                "No wild buddy waiting. Harvest a few crops to spawn one."
            )
            return
        active = await ctx.db.fetch_one(
            "SELECT * FROM cc_buddies WHERE guild_id=$1 AND owner_user_id=$2 "
            "AND status='owned' AND is_active = TRUE LIMIT 1",
            gid, uid,
        )
        if not active:
            await ctx.reply_error_hint(
                "You need an active CC buddy to fight a wild one. "
                "Activate one with `,buddy panel`.",
                hint="buddy panel",
                command_name="farm battle",
            )
            return
        # Surface a slot warning at fight-start so the player knows
        # up front that a successful capture won't drop into their
        # shelter when it's full.
        try:
            from core.framework.slot_warning import maybe_warn_full_slots
            await maybe_warn_full_slots(
                ctx, surface="farming", phase="fight_start",
            )
        except Exception:
            log.debug("farm slot warning failed", exc_info=True)
        from services import buddy_battle as _bb
        result = _bb.run_battle(dict(active), dict(wild_buddy))
        won = bool(result.winner is not None and int(result.winner.owner_id or 0) == int(uid))
        bonus_pct = 0.0
        if won and int(result.rounds) > 0:
            bonus_pct = max(0.0, min(0.30, (12 - int(result.rounds)) / 24.0))
        try:
            res = await farm_svc.resolve_wild_battle(
                ctx.db, gid, uid,
                won=won,
                zone=str(state.get("current_zone") or "meadow"),
                opponent_species=str(wild_buddy.get("species") or ""),
                opponent_level=int(wild_buddy.get("level") or 1),
                opponent_rarity_tier=int(wild_buddy.get("rarity_tier") or 1),
                bonus_pct=bonus_pct,
            )
        except Exception:
            log.exception(
                "farm battle: resolve failed uid=%s gid=%s", uid, gid,
            )
            await ctx.reply_error("Battle resolution failed -- try again.")
            return
        log_lines = list(result.log or [])
        if len(log_lines) > 12:
            log_lines = log_lines[:6] + ["..."] + log_lines[-5:]
        sp_display = str(wild_buddy.get("species") or "wild buddy").title()
        title = (
            f"\U0001F4AB Wild {sp_display} defeated!" if won
            else f"\U0001F4A8 The {sp_display} got away."
        )
        color = C_GOLD if won else C_AMBER
        b = card(title, color=color).description("\n".join(log_lines))
        if won:
            hrv_h = to_human(int(res.hrv_reward_raw))
            bbt_h = to_human(int(res.bbt_reward_raw))
            reward_lines = []
            if hrv_h > 0:
                reward_lines.append(_fmt_hrv(hrv_h))
            if bbt_h > 0:
                reward_lines.append(f"\U0001F94A {bbt_h:,.4f} BBT")
            if reward_lines:
                b = b.field("Rewards", "\n".join(reward_lines), False)
            if bonus_pct > 0:
                b = b.field(
                    "Clean fight bonus",
                    f"+{bonus_pct * 100:.0f}% (cleared in {result.rounds} rounds)",
                    True,
                )
        if res.captured and res.captured_buddy_row:
            cap_status = str(res.captured_buddy_row.get("status") or "owned")
            destination_line = (
                "Active slots full -- went to your **storage** "
                "(`,buddy storage`)."
                if cap_status == "stored"
                else "Joins your active roster."
            )
            b = b.field(
                "Captured!",
                f"**{res.captured_buddy_row.get('name')}** "
                f"({sp_display}, Lv {int(res.captured_buddy_row.get('level') or 1)}) "
                f"-- {destination_line}",
                False,
            )
        if bool(wild_buddy.get("attractor_pulled")):
            b = b.field(
                "\U0001F9F2 Battle Attractor",
                "Your attractor lured this encounter.",
                inline=False,
            )
        b = b.footer(
            f"Wild wins {res.new_won_total} | losses {res.new_lost_total} | "
            f"captures {res.new_captured_total}"
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # -- Zones ---------------------------------------------------------------

    @farm.command(name="zones")
    @guild_only
    @no_bots
    @ensure_registered
    async def farm_zones(self, ctx: DiscoContext) -> None:
        """List all farming zones."""
        await ctx.reply(embed=_zones_embed(), mention_author=False)

    @farm.command(name="zone")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_zone(self, ctx: DiscoContext, key: str) -> None:
        """Switch to a farming zone."""
        try:
            await farm_svc.set_zone(ctx.db, ctx.guild_id, ctx.author.id, key.lower())
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        z = fc.zone_meta(key.lower()) or {}
        await ctx.reply_success(
            f"Moved to **{z.get('name', key)}**. {z.get('blurb', '')}",
            title=f"{z.get('emoji', '')} Zone Changed",
        )

    # -- Crops catalog -------------------------------------------------------

    @farm.command(name="crops")
    @guild_only
    @no_bots
    @ensure_registered
    async def farm_crops(self, ctx: DiscoContext) -> None:
        """Browse the full crop catalog."""
        await ctx.reply(embed=_crops_embed(), mention_author=False)

    # -- Shop ----------------------------------------------------------------

    @farm.command(name="shop")
    @guild_only
    @no_bots
    @ensure_registered
    async def farm_shop(self, ctx: DiscoContext) -> None:
        """Browse the farm shop."""
        state = await farm_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        wh = await ctx.db.get_wallet_holding(
            ctx.author.id, ctx.guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
        )
        hrv_balance = to_human(int(wh["amount"]) if wh else 0)
        hrv_oracle, _ = await _oracle_pair(ctx)
        view = QuickBuyView(
            ctx=ctx,
            command_template="farm buy {item}",
            accepted_currency=fc.HRV_SYMBOL,
            item_label="What to buy",
            item_placeholder="plot | fertilizer compost 5 | seed wheat 10",
            modal_title=f"Farm Quick Buy ({fc.HRV_SYMBOL})",
        )
        sent = await ctx.reply(
            embed=_shop_embed(state, hrv_balance=hrv_balance, hrv_oracle=hrv_oracle),
            view=view,
            mention_author=False,
        )
        view.message = sent

    @farm.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_buy(
        self, ctx: DiscoContext, what: str, key: str = "", qty: str = "1",
    ) -> None:
        """Buy a plot upgrade, fertilizer, or seed packet.

        Examples:
            ,farm buy plot
            ,farm buy fertilizer compost 5
            ,farm buy seed wheat 10        -- exact qty
            ,farm buy seed wheat all       -- max your HRV will buy
            ,farm buy seed "world tree"    -- crop name with spaces
        """
        gid, uid = ctx.guild_id, ctx.author.id
        w = what.lower()
        # Quick-buy short form: ``,farm buy Bonemeal`` and ``,farm buy wheat 5``
        # are forwarded from the shop modal without a category prefix. When
        # ``what`` matches a fertilizer or crop key, shuffle args so the
        # canonical ``buy <category> <key> [qty]`` parser sees them.
        if w not in ("plot", "fertilizer", "fert", "seed"):
            if fc.fertilizer_meta(w):
                qty = key.strip() if key else qty
                key = w
                w = "fertilizer"
            else:
                resolved_crop = self._resolve_crop_key(what)
                if resolved_crop:
                    qty = key.strip() if key else qty
                    key = resolved_crop
                    w = "seed"
        # Parse qty -- must accept 'all' / 'max' for seeds + fert. The default
        # discord.py int converter rejects non-digits before our handler runs,
        # so we take a string and convert here with a helpful error.
        qty_raw = (qty or "1").strip().lower()
        qty_all = qty_raw in ("all", "max", "everything")
        qty_n = 1
        if not qty_all:
            try:
                qty_n = max(1, int(qty_raw))
            except ValueError:
                await ctx.reply_error_hint(
                    f"`{qty}` is not a number. Use a count, or `all` for the max your HRV will buy.",
                    hint=f"farm buy {w} {key or '<key>'} 5",
                    command_name="farm buy",
                )
                return
        try:
            if w == "plot":
                state = await farm_svc.ensure_state(ctx.db, gid, uid)
                next_tier = int(state.get("plot_tier") or 1) + 1
                res = await farm_svc.buy_plot_tier(ctx.db, gid, uid, next_tier)
            elif w in ("fertilizer", "fert"):
                if qty_all:
                    qty_n = await self._max_buyable_fertilizer(ctx, key.lower())
                    if qty_n <= 0:
                        await ctx.reply_error("Not enough HRV to buy a single one.")
                        return
                res = await farm_svc.buy_fertilizer(ctx.db, gid, uid, key.lower(), qty_n)
            elif w == "seed":
                ckey = self._resolve_crop_key(key)
                if not ckey:
                    await ctx.reply_error_hint(
                        f"Unknown crop `{key}`. Use a name from `,farm crops` (try `wheat`, `carrot`, `world_tree`).",
                        hint="farm buy seed wheat 5",
                        command_name="farm buy seed",
                    )
                    return
                if qty_all:
                    qty_n = await self._max_buyable_seed(ctx, ckey)
                    if qty_n <= 0:
                        await ctx.reply_error("Not enough HRV to buy a single packet.")
                        return
                res = await farm_svc.buy_seed_packet(ctx.db, gid, uid, ckey, qty_n)
            else:
                await ctx.reply_error_hint(
                    f"Unknown buy category `{what}`.",
                    hint="`plot` | `fertilizer <key> <qty>` | `seed <crop> <qty|all>`",
                    command_name="farm buy",
                )
                return
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if not res.ok:
            await ctx.reply_error(res.msg or "Purchase failed.")
            return
        spent = to_human(int(res.hrv_spent_raw))
        await ctx.reply_success(
            f"Purchased **{res.key}** x{res.qty}  -  spent {_fmt_hrv(spent)}",
            title="\U0001F6D2 Purchased",
        )

    def _resolve_crop_key(self, key: str) -> str | None:
        """Run a crop lookup through farming_config.crop_meta and return its
        canonical key, or ``None`` when no fuzzy match was found.
        """
        meta = fc.crop_meta(key)
        return str(meta["key"]) if meta else None

    async def _max_buyable_seed(self, ctx: DiscoContext, crop_key: str) -> int:
        """Compute how many seed packets the user can afford right now."""
        cmeta = fc.crop_meta(crop_key)
        if not cmeta:
            return 0
        price_each = float(cmeta["hrv_sell_price"]) * 0.20
        if price_each <= 0:
            return 0
        wh = await ctx.db.get_wallet_holding(
            ctx.author.id, ctx.guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
        )
        bal = to_human(int(wh["amount"]) if wh else 0)
        return max(0, int(bal // price_each))

    async def _max_buyable_fertilizer(self, ctx: DiscoContext, fert_key: str) -> int:
        """Compute how many fertilizer units the user can afford."""
        fmeta = fc.fertilizer_meta(fert_key)
        if not fmeta:
            return 0
        price_each = float(fmeta.get("price_hrv") or 0)
        if price_each <= 0:
            return 0
        wh = await ctx.db.get_wallet_holding(
            ctx.author.id, ctx.guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
        )
        bal = to_human(int(wh["amount"]) if wh else 0)
        return max(0, int(bal // price_each))

    # -- Equip fertilizer ----------------------------------------------------

    @farm.command(name="equip")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_equip(self, ctx: DiscoContext, key: str) -> None:
        """Equip a fertilizer (or 'none' to unequip)."""
        try:
            await farm_svc.set_fertilizer(ctx.db, ctx.guild_id, ctx.author.id, key.lower())
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if key.lower() in ("none", "off", "clear"):
            await ctx.reply_success("Fertilizer unequipped.", title="\U0001F9F4 Unequipped")
        else:
            fmeta = fc.fertilizer_meta(key.lower()) or {}
            await ctx.reply_success(
                f"{fmeta.get('emoji', '')} **{fmeta.get('name', key)}** equipped.",
                title="\U0001F9F4 Equipped",
            )

    # -- Sell ----------------------------------------------------------------

    @farm.command(name="sell")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_sell(self, ctx: DiscoContext, target: str = "all") -> None:
        """Sell harvested crops for HRV (e.g. ,farm sell wheat or ,farm sell all)."""
        hrv_oracle, _ = await _oracle_pair(ctx)
        try:
            res = await farm_svc.sell_crop(ctx.db, ctx.guild_id, ctx.author.id, target.lower())
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        hrv_human = to_human(int(res.hrv_received_raw))
        await ctx.reply_success(
            f"Sold **{res.qty_sold}** {res.crop_or_recipe_key} for "
            f"**{_fmt_hrv(hrv_human)}**{_with_usd(hrv_human, hrv_oracle)}\n"
            f"-# Slippage: {res.slippage_pct * 100:.2f}%",
            title="\U0001F4B0 Sold",
        )

    # -- Process recipe ------------------------------------------------------

    @farm.command(name="process")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_process(self, ctx: DiscoContext, recipe: str, qty: int = 1) -> None:
        """Process crops into a recipe (e.g. ,farm process bread)."""
        try:
            res = await farm_svc.process_recipe(
                ctx.db, ctx.guild_id, ctx.author.id, recipe.lower(), qty,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if not res.ok:
            await ctx.reply_error(res.msg or "Recipe failed.")
            return
        rmeta = fc.recipe_meta(res.recipe_key) or {}
        seed_bonus = to_human(int(res.seed_bonus_raw))
        await ctx.reply_success(
            f"{rmeta.get('emoji', '')} **{rmeta.get('name', res.recipe_key)}** x{res.qty_made}\n"
            + (f"+{_fmt_seed(seed_bonus)} SEED bonus" if seed_bonus > 0 else ""),
            title="\U0001F373 Processed",
        )

    # -- Bag -----------------------------------------------------------------

    @farm.command(name="bag", aliases=["inv", "inventory", "tacklebox"])
    @guild_only
    @no_bots
    @ensure_registered
    async def farm_bag(self, ctx: DiscoContext) -> None:
        """Show your farm bag (crops, seeds, fertilizer, processed goods)."""
        state = await farm_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        summary = farm_svc.inventory_summary(state)
        await ctx.reply(embed=_bag_embed(ctx.author, summary), mention_author=False)

    # -- History -------------------------------------------------------------

    @farm.command(name="history")
    @guild_only
    @no_bots
    @ensure_registered
    async def farm_history(self, ctx: DiscoContext) -> None:
        """Show your last 10 harvests."""
        rows = await farm_svc.get_user_harvests(
            ctx.db, ctx.guild_id, ctx.author.id, limit=10,
        )
        await ctx.reply(embed=_history_embed(ctx.author, rows), mention_author=False)

    # -- Forage minigame -----------------------------------------------------

    @farm.command(name="forage", aliases=["scavenge", "wander", "hunt"])
    @guild_only
    @no_bots
    @ensure_registered
    async def farm_forage(self, ctx: DiscoContext) -> None:
        """Wander the brambles for a randomized payout.

        Free roll every 10 minutes. Drops range from a small HRV / SEED
        purse up to a stash of seed packets, a fertilizer pack, or the
        rare Ancient Tuber jackpot (a legendary crop straight to your
        bag). Mirrors `,fish dig` in shape and feel.
        """
        gid, uid = ctx.guild_id, ctx.author.id
        try:
            res = await farm_svc.farm_forage(ctx.db, gid, uid)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # Pre-frame so the reveal lands with a beat. Same send-then-edit
        # cadence the cast / dig views use.
        pre_msg = None
        try:
            pre_msg = await ctx.reply(
                embed=card(
                    "\U0001F33F Following the trail...",
                    description=f"```\n{fc.FRAMES['forage_start']}\n```",
                    color=C_AMBER,
                ).build(),
                mention_author=False,
            )
            await asyncio.sleep(0.8)
        except Exception:
            log.debug("farm forage pre-frame send failed", exc_info=True)
        hrv_oracle, seed_oracle = await _oracle_pair(ctx)
        # Pick the right ASCII frame for the outcome reveal.
        frame_key = {
            "hrv_purse_small": "forage_hrv_purse",
            "hrv_purse_big":   "forage_hrv_purse",
            "seed_pile_small": "forage_seed_pile",
            "seed_pile_big":   "forage_seed_pile",
            "seed_packets":    "forage_packets",
            "fertilizer_find": "forage_fertilizer",
            "ancient_tuber":   "forage_jackpot",
            "empty":           "forage_empty",
        }.get(res.outcome_key, "forage_empty")
        frame = fc.FRAMES.get(frame_key, "")
        # Detail bullets per outcome -- single switch, no extra DB reads.
        detail_lines: list[str] = []
        if res.hrv_credited > 0:
            detail_lines.append(
                f"\U0001F4B0 +**{_fmt_hrv(res.hrv_credited)}**"
                f"{_with_usd(res.hrv_credited, hrv_oracle)}"
            )
        if res.seed_credited > 0:
            detail_lines.append(
                f"\U0001F331 +**{_fmt_seed(res.seed_credited)}**"
                f"{_with_usd(res.seed_credited, seed_oracle)}"
            )
        for crop_key, qty in res.packets_added:
            cmeta = fc.crop_meta(crop_key) or {}
            detail_lines.append(
                f"{cmeta.get('emoji', '')} **{qty}x {cmeta.get('name', crop_key)}** seed packet(s)"
            )
        if res.fertilizer_added:
            fk, qty = res.fertilizer_added
            fmeta = fc.fertilizer_meta(fk) or {}
            detail_lines.append(
                f"{fmeta.get('emoji', '')} **{qty}x {fmeta.get('name', fk)}** fertilizer"
            )
        if res.jackpot_crop:
            ck, qty = res.jackpot_crop
            cmeta = fc.crop_meta(ck) or {}
            detail_lines.append(
                f"\U00002728 {cmeta.get('emoji', '')} **{qty}x {cmeta.get('name', ck)}** "
                f"({str(cmeta.get('rarity', '?')).title()})"
            )
        color = (
            C_GOLD     if res.outcome_key == "ancient_tuber"
            else C_TEAL if res.outcome_key in ("seed_packets", "fertilizer_find")
            else C_AMBER if res.outcome_key.startswith("hrv_") or res.outcome_key.startswith("seed_")
            else C_NEUTRAL
        )
        title = "\U0001F33F " + res.label
        body = "\n".join(detail_lines) if detail_lines else "_The brambles only had stickers for you._"
        desc = f"```\n{frame}\n```\n{body}"
        desc += f"\n-# Next forage in {fc.FORAGE_COOLDOWN_S // 60}m."
        final_embed = card(title, description=desc, color=color).build()
        if pre_msg is not None:
            try:
                await pre_msg.edit(embed=final_embed)
                return
            except Exception:
                log.debug("farm forage final edit failed", exc_info=True)
        await ctx.reply(embed=final_embed, mention_author=False)

    # -- Daily contract ------------------------------------------------------

    @farm.command(name="contract", aliases=["contracts", "order", "delivery"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_contract(
        self, ctx: DiscoContext, action: str | None = None,
    ) -> None:
        """View or fulfill the daily NPC crop contract.

        ``,farm contract``           -- show today's order + your progress
        ``,farm contract turnin``    -- deliver matching crops from your bag
        """
        gid, uid = ctx.guild_id, ctx.author.id
        act = (action or "").strip().lower()
        if act in ("turnin", "deliver", "submit", "fulfill"):
            try:
                res = await farm_svc.turn_in_daily_contract(ctx.db, gid, uid)
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            cmeta = fc.crop_meta(res.crop_key) or {}
            hrv_human = to_human(int(res.hrv_paid_raw))
            seed_human = to_human(int(res.seed_paid_raw))
            hrv_oracle, _ = await _oracle_pair(ctx)
            tail = f"+{_fmt_hrv(hrv_human)}{_with_usd(hrv_human, hrv_oracle)}"
            if seed_human > 0:
                tail += f"\n+{_fmt_seed(seed_human)} SEED"
            title = "\U0001F4E6 Contract complete!" if res.completed else "\U0001F4E6 Delivery accepted"
            body = (
                f"Delivered {cmeta.get('emoji', '')} **{cmeta.get('name', res.crop_key)}** "
                f"x{res.qty_turned_in}\n{tail}"
            )
            if res.completed:
                body += "\n\nNew contract rolls at UTC midnight."
            await ctx.reply_success(body, title=title)
            return
        # Default: panel view
        view = await farm_svc.get_daily_contract(ctx.db, gid, uid)
        await ctx.reply(
            embed=_contract_embed(ctx.author, view),
            mention_author=False,
        )

    # -- Token economy: swap -------------------------------------------------

    @farm.command(name="swap")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_swap(self, ctx: DiscoContext, amount: str) -> None:
        """Burn SEED to mint HRV (,farm swap 100 or ,farm swap all)."""
        hrv_oracle, seed_oracle = await _oracle_pair(ctx)
        gid, uid = ctx.guild_id, ctx.author.id
        if amount.lower() in ("all", "everything"):
            held = await ctx.db.get_wallet_holding(uid, gid, fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL)
            amt_raw = int((held or {}).get("amount") or 0)
        else:
            try:
                amt_raw = to_raw(float(amount))
            except ValueError:
                await ctx.reply_error("Invalid amount.")
                return
        try:
            res = await farm_svc.burn_seed_for_hrv(ctx.db, gid, uid, amt_raw)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        burned = to_human(int(res.burned_seed_raw))
        minted = to_human(int(res.minted_hrv_raw))
        await ctx.reply_success(
            f"Burned **{_fmt_seed(burned)}** -> minted **{_fmt_hrv(minted)}**"
            f"{_with_usd(minted, hrv_oracle)}\n"
            f"-# Impact: {res.impact_pct * 100:.2f}%",
            title="\U0001F525 SEED -> HRV",
        )

    # -- Token economy: stake ------------------------------------------------

    @farm.command(name="stake", aliases=["lock", "stakes", "stakeinfo"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_stake(self, ctx: DiscoContext, amount: str = "") -> None:
        """Stake SEED for passive HRV yield, or show the stake panel.

        ``,farm stake``                 -- show the stake panel (SEED locked,
                                            pending HRV, daily rate, USD)
        ``,farm stake <amt|all>``        -- lock SEED for passive HRV yield
        """
        s = (amount or "").strip().lower()
        if not s:
            await self._open_stake_panel(ctx)
            return
        if s in ("all", "everything", "max"):
            wh = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id,
                fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
            )
            amt_raw = int(wh["amount"]) if wh else 0
            if amt_raw <= 0:
                await ctx.reply_error("You have no SEED to stake.")
                return
        else:
            try:
                amt_raw = to_raw(float(s))
            except ValueError:
                await ctx.reply_error("Invalid amount.")
                return
        try:
            res = await farm_svc.stake_seed(ctx.db, ctx.guild_id, ctx.author.id, amt_raw)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        from core.framework.staking import stake_receipt
        hrv_oracle, seed_oracle = await _oracle_pair(ctx)
        await ctx.reply(
            embed=stake_receipt(
                action="Staked",
                stake_symbol=fc.SEED_SYMBOL, stake_emoji=fc.SEED_EMOJI,
                delta_h=to_human(int(res.staked_now_raw)),
                total_h=to_human(int(res.total_staked_raw)),
                stake_oracle=seed_oracle,
                note=(
                    f"Earns {fc.SEED_STAKE_HRV_PER_DAY:g} HRV per SEED per day."
                ),
            ),
            mention_author=False,
        )

    @farm.command(name="unstake")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_unstake(self, ctx: DiscoContext, amount: str) -> None:
        """Unstake SEED (also pays accrued HRV)."""
        hrv_oracle, seed_oracle = await _oracle_pair(ctx)
        if amount.lower() in ("all", "everything"):
            amt_raw = 2 ** 62
        else:
            try:
                amt_raw = to_raw(float(amount))
            except ValueError:
                await ctx.reply_error("Invalid amount.")
                return
        try:
            res = await farm_svc.unstake_seed(ctx.db, ctx.guild_id, ctx.author.id, amt_raw)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        from core.framework.staking import stake_receipt
        # ``staked_now_raw`` is signed (negative on unstake); take the
        # magnitude so the receipt shows the actual unlocked amount.
        await ctx.reply(
            embed=stake_receipt(
                action="Unstaked",
                stake_symbol=fc.SEED_SYMBOL, stake_emoji=fc.SEED_EMOJI,
                delta_h=to_human(abs(int(res.staked_now_raw))),
                total_h=to_human(int(res.total_staked_raw)),
                stake_oracle=seed_oracle,
                yield_symbol=fc.HRV_SYMBOL, yield_emoji=fc.HRV_EMOJI,
                yield_paid_h=to_human(int(res.paid_yield_raw)),
                yield_oracle=hrv_oracle,
            ),
            mention_author=False,
        )

    @farm.command(name="claim")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_claim(self, ctx: DiscoContext) -> None:
        """Claim accrued HRV yield from staked SEED."""
        hrv_oracle, seed_oracle = await _oracle_pair(ctx)
        try:
            res = await farm_svc.claim_stake_yield(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        from core.framework.staking import claim_receipt
        await ctx.reply(
            embed=claim_receipt(
                yield_symbol=fc.HRV_SYMBOL, yield_emoji=fc.HRV_EMOJI,
                yield_paid_h=to_human(int(res.paid_yield_raw)),
                yield_oracle=hrv_oracle,
                stake_symbol=fc.SEED_SYMBOL, stake_emoji=fc.SEED_EMOJI,
                total_staked_h=to_human(int(res.total_staked_raw)),
                stake_oracle=seed_oracle,
            ),
            mention_author=False,
        )

    async def _open_stake_panel(self, ctx: DiscoContext) -> None:
        """Open the unified stake panel for SEED -> HRV.

        Mirrors the same buttons + layout used by ,craft stake / ,buddy
        stake / ,delve stake / ,fish stake -- one shape across every game.
        """
        from core.framework.staking import StakeAdapter, StakePanelView, StakeToken

        async def _state(c: DiscoContext) -> dict:
            state = await farm_svc.list_state(c.db, c.guild_id, c.author.id)
            staked_raw = int(state.get("seed_staked_raw") or 0)
            pending_raw = int(
                await farm_svc.accrued_stake_yield(
                    c.db, c.guild_id, c.author.id,
                ) or 0
            )
            held = await c.db.get_wallet_holding(
                c.author.id, c.guild_id,
                fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
            )
            wallet_raw = int((held or {}).get("amount") or 0)
            staked_h = to_human(staked_raw)
            daily_h = staked_h * float(fc.SEED_STAKE_HRV_PER_DAY)
            hrv_oracle, seed_oracle = await _oracle_pair(c)
            return {
                "staked_by_sym": {fc.SEED_SYMBOL: staked_raw},
                "wallet_by_sym": {fc.SEED_SYMBOL: wallet_raw},
                "stake_oracle_by_sym": {fc.SEED_SYMBOL: seed_oracle},
                "yield_oracle": hrv_oracle,
                "pending_raw": pending_raw,
                "daily_rate_raw": int(to_raw(daily_h)),
            }

        async def _stake(c: DiscoContext, raw: int, _sym: str) -> int:
            res = await farm_svc.stake_seed(
                c.db, c.guild_id, c.author.id, int(raw),
            )
            return int(res.total_staked_raw)

        async def _unstake(c: DiscoContext, raw: int, _sym: str) -> int:
            res = await farm_svc.unstake_seed(
                c.db, c.guild_id, c.author.id, int(raw),
            )
            return int(res.total_staked_raw)

        async def _claim(c: DiscoContext) -> int:
            res = await farm_svc.claim_stake_yield(
                c.db, c.guild_id, c.author.id,
            )
            return int(getattr(res, "paid_yield_raw", 0) or 0)

        adapter = StakeAdapter(
            title="\U0001F331 Farming Stake (SEED -> HRV)",
            color=C_GOLD,
            stake_tokens=[StakeToken(fc.SEED_SYMBOL, fc.SEED_EMOJI)],
            yield_symbol=fc.HRV_SYMBOL, yield_emoji=fc.HRV_EMOJI,
            get_state=_state, do_stake=_stake,
            do_unstake=_unstake, do_claim=_claim,
            note=(
                f"Stake SEED to drip HRV. Yield: "
                f"{fc.SEED_STAKE_HRV_PER_DAY:g} HRV per SEED per day."
            ),
        )
        await StakePanelView.send(ctx, adapter)

    @farm.command(name="cashout")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(fc.ACTION_COOLDOWN_S)
    async def farm_cashout(self, ctx: DiscoContext, amount: str) -> None:
        """Burn HRV to cash out to USD."""
        gid, uid = ctx.guild_id, ctx.author.id
        if amount.lower() in ("all", "everything"):
            held = await ctx.db.get_wallet_holding(uid, gid, fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL)
            amt_raw = int((held or {}).get("amount") or 0)
        else:
            try:
                amt_raw = to_raw(float(amount))
            except ValueError:
                await ctx.reply_error("Invalid amount.")
                return
        try:
            res = await farm_svc.cashout_hrv(ctx.db, gid, uid, amt_raw)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # V3 Pillar 2: farmer mastery XP scales with USD cashed out.
        try:
            from services import mastery as _mastery
            _xp = _mastery.xp_for_action(to_human(int(res.paid_usd_raw)))
            await _mastery.add_mastery(ctx.db, uid, gid, "farmer", _xp)
        except Exception:
            pass
        from core.framework.staking import cashout_receipt
        hrv_oracle, _ = await _oracle_pair(ctx)
        burned_h = to_human(int(res.burned_hrv_raw))
        oracle_after = (
            hrv_oracle * (1.0 - res.impact_pct) if hrv_oracle > 0 else 0.0
        )
        await ctx.reply(
            embed=cashout_receipt(
                burned_symbol=fc.HRV_SYMBOL, burned_emoji=fc.HRV_EMOJI,
                burned_h=burned_h,
                usd_credited_h=to_human(int(res.paid_usd_raw)),
                oracle_before=hrv_oracle,
                oracle_after=oracle_after,
                impact_pct=float(res.impact_pct),
            ),
            mention_author=False,
        )

    # -- Leaderboard ---------------------------------------------------------

    @farm.command(name="lb")
    @guild_only
    @no_bots
    @ensure_registered
    async def farm_lb(self, ctx: DiscoContext) -> None:
        """Farming leaderboard."""
        rows = await farm_svc.get_top_farmers(ctx.db, ctx.guild_id, limit=50)
        if rows:
            from core.framework.leaderboard import filter_lb_user_ids
            keep = await filter_lb_user_ids(
                ctx, [int(r.get("user_id") or 0) for r in rows],
            )
            rows = [r for r in rows if int(r.get("user_id") or 0) in keep][:10]
        await ctx.reply(embed=_lb_embed(rows, "\U0001F3C6 Top Farmers"), mention_author=False)

    # -- Help ----------------------------------------------------------------

    @farm.command(name="help", aliases=["commands"])
    @guild_only
    @no_bots
    async def farm_help(self, ctx: DiscoContext) -> None:
        """In-cog command reference for ,farm."""
        p = ctx.prefix
        embed = (
            card("\U0001F33E Farming Help", color=C_GOLD)
            .field("Field", f"`{p}farm` -- field view", False)
            .field(
                "Plot Actions",
                f"`{p}farm plant <slot> <crop>`\n"
                f"`{p}farm water [slot]`\n"
                f"`{p}farm fertilize <slot|all>`\n"
                f"`{p}farm harvest [slot]`",
                False,
            )
            .field(
                "Zones & Crops",
                f"`{p}farm zones` / `{p}farm zone <key>`\n"
                f"`{p}farm crops`",
                True,
            )
            .field(
                "Shop",
                f"`{p}farm shop`\n"
                f"`{p}farm buy plot|fertilizer|seed`\n"
                f"`{p}farm equip <key|none>`",
                True,
            )
            .field(
                "Market",
                f"`{p}farm sell <crop|all>`\n"
                f"`{p}farm process <recipe>`\n"
                f"`{p}farm bag` / `{p}farm history`",
                False,
            )
            .field(
                "Daily Contract",
                f"`{p}farm contract` -- view today's NPC order\n"
                f"`{p}farm contract turnin` -- deliver matching crops",
                False,
            )
            .field(
                "Forage",
                f"`{p}farm forage` -- wander the brambles for random loot\n"
                f"-# Free roll, 10-minute cooldown.",
                False,
            )
            .field(
                "Token Economy",
                f"`{p}farm swap <amt|all>` -- SEED -> HRV\n"
                f"`{p}farm stake/unstake/claim` -- SEED yield\n"
                f"`{p}farm cashout <amt|all>` -- HRV -> USD",
                False,
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Farming(bot))
