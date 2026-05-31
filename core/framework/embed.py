"""
core/framework/embed.py  -  Embed DSL for Discoin.

``card()`` is the canonical, fluent entry point for all embed creation.
It replaces raw ``discord.Embed(...)`` calls across all cogs.

Usage::

    from core.framework.embed import card

    embed = card("💰 Wallet").description("Your balances").color(C_INFO).build()

    embed = (
        card("⛏️ Mining")
        .color(C_AMBER)
        .field("Hashrate", "1,250 H/s", inline=True)
        .field("Rewards", "42.0 SUN", inline=True)
        .footer("Updated just now")
        .build()
    )

    # Keyword shorthand (mirrors discord.Embed signature):
    embed = card("Title", description="Desc", color=C_INFO).field("K", "V").build()
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    pass


class CardBuilder:
    """
    Fluent embed card builder.

    All setter methods return ``self`` so calls can be chained.
    Call ``.build()`` at the end to get the underlying ``discord.Embed``.
    """

    __slots__ = ("_embed",)

    def __init__(
        self,
        title: str = "",
        *,
        description: str | None = None,
        color: int | None = None,
    ) -> None:
        self._embed = discord.Embed(
            title=title or None,
            description=description,
            color=color,
        )

    # ── Content ────────────────────────────────────────────────────────────

    def description(self, text: str) -> "CardBuilder":
        """Set the embed description."""
        self._embed.description = text
        return self

    def color(self, value: int) -> "CardBuilder":
        """Set the embed accent color."""
        self._embed.color = discord.Colour(value)
        return self

    def url(self, value: str) -> "CardBuilder":
        """Set the title hyperlink URL."""
        self._embed.url = value
        return self

    # ── Fields ─────────────────────────────────────────────────────────────

    def field(self, name: str, value: str, inline: bool = False) -> "CardBuilder":
        """Add a field. ``inline=True`` puts it in the same row as adjacent fields."""
        self._embed.add_field(name=name, value=value, inline=inline)
        return self

    def field_if(
        self, condition: bool, name: str, value: str, inline: bool = False
    ) -> "CardBuilder":
        """Add a field only when ``condition`` is truthy. Keeps chains clean."""
        if condition:
            self._embed.add_field(name=name, value=value, inline=inline)
        return self

    def blank(self, inline: bool = False) -> "CardBuilder":
        """Add a blank (zero-width space) field  -  useful for layout spacing."""
        self._embed.add_field(name="\u200b", value="\u200b", inline=inline)
        return self

    # ── Metadata ───────────────────────────────────────────────────────────

    def footer(self, text: str, icon_url: str | None = None) -> "CardBuilder":
        """Set the embed footer."""
        self._embed.set_footer(text=text, icon_url=icon_url)
        return self

    def author(
        self,
        name: str,
        icon_url: str | None = None,
        url: str | None = None,
    ) -> "CardBuilder":
        """Set the embed author row."""
        kwargs: dict = {"name": name}
        if icon_url:
            kwargs["icon_url"] = icon_url
        if url:
            kwargs["url"] = url
        self._embed.set_author(**kwargs)
        return self

    def thumbnail(self, url: str) -> "CardBuilder":
        """Set the thumbnail image (top-right corner)."""
        if url:
            self._embed.set_thumbnail(url=url)
        return self

    def image(self, url: str) -> "CardBuilder":
        """Set the large image (bottom of embed)."""
        if url:
            self._embed.set_image(url=url)
        return self

    def timestamp(self, dt: datetime.datetime | None = None) -> "CardBuilder":
        """Add a timestamp. Defaults to the current UTC time."""
        self._embed.timestamp = dt or datetime.datetime.utcnow()
        return self

    # ── Build ──────────────────────────────────────────────────────────────

    def build(self) -> discord.Embed:
        """Return the underlying ``discord.Embed``."""
        return self._embed


# ── Public API ─────────────────────────────────────────────────────────────────

def card(
    title: str = "",
    *,
    description: str | None = None,
    color: int | None = None,
) -> CardBuilder:
    """
    Create a fluent embed card builder.

    :param title:       Embed title text.
    :param description: Optional description text (shorthand kwarg).
    :param color:       Optional accent color integer (shorthand kwarg).

    Example::

        from core.framework.embed import card
        from core.framework.ui import C_INFO

        embed = (
            card("My Title", description="Some body", color=C_INFO)
            .field("Key", "Value", inline=True)
            .footer("Tip: use /help")
            .build()
        )
    """
    return CardBuilder(title, description=description, color=color)
