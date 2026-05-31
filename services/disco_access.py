"""services/disco_access.py -- access gate for the ,disco command group.

The ,disco group's nested commands are unlocked for a member when ANY of:
  * they are server staff (owner / administrator / Manage Server / Manage
    Messages),
  * they currently boost the server (Discord Nitro boost),
  * they have reached chat level 50 in the server.

Everyone can still run the bare ,disco command to see the help page -- the
gate only covers the nested commands.

Read-only: this never mutates state. Used by cogs/disco.py to gate the
command surface and by cogs/help.py to decide whether Disco answers a member
inline or via a thread.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Chat level that unlocks the ,disco group without boosting.
DISCO_LEVEL_UNLOCK = 50


@dataclass(frozen=True)
class DiscoAccess:
    """Snapshot of why a member can (or cannot) use the ,disco group."""

    unlocked: bool
    reason: str        # "staff" | "boost" | "level" | "locked"
    is_staff: bool
    is_booster: bool
    level: int

    @property
    def label(self) -> str:
        """Short human-readable status for the ,disco help page."""
        if self.reason == "staff":
            return "Unlocked -- server staff"
        if self.reason == "boost":
            return "Unlocked -- server booster"
        if self.reason == "level":
            return f"Unlocked -- reached level {self.level}"
        return "Locked"


def _is_staff(member, guild) -> bool:
    """True when the member is the owner or holds a staff-level permission."""
    if member is None or guild is None:
        return False
    if getattr(guild, "owner_id", None) == getattr(member, "id", None):
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(
        perms.administrator or perms.manage_guild or perms.manage_messages
    )


async def _fetch_level(db, guild_id: int, user_id: int) -> int:
    """Read-only chat level lookup (never inserts a row)."""
    try:
        row = await db.fetch_one(
            "SELECT level FROM chat_levels WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
    except Exception:
        return 0
    if not row:
        return 0
    try:
        return int(row.get("level") or 0)
    except (TypeError, ValueError):
        return 0


async def get_disco_access(member, guild, db) -> DiscoAccess:
    """Resolve whether *member* may use the nested ,disco commands."""
    is_staff = _is_staff(member, guild)
    is_booster = bool(getattr(member, "premium_since", None))
    level = await _fetch_level(db, getattr(guild, "id", 0), getattr(member, "id", 0))

    if is_staff:
        return DiscoAccess(True, "staff", True, is_booster, level)
    if is_booster:
        return DiscoAccess(True, "boost", False, True, level)
    if level >= DISCO_LEVEL_UNLOCK:
        return DiscoAccess(True, "level", False, False, level)
    return DiscoAccess(False, "locked", False, False, level)
