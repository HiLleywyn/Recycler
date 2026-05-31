"""Regenerate / try-harder view for AI chat replies.

Attached to the placeholder message that ``,ask`` / reply-to-bot writes
the final AI response into. Two buttons:

  * **Regenerate**: re-run the same prompt with the same temperature and
    model. Useful when the user wants a different angle without
    re-typing the question.
  * **Try harder**: re-run with the temperature bumped by
    ``Config.AI_REGEN_TRY_HARDER_TEMP_BUMP`` (default +0.35, capped at
    1.5). Cheap creativity dial.

Only the original author can click. Both buttons share a per-state
``asyncio.Lock`` so back-to-back clicks serialize through one
regeneration at a time. After the view's timeout (``Config.AI_REGEN_TTL_S``)
the buttons are disabled and the state entry is evicted from the cog's
in-memory registry.

The view never holds DB rows -- regen replays from the in-memory
``_AskState`` (system prompt + messages + model + backend) so it doesn't
matter if conversation history has rolled past since the original turn.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

import discord

from core.config import Config

if TYPE_CHECKING:  # pragma: no cover -- type-only imports
    from cogs.help import Help


log = logging.getLogger(__name__)


@dataclass
class _AskState:
    """Frozen snapshot of one chat turn, used to replay via Regenerate.

    Kept in-memory only; the registry is GC'd by the view's ``on_timeout``
    callback so we don't leak state for messages users never click.

    ``accumulated_reply`` holds the full assistant text produced so far
    across the original turn + any Continue clicks, so a subsequent
    Continue knows what's already been said. ``was_truncated`` is True
    if the LAST completion hit ``finish_reason="length"`` or the body
    was longer than Discord's 2000-char display cap -- the signal for
    enabling the Continue button in the view.

    ``responses`` accumulates every version of the reply (original +
    each regen/try-harder), newest last. ``current_page`` is the
    zero-based index of the version currently shown in the placeholder.
    ``sources_results`` stores the raw search result list so the nav
    redraw can recreate the Sources button without a DB round-trip.
    """
    user_id: int
    channel_id: int
    placeholder_id: int
    messages: list[dict]
    model: str | None
    backend: Literal["openrouter", "ollama"]
    temperature: float
    tool_schemas: list[dict] | None
    max_tokens: int
    timeout_s: float
    created_at: float
    accumulated_reply: str = ""
    was_truncated: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Response version history (original + each regen, newest last)
    responses: list[str] = field(default_factory=list)
    current_page: int = 0
    # Cached source results so nav redraws can rebuild the Sources button
    sources_results: list[dict] | None = None


def _format_response_page(text: str, page: int, total: int) -> str:
    """Prefix ``text`` with a page indicator when multiple versions exist.

    Uses Discord's ``-#`` subtext syntax so the indicator renders small and
    grey without competing with the actual response content.
    """
    if total <= 1:
        return text
    label = "original" if page == 0 else f"regenerated #{page}"
    return f"-# Response {page + 1} of {total} ({label})\n{text}"


class _AskReplyView(discord.ui.View):
    """Two-button view: Regenerate + Try harder.

    The cog must implement ``regenerate_ask(state, temperature)`` which
    replays the original messages through the chat pipeline and re-edits
    the placeholder. The view never reaches into the cog's private state
    directly -- it only calls that one entrypoint.
    """

    def __init__(
        self,
        state: _AskState,
        cog: "Help",
        *,
        timeout: float | None = None,
        extra_items: list[discord.ui.Item] | None = None,
    ) -> None:
        super().__init__(timeout=timeout or float(Config.AI_REGEN_TTL_S))
        self._state = state
        self._cog = cog
        # The Continue button is only useful when the previous reply was
        # truncated (model hit max_tokens, or text overflowed Discord's
        # 2000-char message cap). Hide it otherwise so users aren't
        # tempted to click it for replies that ended naturally.
        if not state.was_truncated:
            for item in list(self.children):
                if getattr(item, "custom_id", None) == "ask_continue":
                    self.remove_item(item)
                    break
        # Discord caps a View at 25 items; we ship 3 (Regenerate, Try
        # harder, Continue) so there's plenty of room to fold in the
        # Sources button (or anything else the caller wants visible).
        if extra_items:
            for item in extra_items:
                try:
                    self.add_item(item)
                except Exception:  # pragma: no cover -- defensive
                    log.debug("[ask-view] failed to add extra item", exc_info=True)

        # Navigation buttons: appear on row 1 only when multiple versions exist.
        # ◀  [1 / 3]  ▶ -- lets the user flip through original and each regen.
        if len(state.responses) > 1:
            at_first = (state.current_page == 0)
            at_last  = (state.current_page >= len(state.responses) - 1)

            prev_btn = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.secondary,
                custom_id="ask_nav_prev",
                disabled=at_first,
                row=1,
            )
            prev_btn.callback = self._nav_prev
            self.add_item(prev_btn)

            counter_btn = discord.ui.Button(
                label=f"{state.current_page + 1} / {len(state.responses)}",
                style=discord.ButtonStyle.secondary,
                custom_id="ask_nav_counter",
                disabled=True,
                row=1,
            )
            self.add_item(counter_btn)

            next_btn = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.secondary,
                custom_id="ask_nav_next",
                disabled=at_last,
                row=1,
            )
            next_btn.callback = self._nav_next
            self.add_item(next_btn)

    @discord.ui.button(
        label="Regenerate",
        # U+1F504 "🔄" anticlockwise-arrows-button -- standard RGI emoji.
        # Was U+21BB "↻" which is a Unicode SYMBOL but NOT in the RGI
        # emoji set; Discord rejects it on /messages PATCH with
        # ``400 Bad Request: components.0.components.0.emoji.name:
        # Invalid emoji``, and the rejection cascaded -- the renderer's
        # full-edit failed, content-only fallback succeeded but with no
        # view, so the spinner stuck and the reply never landed buttons.
        emoji="\U0001F504",
        style=discord.ButtonStyle.secondary,
        custom_id="ask_regenerate",
    )
    async def regenerate(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Replay the same prompt at the same temperature."""
        await self._regen_common(interaction, temperature=self._state.temperature)

    @discord.ui.button(
        label="Try harder",
        emoji="✨",  # ✨
        style=discord.ButtonStyle.secondary,
        custom_id="ask_try_harder",
    )
    async def try_harder(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Replay with a higher-temperature run for a more creative answer."""
        bump = float(getattr(Config, "AI_REGEN_TRY_HARDER_TEMP_BUMP", 0.35) or 0.0)
        new_temp = min(1.5, self._state.temperature + bump)
        await self._regen_common(interaction, temperature=new_temp)

    @discord.ui.button(
        label="Continue",
        # U+25B6 + U+FE0F variation selector for emoji presentation.
        # Bare U+25B6 is a Unicode geometric shape and some Discord
        # validation paths reject it; the VS16-followed form is the
        # canonical "play button" emoji.
        emoji="▶️",
        style=discord.ButtonStyle.primary,
        custom_id="ask_continue",
    )
    async def continue_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Ask the model to pick up where it left off.

        Only exposed when the previous reply was truncated (model hit
        ``finish_reason="length"`` or the text overflowed Discord's
        2000-char display cap). On click, calls the cog's
        ``continue_ask`` which sends a follow-up message containing the
        rest of the reply -- the original message stays put as the
        head of the thread.
        """
        # Author check first.
        if interaction.user.id != self._state.user_id:
            try:
                await interaction.response.send_message(
                    "Only the original asker can continue this.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return
        if self._state.lock.locked():
            try:
                await interaction.response.send_message(
                    "A continue is already running -- give it a sec.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        # Disable our OWN Continue button while the follow-up runs so
        # rapid double-clicks don't queue duplicates.
        button.disabled = True
        try:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
            async with self._state.lock:
                try:
                    await self._cog.continue_ask(
                        state=self._state, interaction=interaction,
                    )
                except Exception:
                    log.exception(
                        "[ask-view] continue failed for user=%s",
                        self._state.user_id,
                    )
                    try:
                        await interaction.followup.send(
                            "Continue failed. Try again in a moment.",
                            ephemeral=True,
                        )
                    except discord.HTTPException:
                        pass
        finally:
            # Re-enable for any additional continues IF the new reply
            # was ALSO truncated; the cog's continue_ask handles that
            # by attaching a fresh view with its own Continue button.
            # We leave our own disabled because this message's reply
            # is "frozen" -- new content lives on the follow-up.
            pass

    # ── shared regen path ─────────────────────────────────────────────────

    async def _regen_common(
        self,
        interaction: discord.Interaction,
        *,
        temperature: float,
    ) -> None:
        # Author check first. Don't leak the original content to anyone else.
        if interaction.user.id != self._state.user_id:
            try:
                await interaction.response.send_message(
                    "Only the original asker can regenerate this.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return

        # Serialize racing clicks through one regen at a time.
        if self._state.lock.locked():
            try:
                await interaction.response.send_message(
                    "A regenerate is already running -- give it a sec.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return

        # Defer immediately so Discord doesn't drop the interaction while
        # the new chat run takes 2-10s.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

        async with self._state.lock:
            try:
                await self._cog.regenerate_ask(
                    state=self._state, temperature=temperature, interaction=interaction,
                )
            except Exception:
                log.exception("[ask-view] regenerate failed for user=%s", self._state.user_id)
                try:
                    await interaction.followup.send(
                        "Regenerate failed. Try again in a moment.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass

    # ── response history navigation ───────────────────────────────────────

    async def _nav_prev(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._state.user_id:
            await interaction.response.send_message(
                "Not your conversation.", ephemeral=True
            )
            return
        if self._state.current_page <= 0:
            await interaction.response.defer()
            return
        self._state.current_page -= 1
        await self._apply_nav(interaction)

    async def _nav_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._state.user_id:
            await interaction.response.send_message(
                "Not your conversation.", ephemeral=True
            )
            return
        if self._state.current_page >= len(self._state.responses) - 1:
            await interaction.response.defer()
            return
        self._state.current_page += 1
        await self._apply_nav(interaction)

    async def _apply_nav(self, interaction: discord.Interaction) -> None:
        """Edit the placeholder to show the currently selected response version."""
        state = self._state
        page = state.current_page
        content = _format_response_page(
            state.responses[page], page, len(state.responses)
        )
        # Delegate view construction back to the cog so the Sources button
        # can be rebuilt without a circular import into help.py.
        new_view = self._cog.build_view_for_state(state)
        try:
            await interaction.response.edit_message(content=content, view=new_view)
            if interaction.message:
                self._cog._ask_view_messages[state.placeholder_id] = interaction.message
        except discord.HTTPException as exc:
            log.debug("[ask-view] nav edit failed: %s", exc)

    async def on_timeout(self) -> None:  # pragma: no cover -- discord callback
        """Disable buttons after the view expires and evict the state entry."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        # Best-effort edit so the user sees the disabled state.
        try:
            cog = self._cog
            msg = cog._ask_view_messages.get(self._state.placeholder_id)
            if msg is not None:
                await msg.edit(view=self)
        except Exception:
            pass
        finally:
            self._cog._ask_states.pop(self._state.placeholder_id, None)
            self._cog._ask_view_messages.pop(self._state.placeholder_id, None)


def evict_stale_ask_states(
    states: dict[int, _AskState],
    *,
    now: float | None = None,
    max_age_s: float | None = None,
) -> int:
    """Drop ``_AskState`` entries older than ``max_age_s`` from ``states``.

    Returns the number of entries pruned. Called periodically from the
    Help cog's background loop so the registry doesn't grow without
    bound on a long-running bot.
    """
    cutoff = (now or time.monotonic()) - float(max_age_s or Config.AI_REGEN_TTL_S)
    stale = [pid for pid, s in states.items() if s.created_at < cutoff]
    for pid in stale:
        states.pop(pid, None)
    return len(stale)
