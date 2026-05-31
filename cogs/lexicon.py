"""cogs/lexicon.py -- Player-facing item lexicon / catalog browser.

  ,db                          -- overview: item counts by kind
  ,db browse [kind]            -- paginated catalog under a kind
  ,db search <text>            -- fuzzy search by name / address / key
  ,db <name|address>           -- detail view (sources, price, supply, owned)
  ,db help

Backed by the ``item_contracts`` registry (every item the bot tracks
gets a contract). Source-of-acquisition copy comes from
:mod:`services.lexicon`.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only, no_bots
from core.framework.scale import to_human
from core.framework.ui import (
    C_AMBER, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PURPLE, C_TEAL,
)
from services import item_pricing as _pricing
from services import lexicon as _lex

log = logging.getLogger(__name__)


_KIND_EMOJI = {
    "buddy":      "\U0001F436",
    "egg":        "\U0001F423",
    "fish":       "\U0001F41F",
    "crop":       "\U0001F33E",
    "ore":        "\U000026CF",
    "weapon":     "\U00002694",
    "armor":      "\U0001F6E1",
    "consumable": "\U0001F9EA",
    "crafted":    "\U0001F528",
    "bait":       "\U0001FAB1",
    "junk":       "\U0001F45F",
    "shop":       "\U0001F6CD",
    "stone":      "\U0001F48E",
    "token":      "\U0001FA99",
}

_KIND_COLOR = {
    "buddy":      C_PURPLE,
    "egg":        C_PURPLE,
    "fish":       C_TEAL,
    "crop":       C_GOLD,
    "ore":        C_AMBER,
    "weapon":     C_AMBER,
    "armor":      C_INFO,
    "consumable": C_INFO,
    "crafted":    C_AMBER,
    "bait":       C_TEAL,
    "junk":       C_NEUTRAL,
    "shop":       C_NAVY,
    "stone":      C_GOLD,
    "token":      C_NEUTRAL,
}


def _kind_emoji(kind: str) -> str:
    return _KIND_EMOJI.get((kind or "").lower(), "\U0001F4E6")


def _kind_color(kind: str) -> int:
    return _KIND_COLOR.get((kind or "").lower(), C_GOLD)


def _chunk_lines(lines: list[str], max_chars: int = 1000,
                 max_per_chunk: int = 15) -> list[list[str]]:
    """Bucket lines into <=max_chars chunks for Discord embed fields.

    ``max_per_chunk`` defaults to 15 because each "line" passed in can be
    multi-line (we render an item as ``head\\n-# sub`` to keep text from
    overflowing) so 25 entries can blow past the 1024 field cap on dense
    catalogs.
    """
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for ln in lines:
        if cur_len + len(ln) + 1 > max_chars or len(cur) >= max_per_chunk:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        chunks.append(cur)
    return chunks


# Each browse page renders one description block per item (no fields), so
# the per-page item budget controls both readability and the 6000-char
# total-embed cap. 12 items at ~200 chars each ~= 2400 chars per page,
# well under the 4096 description / 6000 total caps.
_DB_BROWSE_PAGE_SIZE: int = 12

# Discord select options are capped at 25, so the per-page item count
# also doubles as the dropdown's option count -- keep them aligned.
assert _DB_BROWSE_PAGE_SIZE <= 25


def _format_browse_line(r: dict, price_str: str) -> str:
    """Render one catalog row as 'head\\n-# sub' for the description block.

    Shared by the cog command (initial render) and the view callbacks
    (kind-switch / page-flip) so the column layout never drifts.
    """
    addr = str(r.get("address") or "?")
    name = str(r.get("name") or addr)
    emoji = (
        str(r.get("emoji") or "")
        or _kind_emoji(str(r.get("kind") or ""))
    )
    tier = r.get("rarity_tier")
    tier_badge = f"  ·  T{int(tier)}" if tier else ""
    head = f"{emoji} **{name}**{tier_badge}"
    sub_bits = [f"`{addr}`"]
    if price_str:
        sub_bits.append(price_str)
    sub = "  ·  ".join(sub_bits)
    return f"{head}\n-# {sub}"


class _DbBrowseView(discord.ui.View):
    """Owner-locked browser: kind switcher + item picker + page nav.

    Row 0: kind select (every kind that has at least one contract,
           plus an "All kinds" option).
    Row 1: item select (up to 25 items from the current page; picking
           one swaps the embed to that contract's detail view).
    Row 2: ◀ Prev / Page x / N / Next ▶ / Refresh / Back-to-list.

    The ``Back`` button only enables itself once the user drilled into a
    detail page so they can return to the same page they came from.
    """

    def __init__(
        self,
        cog: "Lexicon",
        ctx: DiscoContext,
        rows: list[dict],
        kinds: list[tuple[str, int]],
        kind: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.rows = rows
        self.kinds = kinds
        self.kind = kind
        self.page = 0
        self.message: discord.Message | None = None
        self._browse_snap: tuple[discord.Embed, int] | None = None

    @property
    def page_count(self) -> int:
        if not self.rows:
            return 1
        return max(1, (len(self.rows) + _DB_BROWSE_PAGE_SIZE - 1) // _DB_BROWSE_PAGE_SIZE)

    def _page_slice(self) -> list[dict]:
        start = self.page * _DB_BROWSE_PAGE_SIZE
        return self.rows[start:start + _DB_BROWSE_PAGE_SIZE]

    async def _build_embed(self) -> discord.Embed:
        prefix = await self.ctx.get_guild_prefix()
        scope = f"  ·  {self.kind.title()}" if self.kind else ""
        if not self.rows:
            return (
                card(
                    f"\U0001F4D6 Item Lexicon{scope}  ·  0 entries",
                    description=(
                        "No contracts deployed for this kind yet."
                    ),
                    color=_kind_color(self.kind),
                )
                .footer(
                    f"`{prefix}db browse <kind>` to switch  ·  "
                    f"`{prefix}db <name>` to look up one item"
                )
                .build()
            )
        page_rows = self._page_slice()
        lines: list[str] = []
        for r in page_rows:
            ls_raw = r.get("last_sold_raw")
            ls_cur = r.get("last_sold_currency")
            ls_usd = r.get("last_sold_usd_raw")
            price_str = ""
            if ls_raw is not None and ls_cur:
                try:
                    h_cur = to_human(int(ls_raw))
                    bits = [f"`{h_cur:,.2f} {ls_cur}`"]
                    if ls_usd is not None:
                        bits.append(f"`${to_human(int(ls_usd)):,.2f}`")
                    price_str = "  ·  ".join(bits)
                except Exception:
                    pass
            else:
                catalog_str, _ = await _pricing.render_catalog_price(
                    self.ctx.db, self.ctx.guild_id, dict(r),
                )
                price_str = catalog_str
            lines.append(_format_browse_line(dict(r), price_str))
        # Render the page lines in the description (single block) so the
        # 6000-char total-embed cap stays well clear -- the previous
        # multi-field layout could blow past it on big catalogs.
        body = "\n".join(lines)
        # Description hard-cap is 4096; chunking budget keeps us safe.
        if len(body) > 4000:
            body = body[:3990] + "\n…"
        title = (
            f"\U0001F4D6 Item Lexicon{scope}  ·  "
            f"{len(self.rows)} entries  ·  "
            f"page {self.page + 1}/{self.page_count}"
        )
        return (
            card(title, description=body, color=_kind_color(self.kind))
            .footer(
                f"Pick a kind / item below  ·  "
                f"`{prefix}db <name>` for one item's full entry"
            )
            .build()
        )

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(_DbKindSelect(self))
        self.add_item(_DbItemSelect(self))
        self.add_item(_DbPrevButton(disabled=self.page <= 0))
        self.add_item(_DbNextButton(
            disabled=self.page >= self.page_count - 1,
        ))
        self.add_item(_DbRefreshButton())
        self.add_item(_DbBackButton(disabled=self._browse_snap is None))

    async def _redraw(self, interaction: discord.Interaction) -> None:
        embed = await self._build_embed()
        self._browse_snap = None  # leaving detail view, drop the breadcrumb
        self._rebuild()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            log.debug("db browse redraw failed", exc_info=True)

    async def switch_kind(
        self, interaction: discord.Interaction, kind: str,
    ) -> None:
        self.kind = kind
        self.page = 0
        self.rows = await self.cog._fetch_browse_rows(self.ctx, kind)
        await self._redraw(interaction)

    async def open_detail(
        self, interaction: discord.Interaction, address: str,
    ) -> None:
        row = await self.ctx.db.fetch_one(
            "SELECT * FROM item_contracts WHERE address = $1",
            address,
        )
        if not row:
            await interaction.response.send_message(
                f"Contract `{address}` not found.", ephemeral=True,
            )
            return
        embed = await self.cog._build_detail_embed(self.ctx, dict(row))
        # Save current state so the Back button can restore it without
        # re-fetching every catalog row from postgres.
        self._browse_snap = (await self._build_embed(), self.page)
        # Keep the kind / item dropdowns visible so the user can pivot
        # straight to another item without going back to the list first.
        self._rebuild()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            log.debug("db browse detail edit failed", exc_info=True)

    async def restore_browse(
        self, interaction: discord.Interaction,
    ) -> None:
        if self._browse_snap is None:
            await self._redraw(interaction)
            return
        embed, page = self._browse_snap
        self.page = page
        self._browse_snap = None
        self._rebuild()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            log.debug("db browse back edit failed", exc_info=True)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your lexicon panel. Run `,db browse` to open your own.",
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


class _DbKindSelect(discord.ui.Select):
    def __init__(self, parent: "_DbBrowseView") -> None:
        opts: list[discord.SelectOption] = [
            discord.SelectOption(
                label="All kinds", value="_all_",
                emoji="\U0001F4D6",
                default=(parent.kind == ""),
            ),
        ]
        for kind, n in parent.kinds[:24]:
            opts.append(discord.SelectOption(
                label=f"{kind.title()}  ·  {n} items"[:100],
                value=kind or "_all_",
                emoji=_kind_emoji(kind),
                default=(parent.kind == kind),
            ))
        super().__init__(
            placeholder="Pick a kind to browse...",
            options=opts,
            min_values=1, max_values=1, row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DbBrowseView" = self.view  # type: ignore[assignment]
        raw = str(self.values[0])
        await view.switch_kind(interaction, "" if raw == "_all_" else raw)


class _DbItemSelect(discord.ui.Select):
    def __init__(self, parent: "_DbBrowseView") -> None:
        page_rows = parent._page_slice()
        opts: list[discord.SelectOption] = []
        for r in page_rows:
            addr = str(r.get("address") or "?")
            name = str(r.get("name") or addr)
            emoji = (
                str(r.get("emoji") or "")
                or _kind_emoji(str(r.get("kind") or ""))
            )
            tier = r.get("rarity_tier")
            tier_badge = f" T{int(tier)}" if tier else ""
            opts.append(discord.SelectOption(
                label=f"{name}{tier_badge}"[:100],
                value=addr[:100],
                description=addr[:100],
                emoji=emoji,
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no items on this page)", value="_none_",
            )]
        super().__init__(
            placeholder="Inspect an item from this page...",
            options=opts,
            min_values=1, max_values=1, row=1,
            disabled=not page_rows,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DbBrowseView" = self.view  # type: ignore[assignment]
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        await view.open_detail(interaction, v)


class _DbPrevButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(
            label="Prev", emoji="\U000025C0",
            style=discord.ButtonStyle.secondary, row=2,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DbBrowseView" = self.view  # type: ignore[assignment]
        if view.page > 0:
            view.page -= 1
        await view._redraw(interaction)


class _DbNextButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(
            label="Next", emoji="\U000025B6",
            style=discord.ButtonStyle.secondary, row=2,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DbBrowseView" = self.view  # type: ignore[assignment]
        if view.page < view.page_count - 1:
            view.page += 1
        await view._redraw(interaction)


class _DbRefreshButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Refresh", emoji="\U0001F504",
            style=discord.ButtonStyle.secondary, row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DbBrowseView" = self.view  # type: ignore[assignment]
        view.rows = await view.cog._fetch_browse_rows(view.ctx, view.kind)
        # Clamp page index in case rows shrank under the cursor.
        if view.page >= view.page_count:
            view.page = max(0, view.page_count - 1)
        await view._redraw(interaction)


class _DbBackButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(
            label="Back", emoji="\U000021A9",
            style=discord.ButtonStyle.primary, row=2,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_DbBrowseView" = self.view  # type: ignore[assignment]
        await view.restore_browse(interaction)


class Lexicon(commands.Cog, name="Item Lexicon"):
    """`,db` -- browse / search every item the bot tracks."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ── ,db (overview) ────────────────────────────────────────────────────

    @commands.hybrid_group(
        name="db",
        aliases=["lexicon", "wiki", "find", "lookup"],
        invoke_without_command=True, with_app_command=False,
    )
    @guild_only
    @no_bots
    async def db(
        self, ctx: DiscoContext, *, query: str = "",
    ) -> None:
        """Item lexicon: browse, search, or look up any item.

        With no args: overview of every kind + count of contracts.
        With an arg: treated as a search/lookup -- exact-address
        match wins; otherwise fuzzy-search across name + key.
        """
        q = (query or "").strip()
        if q:
            await self._lookup_or_search(ctx, q)
            return
        await self.db_browse(ctx, kind="")

    async def _overview(self, ctx: DiscoContext) -> None:
        rows = await ctx.db.fetch_all(
            "SELECT kind, COUNT(*) AS n FROM item_contracts "
            "GROUP BY kind ORDER BY n DESC",
        )
        if not rows:
            await ctx.reply_error(
                "Item lexicon is empty. Contracts deploy on bot startup; "
                "check back after the next boot."
            )
            return
        prefix = await ctx.get_guild_prefix()
        total = sum(int(r["n"]) for r in rows)
        lines = []
        for r in rows:
            kind = str(r["kind"])
            n = int(r["n"])
            lines.append(
                f"{_kind_emoji(kind)} **{kind.title()}**  ·  `{n}` items"
            )
        embed = (
            card(
                f"\U0001F4D6 Item Lexicon  ·  {total} contracts deployed",
                description="\n".join(lines),
                color=C_GOLD,
            )
            .footer(
                f"`{prefix}db browse <kind>` to list a kind  ·  "
                f"`{prefix}db search <text>` to search  ·  "
                f"`{prefix}db <name>` for one item"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _lookup_or_search(self, ctx: DiscoContext, q: str) -> None:
        # Exact address match wins. Then exact catalog_key. Then fuzzy.
        addr_q = q.strip().lower()
        exact = await ctx.db.fetch_one(
            "SELECT * FROM item_contracts WHERE address = $1",
            addr_q,
        )
        if exact:
            await self._render_detail(ctx, dict(exact))
            return
        key_q = addr_q.replace(" ", "_").replace("-", "_")
        by_key = await ctx.db.fetch_all(
            "SELECT * FROM item_contracts "
            "WHERE LOWER(catalog_key) = $1 ORDER BY kind, address LIMIT 5",
            key_q,
        )
        if by_key and len(by_key) == 1:
            await self._render_detail(ctx, dict(by_key[0]))
            return
        await self._render_search(ctx, q)


    # ── ,db browse [kind] ─────────────────────────────────────────────────

    async def _fetch_browse_rows(
        self, ctx: DiscoContext, kind: str,
    ) -> list[dict]:
        """Catalog-row fetch shared by the cog command + the view's
        kind-switch and refresh callbacks. Each row includes the
        most-recent sold event so the row can render last-sale price
        (network coin + USD) inline; falls back to base_price_raw
        (catalog USD via :mod:`item_pricing`) when no sale has settled.
        """
        if kind:
            rows = await ctx.db.fetch_all(
                """
                WITH last_sold AS (
                    SELECT DISTINCT ON (contract_id)
                           contract_id, price_raw, currency, price_usd_raw
                      FROM item_token_events
                     WHERE event_type = 'sold' AND contract_id IS NOT NULL
                     ORDER BY contract_id, created_at DESC
                )
                SELECT ic.*,
                       ls.price_raw      AS last_sold_raw,
                       ls.currency       AS last_sold_currency,
                       ls.price_usd_raw  AS last_sold_usd_raw
                  FROM item_contracts ic
                  LEFT JOIN last_sold ls ON ls.contract_id = ic.contract_id
                 WHERE ic.kind = $1
                 ORDER BY COALESCE(ic.rarity_tier, 0), ic.name
                 LIMIT 500
                """,
                kind,
            )
        else:
            rows = await ctx.db.fetch_all(
                """
                WITH last_sold AS (
                    SELECT DISTINCT ON (contract_id)
                           contract_id, price_raw, currency, price_usd_raw
                      FROM item_token_events
                     WHERE event_type = 'sold' AND contract_id IS NOT NULL
                     ORDER BY contract_id, created_at DESC
                )
                SELECT ic.*,
                       ls.price_raw      AS last_sold_raw,
                       ls.currency       AS last_sold_currency,
                       ls.price_usd_raw  AS last_sold_usd_raw
                  FROM item_contracts ic
                  LEFT JOIN last_sold ls ON ls.contract_id = ic.contract_id
                 ORDER BY ic.kind, COALESCE(ic.rarity_tier, 0), ic.name
                 LIMIT 1000
                """,
            )
        return [dict(r) for r in (rows or [])]

    @db.command(name="browse", aliases=["list", "all", "kind"])
    @guild_only
    @no_bots
    async def db_browse(
        self, ctx: DiscoContext, kind: str = "",
    ) -> None:
        """Interactive catalog browser with kind + item dropdowns.

        Picks a kind from the row-0 select to switch the catalog, then
        an item from the row-1 select to drill into a single contract's
        detail view. Prev / Next paginate through the catalog so big
        kinds (200+ contracts) never blow Discord's 6000-char embed cap.
        """
        k = (kind or "").strip().lower()
        rows = await self._fetch_browse_rows(ctx, k)
        # Kind catalog used by the kind dropdown -- needs the running
        # count per kind so the dropdown labels stay informative.
        kind_rows = await ctx.db.fetch_all(
            "SELECT kind, COUNT(*) AS n FROM item_contracts "
            "GROUP BY kind ORDER BY n DESC",
        )
        kinds = [(str(r["kind"]), int(r["n"])) for r in (kind_rows or [])]
        if not rows and not kinds:
            await ctx.reply_error(
                f"No contracts deployed{f' for `{k}`' if k else ''}."
            )
            return
        view = _DbBrowseView(self, ctx, rows, kinds, k)
        view._rebuild()
        embed = await view._build_embed()
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    # ── ,db search <text> ─────────────────────────────────────────────────

    @db.command(name="search", aliases=["find", "s"])
    @guild_only
    @no_bots
    async def db_search(self, ctx: DiscoContext, *, query: str = "") -> None:
        """Free-text search across name / address / key. Substring match."""
        q = (query or "").strip()
        if not q:
            await ctx.reply_error("Pass a search term, e.g. `,db search worm`.")
            return
        await self._render_search(ctx, q)

    async def _render_search(self, ctx: DiscoContext, q: str) -> None:
        needle = q.strip().lower()
        rows = await ctx.db.fetch_all(
            """
            SELECT *,
                   CASE
                     WHEN LOWER(address) = $1     THEN 0
                     WHEN LOWER(catalog_key) = $1 THEN 1
                     WHEN LOWER(name) = $1        THEN 2
                     WHEN LOWER(name) LIKE '%' || $1 || '%' THEN 3
                     ELSE 4
                   END AS match_rank
              FROM item_contracts
             WHERE LOWER(address) LIKE '%' || $1 || '%'
                OR LOWER(catalog_key) LIKE '%' || $1 || '%'
                OR LOWER(name) LIKE '%' || $1 || '%'
             ORDER BY match_rank ASC, kind, name
             LIMIT 30
            """,
            needle,
        )
        if not rows:
            await ctx.reply_error(
                f"No items match `{q}`. Try `,db browse` for the full list."
            )
            return
        if len(rows) == 1:
            await self._render_detail(ctx, dict(rows[0]))
            return
        prefix = await ctx.get_guild_prefix()
        lines = []
        for r in rows:
            addr = str(r.get("address") or "?")
            name = str(r.get("name") or addr)
            kind = str(r.get("kind") or "")
            emoji = str(r.get("emoji") or "") or _kind_emoji(kind)
            lines.append(
                f"{emoji} `{addr}`  ·  **{name}**  ·  {kind}"
            )
        chunks = _chunk_lines(lines)
        builder = card(
            f"\U0001F50D Search: `{q}`  ·  {len(rows)} matches",
            color=C_INFO,
        )
        for i, c in enumerate(chunks):
            label = "Matches" if i == 0 else f"Matches ({i + 1})"
            builder = builder.field(label, "\n".join(c), False)
        builder = builder.footer(
            f"`{prefix}db <address>` for the full entry on one item"
        )
        await ctx.reply(embed=builder.build(), mention_author=False)


    # ── detail render ─────────────────────────────────────────────────────

    async def _build_detail_embed(
        self, ctx: DiscoContext, c: dict,
    ) -> discord.Embed:
        """Build the single-item detail embed without sending it.

        Split out from ``_render_detail`` so the browse view can swap the
        same message between catalog + detail without re-fetching or
        re-posting.
        """
        addr = str(c.get("address") or "?")
        name = str(c.get("name") or addr)
        kind = str(c.get("kind") or "")
        emoji = str(c.get("emoji") or "") or _kind_emoji(kind)

        # Population stats: total minted + alive + my owned.
        stats = await ctx.db.fetch_one(
            """
            SELECT
                COUNT(*)                                    AS total_minted,
                COUNT(*) FILTER (WHERE burned_at IS NULL)   AS alive,
                COUNT(*) FILTER (
                    WHERE burned_at IS NULL
                      AND owner_user_id = $2
                )                                           AS mine
              FROM item_instances
             WHERE contract_id = $1
            """,
            int(c["contract_id"]), int(ctx.author.id),
        )
        total = int((stats or {}).get("total_minted") or 0)
        alive = int((stats or {}).get("alive") or 0)
        mine = int((stats or {}).get("mine") or 0)
        burned = total - alive

        builder = card(
            f"{emoji} {name}",
            color=_kind_color(kind),
        )
        builder = builder.field("Address", f"`{addr}`", True)
        builder = builder.field("Kind", kind, True)
        rarity = c.get("rarity_tier")
        if rarity:
            builder = builder.field("Rarity", f"T{int(rarity)}", True)
        catalog_str, _ = await _pricing.render_catalog_price(
            ctx.db, ctx.guild_id, dict(c),
        )
        if catalog_str:
            builder = builder.field("Catalog price", catalog_str, True)

        # Source-of-acquisition lines, dashed.
        sources = _lex.source_lines(c)
        builder = builder.field(
            "Where to get it",
            "\n".join(f"- {ln}" for ln in sources),
            False,
        )

        builder = builder.field(
            "Supply",
            (
                f"`{alive}` alive  ·  `{burned}` burned  ·  "
                f"`{total}` total minted"
            ),
            False,
        )
        builder = builder.field("You own", f"`{mine}`", True)

        prefix = await ctx.get_guild_prefix()
        sell_hint = (
            f"`{prefix}ah list {c.get('catalog_key') or addr} <price>` to sell one"
            if int(mine) > 0 else None
        )
        footer_bits = [
            f"`{prefix}items list {kind}` to see what you own",
            f"`{prefix}ah browse {kind}` for live listings",
        ]
        if sell_hint:
            footer_bits.insert(0, sell_hint)
        builder = builder.footer("  ·  ".join(footer_bits))
        return builder.build()

    async def _render_detail(self, ctx: DiscoContext, c: dict) -> None:
        """Full entry for a single contract -- sources, supply, owned."""
        embed = await self._build_detail_embed(ctx, c)
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,db help ──────────────────────────────────────────────────────────

    @db.command(name="help", aliases=["commands", "?"])
    @guild_only
    @no_bots
    async def db_help(self, ctx: DiscoContext) -> None:
        """Quick reference for the item lexicon."""
        prefix = await ctx.get_guild_prefix()
        body = (
            f"**Browse**\n"
            f"`{prefix}db`  -  count of items per kind (overview)\n"
            f"`{prefix}db browse [kind]`  -  list items in a kind\n"
            f"`{prefix}db search <text>`  -  fuzzy search by name / address\n"
            f"\n"
            f"**Look up one item**\n"
            f"`{prefix}db <name>`  -  e.g. `{prefix}db worm`\n"
            f"`{prefix}db <address>`  -  e.g. `{prefix}db bait.worm`\n"
            f"\n"
            f"**Kinds**: buddy / egg / fish / bait / junk / crop / "
            f"weapon / armor / consumable / crafted / stone / shop / ore.\n"
            f"\n"
            f"Address shape is `<kind>.<catalog_key>` (e.g. `bait.worm`, "
            f"`weapon.bronze_sword`, `egg.zenny`). Per-unit token ids use "
            f"`<network>:<hex>` -- look those up via `{prefix}items inspect`."
        )
        embed = card(
            "\U0001F4D6 Item Lexicon Help",
            color=C_GOLD,
            description=body,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Lexicon(bot))
