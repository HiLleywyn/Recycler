"""cogs/crafting.py -- Crafting minigame commands.

Group: ,craft  (aliases: forge, smith)

  ,craft                       -- forge view (level, balances, recent crafts)
  ,craft list                  -- recipes you can currently make
  ,craft info <key>            -- recipe ingredients, output, level gate
  ,craft make <key> [qty]      -- consume inputs, mint INGOT, deposit output
  ,craft apply <key> [qty]     -- spend a crafted item back into the source game
  ,craft bag                   -- crafted-item inventory
  ,craft history               -- last 10 crafts
  ,craft swap <amt|all>        -- INGOT -> FORGE burn (slippage applies)
  ,craft cashout <amt|all>     -- FORGE -> USD burn (slippage applies)
  ,craft stake <amt|all>       -- INGOT -> staked (drips FORGE)
  ,craft unstake <amt|all>     -- staked INGOT -> wallet
  ,craft claim                 -- sweep accrued FORGE stake yield
  ,craft lb                    -- top crafters by FORGE earned
  ,craft help

Heavy lifting lives in services.crafting -- this module is presentation only.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

import configs.crafting_config as cc
from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_AMBER, C_GOLD, C_SUCCESS, C_TEAL, RARITY_ABBR, RARITY_COLORS,
    RARITY_DOT, fmt_token, fmt_ts,
    fmt_usd,
)
from services import auction as auction_svc
from services import crafting as craft_svc
from services import items as items_svc

log = logging.getLogger(__name__)


# ── Display helpers ─────────────────────────────────────────────────────────

_RARITY_COLOR = RARITY_COLORS
_RARITY_DOT = RARITY_DOT
_RARITY_ABBR = RARITY_ABBR

# Emoji headers per apply-kind. Used to label each general-recipe section
# in ,craft list / ,craft book.
_KIND_LABEL = {
    "bait":   "\U0001FA9D Fishing bait",
    "fert":   "\U0001F33F Farming fertilizer",
    "consum": "\U0001F9EA Dungeon consumables",
    "buddy":  "\U0001F436 Buddy treats",
}

# Per-category cap inside a single embed field. Anything beyond this collapses
# into a single "...and N more in this category." line so the embed never
# blows past Discord's 1024-char field cap and the player isn't faced with
# pagination.
_PER_CATEGORY_CAP: int = 10

# Legend the description blurbs share so the dots / lock marker have a
# meaning without leaving the embed.
_RARITY_LEGEND = (
    f"{_RARITY_DOT['common']} com  ·  "
    f"{_RARITY_DOT['uncommon']} unc  ·  "
    f"{_RARITY_DOT['rare']} rar  ·  "
    f"{_RARITY_DOT['epic']} epi  ·  "
    f"{_RARITY_DOT['legendary']} leg"
)


def _truncate(s: str, n: int) -> str:
    """Trim ``s`` to ``n`` chars with an ellipsis when truncated."""
    s = str(s or "")
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _recipe_table(
    recipes: list[tuple[str, dict]],
    active_specs: set[str] | None = None,
    *,
    show_lock: bool = False,
) -> str:
    """Render a Discord-friendly code-block table of recipes.

    Columns: Lvl / Rar / FGD / Key (truncated) / Name. When ``show_lock``
    is true an extra leading column shows ``L`` for level-gated and ``S``
    for specialty-locked rows so the recipe book can flag them inline.
    """
    if not recipes:
        return "_(none)_"
    active_specs = active_specs or set()
    rows: list[str] = []
    if show_lock:
        rows.append(
            f"{'':<2}{'Lvl':<5}{'Rar':<5}{'FGD':>8}  {'Key':<20}  Name"
        )
    else:
        rows.append(f"{'Lvl':<5}{'Rar':<5}{'FGD':>8}  {'Key':<20}  Name")
    for k, m in recipes:
        rar = str(m.get("rarity") or "common").lower()
        rar_abbr = _RARITY_ABBR.get(rar, rar[:3])
        lvl = int(m.get("min_level", 1))
        fgd = float(m.get("fgd_cost", 0.0))
        key_disp = _truncate(k, 20)
        name_disp = _truncate(str(m.get("name", k)), 22)
        if show_lock:
            spec = str(m.get("specialty") or "").lower()
            requires = bool(m.get("requires_specialty"))
            mark = " "
            if requires and spec not in active_specs:
                mark = "S"
            rows.append(
                f"{mark} L{lvl:<3} {rar_abbr:<5}{fgd:>8.2f}  "
                f"{key_disp:<20}  {name_disp}"
            )
        else:
            rows.append(
                f"L{lvl:<3} {rar_abbr:<5}{fgd:>8.2f}  "
                f"{key_disp:<20}  {name_disp}"
            )
    return "```\n" + "\n".join(rows) + "\n```"


def _split_general_specialist(
    recipes: list[tuple[str, dict]],
) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """Partition recipes into (general, specialist) where specialist is
    every recipe with ``requires_specialty: True``.
    """
    general: list[tuple[str, dict]] = []
    specialist: list[tuple[str, dict]] = []
    for k, m in recipes:
        if bool(m.get("requires_specialty")):
            specialist.append((k, m))
        else:
            general.append((k, m))
    return general, specialist


def _group_by_kind(
    recipes: list[tuple[str, dict]],
) -> dict[str, list[tuple[str, dict]]]:
    """Bucket recipes by apply-kind (``bait`` / ``fert`` / ``consum`` /
    ``buddy``). Order is preserved within each bucket.
    """
    out: dict[str, list[tuple[str, dict]]] = {}
    for k, m in recipes:
        kind, _ = cc.parse_apply_target(str(m.get("apply") or ""))
        out.setdefault(kind or "misc", []).append((k, m))
    return out


def _group_by_specialty(
    recipes: list[tuple[str, dict]],
) -> dict[str, list[tuple[str, dict]]]:
    """Bucket recipes by their specialty. Returned in the canonical
    ``cc.SPECIALTIES`` order with empty buckets dropped.
    """
    out: dict[str, list[tuple[str, dict]]] = {}
    for spec in cc.SPECIALTIES:
        out[spec] = []
    for k, m in recipes:
        spec = str(m.get("specialty") or "").lower()
        out.setdefault(spec, []).append((k, m))
    return {s: rs for s, rs in out.items() if rs}


def _add_recipe_fields(
    builder, bucket: dict[str, list[tuple[str, dict]]],
    label_fn, active_specs: set[str], *, show_lock: bool = False,
):
    """Append one field per non-empty bucket entry. Truncates each bucket at
    ``_PER_CATEGORY_CAP`` and stamps an "...and N more in this category."
    line when over.
    """
    for key, items in bucket.items():
        head = items[: _PER_CATEGORY_CAP]
        table = _recipe_table(head, active_specs, show_lock=show_lock)
        if len(items) > _PER_CATEGORY_CAP:
            extra = len(items) - _PER_CATEGORY_CAP
            table += f"\n...and {extra} more in this category."
        builder.field(label_fn(key, len(items)), table, False)
    return builder


def _ingot_emoji() -> str:
    return Config.TOKENS.get(cc.INGOT_SYMBOL, {}).get("emoji", "")


def _forge_emoji() -> str:
    return Config.TOKENS.get(cc.FORGE_SYMBOL, {}).get("emoji", "")


def _fgd_emoji() -> str:
    return Config.TOKENS.get(cc.FGD_SYMBOL, {}).get("emoji", "")


def _fmt_ingot(amt: float) -> str:
    return fmt_token(amt, cc.INGOT_SYMBOL, _ingot_emoji())


def _fmt_forge(amt: float) -> str:
    return fmt_token(amt, cc.FORGE_SYMBOL, _forge_emoji())


def _fmt_fgd(amt: float) -> str:
    return fmt_token(amt, cc.FGD_SYMBOL, _fgd_emoji())


async def _ah_hint_for_recipe(
    ctx: DiscoContext, key: str, name: str,
) -> str:
    """Build a one-line "List on AH" hint for a crafted recipe.

    Returns a string like ``,ah list <name> <price> -- last sold:
    1,000 INGOT (~$5.00)``. When the contract has never sold,
    omits the last-sold tail and shows only the suggested syntax.
    Failures fall back to a syntax-only hint so a stale auction
    table never blocks the craft receipt.
    """
    syntax = f"`,ah list {name.lower().replace(' ', '_')} <price>`"
    try:
        addr = items_svc.contract_address("crafted", str(key))
        last = await auction_svc.last_sold_price(
            ctx.db, contract_address=addr,
        )
    except Exception:
        log.debug("ah hint last-sold lookup failed", exc_info=True)
        return syntax
    if not last:
        return f"{syntax}\n-# No prior sales -- you set the floor."
    price = to_human(int(last.get("price_raw") or 0))
    cur = str(last.get("currency") or "").upper()
    usd_raw = int(last.get("price_usd_raw") or 0)
    usd_tag = (
        f" (~{fmt_usd(to_human(usd_raw))})" if usd_raw > 0 else ""
    )
    cur_emoji = Config.TOKENS.get(cur, {}).get("emoji", "")
    last_str = (
        fmt_token(price, cur, cur_emoji) if cur else f"{price:,.2f}"
    )
    return f"{syntax}\n-# Last sold: {last_str}{usd_tag}"


async def _wallet_held(ctx: DiscoContext, sym: str) -> int:
    row = await ctx.db.get_wallet_holding(
        ctx.author.id, ctx.guild_id, cc.FORGE_NETWORK_SHORT, sym,
    )
    return int((row or {}).get("amount") or 0)


async def _oracle(ctx: DiscoContext, sym: str) -> float:
    row = await ctx.db.get_price(sym, ctx.guild_id)
    return float(row["price"]) if row and row.get("price") is not None else 0.0


def _resolve_amount(arg: str, held_raw: int) -> int:
    """Parse ``"all"`` / ``"max"`` / a numeric human amount and return raw.
    Raises ValueError on bad input.
    """
    s = (arg or "").strip().lower()
    if s in ("all", "max", "*"):
        return int(held_raw)
    try:
        return int(to_raw(float(s)))
    except (TypeError, ValueError):
        raise ValueError("Specify an amount (e.g. `5`, `1.5`, or `all`).")


# ── Cog ─────────────────────────────────────────────────────────────────────

_RECIPES_PER_PAGE = 15


class _RecipeSpecialtySelect(discord.ui.Select):
    """Specialty filter dropdown for the ``,craft list`` browser."""

    def __init__(self, current: str | None) -> None:
        opts = [discord.SelectOption(
            label="All specialties", value="__all__",
            emoji="\U0001F4DC", default=(current is None),
        )]
        for s in cc.SPECIALTIES:
            meta = cc.specialty_meta(s) or {}
            opts.append(discord.SelectOption(
                label=str(meta.get("name") or s.title())[:100],
                value=s,
                emoji=str(meta.get("emoji") or "")[:1] or None,
                default=(current == s),
            ))
        super().__init__(
            placeholder="Filter by specialty...",
            options=opts,
            min_values=1, max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RecipesView" = self.view  # type: ignore
        choice = self.values[0]
        view.specialty = None if choice == "__all__" else choice
        view.page = 1
        await view.refresh(interaction)


class _RecipePickSelect(discord.ui.Select):
    """Per-page recipe picker. On select the modal opens with a 'Make
    this' button so the player can craft straight from the browser.
    """

    def __init__(self, page_recipes: list[tuple[str, dict]]) -> None:
        opts: list[discord.SelectOption] = []
        for key, meta in page_recipes[:25]:
            name = str(meta.get("name") or key.title())[:100]
            rarity = str(meta.get("rarity") or "")
            spec = str(meta.get("specialty") or "")
            tier_part = f"  ·  {rarity}" if rarity else ""
            spec_part = f"  ·  {spec}" if spec else ""
            desc = (f"{tier_part[4:]}{spec_part}").strip(" ·") or "general"
            opts.append(discord.SelectOption(
                label=name,
                value=key,
                description=desc[:100],
                emoji=str(meta.get("emoji") or "") or None,
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no recipes on this page)",
                value="__none__", default=True,
            )]
        super().__init__(
            placeholder="Pick a recipe to craft...",
            options=opts,
            min_values=1, max_values=1,
            row=2,
            disabled=(opts[0].value == "__none__"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RecipesView" = self.view  # type: ignore
        recipe_key = self.values[0]
        # Run the make path for qty=1 and surface the receipt as a
        # follow-up message so the browser stays open.
        try:
            res = await craft_svc.craft_item(
                view.ctx.db,
                guild_id=view.ctx.guild_id,
                user_id=interaction.user.id,
                craft_key=recipe_key, qty=1,
            )
        except ValueError as e:
            await interaction.response.send_message(
                str(e), ephemeral=True,
            )
            return
        except Exception as e:
            log.exception(
                "craft pick failed key=%s uid=%s",
                recipe_key, interaction.user.id,
            )
            await interaction.response.send_message(
                f"Craft failed: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        from core.framework.scale import to_human as _h
        await interaction.response.send_message(
            embed=card(
                f"\U0001F528 Crafted {res.craft_key}",
                description=(
                    f"Earned **{_h(res.ingot_minted_raw):,.4f} INGOT**.\n"
                    f"XP +{res.xp_gained}  ·  Level {res.new_level}"
                    + ("  ·  \U0001F195 leveled up!" if res.leveled_up else "")
                ),
                color=C_SUCCESS,
            ).build(),
            ephemeral=True,
        )


class RecipesView(discord.ui.View):
    """Interactive ``,craft list`` browser.

    Owner-locked, 5 minute timeout. Provides:
      * Specialty dropdown (All / Smithing / Alchemy / etc).
      * Recipe-pick dropdown that runs ``craft_item`` for qty=1.
      * Prev / Next page buttons (15 recipes per page).
      * Refresh re-fetches state in case crafting level changed.
    """

    def __init__(self, ctx: DiscoContext, *, specialty: str | None = None) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.specialty = specialty
        self.page = 1
        self.message: discord.Message | None = None
        self.add_item(_RecipeSpecialtySelect(specialty))

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your recipe browser. Run `,craft list` "
                "to open your own.",
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

    async def _fetch(self) -> tuple[list[tuple[str, dict]], int, int]:
        """Returns (recipes, level, total_pages)."""
        gid, uid = self.ctx.guild_id, self.ctx.author.id
        state = await craft_svc.ensure_state(self.ctx.db, gid, uid)
        level = int(state.get("crafting_level") or 1)
        recipes = craft_svc.list_recipes(level)
        if self.specialty:
            recipes = [
                (k, m) for k, m in recipes
                if str(m.get("specialty") or "").lower() == self.specialty
            ]
        total_pages = max(
            1, (len(recipes) + _RECIPES_PER_PAGE - 1) // _RECIPES_PER_PAGE,
        )
        return recipes, level, total_pages

    def _build_embed(
        self, recipes: list[tuple[str, dict]],
        level: int, total_pages: int,
    ) -> discord.Embed:
        page = max(1, min(self.page, total_pages))
        start = (page - 1) * _RECIPES_PER_PAGE
        slice_r = recipes[start: start + _RECIPES_PER_PAGE]

        spec_label = (
            (cc.specialty_meta(self.specialty) or {}).get("name", self.specialty.title())
            if self.specialty else "All specialties"
        )
        builder = card(
            f"\U0001F4DC Recipes  ·  {spec_label}  ·  Lv. {level}",
            description=f"**{len(recipes)}** recipe(s) in scope.",
            color=C_AMBER,
        )
        if not slice_r:
            builder = builder.field(
                "Nothing here",
                "No recipes for this filter at your current level. "
                "Switch the dropdown to **All specialties** or level "
                "crafting to unlock more.",
                False,
            )
            return builder.footer(
                f"Page {page}/{total_pages}"
            ).build()

        # Compact per-recipe lines: emoji + name + rarity + spec.
        lines: list[str] = []
        for key, meta in slice_r:
            emoji = str(meta.get("emoji") or "")
            name = str(meta.get("name") or key.title())
            rarity = str(meta.get("rarity") or "")
            spec = str(meta.get("specialty") or "")
            tag = "  ·  " + rarity if rarity else ""
            tag += "  ·  " + spec if spec else ""
            lines.append(f"{emoji} **{name}**  ·  `{key}`{tag}")

        # Chunk into <=1024-char fields for the Discord cap.
        chunks: list[list[str]] = []
        cur: list[str] = []
        cur_len = 0
        for ln in lines:
            if cur_len + len(ln) + 1 > 1000 or len(cur) >= 25:
                chunks.append(cur)
                cur, cur_len = [], 0
            cur.append(ln)
            cur_len += len(ln) + 1
        if cur:
            chunks.append(cur)
        for i, c in enumerate(chunks):
            label = "Recipes" if i == 0 else f"Recipes ({i + 1})"
            builder = builder.field(label, "\n".join(c), False)

        prefix = self.ctx.prefix or "."
        builder = builder.footer(
            f"Page {page}/{total_pages}  ·  "
            f"Pick a recipe in the dropdown to craft 1  ·  "
            f"`{prefix}craft info <key>` for ingredients"
        )
        return builder.build()

    def _replace_pick_select(
        self, page_recipes: list[tuple[str, dict]],
    ) -> None:
        for child in list(self.children):
            if isinstance(child, _RecipePickSelect):
                self.remove_item(child)
        self.add_item(_RecipePickSelect(page_recipes))

    async def refresh(self, interaction: discord.Interaction) -> None:
        recipes, level, total_pages = await self._fetch()
        self.page = max(1, min(self.page, total_pages))
        embed = self._build_embed(recipes, level, total_pages)
        # Page slice for the recipe-pick dropdown.
        start = (self.page - 1) * _RECIPES_PER_PAGE
        slice_r = recipes[start: start + _RECIPES_PER_PAGE]
        self._replace_pick_select(slice_r)
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label == "Prev":
                    child.disabled = self.page <= 1
                elif child.label == "Next":
                    child.disabled = self.page >= total_pages
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Prev", emoji="\U00002B05",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_prev(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        self.page = max(1, self.page - 1)
        await self.refresh(interaction)

    @discord.ui.button(
        label="Next", emoji="\U000027A1",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_next(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        self.page += 1
        await self.refresh(interaction)

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self.refresh(interaction)


class Crafting(commands.Cog):
    """Crafting minigame: turn fishing/farming/dungeon loot into bait,
    fertilizer, dungeon consumables, and buddy treats. Mints INGOT (earn-
    only) on every craft; INGOT -> FORGE -> USD via the same slippage path
    as fishing, farming, and the dungeon.
    """

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_check(self, ctx: DiscoContext) -> bool:
        """Module + premium gate. Crafting is paid; admins do NOT bypass."""
        if not await module_cog_check(self.bot, ctx, "crafting"):
            return False
        from services import entitlements
        from core.framework.premium import PremiumGateFailure
        if not await entitlements.is_premium(ctx.guild_id, ctx.db):
            raise PremiumGateFailure("crafting")
        return True

    async def _fan_out(
        self, uid: int, gid: int, trigger: str, amount: int = 1,
    ) -> list[str]:
        """Mirror services/fishing._fan_out so crafting participates in the
        same achievements / quests / challenges machinery as every other
        minigame. Each downstream call is wrapped so a bookkeeping failure
        never aborts the player's action. Returns newly-granted badge_ids.
        """
        granted: list[str] = []
        try:
            from services import achievements as _ach
            granted = await _ach.bump(self.bot, uid, gid, trigger, amount=amount) or []
        except Exception:
            log.debug("crafting: achievements.bump %s failed", trigger, exc_info=True)
        try:
            from services import quests as _quests
            await _quests.progress_trigger(self.bot.db, uid, gid, trigger, amount=amount)
        except Exception:
            log.debug("crafting: quests.progress_trigger %s failed", trigger, exc_info=True)
        try:
            from services import challenges as _ch
            await _ch.progress_trigger(self.bot, uid, gid, trigger, amount=amount)
        except Exception:
            log.debug("crafting: challenges.progress_trigger %s failed", trigger, exc_info=True)
        return granted

    @commands.hybrid_group(
        name="craft", aliases=["forge", "smith"],
        invoke_without_command=True, with_app_command=False,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def craft(self, ctx: DiscoContext) -> None:
        """Open your forge view."""
        from services.onboarding import maybe_send_intro
        await maybe_send_intro(ctx, "crafting")
        gid, uid = ctx.guild_id, ctx.author.id
        state = await craft_svc.ensure_state(ctx.db, gid, uid)
        ingot_held = await _wallet_held(ctx, cc.INGOT_SYMBOL)
        forge_held = await _wallet_held(ctx, cc.FORGE_SYMBOL)
        fgd_held = await _wallet_held(ctx, cc.FGD_SYMBOL)
        ingot_oracle = await _oracle(ctx, cc.INGOT_SYMBOL)
        forge_oracle = await _oracle(ctx, cc.FORGE_SYMBOL)

        level = int(state.get("crafting_level") or 1)
        xp = int(state.get("crafting_xp") or 0)
        next_xp = cc.xp_for_level(level + 1) if level < cc.MAX_LEVEL else xp
        total_crafts = int(state.get("total_crafts") or 0)
        staked = int(state.get("ingot_staked_raw") or 0)

        embed = (
            card(
                f"\U0001F528  {ctx.author.display_name}'s Forge",
                description=(
                    f"Level **{level}** | XP `{xp:,}` / `{next_xp:,}`\n"
                    f"Lifetime crafts: **{total_crafts:,}**"
                ),
                color=C_AMBER,
            )
            .field(
                "Wallet",
                (
                    f"{_fmt_ingot(to_human(ingot_held))}\n"
                    f"{_fmt_forge(to_human(forge_held))}"
                    f"  ~ **{fmt_usd(to_human(forge_held) * forge_oracle)}**\n"
                    f"{_fmt_fgd(to_human(fgd_held))}"
                ),
                True,
            )
            .field(
                "Stake",
                (
                    f"{_fmt_ingot(to_human(staked))} staked\n"
                    f"Yield: `{cc.INGOT_STAKE_FORGE_PER_DAY:.4f} FORGE`/INGOT/day"
                ),
                True,
            )
            .field(
                "Oracles",
                f"INGOT {fmt_usd(ingot_oracle)} | FORGE {fmt_usd(forge_oracle)}",
                False,
            )
            .footer(
                "Use ,craft list to see what you can make. "
                "INGOT->FORGE and FORGE->USD use the same slippage as ,fish / ,farm."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,craft list ────────────────────────────────────────────────────────

    @craft.command(name="list", aliases=["recipes", "menu"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_list(
        self, ctx: DiscoContext, specialty: str = "",
    ) -> None:
        """Interactive recipe browser.

        Opens a paginated, owner-locked panel with a specialty
        dropdown, a recipe-pick dropdown (pick one + click to craft),
        and Prev / Next / Refresh buttons. Pass an optional starting
        ``specialty`` to pre-filter (``,craft list smithing``).
        """
        spec_filter = (specialty or "").strip().lower() or None
        if spec_filter and spec_filter not in cc.SPECIALTIES:
            await ctx.reply_error(
                f"Unknown specialty `{spec_filter}`. "
                f"Pick from: {', '.join(cc.SPECIALTIES)}."
            )
            return
        view = RecipesView(ctx, specialty=spec_filter)
        recipes, level, total_pages = await view._fetch()
        if not recipes:
            if spec_filter:
                await ctx.reply_error(
                    f"No `{spec_filter}` recipes unlocked yet at "
                    f"crafting level {level}. Try a lower-tier specialty "
                    f"or level up your aggregate crafting first."
                )
            else:
                await ctx.reply_error(
                    "No recipes unlocked yet. Level up by crafting "
                    "common recipes first."
                )
            return

        # Initial render: page 1, populate the recipe-pick dropdown.
        embed = view._build_embed(recipes, level, total_pages)
        view._replace_pick_select(recipes[:_RECIPES_PER_PAGE])
        for child in view.children:
            if isinstance(child, discord.ui.Button):
                if child.label == "Prev":
                    child.disabled = True
                elif child.label == "Next":
                    child.disabled = total_pages <= 1
        msg = await ctx.reply(
            embed=embed, view=view, mention_author=False,
        )
        view.message = msg

    # ── ,craft info ────────────────────────────────────────────────────────

    @craft.command(name="info")
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_info(self, ctx: DiscoContext, key: str = "") -> None:
        """Show ingredients and output target for a recipe."""
        meta = craft_svc.recipe_info(key)
        if not meta:
            await ctx.reply_error(f"No recipe with key `{key}`. See `,craft list`.")
            return
        rarity = str(meta.get("rarity") or "common").lower()
        color = _RARITY_COLOR.get(rarity, C_AMBER)
        rar_dot = _RARITY_DOT.get(rarity, "")
        kind, target = cc.parse_apply_target(str(meta.get("apply") or ""))
        target_blurb = {
            "bait":   f"Tops up your fishing bait inventory under key `{target}`.",
            "fert":   f"Tops up your farming fertilizer inventory under key `{target}`.",
            "consum": f"Tops up your dungeon consumables under key `{target}`.",
            "buddy":  f"Applies a `{target}` effect to your active buddy.",
        }.get(kind, "Unknown apply route.")
        lo, hi = cc.RARITY_INGOT_PAYOUT.get(rarity, (0, 0))
        spec_key = str(meta.get("specialty") or "").lower()
        spec_meta = cc.specialty_meta(spec_key) if spec_key else {}
        spec_label = (
            f"{spec_meta.get('emoji', '')} {spec_meta.get('name', '?')}"
            if spec_meta and spec_meta.get("name") else "-"
        )
        requires = bool(meta.get("requires_specialty"))
        # Surface the specialty-lock + the player's active set so the
        # info card says exactly why a recipe can or can't be crafted.
        is_locked_for_user = False
        if requires:
            spec_label += "  \U0001F512 specialty-locked"
        try:
            _state = await craft_svc.ensure_state(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            _active = set(_state.get("active_specialties") or [])
            if requires and spec_key and spec_key not in _active:
                spec_label += " (need pick)"
                is_locked_for_user = True
        except Exception:
            pass

        # Ingredient table: column-aligned in a code block with the
        # material source so the player knows where to grind each input
        # without leaving the embed.
        ingr = meta.get("inputs") or {}
        if ingr:
            ing_rows = [f"{'Qty':>4}  {'Ingredient':<18}  Source"]
            for ik, qty in ingr.items():
                src = cc.material_source(ik)
                ing_rows.append(
                    f"{int(qty) if isinstance(qty, int) else qty:>4}  "
                    f"{_truncate(str(ik), 18):<18}  {_truncate(src, 38)}"
                )
            inputs_value = "```\n" + "\n".join(ing_rows) + "\n```"
        else:
            inputs_value = "_(none)_"

        # Costs / yields table: side-by-side fee + INGOT range + XP so
        # the player can compare two recipes head-to-head.
        xp_amt = int(cc.RARITY_XP.get(rarity, 0))
        cost_value = (
            "```\n"
            f"FGD fee     : {float(meta.get('fgd_cost', 0)):>8.2f}\n"
            f"INGOT mint  : {lo:>6.1f} - {hi:.1f}\n"
            f"XP per craft: {xp_amt:>8}\n"
            "```\n"
            "-# Off-specialty general crafts grant 10% XP "
            "(specialist-locked recipes are uncraftable off-spec)."
        )

        # Header line with rarity dot + lock status so the player gets
        # the verdict before reading any fields.
        status_line = (
            f"{rar_dot} **{rarity.title()}**  ·  "
            f"Lv. **{int(meta.get('min_level', 1))}**  ·  "
            f"{spec_label}"
        )
        if is_locked_for_user:
            status_line += "\n\U0001F6AB You don't have this specialty active yet."
        elif requires:
            status_line += "\n\U00002705 You have this specialty active."

        blurb = str(meta.get("blurb") or "")
        ah_hint = await _ah_hint_for_recipe(
            ctx, key, str(meta.get("name") or key),
        )
        embed = (
            card(
                f"{meta.get('emoji','')} {meta.get('name', key)}",
                description=f"{status_line}\n{blurb}".strip(),
                color=color,
            )
            .field("Inputs", inputs_value, False)
            .field("Output", target_blurb, False)
            .field("Costs / Yields", cost_value, False)
            .field("Sell on Auction House", ah_hint, False)
            .footer(f"Recipe key: {key}")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,craft make ────────────────────────────────────────────────────────

    @craft.command(name="make", aliases=["build", "do"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_make(
        self, ctx: DiscoContext, key: str = "", qty: int = 1,
    ) -> None:
        """Craft a recipe. Consumes inputs from each source game and mints INGOT."""
        if not key:
            await ctx.reply_error_hint(
                "Specify a recipe key. See `,craft list`.",
                hint="craft make worm_bundle 5",
                command_name="craft make",
            )
            return
        if qty <= 0:
            await ctx.reply_error("Quantity must be positive.")
            return
        try:
            res = await craft_svc.craft_item(ctx.db, ctx.guild_id, ctx.author.id, key, qty)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        await self._fan_out(ctx.author.id, ctx.guild_id, "craft_made", res.qty)
        if res.rarity == "legendary":
            await self._fan_out(ctx.author.id, ctx.guild_id, "craft_legendary", 1)
        meta = craft_svc.recipe_info(key) or {}
        # Tagged trigger for the archer "Quiver Stocked" daily quest. Any
        # recipe that routes to one of the four ammo consumables counts.
        _apply = str((meta or {}).get("apply") or "")
        if _apply.startswith((
            "consum/arrow_bundle", "consum/bolt_bundle",
            "consum/broadhead_bundle", "consum/piercing_bolts",
        )):
            await self._fan_out(
                ctx.author.id, ctx.guild_id, "craft_ammo", res.qty,
            )
        ingot_emoji = _ingot_emoji()
        ah_hint = await _ah_hint_for_recipe(
            ctx, key, str(meta.get("name") or key),
        )
        embed = (
            card(
                f"{meta.get('emoji','')} Crafted: {meta.get('name', key)} x{res.qty}",
                description=str(meta.get("blurb") or ""),
                color=_RARITY_COLOR.get(res.rarity, C_AMBER),
            )
            .field("INGOT minted", _fmt_ingot(to_human(res.ingot_minted_raw)), True)
            .field("FGD spent", _fmt_fgd(to_human(res.fgd_spent_raw)), True)
            .field("XP", f"+{res.xp_gained:,}", True)
            .field_if(
                res.leveled_up,
                "Level up!",
                f"You are now crafting level **{res.new_level}**.",
                False,
            )
            .field_if(
                bool(res.specialty_leveled_up),
                "Specialty up!",
                (
                    f"**{res.specialty.title()}** is now level "
                    f"**{res.specialty_new_level}**."
                ),
                False,
            )
            .field("Sell on Auction House", ah_hint, False)
            .footer(
                f"Use `,craft apply {key}` to send this into "
                f"{cc.parse_apply_target(str(meta.get('apply') or ''))[0] or 'its game'}."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

        # Bus event so the achievements service can fire any
        # specialty_level_<spec> threshold + the renaissance check.
        if res.specialty_leveled_up and res.specialty:
            try:
                await ctx.bot.bus.publish(
                    "specialty_level_up",
                    guild=ctx.guild, user=ctx.author,
                    specialty=res.specialty,
                    new_level=int(res.specialty_new_level),
                    old_level=int(res.specialty_old_level),
                )
            except Exception:
                log.debug(
                    "specialty_level_up event publish failed",
                    exc_info=True,
                )

    # ── ,craft apply ───────────────────────────────────────────────────────

    @craft.command(name="apply", aliases=["use", "send"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_apply(
        self, ctx: DiscoContext, key: str = "", qty: int = 1,
    ) -> None:
        """Spend a crafted item back into its source game."""
        if not key:
            await ctx.reply_error_hint(
                "Specify what to apply.",
                hint="craft apply worm_bundle 3",
                command_name="craft apply",
            )
            return
        if qty <= 0:
            await ctx.reply_error("Quantity must be positive.")
            return
        try:
            res = await craft_svc.apply_item(ctx.db, ctx.guild_id, ctx.author.id, key, qty)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        await self._fan_out(ctx.author.id, ctx.guild_id, "craft_applied", res.qty)
        await ctx.reply_success(res.note, title=f"Applied {res.qty}x `{res.craft_key}`")

    # ── ,craft bag ─────────────────────────────────────────────────────────

    @craft.command(name="bag", aliases=["inv", "inventory"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_bag(self, ctx: DiscoContext) -> None:
        """Show your crafted-item inventory."""
        state = await craft_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        rows = craft_svc.inventory_summary(state)
        if not rows:
            await ctx.reply_error("Your forge inventory is empty. `,craft list` to start.")
            return
        builder = card(
            f"\U0001F392 {ctx.author.display_name}'s Forge Bag",
            description=f"`{sum(r[1] for r in rows)}` items across `{len(rows)}` recipes.",
            color=C_AMBER,
        )
        # Group into chunks of 10 lines per field to stay under 1024 chars.
        chunks: list[list[str]] = [[]]
        for k, n, m in rows:
            chunks[-1].append(f"`{k}` {m.get('emoji','')} **{m.get('name', k)}** x **{n}**")
            if len(chunks[-1]) >= 10:
                chunks.append([])
        for i, lines in enumerate(chunks):
            if not lines:
                continue
            builder = builder.field(f"Items ({i+1})" if len(chunks) > 1 else "Items",
                                    "\n".join(lines), False)
        await ctx.reply(embed=builder.build(), mention_author=False)

    # ── ,craft history ─────────────────────────────────────────────────────

    @craft.command(name="history", aliases=["log", "recent"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_history(self, ctx: DiscoContext) -> None:
        """Show your most recent crafts."""
        rows = await craft_svc.get_user_crafts(ctx.db, ctx.guild_id, ctx.author.id, 10)
        if not rows:
            await ctx.reply_error("No crafts yet. `,craft make <key>` to start.")
            return
        lines = []
        for r in rows:
            meta = craft_svc.recipe_info(str(r.get("craft_key") or "")) or {}
            ingot = to_human(int(r.get("ingot_earned_raw") or 0))
            lines.append(
                f"{fmt_ts(r.get('crafted_at'))} | "
                f"{meta.get('emoji','')} {meta.get('name', r.get('craft_key'))} "
                f"x{int(r.get('qty', 1))} -- "
                f"+{ingot:.2f} INGOT"
            )
        embed = (
            card("Recent Crafts", description="\n".join(lines), color=C_AMBER)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,craft swap (INGOT -> FORGE, slippage applies) ─────────────────────

    @craft.command(name="swap", aliases=["burn"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_swap(self, ctx: DiscoContext, amt: str = "") -> None:
        """Burn INGOT, mint FORGE at the live oracle minus impact slippage."""
        if not amt:
            await ctx.reply_error_hint(
                "Specify an amount.",
                hint="craft swap 100   (or `all`)",
                command_name="craft swap",
            )
            return
        held = await _wallet_held(ctx, cc.INGOT_SYMBOL)
        try:
            amt_raw = _resolve_amount(amt, held)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        if amt_raw <= 0 or amt_raw > held:
            await ctx.reply_error(
                f"You only have {to_human(held):,.4f} INGOT to burn."
            )
            return
        try:
            res = await craft_svc.burn_ingot_for_forge(
                ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        await self._fan_out(ctx.author.id, ctx.guild_id, "craft_ingot_swap", 1)
        embed = (
            card(
                "INGOT -> FORGE",
                description=(
                    f"Burned {_fmt_ingot(to_human(res.burned_ingot_raw))}\n"
                    f"Minted {_fmt_forge(to_human(res.minted_forge_raw))}"
                ),
                color=C_TEAL,
            )
            .field("Slippage", f"`{res.impact_pct * 100:.2f}%`", True)
            .footer("The slippage IS the fee. Same impact formula as ,fish / ,farm.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,craft cashout (FORGE -> USD, slippage applies) ────────────────────

    @craft.command(name="cashout", aliases=["sell", "exit"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_cashout(self, ctx: DiscoContext, amt: str = "") -> None:
        """Burn FORGE, credit your USD wallet at the post-impact oracle price."""
        if not amt:
            await ctx.reply_error_hint(
                "Specify an amount.",
                hint="craft cashout 50   (or `all`)",
                command_name="craft cashout",
            )
            return
        held = await _wallet_held(ctx, cc.FORGE_SYMBOL)
        try:
            amt_raw = _resolve_amount(amt, held)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        if amt_raw <= 0 or amt_raw > held:
            await ctx.reply_error(
                f"You only have {to_human(held):,.4f} FORGE to cash out."
            )
            return
        forge_oracle_before = await _oracle(ctx, cc.FORGE_SYMBOL)
        try:
            res = await craft_svc.cashout_forge(
                ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        # V3 Pillar 2: crafter mastery XP scales with USD cashed out.
        try:
            from services import mastery as _mastery
            _xp = _mastery.xp_for_action(to_human(int(res.usd_credit_raw)))
            await _mastery.add_mastery(
                ctx.db, ctx.author.id, ctx.guild_id, "crafter", _xp,
            )
        except Exception:
            pass
        await self._fan_out(ctx.author.id, ctx.guild_id, "craft_forge_cashout", 1)
        from core.framework.staking import cashout_receipt
        forge_oracle_after = await _oracle(ctx, cc.FORGE_SYMBOL)
        await ctx.reply(
            embed=cashout_receipt(
                burned_symbol=cc.FORGE_SYMBOL, burned_emoji=_forge_emoji(),
                burned_h=to_human(int(res.burned_forge_raw)),
                usd_credited_h=to_human(int(res.usd_credit_raw)),
                oracle_before=forge_oracle_before,
                oracle_after=forge_oracle_after,
                impact_pct=float(res.impact_pct),
            ),
            mention_author=False,
        )

    # ── Staking ────────────────────────────────────────────────────────────

    @craft.command(name="stake")
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_stake(self, ctx: DiscoContext, amt: str = "") -> None:
        """Stake INGOT to drip FORGE.

        With no amount: opens the unified stake panel (Stake / Unstake /
        Claim / Refresh buttons -- same shape as ,buddy stake / ,delve
        stake / ,fish stake / ,farm stake).
        """
        if not (amt or "").strip():
            await self._open_stake_panel(ctx)
            return
        held = await _wallet_held(ctx, cc.INGOT_SYMBOL)
        try:
            amt_raw = _resolve_amount(amt or "0", held)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        if amt_raw <= 0:
            await ctx.reply_error("Specify an amount.")
            return
        try:
            new_total = await craft_svc.stake_ingot(
                ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        from core.framework.staking import stake_receipt
        ingot_oracle = await _oracle(ctx, cc.INGOT_SYMBOL)
        await ctx.reply(
            embed=stake_receipt(
                action="Staked",
                stake_symbol=cc.INGOT_SYMBOL, stake_emoji=_ingot_emoji(),
                delta_h=to_human(int(amt_raw)),
                total_h=to_human(int(new_total)),
                stake_oracle=ingot_oracle,
                note=(
                    f"Earns {cc.INGOT_STAKE_FORGE_PER_DAY:g} FORGE per INGOT per day."
                ),
            ),
            mention_author=False,
        )

    async def _open_stake_panel(self, ctx: DiscoContext) -> None:
        """Open the unified stake panel for INGOT -> FORGE."""
        from core.framework.staking import StakeAdapter, StakePanelView, StakeToken

        async def _state(c: DiscoContext) -> dict:
            st = await craft_svc.ensure_state(
                c.db, c.guild_id, c.author.id,
            )
            staked = int(st.get("ingot_staked_raw") or 0)
            pending = int(
                await craft_svc.accrued_stake_yield(
                    c.db, c.guild_id, c.author.id,
                ) or 0
            )
            wallet = await _wallet_held(c, cc.INGOT_SYMBOL)
            staked_h = to_human(staked)
            daily_h = staked_h * float(cc.INGOT_STAKE_FORGE_PER_DAY)
            ingot_oracle = await _oracle(c, cc.INGOT_SYMBOL)
            forge_oracle = await _oracle(c, cc.FORGE_SYMBOL)
            return {
                "staked_by_sym": {cc.INGOT_SYMBOL: staked},
                "wallet_by_sym": {cc.INGOT_SYMBOL: int(wallet)},
                "stake_oracle_by_sym": {cc.INGOT_SYMBOL: ingot_oracle},
                "yield_oracle": forge_oracle,
                "pending_raw": pending,
                "daily_rate_raw": int(to_raw(daily_h)),
            }

        async def _stake(c: DiscoContext, raw: int, _sym: str) -> int:
            return int(await craft_svc.stake_ingot(
                c.db, c.guild_id, c.author.id, int(raw),
            ))

        async def _unstake(c: DiscoContext, raw: int, _sym: str) -> int:
            return int(await craft_svc.unstake_ingot(
                c.db, c.guild_id, c.author.id, int(raw),
            ))

        async def _claim(c: DiscoContext) -> int:
            return int(await craft_svc.claim_stake_yield(
                c.db, c.guild_id, c.author.id,
            ))

        adapter = StakeAdapter(
            title="\U0001F528 Crafting Stake (INGOT -> FORGE)",
            color=C_AMBER,
            stake_tokens=[StakeToken(cc.INGOT_SYMBOL, _ingot_emoji())],
            yield_symbol=cc.FORGE_SYMBOL, yield_emoji=_forge_emoji(),
            get_state=_state, do_stake=_stake,
            do_unstake=_unstake, do_claim=_claim,
            note=(
                f"Stake INGOT to drip FORGE. Yield: "
                f"{cc.INGOT_STAKE_FORGE_PER_DAY:g} FORGE per INGOT per day."
            ),
        )
        await StakePanelView.send(ctx, adapter)

    @craft.command(name="unstake")
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_unstake(self, ctx: DiscoContext, amt: str = "") -> None:
        """Unstake INGOT back to your wallet."""
        state = await craft_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        staked = int(state.get("ingot_staked_raw") or 0)
        try:
            amt_raw = _resolve_amount(amt or "0", staked)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        if amt_raw <= 0:
            await ctx.reply_error("Specify an amount.")
            return
        try:
            remaining = await craft_svc.unstake_ingot(
                ctx.db, ctx.guild_id, ctx.author.id, amt_raw,
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        from core.framework.staking import stake_receipt
        ingot_oracle = await _oracle(ctx, cc.INGOT_SYMBOL)
        await ctx.reply(
            embed=stake_receipt(
                action="Unstaked",
                stake_symbol=cc.INGOT_SYMBOL, stake_emoji=_ingot_emoji(),
                delta_h=to_human(int(amt_raw)),
                total_h=to_human(int(remaining)),
                stake_oracle=ingot_oracle,
            ),
            mention_author=False,
        )

    @craft.command(name="claim")
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_claim(self, ctx: DiscoContext) -> None:
        """Claim accrued INGOT-stake yield as FORGE."""
        owed = await craft_svc.claim_stake_yield(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if owed <= 0:
            await ctx.reply_error("Nothing to claim yet -- stake INGOT first.")
            return
        from core.framework.staking import claim_receipt
        forge_oracle = await _oracle(ctx, cc.FORGE_SYMBOL)
        ingot_oracle = await _oracle(ctx, cc.INGOT_SYMBOL)
        st = await craft_svc.ensure_state(ctx.db, ctx.guild_id, ctx.author.id)
        await ctx.reply(
            embed=claim_receipt(
                yield_symbol=cc.FORGE_SYMBOL, yield_emoji=_forge_emoji(),
                yield_paid_h=to_human(int(owed)),
                yield_oracle=forge_oracle,
                stake_symbol=cc.INGOT_SYMBOL, stake_emoji=_ingot_emoji(),
                total_staked_h=to_human(int(st.get("ingot_staked_raw") or 0)),
                stake_oracle=ingot_oracle,
            ),
            mention_author=False,
        )

    # ── Specialties ────────────────────────────────────────────────────────

    @craft.command(name="specialties", aliases=["specialty", "tracks", "skills"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_specialties(self, ctx: DiscoContext) -> None:
        """Show per-specialty XP/level + your active picks.

        Six tracks: Smithing / Alchemy / Cooking / Fletching / Tinkering /
        Enchanting. Pick up to ``ACTIVE_SPECIALTY_CAP`` (default 2) with
        ``,craft specialize <key>``; ``,shop buy specialty_slot`` adds a
        permanent **third** slot (premium one-time unlock). In-specialty
        recipes get +1% INGOT per specialty level; off-specialty crafts
        pay a 50% XP penalty. Specialty-locked recipes (the
        ``requires_specialty`` flag) only craft when their specialty is
        in your active set.
        """
        from configs.crafting_config import (
            SPECIALTIES, SPECIALTY_META, MAX_LEVEL,
            ACTIVE_SPECIALTY_CAP, SPECIALTY_INGOT_BONUS_PER_LEVEL,
            OFF_SPECIALTY_XP_MULT, xp_for_level,
        )

        state = await ctx.db.fetch_one(
            "SELECT * FROM user_crafting WHERE guild_id = $1 AND user_id = $2",
            ctx.guild_id, ctx.author.id,
        )
        if not state:
            await ctx.reply_error(
                "No crafting state yet -- run `,craft list` first."
            )
            return

        agg_lvl = int(state.get("crafting_level") or 1)
        agg_xp = int(state.get("crafting_xp") or 0)
        active = list(state.get("active_specialties") or [])
        # Effective cap = base cap + purchased extra slots (migration 0179).
        # Without this addend the panel shows "Active 3/2" once the player
        # buys ,shop buy specialty_slot, which makes the third slot read
        # as broken even though it actually works on the service side.
        extra_slots = int(state.get("extra_specialty_slots") or 0)
        eff_cap = ACTIVE_SPECIALTY_CAP + extra_slots

        if active:
            badges = []
            for s in active:
                meta = SPECIALTY_META.get(s) or {}
                badges.append(
                    f"{meta.get('emoji', '')} **{meta.get('name', s.title())}**"
                )
            active_line = "  ·  ".join(badges)
            extra_tag = f" (+{extra_slots} purchased)" if extra_slots > 0 else ""
            picks_line = (
                f"Active ({len(active)}/{eff_cap}{extra_tag}): {active_line}"
            )
        else:
            extra_tag = f" (+{extra_slots} purchased)" if extra_slots > 0 else ""
            picks_line = (
                f"_No active specialties (0/{eff_cap}{extra_tag})._  "
                f"Pick with `,craft specialize <key>`."
            )

        bonus_pct = int(SPECIALTY_INGOT_BONUS_PER_LEVEL * 100)
        off_pct = int(OFF_SPECIALTY_XP_MULT * 100)
        builder = card(
            "\U0001F528 Crafting Specialties",
            color=C_AMBER,
        ).description(
            f"Aggregate: **Lv. {agg_lvl}**  ·  {agg_xp:,} total XP\n"
            f"{picks_line}\n"
            f"-# In-specialty: **+{bonus_pct}% INGOT per Lv**.  "
            f"Off-specialty general crafts: **{off_pct}% XP** "
            f"(you still level up, just ~10x slower).  "
            f"`requires_specialty` recipes stay uncraftable until "
            f"the matching branch is in your active set."
        )
        for spec in SPECIALTIES:
            meta = SPECIALTY_META.get(spec) or {}
            sym = str(meta.get("emoji") or "")
            name = str(meta.get("name") or spec.title())
            blurb = str(meta.get("blurb") or "")
            spec_lvl = int(state.get(f"{spec}_level") or 1)
            spec_xp = int(state.get(f"{spec}_xp") or 0)
            tag = "  ·  \U00002705 active" if spec in active else ""
            if spec_lvl >= MAX_LEVEL:
                progress = f"**Lv. {spec_lvl}** (max)  ·  {spec_xp:,} XP"
            else:
                next_xp = xp_for_level(spec_lvl + 1)
                cur_xp = xp_for_level(spec_lvl)
                pct = max(
                    0.0,
                    min(
                        100.0,
                        100.0 * (spec_xp - cur_xp)
                        / max(1, next_xp - cur_xp),
                    ),
                )
                progress = (
                    f"**Lv. {spec_lvl}**  ·  "
                    f"{spec_xp:,} / {next_xp:,} XP  ({pct:.0f}%)"
                )
            builder = builder.field(
                f"{sym} {name}{tag}",
                f"{progress}\n-# {blurb}",
                False,
            )
        await ctx.send_embed(builder.build())

    @craft.command(name="specialize", aliases=["pick", "select"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_specialize(
        self, ctx: DiscoContext, key: str = "",
    ) -> None:
        """Lock in a specialty. You can hold up to 2 at once.

        ``,craft specialize smithing`` adds Smithing to your active set.
        Drop one with ``,craft despecialize <key>`` if you want to swap.
        """
        if not key:
            await ctx.reply_error_hint(
                "Specify a specialty.",
                hint=f"craft specialize {cc.SPECIALTIES[0]}",
                command_name="craft specialize",
            )
            return
        try:
            new_set, msg = await craft_svc.add_specialty(
                ctx.db, ctx.guild_id, ctx.author.id, key,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await ctx.reply_success(
            f"{msg}\nActive: {', '.join(s.title() for s in new_set) or 'none'}.",
            title="Specialty Locked In",
        )

    @craft.command(name="despecialize", aliases=["unpick", "drop-spec", "deselect"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_despecialize(
        self, ctx: DiscoContext, key: str = "",
    ) -> None:
        """Drop a specialty from your active set."""
        if not key:
            await ctx.reply_error_hint(
                "Specify which specialty to drop.",
                hint="craft despecialize smithing",
                command_name="craft despecialize",
            )
            return
        try:
            new_set, msg = await craft_svc.remove_specialty(
                ctx.db, ctx.guild_id, ctx.author.id, key,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await ctx.reply_success(
            f"{msg}\nActive: {', '.join(s.title() for s in new_set) or 'none'}.",
            title="Specialty Dropped",
        )

    @craft.command(
        name="book",
        aliases=["recipebook", "rbook", "allrecipes"],
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_book(
        self, ctx: DiscoContext, specialty: str = "",
    ) -> None:
        """Browse the full recipe catalog, grouped by general vs specialist.

        Pass a specialty name to filter to one branch
        (``,craft book alchemy``). Recipes you can't access yet (level
        gate or specialty pick) still render so you know what to work
        toward; per-category truncation keeps the output to a single
        embed -- use ``,craft info <key>`` for full ingredient detail.
        """
        spec_filter = (specialty or "").strip().lower()
        if spec_filter and spec_filter not in cc.SPECIALTIES:
            await ctx.reply_error(
                f"Unknown specialty `{spec_filter}`. "
                f"Pick from: {', '.join(cc.SPECIALTIES)}."
            )
            return

        state = await craft_svc.ensure_state(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        my_lvl = int(state.get("crafting_level") or 1)
        active = set(state.get("active_specialties") or [])
        extra_slots = int(state.get("extra_specialty_slots") or 0)
        eff_cap = cc.ACTIVE_SPECIALTY_CAP + extra_slots

        rows = craft_svc.all_recipes()
        if spec_filter:
            rows = [
                (k, m) for k, m in rows
                if str(m.get("specialty") or "").lower() == spec_filter
            ]
        if not rows:
            await ctx.reply_error("No recipes match that filter.")
            return

        general, specialist = _split_general_specialist(rows)

        prefix = await ctx.get_guild_prefix()
        title = "\U0001F4D6 Recipe Book"
        if spec_filter:
            sm = cc.specialty_meta(spec_filter)
            title += (
                f"  -  {sm.get('emoji', '')} "
                f"{sm.get('name', spec_filter.title())}"
            )
        active_line = (
            ", ".join(s.title() for s in sorted(active)) or "none"
        )

        builder = card(title, color=C_AMBER).description(
            f"**{len(rows)}** recipe(s) in catalog "
            f"(**{len(general)}** general, **{len(specialist)}** specialist).\n"
            f"Your level: **{my_lvl}**  -  "
            f"Active: **{active_line}** "
            f"({len(active)}/{eff_cap}"
            + (f" +{extra_slots}" if extra_slots > 0 else "")
            + ")\n"
            f"-# {_RARITY_LEGEND}  -  "
            f"`L` = level-locked  -  `S` = specialty-locked\n"
            f"-# Filter: `{prefix}craft book <specialty>`. "
            f"Details: `{prefix}craft info <key>`."
        )

        # Per-row marker function: precompute lock state so the table can
        # show L / S / blank in a single character column.
        def _book_table(items, *, with_specialty_col=False):
            """Render a code-block table for the recipe book. Adds an
            extra leading column showing ``L`` (level-gated) / ``S``
            (specialty-locked) / blank (ready to craft)."""
            if not items:
                return "_(none)_"
            head = items[: _PER_CATEGORY_CAP]
            rows = [
                f"{'':<2}{'Lvl':<5}{'Rar':<5}{'FGD':>8}  "
                f"{'Key':<20}  Name"
            ]
            for k, m in head:
                rar = str(m.get("rarity") or "common").lower()
                rar_abbr = _RARITY_ABBR.get(rar, rar[:3])
                lvl = int(m.get("min_level", 1))
                fgd = float(m.get("fgd_cost", 0.0))
                spec = str(m.get("specialty") or "").lower()
                requires = bool(m.get("requires_specialty"))
                if my_lvl < lvl:
                    mark = "L"
                elif requires and spec not in active:
                    mark = "S"
                else:
                    mark = " "
                rows.append(
                    f"{mark} L{lvl:<3} {rar_abbr:<5}{fgd:>8.2f}  "
                    f"{_truncate(k, 20):<20}  "
                    f"{_truncate(str(m.get('name', k)), 22)}"
                )
            out = "```\n" + "\n".join(rows) + "\n```"
            if len(items) > _PER_CATEGORY_CAP:
                extra = len(items) - _PER_CATEGORY_CAP
                out += f"\n...and {extra} more in this category."
            return out

        if general:
            builder.field(
                "\U0001F513  GENERAL RECIPES",
                "_Anyone can craft these (off-spec = 10% XP, still levels)._",
                False,
            )
            for kind, items in _group_by_kind(general).items():
                label = (
                    f"{_KIND_LABEL.get(kind, kind.title())} ({len(items)})"
                )
                builder.field(label, _book_table(items), False)

        if specialist:
            builder.field(
                "\U0001F512  SPECIALIST RECIPES",
                "_Need the matching specialty active (no off-spec craft)._",
                False,
            )
            for spec, items in _group_by_specialty(specialist).items():
                meta = cc.specialty_meta(spec) or {}
                emoji = meta.get("emoji", "")
                name = meta.get("name", spec.title())
                tag = (
                    "  -  \U00002705 active"
                    if spec in active
                    else "  -  \U0001F512 locked"
                )
                label = f"{emoji} {name} ({len(items)}){tag}"
                builder.field(label, _book_table(items), False)

        builder.footer(
            "Single page -- per-category truncation keeps it readable. "
            "Use ,craft info <key> for ingredients + sources."
        )
        await ctx.send_embed(builder.build())

    # ── Leaderboard ────────────────────────────────────────────────────────

    @craft.command(name="lb", aliases=["leaderboard", "top"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_lb(self, ctx: DiscoContext) -> None:
        """Top crafters by lifetime FORGE earned."""
        rows = await craft_svc.get_top_crafters(ctx.db, ctx.guild_id, 50)
        if rows:
            from core.framework.leaderboard import filter_lb_user_ids
            keep = await filter_lb_user_ids(
                ctx, [int(r.get("user_id") or 0) for r in rows],
            )
            rows = [r for r in rows if int(r.get("user_id") or 0) in keep][:10]
        if not rows:
            await ctx.reply_error("No crafters yet -- be the first!")
            return
        lines = []
        for i, r in enumerate(rows, 1):
            uid = int(r.get("user_id"))
            forge_h = to_human(int(r.get("total_forge_earned_raw") or 0))
            crafts = int(r.get("total_crafts") or 0)
            lvl = int(r.get("crafting_level") or 1)
            lines.append(
                f"`{i:>2}.` <@{uid}> -- L{lvl} | "
                f"{forge_h:,.2f} FORGE | {crafts:,} crafts"
            )
        embed = (
            card("Top Crafters", description="\n".join(lines), color=C_GOLD)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @craft.command(name="help", aliases=["commands"])
    @guild_only
    @no_bots
    @ensure_registered
    async def craft_help(self, ctx: DiscoContext) -> None:
        """Show all crafting commands."""
        embed = (
            card("\U0001F528 Crafting Help", color=C_AMBER)
            .field("Browse",
                   "`,craft list` -- recipes at your level\n"
                   "`,craft info <key>` -- recipe details", False)
            .field("Make / apply",
                   "`,craft make <key> [qty]` -- consume inputs, mint INGOT\n"
                   "`,craft apply <key> [qty]` -- send back into source game\n"
                   "`,craft bag` -- crafted-item inventory\n"
                   "`,craft history` -- recent crafts", False)
            .field("Token economy",
                   "`,craft swap <amt|all>` -- INGOT -> FORGE (slippage)\n"
                   "`,craft cashout <amt|all>` -- FORGE -> USD (slippage)\n"
                   "`,craft stake / unstake / claim` -- INGOT staking", False)
            .footer("Crafting is fed by ,fish / ,farm / ,delve / ,buddy outputs.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Crafting(bot))
