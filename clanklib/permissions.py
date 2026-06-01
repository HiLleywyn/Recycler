"""clanklib/permissions.py — the bot's permission model.

One place that defines:

  * which Discord permissions each feature actually needs (so the invite link
    requests the minimum, never blanket Administrator),
  * a cog base that gates every command in a feature to moderators and above,
  * a per-feature readiness audit the setup command renders.

Keeping this in one module means the invite, the gating, and the setup advice
can never disagree about what the bot requires.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import discord
from discord.ext import commands

from core.framework.cogs import GuildCog


# The minimum a moderator needs to run any management command. We gate on
# Manage Server because that is the natural "this person runs the server" bar;
# owners and administrators have it implicitly.
MOD_PERMISSION = "manage_guild"


@dataclass(frozen=True)
class FeaturePerm:
    """A feature and the bot permissions it needs to function."""

    key: str
    label: str
    bot_perms: tuple[str, ...]      # permissions the BOT needs
    note: str = ""


# What the bot needs, per feature. These drive both the invite scope and the
# setup audit. They are deliberately scoped: only backups/templates need the
# heavy role/channel management, and nothing needs Administrator.
FEATURES: tuple[FeaturePerm, ...] = (
    FeaturePerm(
        "core", "Core",
        ("view_channel", "send_messages", "embed_links", "read_message_history"),
        "Read and reply in channels.",
    ),
    FeaturePerm(
        "backups", "Backups and templates",
        ("manage_channels", "manage_roles"),
        "Recreate channels and roles when restoring a backup or template.",
    ),
    FeaturePerm(
        "chatlog_sync", "Chatlog and sync",
        ("manage_webhooks", "ban_members"),
        "Create webhooks to replay archived messages and mirror channels, and "
        "propagate bans between synced guilds.",
    ),
)


def required_bot_permissions() -> discord.Permissions:
    """The union of every feature's bot permissions — the exact set the invite
    link requests. No Administrator."""
    perms = discord.Permissions.none()
    for feat in FEATURES:
        for name in feat.bot_perms:
            setattr(perms, name, True)
    return perms


def invite_url(client_id: int | str) -> str:
    """An OAuth invite that asks for exactly the permissions the bot uses."""
    value = required_bot_permissions().value
    return (
        f"https://discord.com/oauth2/authorize?client_id={client_id}"
        f"&permissions={value}&scope=bot%20applications.commands"
    )


@dataclass
class PermAudit:
    feature: FeaturePerm
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def audit_permissions(me: discord.Member) -> list[PermAudit]:
    """Compare the bot's current guild permissions against every feature's
    needs. Administrator short-circuits everything to OK (Discord grants all)."""
    have = me.guild_permissions
    results: list[PermAudit] = []
    for feat in FEATURES:
        if have.administrator:
            results.append(PermAudit(feat, []))
            continue
        missing = [p for p in feat.bot_perms if not getattr(have, p, False)]
        results.append(PermAudit(feat, missing))
    return results


def pretty_perm(name: str) -> str:
    """Discord-style permission label, e.g. manage_roles -> Manage Roles."""
    return name.replace("_", " ").title()


class ModCog(GuildCog):
    """A guild-only cog whose every command requires the moderator permission.

    This gates *all* commands in the cog at once — including read-only listings
    — so the management surface is never exposed to ordinary members. Owners and
    administrators pass implicitly (they hold Manage Server).
    """

    async def cog_check(self, ctx: commands.Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        perms = ctx.author.guild_permissions
        if perms.administrator or getattr(perms, MOD_PERMISSION, False):
            return True
        raise commands.MissingPermissions([MOD_PERMISSION])
