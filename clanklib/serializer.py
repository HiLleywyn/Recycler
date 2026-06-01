"""clanklib/serializer.py -- turn a Discord guild into JSON and back.

This is the engine behind backups and templates. It captures a guild's
*structure* (settings, roles, categories, channels and their permission
overwrites) and, for backups, optionally a window of recent messages per
text channel. Restoring rebuilds that structure into a target guild,
remapping old role/channel ids to the freshly created ones.

Design notes
------------
* Serialization is pure data: every function returns plain JSON-able dicts,
  so a backup is just a row in Postgres.
* Restoration is deliberately conservative about what it deletes and is
  always driven by an explicit :class:`RestoreOptions`.
* ``@everyone`` is captured/applied as permissions only (never recreated),
  managed/integration roles are skipped on restore, and the bot never tries
  to move roles above its own top role (Discord forbids it).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import discord

log = logging.getLogger(__name__)

# Schema version for stored payloads, so future loaders can migrate old data.
SCHEMA_VERSION = 1

# Sensible caps so a backup payload stays well under Postgres/practical limits.
DEFAULT_MESSAGE_LIMIT = 50
MAX_MESSAGE_LIMIT = 250


# ── Serialization ─────────────────────────────────────────────────────────────

def _overwrites_to_list(channel: discord.abc.GuildChannel) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        out.append({
            "id": target.id,
            "type": "role" if isinstance(target, discord.Role) else "member",
            "name": getattr(target, "name", str(target.id)),
            "allow": allow.value,
            "deny": deny.value,
        })
    return out


def _serialize_role(role: discord.Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "permissions": role.permissions.value,
        "color": role.colour.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "position": role.position,
        "managed": role.managed,
        "is_default": role.is_default(),
    }


def _serialize_channel(channel: discord.abc.GuildChannel) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type.value if hasattr(channel.type, "value") else int(channel.type),
        "position": channel.position,
        "category_id": channel.category_id,
        "overwrites": _overwrites_to_list(channel),
    }
    if isinstance(channel, discord.TextChannel):
        base.update({
            "topic": channel.topic,
            "nsfw": channel.is_nsfw(),
            "slowmode_delay": channel.slowmode_delay,
            "news": channel.is_news(),
        })
    elif isinstance(channel, discord.VoiceChannel):
        base.update({
            "bitrate": channel.bitrate,
            "user_limit": channel.user_limit,
            "rtc_region": str(channel.rtc_region) if channel.rtc_region else None,
        })
    elif isinstance(channel, discord.StageChannel):
        base.update({"bitrate": channel.bitrate, "user_limit": channel.user_limit})
    elif isinstance(channel, discord.ForumChannel):
        base.update({"topic": channel.topic, "nsfw": channel.is_nsfw()})
    return base


async def _serialize_messages(channel: discord.TextChannel, limit: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    try:
        async for m in channel.history(limit=limit, oldest_first=False):
            if not (m.content or m.attachments or m.embeds):
                continue
            msgs.append({
                "author_name": m.author.display_name,
                "author_id": m.author.id,
                "avatar_url": str(m.author.display_avatar.url) if m.author.display_avatar else None,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "pinned": m.pinned,
                "attachments": [a.url for a in m.attachments],
                "embeds": [e.to_dict() for e in m.embeds][:1],
            })
    except discord.Forbidden:
        log.debug("no history access for #%s", channel.name)
    msgs.reverse()  # store oldest-first for replay
    return msgs


def serialize_settings(guild: discord.Guild) -> dict[str, Any]:
    return {
        "name": guild.name,
        "icon_url": str(guild.icon.url) if guild.icon else None,
        "banner_url": str(guild.banner.url) if guild.banner else None,
        "verification_level": int(guild.verification_level.value),
        "default_notifications": int(guild.default_notifications.value),
        "explicit_content_filter": int(guild.explicit_content_filter.value),
        "afk_timeout": guild.afk_timeout,
        "afk_channel_id": guild.afk_channel.id if guild.afk_channel else None,
        "system_channel_id": guild.system_channel.id if guild.system_channel else None,
        "preferred_locale": str(guild.preferred_locale),
    }


async def serialize_guild(
    guild: discord.Guild,
    *,
    include_messages: bool = False,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
) -> dict[str, Any]:
    """Capture a full guild snapshot as a JSON-able dict."""
    message_limit = max(0, min(int(message_limit), MAX_MESSAGE_LIMIT))
    roles = [_serialize_role(r) for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)]
    categories = [
        {"id": c.id, "name": c.name, "position": c.position, "overwrites": _overwrites_to_list(c)}
        for c in sorted(guild.categories, key=lambda c: c.position)
    ]
    channels = [
        _serialize_channel(ch)
        for ch in sorted(guild.channels, key=lambda c: (c.position, c.id))
        if not isinstance(ch, discord.CategoryChannel)
    ]

    message_count = 0
    if include_messages and message_limit:
        for ch in channels:
            real = guild.get_channel(ch["id"])
            if isinstance(real, discord.TextChannel):
                ch["messages"] = await _serialize_messages(real, message_limit)
                message_count += len(ch["messages"])

    return {
        "schema": SCHEMA_VERSION,
        "settings": serialize_settings(guild),
        "roles": roles,
        "categories": categories,
        "channels": channels,
        "message_count": message_count,
        "source_guild_id": guild.id,
    }


def serialize_template(guild: discord.Guild) -> dict[str, Any]:
    """Structure-only snapshot: settings, roles, categories, channels.

    Member-specific permission overwrites are dropped (a template is portable
    across servers, so per-member rules make no sense).
    """
    data = {
        "schema": SCHEMA_VERSION,
        "settings": {"name": guild.name, "verification_level": int(guild.verification_level.value)},
        "roles": [_serialize_role(r) for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)],
        "categories": [
            {"id": c.id, "name": c.name, "position": c.position,
             "overwrites": [o for o in _overwrites_to_list(c) if o["type"] == "role"]}
            for c in sorted(guild.categories, key=lambda c: c.position)
        ],
        "channels": [],
    }
    for ch in sorted(guild.channels, key=lambda c: (c.position, c.id)):
        if isinstance(ch, discord.CategoryChannel):
            continue
        sc = _serialize_channel(ch)
        sc["overwrites"] = [o for o in sc["overwrites"] if o["type"] == "role"]
        data["channels"].append(sc)
    return data


# ── Restoration ───────────────────────────────────────────────────────────────

@dataclass
class RestoreOptions:
    delete_roles: bool = True
    delete_channels: bool = True
    restore_roles: bool = True
    restore_channels: bool = True
    restore_settings: bool = True
    restore_messages: bool = True
    reason: str = "Recycler restore"


@dataclass
class RestoreStats:
    roles_created: int = 0
    roles_deleted: int = 0
    channels_created: int = 0
    channels_deleted: int = 0
    messages_sent: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.roles_created} roles, {self.channels_created} channels"
            + (f", {self.messages_sent} messages" if self.messages_sent else "")
            + (f"  -  {len(self.errors)} error(s)" if self.errors else "")
        )


def _overwrites_from_list(
    raw: list[dict[str, Any]], role_map: dict[int, discord.Role], guild: discord.Guild
) -> dict[Any, discord.PermissionOverwrite]:
    out: dict[Any, discord.PermissionOverwrite] = {}
    for o in raw:
        if o["type"] == "role":
            target = role_map.get(o["id"])
        else:
            target = guild.get_member(o["id"])
        if target is None:
            continue
        ow = discord.PermissionOverwrite.from_pair(
            discord.Permissions(int(o["allow"])), discord.Permissions(int(o["deny"]))
        )
        out[target] = ow
    return out


async def restore_guild(
    guild: discord.Guild,
    data: dict[str, Any],
    options: RestoreOptions,
    *,
    me: Optional[discord.Member] = None,
) -> RestoreStats:
    """Rebuild ``data`` into ``guild``. Returns what changed.

    The bot must have Administrator (or the relevant manage perms) and a top
    role high enough to manage the roles/channels it creates.
    """
    stats = RestoreStats()
    me = me or guild.me
    my_top = me.top_role.position if me else 0

    # 1. Delete existing channels / roles (best-effort, never @everyone or
    #    managed/integration roles, never above the bot's top role).
    if options.delete_channels:
        for ch in list(guild.channels):
            try:
                await ch.delete(reason=options.reason)
                stats.channels_deleted += 1
            except discord.HTTPException as exc:
                stats.errors.append(f"delete #{ch.name}: {exc}")
    if options.delete_roles:
        for role in list(guild.roles):
            if role.is_default() or role.managed or role.position >= my_top:
                continue
            try:
                await role.delete(reason=options.reason)
                stats.roles_deleted += 1
            except discord.HTTPException as exc:
                stats.errors.append(f"delete @{role.name}: {exc}")

    # 2. Roles (highest first so positions land correctly). Map old id -> new.
    role_map: dict[int, discord.Role] = {}
    if options.restore_roles:
        for r in data.get("roles", []):
            if r.get("is_default"):
                # @everyone: apply permissions only.
                try:
                    await guild.default_role.edit(
                        permissions=discord.Permissions(int(r["permissions"])),
                        reason=options.reason,
                    )
                    role_map[r["id"]] = guild.default_role
                except discord.HTTPException as exc:
                    stats.errors.append(f"@everyone perms: {exc}")
                continue
            if r.get("managed"):
                continue
            try:
                new_role = await guild.create_role(
                    name=r["name"],
                    permissions=discord.Permissions(int(r["permissions"])),
                    colour=discord.Colour(int(r["color"])),
                    hoist=bool(r["hoist"]),
                    mentionable=bool(r["mentionable"]),
                    reason=options.reason,
                )
                role_map[r["id"]] = new_role
                stats.roles_created += 1
            except discord.HTTPException as exc:
                stats.errors.append(f"create @{r.get('name')}: {exc}")

    # 3. Categories, then their channels, applying overwrites via role_map.
    if options.restore_channels:
        cat_map: dict[int, discord.CategoryChannel] = {}
        for c in sorted(data.get("categories", []), key=lambda c: c.get("position", 0)):
            try:
                overwrites = _overwrites_from_list(c.get("overwrites", []), role_map, guild)
                new_cat = await guild.create_category(
                    name=c["name"], overwrites=overwrites, reason=options.reason
                )
                cat_map[c["id"]] = new_cat
                stats.channels_created += 1
            except discord.HTTPException as exc:
                stats.errors.append(f"create category {c.get('name')}: {exc}")

        for ch in sorted(data.get("channels", []), key=lambda c: c.get("position", 0)):
            try:
                new_ch = await _create_channel(guild, ch, role_map, cat_map, options)
                if new_ch is not None:
                    stats.channels_created += 1
                    if options.restore_messages and ch.get("messages"):
                        sent = await _replay_messages(new_ch, ch["messages"])
                        stats.messages_sent += sent
            except discord.HTTPException as exc:
                stats.errors.append(f"create #{ch.get('name')}: {exc}")

    # 4. Guild settings.
    if options.restore_settings:
        await _restore_settings(guild, data.get("settings", {}), stats, options.reason)

    return stats


async def _create_channel(
    guild: discord.Guild,
    ch: dict[str, Any],
    role_map: dict[int, discord.Role],
    cat_map: dict[int, discord.CategoryChannel],
    options: RestoreOptions,
) -> Optional[discord.abc.GuildChannel]:
    overwrites = _overwrites_from_list(ch.get("overwrites", []), role_map, guild)
    category = cat_map.get(ch.get("category_id")) if ch.get("category_id") else None
    ctype = ch.get("type")
    common = {"overwrites": overwrites, "category": category, "reason": options.reason}

    # 2 = voice, 13 = stage, 15 = forum; everything else -> text.
    if ctype == 2:
        return await guild.create_voice_channel(
            ch["name"], bitrate=min(ch.get("bitrate", 64000), guild.bitrate_limit),
            user_limit=ch.get("user_limit", 0), **common,
        )
    if ctype == 13:
        return await guild.create_stage_channel(ch["name"], **common)
    if ctype == 15 and hasattr(guild, "create_forum"):
        return await guild.create_forum(ch["name"], topic=ch.get("topic"), **common)
    return await guild.create_text_channel(
        ch["name"], topic=ch.get("topic"), nsfw=ch.get("nsfw", False),
        slowmode_delay=ch.get("slowmode_delay", 0), **common,
    )


async def _replay_messages(channel: discord.abc.GuildChannel, messages: list[dict[str, Any]]) -> int:
    """Replay archived messages into ``channel`` via a temporary webhook."""
    if not isinstance(channel, discord.TextChannel):
        return 0
    sent = 0
    try:
        webhook = await channel.create_webhook(name="Recycler Restore")
    except discord.HTTPException:
        return 0
    try:
        for m in messages:
            content = m.get("content") or ""
            if not content and not m.get("attachments"):
                continue
            if m.get("attachments"):
                content = (content + "\n" + "\n".join(m["attachments"])).strip()
            try:
                await webhook.send(
                    content=content[:2000] or "​",
                    username=(m.get("author_name") or "Unknown")[:80],
                    avatar_url=m.get("avatar_url"),
                    allowed_mentions=discord.AllowedMentions.none(),
                    wait=False,
                )
                sent += 1
            except discord.HTTPException:
                continue
    finally:
        try:
            await webhook.delete()
        except discord.HTTPException:
            pass
    return sent


async def _restore_settings(
    guild: discord.Guild, s: dict[str, Any], stats: RestoreStats, reason: str
) -> None:
    edits: dict[str, Any] = {}
    if s.get("name"):
        edits["name"] = s["name"]
    if "verification_level" in s:
        edits["verification_level"] = discord.VerificationLevel(int(s["verification_level"]))
    if "explicit_content_filter" in s:
        edits["explicit_content_filter"] = discord.ContentFilter(int(s["explicit_content_filter"]))
    if "afk_timeout" in s and s["afk_timeout"]:
        edits["afk_timeout"] = int(s["afk_timeout"])
    if edits:
        try:
            await guild.edit(reason=reason, **edits)
        except discord.HTTPException as exc:
            stats.errors.append(f"settings: {exc}")
