"""Buddy Arena Map + Tournament + Battle Consumables (helpers + views).

Helper functions and view classes for the ``,buddy map ...`` and
``,buddy tourney ...`` subcommand groups defined in cogs/buddy.py.
Originally shipped as its own ``,arena`` cog, but the project
convention is that everything buddy-related sits under ``,buddy`` --
this module now hosts the implementations and cogs/buddy.py wires
the subcommands to call them.

Exposed:
    show_arena_map(ctx)              -- ,buddy map
    do_travel(ctx, zone_id)          -- ,buddy map travel <zone>
    do_zone_battle(ctx, is_boss)     -- ,buddy map battle / boss
    show_items(ctx)                  -- ,buddy map items
    show_tournament(ctx)             -- ,buddy tourney
    tourney_start_cmd(ctx)           -- ,buddy tourney start
    tourney_fight_cmd(ctx)           -- ,buddy tourney fight

The battle UI uses a Discord ``Select`` for consumables (drop-down,
per-round CD) and bursts ~6 FPS animation frames through ``message.edit``
during attack resolution.
"""
from __future__ import annotations

import asyncio
import io
import logging
import random

import discord

from configs.buddies_config import (
    ARENA_DEFAULT_WILD_POOL,
    ARENA_ZONES,
    ARENA_REGIONS,
    BATTLE_BURST_FRAMES,
    BATTLE_CONSUMABLES,
    BATTLE_FRAME_INTERVAL_S,
    BATTLE_MAX_BURSTS_PER_BATTLE,
    TOURNAMENT_BRACKET,
    ZONE_BATTLE_COOLDOWN_S,
    ZONE_BOSS_SPECIES,
    ZONE_WILD_POOLS,
    battle_consumable,
    effective_level,
)
from core.config import Config
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.scale import to_human
from core.framework.ui import (
    C_ERROR, C_GOLD, C_INFO, C_SUCCESS, C_WARNING,
    fmt_token, fmt_usd,
)
from services import buddy_arena_map as _map
from services import buddy_consumables as _cons
from services import mastery as _mastery
from services.buddy_arena_render import (
    render_arena_map,
    render_tournament_bracket,
)
from services.buddy_battle import (
    PVE_SPECIAL_COST,
    PVE_STAMINA_MAX,
    PveActionState,
    StepBattle,
    finalize_step_battle,
    step_round_with_player_action,
)
from services.buddy_battle_scene import (
    render_attack_burst,
    render_battle_frame,
)

log = logging.getLogger(__name__)


_BATTLE_BURST_SEMAPHORE = asyncio.Semaphore(4)


# ── Helpers ───────────────────────────────────────────────────────────

async def _active_buddy(ctx: DiscoContext) -> dict | None:
    """Best-effort fetch of the user's active buddy row."""
    return await ctx.db.fetch_one(
        "SELECT * FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 "
        "  AND status = 'owned' AND is_active "
        "LIMIT 1",
        ctx.guild_id, ctx.author.id,
    )


def _synth_opponent(zone: dict, player_level: int, *, zone_id: str = "") -> dict:
    """Build a synthetic opponent row for a zone battle.

    Tier-matched against the zone's [tier_min, tier_max] band, biased
    a touch by player_level so well-levelled players see a real fight.
    Species are drawn from ``ZONE_WILD_POOLS[zone_id]`` so the zone's
    theme drives what shows up -- no more shrimp on a mountain.
    """
    tmin = max(1, int(zone.get("tier_min") or 1))
    tmax = max(tmin, int(zone.get("tier_max") or tmin + 2))
    foe_lvl = max(tmin, min(tmax, int(round((tmin + tmax) / 2))))
    # Boss zones force a fixed species + rarity tier so the player
    # encounters the same intimidating opponent each attempt. The
    # boss_zone_id stamp on the row is what makes Fighter.from_rows
    # swap in the variant's named ability and the portrait renderer
    # paint the unique overlay (crown / antlers / flame mane / etc.).
    if zone.get("boss"):
        boss_species = (
            zone.get("boss_species")
            or ZONE_BOSS_SPECIES.get(zone_id)
            or "wolf"
        )
        from configs.buddies_config import boss_variant as _bv
        variant = _bv(zone_id)
        name = str(variant.get("display_name") or f"Boss {str(boss_species).title()}")
        return {
            "id":            -1,
            "species":       str(boss_species),
            "name":          name,
            "rarity_tier":   3,
            "level":         int(zone.get("tier_max") or foe_lvl),
            "xp":            0,
            "hunger":        80,
            "happiness":     80,
            "energy":        80,
            "hp_alloc":      0,
            "atk_alloc":     0,
            "spd_alloc":     0,
            "gear":          {},
            "boss_zone_id":  str(zone_id),
        }
    pool = ZONE_WILD_POOLS.get(zone_id) or ARENA_DEFAULT_WILD_POOL
    species = random.choice(pool) if pool else "zenny"
    return {
        "id":            -1,
        "species":       species,
        "name":          f"Wild {species.title()}",
        "rarity_tier":   1,
        "level":         foe_lvl,
        "xp":            0,
        "hunger":        80,
        "happiness":     80,
        "energy":        80,
        "hp_alloc":      0,
        "atk_alloc":     0,
        "spd_alloc":     0,
        "gear":          {},
    }


def _zone_color(zone_id: str) -> int:
    z = ARENA_ZONES.get(zone_id, {})
    return int(ARENA_REGIONS.get(z.get("region", ""), {}).get("theme_color")
               or C_INFO)


def _battle_state(b: StepBattle, *, zone_id: str) -> dict:
    """Build the dict consumed by services/buddy_battle_scene.render_*."""
    return {
        "p1_row": _fighter_as_row(b.f1),
        "p2_row": _fighter_as_row(b.f2),
        "p1_hp": int(b.f1.hp), "p1_max_hp": int(b.f1.max_hp),
        "p2_hp": int(b.f2.hp), "p2_max_hp": int(b.f2.max_hp),
        "p1_status_icons": _status_icons(b.f1),
        "p2_status_icons": _status_icons(b.f2),
        "round": int(b.round_num),
        "max_rounds": 30,
        "zone_id": str(zone_id),
        "action_banner": "",
        "is_player_turn": False,
    }


def _fighter_as_row(f) -> dict:
    """Adapt a Fighter back to the row-shaped dict the portrait renderer wants."""
    return {
        "id":            f.id,
        "species":       f.species,
        "name":          f.name,
        "level":         f.level,
        "rarity_tier":   f.tier,
        "hunger":        80, "happiness": 80, "energy": 80,
        "gear":          {},
        "boss_zone_id":  str(getattr(f, "boss_zone_id", "") or ""),
    }


def _build_zone_intro_embed(
    ctx: DiscoContext,
    buddy: dict,
    opponent: dict,
    zone_id: str,
    *,
    is_boss: bool,
    progress: dict,
    inventory: dict[str, int],
    repeat_clear: bool,
) -> "discord.Embed":
    """Build the rich pre-fight embed for ``,buddy map battle`` / ``boss``.

    Mirrors the layout of ``cogs/buddy.py:_build_arena_intro_embed`` so
    the map metagame surfaces the same shape of info as the arena:
    both fighters' stat blocks, the zone tier band, region progress,
    the BUD + BBT win-reward preview, item drop hint, and the combat
    move cheat sheet. Used by ``do_zone_battle`` immediately before the
    battle scene PNG is composed.
    """
    # Lazy import: _fighter_field lives in cogs/buddy.py (where the
    # arena flow uses it), and cogs/buddy.py also lazy-imports from
    # this module -- top-level cross-import would be circular.
    from cogs.buddy import _fighter_field

    zone = ARENA_ZONES.get(zone_id, {})
    bud_meta = Config.TOKENS.get("BUD", {}) or {}
    bbt_meta = Config.TOKENS.get("BBT", {}) or {}
    bud_emoji = str(bud_meta.get("emoji") or "")
    bbt_emoji = str(bbt_meta.get("emoji") or "")

    bud_h, bbt_h = _map.zone_rewards_human(zone, first_clear=not repeat_clear)
    # Boss vs zone-clear title + flavor.
    zone_name = str(zone.get("name") or "Zone")
    region_key = str(zone.get("region") or "")
    region_meta = ARENA_REGIONS.get(region_key, {})
    region_label = str(region_meta.get("label") or region_key.title())
    tier_min = int(zone.get("tier_min") or 1)
    tier_max = int(zone.get("tier_max") or tier_min + 2)

    if is_boss:
        from configs.buddies_config import boss_variant as _bv
        bv = _bv(zone_id)
        if bv.get("display_name"):
            title = f"\U0001F47A {bv['display_name']}  -  {zone_name}"
        else:
            title = f"\U0001F47A Boss Battle  -  {zone_name}"
    else:
        title = f"\U0001F3DE️ Zone Battle  -  {zone_name}"

    boss_title_line = ""
    if is_boss:
        from configs.buddies_config import boss_variant as _bv
        bv = _bv(zone_id)
        if bv.get("title"):
            boss_title_line = f"**{bv['title']}**\n"

    desc = (
        boss_title_line
        + f"_{zone.get('tagline', '')}_\n"
        "Pick an item from the dropdown or hit **Attack** to swing. "
        "**Forfeit** ends the run without the cooldown reward."
    )

    p_name, p_block = _fighter_field(
        dict(buddy), owner_name=ctx.author.display_name,
    )
    op_owner = "Zone Boss" if is_boss else "Wild"
    o_name, o_block = _fighter_field(dict(opponent), owner_name=op_owner)

    # Zone tier band -- shows opponent level range vs your buddy level.
    buddy_lvl = int(effective_level(buddy))
    zone_line = (
        f"**{region_label}**  -  L{tier_min}-{tier_max} band\n"
        f"-# Your buddy: **L{buddy_lvl}**"
    )
    if is_boss:
        zone_line += "  -  opponent forced to L{} (boss)".format(tier_max)

    # Region progress -- cleared zones in the current region, plus the
    # cross-region tournament gate so the player can see how close they
    # are to qualifying for the bracket.
    cleared = set(progress.get("cleared_zones") or [])
    region_zones = [
        zid for zid, zd in ARENA_ZONES.items()
        if str(zd.get("region") or "") == region_key
    ]
    region_cleared = sum(1 for zid in region_zones if zid in cleared)
    region_total = len(region_zones)
    bosses_done = sum(
        1 for r_key, r_meta in ARENA_REGIONS.items()
        if str(r_meta.get("boss_zone") or "") in cleared
    )
    region_line = (
        f"This region: **{region_cleared} / {region_total}** zones\n"
        f"Region bosses: **{bosses_done} / {len(ARENA_REGIONS)}** "
        "for tournament gate"
    )

    # Reward preview. Shows BUD + BBT for THIS clear (first vs repeat).
    # Bosses get a flat bonus on top; item drop is 100% on first clear,
    # 30% (+ mastery luck) on repeats.
    reward_lines = [
        (
            f"+{fmt_token(bud_h, 'BUD', bud_emoji)}  -  "
            f"+{fmt_token(bbt_h, 'BBT', bbt_emoji)}"
        ),
    ]
    if repeat_clear:
        reward_lines.append(
            f"-# Repeat clear -- {int(_map.ZONE_REPEAT_CLEAR_FRACTION * 100)}% "
            f"of first-clear payout."
        )
    else:
        reward_lines.append(
            "-# First clear -- full payout, guaranteed item drop."
        )
    if zone.get("boss"):
        reward_lines.append(
            f"-# Boss cherry: +{_map.ZONE_BOSS_BUD_BONUS:.0f} BUD, "
            f"+{_map.ZONE_BOSS_BBT_BONUS:.0f} BBT baked in."
        )
    drop_key = str(zone.get("item_drop") or "")
    if drop_key:
        item_meta = BATTLE_CONSUMABLES.get(drop_key, {}) or {}
        drop_chance = "100%" if not repeat_clear else "30%"
        reward_lines.append(
            f"Drop: {item_meta.get('emoji', '')} "
            f"**{item_meta.get('name', drop_key)}** "
            f"({drop_chance} chance)"
        )

    # Surface the buddy's actual ability name so the player can see which
    # spell their Special button casts -- matches the PvP / arena flow.
    from configs.buddies_config import SPECIES as _SPECIES
    ability_name = str(
        _SPECIES.get(str(buddy.get("species") or ""), {}).get("ability_name")
        or "Special"
    )
    combat_line = (
        f"**Strike** -- swing (+1 stamina)\n"
        f"**{ability_name}** -- 2 stamina, big damage\n"
        f"**Brace** -- heal 8%, halve next hit\n"
        f"**Risky** -- big swing, 25% miss / 15% backfire\n"
        f"**Item** -- pick from your bag (per-round CD)"
    )

    color = C_GOLD if is_boss else _zone_color(zone_id)

    builder = (
        card(title, color=color)
        .description(desc)
        .field(p_name, p_block, True)
        .field(o_name, o_block, True)
        .field("Zone", zone_line, True)
        .field(
            "Win Reward (BUD + BBT)",
            "\n".join(reward_lines),
            False,
        )
        .field("Region Progress", region_line, True)
        .field("Combat", combat_line, True)
        .field(
            "Bag",
            f"{sum(inventory.values())} consumable(s) ready",
            True,
        )
        .image("attachment://battle.png")
        .footer(
            f"Round 1 / 30  -  Zone cooldown {ZONE_BATTLE_COOLDOWN_S}s. "
            "Rewards are paid in BUD + BBT only."
        )
    )
    return builder.build()


def _status_icons(f) -> list[str]:
    out: list[str] = []
    if f.poison_turns > 0:
        out.append("poison")
    if f.stunned_turns > 0:
        out.append("stunned")
    if f.regen_pct > 0:
        out.append("regen")
    return out


# ── Consumable dropdown ───────────────────────────────────────────────

class _ConsumableSelect(discord.ui.Select):
    """Per-round 'pick an item' dropdown.

    Disabled options (CD > 0) still render so the player can see what
    they own; Discord greys them automatically when ``description``
    starts with "(CD ...)".
    """

    def __init__(self, view: "_PveBattleView") -> None:
        opts = _cons.selectable_options(
            view.battle.f1,
            view.inventory,
            max_options=25,
        )
        choices: list[discord.SelectOption] = []
        for o in opts:
            choices.append(discord.SelectOption(
                label=o["label"][:100],
                value=o["key"],
                description=o["description"][:100],
                default=False,
            ))
        if not choices:
            choices.append(discord.SelectOption(
                label="(no items)", value="__none__",
                description="Craft battle consumables to use them.",
            ))
        super().__init__(
            placeholder="Use an item (or attack)",
            min_values=1, max_values=1,
            options=choices, custom_id="arena_battle_item",
            row=0,
            disabled=not bool(choices) or choices[0].value == "__none__",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        v: _PveBattleView = self.view  # type: ignore[assignment]
        if interaction.user.id != v.owner_id:
            await interaction.response.send_message(
                "Not your battle.", ephemeral=True,
            )
            return
        chosen = self.values[0]
        if chosen == "__none__":
            await interaction.response.defer()
            return
        await v.on_item_used(interaction, chosen)


class _ActionButton(discord.ui.Button):
    """One of the four combat actions (Strike / Special / Brace / Risky).

    Same vocabulary as the PvP view in cogs/buddy.py so the player's
    move set is identical across every battle type. Special is greyed
    out when the player lacks the stamina to cast it -- the view rebuilds
    these buttons each round via ``_refresh_items``.
    """

    def __init__(
        self, *, action_key: str, label: str, emoji: str,
        style: discord.ButtonStyle, disabled: bool = False, row: int = 1,
    ) -> None:
        super().__init__(
            label=label, emoji=emoji, style=style, row=row,
            disabled=disabled,
        )
        self.action_key = action_key

    async def callback(self, interaction: discord.Interaction) -> None:
        v: _PveBattleView = self.view  # type: ignore[assignment]
        if interaction.user.id != v.owner_id:
            await interaction.response.send_message(
                "Not your battle.", ephemeral=True,
            )
            return
        await v.on_action(interaction, self.action_key)


class _ForfeitButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Forfeit",
            style=discord.ButtonStyle.secondary,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        v: _PveBattleView = self.view  # type: ignore[assignment]
        if interaction.user.id != v.owner_id:
            await interaction.response.send_message(
                "Not your battle.", ephemeral=True,
            )
            return
        await v.on_forfeit(interaction)


class _CaptureButton(discord.ui.Button):
    """Capture the active opponent. Enabled only at low HP.

    Wild captures unlock at <=20% HP, boss captures at <=5% HP. After
    a failed attempt the button is locked for 3 rounds to prevent
    spam-tapping a fight back from the brink.
    """
    def __init__(self, *, enabled: bool, is_boss: bool) -> None:
        super().__init__(
            label=("Tame Boss" if is_boss else "Capture"),
            style=discord.ButtonStyle.success,
            emoji="\U0001F4AB",
            row=2,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        v: _PveBattleView = self.view  # type: ignore[assignment]
        if interaction.user.id != v.owner_id:
            await interaction.response.send_message(
                "Not your battle.", ephemeral=True,
            )
            return
        await v.on_capture(interaction)


# ── Battle view ───────────────────────────────────────────────────────

class _PveBattleView(discord.ui.View):
    """Interactive PvE battle view with per-round consumable dropdown."""

    def __init__(
        self,
        *,
        owner_id: int,
        ctx: DiscoContext,
        battle: StepBattle,
        zone_id: str,
        is_boss: bool,
        is_tournament: bool,
        inventory: dict[str, int],
        mastery_passives: dict[str, float],
    ) -> None:
        super().__init__(timeout=300.0)
        self.owner_id = int(owner_id)
        self.ctx = ctx
        self.battle = battle
        self.zone_id = str(zone_id)
        self.is_boss = bool(is_boss)
        self.is_tournament = bool(is_tournament)
        self.inventory = dict(inventory)
        self.mastery_passives = dict(mastery_passives or {})
        # Shared PvE action state -- same shape as the PvP _PvpBattle
        # stamina/brace pair so abilities feel identical across battle
        # types. Endurance Charm seed bonus carries over from gear.
        self.action_state = PveActionState(
            stamina=min(
                PVE_STAMINA_MAX,
                int(getattr(battle.f1, "start_stamina_bonus", 0) or 0),
            ),
        )
        self.message: discord.Message | None = None
        self.burst_count = 0
        self._lock = asyncio.Lock()
        # Capture button lockout: failed attempts disable it for N rounds.
        self.capture_cd_until_round: int = 0
        # Tournament battles never offer capture; only zone wilds + bosses.
        self.capture_enabled_overall: bool = not bool(is_tournament)
        self._refresh_items()

    def _refresh_items(self) -> None:
        """Rebuild the children list (dropdown + action buttons + forfeit).

        Special is auto-disabled when stamina is short so the player can
        see at a glance which moves are available. Same pattern the PvP
        view uses, just rebuilt each round.
        """
        for child in list(self.children):
            self.remove_item(child)
        self.add_item(_ConsumableSelect(self))
        self.add_item(_ActionButton(
            action_key="strike", label="Strike", emoji="⚔️",
            style=discord.ButtonStyle.primary, row=1,
        ))
        self.add_item(_ActionButton(
            action_key="special",
            label=(self.battle.f1.ability_name or "Special")[:20],
            emoji="\U0001F4A5",
            style=discord.ButtonStyle.success,
            disabled=self.action_state.stamina < PVE_SPECIAL_COST,
            row=1,
        ))
        self.add_item(_ActionButton(
            action_key="brace", label="Brace", emoji="\U0001F6E1️",
            style=discord.ButtonStyle.secondary, row=1,
        ))
        self.add_item(_ActionButton(
            action_key="risky", label="Risky", emoji="\U0001F3AF",
            style=discord.ButtonStyle.danger, row=1,
        ))
        self.add_item(_ForfeitButton())
        if self.capture_enabled_overall:
            self.add_item(_CaptureButton(
                enabled=self._capture_available(),
                is_boss=self.is_boss,
            ))

    def _capture_available(self) -> bool:
        """True when the Capture button should be clickable this round.

        Wild captures unlock at HP <= 20% of max; boss captures at
        HP <= 5%. A failed attempt locks the button until the round
        counter advances past ``capture_cd_until_round``.
        """
        if not self.capture_enabled_overall:
            return False
        if self.battle.round_num < self.capture_cd_until_round:
            return False
        f2 = self.battle.f2
        if f2.max_hp <= 0 or f2.hp <= 0:
            return False
        hp_pct = f2.hp / f2.max_hp
        ceiling = 0.05 if self.is_boss else 0.20
        return hp_pct <= ceiling

    async def on_item_used(
        self, interaction: discord.Interaction, item_key: str,
    ) -> None:
        async with self._lock:
            # Defensive: a prior round may have already killed someone
            # but the resolve coroutine hasn't finished yet. Don't run
            # another round on a dead fighter.
            if self.battle.over or self.battle.f1.hp <= 0 or self.battle.f2.hp <= 0:
                await interaction.response.defer()
                if not self.is_finished():
                    await self._resolve_battle(forfeited=False)
                return
            meta = battle_consumable(item_key)
            if not meta:
                await interaction.response.send_message(
                    "Unknown item.", ephemeral=True,
                )
                return
            qty = int(self.inventory.get(item_key) or 0)
            if qty <= 0:
                await interaction.response.send_message(
                    "Out of stock.", ephemeral=True,
                )
                return
            ok, reason = _cons.can_use(self.battle.f1, item_key)
            if not ok:
                await interaction.response.send_message(
                    f"Can't use that: {reason}", ephemeral=True,
                )
                return

            result = _cons.apply(
                self.battle.f1, self.battle.f2, item_key,
                mastery_passives=self.mastery_passives,
            )
            if not result.ok:
                await interaction.response.send_message(
                    f"Can't use: {result.reason}", ephemeral=True,
                )
                return

            # Decrement inventory + set CD
            self.inventory[item_key] = qty - 1
            if self.inventory[item_key] <= 0:
                self.inventory.pop(item_key, None)
            await _map.consume_battle_item(
                self.ctx.db, self.ctx.guild_id, self.owner_id, item_key,
            )
            _cons.set_cd(self.battle.f1, item_key, self.mastery_passives)

            self.battle.log_lines.append(result.log_line)

            await interaction.response.defer()
            await self._play_item_animation(item_key, result)
            await self._after_player_action(action="strike", item_used=item_key)

    async def on_action(
        self, interaction: discord.Interaction, action_key: str,
    ) -> None:
        """Player picked Strike / Special / Brace / Risky for this round."""
        async with self._lock:
            # Defensive: never run another round when the battle is
            # already won/lost. Without this guard the previous round's
            # killing blow could leave the view "alive" for one extra
            # click before resolving -- the exact bug reported by
            # players ("hp is 0 but it makes me hit them again").
            if self.battle.over or self.battle.f1.hp <= 0 or self.battle.f2.hp <= 0:
                await interaction.response.defer()
                if not self.is_finished():
                    await self._resolve_battle(forfeited=False)
                return
            # Guard: Special with no stamina (shouldn't happen because the
            # button is disabled, but mirror the PvP refusal anyway).
            if action_key == "special" and self.action_state.stamina < PVE_SPECIAL_COST:
                await interaction.response.send_message(
                    f"Need {PVE_SPECIAL_COST} stamina for "
                    f"{self.battle.f1.ability_name or 'Special'} "
                    f"(you have {self.action_state.stamina}).",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            # Focus Berry pre-arms a guaranteed crit on the next swing,
            # whether it's a Strike / Special / Risky.
            if _cons.crit_next_pending(self.battle.f1) and action_key != "brace":
                self.battle.f1.first_strike_pending = True
            await self._play_action_animation(action_key)
            await self._after_player_action(action=action_key, item_used=None)

    async def on_forfeit(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            await interaction.response.defer()
            self.battle.f1.hp = 0
            self.battle.over = True
            await self._resolve_battle(forfeited=True)

    async def on_capture(self, interaction: discord.Interaction) -> None:
        """Attempt to tame the active opponent.

        On success: ends the battle (replacing the kill reward with a
        new buddy added to the roster). On failure: locks the capture
        button for 3 rounds and continues with a default Strike round
        so the enemy still gets a swing.
        """
        async with self._lock:
            await interaction.response.defer()
            if not self._capture_available():
                # Disabled buttons shouldn't fire, but mirror the PvP
                # refusal so a stale interaction is safe.
                return
            from services import buddy_capture as _cap
            f2 = self.battle.f2
            hp_pct = max(0.0, f2.hp / max(1, f2.max_hp))
            luck = float(self.mastery_passives.get("luck.rare_catch") or 0.0)
            result = await _cap.attempt_arena_capture(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
                species=str(f2.species or "fox"),
                level=int(f2.level or 1),
                rarity_tier=int(f2.tier or 1),
                is_boss=bool(self.is_boss),
                hp_pct=hp_pct,
                luck_bonus=luck,
                zone_id=self.zone_id,
            )
            if result.success:
                self.battle.log_lines.append(
                    f"\U00002728 Captured! **{result.species.title()}** "
                    f"L{result.level} joins your roster (rolled "
                    f"{int(result.chance * 100)}%)."
                )
                # Reward replaces the kill -- still mark zone activity
                # and end the battle. Don't grant XP/BUD for a capture.
                self.battle.f2.hp = 0
                self.battle.over = True
                await self._resolve_capture(result)
            else:
                self.battle.log_lines.append(
                    f"\U0001F4A8 Capture failed -- {result.reason}"
                )
                self.capture_cd_until_round = self.battle.round_num + 3
                # Run a default Strike round so the enemy still acts.
                await self._after_player_action(action="strike", item_used=None)

    async def _resolve_capture(self, result) -> None:
        """End-of-battle path when the player tamed the opponent.

        Mirrors _resolve_battle but skips XP / BUD rewards and shows a
        capture-themed final card. Still records the zone-battle stamp
        so the standard cooldown applies.
        """
        await _map.mark_zone_battle(
            self.ctx.db, self.ctx.guild_id, self.owner_id,
        )
        bits = [
            f"\U0001F4AB **Captured!**  Wild "
            f"**{result.species.title()}** L{result.level} joins your "
            f"roster ({int(result.chance * 100)}% roll).",
            "Visit `,buddy storage` to swap them into your active slots.",
        ]
        msg = "\n".join(bits)
        color = C_SUCCESS
        if self.message is not None:
            try:
                state = _battle_state(self.battle, zone_id=self.zone_id)
                state["action_banner"] = "TAMED!"
                png = render_battle_frame(state)
                file = discord.File(io.BytesIO(png), filename="capture.png")
                embed = (
                    card(self._title_for_embed(), color=color)
                    .description(msg)
                    .image("attachment://capture.png")
                    .footer(f"Rounds played: {self.battle.round_num}")
                    .build()
                )
                self.clear_items()
                await self.message.edit(
                    embed=embed, attachments=[file], view=self,
                )
            except discord.HTTPException as exc:
                log.debug("capture final edit failed: %s", exc)
        self.stop()

    async def _after_player_action(
        self, *, action: str | None, item_used: str | None,
    ) -> None:
        """Run one engine round and update the UI.

        ``action`` is set when the player picked a combat move; ``None``
        when only an item was used (the round still ticks via a default
        Strike so the enemy gets a swing in).
        """
        new_lines = step_round_with_player_action(
            self.battle, action or "strike", self.action_state,
        )
        _ = new_lines  # already appended to b.log_lines by the engine
        # Decrement consumable CDs at round end
        _cons.tick_cd(self.battle.f1, self.mastery_passives)
        # Tick temp atk/def buffs
        revert_lines = _cons.consume_timed_buffs(self.battle.f1)
        self.battle.log_lines.extend(revert_lines)
        # Phoenix Tear auto-revive
        if self.battle.f1.hp <= 0:
            revive = _cons.revive_if_armed(self.battle.f1)
            if revive:
                self.battle.log_lines.append(revive)

        if self.battle.over or self.battle.f1.hp <= 0 or self.battle.f2.hp <= 0:
            await self._resolve_battle(forfeited=False)
            return

        # Render the new static round frame
        self._refresh_items()
        await self._edit_scene(action_banner="")

    async def _play_item_animation(self, item_key: str, result) -> None:
        """Short FPS burst for an item use."""
        if self.burst_count >= BATTLE_MAX_BURSTS_PER_BATTLE:
            return
        self.burst_count += 1
        meta = battle_consumable(item_key) or {}
        banner = f"{meta.get('emoji', '')} {meta.get('name', item_key)}".strip()
        async with _BATTLE_BURST_SEMAPHORE:
            try:
                for i in range(BATTLE_BURST_FRAMES):
                    state = _battle_state(self.battle, zone_id=self.zone_id)
                    state["action_banner"] = banner
                    state["acting_side"] = "p1"
                    png = render_attack_burst(state, i, total_frames=BATTLE_BURST_FRAMES)
                    await self._edit_png(png, "battle.png")
                    await asyncio.sleep(BATTLE_FRAME_INTERVAL_S)
            except discord.HTTPException as exc:
                log.debug("FPS burst hit Discord error: %s", exc)

    async def _play_action_animation(self, action_key: str) -> None:
        """Short burst keyed to the player's chosen action.

        Strike / Special / Risky show the attack-burst frames pointed at
        the enemy; Brace shows a defensive flash on the player side.
        """
        if self.burst_count >= BATTLE_MAX_BURSTS_PER_BATTLE:
            return
        self.burst_count += 1
        banner_map = {
            "strike":  "STRIKE",
            "special": (self.battle.f1.ability_name or "SPECIAL").upper()[:18],
            "brace":   "BRACE",
            "risky":   "RISKY",
        }
        banner = banner_map.get(action_key, "ATTACK")
        is_brace = action_key == "brace"
        async with _BATTLE_BURST_SEMAPHORE:
            try:
                for i in range(BATTLE_BURST_FRAMES):
                    state = _battle_state(self.battle, zone_id=self.zone_id)
                    state["action_banner"] = banner
                    state["acting_side"] = "p1"
                    if not is_brace:
                        state["hit_side"] = "p2"
                    png = render_attack_burst(state, i, total_frames=BATTLE_BURST_FRAMES)
                    await self._edit_png(png, "battle.png")
                    await asyncio.sleep(BATTLE_FRAME_INTERVAL_S)
            except discord.HTTPException as exc:
                log.debug("FPS burst hit Discord error: %s", exc)

    async def _edit_scene(self, *, action_banner: str = "") -> None:
        state = _battle_state(self.battle, zone_id=self.zone_id)
        state["action_banner"] = action_banner
        state["is_player_turn"] = True
        png = render_battle_frame(state)
        await self._edit_png(png, "battle.png")

    async def _edit_png(self, png_bytes: bytes, filename: str) -> None:
        if self.message is None:
            return
        try:
            file = discord.File(io.BytesIO(png_bytes), filename=filename)
            embed = (
                card(self._title_for_embed(),
                     color=_zone_color(self.zone_id))
                .description(self._round_description())
                .image(f"attachment://{filename}")
                .footer(self._footer_line())
                .build()
            )
            await self.message.edit(
                embed=embed, attachments=[file], view=self,
            )
        except discord.HTTPException as exc:
            log.debug("battle edit failed: %s", exc)

    def _title_for_embed(self) -> str:
        z = ARENA_ZONES.get(self.zone_id, {})
        if self.is_tournament:
            return "Champion Tournament"
        if self.is_boss:
            return f"Boss Battle  -  {z.get('name', self.zone_id)}"
        return f"Zone Battle  -  {z.get('name', self.zone_id)}"

    def _round_description(self) -> str:
        z = ARENA_ZONES.get(self.zone_id, {})
        last_log = "\n".join(self.battle.log_lines[-6:]) or "_The fight begins._"
        return (
            f"_{z.get('tagline', '')}_\n\n{last_log[:1500]}"
        )

    def _footer_line(self) -> str:
        stam_pips = (
            "●" * self.action_state.stamina
            + "○" * (PVE_STAMINA_MAX - self.action_state.stamina)
        )
        brace_tag = "  -  \U0001F6E1️ BRACED" if self.action_state.brace_next else ""
        return (
            f"Round {self.battle.round_num} / 30  -  "
            f"Stamina {stam_pips} ({self.action_state.stamina}/{PVE_STAMINA_MAX})  -  "
            f"Items: {sum(self.inventory.values())}{brace_tag}"
        )

    async def _resolve_battle(self, *, forfeited: bool) -> None:
        """Finalize the battle, persist rewards, edit the message to end state."""
        result = finalize_step_battle(self.battle)
        won = (result.winner is self.battle.f1) and not forfeited

        # Award XP on win. Apex Mastery: Arena Veteran (combat.tourney_xp)
        # scales the XP award on both zone and tournament victories.
        if won and result.xp_award > 0 and self.battle.f1.id > 0:
            try:
                from services import buddy_battle as _bb
                xp = int(result.xp_award)
                tourney_xp = float(self.mastery_passives.get("combat.tourney_xp") or 0.0)
                if tourney_xp > 0:
                    xp = max(1, int(round(xp * (1.0 + tourney_xp))))
                await _bb.award_battle_xp(
                    self.ctx.db, self.ctx.guild_id,
                    self.owner_id, int(self.battle.f1.id), int(xp),
                )
            except Exception:
                log.debug("award_battle_xp failed in arena", exc_info=True)

        # Tournament path: forward result to advance_tournament
        if self.is_tournament:
            adv = await _map.advance_tournament(
                self.ctx.db, self.ctx.guild_id, self.owner_id, victory=won,
            )
            if won:
                # Credit reward via the existing economy path -- the helper
                # lives on services.buddy_economy but we keep this cog free
                # of that import chain by posting an embed-level summary.
                msg = (
                    f"**{adv.label}** cleared!  Reward: {fmt_usd(adv.reward_usd)}"
                )
                if adv.champion:
                    msg += "  -  **CHAMPION**!"
                color = C_GOLD if adv.champion else C_SUCCESS
            else:
                msg = f"Eliminated in **{adv.label}**. Bracket reset."
                color = C_ERROR
        else:
            # Zone / boss battle
            await _map.mark_zone_battle(
                self.ctx.db, self.ctx.guild_id, self.owner_id,
            )
            if won:
                drop_bonus = float(self.mastery_passives.get("luck.zone_drops") or 0.0)
                cleared = await _map.on_zone_cleared(
                    self.ctx.db, self.ctx.guild_id, self.owner_id,
                    self.zone_id,
                    rounds_remaining=max(0, 30 - self.battle.round_num),
                    zone_drop_bonus=drop_bonus,
                    is_boss_clear=self.is_boss,
                )
                bud_meta = Config.TOKENS.get("BUD", {}) or {}
                bbt_meta = Config.TOKENS.get("BBT", {}) or {}
                bud_emoji = str(bud_meta.get("emoji") or "")
                bbt_emoji = str(bbt_meta.get("emoji") or "")
                bud_h = to_human(int(cleared.bud_reward_raw))
                bbt_h = to_human(int(cleared.bbt_reward_raw))
                reward_tag = (
                    f"+{fmt_token(bud_h, 'BUD', bud_emoji)}  -  "
                    f"+{fmt_token(bbt_h, 'BBT', bbt_emoji)}"
                )
                bits = [f"**Victory!**  {reward_tag}"]
                if not cleared.first_clear:
                    bits.append(
                        f"-# Repeat clear -- "
                        f"{int(_map.ZONE_REPEAT_CLEAR_FRACTION * 100)}% payout."
                    )
                if cleared.item_drop:
                    item_meta = battle_consumable(cleared.item_drop) or {}
                    bits.append(
                        f"You found {item_meta.get('emoji', '')} "
                        f"**{item_meta.get('name', cleared.item_drop)}**!"
                    )
                if cleared.region_completed:
                    bits.append(
                        f"Region cleared: **{cleared.region_completed.title()}**."
                    )
                if cleared.tournament_unlocked:
                    bits.append(
                        "**Champion Tournament unlocked!**  Run `,tourney start`."
                    )
                msg = "\n".join(bits)
                color = C_SUCCESS
            else:
                msg = "**Defeated.**  Heal up and try again."
                color = C_WARNING if forfeited else C_ERROR

        if self.message is not None:
            try:
                state = _battle_state(self.battle, zone_id=self.zone_id)
                state["action_banner"] = "VICTORY!" if won else "K.O."
                png = render_battle_frame(state)
                file = discord.File(io.BytesIO(png), filename="battle_final.png")
                embed = (
                    card(self._title_for_embed(), color=color)
                    .description(msg)
                    .image("attachment://battle_final.png")
                    .footer(f"Rounds played: {self.battle.round_num}")
                    .build()
                )
                # Clear the view so buttons disappear
                self.clear_items()
                await self.message.edit(
                    embed=embed, attachments=[file], view=self,
                )
            except discord.HTTPException as exc:
                log.debug("final edit failed: %s", exc)
        self.stop()


# ── Public command implementations (called by cogs/buddy.py) ─────────
#
# These were originally a separate Cog with their own ``,arena`` and
# ``,tourney`` top-level groups; that turned out to be the wrong shape
# (the project convention is everything buddy-related sits under the
# existing ``,buddy`` group). The implementations now live here as
# plain async functions and the buddy cog wires them up as
# ``,buddy map ...`` and ``,buddy tourney ...`` subcommands.


async def show_arena_map(ctx: DiscoContext) -> None:
    """Render the arena map PNG and panel embed. ``,buddy map``."""
    progress = await _map.get_progress(ctx.db, ctx.guild_id, ctx.author.id)
    passives = await _mastery.passives(ctx.db, ctx.author.id, ctx.guild_id)
    hidden: set[str] = set()
    if passives.get("luck.rare_catch", 0) > 0:
        hidden.add("moonlit_pool")
    png = render_arena_map(progress, can_use_hidden=hidden)
    file = discord.File(io.BytesIO(png), filename="arena_map.png")

    zone = ARENA_ZONES.get(progress.get("current_zone_id") or "", {})
    neighbors = zone.get("neighbors") or []
    nb_list = "\n".join(
        f"- `{n}`  ({ARENA_ZONES.get(n, {}).get('name', n)})"
        for n in neighbors
    ) or "_No outbound paths._"
    embed = (
        card("Buddy Arena Map", color=_zone_color(progress.get("current_zone_id") or ""))
        .description(
            f"**Current:** {zone.get('name', '???')}\n"
            f"_{zone.get('tagline', '')}_"
        )
        .field("Travel to", nb_list, False)
        .field(
            "Cleared",
            f"{len(progress.get('cleared_zones') or [])} / {len(ARENA_ZONES)}",
            True,
        )
        .field(
            "Tournament",
            str(progress.get("tournament_state") or "locked").title(), True,
        )
        .field(
            "Champion runs",
            f"{int(progress.get('champion_count') or 0)}", True,
        )
        .image("attachment://arena_map.png")
        .footer(
            "`,buddy map travel <zone>`  -  `,buddy map battle`  -  "
            "`,buddy map boss`  -  `,buddy map items`  -  `,buddy tourney`"
        )
        .build()
    )
    await ctx.reply(embed=embed, file=file, mention_author=False)


async def do_travel(ctx: DiscoContext, zone_id: str) -> None:
    """Travel to a neighbouring zone. ``,buddy map travel <zone_id>``"""
    buddy = await _active_buddy(ctx)
    if not buddy:
        await ctx.reply_error("You need an active buddy to travel.")
        return
    level = int(effective_level(buddy))
    passives = await _mastery.passives(ctx.db, ctx.author.id, ctx.guild_id)
    skip = int(passives.get("combat.zone_travel") or 0)
    hidden: set[str] = set()
    if passives.get("luck.rare_catch", 0) > 0:
        hidden.add("moonlit_pool")

    result = await _map.travel(
        ctx.db, ctx.guild_id, ctx.author.id, zone_id.strip(),
        active_buddy_level=level,
        mastery_skip=skip,
        can_use_hidden=hidden,
    )
    if not result.ok:
        if result.cooldown_s > 0:
            await ctx.reply_cooldown(int(result.cooldown_s))
        else:
            await ctx.reply_error(result.reason)
        return
    z = ARENA_ZONES.get(result.new_zone_id, {})
    bits = [f"Arrived at **{z.get('name', result.new_zone_id)}**."]
    if result.skipped:
        bits.append("Trailblazer shortcut used.")
    bits.append(f"_{z.get('tagline', '')}_")
    await ctx.reply_success("\n".join(bits), title="Travel")


async def do_zone_battle(ctx: DiscoContext, *, is_boss: bool = False) -> None:
    """Start a zone or boss battle. ``,buddy map battle`` / ``,buddy map boss``."""
    progress = await _map.get_progress(ctx.db, ctx.guild_id, ctx.author.id)
    cur_zone_id = str(progress.get("current_zone_id") or "plains_gate")
    z = ARENA_ZONES.get(cur_zone_id, {})

    # Specials are non-combat: redirect the player to ,buddy map visit.
    if str(z.get("region") or "") == "special":
        await ctx.reply_error(
            "This is a special location -- use `,buddy map visit` here.",
        )
        return

    if is_boss:
        if not z.get("boss"):
            await ctx.reply_error(
                "This isn't a boss zone -- travel to one first.",
            )
            return
        if cur_zone_id in (progress.get("cleared_zones") or []):
            await ctx.reply_error(
                "You've already cleared this boss. "
                "`,buddy map battle` for a re-run.",
            )
            return
    else:
        # Boss zones can ONLY be cleared by the boss fight itself.
        # The old behaviour silently let `,buddy map battle` clear a boss
        # zone via a wild encounter -- which is how players "fought a
        # missing boss" and still got the zone marked complete.
        if z.get("boss") and cur_zone_id not in (progress.get("cleared_zones") or []):
            await ctx.reply_error(
                "This is a boss zone -- challenge the boss with "
                "`,buddy map boss`.",
            )
            return

    buddy = await _active_buddy(ctx)
    if not buddy:
        await ctx.reply_error("You need an active buddy.")
        return
    zone_id = cur_zone_id
    ok, remain = await _map.can_start_zone_battle(
        ctx.db, ctx.guild_id, ctx.author.id,
    )
    if not ok:
        await ctx.reply_cooldown(int(remain))
        return

    level = int(effective_level(buddy))
    if level < int(z.get("tier_min") or 1):
        await ctx.reply_error(
            f"Your buddy must be at least L{z.get('tier_min', 1)} for this zone.",
        )
        return

    passives = await _mastery.passives(ctx.db, ctx.author.id, ctx.guild_id)
    inventory = await _map.battle_inventory(ctx.db, ctx.guild_id, ctx.author.id)
    opponent = _synth_opponent(z, level, zone_id=zone_id)

    battle = StepBattle.from_rows(buddy, opponent)
    _bd = float(passives.get("combat.buddy_dmg") or 0.0)
    if _bd > 0:
        battle.f1.atk *= (1.0 + _bd)
    view = _PveBattleView(
        owner_id=ctx.author.id, ctx=ctx,
        battle=battle, zone_id=zone_id,
        is_boss=bool(is_boss), is_tournament=False,
        inventory=inventory, mastery_passives=passives,
    )
    state = _battle_state(battle, zone_id=zone_id)
    state["action_banner"] = "FIGHT!"
    png = render_battle_frame(state)
    file = discord.File(io.BytesIO(png), filename="battle.png")
    repeat_clear = zone_id in (progress.get("cleared_zones") or [])
    embed = _build_zone_intro_embed(
        ctx, dict(buddy), opponent, zone_id,
        is_boss=bool(is_boss),
        progress=progress,
        inventory=inventory,
        repeat_clear=bool(repeat_clear),
    )
    msg = await ctx.reply(
        embed=embed, file=file, view=view, mention_author=False,
    )
    view.message = msg


async def show_items(ctx: DiscoContext) -> None:
    """List battle consumable inventory + craft hints. ``,buddy map items``."""
    inv = await _map.battle_inventory(ctx.db, ctx.guild_id, ctx.author.id)
    lines: list[str] = []
    for k, meta in BATTLE_CONSUMABLES.items():
        qty = int(inv.get(k) or 0)
        mark = "x" + str(qty) if qty > 0 else "(none)"
        lines.append(
            f"{meta['emoji']} **{meta['name']}** {mark}\n"
            f"   {meta['description']}  -  CD `{meta['round_cd']}r`  -  "
            f"rarity `{meta['rarity']}`"
        )
    embed = (
        card("Battle Consumables", color=C_INFO)
        .description("\n\n".join(lines)[:4000])
        .footer(
            "Craft via `,craft make <recipe>` (e.g. `berry_quick_craft`). "
            "Stock drops also possible on zone clears."
        )
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)


async def do_visit_special(ctx: DiscoContext) -> None:
    """``,buddy map visit`` -- run the flow for the current special zone.

    Specials are non-combat nodes (region == 'special'). Each kind has
    its own handler in services.buddy_arena_specials:
        shop    -> item shop (BUD prices)
        spring  -> heal roster + free Cure Balm (1h CD)
        dig     -> random consumable (24h CD)
        trader  -> 3 rotating offers (6h refresh)
    """
    from services import buddy_arena_specials as _spec
    progress = await _map.get_progress(ctx.db, ctx.guild_id, ctx.author.id)
    cur = str(progress.get("current_zone_id") or "")
    kind = _spec.zone_kind(cur)
    if not kind:
        await ctx.reply_error(
            "This isn't a special location. Travel to a market, spring, "
            "dig spot, or trader first.",
        )
        return
    z = ARENA_ZONES.get(cur, {})
    name = str(z.get("name") or cur)
    tagline = str(z.get("tagline") or "")

    if kind == "spring":
        result = await _spec.spring_apply(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if not result.ok:
            mins = int(result.cooldown_remaining_s // 60)
            await ctx.reply_cooldown(int(result.cooldown_remaining_s))
            return
        bits = [
            f"Restored **{result.buddies_restored}** "
            f"{'buddy' if result.buddies_restored == 1 else 'buddies'} "
            "to full vigor.",
        ]
        if result.free_item_key:
            meta = BATTLE_CONSUMABLES.get(result.free_item_key) or {}
            bits.append(
                f"Take this for the road: {meta.get('emoji', '')} "
                f"**{meta.get('name', result.free_item_key)}**."
            )
        embed = (
            card(name, color=C_SUCCESS)
            .description(f"_{tagline}_\n\n" + "\n".join(bits))
            .footer("Available again in 1 hour.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)
        return

    if kind == "dig":
        passives = await _mastery.passives(ctx.db, ctx.author.id, ctx.guild_id)
        luck = float(passives.get("luck.rare_catch") or 0.0)
        result = await _spec.dig_apply(
            ctx.db, ctx.guild_id, ctx.author.id, luck_bonus=luck,
        )
        if not result.ok:
            hours = int(result.cooldown_remaining_s // 3600)
            mins  = int((result.cooldown_remaining_s % 3600) // 60)
            embed = (
                card(name, color=C_WARNING)
                .description(
                    f"_{tagline}_\n\n"
                    f"The stones are stubborn today. Try again in "
                    f"~{hours}h {mins}m."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return
        meta = BATTLE_CONSUMABLES.get(result.item_key) or {}
        embed = (
            card(name, color=C_GOLD)
            .description(
                f"_{tagline}_\n\n"
                f"You unearth {meta.get('emoji', '')} "
                f"**{meta.get('name', result.item_key)}** "
                f"(now x{result.new_inventory_qty})."
            )
            .footer("Available again tomorrow.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)
        return

    if kind == "shop":
        offers = _spec.shop_offers()
        lines = [
            f"{o.emoji} **{o.name}** -- {fmt_token(o.price_bud_human, 'BUD')}\n"
            f"   {o.description}  -  rarity `{o.rarity}`"
            for o in offers
        ]
        embed = (
            card(name, color=C_INFO)
            .description(
                f"_{tagline}_\n\nBuy any item with BUD:\n\n"
                + "\n\n".join(lines)
            )
            .footer(
                "Buy via `,buddy shopbuy <item>` -- "
                "all items also drop from zone clears."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)
        return

    if kind == "trader":
        offers = await _spec.trader_offers(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        lines = [
            f"**#{o.slot_idx + 1}** -- {o.label}\n"
            f"   Cost: `{o.cost_label}`  ->  Grants: `{o.grants_label}`"
            for o in offers
        ]
        embed = (
            card(name, color=C_INFO)
            .description(
                f"_{tagline}_\n\nToday's offers (refresh every 6h):\n\n"
                + "\n\n".join(lines)
            )
            .footer(
                "Redeem via `,buddy map trade <slot>` "
                "(e.g. `,buddy map trade 1`)."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)
        return

    await ctx.reply_error(f"Unknown special kind: {kind}")


async def do_shop_buy(ctx: DiscoContext, item_key: str, qty: int = 1) -> None:
    """``,buddy map buy <item> [qty]`` -- purchase from the Mossy Market."""
    from services import buddy_arena_specials as _spec
    progress = await _map.get_progress(ctx.db, ctx.guild_id, ctx.author.id)
    cur = str(progress.get("current_zone_id") or "")
    if _spec.zone_kind(cur) != "shop":
        await ctx.reply_error(
            "You're not at a shop -- travel to the Mossy Market first.",
        )
        return
    result = await _spec.purchase_shop(
        ctx.db, ctx.guild_id, ctx.author.id, item_key, qty=qty,
    )
    if not result.ok:
        await ctx.reply_error(result.reason)
        return
    meta = BATTLE_CONSUMABLES.get(result.item_key) or {}
    bud_h = to_human(int(result.bud_spent_raw))
    await ctx.reply_success(
        f"Bought x{result.qty} {meta.get('emoji', '')} "
        f"**{meta.get('name', result.item_key)}** for "
        f"{fmt_token(bud_h, 'BUD')} (you now have "
        f"x{result.new_inventory_qty}).",
        title="Mossy Market",
    )


async def do_trader_redeem(ctx: DiscoContext, slot: int) -> None:
    """``,buddy map trade <slot>`` -- redeem one of the trader's offers."""
    from services import buddy_arena_specials as _spec
    progress = await _map.get_progress(ctx.db, ctx.guild_id, ctx.author.id)
    cur = str(progress.get("current_zone_id") or "")
    if _spec.zone_kind(cur) != "trader":
        await ctx.reply_error(
            "You're not at the trader -- travel to the Caravan Clearing first.",
        )
        return
    # User passes 1-indexed slot for ergonomics; service uses 0-indexed.
    idx = max(0, int(slot) - 1)
    result = await _spec.redeem_trader(
        ctx.db, ctx.guild_id, ctx.author.id, idx,
    )
    if not result.ok:
        await ctx.reply_error(result.reason)
        return
    meta = BATTLE_CONSUMABLES.get(result.item_key) or {}
    cost_h = to_human(int(result.cost_amount_raw))
    await ctx.reply_success(
        f"Traded {fmt_token(cost_h, result.cost_kind.upper())} for "
        f"x{result.qty} {meta.get('emoji', '')} "
        f"**{meta.get('name', result.item_key)}**.",
        title="Caravan Clearing",
    )


async def show_tournament(ctx: DiscoContext) -> None:
    """Show the Champion Tournament bracket. ``,buddy tourney``."""
    progress = await _map.get_progress(ctx.db, ctx.guild_id, ctx.author.id)
    state = str(progress.get("tournament_state") or "locked")
    cur_round = int(progress.get("tournament_round") or 0)
    png = render_tournament_bracket(progress, current_round=max(1, cur_round))
    file = discord.File(io.BytesIO(png), filename="tourney.png")
    if state == "locked":
        desc = "Clear all three region bosses to qualify."
        color = C_WARNING
    elif state == "qualified":
        desc = "Qualified. Run `,buddy tourney start` to enter the bracket."
        color = C_INFO
    elif state == "in_progress":
        meta = next(
            (e for e in TOURNAMENT_BRACKET if int(e["round"]) == cur_round),
            {},
        )
        desc = (
            f"Bracket in progress: **Round {cur_round} -- {meta.get('label', '')}**\n"
            f"Run `,buddy tourney fight` to play the round."
        )
        color = C_GOLD
    else:
        desc = (
            f"You are the reigning **Buddy Champion** "
            f"(x{int(progress.get('champion_count') or 0)}). "
            f"`,buddy tourney start` to defend."
        )
        color = C_GOLD
    embed = (
        card("Champion Tournament", color=color)
        .description(desc)
        .image("attachment://tourney.png")
        .build()
    )
    await ctx.reply(embed=embed, file=file, mention_author=False)


async def tourney_start_cmd(ctx: DiscoContext) -> None:
    """Begin / resume the Champion Tournament bracket. ``,buddy tourney start``."""
    res = await _map.start_tournament(ctx.db, ctx.guild_id, ctx.author.id)
    if not res.ok:
        await ctx.reply_error(res.reason)
        return
    await ctx.reply_success(
        f"Round **{res.round}** ready. Run `,buddy tourney fight` to play it.",
        title="Tournament",
    )


async def tourney_fight_cmd(ctx: DiscoContext) -> None:
    """Play the current bracket round vs scaling AI. ``,buddy tourney fight``."""
    progress = await _map.get_progress(ctx.db, ctx.guild_id, ctx.author.id)
    if str(progress.get("tournament_state")) != "in_progress":
        await ctx.reply_error(
            "No active tournament round. Run `,buddy tourney start` first.",
        )
        return
    round_num = int(progress.get("tournament_round") or 1)
    meta = next(
        (e for e in TOURNAMENT_BRACKET if int(e["round"]) == round_num),
        None,
    )
    if not meta:
        await ctx.reply_error("Tournament data is missing. Try `,buddy tourney start`.")
        return

    buddy = await _active_buddy(ctx)
    if not buddy:
        await ctx.reply_error("You need an active buddy.")
        return

    passives = await _mastery.passives(ctx.db, ctx.author.id, ctx.guild_id)
    inventory = await _map.battle_inventory(ctx.db, ctx.guild_id, ctx.author.id)
    player_level = int(effective_level(buddy))
    opponent = {
        "id":            -2,
        "species":       random.choice(["wolf", "draclet", "blazer", "thornling"]),
        "name":          f"Champion R{round_num} AI",
        "rarity_tier":   min(5, 3 + round_num // 2),
        "level":         player_level + int(meta.get("level_bonus") or 0),
        "xp":            0,
        "hunger": 100, "happiness": 100, "energy": 100,
        "hp_alloc": 0, "atk_alloc": 0, "spd_alloc": 0,
        "gear": {},
    }
    battle = StepBattle.from_rows(buddy, opponent)
    _bd = float(passives.get("combat.buddy_dmg") or 0.0)
    if _bd > 0:
        battle.f1.atk *= (1.0 + _bd)
    view = _PveBattleView(
        owner_id=ctx.author.id, ctx=ctx,
        battle=battle, zone_id="champion_hall",
        is_boss=False, is_tournament=True,
        inventory=inventory, mastery_passives=passives,
    )
    state = _battle_state(battle, zone_id="champion_hall")
    state["action_banner"] = f"Round {round_num}: {meta.get('label', '')}"
    png = render_battle_frame(state)
    file = discord.File(io.BytesIO(png), filename="battle.png")
    embed = (
        card(f"Tournament -- {meta.get('label', '')}", color=C_GOLD)
        .description(
            f"AI Champion is +{int(meta.get('level_bonus') or 0)} levels.\n"
            f"Reward: {fmt_usd(int(meta.get('reward_usd') or 0))}  +  "
            f"`{meta.get('reward_item', '')}`."
        )
        .image("attachment://battle.png")
        .footer(f"Round 1 / 30  -  Items in bag: {sum(inventory.values())}")
        .build()
    )
    msg = await ctx.reply(
        embed=embed, file=file, view=view, mention_author=False,
    )
    view.message = msg
