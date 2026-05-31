"""cogs/twofa.py  -  Discord commands for managing two-factor authentication.

Commands:
  .2fa           -  check 2FA status
  .2fa setup     -  set up TOTP via DM
  .2fa disable   -  disable 2FA via DM
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.fuzzy import suggest_subcommand
from core.framework.totp import generate_secret, verify_totp, otpauth_uri
from core.framework.ui import C_INFO, C_SUCCESS, C_ERROR, C_WARNING


class TwoFA(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ── helpers ────────────────────────────────────────────────────────────

    async def _get_2fa_row(self, user_id: int):
        try:
            return await self.bot.db.fetch_one(
                "SELECT totp_secret, enabled FROM user_2fa WHERE user_id = $1",
                user_id,
            )
        except Exception:
            return None

    def _wait_for_dm_code(self, author: discord.User, timeout: float = 120):
        """Return a check + wait coroutine for a 6-digit DM reply."""
        def check(m: discord.Message):
            return (
                m.author.id == author.id
                and isinstance(m.channel, discord.DMChannel)
                and m.content.strip().isdigit()
                and len(m.content.strip()) == 6
            )
        return self.bot.wait_for("message", check=check, timeout=timeout)

    # ── .2fa (status) ─────────────────────────────────────────────────────

    @commands.hybrid_group(name="2fa", fallback="status", with_app_command=False)
    async def tfa(self, ctx: DiscoContext) -> None:
        """Check your two-factor authentication status."""
        if await suggest_subcommand(ctx, self.tfa):
            return
        row = await self._get_2fa_row(ctx.author.id)
        enabled = bool(row and row["enabled"])

        prefix = await ctx.get_guild_prefix()
        if enabled:
            embed = (
                card("🔐 Two-Factor Authentication", color=C_SUCCESS)
                .description(
                    "2FA is **enabled** on your account. You'll be prompted for "
                    "a code when logging in to the dashboard."
                )
                .footer(f"Use {prefix}2fa disable to remove 2FA")
                .build()
            )
        else:
            embed = (
                card("🔐 Two-Factor Authentication", color=C_INFO)
                .description(
                    "2FA is **not enabled**. Add an extra layer of security "
                    "to your dashboard login."
                )
                .footer(f"Use {prefix}2fa setup to enable")
                .build()
            )

        await ctx.reply(embed=embed, mention_author=False)

    # ── .2fa setup ─────────────────────────────────────────────────────────

    @tfa.command(name="setup")
    async def tfa_setup(self, ctx: DiscoContext) -> None:
        """Set up two-factor authentication (secret sent via DM)."""
        user_id = ctx.author.id
        row = await self._get_2fa_row(user_id)

        if row and row["enabled"]:
            await ctx.reply_error(
                "Two-factor authentication is already active on your account. "
                "Use `.2fa disable` to remove it first."
            )
            return

        secret = generate_secret()
        uri = otpauth_uri(secret, ctx.author.name)
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={quote(uri)}"

        # Save secret (not yet enabled)
        await self.bot.db.execute(
            "INSERT INTO user_2fa (user_id, totp_secret, enabled) VALUES ($1, $2, FALSE) "
            "ON CONFLICT (user_id, guild_id) DO UPDATE SET totp_secret = $2, enabled = FALSE",
            user_id, secret,
        )

        # Format secret in groups of 4
        formatted = " ".join(secret[i:i+4] for i in range(0, len(secret), 4))

        dm_embed = (
            card("🔐 2FA Setup", color=C_INFO)
            .description(
                "Scan the QR code below with your authenticator app "
                "(Google Authenticator, Authy, 1Password, etc.).\n\n"
                f"**Manual key:** `{formatted}`\n\n"
                "Reply to this DM with the **6-digit code** from your app to complete setup."
            )
            .footer("Code expires after 120 seconds")
            .build()
        )
        dm_embed.set_image(url=qr_url)

        try:
            await ctx.author.send(embed=dm_embed)
        except discord.Forbidden:
            await ctx.reply_error(
                "I couldn't send you a DM. Please enable DMs from server members and try again."
            )
            return

        # Confirm in channel
        if ctx.guild:
            await ctx.reply(
                embed=card("🔐 2FA Setup", color=C_INFO)
                .description("Check your DMs for the setup instructions.")
                .build(),
                mention_author=False,
            )

        # Wait for code in DM
        try:
            msg = await self._wait_for_dm_code(ctx.author, timeout=120)
        except asyncio.TimeoutError:
            await ctx.author.send(
                embed=card("⏳ Setup Timed Out", color=C_WARNING)
                .description("You didn't enter a code in time. Run `.2fa setup` again to restart.")
                .build()
            )
            return

        code = msg.content.strip()
        if not verify_totp(secret, code):
            await ctx.author.send(
                embed=card("❌ Invalid Code", color=C_ERROR)
                .description("That code was incorrect. Run `.2fa setup` again to restart.")
                .build()
            )
            return

        # Enable
        await self.bot.db.execute(
            "UPDATE user_2fa SET enabled = TRUE WHERE user_id = $1", user_id,
        )
        await ctx.author.send(
            embed=card("✅ 2FA Enabled", color=C_SUCCESS)
            .description(
                "Two-factor authentication is now active. You'll be asked "
                "for a code when logging in to the dashboard."
            )
            .build()
        )

    # ── .2fa disable ───────────────────────────────────────────────────────

    @tfa.command(name="disable")
    async def tfa_disable(self, ctx: DiscoContext) -> None:
        """Disable two-factor authentication (verified via DM)."""
        user_id = ctx.author.id
        row = await self._get_2fa_row(user_id)

        if not row or not row["enabled"]:
            await ctx.reply_error("Two-factor authentication is not enabled on your account.")
            return

        dm_embed = (
            card("🔓 Disable 2FA", color=C_WARNING)
            .description("Reply with your current **6-digit authenticator code** to disable 2FA.")
            .footer("Code expires after 120 seconds")
            .build()
        )

        try:
            await ctx.author.send(embed=dm_embed)
        except discord.Forbidden:
            await ctx.reply_error(
                "I couldn't send you a DM. Please enable DMs from server members and try again."
            )
            return

        if ctx.guild:
            await ctx.reply(
                embed=card("🔓 Disable 2FA", color=C_INFO)
                .description("Check your DMs to confirm.")
                .build(),
                mention_author=False,
            )

        try:
            msg = await self._wait_for_dm_code(ctx.author, timeout=120)
        except asyncio.TimeoutError:
            await ctx.author.send(
                embed=card("⏳ Timed Out", color=C_WARNING)
                .description("You didn't enter a code in time. Run `.2fa disable` again to retry.")
                .build()
            )
            return

        code = msg.content.strip()
        if not verify_totp(row["totp_secret"], code):
            await ctx.author.send(
                embed=card("❌ Invalid Code", color=C_ERROR)
                .description("That code was incorrect. Run `.2fa disable` again to retry.")
                .build()
            )
            return

        await self.bot.db.execute(
            "DELETE FROM user_2fa WHERE user_id = $1", user_id,
        )
        await ctx.author.send(
            embed=card("✅ 2FA Disabled", color=C_SUCCESS)
            .description("Two-factor authentication has been removed from your account.")
            .build()
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(TwoFA(bot))
