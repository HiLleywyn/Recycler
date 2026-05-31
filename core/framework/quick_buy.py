"""core/framework/quick_buy.py -- Reusable Quick Buy modal + button.

Every per-game shop (`,farm shop`, `,fish shop`, `,delve shop`,
`,buddy shop`) now exposes the same Quick Buy pattern that ``,shop``
uses: a button that pops a modal, the modal collects the item key,
and submission re-dispatches a synthetic ``,<shop> buy <item>``
message through the bot's command pipeline so every decorator runs
exactly as if the player had typed it.

The modal pre-fills + locks the "Pays in" field to the single currency
the host shop accepts (REEL, HRV, RUNE, BUD, ...). Submitting with a
different value gets rejected before the dispatch -- shops that only
spend one currency should never silently route a player into a
mismatched buy attempt.
"""
from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

import discord

from core.config import Config

if TYPE_CHECKING:
    from core.framework.context import DiscoContext

log = logging.getLogger(__name__)


# Discord caps: Modal title 45, TextInput label 45, placeholder 100.
_TITLE_MAX = 45
_LABEL_MAX = 45
_PLACEHOLDER_MAX = 100


class QuickBuyModal(discord.ui.Modal):
    """Generic single-currency Quick Buy modal.

    ``command_template`` must contain ``{item}`` -- the raw user input
    is substituted in and the result is sent through
    ``bot.process_commands`` exactly like the player typed it.
    The currency input is pre-filled with ``accepted_currency``;
    if the player edits it to anything else the submission is rejected.
    """

    def __init__(
        self,
        *,
        ctx: "DiscoContext",
        modal_title: str,
        command_template: str,
        accepted_currency: str,
        item_label: str,
        item_placeholder: str,
    ) -> None:
        super().__init__(title=modal_title[:_TITLE_MAX])
        if "{item}" not in command_template:
            raise ValueError("command_template must contain '{item}'")
        self._ctx = ctx
        self._command_template = command_template
        self._currency_sym = accepted_currency.upper()

        self.item_input = discord.ui.TextInput(
            label=item_label[:_LABEL_MAX],
            placeholder=item_placeholder[:_PLACEHOLDER_MAX],
            required=True,
            max_length=80,
        )
        self.add_item(self.item_input)

        self.currency_input = discord.ui.TextInput(
            label=f"Pays in {self._currency_sym} (locked)"[:_LABEL_MAX],
            placeholder=self._currency_sym,
            default=self._currency_sym,
            required=False,
            max_length=10,
        )
        self.add_item(self.currency_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        item = str(self.item_input.value or "").strip()
        cur = str(self.currency_input.value or "").strip().upper()
        if not item:
            await interaction.response.send_message(
                "Please enter what to buy.", ephemeral=True,
            )
            return
        if cur and cur != self._currency_sym:
            await interaction.response.send_message(
                f"This shop only accepts **{self._currency_sym}**. "
                f"Leave the **Pays in** field as `{self._currency_sym}`.",
                ephemeral=True,
            )
            return

        ctx = self._ctx
        prefix = ctx.prefix or Config.PREFIX
        full_command = f"{prefix}{self._command_template.format(item=item)}"

        # Acknowledge first so Discord doesn't fire the 3s timeout while
        # process_commands runs the host-side flow (which itself may
        # mount a ConfirmView and wait on the player). Ephemeral so it
        # doesn't clutter the channel.
        try:
            await interaction.response.send_message(
                f"\U0001F6D2 Running `{full_command}`...",
                ephemeral=True,
            )
        except discord.HTTPException:
            log.debug("quick buy: ack failed", exc_info=True)

        # Re-dispatch as a synthetic message so the FULL command pipeline
        # runs (decorators, cooldowns, ensure_registered, ConfirmView).
        try:
            new_msg = copy.copy(ctx.message)
            new_msg.content = full_command  # type: ignore[attr-defined]
            await ctx.bot.process_commands(new_msg)
        except Exception as e:
            log.exception("quick buy failed: %s", full_command)
            try:
                await interaction.followup.send(
                    f"Quick buy hit an error: `{type(e).__name__}: {e}`. "
                    f"Try `{full_command}` directly.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass


class QuickBuyButton(discord.ui.Button):
    """Drop-in Quick Buy button. Click opens a ``QuickBuyModal`` wired
    for the host shop. ``owner_id`` defaults to ``ctx.author.id``.
    """

    def __init__(
        self,
        *,
        ctx: "DiscoContext",
        command_template: str,
        accepted_currency: str,
        item_label: str,
        item_placeholder: str,
        modal_title: str | None = None,
        owner_id: int | None = None,
        row: int = 1,
    ) -> None:
        super().__init__(
            label="Quick Buy",
            emoji="\U0001F6D2",
            style=discord.ButtonStyle.success,
            row=row,
        )
        self._ctx = ctx
        self._command_template = command_template
        self._accepted_currency = accepted_currency.upper()
        self._item_label = item_label
        self._item_placeholder = item_placeholder
        self._owner_id = (
            int(owner_id) if owner_id is not None else int(ctx.author.id)
        )
        self._modal_title = (
            modal_title or f"Quick Buy ({self._accepted_currency})"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This isn't your shop.", ephemeral=True,
            )
            return
        try:
            await interaction.response.send_modal(QuickBuyModal(
                ctx=self._ctx,
                modal_title=self._modal_title,
                command_template=self._command_template,
                accepted_currency=self._accepted_currency,
                item_label=self._item_label,
                item_placeholder=self._item_placeholder,
            ))
        except Exception as e:
            log.exception("quick buy: send_modal failed")
            try:
                await interaction.response.send_message(
                    f"Couldn't open Quick Buy: `{type(e).__name__}: {e}`.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass


class QuickBuyView(discord.ui.View):
    """Owner-locked one-button view that wraps a shop embed which didn't
    previously have an interactive view. Use ``view.message = sent``
    after replying so ``on_timeout`` can disable the button cleanly.
    """

    def __init__(
        self,
        *,
        ctx: "DiscoContext",
        command_template: str,
        accepted_currency: str,
        item_label: str,
        item_placeholder: str,
        modal_title: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.owner_id = int(ctx.author.id)
        self.message: discord.Message | None = None
        self.add_item(QuickBuyButton(
            ctx=ctx,
            command_template=command_template,
            accepted_currency=accepted_currency,
            item_label=item_label,
            item_placeholder=item_placeholder,
            modal_title=modal_title,
            owner_id=self.owner_id,
            row=0,
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This isn't your shop. Run the command yourself to use it.",
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
