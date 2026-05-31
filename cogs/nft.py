"""cogs/nft.py -- Player-facing NFT explorer + transfer.

Exposes the per-unit NFT layer (:mod:`services.items`) to players:

  ,nft                              -- overview: count by kind
  ,nft list [kind]                  -- list owned tokens, paginated
  ,nft inspect <token_id>           -- full details for one token
  ,nft contracts [kind]             -- catalog of deployed contracts
  ,nft contract <address>           -- details for one contract
  ,nft transfer <token_id> @user    -- gift a token to another player
  ,nft help

Read paths are presentation-only against ``item_instances`` /
``item_contracts``. The transfer command is the player's gift surface
that mirrors what the AH does internally on a sale.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import (
    C_AMBER, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PURPLE, C_SUCCESS, C_TEAL,
    fmt_ts,
)
from core.framework.scale import to_human
from services import item_pricing as _pricing
from services import items as _items

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


# Short-network -> full display name. Keeps the token id short (bud:k889..)
# but lets the inspect panel say "Buddy Network" instead of "bud".
_NETWORK_FULL = {
    "bud":   "Buddy Network",
    "lur":   "Lure Network",
    "reel":  "Lure Network",
    "har":   "Harvest Network",
    "cry":   "Crypt Network",
    "rune":  "Crypt Network",
    "fge":   "Forge Network",
    "dsc":   "Discoin Network",
    "arc":   "Arcadia Network",
    "mta":   "Moneta Chain",
    "sun":   "Sun Network",
    "moon":  "Moon Network",
}


def _network_full(short: str) -> str:
    """Map a short network code (``bud``) to its display name
    (``Buddy Network``). Falls back to the original code in ALLCAPS
    when the mapping is missing so unknown networks still look
    intentional rather than blank.
    """
    s = (short or "").strip().lower()
    if not s:
        return "?"
    return _NETWORK_FULL.get(s, f"{s.upper()} Network")


def _short(token_id: str) -> str:
    return _items.short_id(token_id)


# Pretty per-event glyph for the inspect history field.
_EVENT_GLYPH = {
    "mint":     "\U0001F195",       # NEW
    "transfer": "\U0001F381",       # gift
    "list":     "\U0001F3DB",       # auction
    "unlist":   "\U000021A9",       # arrow back
    "sold":     "\U0001F4B0",       # money bag
    "burn":     "\U0001F525",       # fire
}


def _fmt_network_and_usd(price_raw: int | None, currency: str | None,
                         price_usd_raw: int | None) -> str:
    """Render '`123.00 LURE`  ·  `$45.21 USD`' for price displays."""
    parts: list[str] = []
    if price_raw is not None and currency:
        try:
            human = to_human(int(price_raw))
            parts.append(f"`{human:,.2f} {currency}`")
        except Exception:
            pass
    if price_usd_raw is not None:
        try:
            usd_h = to_human(int(price_usd_raw))
            parts.append(f"`${usd_h:,.2f}`")
        except Exception:
            pass
    return "  ·  ".join(parts) if parts else "_no price_"


def _kind_emoji(kind: str) -> str:
    return _KIND_EMOJI.get((kind or "").lower(), "\U0001F4E6")


def _kind_color(kind: str) -> int:
    return _KIND_COLOR.get((kind or "").lower(), C_GOLD)


# All kinds the bot tracks (matches services/items.py KIND_NETWORK_DEFAULTS
# plus the post-Phase-1 additions). Used by the dropdown filter.
_ALL_KINDS = (
    "buddy", "egg", "fish", "bait", "junk", "crop",
    "weapon", "armor", "consumable", "crafted", "stone", "shop",
)

_PER_PAGE = 8           # Contract rows per ,items list page.
_SORT_NEXT = {
    "count":     "usd",
    "usd":       "name",
    "name":      "count",
}
_SORT_LABEL = {
    "count": "By count",
    "usd":   "By USD",
    "name":  "By name",
}


class _ItemsKindSelect(discord.ui.Select):
    """Tier 1 dropdown -- pick a category (kind).

    Selecting a category resets the stack + token selection so the
    browser falls back to the overview embed for the chosen kind.
    """

    def __init__(self, current: str | None) -> None:
        opts = [
            discord.SelectOption(
                label="All kinds",
                value="__all__",
                emoji="\U0001F4E6",
                description="Mixed view, every kind",
                default=(current is None),
            ),
        ]
        for k in _ALL_KINDS:
            opts.append(discord.SelectOption(
                label=k.title(),
                value=k,
                emoji=_kind_emoji(k),
                default=(current == k),
            ))
        super().__init__(
            placeholder="1. Category -- filter by kind...",
            options=opts,
            min_values=1, max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "ItemsView" = self.view  # type: ignore
        choice = self.values[0]
        view.kind = None if choice == "__all__" else choice
        view.stack_addr = None
        view.selected_token = None
        view.page = 1
        await view.refresh(interaction)


class _StackSelect(discord.ui.Select):
    """Tier 2 dropdown -- pick a specific item stack (contract) the
    player owns within the current category.

    "All stacks" reverts to the category overview. Picking a stack
    drills the embed into stack-detail mode and repopulates the token
    picker with that stack's individual tokens.
    """

    def __init__(
        self, stacks: list[dict], current_addr: str | None,
    ) -> None:
        opts: list[discord.SelectOption] = [
            discord.SelectOption(
                label="All stacks",
                value="__all__",
                emoji="\U0001F4E6",
                description="Show every stack in the current category.",
                default=(current_addr is None),
            ),
        ]
        for s in stacks[:24]:
            addr = str(s.get("address") or "")
            if not addr:
                continue
            kind = str(s.get("kind") or "")
            n = int(s.get("owned_n") or 0)
            tier = s.get("rarity_tier")
            tier_part = f"  T{int(tier)}" if tier else ""
            label = f"{s.get('name') or addr} ×{n}{tier_part}"
            emoji = str(s.get("emoji") or "") or _kind_emoji(kind)
            opts.append(discord.SelectOption(
                label=label[:100],
                value=addr,
                emoji=emoji or None,
                description=f"`{addr}`"[:100],
                default=(addr == (current_addr or "").lower()),
            ))
        disabled = False
        if len(opts) == 1:
            opts.append(discord.SelectOption(
                label="(no stacks to show)",
                value="__none__",
                description="Catch / harvest / craft / shop to mint a stack.",
            ))
            disabled = True
        super().__init__(
            placeholder="2. Item stack -- pick a contract...",
            options=opts,
            min_values=1, max_values=1,
            row=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "ItemsView" = self.view  # type: ignore
        v = str(self.values[0])
        if v == "__none__":
            await interaction.response.defer()
            return
        view.stack_addr = None if v == "__all__" else v
        view.selected_token = None
        await view.refresh(interaction)


class _TokenPickSelect(discord.ui.Select):
    """Tier 3 dropdown -- pick a specific token within the chosen stack.

    Falls back to a category-wide token list when no stack is
    selected. Each option exposes the full token id so the player can
    tell siblings apart (e.g. ``Trout 4.2 lbs - lur:abc12``). Selecting
    a token pivots the embed to a token-detail view and enables the
    List on AH / Transfer / Inspect action buttons.
    """

    def __init__(
        self, tokens: list[dict], current_token: str | None,
    ) -> None:
        opts: list[discord.SelectOption] = []
        for tok in tokens[:25]:
            tid = str(tok.get("token_id") or "")
            if not tid:
                continue
            kind = str(tok.get("kind") or "")
            md = tok.get("metadata") or {}
            cname = str(tok.get("contract_name") or "")
            cemoji = str(tok.get("contract_emoji") or "") or _kind_emoji(kind)
            label = _token_pick_label(kind, cname, md, tid)
            desc = _token_pick_desc(kind, md, tid) or tid
            opts.append(discord.SelectOption(
                label=label[:100],
                value=tid,
                description=desc[:100] if desc else None,
                emoji=cemoji or None,
                default=(tid == (current_token or "")),
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no tokens to pick)",
                value="__none__",
                description="Catch / harvest / craft something first.",
                default=True,
            )]
        super().__init__(
            placeholder="3. Item -- pick a specific token...",
            options=opts,
            min_values=1, max_values=1, row=2,
            disabled=(opts[0].value == "__none__"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "ItemsView" = self.view  # type: ignore
        v = str(self.values[0])
        if v == "__none__":
            await interaction.response.defer()
            return
        view.selected_token = v
        await view.refresh(interaction)


def _token_pick_label(kind: str, cname: str, md: dict, tid: str) -> str:
    """Build the dropdown option label for one token.

    Includes a short token id so the player can copy it without opening
    the inspect view. Per-kind metadata enriches the label where it
    helps choose between siblings (lbs for fish, rarity for egg /
    crafted, level for buddy, etc.).
    """
    short = _short(tid)
    extra = ""
    if kind == "fish":
        lbs = md.get("lbs")
        if lbs is not None:
            try:
                extra = f" - {float(lbs):.2f} lbs"
            except (TypeError, ValueError):
                pass
    elif kind == "egg":
        try:
            from configs.buddies_config import rarity_meta as _rm
            rt = int(md.get("rarity_tier") or 1)
            extra = f" - {_rm(rt).get('name') or f'T{rt}'}"
        except Exception:
            pass
    elif kind == "buddy":
        nm = md.get("name")
        lvl = md.get("level")
        if nm or lvl is not None:
            extra = " - " + " ".join(
                str(x) for x in [
                    f"{nm}" if nm else None,
                    f"L{int(lvl)}" if lvl is not None else None,
                ] if x is not None
            )
    elif kind == "crop":
        try:
            from configs.buddies_config import rarity_meta as _rm
            rt = int(md.get("rarity_tier") or 1)
            extra = f" - {_rm(rt).get('name') or f'T{rt}'}"
        except Exception:
            pass
    label = f"{cname or kind.title()}{extra}  ·  {short}"
    return label


def _token_pick_desc(kind: str, md: dict, tid: str) -> str:
    """Optional second-line description on the dropdown option."""
    bits: list[str] = []
    if kind == "fish":
        species = md.get("species") or md.get("fish_key")
        if species:
            bits.append(str(species))
    if kind == "egg" or kind == "buddy":
        species = md.get("species")
        if species:
            bits.append(str(species).title())
    if not bits:
        return ""
    return " · ".join(bits)


async def _build_inspect_embed_for_token(
    ctx: DiscoContext, tok: dict, contract: dict | None,
) -> discord.Embed:
    """Render the same inspect embed ``,items inspect <token_id>`` builds
    so the picker dropdown's selection result is visually identical to
    the manual flow. Pulled out so both surfaces use one renderer.
    """
    kind = str(tok.get("kind") or "")
    emoji = (
        str((contract or {}).get("emoji") or "")
        or _kind_emoji(kind)
    )
    name = str((contract or {}).get("name") or kind.title())
    addr = str((contract or {}).get("address") or "?")
    owner = tok.get("owner_user_id")
    burned_at = tok.get("burned_at")
    listing_id = tok.get("listing_id")

    builder = card(f"{emoji} {name}", color=_kind_color(kind))
    builder = builder.field("Token id", f"`{tok['token_id']}`", False)
    builder = builder.field("Contract", f"`{addr}` ({kind})", True)
    builder = builder.field(
        "Network", _network_full(str(tok.get("network") or "")), True,
    )
    if burned_at:
        builder = builder.field(
            "Status", f"\U0001F525 Burned ({fmt_ts(burned_at)})", True,
        )
    elif listing_id:
        builder = builder.field(
            "Status",
            f"\U0001F3DB Escrowed in listing #{int(listing_id)}",
            True,
        )
    elif owner is None:
        builder = builder.field("Status", "\U0001F3E0 Unowned", True)
    else:
        builder = builder.field("Owner", f"<@{int(owner)}>", True)

    md = tok.get("metadata") or {}
    if isinstance(md, str):
        try:
            import json as _json
            md = _json.loads(md)
        except Exception:
            md = {}
    if md:
        interesting = {
            k: v for k, v in md.items()
            if k not in ("contract", "unit_index")
            and v not in (None, "", [], {})
        }
        if interesting:
            lines = []
            for k, v in interesting.items():
                s = f"`{k}` -> {v}"
                if sum(len(x) for x in lines) + len(s) > 900:
                    break
                lines.append(s)
            builder = builder.field("Metadata", "\n".join(lines), False)

    prefix = await ctx.get_guild_prefix()
    builder = builder.footer(
        f"`{prefix}items transfer {tok['token_id']} @user` to gift"
    )
    return builder.build()


class ItemsView(discord.ui.View):
    """Interactive 3-tier ``,items`` browser.

    Owner-locked, 5 minute timeout. Layout (top to bottom):
      * Row 0 -- Category dropdown (kind: buddy/egg/fish/...).
      * Row 1 -- Item-stack dropdown (a contract within the category).
      * Row 2 -- Item dropdown (a specific token within the stack).
      * Row 3 -- Prev / Next / Sort / Refresh.
      * Row 4 -- List on AH / Transfer / Inspect / Sell Junk.

    Selecting a category resets the stack + token; selecting a stack
    resets the token. The embed pivots between three modes based on
    state:
      1. Overview     -- no stack picked. Lists every owned stack.
      2. Stack detail -- stack picked, no token. Stack stats + the
                         stack's individual tokens.
      3. Token detail -- a token is picked. Inline inspect view.

    Action buttons are auto-disabled when they don't apply (e.g.
    Transfer is disabled when no token is selected; Sell Junk only
    enables in the junk category).
    """

    def __init__(
        self, ctx: DiscoContext, *,
        kind: str | None = None,
        stack_addr: str | None = None,
        sort: str = "count",
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.kind = kind
        self.stack_addr = (stack_addr or None)
        self.selected_token: str | None = None
        self.sort = sort
        self.page = 1
        self.message: discord.Message | None = None
        self.add_item(_ItemsKindSelect(kind))

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your items session. Run `,items` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def _fetch_owned_tokens(self, limit: int = 25) -> list[dict]:
        """Pull up to ``limit`` of the player's owned individual tokens
        for the tier-3 token picker. Filters by ``self.stack_addr`` (the
        chosen stack/contract) when set, else falls back to the
        category-wide ``self.kind`` filter, else returns an unscoped
        owned-token list.

        Each row carries token_id + contract metadata + the per-token
        metadata blob so the picker label can render meaningful detail
        (lbs for fish, rarity for egg, level for buddy, etc.).
        """
        params: list = [self.ctx.guild_id, self.ctx.author.id]
        clause = ""
        if self.stack_addr:
            params.append(self.stack_addr.lower())
            clause = f"AND ic.address = ${len(params)}"
        elif self.kind:
            params.append(self.kind)
            clause = f"AND ii.kind = ${len(params)}"
        rows = await self.ctx.db.fetch_all(
            f"""
            SELECT ii.token_id, ii.kind, ii.metadata,
                   ii.contract_id,
                   ic.name      AS contract_name,
                   ic.emoji     AS contract_emoji,
                   ic.address   AS contract_address,
                   ic.catalog_key
              FROM item_instances ii
              LEFT JOIN item_contracts ic ON ic.contract_id = ii.contract_id
             WHERE ii.guild_id = $1
               AND ii.owner_user_id = $2
               AND ii.burned_at IS NULL
               AND ii.listing_id IS NULL
               {clause}
             ORDER BY ii.minted_at DESC NULLS LAST, ii.token_id ASC
             LIMIT {int(limit)}
            """,
            *params,
        )
        out: list[dict] = []
        for r in (rows or []):
            d = dict(r)
            md = d.get("metadata") or {}
            if isinstance(md, str):
                try:
                    import json as _json
                    md = _json.loads(md)
                except Exception:
                    md = {}
            d["metadata"] = md
            out.append(d)
        return out

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

    async def _fetch(self) -> tuple[list[dict], int]:
        """Pull contract rows + per-contract last-sold USD + count of
        owned tokens, optionally filtered by kind. Returns
        ``(rows, total_pages)``.
        """
        # Note: ``unit_usd_raw`` was removed from the SELECT in the
        # native-catalog-price refactor (last-sold + base + native are
        # selected separately now and merged client-side). The "usd"
        # branch DOES NOT reference it from SQL anymore -- the per-row
        # Python pricing pass below resolves usd_for_sort and the
        # ``rows.sort(key=...)`` block past line 320 produces the final
        # USD-descending order. SQL just gives a stable initial sort.
        sort_clause = {
            "count": "owned_n DESC, name ASC",
            "usd":   "owned_n DESC, name ASC",
            "name":  "name ASC",
        }.get(self.sort, "owned_n DESC, name ASC")

        kind_clause = "AND ic.kind = $3" if self.kind else ""
        params = [self.ctx.guild_id, self.ctx.author.id]
        if self.kind:
            params.append(self.kind)

        rows = await self.ctx.db.fetch_all(
            f"""
            WITH owned AS (
                SELECT contract_id, COUNT(*) AS owned_n
                  FROM item_instances
                 WHERE guild_id = $1
                   AND owner_user_id = $2
                   AND burned_at IS NULL
                 GROUP BY contract_id
            ),
            last_sold AS (
                SELECT DISTINCT ON (contract_id)
                       contract_id, price_usd_raw, price_raw, currency
                  FROM item_token_events
                 WHERE event_type = 'sold' AND contract_id IS NOT NULL
                 ORDER BY contract_id, created_at DESC
            )
            SELECT ic.contract_id, ic.address, ic.name, ic.kind, ic.emoji,
                   ic.rarity_tier, ic.base_price_raw,
                   ic.base_price_native_raw, ic.base_price_currency,
                   o.owned_n,
                   ls.price_usd_raw   AS last_sold_usd_raw,
                   ls.price_raw       AS last_sold_raw,
                   ls.currency        AS last_sold_currency
              FROM owned o
              JOIN item_contracts ic ON ic.contract_id = o.contract_id
              LEFT JOIN last_sold ls ON ls.contract_id = o.contract_id
             WHERE 1=1 {kind_clause}
             ORDER BY {sort_clause}
            """,
            *params,
        )
        rows = [dict(r) for r in (rows or [])]
        # Resolve USD-per-unit + native-currency display for sort + render.
        # Priority: last sale's USD snapshot -> catalog USD -> oracle-converted
        # catalog native. Stash on the row so _build_embed doesn't re-query.
        for r in rows:
            usd_for_sort: int | None = None
            ls_usd = r.get("last_sold_usd_raw")
            if ls_usd is not None:
                try:
                    usd_for_sort = int(ls_usd)
                except (TypeError, ValueError):
                    usd_for_sort = None
            if usd_for_sort is None:
                price_str, usd_raw = await _pricing.render_catalog_price(
                    self.ctx.db, self.ctx.guild_id, r,
                )
                r["_catalog_price_str"] = price_str
                usd_for_sort = usd_raw
            else:
                r["_catalog_price_str"] = ""
            r["unit_usd_raw"] = usd_for_sort
        # Re-sort if we sorted by USD (DB sort used base_price_raw only, but
        # we now have native-converted USD values too).
        if self.sort == "usd":
            rows.sort(
                key=lambda r: (
                    -(int(r.get("unit_usd_raw") or 0)),
                    -(int(r.get("owned_n") or 0)),
                ),
            )
        total_pages = max(1, (len(rows) + _PER_PAGE - 1) // _PER_PAGE)
        return rows, total_pages

    def _build_overview_embed(
        self, rows: list[dict], total_pages: int,
    ) -> discord.Embed:
        page = max(1, min(self.page, total_pages))
        slice_rows = rows[(page - 1) * _PER_PAGE : page * _PER_PAGE]

        total_count = sum(int(r.get("owned_n") or 0) for r in rows)
        total_usd_raw = sum(
            int((r.get("unit_usd_raw") or 0)) * int(r.get("owned_n") or 0)
            for r in rows
        )

        kind_label = self.kind.title() if self.kind else "All kinds"
        title_usd = (
            f"  ·  ${to_human(total_usd_raw):,.2f}"
            if total_usd_raw > 0 else ""
        )
        builder = card(
            f"\U0001F4E6 Your Items  ·  {kind_label}  ·  "
            f"{total_count} units{title_usd}",
            color=_kind_color(self.kind or ""),
        )

        if not slice_rows:
            builder = builder.field(
                "Nothing here",
                "You don't own any NFTs in this kind. Catch / harvest / "
                "craft / shop something, or buy from `,ah`.",
                False,
            )
            return builder.footer(
                f"Sort: {_SORT_LABEL.get(self.sort, self.sort)}  ·  "
                f"Page {page}/{total_pages}"
            ).build()

        lines: list[str] = []
        for r in slice_rows:
            addr = str(r.get("address") or "?")
            name = str(r.get("name") or addr)
            emoji = str(r.get("emoji") or "") or _kind_emoji(
                r.get("kind") or "",
            )
            n = int(r.get("owned_n") or 0)
            tier = r.get("rarity_tier")
            tier_badge = f"  ·  T{int(tier)}" if tier else ""

            # Price line: prefer last-sold, then catalog (native + USD).
            ls_raw = r.get("last_sold_raw")
            ls_cur = r.get("last_sold_currency")
            ls_usd = r.get("last_sold_usd_raw")
            if ls_raw is not None and ls_cur:
                price_str = _fmt_network_and_usd(ls_raw, ls_cur, ls_usd)
            else:
                price_str = str(r.get("_catalog_price_str") or "")

            # Owned-stack USD subtotal (only when we know per-unit USD and
            # there's more than one).
            unit_usd = r.get("unit_usd_raw")
            stack_usd = ""
            if unit_usd is not None and n > 1:
                try:
                    per_h = to_human(int(unit_usd))
                    if per_h > 0:
                        stack_usd = f"  ·  `${per_h * n:,.2f}` total"
                except Exception:
                    pass

            head = f"{emoji} **{name}** ×{n}{tier_badge}"
            sub_bits = [f"`{addr}`"]
            if price_str:
                sub_bits.append(price_str)
            sub = "  ·  ".join(sub_bits) + stack_usd
            lines.append(f"{head}\n-# {sub}")

        builder = builder.field(
            f"Owned stacks  ·  {len(rows)} total",
            "\n".join(lines),
            False,
        )
        prefix_note = (
            "Pick a stack (row 2) to drill in  ·  "
            "Pick a token (row 3) to inspect / list / transfer  ·  "
            "Sort cycles count -> USD -> name"
        )
        return builder.footer(
            f"{prefix_note}  ·  "
            f"Sort: {_SORT_LABEL.get(self.sort, self.sort)}  ·  "
            f"Page {page}/{total_pages}"
        ).build()

    def _build_stack_embed(
        self, stack: dict | None, tokens: list[dict],
    ) -> discord.Embed:
        """Tier-2 view: drill-in for one selected stack (contract).

        Lists each owned token within that stack with its full token id
        visible so the player can copy / pick from the row-3 dropdown.
        """
        if not stack:
            return card(
                "\U0001F4E6 Stack not found",
                description=(
                    "That stack address doesn't match anything you own "
                    "anymore -- click Refresh to reload."
                ),
                color=C_NEUTRAL,
            ).build()

        addr = str(stack.get("address") or "?")
        name = str(stack.get("name") or addr)
        kind = str(stack.get("kind") or "")
        emoji = str(stack.get("emoji") or "") or _kind_emoji(kind)
        n = int(stack.get("owned_n") or 0)
        tier = stack.get("rarity_tier")
        tier_badge = f"  ·  T{int(tier)}" if tier else ""

        # Price line: prefer last-sold, then catalog (native + USD).
        ls_raw = stack.get("last_sold_raw")
        ls_cur = stack.get("last_sold_currency")
        ls_usd = stack.get("last_sold_usd_raw")
        if ls_raw is not None and ls_cur:
            price_str = _fmt_network_and_usd(ls_raw, ls_cur, ls_usd)
        else:
            price_str = str(stack.get("_catalog_price_str") or "")
        unit_usd = stack.get("unit_usd_raw")
        stack_usd_part = ""
        if unit_usd is not None and n > 1:
            try:
                per_h = to_human(int(unit_usd))
                if per_h > 0:
                    stack_usd_part = (
                        f"  ·  Stack value: `${per_h * n:,.2f}`"
                    )
            except Exception:
                pass

        builder = card(
            f"{emoji} {name}{tier_badge}  ·  ×{n}",
            color=_kind_color(kind),
        )
        builder = builder.field("Address", f"`{addr}`", True)
        builder = builder.field("Kind", kind or "?", True)
        if price_str:
            builder = builder.field(
                "Price", price_str + stack_usd_part, False,
            )

        if tokens:
            lines: list[str] = []
            for t in tokens[:25]:
                tid = str(t.get("token_id") or "")
                md = t.get("metadata") or {}
                hint = _token_pick_label(
                    kind, str(t.get("contract_name") or name), md, tid,
                )
                # Strip the trailing `  ·  short_id` repeat -- we want
                # the FULL token id on this line.
                if "  ·  " in hint:
                    hint = hint.rsplit("  ·  ", 1)[0]
                lines.append(f"`{tid}`  ·  {hint}")
            value = "\n".join(lines)
            if len(value) > 1000:
                value = value[:980] + "\n-# (truncated)"
            extra = (
                f"  ·  +{n - len(tokens)} more not shown"
                if n > len(tokens) else ""
            )
            builder = builder.field(
                f"Tokens in this stack  ·  {len(tokens)}/{n}{extra}",
                value, False,
            )
        else:
            builder = builder.field(
                "Tokens", "_none alive in this stack_", False,
            )

        builder = builder.footer(
            "Pick a token (row 3) to inspect / list / transfer  ·  "
            "Junk stacks can be sold via Sell Junk"
        )
        return builder.build()

    async def _build_token_embed(self) -> discord.Embed | None:
        """Tier-3 view: render the inspect embed for the selected token.

        Returns None when the token can't be resolved (so the caller
        falls back to an earlier view rather than rendering an empty
        card).
        """
        if not self.selected_token:
            return None
        tok = await _items.get_token(self.ctx.db, self.selected_token)
        if not tok:
            return None
        contract = None
        if tok.get("contract_id"):
            contract = await _items.get_contract(
                self.ctx.db, contract_id=int(tok["contract_id"]),
            )
        return await _build_inspect_embed_for_token(
            self.ctx, tok, contract,
        )

    def _rebuild_stack_picker(self, stacks: list[dict]) -> None:
        for child in list(self.children):
            if isinstance(child, _StackSelect):
                self.remove_item(child)
        self.add_item(_StackSelect(stacks, self.stack_addr))

    def _rebuild_token_picker(self, tokens: list[dict]) -> None:
        """Drop any prior _TokenPickSelect and add a fresh one.

        Called from every refresh so the picker matches the current
        category + stack selection. Newly minted tokens show up
        immediately; sold / gifted tokens disappear without a stale
        entry that would 404 on click.
        """
        for child in list(self.children):
            if isinstance(child, _TokenPickSelect):
                self.remove_item(child)
        self.add_item(_TokenPickSelect(tokens, self.selected_token))

    def _stack_row(self, rows: list[dict]) -> dict | None:
        if not self.stack_addr:
            return None
        addr = self.stack_addr.lower()
        return next(
            (r for r in rows
             if str(r.get("address") or "").lower() == addr),
            None,
        )

    def _has_owned_junk(self, rows: list[dict]) -> bool:
        """Cheap check: does the player own any junk stack right now?

        Used to enable the Sell Junk button in the action row. Looks
        at the same fetched stack list the embed renders so we don't
        re-query.
        """
        for r in rows:
            if str(r.get("kind") or "").lower() == "junk":
                if int(r.get("owned_n") or 0) > 0:
                    return True
        return False

    def _update_action_buttons(
        self, rows: list[dict], total_pages: int,
    ) -> None:
        """Sync nav + action button enabled-state to the current view
        state. Called at the end of every refresh.
        """
        in_overview = (
            self.stack_addr is None and self.selected_token is None
        )
        token_picked = self.selected_token is not None
        sell_junk_eligible = (
            self.kind == "junk" and self._has_owned_junk(rows)
        ) or (
            self.stack_addr is not None
            and any(
                str(r.get("address") or "").lower() == self.stack_addr.lower()
                and str(r.get("kind") or "").lower() == "junk"
                for r in rows
            )
        )
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            label = child.label or ""
            if label == "Prev":
                child.disabled = (
                    not in_overview or self.page <= 1
                )
            elif label == "Next":
                child.disabled = (
                    not in_overview or self.page >= total_pages
                )
            elif label == "Sort":
                child.disabled = not in_overview
            elif label in ("List on AH", "Transfer", "Inspect"):
                child.disabled = not token_picked
            elif label == "Sell Junk":
                child.disabled = not sell_junk_eligible

    async def refresh(self, interaction: discord.Interaction) -> None:
        rows, total_pages = await self._fetch()
        self.page = max(1, min(self.page, total_pages))

        # If a stack is selected but no longer in the user's owned set
        # (e.g. the last unit was sold), fall back to overview.
        if self.stack_addr and not self._stack_row(rows):
            self.stack_addr = None
            self.selected_token = None

        # Tier 2 stack picker -- limited to stacks within the current kind.
        kind_stacks = (
            rows if not self.kind
            else [r for r in rows
                  if str(r.get("kind") or "").lower() == self.kind]
        )
        self._rebuild_stack_picker(kind_stacks)

        # Tier 3 token picker -- scoped to the chosen stack (or category).
        try:
            tokens = await self._fetch_owned_tokens(limit=25)
        except Exception:
            log.debug("items: token-pick fetch failed", exc_info=True)
            tokens = []

        # If selected_token is no longer ours, drop it.
        if self.selected_token and not any(
            str(t.get("token_id") or "") == self.selected_token
            for t in tokens
        ):
            self.selected_token = None

        self._rebuild_token_picker(tokens)

        # Pick the embed mode that matches the current state.
        embed: discord.Embed | None = None
        if self.selected_token:
            embed = await self._build_token_embed()
            if embed is None:
                # token vanished between fetches -- drop it and re-render.
                self.selected_token = None
        if embed is None and self.stack_addr:
            stack_row = self._stack_row(rows)
            embed = self._build_stack_embed(stack_row, tokens)
        if embed is None:
            embed = self._build_overview_embed(rows, total_pages)

        self._update_action_buttons(rows, total_pages)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Prev", emoji="\U00002B05",
        style=discord.ButtonStyle.secondary, row=3,
    )
    async def btn_prev(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        self.page = max(1, self.page - 1)
        await self.refresh(interaction)

    @discord.ui.button(
        label="Next", emoji="\U000027A1",
        style=discord.ButtonStyle.secondary, row=3,
    )
    async def btn_next(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        self.page += 1
        await self.refresh(interaction)

    @discord.ui.button(
        label="Sort", emoji="\U0001F500",
        style=discord.ButtonStyle.primary, row=3,
    )
    async def btn_sort(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        self.sort = _SORT_NEXT.get(self.sort, "count")
        self.page = 1
        await self.refresh(interaction)

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=3,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self.refresh(interaction)

    @discord.ui.button(
        label="List on AH", emoji="\U0001F3DB",
        style=discord.ButtonStyle.success, row=4,
    )
    async def btn_action_list(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if not self.selected_token:
            await interaction.response.send_message(
                "Pick a token from the row 3 dropdown first.",
                ephemeral=True,
            )
            return
        tok = await _items.get_token(self.ctx.db, self.selected_token)
        if not tok or tok.get("burned_at") is not None:
            await interaction.response.send_message(
                "That token is gone or burned -- click Refresh.",
                ephemeral=True,
            )
            return
        if tok.get("listing_id"):
            await interaction.response.send_message(
                f"Already listed in #{int(tok['listing_id'])}. Cancel "
                "the existing listing first with `,ah cancel`.",
                ephemeral=True,
            )
            return
        if int(tok.get("owner_user_id") or 0) != int(self.ctx.author.id):
            await interaction.response.send_message(
                "You don't own that token.", ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            _ListPriceModal(self.ctx, self.selected_token),
        )

    @discord.ui.button(
        label="Transfer", emoji="\U0001F381",
        style=discord.ButtonStyle.primary, row=4,
    )
    async def btn_action_transfer(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if not self.selected_token:
            await interaction.response.send_message(
                "Pick a token from the row 3 dropdown first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            _TransferModal(self.ctx, self.selected_token),
        )

    @discord.ui.button(
        label="Inspect", emoji="\U0001F50D",
        style=discord.ButtonStyle.secondary, row=4,
    )
    async def btn_action_inspect(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if not self.selected_token:
            await interaction.response.send_message(
                "Pick a token from the row 3 dropdown first.",
                ephemeral=True,
            )
            return
        tok = await _items.get_token(self.ctx.db, self.selected_token)
        if not tok:
            await interaction.response.send_message(
                f"Token `{self.selected_token}` no longer exists.",
                ephemeral=True,
            )
            return
        contract = None
        if tok.get("contract_id"):
            contract = await _items.get_contract(
                self.ctx.db, contract_id=int(tok["contract_id"]),
            )
        embed = await _build_inspect_embed_for_token(
            self.ctx, tok, contract,
        )
        is_owner = (
            int(tok.get("owner_user_id") or 0) == int(self.ctx.author.id)
        )
        is_listable = (
            tok.get("burned_at") is None
            and not tok.get("listing_id")
        )
        addr = str((contract or {}).get("address") or "")
        action_view = TokenActionView(
            self.ctx,
            token_id=str(tok["token_id"]),
            contract_address=addr if addr and addr != "?" else None,
            is_owner=is_owner,
            is_listable=is_listable,
        )
        await interaction.response.send_message(
            embed=embed, view=action_view, ephemeral=True,
        )
        try:
            action_view.message = await interaction.original_response()
        except Exception:
            pass

    @discord.ui.button(
        label="Sell Junk", emoji="\U0001F45F",
        style=discord.ButtonStyle.danger, row=4,
    )
    async def btn_action_sell_junk(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=False, thinking=False)
        # Sell across both fishing and dungeon junk inventories. Each
        # service has its own payout currency (LURE / RUNE), so we
        # report them on separate lines.
        from services import fishing as _fish_svc
        from services import dungeon as _dungeon_svc
        sold_lines: list[str] = []
        any_sold = False
        try:
            count, lure = await _fish_svc.sell_inventory(
                self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
                junk_only=True,
            )
            if count > 0 and lure > 0:
                sold_lines.append(
                    f"\U0001F41F Fishing junk  ·  ×{int(count)}  ·  "
                    f"`{lure:,.2f} LURE`"
                )
                any_sold = True
        except Exception:
            log.debug("items: sell fishing junk failed", exc_info=True)

        try:
            rune, sold = await _dungeon_svc.sell_junk(
                self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
            )
            if rune > 0 and sold:
                total_count = sum(int(v) for v in sold.values())
                sold_lines.append(
                    f"\U000026CF Dungeon junk  ·  ×{total_count}  ·  "
                    f"`{rune:,.2f} RUNE`"
                )
                any_sold = True
        except ValueError:
            # "Your junk inventory is empty." -- benign in mixed paths.
            pass
        except Exception:
            log.debug("items: sell dungeon junk failed", exc_info=True)

        if not any_sold:
            await interaction.followup.send(
                embed=card(
                    "\U0001F45F Nothing to sell",
                    description=(
                        "No junk to liquidate right now. Catch fish "
                        "for fishing junk, or run `,delve` for "
                        "dungeon junk."
                    ),
                    color=C_NEUTRAL,
                ).build(),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=card(
                "\U0001F45F Junk sold",
                description="\n".join(sold_lines),
                color=C_SUCCESS,
            ).footer(
                "Click Refresh to reload your stacks."
            ).build(),
            ephemeral=False,
        )


class _ListPriceModal(discord.ui.Modal, title="List on Auction House"):
    """Modal: ask for a price (and optional currency override) to
    list this token on the AH. Submitted via the "List on AH" button on
    either the inspect view or the unified ``,items`` browser; runs
    ``services.auction.create_listing_by_token`` directly.
    """

    price = discord.ui.TextInput(
        label="Price",
        placeholder="e.g. 5",
        required=True,
        max_length=20,
    )
    currency = discord.ui.TextInput(
        label="Currency (optional)",
        placeholder="leave blank for the network's default",
        required=False,
        max_length=10,
    )

    def __init__(self, ctx: DiscoContext, token_id: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.token_id = str(token_id)

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
            listing_id, tok, msg = await _auc.create_listing_by_token(
                self.ctx.db,
                guild_id=self.ctx.guild_id,
                seller_user_id=interaction.user.id,
                token_id=self.token_id,
                price=price_v,
                currency=cur,
            )
        except ValueError as e:
            await interaction.response.send_message(
                str(e), ephemeral=True,
            )
            return
        except Exception as e:
            log.exception(
                "items List click failed token=%s",
                self.token_id,
            )
            await interaction.response.send_message(
                f"Could not list: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=card(
                f"\U0001F3DB Listed  ·  #{int(listing_id)}",
                description=msg,
                color=C_SUCCESS,
            ).build(),
            ephemeral=False,
        )


class _TransferModal(discord.ui.Modal, title="Transfer NFT"):
    """Modal: ask for a recipient (mention or numeric user id) and
    transfer this token to them. Charges gas on the sender via
    ``services.items.charge_gas`` before the underlying transfer
    so an insufficient-balance failure aborts cleanly with no
    state change.
    """

    recipient = discord.ui.TextInput(
        label="Recipient",
        placeholder="@user mention OR numeric user id (e.g. 1234567890)",
        required=True,
        max_length=80,
    )

    def __init__(self, ctx: DiscoContext, token_id: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.token_id = str(token_id)

    @staticmethod
    def _parse_user_id(raw: str) -> int | None:
        s = (raw or "").strip()
        if not s:
            return None
        # Strip <@...> mention wrappers.
        if s.startswith("<@") and s.endswith(">"):
            s = s[2:-1].lstrip("!&")
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        target_id = self._parse_user_id(str(self.recipient.value))
        if not target_id:
            await interaction.response.send_message(
                "Bad recipient. Pass a `@mention` or a numeric user id.",
                ephemeral=True,
            )
            return
        if int(target_id) == int(interaction.user.id):
            await interaction.response.send_message(
                "Can't gift a token to yourself.", ephemeral=True,
            )
            return
        # Look up the recipient member to confirm they're in the guild
        # and not a bot. We use ctx.guild because the modal interaction
        # may not have a member object hanging off it directly.
        guild = self.ctx.guild
        member = guild.get_member(int(target_id)) if guild else None
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(int(target_id))
            except Exception:
                member = None
        if member is None:
            await interaction.response.send_message(
                f"Recipient `{target_id}` isn't in this server.",
                ephemeral=True,
            )
            return
        if member.bot:
            await interaction.response.send_message(
                "Can't gift a token to a bot.", ephemeral=True,
            )
            return

        tok = await _items.get_token(self.ctx.db, self.token_id)
        if not tok or tok.get("burned_at") is not None:
            await interaction.response.send_message(
                "Token is gone or burned. Refresh and try again.",
                ephemeral=True,
            )
            return
        if tok.get("listing_id"):
            await interaction.response.send_message(
                f"Token is escrowed in listing #{int(tok['listing_id'])}. "
                f"Cancel the listing first.",
                ephemeral=True,
            )
            return
        if int(tok.get("owner_user_id") or 0) != int(interaction.user.id):
            await interaction.response.send_message(
                "You don't own that token.", ephemeral=True,
            )
            return

        # Charge gas (sender pays) before the transfer.
        gas_info: tuple[int, str] | None = None
        try:
            gas_info = await _items.charge_gas(
                self.ctx.db,
                guild_id=self.ctx.guild_id,
                payer_user_id=int(interaction.user.id),
                network_short=str(tok.get("network") or ""),
                event_type="transfer",
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        try:
            await _items.transfer(
                self.ctx.db, str(tok["token_id"]), int(target_id),
            )
        except Exception as e:
            log.exception(
                "items inspect Transfer click failed token=%s",
                self.token_id,
            )
            await interaction.response.send_message(
                f"Transfer failed: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return

        # Stamp the gas onto the most recent transfer event for this
        # token so the inspect history shows it.
        if gas_info is not None:
            try:
                gas_raw, gas_cur = gas_info
                await self.ctx.db.execute(
                    "UPDATE item_token_events "
                    "   SET gas_raw = $2::numeric, gas_currency = $3 "
                    " WHERE event_id = ("
                    "   SELECT event_id FROM item_token_events "
                    "    WHERE token_id = $1 AND event_type = 'transfer' "
                    "    ORDER BY event_id DESC LIMIT 1"
                    " )",
                    str(tok["token_id"]),
                    str(int(gas_raw)), str(gas_cur),
                )
            except Exception:
                log.debug("gas-stamp on transfer event failed", exc_info=True)

        gas_part = ""
        if gas_info is not None:
            try:
                gas_raw, gas_cur = gas_info
                gas_part = (
                    f" Gas: **{to_human(int(gas_raw)):,.4f} {gas_cur}**."
                )
            except Exception:
                gas_part = ""
        await interaction.response.send_message(
            embed=card(
                "\U0001F381 NFT Gifted",
                description=(
                    f"`{_short(self.token_id)}` transferred to "
                    f"<@{member.id}>.{gas_part}"
                ),
                color=C_SUCCESS,
            ).build(),
            ephemeral=False,
        )


class TokenActionView(discord.ui.View):
    """Action buttons for ``,items inspect <token_id>``.

    Owner-locked, 5-min timeout. Buttons:
      * List on AH -- pops a price modal; submits via the token-id flow.
      * Refresh -- re-renders the inspect embed.
      * View Contract -- runs ``,items contract <addr>`` for the
        token's contract address.

    The List button is hidden when the caller doesn't own the token
    (or it's burned / escrowed) -- no point asking for a price the
    seller can't deliver on.
    """

    def __init__(
        self,
        ctx: DiscoContext,
        token_id: str,
        contract_address: str | None,
        is_owner: bool,
        is_listable: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.token_id = str(token_id)
        self.contract_address = (
            str(contract_address) if contract_address else None
        )
        self.message: discord.Message | None = None
        if not (is_owner and is_listable):
            for child in list(self.children):
                if (
                    isinstance(child, discord.ui.Button)
                    and child.label in ("List on AH", "Transfer")
                ):
                    self.remove_item(child)
        if not self.contract_address:
            for child in list(self.children):
                if (
                    isinstance(child, discord.ui.Button)
                    and child.label == "View Contract"
                ):
                    self.remove_item(child)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your inspect view. Run "
                "`,items inspect <token_id>` to open your own.",
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

    @discord.ui.button(
        label="List on AH", emoji="\U0001F3DB",
        style=discord.ButtonStyle.success, row=0,
    )
    async def btn_list(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            _ListPriceModal(self.ctx, self.token_id),
        )

    @discord.ui.button(
        label="Transfer", emoji="\U0001F381",
        style=discord.ButtonStyle.primary, row=0,
    )
    async def btn_transfer(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            _TransferModal(self.ctx, self.token_id),
        )

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=0,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        # Just acknowledge -- the inspect embed pulls fresh on the
        # next ,items inspect call. Cheap to do nothing here so we
        # don't try to recompute the entire embed in-line.
        await interaction.response.send_message(
            f"Run `,items inspect {self.token_id}` again to refresh.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="View Contract", emoji="\U0001F4DC",
        style=discord.ButtonStyle.secondary, row=0,
    )
    async def btn_contract(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if not self.contract_address:
            await interaction.response.send_message(
                "This token has no contract row.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Run `,items contract {self.contract_address}` to view "
            f"the contract.",
            ephemeral=True,
        )


class NFT(commands.Cog, name="NFT"):
    """Per-unit NFT browse + transfer commands."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.hybrid_group(
        name="items",
        aliases=["bag", "tokens"],
        invoke_without_command=True, with_app_command=False,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def nft(self, ctx: DiscoContext) -> None:
        """Interactive browser for the per-unit item NFTs you own.

        Top-level command is ``,items`` (with ``,bag`` / ``,tokens``
        aliases) -- the ``,nft`` namespace is owned by the legacy NFT
        collection cog (``cogs/nfts.py``), so this layer uses
        ``items`` to avoid the registration collision.

        Opens an owner-locked 3-tier browser:
          * Tier 1 -- Category dropdown (kind: buddy/egg/fish/...).
          * Tier 2 -- Item-stack dropdown (a contract within the
            category) so the player can drill into one stack.
          * Tier 3 -- Item dropdown listing each token in the chosen
            stack with its full token id visible.

        Buttons: Prev/Next/Sort/Refresh for stack-list pagination plus
        List on AH / Transfer / Inspect / Sell Junk for the currently
        selected token (Sell Junk lights up in the junk category).
        """
        view = ItemsView(ctx)
        rows, total_pages = await view._fetch()
        if not rows:
            await ctx.reply_error(
                "You don't own any NFTs yet. Catch a fish, harvest a crop, "
                "or buy something from the shop / auction house to mint "
                "your first one."
            )
            return
        # Initial pickers so the tier-2 and tier-3 dropdowns are
        # populated on the first render -- otherwise they'd only
        # appear after the first interaction (Prev/Next/Sort/kind).
        kind_stacks = (
            rows if not view.kind
            else [r for r in rows
                  if str(r.get("kind") or "").lower() == view.kind]
        )
        view._rebuild_stack_picker(kind_stacks)
        try:
            tokens = await view._fetch_owned_tokens(limit=25)
        except Exception:
            log.debug("items: initial token-pick fetch failed", exc_info=True)
            tokens = []
        view._rebuild_token_picker(tokens)
        embed = view._build_overview_embed(rows, total_pages)
        view._update_action_buttons(rows, total_pages)
        msg = await ctx.reply(
            embed=embed, view=view, mention_author=False,
        )
        view.message = msg


    # -- ,nft list ---------------------------------------------------------

    @nft.command(name="list", aliases=["owned"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_list(
        self, ctx: DiscoContext, kind: str = "",
    ) -> None:
        """List your NFTs, optionally filtered by kind.

        ``kind`` accepts ``buddy`` / ``egg`` / ``fish`` / ``crop`` /
        ``bait`` / ``junk`` / ``weapon`` / ``armor`` / ``consumable`` /
        ``crafted`` / ``stone`` / ``shop`` / ``ore`` / ``token``.
        """
        kind = (kind or "").strip().lower()
        rows = await _items.list_owned(
            ctx.db,
            guild_id=ctx.guild_id, user_id=ctx.author.id,
            kind=kind or None, limit=200,
        )
        if not rows:
            scope = f"`{kind}` " if kind else ""
            await ctx.reply_error(
                f"You don't own any {scope}NFTs."
            )
            return

        # Group by contract for compact rendering. Row count per
        # contract collapses 50 worm bait into one line.
        by_contract: dict[int, list[dict]] = {}
        for r in rows:
            cid = int(r.get("contract_id") or 0)
            by_contract.setdefault(cid, []).append(r)

        # Pull contract rows + per-contract last-sold USD in a single
        # CTE so each row already knows its current "best price."
        cids = list(by_contract.keys()) or [0]
        contract_rows = await ctx.db.fetch_all(
            """
            WITH last_sold AS (
                SELECT DISTINCT ON (contract_id)
                       contract_id, price_usd_raw, price_raw, currency
                  FROM item_token_events
                 WHERE event_type = 'sold'
                   AND contract_id = ANY($1::bigint[])
                 ORDER BY contract_id, created_at DESC
            )
            SELECT ic.*,
                   ls.price_usd_raw   AS last_sold_usd_raw,
                   ls.price_raw       AS last_sold_raw,
                   ls.currency        AS last_sold_currency
              FROM item_contracts ic
              LEFT JOIN last_sold ls ON ls.contract_id = ic.contract_id
             WHERE ic.contract_id = ANY($1::bigint[])
            """,
            cids,
        )
        contracts_map = {int(c["contract_id"]): c for c in contract_rows or []}

        prefix = await ctx.get_guild_prefix()
        lines = []
        kind_total_usd_raw = 0
        for cid, toks in sorted(
            by_contract.items(),
            key=lambda kv: -len(kv[1]),
        ):
            c = contracts_map.get(cid) or {}
            addr = str(c.get("address") or "?")
            name = str(c.get("name") or addr)
            emoji = str(c.get("emoji") or _kind_emoji(c.get("kind", "")))
            n = len(toks)
            tier = c.get("rarity_tier")
            tier_badge = f"  ·  T{int(tier)}" if tier else ""
            # Per-contract price: last sale in network coin + USD if a sale
            # has settled; otherwise the catalog (native + USD via oracle).
            per_unit_raw = c.get("last_sold_raw")
            per_unit_cur = c.get("last_sold_currency")
            per_unit_usd = c.get("last_sold_usd_raw")
            price_str = ""
            usd_per: int | None = None
            if per_unit_raw is not None and per_unit_cur:
                price_str = _fmt_network_and_usd(
                    per_unit_raw, per_unit_cur, per_unit_usd,
                )
                if per_unit_usd is not None:
                    try:
                        usd_per = int(per_unit_usd)
                    except (TypeError, ValueError):
                        usd_per = None
            else:
                catalog_str, catalog_usd = await _pricing.render_catalog_price(
                    ctx.db, ctx.guild_id, dict(c),
                )
                price_str = catalog_str
                usd_per = catalog_usd
            if usd_per is not None:
                kind_total_usd_raw += int(usd_per) * int(n)
            head = f"{emoji} **{name}** ×{n}{tier_badge}"
            sub_bits = [f"`{addr}`"]
            if price_str:
                sub_bits.append(price_str)
            sub = "  ·  ".join(sub_bits)
            lines.append(f"{head}\n-# {sub}")

        # Discord field cap: chunk past ~15 contracts. Each entry is
        # 2 lines (head + sub) so the older 25-cap could exceed 1024.
        chunks = []
        cur: list[str] = []
        cur_len = 0
        for ln in lines:
            if cur_len + len(ln) + 1 > 1000 or len(cur) >= 15:
                chunks.append(cur)
                cur, cur_len = [], 0
            cur.append(ln)
            cur_len += len(ln) + 1
        if cur:
            chunks.append(cur)

        title_kind = f" · {kind.title()}" if kind else ""
        usd_part = (
            f"  ·  ${to_human(kind_total_usd_raw):,.2f}"
            if kind_total_usd_raw > 0 else ""
        )
        builder = card(
            f"\U0001F4E6 Your NFTs{title_kind}  ·  {len(rows)} total{usd_part}",
            color=_kind_color(kind),
        )
        for i, c in enumerate(chunks):
            builder = builder.field(
                "Contracts" if i == 0 else f"Contracts ({i + 1})",
                "\n".join(c),
                False,
            )
        builder = builder.footer(
            f"`{prefix}items inspect <token_id>` for full details"
        )
        await ctx.reply(embed=builder.build(), mention_author=False)

    # -- ,nft inspect ------------------------------------------------------

    @nft.command(name="inspect", aliases=["show", "info"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_inspect(
        self, ctx: DiscoContext, token_id: str = "",
    ) -> None:
        """Details for one token by its full or short id."""
        if not token_id:
            await ctx.reply_error(
                "Pass a token id, e.g. `,nft inspect bud:k889kak`."
            )
            return
        tok = await _items.get_token(ctx.db, token_id.strip().lower())
        if not tok:
            await ctx.reply_error(
                f"No token found for `{token_id}`. Try the full "
                f"`<network>:<hex>` shape from `,nft list`."
            )
            return
        contract = None
        if tok.get("contract_id"):
            contract = await _items.get_contract(
                ctx.db, contract_id=int(tok["contract_id"]),
            )
        kind = str(tok.get("kind") or "")
        emoji = (
            str((contract or {}).get("emoji") or "")
            or _kind_emoji(kind)
        )
        name = str((contract or {}).get("name") or kind.title())
        addr = str((contract or {}).get("address") or "?")
        owner = tok.get("owner_user_id")
        burned_at = tok.get("burned_at")
        listing_id = tok.get("listing_id")

        builder = card(
            f"{emoji} {name}",
            color=_kind_color(kind),
        )
        builder = builder.field("Token id", f"`{tok['token_id']}`", False)
        builder = builder.field("Contract", f"`{addr}` ({kind})", True)
        builder = builder.field(
            "Network", _network_full(str(tok.get("network") or "")), True,
        )
        if burned_at:
            builder = builder.field(
                "Status",
                f"\U0001F525 Burned ({fmt_ts(burned_at)})",
                True,
            )
        elif listing_id:
            builder = builder.field(
                "Status",
                f"\U0001F3DB Escrowed in listing #{int(listing_id)}",
                True,
            )
        elif owner is None:
            builder = builder.field("Status", "\U0001F3E0 Unowned", True)
        else:
            builder = builder.field("Owner", f"<@{int(owner)}>", True)

        minted_at = tok.get("minted_at") or tok.get("created_at")
        if minted_at:
            builder = builder.field("Minted", fmt_ts(minted_at), True)
        if tok.get("mint_source"):
            builder = builder.field(
                "Source", f"`{tok['mint_source']}`", True,
            )

        md = tok.get("metadata") or {}
        if isinstance(md, str):
            try:
                import json as _json
                md = _json.loads(md)
            except Exception:
                md = {}
        if md:
            interesting = {
                k: v for k, v in md.items()
                if k not in ("contract", "unit_index")
                and v not in (None, "", [], {})
            }
            if interesting:
                # Compact key:value pairs, capped at ~900 chars.
                lines = []
                for k, v in interesting.items():
                    s = f"`{k}` -> {v}"
                    if sum(len(x) for x in lines) + len(s) > 900:
                        break
                    lines.append(s)
                builder = builder.field("Metadata", "\n".join(lines), False)

        # Price summary: last sale (network coin + USD) or catalog
        # (native + USD via oracle). The contract row already has the
        # native columns we need; ``render_catalog_price`` does the
        # native -> USD conversion when no sale has settled.
        cid = tok.get("contract_id")
        if cid:
            try:
                summary = await _pricing.contract_price_summary(
                    ctx.db, int(cid),
                )
            except Exception:
                summary = {}
        else:
            summary = {}
        last_raw = summary.get("last_sold_raw")
        last_cur = summary.get("last_sold_currency")
        last_usd = summary.get("last_sold_usd_raw")
        n_sales = int(summary.get("n_sales") or 0)
        if last_raw is not None and last_cur:
            price_value = _fmt_network_and_usd(last_raw, last_cur, last_usd)
            label = (
                f"Last sale  ·  {n_sales} total"
                if n_sales > 0 else "Last sale"
            )
            builder = builder.field(label, price_value, False)
        elif contract:
            catalog_str, _ = await _pricing.render_catalog_price(
                ctx.db, ctx.guild_id, dict(contract),
            )
            if catalog_str:
                builder = builder.field("Catalog price", catalog_str, False)

        # Token-level event history (oldest first, max 10 most recent).
        try:
            events = await _items.get_token_events(
                ctx.db, str(tok["token_id"]), limit=200,
            )
        except Exception:
            events = []
        if events:
            tail = events[-10:]
            hist_lines: list[str] = []
            for ev in tail:
                glyph = _EVENT_GLYPH.get(
                    str(ev.get("event_type") or ""), "\U000026AA",
                )
                ts = fmt_ts(ev.get("created_at"))
                etype = str(ev.get("event_type") or "?").title()
                trail = ""
                if ev.get("event_type") == "transfer":
                    f = ev.get("from_user_id")
                    t = ev.get("to_user_id")
                    f_s = f"<@{int(f)}>" if f else "?"
                    t_s = f"<@{int(t)}>" if t else "?"
                    trail = f"  ·  {f_s} -> {t_s}"
                elif ev.get("event_type") == "sold":
                    f = ev.get("from_user_id")
                    t = ev.get("to_user_id")
                    f_s = f"<@{int(f)}>" if f else "?"
                    t_s = f"<@{int(t)}>" if t else "?"
                    price_part = _fmt_network_and_usd(
                        ev.get("price_raw"), ev.get("currency"),
                        ev.get("price_usd_raw"),
                    )
                    trail = f"  ·  {f_s} -> {t_s}  ·  {price_part}"
                elif ev.get("event_type") == "list":
                    price_part = _fmt_network_and_usd(
                        ev.get("price_raw"), ev.get("currency"), None,
                    )
                    lid = ev.get("listing_id")
                    trail = (
                        f"  ·  listing #{int(lid)}  ·  {price_part}"
                        if lid else f"  ·  {price_part}"
                    )
                elif ev.get("event_type") == "mint":
                    src = (ev.get("metadata") or {}).get("mint_source")
                    if isinstance(src, str) and src:
                        trail = f"  ·  source `{src}`"
                # Append gas paid (network coin) when the event paid one.
                gas_raw = ev.get("gas_raw")
                gas_cur = ev.get("gas_currency")
                gas_part = ""
                if gas_raw is not None and gas_cur:
                    try:
                        gas_h = to_human(int(gas_raw))
                        gas_part = f"  ·  gas `{gas_h:,.4f} {gas_cur}`"
                    except Exception:
                        gas_part = ""
                hist_lines.append(
                    f"{glyph} `{ts}`  ·  {etype}{trail}{gas_part}"
                )
            value = "\n".join(hist_lines)
            if len(events) > 10:
                value = (
                    f"-# +{len(events) - 10} earlier event(s) not shown.\n"
                    + value
                )
            # Field-cap guard: chunk if needed.
            if len(value) > 1000:
                value = value[-980:]
                value = "-# (truncated)\n" + value
            builder = builder.field("History", value, False)

        prefix = await ctx.get_guild_prefix()
        builder = builder.footer(
            f"`{prefix}items transfer {tok['token_id']} @user` to gift  ·  "
            f"`{prefix}items contract {addr}` for the contract"
        )
        is_owner = (
            int(tok.get("owner_user_id") or 0) == int(ctx.author.id)
        )
        is_listable = (
            tok.get("burned_at") is None
            and not tok.get("listing_id")
        )
        view = TokenActionView(
            ctx,
            token_id=str(tok["token_id"]),
            contract_address=addr if addr and addr != "?" else None,
            is_owner=is_owner,
            is_listable=is_listable,
        )
        msg = await ctx.reply(
            embed=builder.build(), view=view, mention_author=False,
        )
        view.message = msg

    # -- ,nft contracts ----------------------------------------------------

    @nft.command(name="contracts", aliases=["catalog", "deployed"])
    @guild_only
    @no_bots
    async def nft_contracts(
        self, ctx: DiscoContext, kind: str = "",
    ) -> None:
        """List deployed contracts, optionally filtered by kind."""
        kind_in = (kind or "").strip().lower()
        rows = await _items.list_contracts(
            ctx.db, kind=kind_in or None,
        )
        if not rows:
            await ctx.reply_error(
                f"No contracts deployed for `{kind_in}`."
                if kind_in else "No contracts deployed yet."
            )
            return

        prefix = await ctx.get_guild_prefix()
        lines = []
        for r in rows[:200]:
            addr = str(r.get("address") or "?")
            name = str(r.get("name") or addr)
            k = str(r.get("kind") or "")
            emoji = str(r.get("emoji") or _kind_emoji(k))
            tier = r.get("rarity_tier")
            tier_part = f"  ·  T{int(tier)}" if tier else ""
            lines.append(
                f"{emoji} `{addr}`  ·  {name}{tier_part}"
            )

        # Field cap chunking like ,nft list.
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

        title_kind = f" · {kind_in.title()}" if kind_in else ""
        builder = card(
            f"\U0001F4DC Deployed Contracts{title_kind}  ·  {len(rows)} total",
            color=_kind_color(kind_in),
        )
        for i, c in enumerate(chunks):
            builder = builder.field(
                "Contracts" if i == 0 else f"Contracts ({i + 1})",
                "\n".join(c),
                False,
            )
        builder = builder.footer(
            f"`{prefix}items contract <address>` for one contract  ·  "
            f"`{prefix}items list <kind>` for what you own"
        )
        await ctx.reply(embed=builder.build(), mention_author=False)

    # -- ,nft contract <address> -------------------------------------------

    @nft.command(name="contract", aliases=["deploy"])
    @guild_only
    @no_bots
    async def nft_contract(
        self, ctx: DiscoContext, address: str = "",
    ) -> None:
        """Detail view for one deployed contract."""
        addr = (address or "").strip().lower()
        if not addr:
            await ctx.reply_error(
                "Pass a contract address, e.g. `,nft contract bait.worm`."
            )
            return
        c = await _items.get_contract(ctx.db, address=addr)
        if not c:
            await ctx.reply_error(
                f"No contract `{addr}` deployed. "
                f"Try `,nft contracts` to browse the catalog."
            )
            return

        kind = str(c.get("kind") or "")
        emoji = str(c.get("emoji") or _kind_emoji(kind))
        name = str(c.get("name") or addr)

        # Population stats: total minted + alive + my count.
        stats = await ctx.db.fetch_one(
            """
            SELECT
                COUNT(*)                        AS total_minted,
                COUNT(*) FILTER (
                    WHERE burned_at IS NULL
                )                               AS alive,
                COUNT(*) FILTER (
                    WHERE owner_user_id = $2
                      AND burned_at IS NULL
                )                               AS mine
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
        builder = builder.field(
            "Network", _network_full(str(c.get("network") or "")), True,
        )
        if c.get("rarity_tier"):
            builder = builder.field(
                "Rarity tier", f"T{int(c['rarity_tier'])}", True,
            )
        catalog_str, _ = await _pricing.render_catalog_price(
            ctx.db, ctx.guild_id, dict(c),
        )
        if catalog_str:
            builder = builder.field("Catalog price", catalog_str, True)
        builder = builder.field(
            "Supply",
            (
                f"`{alive}` alive  ·  `{burned}` burned  ·  "
                f"`{total}` total minted"
            ),
            False,
        )
        builder = builder.field("You own", f"`{mine}`", True)
        if c.get("deployed_at"):
            builder = builder.field(
                "Deployed", fmt_ts(c["deployed_at"]), True,
            )

        prefix = await ctx.get_guild_prefix()
        builder = builder.footer(
            f"`{prefix}items list {kind}` to see what you own"
        )
        await ctx.reply(embed=builder.build(), mention_author=False)

    # -- ,nft transfer -----------------------------------------------------

    @nft.command(name="transfer", aliases=["gift", "send"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_transfer(
        self,
        ctx: DiscoContext,
        token_id: str = "",
        member: discord.Member | None = None,
    ) -> None:
        """Gift one of your NFTs to another player.

        The token ownership flips immediately. The underlying
        inventory (cc_buddies row, JSONB count, etc.) is NOT moved by
        this command -- this is a pure NFT transfer that lets you
        hand someone a collectible token. The auction house and the
        cog-specific gift commands (`,fish egg gift`, etc.) handle
        their own inventory + token transfer in tandem.
        """
        if not token_id:
            await ctx.reply_error(
                "Pass a token id and a recipient: "
                "`,nft transfer bud:k889kak @user`."
            )
            return
        if member is None:
            await ctx.reply_error(
                "Mention the recipient: "
                f"`,nft transfer {token_id} @user`."
            )
            return
        if int(member.id) == int(ctx.author.id):
            await ctx.reply_error("Can't gift a token to yourself.")
            return
        if member.bot:
            await ctx.reply_error("Can't gift a token to a bot.")
            return

        tok = await _items.get_token(ctx.db, token_id.strip().lower())
        if not tok:
            await ctx.reply_error(f"No token `{token_id}`.")
            return
        if tok.get("burned_at") is not None:
            await ctx.reply_error("That token is burned.")
            return
        if int(tok.get("guild_id") or 0) != int(ctx.guild_id):
            await ctx.reply_error("That token isn't from this server.")
            return
        if tok.get("listing_id"):
            await ctx.reply_error(
                f"That token is escrowed in listing "
                f"#{int(tok['listing_id'])}. Cancel the listing first."
            )
            return
        if int(tok.get("owner_user_id") or 0) != int(ctx.author.id):
            await ctx.reply_error("You don't own that token.")
            return

        # Gas: sender pays in the network's native coin. Aborts the
        # transfer cleanly with a wallet-error message if the sender
        # can't cover it -- nothing has changed yet.
        gas_info: tuple[int, str] | None = None
        try:
            gas_info = await _items.charge_gas(
                ctx.db,
                guild_id=ctx.guild_id,
                payer_user_id=ctx.author.id,
                network_short=str(tok.get("network") or ""),
                event_type="transfer",
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        await _items.transfer(ctx.db, str(tok["token_id"]), int(member.id))
        # Stamp gas on the transfer event the helper just emitted so
        # the inspect history shows it. The transfer() call above
        # writes a 'transfer' event with no gas; we patch the most
        # recent one in.
        if gas_info is not None:
            try:
                gas_raw, gas_cur = gas_info
                await ctx.db.execute(
                    "UPDATE item_token_events "
                    "   SET gas_raw = $2::numeric, gas_currency = $3 "
                    " WHERE event_id = ("
                    "   SELECT event_id FROM item_token_events "
                    "    WHERE token_id = $1 AND event_type = 'transfer' "
                    "    ORDER BY event_id DESC LIMIT 1"
                    " )",
                    str(tok["token_id"]), str(int(gas_raw)), str(gas_cur),
                )
            except Exception:
                log.debug(
                    "gas-stamp on transfer event failed token=%s",
                    tok.get("token_id"), exc_info=True,
                )
        try:
            await ctx.bot.bus.publish(
                "nft_transferred",
                guild=ctx.guild,
                from_user_id=int(ctx.author.id),
                to_user_id=int(member.id),
                token_id=str(tok["token_id"]),
            )
        except Exception:
            log.debug("nft_transferred publish failed", exc_info=True)

        contract = None
        if tok.get("contract_id"):
            contract = await _items.get_contract(
                ctx.db, contract_id=int(tok["contract_id"]),
            )
        name = str((contract or {}).get("name") or tok.get("kind") or "Item")
        kind = str(tok.get("kind") or "")
        emoji = (
            str((contract or {}).get("emoji") or "")
            or _kind_emoji(kind)
        )
        gas_part = ""
        if gas_info is not None:
            try:
                gas_raw, gas_cur = gas_info
                gas_part = (
                    f" Gas: **{to_human(int(gas_raw)):,.4f} {gas_cur}**."
                )
            except Exception:
                gas_part = ""
        await ctx.reply_success(
            f"{emoji} **{name}** `{_short(tok['token_id'])}` "
            f"transferred to <@{member.id}>.{gas_part}",
            title="NFT Gifted",
        )

    # -- ,nft help ---------------------------------------------------------

    @nft.command(name="help", aliases=["commands"])
    @guild_only
    @no_bots
    async def nft_help(self, ctx: DiscoContext) -> None:
        """Quick reference for the NFT commands."""
        prefix = await ctx.get_guild_prefix()
        body = (
            f"**Browse**\n"
            f"`{prefix}items`  -  3-tier interactive browser "
            f"(category -> stack -> item)\n"
            f"`{prefix}items list [kind]`  -  list owned tokens, grouped by contract\n"
            f"`{prefix}items inspect <token_id>`  -  full details for one token\n"
            f"\n"
            f"**Browser actions** (buttons next to the dropdowns)\n"
            f"· **List on AH**  -  modal for price + currency, "
            f"acts on the token picked in row 3\n"
            f"· **Transfer**  -  gift the picked token to another player\n"
            f"· **Inspect**  -  full inspect card for the picked token\n"
            f"· **Sell Junk**  -  sells your fishing + dungeon junk "
            f"inventories at once (LURE + RUNE)\n"
            f"\n"
            f"**Catalog**\n"
            f"`{prefix}items contracts [kind]`  -  every deployed contract\n"
            f"`{prefix}items contract <address>`  -  detail view + supply stats\n"
            f"\n"
            f"**Transfer**\n"
            f"`{prefix}items transfer <token_id> @user`  -  gift a token "
            f"(does NOT move the underlying inventory; use cog-specific "
            f"gift commands or the auction house for that)\n"
            f"\n"
            f"**Token id shape**: `<network>:<hex>` -- e.g. `bud:k889kak`, "
            f"`reel:81819kak`, `hrv:af09c12`. Network prefix per kind: "
            f"buddy/egg=`bud`, fish/bait/junk=`lur`, crop=`har`, "
            f"weapon/armor/consumable/ore=`cry`, crafted=`fge`, stone=`dsc`."
        )
        embed = card(
            "\U0001F4E6 NFT Commands",
            color=C_GOLD,
            description=body,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(NFT(bot))
