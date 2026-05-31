"""Shared utility functions used across multiple cogs."""
from __future__ import annotations

import copy

import discord

from core.framework.amount_parser import translate_emoji_amount


class ActionSuggestionView(discord.ui.View):
    """Error embed helper: shows a primary action button and a Cancel button.

    When the action button is clicked, the suggested *command* (without prefix)
    is processed as if the original author typed it.  Only the original author
    can interact with the view.
    """

    def __init__(
        self,
        ctx,
        label: str,
        command: str,
        *,
        followup: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._ctx = ctx
        self._command = command
        self._followup = followup

        action_btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        action_btn.callback = self._action_callback
        self.add_item(action_btn)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self._cancel_callback
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._ctx.author.id:
            await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
            return False
        return True

    async def _action_callback(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        prefix = self._ctx.prefix or "."
        new_msg = copy.copy(self._ctx.message)
        object.__setattr__(new_msg, "content", f"{prefix}{self._command}")
        await self._ctx.bot.process_commands(new_msg)
        if self._followup:
            followup_msg = copy.copy(self._ctx.message)
            object.__setattr__(followup_msg, "content", f"{prefix}{self._followup}")
            # Mark as a system-initiated re-run so the cooldown handler skips it.
            object.__setattr__(followup_msg, "_bypass_cooldown", True)
            await self._ctx.bot.process_commands(followup_msg)
        self.stop()

    async def _cancel_callback(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        self.stop()


class ErrorHintView(discord.ui.View):
    """Error embed with Report Bug and Show Help buttons."""

    def __init__(self, ctx, *, command_name: str = "", timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self._ctx = ctx
        self._command_name = command_name

        report_btn = discord.ui.Button(label="Report Bug", style=discord.ButtonStyle.danger, emoji="🐛")
        report_btn.callback = self._report_callback
        self.add_item(report_btn)

        if command_name:
            help_btn = discord.ui.Button(label="Help", style=discord.ButtonStyle.secondary, emoji="❓")
            help_btn.callback = self._help_callback
            self.add_item(help_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._ctx.author.id:
            await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
            return False
        return True

    async def _report_callback(self, interaction: discord.Interaction) -> None:
        prefix = self._ctx.prefix or "."
        cmd_text = self._ctx.message.content if self._ctx.message else "unknown"
        hint = (
            f"You can report this bug with:\n"
            f"`{prefix}report bugs {cmd_text} - gave an error`"
        )
        await interaction.response.send_message(hint, ephemeral=True)

    async def _help_callback(self, interaction: discord.Interaction) -> None:
        prefix = self._ctx.prefix or "."
        # Route to help for the command's parent group or the command itself
        cmd = self._command_name.split()[0] if self._command_name else ""
        new_msg = copy.copy(self._ctx.message)
        object.__setattr__(new_msg, "content", f"{prefix}help {cmd}")
        await interaction.response.defer()
        await self._ctx.bot.process_commands(new_msg)

    async def on_timeout(self) -> None:
        self.stop()


def guild_currency_name(settings: dict) -> str:
    return settings.get("currency_name") or "USD"


def parse_amount(raw: str) -> tuple[float, bool]:
    """Parse a user-provided amount string.

    Handles ``$`` prefix (USD mode), comma separators, and numeric emojis
    (e.g. ``💯`` -> 100, ``1️⃣0️⃣0️⃣`` -> 100).
    Returns ``(value, is_usd_mode)``.  Raises ``ValueError`` if not numeric.
    """
    s = translate_emoji_amount(raw.strip()).replace(",", "")
    usd_mode = s.startswith("$")
    if usd_mode:
        s = s[1:]
    return float(s), usd_mode


def parse_sym_amt(arg1: str, arg2: str) -> tuple[str, str]:
    """Accept both 'SYMBOL amount' and 'amount SYMBOL' argument orders.

    Returns (symbol_upper, amount_str).  The amount_str is returned as-is
    (the caller is responsible for converting to float and validating).
    """
    # "all" counts as an amount, not a symbol
    if arg1.lower() == "all":
        return arg2.upper(), arg1
    if arg2.lower() == "all":
        return arg1.upper(), arg2
    # Strip $ for numeric detection, but return original string
    _a1 = arg1.lstrip("$").replace(",", "")
    try:
        float(_a1)
        # arg1 is numeric → order is: amount SYMBOL
        return arg2.upper(), arg1
    except ValueError:
        # arg1 is non-numeric → order is: SYMBOL amount
        return arg1.upper(), arg2
