"""
services/chat_leveling_csv.py  -  CSV parser + bulk importer for chat leveling.

Accepts the common column layouts used by MEE6, Arcane, Tatsu, and generic
exports.  The importer prefers total-XP over level: if a total-XP column is
present and positive, it is written verbatim so progression is preserved at
the same granularity the source bot used.  Otherwise we fall back to the
level column and compute the total-XP floor for that level under the guild's
configured curve.
"""
from __future__ import annotations

import contextlib
import csv
import io
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from services.chat_leveling import (
    LevelingConfig,
    set_user_level,
    set_user_streak,
    set_user_xp,
)

if TYPE_CHECKING:
    from database.database import PgDatabase

log = logging.getLogger(__name__)

# Column alias tables -- lowercase keys only, matched case-insensitively.
_USER_ID_KEYS = {"user_id", "userid", "id", "discord_id", "user", "snowflake"}
_LEVEL_KEYS   = {"level", "lvl"}
_XP_KEYS      = {"xp", "total_xp", "totalxp", "experience", "exp"}
_MSG_KEYS     = {"messages", "msgs", "message_count", "total_messages"}
_STREAK_KEYS      = {"streak_days", "streak", "daily_streak", "streak_count"}
_LAST_ACTIVE_KEYS = {"last_active_date", "last_active", "last_activity", "last_active_at"}
_NAME_KEYS = {"username", "name", "display_name", "displayname", "nickname", "user_name"}


@dataclass
class ImportResult:
    total_rows: int = 0
    imported: int = 0
    skipped: int = 0
    errored: int = 0
    first_errors: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errored += 1
        if len(self.first_errors) < 5:
            self.first_errors.append(msg)


def _decode(csv_bytes: bytes) -> str:
    """Decode CSV bytes with UTF-8 first, latin-1 as fallback."""
    try:
        return csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return csv_bytes.decode("latin-1", errors="replace")


def _normalize_fieldnames(reader: csv.DictReader) -> dict[str, str]:
    """Return {lowercased_clean_header: original_header}."""
    out: dict[str, str] = {}
    for name in reader.fieldnames or []:
        if name is None:
            continue
        key = name.strip().lower().lstrip("\ufeff")
        if key and key not in out:
            out[key] = name
    return out


def _pick(headers: dict[str, str], aliases: set[str]) -> str | None:
    for alias in aliases:
        if alias in headers:
            return headers[alias]
    return None


def _parse_int(value: str | None) -> int | None:
    """Parse a potentially-formatted integer field.

    Discord snowflake IDs are 17-19 digits, which overflows float64's
    ~15-16 digit precision -- the old int(float(s)) path silently
    corrupted the last few digits of every user id above ~10^16, writing
    imported levels to the wrong account. Go through int() directly and
    only fall back to float() for exotic forms (scientific notation,
    trailing decimals).
    """
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("_", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _parse_date(value):
    """Parse an ISO-8601 date or datetime into a ``datetime.date``.

    Accepts bare ``YYYY-MM-DD``, full ISO timestamps with or without ``Z``,
    and returns ``None`` for empty / unparseable values.
    """
    import datetime as _dt
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Tolerate the trailing Z used in the source-system export.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(s)
        return parsed.date()
    except ValueError:
        pass
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


async def parse_and_import(
    db: "PgDatabase",
    guild_id: int,
    cfg: LevelingConfig,
    csv_bytes: bytes,
    *,
    batch_size: int = 250,
    progress_cb: Callable[[int, int], Awaitable[None]] | None = None,
    dry_run: bool = False,
) -> ImportResult:
    """Parse a CSV payload and upsert each row into chat_levels.

    ``progress_cb(done, total)`` is awaited after every batch if provided.
    When ``dry_run`` is True every row is parsed and validated but no DB
    writes are issued; the returned ``ImportResult`` reports what *would*
    have been imported/skipped/errored.
    """
    result = ImportResult()
    text = _decode(csv_bytes)
    reader = csv.DictReader(io.StringIO(text))

    headers = _normalize_fieldnames(reader)
    uid_col   = _pick(headers, _USER_ID_KEYS)
    level_col = _pick(headers, _LEVEL_KEYS)
    xp_col    = _pick(headers, _XP_KEYS)
    msg_col   = _pick(headers, _MSG_KEYS)
    streak_col     = _pick(headers, _STREAK_KEYS)
    last_active_col = _pick(headers, _LAST_ACTIVE_KEYS)
    name_col  = _pick(headers, _NAME_KEYS)

    if uid_col is None:
        result.add_error(
            "No user-id column found.  Expected one of: "
            + ", ".join(sorted(_USER_ID_KEYS))
        )
        return result

    if level_col is None and xp_col is None:
        result.add_error(
            "No level or XP column found.  Expected one of: "
            + ", ".join(sorted(_LEVEL_KEYS | _XP_KEYS))
        )
        return result

    rows = list(reader)
    result.total_rows = len(rows)

    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        batch_ctx = contextlib.AsyncExitStack() if dry_run else db.atomic()
        async with batch_ctx:
            for row_idx, row in enumerate(chunk, start=i + 1):
                uid = _parse_int(row.get(uid_col))
                if uid is None or uid <= 0:
                    result.add_error(f"Row {row_idx}: invalid user id {row.get(uid_col)!r}")
                    continue

                total_xp: int | None = None
                if xp_col is not None:
                    parsed_xp = _parse_int(row.get(xp_col))
                    if parsed_xp is not None and parsed_xp > 0:
                        total_xp = parsed_xp

                level_val: int | None = None
                if level_col is not None:
                    level_val = _parse_int(row.get(level_col))

                if total_xp is None and (level_val is None or level_val < 0):
                    result.skipped += 1
                    continue

                display_name: str | None = None
                if name_col is not None:
                    raw_name = row.get(name_col)
                    if raw_name:
                        trimmed = str(raw_name).strip()
                        if trimmed:
                            # Cap at 128 chars -- Discord display names are
                            # capped at 32 but we store under 100 to be safe.
                            display_name = trimmed[:128]

                if dry_run:
                    result.imported += 1
                    continue

                try:
                    if total_xp is not None:
                        await set_user_xp(
                            db, guild_id, uid, total_xp, cfg,
                            display_name=display_name,
                        )
                    else:
                        await set_user_level(
                            db, guild_id, uid, level_val, cfg,
                            display_name=display_name,
                        )
                except Exception as exc:
                    result.add_error(f"Row {row_idx}: {type(exc).__name__}: {exc}")
                    continue

                if msg_col is not None:
                    msgs = _parse_int(row.get(msg_col))
                    if msgs is not None and msgs > 0:
                        try:
                            await db.execute(
                                "UPDATE chat_levels SET total_messages=$3 "
                                "WHERE guild_id=$1 AND user_id=$2",
                                guild_id, uid, int(msgs),
                            )
                        except Exception:
                            log.debug(
                                "parse_and_import: message count update failed "
                                "gid=%s uid=%s", guild_id, uid,
                            )

                if streak_col is not None or last_active_col is not None:
                    streak_val = None
                    if streak_col is not None:
                        streak_val = _parse_int(row.get(streak_col))
                    date_val = None
                    if last_active_col is not None:
                        date_val = _parse_date(row.get(last_active_col))
                    # If the source gave us a streak count but no last-active
                    # date, anchor the streak to today. Otherwise apply_streak
                    # sees last_active_date IS NULL on the next chat and wipes
                    # the imported streak back to 1.
                    if streak_val and streak_val > 0 and date_val is None:
                        import datetime as _dt
                        date_val = _dt.date.today()
                    if streak_val is not None or date_val is not None:
                        try:
                            await set_user_streak(
                                db, guild_id, uid,
                                max(0, int(streak_val or 0)),
                                date_val,
                            )
                        except Exception:
                            log.debug(
                                "parse_and_import: streak update failed "
                                "gid=%s uid=%s", guild_id, uid,
                            )

                result.imported += 1

        if progress_cb is not None:
            try:
                await progress_cb(min(i + batch_size, result.total_rows), result.total_rows)
            except Exception:
                pass

    return result
