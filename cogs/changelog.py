"""Player-facing changelog viewer + optional daily auto-post.

Reads the project's CHANGELOG.md (shipped inside the bot container) and
renders one date's entries as a Discord embed. Supports:

  ,changelog                today's entries (UTC) or the most recent date
  ,changelog 042426         entries for 04/24/2026 (MMDDYY)
  ,changelog 04242026       entries for 04/24/2026 (MMDDYYYY)
  ,changelog 04-24-2026     same, separators are ignored

The file is parsed once and cached by mtime so a redeploy automatically
picks up new entries without restarting the cog.

Auto-post: if a guild sets a changelog channel via
  ,admin changelog channel #channel
the bot posts the latest changelog entry to that channel once per day,
only when there is a new date that hasn't been posted yet.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from core.framework.ui import C_INFO

log = logging.getLogger(__name__)

# How often to check whether any guild's changelog needs posting.
_AUTO_POST_CHECK_INTERVAL_S: int = 3600

# CHANGELOG.md sits at the repo root (one level above cogs/).
_CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# Date headers in the file look like:  ## [main] -- 2026-04-25
# Per the project style rule (CLAUDE.md: no em / en / Unicode-minus dashes
# in source files), CHANGELOG.md uses a double ASCII hyphen ``--`` as the
# visual separator. Older entries used an em dash (U+2014) before the rule
# was tightened, so we accept both forms here. The fancy dashes are built
# from chr() so this source file stays pure ASCII.
_DASH_CHARS = "[" + chr(0x2014) + chr(0x2013) + r"\-" + "]"
_DASH_SEP = "(?:" + _DASH_CHARS + r"+|--)"
_DATE_HEADER = re.compile(
    r"^##\s*\[[^\]]+\]\s*" + _DASH_SEP + r"\s*(\d{4}-\d{2}-\d{2})\s*$"
)
_SUBSECTION = re.compile(r"^###\s+(.+?)\s*$")
_ENTRY_TITLE = re.compile(r"^-\s+\*\*(.+?)\*\*")

_SECTION_EMOJI = {
    "Bug Fixes":    "🐛",
    "New Features": "✨",
    "Maintenance":  "🔧",
    "Refactoring":  "♻️",
    "Discord Bot":  "🤖",
    "Performance":  "⚡",
    "Security":     "🔒",
    "Docs":         "📚",
    "Tests":        "🧪",
}

# Discord limits we have to respect: 1024 chars per field value, 6000 chars
# per embed, 25 fields per embed. We chunk well under all three.
_FIELD_BODY_SOFT_CAP = 1000
_FIELDS_PER_PAGE     = 6

# In-process parse cache, keyed by file mtime so redeploys auto-refresh.
_cache: dict[str, dict[str, list[str]]] | None = None
_cache_mtime: float | None = None


def _load_changelog() -> dict[str, dict[str, list[str]]]:
    """Parse CHANGELOG.md into ``{iso_date: {section: [title, ...]}}``."""
    global _cache, _cache_mtime
    try:
        mtime = _CHANGELOG_PATH.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _cache is not None and _cache_mtime == mtime:
        return _cache

    parsed: dict[str, dict[str, list[str]]] = {}
    cur_date: str | None = None
    cur_sect: str | None = None

    with _CHANGELOG_PATH.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            m = _DATE_HEADER.match(line)
            if m:
                cur_date = m.group(1)
                cur_sect = None
                parsed.setdefault(cur_date, {})
                continue
            m = _SUBSECTION.match(line)
            if m and cur_date is not None:
                cur_sect = m.group(1).strip()
                parsed[cur_date].setdefault(cur_sect, [])
                continue
            if cur_date and cur_sect:
                em = _ENTRY_TITLE.match(line)
                if em:
                    parsed[cur_date][cur_sect].append(em.group(1).strip())

    _cache = parsed
    _cache_mtime = mtime
    return parsed


def _parse_date_arg(arg: str) -> date | None:
    """Parse ``MMDDYY`` or ``MMDDYYYY`` (separators allowed) into a date."""
    digits = re.sub(r"[^0-9]", "", arg)
    if len(digits) == 6:
        fmt = "%m%d%y"
    elif len(digits) == 8:
        fmt = "%m%d%Y"
    else:
        return None
    try:
        return datetime.strptime(digits, fmt).date()
    except ValueError:
        return None


def _section_emoji(name: str) -> str:
    return _SECTION_EMOJI.get(name, "📌")


def _build_pages(target: date, sections: dict[str, list[str]], note: str = "") -> list[discord.Embed]:
    """Render one date's sections into one or more embeds, chunking long
    section bodies so no single field value exceeds Discord's 1024-char cap."""
    title = f"📝 Changelog {target.strftime('%m/%d/%Y')}"
    total_entries = sum(len(v) for v in sections.values())
    desc = f"**{total_entries}** change{'s' if total_entries != 1 else ''}"
    if note:
        desc += f"\n{note}"

    fields: list[tuple[str, str]] = []
    for sect_name, titles in sections.items():
        emoji = _section_emoji(sect_name)
        bullets = [f"- {t}" for t in titles]
        chunk: list[str] = []
        chunk_len = 0
        part = 0
        for b in bullets:
            line_len = len(b) + 1
            if chunk and chunk_len + line_len > _FIELD_BODY_SOFT_CAP:
                part += 1
                label = f"{emoji} {sect_name}" + (" (cont.)" if part > 1 else "")
                fields.append((label, "\n".join(chunk)))
                chunk = []
                chunk_len = 0
            chunk.append(b)
            chunk_len += line_len
        if chunk:
            part += 1
            label = f"{emoji} {sect_name}" + (" (cont.)" if part > 1 else "")
            fields.append((label, "\n".join(chunk)))

    if not fields:
        return [card(title, description="No entries.", color=C_INFO).build()]

    pages: list[discord.Embed] = []
    for i in range(0, len(fields), _FIELDS_PER_PAGE):
        chunk = fields[i : i + _FIELDS_PER_PAGE]
        b = card(title, description=desc, color=C_INFO)
        for name, value in chunk:
            b = b.field(name, value, inline=False)
        if len(fields) > _FIELDS_PER_PAGE:
            page_no = (i // _FIELDS_PER_PAGE) + 1
            page_total = (len(fields) + _FIELDS_PER_PAGE - 1) // _FIELDS_PER_PAGE
            b = b.footer(f"Page {page_no}/{page_total}")
        pages.append(b.build())
    return pages


class Changelog(commands.Cog):
    """Player-facing CHANGELOG.md viewer with optional daily auto-post."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._auto_post_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        if self._auto_post_task is None or self._auto_post_task.done():
            self._auto_post_task = asyncio.create_task(self._auto_post_loop())

    async def cog_unload(self) -> None:
        if self._auto_post_task and not self._auto_post_task.done():
            self._auto_post_task.cancel()

    async def _auto_post_loop(self) -> None:
        """Hourly loop: post latest changelog to guilds that configured a
        changelog channel, but only when the latest date is new."""
        await self.bot.wait_until_ready()
        while True:
            try:
                await asyncio.sleep(_AUTO_POST_CHECK_INTERVAL_S)
                await self._run_auto_post()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("changelog auto-post loop: unhandled error")

    async def _run_auto_post(self) -> None:
        parsed = _load_changelog()
        if not parsed:
            return
        available = sorted(parsed.keys(), reverse=True)
        if not available:
            return
        latest_iso = available[0]
        latest_sections = parsed[latest_iso]
        if not latest_sections:
            return
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT guild_id, changelog_channel, changelog_last_posted "
                "FROM guild_settings "
                "WHERE changelog_channel IS NOT NULL",
            )
        except Exception:
            log.debug("changelog auto-post: DB fetch failed", exc_info=True)
            return
        for row in (rows or []):
            try:
                await self._maybe_post_to_guild(
                    int(row["guild_id"]),
                    int(row["changelog_channel"]),
                    str(row.get("changelog_last_posted") or ""),
                    latest_iso,
                    latest_sections,
                )
            except Exception:
                log.debug(
                    "changelog auto-post: guild %s failed",
                    row.get("guild_id"), exc_info=True,
                )

    async def _maybe_post_to_guild(
        self,
        guild_id: int,
        channel_id: int,
        last_posted: str,
        latest_iso: str,
        sections: dict,
    ) -> None:
        if last_posted == latest_iso:
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        target = datetime.strptime(latest_iso, "%Y-%m-%d").date()
        pages = _build_pages(target, sections)
        for embed in pages:
            await channel.send(embed=embed)
        await self.bot.db.execute(
            "UPDATE guild_settings SET changelog_last_posted=$1 WHERE guild_id=$2",
            latest_iso, guild_id,
        )
        log.info(
            "changelog auto-post: posted %s to guild %s channel %s",
            latest_iso, guild_id, channel_id,
        )

    @commands.command(name="changelog", aliases=["changes", "whatsnew"])
    @guild_only
    async def changelog_cmd(self, ctx: DiscoContext, *, date_arg: str | None = None) -> None:
        """Show today's changelog, or pass MMDDYY / MMDDYYYY for a specific day."""
        parsed = _load_changelog()
        if not parsed:
            await ctx.reply_error("Changelog file not found on this deploy.")
            return

        note = ""
        if date_arg:
            target = _parse_date_arg(date_arg)
            if target is None:
                await ctx.reply_error_hint(
                    "Could not parse that date.",
                    hint=f"{ctx.prefix}changelog 042426",
                    command_name="changelog",
                )
                return
        else:
            target = datetime.now(timezone.utc).date()

        sections = parsed.get(target.isoformat())

        # No entry for the requested date: fall back to the most recent one
        # and tell the user which day they actually got.
        if not sections:
            available = sorted(parsed.keys(), reverse=True)
            if not available:
                await ctx.reply_error("Changelog is empty.")
                return
            fallback_iso = available[0]
            fallback = datetime.strptime(fallback_iso, "%Y-%m-%d").date()
            sections = parsed[fallback_iso]
            note = (
                f"No entries for **{target.strftime('%m/%d/%Y')}**, "
                f"showing most recent: **{fallback.strftime('%m/%d/%Y')}**."
            )
            target = fallback

        pages = _build_pages(target, sections, note=note)
        if len(pages) == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
        else:
            await ctx.paginate(pages)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Changelog(bot))
