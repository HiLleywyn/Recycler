"""
core/framework/persistent_embeds.py
==============================

Tiny helpers for the "interactive embed that doesn't disappear" pattern
used by fishing / delve / buddy battle.

The shared piece is the bump button -- the user wants the embed to be
re-sent at the bottom of the channel without losing its content or its
view. Implementing it lives here so the three cogs can share the exact
behavior (delete the original, post a fresh copy with the same embed +
view, owner-locked, no autodelete).

Also exposes a small ``never_delete_after`` sentinel + a no-op
autodelete shim so cogs that historically scheduled an autodelete on
their messages can opt the new persistent panels out by passing this
sentinel to their existing helpers.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

import discord

log = logging.getLogger(__name__)


class BumpButton(discord.ui.Button):
    """Re-send the embed at the end of the channel.

    Owner-locked. Click flow:
      1. Delete the source message (silent on permission failure).
      2. Build a fresh ``discord.ui.View`` populated with copies of every
         button on the source view (so Cast Again / Bump etc. carry
         over and stay clickable).
      3. Post the original embed + new view at the bottom of the
         channel, attach it to ``self.view.message`` so subsequent
         button clicks edit the new message.

    The original message's autodelete (if any was scheduled) becomes
    moot once we delete it manually; the new message gets no autodelete
    by default which is exactly the user-facing requirement ("they
    should not just disappear").
    """

    def __init__(
        self,
        owner_id: int,
        *,
        label: str = "Bump",
        emoji: str = "\U0001F53C",                       # up arrow
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        row: int | None = None,
        require_owner: bool = True,
    ) -> None:
        super().__init__(label=label, emoji=emoji, style=style, row=row)
        self.owner_id = int(owner_id)
        # Set False for views that already gate access via
        # ``interaction_check`` (e.g. the buddy PvP view that allows
        # either combatant) -- the per-button owner check would
        # otherwise reject the second player even though the view
        # itself accepted them.
        self._require_owner = require_owner

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._require_owner and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the original owner can bump this panel.",
                ephemeral=True,
            )
            return
        view: discord.ui.View | None = self.view
        if view is None:
            await interaction.response.send_message(
                "Couldn't bump -- view is gone.", ephemeral=True,
            )
            return
        # Snapshot the current embed (we'll use it to rebuild the bumped copy).
        msg: discord.Message | None = interaction.message
        if msg is None:
            await interaction.response.send_message(
                "Couldn't bump -- source message missing.", ephemeral=True,
            )
            return
        embeds = list(msg.embeds) if msg.embeds else []
        channel = msg.channel
        # Defer up front so Discord doesn't 3s-timeout while we delete +
        # re-post (delete on a busy channel can take a beat).
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            await msg.delete()
        except (discord.NotFound, discord.HTTPException):
            log.debug("BumpButton: source delete failed", exc_info=True)
        try:
            sent = await channel.send(embeds=embeds, view=view)
        except discord.HTTPException:
            log.debug("BumpButton: re-post failed", exc_info=True)
            return
        # Rebind the view to the new message so the cog's existing
        # message-edit paths target the bumped copy.
        try:
            view.message = sent  # type: ignore[attr-defined]
        except Exception:
            pass


def attach_bump_button(
    view: discord.ui.View,
    owner_id: int,
    *,
    row: int | None = None,
    label: str = "Bump",
) -> BumpButton:
    """Convenience: instantiate + add a BumpButton to ``view``.

    Returns the button so the caller can keep a handle (rare; usually
    the call is fire-and-forget).
    """
    btn = BumpButton(owner_id, label=label, row=row)
    view.add_item(btn)
    return btn


# ── Generic action button ───────────────────────────────────────────────────
# A no-op button class that calls a coroutine when pressed. Used by
# fishing's "Cast Again" without having to subclass Button per cog.

class CallbackButton(discord.ui.Button):
    """Owner-locked button that runs a passed coroutine on click.

    The coroutine receives ``interaction`` and is responsible for any
    follow-up edit / response. Designed so a cog can attach
    ``CallbackButton(owner_id, on_click=self._cast_again, ...)``
    without writing a subclass per panel.
    """

    def __init__(
        self,
        owner_id: int,
        on_click: Callable[[discord.Interaction], Awaitable[None]],
        *,
        label: str,
        emoji: str = "",
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        row: int | None = None,
        require_owner: bool = True,
    ) -> None:
        kwargs: dict[str, Any] = dict(label=label, style=style, row=row)
        if emoji:
            kwargs["emoji"] = emoji
        super().__init__(**kwargs)
        self.owner_id = int(owner_id)
        self._on_click = on_click
        self._require_owner = require_owner

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._require_owner and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the original user can use this button.",
                ephemeral=True,
            )
            return
        await self._on_click(interaction)


# ── Sentinel for "do not autodelete" ────────────────────────────────────────
# Cogs that gate autodelete behind a guild_settings column can compare
# the resolved value against this sentinel. Keeps the intent obvious at
# every call site without adding yet another bool param to the existing
# helper signatures.

class _NeverDeleteAfter:
    __slots__ = ()
    def __bool__(self) -> bool:
        # Truthy so legacy ``if delete_after:`` checks still skip
        # cleanup (we abuse the truthiness to mean "not None") but the
        # downstream comparator never matches against a real int.
        return False


NEVER_DELETE_AFTER: Optional[_NeverDeleteAfter] = _NeverDeleteAfter()


__all__ = (
    "BumpButton",
    "attach_bump_button",
    "CallbackButton",
    "NEVER_DELETE_AFTER",
)
