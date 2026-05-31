"""cogs/auction.py -- Generic auction-house commands.

Replaces the buddy-only ``,buddy market`` flow with a single ``,ah``
group that lists, browses, buys, and cancels any item kind: buddies,
eggs, fish, crops, ore, weapons, armors, consumables, crafted items.

Heavy lifting lives in :mod:`services.auction` -- this module is
presentation only.

Group: ``,ah`` (aliases: ``auction``, ``ahouse``, ``auctionhouse``)

  ,ah                                -- categorised browser (dropdown + buttons)
  ,ah browse [kind]                  -- same browser, optionally pre-filtered
  ,ah search <text>                  -- free-text search by name/species/token
  ,ah list <buddy_id|token_id|name> <price> [currency] [--ttl=days]
  ,ah buy <listing_id> [pay_currency]
  ,ah cancel <listing_id>
  ,ah mine
  ,ah inspect <listing_id>           -- detailed view, includes token id
  ,ah sold                           -- your settled history
  ,ah token <token_id>               -- inspect any minted item
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.scale import to_human
from core.framework.ui import (
    C_AMBER, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PURPLE, C_SUCCESS, C_TEAL,
    fmt_ts,
)
from services import auction as auc
from services import items as _items

log = logging.getLogger(__name__)


# Color cue per kind so a buddy listing reads as purple, a fish as
# teal, an enchanted relic as legendary, etc.
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
}

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
}

# Pretty kind labels for the dropdown + section headers.
_KIND_LABEL = {
    "buddy":      "Buddies",
    "egg":        "Eggs",
    "fish":       "Fish",
    "crop":       "Crops",
    "ore":        "Ore",
    "weapon":     "Weapons",
    "armor":      "Armor",
    "consumable": "Consumables",
    "crafted":    "Crafted Items",
}

# Sort cycle states + their labels for the cycle button.
_SORT_LABEL = {
    "newest":    "\U0001F195 Newest",
    "cheapest":  "\U0001F4B8 Cheapest",
    "expensive": "\U0001F48E Most Expensive",
    "expiring":  "\U000023F0 Expiring Soon",
}
_SORT_NEXT = {
    "newest":    "cheapest",
    "cheapest":  "expensive",
    "expensive": "expiring",
    "expiring":  "newest",
}


def _row_label(row: dict) -> str:
    """One-line summary for a listing row."""
    kind = str(row.get("kind") or "")
    ref = str(auc._as_dict(row.get("metadata")).get("ref") or "?")
    qty = int(row.get("qty") or 1)
    price_h = to_human(int(row.get("price_raw") or 0))
    cur = str(row.get("currency") or "")
    emoji = _KIND_EMOJI.get(kind, "\U0001F4E6")
    qty_part = f" x{qty}" if qty > 1 else ""
    return (
        f"{emoji} **{ref}**{qty_part}  -  "
        f"**{price_h:,.2f} {cur}**  -  `#{int(row['id'])}`"
    )


# ── Browse view ─────────────────────────────────────────────────────────────


def _stack_listings(rows: list[dict]) -> list[dict]:
    """Collapse listings that look identical to a buyer into one row.

    The auction house ends up noisy when a single seller posts many
    copies of the same thing -- 50 separate Healing Herb listings at
    the same price, or 8 buddies that share name / level / rarity /
    gender. To a buyer they read as one bucket, so the browse view
    folds them: same seller + kind + ref + price + currency + the
    visible distinguishing metadata (rarity / level / name / gender /
    species) collapse into a single row whose qty is the sum across
    the stack.

    First-seen order is preserved so the upstream sort (newest /
    cheapest / ...) still drives the page. The lowest listing id wins
    the representative slot so ``,ah buy <id>`` from the embed still
    targets a real, oldest-first auction. The full id list rides along
    on ``_stack_ids`` for callers that want it (e.g. the row renderer
    surfaces ``×N listings`` when N > 1).
    """
    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []
    for r in rows or []:
        md = auc._as_dict(r.get("metadata"))
        rarity = md.get("rarity_tier")
        level = md.get("level")
        key = (
            int(r.get("seller_user_id") or 0),
            str(r.get("kind") or ""),
            str(md.get("ref") or ""),
            str(r.get("price_raw") or "0"),
            str(r.get("currency") or "").upper(),
            int(rarity) if rarity is not None else None,
            int(level) if level is not None else None,
            str(md.get("name") or ""),
            str(md.get("gender") or ""),
            str(md.get("species") or ""),
        )
        rid = int(r.get("id") or 0)
        if key not in grouped:
            stacked = dict(r)
            stacked["_stack_count"] = 1
            stacked["_stack_ids"] = [rid]
            grouped[key] = stacked
            order.append(key)
            continue
        agg = grouped[key]
        agg["qty"] = int(agg.get("qty") or 1) + int(r.get("qty") or 1)
        agg["_stack_count"] = int(agg["_stack_count"]) + 1
        agg["_stack_ids"].append(rid)
        if rid < int(agg.get("id") or 0):
            agg["id"] = rid
    return [grouped[k] for k in order]


async def _oracle_map_for_rows(
    db: Any, guild_id: int, rows: list[dict],
) -> dict[str, float]:
    """Fetch oracle prices for every unique currency on this page.

    Used by ``,ah browse`` / ``,ah search`` to render a `~$X.XX` USD
    equivalent next to each row's listed-currency price. Returns a
    map keyed by uppercased symbol; symbols without an oracle row
    are simply absent (the row will skip the USD column).
    """
    syms: set[str] = set()
    for r in rows or []:
        c = (r.get("currency") or "").upper()
        if c and c != "USD":
            syms.add(c)
    out: dict[str, float] = {}
    for sym in syms:
        try:
            row = await db.get_price(sym, int(guild_id))
        except Exception:
            row = None
        if row and float(row.get("price") or 0.0) > 0:
            out[sym] = float(row["price"])
    # USD is always 1.0 -- saves the per-symbol if-check downstream.
    out["USD"] = 1.0
    return out


def _build_browse_embed(
    rows: list[dict],
    *,
    kind: str | None,
    sort: str,
    page: int,
    page_size: int = 8,
    title_extra: str = "",
    oracle_map: dict[str, float] | None = None,
) -> tuple[discord.Embed, int]:
    """Render one page of the browse view.

    Returns ``(embed, total_pages)`` so the caller can disable nav
    buttons when there's only one page. Color tints to the active
    kind filter so a fish-only browse reads teal, a buddy-only browse
    reads purple, etc. -- the colored cue the user asked for.

    Discord caps a single embed at 6000 chars combined (title +
    description + every field name + every field value + footer +
    author). The categorised view used to render ``page_size`` (8) rows
    spread across up to 9 kind fields with metadata-heavy buddy rows
    that average ~200 chars each, plus ``(cont)`` chunking when a kind
    overflowed 1024-per-field; on a busy server the combined size
    crossed 6000 and ,ah browse 400'd. We now compute the running
    char-budget as we add fields and stop adding rows once we'd cross
    the cap, surfacing a one-line "+N more" hint so the player knows
    to filter or page.
    """
    rows = _stack_listings(rows)
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    chunk = rows[start: start + page_size]

    color = _KIND_COLOR.get(kind or "", C_GOLD) if kind else C_GOLD
    title = "\U0001F3DB Auction House"
    if kind:
        title += f"  ·  {_KIND_EMOJI.get(kind, '')} {_KIND_LABEL.get(kind, kind.title())}"
    if title_extra:
        title += f"  ·  {title_extra}"
    title += f"  ({total} listing{'s' if total != 1 else ''})"

    if not chunk:
        return (
            card(
                title, color=color,
                description=(
                    "_(no listings match this filter)_\n\n"
                    "Try a different kind or `,ah list` something yourself."
                ),
            )
            .footer(_SORT_LABEL.get(sort, sort))
            .build(),
            1,
        )

    builder = card(title, color=color)
    # Discord caps a single embed at 6000 chars combined (title +
    # description + every field name + value + footer + author). Cap
    # ourselves at 5500 so the footer + any "Truncated" hint we tack
    # on at the end always fit. ``discord.Embed.__len__`` already sums
    # those fields, so we read it through the CardBuilder's internal
    # embed instance to know our running size as we add content.
    _EMBED_SOFT_CAP = 5500

    def _embed_len() -> int:
        try:
            return len(builder._embed)  # type: ignore[arg-type]
        except Exception:
            return 0

    truncated = 0
    if kind is None:
        # Categorised view: bucket the chunk by kind so the player can
        # eyeball what's selling across surfaces. One field per kind
        # with the per-row label inside, chunked into successive
        # ``Kind (cont)`` fields when the joined lines exceed
        # Discord's 1024-char field-value cap (mirrors ,buddy pools /
        # ,buddy species). 1000 keeps a buffer under the hard cap.
        by_kind: dict[str, list[dict]] = {}
        for r in chunk:
            by_kind.setdefault(str(r.get("kind") or "?"), []).append(r)
        kinds_left = list(auc.SUPPORTED_KINDS)
        rows_dropped = 0
        for k in auc.SUPPORTED_KINDS:
            kinds_left.pop(0)
            bucket = by_kind.get(k) or []
            if not bucket:
                continue
            kemoji = _KIND_EMOJI.get(k, "\U0001F4E6")
            klabel = _KIND_LABEL.get(k, k.title())
            lines = [_browse_row_line(r, oracle_map) for r in bucket]
            base_name = f"{kemoji} {klabel}  ({len(bucket)})"
            cont_name = f"{kemoji} {klabel}  (cont)"
            current: list[str] = []
            current_len = 0
            field_idx = 0
            stopped = False

            def _commit() -> bool:
                """Try to commit ``current`` as the next field. Returns
                True on success; False when the field would overflow the
                soft cap (in which case the caller should bail out).
                """
                nonlocal field_idx, current, current_len
                if not current:
                    return True
                field_name = base_name if field_idx == 0 else cont_name
                # +1 chars/line accounted for by "\n".join already; this
                # is the projected chars added to the embed if we commit.
                projected = _embed_len() + len(field_name) + current_len
                if projected > _EMBED_SOFT_CAP:
                    return False
                builder.field(field_name, "\n".join(current), False)
                field_idx += 1
                current = []
                current_len = 0
                return True

            for idx, line in enumerate(lines):
                projected_field = current_len + (1 if current else 0) + len(line)
                if current and projected_field > 1000:
                    if not _commit():
                        # Flush current to truncated count, then bail.
                        rows_dropped += len(bucket) - idx
                        stopped = True
                        break
                # Will adding this line push the would-be embed over?
                tentative_field_len = (
                    len(line) if not current
                    else current_len + 1 + len(line)
                )
                tentative_field_name = (
                    base_name if field_idx == 0 and not current else (
                        cont_name if field_idx > 0 and not current else ""
                    )
                )
                if (
                    _embed_len() + len(tentative_field_name)
                    + tentative_field_len
                ) > _EMBED_SOFT_CAP:
                    # Commit whatever we have, then stop.
                    _commit()
                    rows_dropped += len(bucket) - idx
                    stopped = True
                    break
                current.append(line)
                current_len = tentative_field_len
            if not stopped:
                # End of bucket -- flush the trailing partial field.
                if not _commit():
                    # Last field couldn't fit either; everything in
                    # ``current`` is dropped.
                    rows_dropped += len(current)
                    stopped = True
            if stopped:
                # Add any kinds we haven't reached yet to the dropped
                # count so the hint reflects the full pageful.
                for kk in kinds_left:
                    rows_dropped += len(by_kind.get(kk) or [])
                break
        truncated = rows_dropped
    else:
        # Single-kind view: pack rows into the description, stopping
        # before we cross the embed cap. Description shares the same
        # per-embed pool, so we treat each line's cost as len(line) +
        # 2 for the "\n\n" join we'll use below.
        desc_lines: list[str] = []
        running = 0
        for r in chunk:
            line = _browse_row_line(r, oracle_map)
            cost = len(line) + (2 if desc_lines else 0)
            if (_embed_len() + running + cost) > _EMBED_SOFT_CAP:
                truncated = len(chunk) - len(desc_lines)
                break
            desc_lines.append(line)
            running += cost
        builder = builder.description("\n\n".join(desc_lines))

    if truncated > 0:
        # Best-effort hint -- the embed is already at cap, so if we
        # can't fit the field even this short, just drop it silently.
        hint_name = "⚠️ Truncated"
        hint_value = (
            f"+{truncated} more on this page didn't fit. Filter by "
            f"kind (`,ah browse buddy`, `,ah browse fish`, ...) "
            f"or sort cheapest to narrow down."
        )
        if (
            _embed_len() + len(hint_name) + len(hint_value)
        ) <= _EMBED_SOFT_CAP + 200:  # small slack for the hint itself
            builder = builder.field(hint_name, hint_value, False)

    builder = builder.footer(
        f"Page {page}/{total_pages}  ·  "
        f"Sort: {_SORT_LABEL.get(sort, sort)}"
    )
    return builder.build(), total_pages


def _browse_row_line(
    r: dict, oracle_map: dict[str, float] | None = None,
) -> str:
    """One pretty line per listing for the browse embed.

    ``oracle_map`` (optional): maps currency symbol -> live USD oracle
    price. When provided, the row's price gets a `~$X.XX` USD
    equivalent appended so players can compare across currencies at
    a glance. Not provided -> network-coin price only (legacy).
    """
    md = auc._as_dict(r.get("metadata"))
    seller = int(r.get("seller_user_id") or 0)
    ref = str(md.get("ref") or "?")
    kind = str(r.get("kind") or "")
    qty = int(r.get("qty") or 1)
    price_h = to_human(int(r.get("price_raw") or 0))
    cur = str(r.get("currency") or "")
    usd_h: float | None = None
    if oracle_map and cur:
        oracle = oracle_map.get(cur.upper())
        if oracle is not None and oracle > 0:
            try:
                usd_h = price_h * float(oracle)
            except Exception:
                usd_h = None
    usd_part = f"  ·  ~${usd_h:,.2f}" if usd_h is not None else ""
    tok_short = _items.short_id(str(r.get("token_id") or ""))
    # Gender glyph only for buddies (eggs are genderless until they
    # hatch, so we don't show one for them).
    try:
        from configs.buddies_config import gender_glyph as _gender_glyph
    except Exception:
        _gender_glyph = lambda g: ""  # type: ignore
    glyph = _gender_glyph(md.get("gender")) if kind == "buddy" else ""
    extras: list[str] = []
    if kind == "buddy":
        if md.get("name"):
            extras.append(str(md["name"]))
        extras.append(f"Lv.{int(md.get('level') or 1)}")
        # Render rarity by name when known so "Legendary" reads
        # better than "T5"; fall through to T-prefix when the
        # buddies_config import is unavailable (test envs).
        try:
            from configs.buddies_config import rarity_meta as _b_rarity
            tier_v = md.get("rarity_tier")
            if tier_v is not None:
                rt = int(tier_v)
                tier_name = str(_b_rarity(rt).get("name") or f"T{rt}")
                extras.append(tier_name)
        except Exception:
            if md.get("rarity_tier") is not None:
                extras.append(f"T{int(md.get('rarity_tier'))}")
        if glyph:
            extras.append(glyph)
    elif kind == "egg":
        try:
            from configs.buddies_config import rarity_meta as _b_rarity
            tier_v = md.get("rarity_tier")
            if tier_v is not None:
                rt = int(tier_v)
                tier_name = str(_b_rarity(rt).get("name") or f"T{rt}")
                extras.append(
                    f"{tier_name} "
                    f"{str(md.get('species') or '?').title()}"
                )
            else:
                extras.append(str(md.get("species") or "?").title())
        except Exception:
            extras.append(
                f"T{int(md.get('rarity_tier') or 1)} "
                f"{str(md.get('species') or '?').title()}"
            )
    qty_part = f" x{qty}" if qty > 1 else ""
    extras_part = f"  ·  {' / '.join(extras)}" if extras else ""
    emoji = _KIND_EMOJI.get(kind, "")
    # Stacked rows (same seller + same item + same price merged by
    # _stack_listings) swap the per-token id footer for a count of
    # collapsed listings; the leading id is the lowest in the stack
    # so ,ah buy still targets a real auction.
    stack_count = int(r.get("_stack_count") or 1)
    if stack_count > 1:
        footer = (
            f"-# seller <@{seller}>  ·  ×{stack_count} listings "
            f"(buy `#{int(r['id'])}` first)"
        )
    else:
        footer = f"-# seller <@{seller}>  ·  token `{tok_short}`"
    return (
        f"`#{int(r['id']):>4}` {emoji} **{ref}**{qty_part}  ·  "
        f"**{price_h:,.2f} {cur}**{usd_part}{extras_part}\n"
        f"{footer}"
    )


class _KindSelect(discord.ui.Select):
    """Dropdown for kind filter on the browse view."""

    def __init__(self, current: str | None) -> None:
        opts = [
            discord.SelectOption(
                label="All kinds",
                value="__all__",
                emoji="\U0001F3DB",
                description="Mixed view, all listings",
                default=(current is None),
            ),
        ]
        for k in auc.SUPPORTED_KINDS:
            opts.append(discord.SelectOption(
                label=_KIND_LABEL.get(k, k.title()),
                value=k,
                emoji=_KIND_EMOJI.get(k, "\U0001F4E6"),
                default=(current == k),
            ))
        super().__init__(
            placeholder="Filter by kind...",
            options=opts,
            min_values=1, max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "BrowseView" = self.view  # type: ignore
        choice = self.values[0]
        view.kind = None if choice == "__all__" else choice
        view.page = 1
        await view.refresh(interaction)


class _BrowsePickSelect(discord.ui.Select):
    """Per-page listing picker. Populated dynamically from the
    BrowseView's last fetched page slice. Selecting a row opens the
    full ``ListingActionView`` (Buy / Cancel / Refresh) as a follow-up
    message so the buyer can settle without typing the listing id.
    """

    def __init__(self, page_rows: list[dict]) -> None:
        opts: list[discord.SelectOption] = []
        for r in page_rows[:25]:
            try:
                lid = int(r.get("id") or 0)
            except (TypeError, ValueError):
                continue
            kind = str(r.get("kind") or "")
            md = auc._as_dict(r.get("metadata"))
            ref = str(md.get("ref") or "?")
            cur = str(r.get("currency") or "")
            try:
                price_h = to_human(int(r.get("price_raw") or 0))
            except Exception:
                price_h = 0.0
            label = f"#{lid}  ·  {ref}"[:100]
            desc = f"{price_h:,.2f} {cur}  ·  {kind}"[:100]
            opts.append(discord.SelectOption(
                label=label,
                value=str(lid),
                description=desc,
                emoji=_KIND_EMOJI.get(kind, "\U0001F4E6"),
            ))
        if not opts:
            # Discord requires at least one option; ship a disabled dummy.
            opts = [discord.SelectOption(
                label="(no listings on this page)",
                value="__none__",
                default=True,
            )]
        super().__init__(
            placeholder="Pick a listing to inspect / buy...",
            options=opts,
            min_values=1, max_values=1,
            row=2,
            disabled=(opts[0].value == "__none__"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "BrowseView" = self.view  # type: ignore
        try:
            lid = int(self.values[0])
        except (TypeError, ValueError):
            await interaction.response.send_message(
                "Invalid listing pick.", ephemeral=True,
            )
            return
        row = await auc.find_listing(view.ctx.db, lid)
        if not row:
            await interaction.response.send_message(
                f"Listing #{lid} no longer exists -- click Refresh.",
                ephemeral=True,
            )
            return
        prefix = await view.ctx.get_guild_prefix()
        embed = _build_listing_inspect_embed(row, prefix)
        action_view = ListingActionView(
            view.ctx,
            listing_id=int(row["id"]),
            seller_user_id=int(row.get("seller_user_id") or 0),
            is_active=(str(row.get("status") or "") == "active"),
        )
        await interaction.response.send_message(
            embed=embed, view=action_view,
        )
        try:
            action_view.message = await interaction.original_response()
        except Exception:
            pass


class BrowseView(discord.ui.View):
    """Interactive auction-house browser.

    Owner-locked (only the player who opened it can interact). 5 minute
    timeout. Provides a kind dropdown, a sort cycle button, and prev /
    next page buttons. Re-fetches on every interaction so newly-listed
    items show up immediately and just-bought ones disappear.
    """

    def __init__(
        self, ctx: DiscoContext, *,
        kind: str | None = None,
        sort: str = "newest",
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.kind = kind
        self.sort = sort
        self.page = 1
        self.message: discord.Message | None = None
        self.add_item(_KindSelect(kind))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your browse session. Run `,ah` to open your own.",
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

    async def _fetch(self) -> list[dict]:
        return await auc.browse_active(
            self.ctx.db, self.ctx.guild_id,
            kind=self.kind, sort=self.sort, limit=200,
        )

    def _replace_pick_select(self, page_rows: list[dict]) -> None:
        """Rebuild the row-2 listing picker from the current page slice.

        Selects can't have their options mutated post-construction
        (Discord locks them once attached), so we drop the old one
        and add a fresh _BrowsePickSelect with the new page rows.
        """
        for child in list(self.children):
            if isinstance(child, _BrowsePickSelect):
                self.remove_item(child)
        self.add_item(_BrowsePickSelect(page_rows))

    async def refresh(self, interaction: discord.Interaction) -> None:
        rows = await self._fetch()
        # Oracle prices for the current page so each row can show
        # ~$X.XX next to its native-currency price.
        page_size = 8
        page = max(1, self.page)
        page_slice = rows[(page - 1) * page_size: page * page_size]
        oracle_map = await _oracle_map_for_rows(
            self.ctx.db, self.ctx.guild_id, page_slice,
        )
        embed, total_pages = _build_browse_embed(
            rows, kind=self.kind, sort=self.sort, page=self.page,
            oracle_map=oracle_map,
        )
        # Compute the current page slice + replace the picker dropdown
        # so each row maps to a clickable listing on this page.
        page = max(1, min(self.page, total_pages))
        self.page = page
        page_size = 8
        start = (page - 1) * page_size
        page_rows = rows[start: start + page_size]
        self._replace_pick_select(page_rows)
        # Update prev/next button disabled states for the current page.
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
        label="Sort", emoji="\U0001F500",
        style=discord.ButtonStyle.primary, row=1,
    )
    async def btn_sort(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        self.sort = _SORT_NEXT.get(self.sort, "newest")
        self.page = 1
        await self.refresh(interaction)

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self.refresh(interaction)


def _build_listing_inspect_embed(row: dict, prefix: str) -> discord.Embed:
    """Compose the ``,ah inspect`` embed from a listing row + the
    guild prefix. Extracted so the Refresh button can re-render it
    without duplicating the entire build pipeline.
    """
    md = auc._as_dict(row.get("metadata"))
    kind = str(row.get("kind") or "")
    ref = str(md.get("ref") or "?")
    qty = int(row.get("qty") or 1)
    price_h = to_human(int(row.get("price_raw") or 0))
    cur = str(row.get("currency") or "")
    tok = str(row.get("token_id") or "")
    seller = int(row.get("seller_user_id") or 0)
    status = str(row.get("status") or "")

    kind_emoji = _KIND_EMOJI.get(kind, "\U0001F4E6")
    builder = card(
        f"{kind_emoji} Listing #{int(row['id'])}",
        color=_KIND_COLOR.get(kind, C_GOLD),
        description=(
            f"**{ref}** ({kind}) x{qty}  -  "
            f"**{price_h:,.2f} {cur}**\n"
            f"Status: **{status}**  ·  seller <@{seller}>"
        ),
    ).field(
        "Token ID", f"`{tok}`", True,
    ).field(
        "Listed at", fmt_ts(row.get("listed_at")), True,
    )
    if row.get("expires_at"):
        builder = builder.field(
            "Expires", fmt_ts(row["expires_at"]), True,
        )
    try:
        from configs.buddies_config import gender_glyph as _gender_glyph
    except Exception:
        _gender_glyph = lambda g: ""  # type: ignore
    if kind == "buddy":
        details = []
        if md.get("name"):
            details.append(f"**{md['name']}**")
        if md.get("species"):
            details.append(str(md["species"]).title())
        details.append(f"Lv. {int(md.get('level') or 1)}")
        # Render rarity by NAME (Common / Uncommon / Rare / Epic /
        # Legendary) rather than the raw tier int -- "Legendary"
        # reads better than "Tier 5", and for missing-data listings
        # the explicit name surfaces the "we don't know" case better
        # than a silent default-to-1.
        try:
            from configs.buddies_config import rarity_meta as _b_rarity
            tier_v = md.get("rarity_tier")
            if tier_v is not None:
                rt = int(tier_v)
                tier_name = str(_b_rarity(rt).get("name") or f"Tier {rt}")
                details.append(f"**{tier_name}** (T{rt})")
        except Exception:
            if md.get("rarity_tier") is not None:
                details.append(f"Tier {int(md.get('rarity_tier'))}")
        buddy_glyph = _gender_glyph(md.get("gender"))
        if buddy_glyph:
            details.append(buddy_glyph)
        if md.get("wins") or md.get("losses"):
            details.append(
                f"{int(md.get('wins') or 0)}W-"
                f"{int(md.get('losses') or 0)}L"
            )
        builder = builder.field(
            "Buddy", "  ·  ".join(details), False,
        )
    elif kind == "egg":
        egg_line = (
            f"Tier {int(md.get('rarity_tier') or 1)} "
            f"{str(md.get('species') or '?').title()}"
        )
        builder = builder.field("Egg", egg_line, False)
    elif kind == "fish":
        entries = md.get("entries") or []
        if entries:
            weights = [
                f"{float(e.get('lbs') or 0):,.2f} lbs"
                for e in entries[:5]
            ]
            builder = builder.field(
                "Catch sizes", " / ".join(weights), False,
            )
    if row.get("notes"):
        builder = builder.field("Seller note", str(row["notes"]), False)
    builder = builder.footer(
        f"{prefix}ah buy {int(row['id'])}"
        + (f" [pay_currency]" if status == "active" else "")
        + f"  ·  {prefix}ah cancel {int(row['id'])} (seller only)"
    )
    return builder.build()


class ListingActionView(discord.ui.View):
    """Action buttons for ``,ah inspect <id>``.

    Owner-locked, 5-min timeout. Buttons:
      * Buy -- runs the same code path as ``,ah buy <id>`` for the
        invoking user, paying in the listed currency.
      * Cancel -- seller-only; runs ``,ah cancel <id>``.
      * Refresh -- re-renders the inspect embed.

    The view's owner is the player who ran ``,ah inspect``, NOT
    necessarily the seller. The Cancel button is hidden when the
    invoking user isn't the seller, so a buyer can't accidentally
    fire it.
    """

    def __init__(
        self,
        ctx: DiscoContext,
        listing_id: int,
        seller_user_id: int,
        is_active: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.listing_id = int(listing_id)
        self.seller_user_id = int(seller_user_id)
        self.message: discord.Message | None = None
        # Buy is only useful when active + you're not the seller.
        if not (is_active and ctx.author.id != seller_user_id):
            for child in list(self.children):
                if (
                    isinstance(child, discord.ui.Button)
                    and child.label == "Buy"
                ):
                    self.remove_item(child)
        # Cancel only shows for the seller.
        if not (is_active and ctx.author.id == seller_user_id):
            for child in list(self.children):
                if (
                    isinstance(child, discord.ui.Button)
                    and child.label == "Cancel"
                ):
                    self.remove_item(child)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your inspect view. Run `,ah inspect <id>` "
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

    @discord.ui.button(
        label="Buy", emoji="\U0001F4B0",
        style=discord.ButtonStyle.success, row=0,
    )
    async def btn_buy(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            res = await auc.buy_listing(
                self.ctx.db,
                guild_id=self.ctx.guild_id,
                buyer_user_id=interaction.user.id,
                listing_id=self.listing_id,
            )
        except ValueError as e:
            await interaction.response.send_message(
                str(e), ephemeral=True,
            )
            return
        except Exception as e:
            log.exception(
                "ah inspect Buy click failed listing=%s uid=%s",
                self.listing_id, interaction.user.id,
            )
            await interaction.response.send_message(
                f"Could not settle: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=card(
                f"\U0001F4B0 Bought  ·  Listing #{self.listing_id}",
                description=(
                    f"Token: `{_items.short_id(str(res.token_id))}`\n"
                    f"Paid: `{to_human(res.paid_price_raw):,.4f} "
                    f"{res.currency_paid}`"
                ),
                color=C_SUCCESS,
            ).build(),
            ephemeral=False,
        )

    @discord.ui.button(
        label="Cancel", emoji="\U000026D4",
        style=discord.ButtonStyle.danger, row=0,
    )
    async def btn_cancel(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            ok, msg = await auc.cancel_listing(
                self.ctx.db, self.listing_id, interaction.user.id,
            )
        except ValueError as e:
            await interaction.response.send_message(
                str(e), ephemeral=True,
            )
            return
        except Exception as e:
            log.exception(
                "ah inspect Cancel click failed listing=%s",
                self.listing_id,
            )
            await interaction.response.send_message(
                f"Cancel failed: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=card(
                f"\U000026D4 Cancelled  ·  Listing #{self.listing_id}",
                description=msg,
                color=C_NEUTRAL,
            ).build(),
            ephemeral=False,
        )

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=0,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        # Re-fetch + re-render the listing embed in place.
        row = await auc.find_listing(self.ctx.db, self.listing_id)
        if not row:
            await interaction.response.send_message(
                f"Listing #{self.listing_id} no longer exists.",
                ephemeral=True,
            )
            return
        embed = _build_listing_inspect_embed(row, await self.ctx.get_guild_prefix())
        await interaction.response.edit_message(embed=embed, view=self)


class Auction(commands.Cog, name="Auction"):
    """``,ah`` -- generic auction-house: list, browse, buy, cancel."""

    # Listings older than expires_at are auto-returned to the seller.
    # Sweep every 5 min so an expired buddy / fish / weapon gets back
    # in the player's hands soon after the timer pops, but the loop
    # isn't hammering the DB.
    _EXPIRE_SWEEP_INTERVAL_S: int = 300

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._expire_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        if self._expire_task is None or self._expire_task.done():
            self._expire_task = asyncio.create_task(self._expire_loop())

    async def cog_unload(self) -> None:
        if self._expire_task and not self._expire_task.done():
            self._expire_task.cancel()

    async def _expire_loop(self) -> None:
        """Background sweep: returns expired-listing items to sellers
        and DMs them so they know to relist.

        Wraps the service call so the loop is resilient to transient
        DB hiccups -- one failed sweep doesn't kill the task.
        """
        await self.bot.wait_until_ready()
        while True:
            try:
                await asyncio.sleep(self._EXPIRE_SWEEP_INTERVAL_S)
                # Snapshot the about-to-expire rows BEFORE running the
                # sweep so we can DM each seller with their listing
                # details. The sweep itself flips status='expired' and
                # returns the items.
                pending = await self.bot.db.fetch_all(
                    "SELECT * FROM auction_listings "
                    "WHERE status = 'active' "
                    "  AND expires_at IS NOT NULL "
                    "  AND expires_at <= NOW() "
                    "LIMIT 500",
                )
                expired = await auc.expire_old(self.bot.db)
                for row in (pending or [])[:expired]:
                    try:
                        await self._dm_expiry(dict(row))
                    except Exception:
                        log.debug(
                            "auction expire DM failed for listing %s",
                            row.get("id"), exc_info=True,
                        )
                if expired:
                    log.info(
                        "auction expire sweep: returned %s expired listing(s)",
                        expired,
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("auction expire sweep: unhandled error")

    # ── Token-id-driven list helpers ───────────────────────────────────────

    @staticmethod
    def _looks_like_token_id(s: str) -> bool:
        """Cheap shape check: ``<network>:<hex>``. Doesn't validate that
        the token actually exists -- that happens in the service call.
        """
        if not s or s.count(":") != 1:
            return False
        net, hx = s.split(":", 1)
        if not net or not hx:
            return False
        return all(c in "0123456789abcdef" for c in hx.lower())

    @staticmethod
    def _is_num(s: str) -> bool:
        try:
            float(s)
            return True
        except ValueError:
            return False

    async def _ah_list_by_token(
        self,
        ctx: DiscoContext,
        token_id: str,
        rest: list[str],
        ttl_days: int | None,
    ) -> None:
        """Run the ``,ah list <token_id> <price> [currency]`` path."""
        if not rest or not self._is_num(rest[0]):
            await ctx.reply_error(
                f"Need a price after the token id: "
                f"`,ah list {token_id} 100`."
            )
            return
        try:
            price = float(rest[0])
        except ValueError:
            await ctx.reply_error("Couldn't parse price.")
            return
        currency = rest[1].upper() if len(rest) >= 2 else None

        try:
            listing_id, escrowed_token_id, msg = (
                await auc.create_listing_by_token(
                    ctx.db,
                    guild_id=ctx.guild_id,
                    seller_user_id=ctx.author.id,
                    token_id=token_id,
                    price=price,
                    currency=currency,
                    ttl_days=ttl_days,
                )
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        except Exception as e:
            log.exception(
                "ah list (token) failed gid=%s uid=%s token=%s",
                ctx.guild_id, ctx.author.id, token_id,
            )
            err_cls = type(e).__name__
            err_msg = str(e) or "no detail"
            await ctx.reply_error(
                f"Could not list that token: `{err_cls}: {err_msg}`."
            )
            return

        prefix = await ctx.get_guild_prefix()
        try:
            await ctx.bot.bus.publish(
                "ah_listing_created",
                guild=ctx.guild, user=ctx.author,
                listing_id=int(listing_id),
                kind="(token)",
                ref=str(token_id),
            )
        except Exception:
            log.debug("ah_listing_created publish failed", exc_info=True)

        embed = (
            card(
                f"\U0001F3DB Listed!  -  #{int(listing_id)}",
                description=msg,
                color=C_SUCCESS,
            )
            .footer(
                f"Cancel any time with `{prefix}ah cancel {int(listing_id)}` "
                f"(seller only)."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _dm_sale_to_seller(
        self,
        res: "auc.SaleResult",
        listed_currency: str,
        seller_received_h: float,
        fee_burned_h: float,
        *,
        buyer_display: str,
    ) -> None:
        """DM the seller after their listing sells. Best-effort -- a
        closed DM logs and moves on.
        """
        seller_uid = int(res.seller_id)
        if seller_uid <= 0:
            return
        try:
            user = (
                self.bot.get_user(seller_uid)
                or await self.bot.fetch_user(seller_uid)
            )
        except Exception:
            return
        if user is None:
            return
        emoji = _KIND_EMOJI.get(res.kind, "\U0001F4E6")
        seller_bonus_h = to_human(int(getattr(res, "seller_bonus_raw", 0) or 0))
        builder = (
            card(
                f"{emoji} Your listing sold!  ·  #{res.listing_id}",
                color=C_SUCCESS,
                description=(
                    f"Buyer: **{buyer_display}**  ·  "
                    f"Token: `{_items.short_id(res.token_id)}`"
                ),
            )
            .field(
                "You received",
                f"**{seller_received_h:,.4f} {listed_currency}**",
                True,
            )
            .field(
                "House fee burned",
                f"{fee_burned_h:,.4f} {listed_currency}",
                True,
            )
        )
        if seller_bonus_h > 0:
            builder = builder.field(
                "\U0001FA99 Gavelstone bonus",
                f"+{seller_bonus_h:,.4f} {listed_currency}",
                True,
            )
        embed = builder.footer(
            "Sale settled. Funds are in your wallet."
        ).build()
        try:
            await user.send(embed=embed)
        except Exception:
            log.debug(
                "auction sale DM failed for uid=%s", seller_uid, exc_info=True,
            )

    async def _dm_purchase_to_buyer(
        self,
        res: "auc.SaleResult",
        listed_currency: str,
        listed_h: float,
        paid_h: float,
    ) -> None:
        """DM the buyer a purchase receipt. Useful for cross-channel
        confirmations and for cross-currency buys where the impact +
        listed-vs-paid difference matters.
        """
        buyer_uid = int(res.buyer_id)
        if buyer_uid <= 0:
            return
        try:
            user = (
                self.bot.get_user(buyer_uid)
                or await self.bot.fetch_user(buyer_uid)
            )
        except Exception:
            return
        if user is None:
            return
        emoji = _KIND_EMOJI.get(res.kind, "\U0001F4E6")
        rebate_h = to_human(int(getattr(res, "buyer_rebate_raw", 0) or 0))
        builder = (
            card(
                f"{emoji} Purchase confirmed  ·  #{res.listing_id}",
                color=C_SUCCESS,
                description=(
                    f"You bought **{res.qty}x {res.kind}** from "
                    f"<@{res.seller_id}>.\n"
                    f"Token: `{_items.short_id(res.token_id)}`"
                ),
            )
            .field(
                "Listed price",
                f"{listed_h:,.2f} {listed_currency}",
                True,
            )
            .field(
                "You paid",
                f"{paid_h:,.4f} {res.currency_paid}",
                True,
            )
        )
        if rebate_h > 0:
            builder = builder.field(
                "\U0001FA99 Gavelstone rebate",
                f"+{rebate_h:,.4f} {res.currency_paid}",
                True,
            )
        embed = builder.footer(
            res.note
            or "Settled. The item is in your inventory now."
        ).build()
        try:
            await user.send(embed=embed)
        except Exception:
            log.debug(
                "auction purchase DM failed for uid=%s",
                buyer_uid, exc_info=True,
            )

    async def _dm_expiry(self, row: dict) -> None:
        """DM the seller after their listing expires + the item gets
        returned. Best-effort; closed DMs are a no-op.
        """
        seller_uid = int(row.get("seller_user_id") or 0)
        if seller_uid <= 0:
            return
        try:
            user = (
                self.bot.get_user(seller_uid)
                or await self.bot.fetch_user(seller_uid)
            )
        except Exception:
            return
        if user is None:
            return
        md = auc._as_dict(row.get("metadata"))
        ref = str(md.get("ref") or "?")
        kind = str(row.get("kind") or "")
        qty = int(row.get("qty") or 1)
        price_h = to_human(int(row.get("price_raw") or 0))
        cur = str(row.get("currency") or "")
        emoji = _KIND_EMOJI.get(kind, "\U0001F4E6")
        embed = (
            card(
                f"{emoji} Listing expired  ·  #{int(row.get('id') or 0)}",
                color=C_NEUTRAL,
                description=(
                    f"**{qty}x {ref}** ({kind}) @ "
                    f"**{price_h:,.2f} {cur}** didn't sell. "
                    f"The item is back in your inventory -- "
                    f"relist with `,ah list` if you want another shot."
                ),
            )
            .footer(f"Token: {_items.short_id(str(row.get('token_id') or ''))}")
            .build()
        )
        try:
            await user.send(embed=embed)
        except Exception:
            log.debug(
                "auction expiry DM failed for uid=%s", seller_uid,
                exc_info=True,
            )

    @commands.group(
        name="ah",
        # NOTE: ``market`` is intentionally NOT in this list -- there's
        # already a top-level ``,market`` command on cogs/overview.py
        # (the cross-token price browser) and registering an alias here
        # collides with it at cog-load time. Stick to ``ah`` plus the
        # explicit ``auction`` / ``ahouse`` / ``auctionhouse`` synonyms.
        aliases=["auction", "ahouse", "auctionhouse"],
        invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def ah(self, ctx: DiscoContext) -> None:
        """Open the auction-house browser: every active listing,
        categorised by kind, with a dropdown filter and pagination.

        Use ``,ah mine`` for your own listings, ``,ah list ...`` to put
        an item up, ``,ah search <text>`` to find a specific item, and
        ``,ah help`` for the full command list.
        """
        try:
            from services.onboarding import maybe_send_intro
            await maybe_send_intro(ctx, "auction")
        except Exception:
            pass
        await self._open_browser(ctx, kind=None)

    async def _open_browser(
        self,
        ctx: DiscoContext,
        *,
        kind: str | None,
        sort: str = "newest",
    ) -> None:
        """Shared open path for ``,ah`` and ``,ah browse`` -- builds
        the BrowseView, fetches the first page, and sends the message.
        """
        rows = await auc.browse_active(
            ctx.db, ctx.guild_id, kind=kind, sort=sort, limit=200,
        )
        prefix = await ctx.get_guild_prefix()
        if not rows:
            await ctx.reply(
                embed=card(
                    "\U0001F3DB Auction House",
                    color=_KIND_COLOR.get(kind or "", C_GOLD),
                    description=(
                        "_(no active listings"
                        + (f" for `{kind}`" if kind else "")
                        + " right now)_\n\n"
                        f"`{prefix}ah list <name> <price>`  list one yourself "
                        f"(e.g. `{prefix}ah list minnow 5`)\n"
                        f"`{prefix}ah search <text>`  search by name / species / token\n"
                        f"`{prefix}ah help`  full command list"
                    ),
                ).build(),
                mention_author=False,
            )
            return
        view = BrowseView(ctx, kind=kind, sort=sort)
        oracle_map = await _oracle_map_for_rows(
            ctx.db, ctx.guild_id, rows[:8],
        )
        embed, total_pages = _build_browse_embed(
            rows, kind=kind, sort=sort, page=1,
            oracle_map=oracle_map,
        )
        # Initial picker population from page 1.
        view._replace_pick_select(rows[:8])
        # Disable nav buttons that don't apply on the first page.
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

    # ── ,ah list ───────────────────────────────────────────────────────────

    @ah.command(name="list", aliases=["sell", "post"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_list(
        self, ctx: DiscoContext, *, args: str = "",
    ) -> None:
        """List an item on the auction house.

        Three forms, all NFT-driven (one token per listing):

          ``,ah list <buddy_id> <price>``
            ``,ah list 1234 50000``        -- buddy id from ``,buddy stats``

          ``,ah list <token_id> <price> [currency]``
            ``,ah list bud:k889kak 50000`` -- copy id from ``,items inspect``
            ``,ah list fge:fd67e9ee 100``

          ``,ah list <name> <price> [currency]``
            ``,ah list minnow 5``          -- auto-resolves to your oldest
                                              minnow NFT
            ``,ah list worm 0.5 LURE``     -- explicit currency override

        Default currency comes from the network's home coin
        (``bud`` -> BUD, ``lur`` -> LURE, ``cry`` -> RUNE,
        ``fge`` -> INGOT, ``har`` -> HRV).
        Pass ``--ttl=N`` to override the default 7-day expiry.
        """
        parts = (args or "").split()
        if len(parts) < 2:
            prefix = await ctx.get_guild_prefix()
            await ctx.reply_error_hint(
                "Need a name + price. Try "
                "`,ah list <name> <price>` (e.g. `,ah list minnow 5`) "
                "or pass a token id from `,items inspect`.",
                hint=f"{prefix}ah list minnow 5",
                command_name="ah list",
            )
            return

        # Pluck --ttl=N out first so the rest of the parser stays simple.
        ttl_days: int | None = None
        keep: list[str] = []
        for tok in parts:
            if tok.startswith("--ttl="):
                try:
                    ttl_days = max(0, int(tok.split("=", 1)[1]))
                except ValueError:
                    await ctx.reply_error("Bad --ttl value (use a number).")
                    return
            else:
                keep.append(tok)
        parts = keep

        # Token-id-driven path: ``,ah list <network>:<hex> <price> [cur]``.
        # Detected by the first arg containing exactly one ':' and the
        # tail being a valid hex string. Routes to
        # services.auction.create_listing_by_token which derives kind /
        # catalog_key / default-currency from the contract registry.
        first = parts[0].strip()
        if (
            ":" in first
            and len(parts) >= 2
            and self._looks_like_token_id(first)
        ):
            await self._ah_list_by_token(ctx, first, parts[1:], ttl_days)
            return

        # Buddy-id path: ``,ah list 1234 50000`` -- numeric first arg
        # treated as a cc_buddies id. Looks up the buddy + its NFT,
        # routes through the token-id flow.
        if (
            len(parts) >= 2
            and self._is_num(first)
            and self._is_num(parts[1])
            and "." not in first  # decimals are prices, not buddy ids
        ):
            try:
                buddy_id = int(first)
            except ValueError:
                buddy_id = 0
            if buddy_id > 0:
                try:
                    resolved = await auc.find_owned_buddy_token(
                        ctx.db,
                        guild_id=ctx.guild_id,
                        seller_user_id=ctx.author.id,
                        buddy_id=buddy_id,
                    )
                except Exception:
                    resolved = None
                if not resolved:
                    prefix = await ctx.get_guild_prefix()
                    await ctx.reply_error(
                        f"You don't own a buddy with id `{buddy_id}` "
                        f"(or its NFT isn't minted yet). Try "
                        f"`{prefix}buddy stats` to see your buddies' ids."
                    )
                    return
                await self._ah_list_by_token(
                    ctx, resolved, parts[1:], ttl_days,
                )
                return

        # Bare-name path: ``,ah list minnow 5`` -- look up the contract
        # by name / catalog_key, find the seller's OLDEST owned token
        # of that contract, then route through the token-id flow with
        # the auto-resolved id.
        if (
            len(parts) >= 2
            and self._is_num(parts[1])
            and not self._is_num(first)
            and ":" not in first
        ):
            try:
                resolved = await auc.find_owned_token_for_contract(
                    ctx.db,
                    guild_id=ctx.guild_id,
                    seller_user_id=ctx.author.id,
                    name_or_address=first,
                )
            except Exception:
                resolved = None
            if not resolved:
                prefix = await ctx.get_guild_prefix()
                await ctx.reply_error(
                    f"Couldn't find an owned NFT matching `{first}`. "
                    f"Try `{prefix}items list` to see what you own, or "
                    f"`{prefix}db {first}` to look up the contract."
                )
                return
            await self._ah_list_by_token(ctx, resolved, parts[1:], ttl_days)
            return

        # Nothing matched a supported form -- surface the three valid
        # shapes instead of falling through to the legacy parser.
        prefix = await ctx.get_guild_prefix()
        await ctx.reply_error_hint(
            "Three ways to list:\n"
            f"`{prefix}ah list <buddy_id> <price>`  -  e.g. `,ah list 1234 50000`\n"
            f"`{prefix}ah list <token_id> <price>`  -  e.g. `,ah list bud:k889kak 50000`\n"
            f"`{prefix}ah list <name> <price>`  -  e.g. `,ah list minnow 5`",
            hint=f"{prefix}ah list minnow 5",
            command_name="ah list",
        )
        return

    # ── ,ah browse ─────────────────────────────────────────────────────────

    @ah.command(name="browse", aliases=["view", "all", "catalog"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_browse(
        self, ctx: DiscoContext, *, args: str = "",
    ) -> None:
        """Open the auction-house browser, optionally pre-filtered.

        ``,ah browse``                  -- everything, categorised
        ``,ah browse <kind>``           -- filter by kind (buddy / egg / fish / ...)
        ``,ah browse <kind> --sort=cheapest``  -- pick a sort mode

        The browser is interactive: use the dropdown to swap kinds, the
        Sort button to cycle modes, and Prev / Next to page. For text
        search use ``,ah search <query>``.
        """
        kind_filter: str | None = None
        sort_mode = "newest"
        for tok in (args or "").split():
            if tok.startswith("--sort="):
                sort_mode = tok.split("=", 1)[1].strip().lower()
                if sort_mode not in (
                    "newest", "cheapest", "expensive", "expiring",
                ):
                    await ctx.reply_error(
                        "--sort must be one of: newest / cheapest / "
                        "expensive / expiring."
                    )
                    return
            elif kind_filter is None:
                kind_filter = tok.strip().lower()
        if kind_filter and kind_filter not in auc.SUPPORTED_KINDS:
            await ctx.reply_error(
                f"Unknown kind `{kind_filter}`. "
                f"Try: {', '.join(auc.SUPPORTED_KINDS)}."
            )
            return
        await self._open_browser(ctx, kind=kind_filter, sort=sort_mode)

    # ── ,ah search ─────────────────────────────────────────────────────────

    @ah.command(name="search", aliases=["find", "lookup"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_search(
        self, ctx: DiscoContext, *, query: str = "",
    ) -> None:
        """Search active listings by name / species / token id.

        ``,ah search wecco``           -- every wecco listing (egg or buddy)
        ``,ah search bud:k889ka2c``    -- find a listing by token id
        ``,ah search legendary``      -- substring match on the ref/name

        Hit the ``,ah browse`` view if you want to drill in by kind /
        sort instead of free-text.
        """
        q = (query or "").strip()
        if not q:
            await ctx.reply_error_hint(
                "Pass a search term, like `,ah search wecco`.",
                hint="ah search <text>",
                command_name="ah search",
            )
            return
        rows = await auc.search_active(
            ctx.db, ctx.guild_id, q, limit=100,
        )
        prefix = await ctx.get_guild_prefix()
        if not rows:
            await ctx.reply(
                embed=card(
                    f"\U0001F50D Auction Search  ·  `{q}`",
                    color=C_NEUTRAL,
                    description=(
                        f"_(no active listings match `{q}`)_\n\n"
                        f"Try a different term or browse everything with "
                        f"`{prefix}ah browse`."
                    ),
                ).build(),
                mention_author=False,
            )
            return
        # Reuse the same row renderer as the browse view so the result
        # set looks identical to a filtered browse. Stack identical
        # listings from the same seller before slicing so the visible
        # count reflects what the buyer actually sees.
        stacked = _stack_listings(rows)
        oracle_map = await _oracle_map_for_rows(
            ctx.db, ctx.guild_id, stacked[:25],
        )
        lines = [_browse_row_line(r, oracle_map) for r in stacked[:25]]
        match_count = len(stacked)
        embed = (
            card(
                f"\U0001F50D Auction Search  ·  `{q}`  "
                f"({match_count} match{'es' if match_count != 1 else ''})",
                color=C_GOLD,
                description="\n\n".join(lines),
            )
            .footer(
                f"{prefix}ah inspect <id>  ·  {prefix}ah buy <id>"
                + (f"  ·  showing first 25 of {match_count}" if match_count > 25 else "")
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,ah cancel ─────────────────────────────────────────────────────────

    @ah.command(name="cancel", aliases=["delist", "pull"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_cancel(
        self, ctx: DiscoContext, listing_id: int,
    ) -> None:
        """Pull an active listing. Returns the escrowed item to you."""
        try:
            ok, msg = await auc.cancel_listing(
                ctx.db, int(listing_id), ctx.author.id,
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        await ctx.reply_success(msg, title="Listing Cancelled")

    # ── ,ah buy ────────────────────────────────────────────────────────────

    @ah.command(name="buy", aliases=["purchase", "snipe"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_buy(
        self,
        ctx: DiscoContext,
        listing_id: int,
        pay_currency: str = "",
    ) -> None:
        """Purchase an active listing.

        Defaults to paying in the listed currency (no slippage). Pass
        a different symbol (``,ah buy 12 USD``) to convert via the AMM
        on settle -- standard slippage applies, same shape as
        ``,trade swap`` / ``,buy``.
        """
        try:
            res = await auc.buy_listing(
                ctx.db,
                guild_id=ctx.guild_id,
                buyer_user_id=ctx.author.id,
                listing_id=int(listing_id),
                pay_currency=(pay_currency or None),
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        except Exception as e:
            log.exception(
                "ah buy failed gid=%s uid=%s listing=%s",
                ctx.guild_id, ctx.author.id, listing_id,
            )
            # Surface the exception class + message so a player hitting a
            # constraint violation or asyncpg integrity error gets enough
            # to report a real bug, instead of the catch-all "try again".
            err_cls = type(e).__name__
            err_msg = str(e) or "no detail"
            await ctx.reply_error(
                f"Could not settle listing #{int(listing_id)}: "
                f"`{err_cls}: {err_msg}`. The seller still holds the item; "
                f"please report this if it keeps happening."
            )
            return

        prefix = await ctx.get_guild_prefix()
        listed_h = to_human(res.listed_price_raw)
        paid_h = to_human(res.paid_price_raw)
        seller_h = to_human(res.seller_received_raw)
        fee_h = to_human(res.fee_burned_raw)
        listed_currency = (
            (await auc.find_listing(ctx.db, res.listing_id)) or {}
        ).get("currency") or res.currency_paid
        emoji = _KIND_EMOJI.get(res.kind, "\U0001F4E6")
        # Symmetric DMs: seller gets sale receipt, buyer gets purchase
        # receipt. Both best-effort -- closed DMs log + move on so the
        # in-channel embed still renders.
        await self._dm_sale_to_seller(
            res, listed_currency, seller_h, fee_h,
            buyer_display=str(ctx.author),
        )
        await self._dm_purchase_to_buyer(
            res, listed_currency, listed_h, paid_h,
        )
        # Gavelstone (auction-house meta gem) extras -- buyer rebate +
        # seller bonus paid in the listed currency on top of the
        # settlement transfer. Zero when the party owns no Gavelstone.
        rebate_h = to_human(int(getattr(res, "buyer_rebate_raw", 0) or 0))
        seller_bonus_h = to_human(int(getattr(res, "seller_bonus_raw", 0) or 0))
        embed = (
            card(
                f"{emoji} Sold!  -  Listing #{res.listing_id}",
                color=C_SUCCESS,
                description=(
                    f"You bought a **{res.kind}** ({res.qty}x) from "
                    f"<@{res.seller_id}>.\n"
                    f"Token: `{_items.short_id(res.token_id)}`"
                ),
            )
            .field(
                "Listed price",
                f"{listed_h:,.2f} {listed_currency}",
                True,
            )
            .field(
                "You paid",
                f"{paid_h:,.4f} {res.currency_paid}",
                True,
            )
            .field(
                "Seller received",
                f"{seller_h:,.2f} {listed_currency} "
                f"(fee {fee_h:,.4f} burned)",
                False,
            )
        )
        if rebate_h > 0:
            embed = embed.field(
                "\U0001FA99 Gavelstone rebate",
                f"+{rebate_h:,.4f} {res.currency_paid} refunded to you",
                True,
            )
        if seller_bonus_h > 0:
            embed = embed.field(
                "\U0001FA99 Seller's Gavelstone bonus",
                f"+{seller_bonus_h:,.2f} {listed_currency} sent to seller",
                True,
            )
        if res.note:
            embed = embed.footer(res.note)
        await ctx.reply(embed=embed.build(), mention_author=False)
        # Achievement-tracking events. ``ah_purchase_settled`` fires for
        # the buyer; ``ah_sale_settled`` fires for the seller. The
        # cross-currency bump only fires when the player paid in a
        # different token than the listing was posted in.
        try:
            buyer = ctx.author
            await ctx.bot.bus.publish(
                "ah_purchase_settled",
                guild=ctx.guild, user=buyer,
                listing_id=res.listing_id, kind=res.kind,
            )
            if res.currency_paid.upper() != str(listed_currency).upper():
                await ctx.bot.bus.publish(
                    "ah_cross_currency_buy",
                    guild=ctx.guild, user=buyer,
                    listing_id=res.listing_id,
                    paid_currency=res.currency_paid,
                    listed_currency=str(listed_currency),
                )
            seller = ctx.guild.get_member(int(res.seller_id))
            await ctx.bot.bus.publish(
                "ah_sale_settled",
                guild=ctx.guild,
                user_id=int(res.seller_id),
                # Member object so handlers that prefer .display_name
                # work even when the cache miss.
                user=seller,
                listing_id=res.listing_id, kind=res.kind,
            )
        except Exception:
            log.debug(
                "auction settle bus publish failed", exc_info=True,
            )

    # ── ,ah inspect ────────────────────────────────────────────────────────

    @ah.command(name="inspect", aliases=["view-listing", "details"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_inspect(
        self, ctx: DiscoContext, ref: str,
    ) -> None:
        """Detailed view of one listing -- token id, full metadata,
        seller, expiry.

        Accepts either a numeric **listing id** (``,ah inspect 17``)
        or a **token id** (``,ah inspect bud:k889ka2c``); when given
        a token id with no active listing, falls through to the
        ``,ah token`` view so the same string always lands the player
        somewhere useful.
        """
        ref = (ref or "").strip()
        # Token-id route. Everything with a ``:`` is a token id; bare
        # numerics are listing ids.
        if ":" in ref:
            tok_row = await _items.get_token(ctx.db, ref.lower())
            if not tok_row:
                await ctx.reply_error(
                    f"No item with token id `{ref}`."
                )
                return
            listing_id = tok_row.get("listing_id")
            if not listing_id:
                # Item exists but isn't actively listed -- fall through
                # to the token-detail view so the inspect command DOES
                # something useful instead of a "no listing" dead end.
                await self.ah_token.callback(self, ctx, token_id=ref)
                return
            row = await auc.find_listing(ctx.db, int(listing_id))
            if not row:
                await ctx.reply_error(
                    f"Token `{ref}` claims listing #{listing_id} but "
                    f"that listing is missing -- ledger out of sync."
                )
                return
        else:
            try:
                listing_id_int = int(ref)
            except ValueError:
                await ctx.reply_error(
                    "Pass a listing id (`17`) or a token id "
                    "(`bud:k889ka2c`)."
                )
                return
            row = await auc.find_listing(ctx.db, listing_id_int)
            if not row:
                await ctx.reply_error(
                    f"Listing #{listing_id_int} not found."
                )
                return
        prefix = await ctx.get_guild_prefix()
        embed = _build_listing_inspect_embed(row, prefix)
        view = ListingActionView(
            ctx,
            listing_id=int(row["id"]),
            seller_user_id=int(row.get("seller_user_id") or 0),
            is_active=(str(row.get("status") or "") == "active"),
        )
        msg = await ctx.reply(
            embed=embed, view=view, mention_author=False,
        )
        view.message = msg

    # ── ,ah mine ───────────────────────────────────────────────────────────

    @ah.command(name="mine", aliases=["mylistings", "listings"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_mine(
        self, ctx: DiscoContext, status: str = "active",
    ) -> None:
        """Your listings, defaulting to active. Pass a status
        (``active`` / ``sold`` / ``cancelled`` / ``expired``).
        """
        st = (status or "active").strip().lower()
        if st not in ("active", "sold", "cancelled", "expired"):
            await ctx.reply_error(
                "Status must be one of: active / sold / cancelled / expired."
            )
            return
        rows = await auc.list_user_listings(
            ctx.db, ctx.guild_id, ctx.author.id, status=st,
        )
        if not rows:
            await ctx.reply_error(
                f"You have no {st} listings."
            )
            return
        lines = [_row_label(r) for r in rows[:25]]
        embed = (
            card(
                f"\U0001F3DB Your Listings  ·  {st.title()}  ({len(rows)})",
                color=C_GOLD,
                description="\n".join(lines),
            )
            .footer(
                f"`,ah inspect <id>` for full details, "
                f"`,ah cancel <id>` to pull an active one."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @ah.command(name="sold")
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_sold(self, ctx: DiscoContext) -> None:
        """Shorthand for ``,ah mine sold``."""
        await self.ah_mine.callback(self, ctx, "sold")

    # ── ,ah history ────────────────────────────────────────────────────────

    @ah.command(name="history", aliases=["log", "trades"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_history(self, ctx: DiscoContext) -> None:
        """Your settled trades on the auction house.

        Combines every listing where you were the seller (sold /
        cancelled / expired) AND every listing where you were the
        buyer (sold). Newest first, last 50.
        """
        rows = await auc.trade_history(
            ctx.db, ctx.guild_id, ctx.author.id, limit=50,
        )
        if not rows:
            await ctx.reply_error(
                "No settled trades yet. List something with `,ah list`."
            )
            return

        # Stat summary up top so the player can read totals at a glance.
        sold_n = sum(1 for r in rows if r["role"] == "sold")
        bought_n = sum(1 for r in rows if r["role"] == "bought")
        cancelled_n = sum(1 for r in rows if r["role"] == "cancelled")
        expired_n = sum(1 for r in rows if r["role"] == "expired")

        ROLE_GLYPH = {
            "sold":      "\U0001F4B0",   # money bag
            "bought":    "\U0001F6CD",   # shopping bag
            "cancelled": "\U000026D4",   # no-entry
            "expired":   "\U000023F0",   # alarm clock
        }

        prefix = await ctx.get_guild_prefix()
        page_lines: list[str] = []
        for r in rows:
            role = str(r["role"])
            md = auc._as_dict(r.get("metadata"))
            ref = str(md.get("ref") or "?")
            kind = str(r.get("kind") or "")
            qty = int(r.get("qty") or 1)
            price_h = to_human(int(
                r.get("sold_price_raw") or r.get("price_raw") or 0,
            ))
            cur = str(r["settled_currency"])
            ts_col = (
                r.get("settled_at")
                or r.get("cancelled_at")
                or r.get("listed_at")
            )
            glyph = ROLE_GLYPH.get(role, "\U0001F4DC")
            kemo = _KIND_EMOJI.get(kind, "")
            qty_part = f" x{qty}" if qty > 1 else ""
            page_lines.append(
                f"{glyph} `#{int(r['id']):>5}`  {kemo} **{ref}**{qty_part}"
                f"  ·  **{price_h:,.2f} {cur}**  ·  "
                f"{role}  ·  {fmt_ts(ts_col)}"
            )

        # Single embed, no pagination -- 50 rows fits inside the 6000-
        # char ceiling comfortably (each line ~100 chars).
        summary = (
            f"\U0001F4B0 sold **{sold_n}**  ·  "
            f"\U0001F6CD bought **{bought_n}**  ·  "
            f"\U000026D4 cancelled **{cancelled_n}**  ·  "
            f"\U000023F0 expired **{expired_n}**"
        )
        embed = (
            card(
                f"\U0001F4DC Auction History  ·  {ctx.author.display_name}",
                color=C_NAVY,
                description=summary + "\n\n" + "\n".join(page_lines),
            )
            .footer(
                f"Last {len(rows)} settled trades  ·  "
                f"{prefix}ah inspect <id> for full details"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,ah token ──────────────────────────────────────────────────────────

    @ah.command(name="token", aliases=["tok", "id", "tokenid"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ah_token(self, ctx: DiscoContext, *, token_id: str) -> None:
        """Inspect any item by its NFT-style token id.

        Works for items currently listed AND items that aren't (e.g.
        a buddy you bought 3 weeks ago that's been minted into the
        item_instances ledger). Format: ``<network>:<hex>``.
        """
        tok = (token_id or "").strip().lower()
        if not tok:
            await ctx.reply_error_hint(
                "Pass a token id, like `bud:k889ka2c`.",
                hint="ah token bud:k889ka2c",
                command_name="ah token",
            )
            return
        try:
            _items.parse_id(tok)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return

        row = await _items.get_token(ctx.db, tok)
        if not row:
            await ctx.reply_error(
                f"No item with token id `{tok}`. "
                f"Tokens are minted on first listing -- if it's never "
                f"been on the auction house, it doesn't have one yet."
            )
            return

        kind = str(row.get("kind") or "")
        net = str(row.get("network") or "")
        owner_uid = row.get("owner_user_id")
        listing_id = row.get("listing_id")
        md = auc._as_dict(row.get("metadata"))

        if listing_id:
            owner_label = "\U0001F3DB Escrowed by active listing"
        elif owner_uid:
            owner_label = f"<@{int(owner_uid)}>"
        else:
            owner_label = "_(unowned)_"

        kind_emoji = _KIND_EMOJI.get(kind, "\U0001F4E6")
        builder = card(
            f"{kind_emoji} Token  ·  `{tok}`",
            color=_KIND_COLOR.get(kind, C_GOLD),
            description=(
                f"**Kind:** {kind}  ·  **Network:** {net}\n"
                f"**Current owner:** {owner_label}"
            ),
        )

        # Kind-specific extras when we have them in metadata.
        try:
            from configs.buddies_config import gender_glyph as _gender_glyph
        except Exception:
            _gender_glyph = lambda g: ""  # type: ignore
        if kind == "buddy" and md:
            details = []
            if md.get("name"):
                details.append(f"**{md['name']}**")
            if md.get("species"):
                details.append(str(md["species"]).title())
            details.append(f"Lv. {int(md.get('level') or 1)}")
            try:
                from configs.buddies_config import rarity_meta as _b_rarity
                tier_v = md.get("rarity_tier")
                if tier_v is not None:
                    rt = int(tier_v)
                    tier_name = str(_b_rarity(rt).get("name") or f"Tier {rt}")
                    details.append(f"**{tier_name}** (T{rt})")
            except Exception:
                if md.get("rarity_tier") is not None:
                    details.append(f"Tier {int(md.get('rarity_tier'))}")
            buddy_glyph = _gender_glyph(md.get("gender"))
            if buddy_glyph:
                details.append(buddy_glyph)
            if md.get("wins") or md.get("losses"):
                details.append(
                    f"{int(md.get('wins') or 0)}W-"
                    f"{int(md.get('losses') or 0)}L"
                )
            builder = builder.field("Buddy", "  ·  ".join(details), False)
        elif kind == "fish" and md.get("entries"):
            weights = [
                f"{float(e.get('lbs') or 0):,.2f} lbs"
                for e in (md.get("entries") or [])[:5]
            ]
            builder = builder.field(
                f"{md.get('fish_key', 'Fish')}",
                " / ".join(weights), False,
            )
        elif kind == "egg" and md:
            # Eggs are genderless until they hatch, so no glyph here.
            try:
                from configs.buddies_config import rarity_meta as _b_rarity
                tier_v = md.get("rarity_tier")
                if tier_v is not None:
                    rt = int(tier_v)
                    tier_name = str(_b_rarity(rt).get("name") or f"Tier {rt}")
                    egg_line = (
                        f"**{tier_name}** "
                        f"{str(md.get('species') or '?').title()}"
                    )
                else:
                    egg_line = str(md.get("species") or "?").title()
            except Exception:
                egg_line = (
                    f"Tier {int(md.get('rarity_tier') or 1)} "
                    f"{str(md.get('species') or '?').title()}"
                )
            builder = builder.field("Egg", egg_line, False)

        builder = builder.field(
            "Source",
            f"`{row.get('source_table', '?')}` :: "
            f"`{row.get('source_id', '?')}`",
            False,
        )
        if listing_id:
            prefix = await ctx.get_guild_prefix()
            builder = builder.footer(
                f"Active listing #{int(listing_id)}  ·  "
                f"{prefix}ah inspect {int(listing_id)} for sale details"
            )
        await ctx.reply(embed=builder.build(), mention_author=False)

    # ── ,ah help ───────────────────────────────────────────────────────────

    @ah.command(name="help", aliases=["commands"])
    @guild_only
    @no_bots
    async def ah_help(self, ctx: DiscoContext) -> None:
        """Quick command reference for the auction house."""
        prefix = await ctx.get_guild_prefix()
        body = (
            f"**Browsing**\n"
            f"`{prefix}ah`  -  open the categorised browser (dropdown + buttons)\n"
            f"`{prefix}ah browse [kind]`  -  same browser, optionally pre-filtered\n"
            f"`{prefix}ah search <text>`  -  free-text find (name, species, token id)\n"
            f"`{prefix}ah inspect <id>`  -  full details for one listing\n"
            f"`{prefix}ah buy <id> [pay_currency]`\n\n"
            f"**Listing** -- three forms, all NFT-driven\n"
            f"`{prefix}ah list <buddy_id> <price>`\n"
            f"  e.g. `{prefix}ah list 1234 50000`\n"
            f"`{prefix}ah list <token_id> <price> [currency]`\n"
            f"  e.g. `{prefix}ah list bud:k889kak 50000` "
            f"(copy id from `{prefix}items inspect`)\n"
            f"`{prefix}ah list <name> <price> [currency]`\n"
            f"  e.g. `{prefix}ah list minnow 5`  -  resolves to your oldest "
            f"matching NFT.\n"
            f"`{prefix}ah cancel <id>`\n"
            f"`{prefix}ah mine [status]`  ·  `{prefix}ah sold`  ·  `{prefix}ah history`\n\n"
            f"**Default currency** per network: `bud` -> BUD, `lur` -> LURE, "
            f"`har` -> HRV, `cry` -> RUNE, `fge` -> INGOT. "
            f"Pass an explicit symbol to override.\n\n"
            f"**Cross-currency buys** auto-route through the AMM at "
            f"oracle minus impact (~1% slippage).\n"
            f"**Fee:** 5% of sale price burned as a sink.\n"
            f"**Expiry:** 7 days default; pass `--ttl=N` to override.\n"
            f"**Token IDs:** every listed item gets a stable "
            f"`<network>:<hex>` identifier visible on `inspect`."
        )
        embed = card(
            "\U0001F3DB Auction House Help",
            color=C_GOLD,
            description=body,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Auction(bot))