from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from constants.ui import C_ERROR, C_WARNING, C_SUCCESS, C_NAVY, C_GOLD
from core.framework.embed import card

if TYPE_CHECKING:
    from database import Database

class _SilentMessage:
    """No-op stub returned by DiscoContext when the message is a chain-step replay.

    Prevents individual command embeds from appearing while a chain is running.
    Implements the discord.Message surface that commands commonly call after sending.
    """

    id: int = 0
    channel = None

    async def edit(self, *args, **kwargs) -> "_SilentMessage":
        return self

    async def delete(self, *args, **kwargs) -> None:
        pass

    async def add_reaction(self, *args, **kwargs) -> None:
        pass

    async def remove_reaction(self, *args, **kwargs) -> None:
        pass

    async def pin(self, *args, **kwargs) -> None:
        pass

    def __bool__(self) -> bool:
        return False


class DiscoContext(commands.Context):
    """Custom context injected by Discoin.get_context(). Adds .db and embed helpers."""

    # Injected by Discoin.get_context()
    db: "Database"
    # Set by the ensure_registered middleware decorator
    user_row: dict

    @property
    def guild_id(self) -> int:
        return self.guild.id  # safe: guild_only check runs first

    async def get_guild_prefix(self) -> str:
        """Return the guild's configured prefix, falling back to Config.PREFIX."""
        if self.guild:
            try:
                s = await self.bot.db.get_guild_settings(self.guild.id)
                p = s.get("prefix")
                if p:
                    return p
            except Exception as exc:
                import logging as _log
                _log.getLogger("discoin.context").debug(
                    "get_guild_prefix: DB lookup failed for guild %s (%s), using default.",
                    self.guild.id, exc,
                )
        from core.config import Config
        return Config.PREFIX

    # ── Basic helpers (backwards-compatible) ───────────────────────────────

    async def reply_error(self, message: str, **kwargs) -> discord.Message:
        embed = card(description=f"❌  {message}", color=C_ERROR).build()
        return await self.reply(embed=embed, mention_author=False, **kwargs)

    async def reply_error_action(
        self,
        message: str,
        button_label: str,
        command: str,
        *,
        rerun_original: bool = False,
    ) -> discord.Message:
        """Reply with an error embed plus a primary action button and Cancel.

        If *rerun_original* is True, after running *command* the view will also
        re-process the original message the user sent (so e.g. after creating a
        wallet the command that required the wallet runs automatically).
        """
        from core.framework.utils import ActionSuggestionView
        embed = card(description=f"❌  {message}", color=C_ERROR).build()
        followup: str | None = None
        if rerun_original and self.message:
            raw = self.message.content
            prefix = await self.get_guild_prefix()
            if raw.startswith(prefix):
                followup = raw[len(prefix):]
        view = ActionSuggestionView(self, button_label, command, followup=followup)
        return await self.reply(embed=embed, view=view, mention_author=False)

    async def reply_cooldown(self, seconds: float, **kwargs) -> discord.Message:
        """Reply with a non-error cooldown notice (amber, not red)."""
        embed = card(
            description=f"⏳  Try again in **{seconds:.0f}s**.",
            color=C_WARNING,
        ).build()
        return await self.reply(embed=embed, mention_author=False, delete_after=min(seconds + 1, 10), **kwargs)

    async def reply_error_hint(
        self,
        message: str,
        hint: str = "",
        command_name: str = "",
        **kwargs,
    ) -> discord.Message:
        """Reply with an error embed plus a hint and optional Report/Help buttons."""
        from core.framework.utils import ErrorHintView
        desc = f"❌  {message}"
        if hint:
            desc += f"\n\n💡 **Try:** `{hint}`"
        embed = card(description=desc, color=C_ERROR).build()
        view = ErrorHintView(self, command_name=command_name)
        return await self.reply(embed=embed, view=view, mention_author=False, **kwargs)

    async def reply_success(self, message: str, title: str = "", **kwargs) -> discord.Message:
        # Mirror reply_error's leading glyph so success / failure read symmetrically.
        embed = card(title=title, description=f"✅  {message}", color=C_SUCCESS).build()
        return await self.reply(embed=embed, mention_author=False, **kwargs)

    async def reply_premium_required(
        self,
        feature_key: str,
        **kwargs,
    ) -> discord.Message:
        """Standard locked-feature reply.

        Tells the user the feature is paid, what it covers, and how the
        server admin can subscribe. The same embed is shown regardless of
        whether the gate fired from a per-command @premium_required or a
        cog-level PremiumCog check, so users see one consistent message.
        """
        from services.entitlements import PREMIUM_FEATURES
        prefix = await self.get_guild_prefix()
        feature_label = PREMIUM_FEATURES.get(feature_key, feature_key)
        is_admin = bool(getattr(getattr(self.author, "guild_permissions", None),
                                "manage_guild", False))
        if is_admin:
            cta = (
                f"Run `{prefix}premium info` to see plans and subscribe via PayPal."
            )
        else:
            cta = (
                f"Ask a server admin to run `{prefix}premium info` to subscribe."
            )
        embed = (
            card(
                title="🔒 Premium Feature",
                description=(
                    f"**{feature_label}** is part of Discoin Premium and isn't "
                    f"unlocked on this server.\n\n{cta}"
                ),
                color=C_GOLD,
            )
            .field(
                "Free everywhere",
                "Trading, gambling, bank, profile, basic buddy management",
                False,
            )
            .field(
                "Premium only",
                "AI, fishing, crafting, delves, expeditions, buddy battles/breeding/market",
                False,
            )
            .footer(f"{prefix}premium status -- view this server's status")
            .build()
        )
        return await self.reply(embed=embed, mention_author=False, **kwargs)

    # ── UI helpers ─────────────────────────────────────────────────────────

    async def confirm(self, prompt: str, timeout: float = 30.0) -> bool:
        """Send a yes/no confirmation and return True if user confirmed."""
        from core.framework.ui import ConfirmView
        embed = card(title="⚠️", description=prompt, color=C_WARNING).build()
        view = ConfirmView(self.author.id, timeout=timeout)
        msg = await self.reply(embed=embed, view=view, mention_author=False)
        result = await view.wait_result()
        return bool(result)

    async def paginate(self, pages: list[discord.Embed], timeout: float = 120.0) -> None:
        """Send one embed or launch a multi-page paginator."""
        from core.framework.ui import Paginator
        await Paginator.send(self, pages, timeout=timeout)

    async def send_embed(self, embed: discord.Embed) -> discord.Message:
        """Reply with an embed."""
        return await self.reply(embed=embed, mention_author=False)

    async def send_group_help(self, group: commands.Group, *, title: str = "", color: int = C_NAVY) -> discord.Message:
        """Send a rich help embed for a command group, listing all subcommands."""
        prefix = await self.get_guild_prefix()
        name = group.qualified_name
        title = title or f"📖 {name.title()} Commands"

        lines: list[str] = []
        for cmd in sorted(group.commands, key=lambda c: c.name):
            if cmd.hidden:
                continue
            brief = cmd.short_doc or cmd.help or "No description"
            if isinstance(cmd, commands.Group):
                sub_count = len([c for c in cmd.commands if not c.hidden])
                lines.append(f"`{prefix}{cmd.qualified_name}`  -  {brief} ({sub_count} subcommands)")
            else:
                lines.append(f"`{prefix}{cmd.qualified_name}`  -  {brief}")

        embed = (
            card(
                title=title,
                description="\n".join(lines) if lines else "No commands available.",
                color=color,
            )
            .footer(f"Use {prefix}{name} <subcommand> for details")
            .build()
        )
        return await self.reply(embed=embed, mention_author=False)

    async def _process_embed_if_present(self, kwargs: dict) -> None:
        """If kwargs contains an Embed, run it through LinkManager."""
        embed = kwargs.get("embed")
        if embed and isinstance(embed, discord.Embed):
            try:
                from core.framework.links import LinkManager

                lm = LinkManager()
                processed, _ = lm.process_embed(embed)
                kwargs["embed"] = processed
            except Exception:
                # best-effort: if processing fails, fall back to original embed
                pass

    @property
    def is_chain_step(self) -> bool:
        """True when this context was created by a chain command replay."""
        return bool(getattr(self.message, "_chain_step", False))

    @staticmethod
    def _is_persistent_message(kwargs: dict) -> bool:
        """Return True if this message must NOT be auto-deleted.

        Interactive embeds (anything with a ``view=...``) are stateful
        UIs the player keeps clicking on -- delve room views, fish cast
        views, buddy panels, the today / start tabbed panel, AH browser,
        battle live-tick views, etc. Auto-deleting them mid-play breaks
        every button click after the timer fires.

        Files / attachments are likewise treated as persistent so chart
        screenshots and ASCII frames don't disappear out from under the
        player.

        Callers can also force an opt-out with ``no_autodelete=True`` for
        the rare case of a static result embed that should outlive the
        guild's reply_delete_after window (e.g. capture confirmations
        or trade receipts the player wants to keep).
        """
        if kwargs.pop("no_autodelete", False):
            return True
        if kwargs.get("view") is not None:
            return True
        # Embeds + files alone are NOT auto-classified as persistent --
        # short text-only embeds (success / error / cooldown notices)
        # SHOULD respect the guild's reply_delete_after so the game cogs
        # don't keep flooding the channel after a series of cooldowns.
        return False

    async def reply(self, *args, **kwargs):
        """Override reply to preprocess any embed before sending.
        Falls back to send() if the original message was deleted (e.g. auto-delete enabled).
        Silently drops the response when running inside a chain step replay."""
        if self.is_chain_step:
            return _SilentMessage()
        await self._process_embed_if_present(kwargs)
        persistent = self._is_persistent_message(kwargs)
        if self.guild and "delete_after" not in kwargs and not persistent:
            try:
                s = await self.bot.db.get_guild_settings(self.guild.id)
                qual = getattr(self.command, "qualified_name", "") if self.command else ""
                _AI_QUAL = {"ask", "disco image"}
                if qual in _AI_QUAL:
                    d = int(s.get("ai_reply_delete_after", 0) or 0)
                else:
                    d = int(s.get("reply_delete_after", 0) or 0)
                if d > 0:
                    kwargs["delete_after"] = float(d)
            except Exception:
                pass
        try:
            return await super().reply(*args, **kwargs)
        except discord.HTTPException as e:
            # 50035 = Invalid Form Body / Unknown message  -  original was deleted
            if e.code == 50035 or "Unknown message" in str(e):
                kwargs.pop("mention_author", None)
                return await self.send(*args, **kwargs)
            # 40062 / 429 = rate limited  -  don't fall back to send(), let caller handle
            raise

    async def send(self, *args, **kwargs):
        """Override send to preprocess any embed before sending.
        Silently drops the response when running inside a chain step replay."""
        if self.is_chain_step:
            return _SilentMessage()
        await self._process_embed_if_present(kwargs)
        persistent = self._is_persistent_message(kwargs)
        if self.guild and "delete_after" not in kwargs and not persistent:
            try:
                s = await self.bot.db.get_guild_settings(self.guild.id)
                qual = getattr(self.command, "qualified_name", "") if self.command else ""
                _AI_QUAL = {"ask", "disco image"}
                if qual in _AI_QUAL:
                    d = int(s.get("ai_reply_delete_after", 0) or 0)
                else:
                    d = int(s.get("reply_delete_after", 0) or 0)
                if d > 0:
                    kwargs["delete_after"] = float(d)
            except Exception:
                pass
        return await super().send(*args, **kwargs)
