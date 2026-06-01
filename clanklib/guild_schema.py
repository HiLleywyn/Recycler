"""clanklib/guild_schema.py -- the per-guild settings contract.

One declarative description of which guild settings an operator can edit, what
type each is, and how to validate/coerce a submitted value. Shared by:

* the guild-settings REST API (``api/v2``) -- so a web panel renders + validates
  the same fields, and
* the in-Discord ``.set`` command -- so both surfaces agree.

A field maps to either a real ``guild_settings`` column or a key inside the
``features`` JSONB (the DB layer routes it by name). Channel/role types are
stored as ``BIGINT`` ids; ``string``/``bool`` are stored as-is.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class GuildField:
    key: str
    type: str            # string | bool | discord_channel | discord_role | number
    label: str
    help: str = ""
    max_len: Optional[int] = None
    min: Optional[int] = None
    max: Optional[int] = None


# The editable per-guild surface. Keep this aligned with the columns the DB
# layer recognises (database.database._GUILD_SETTING_COLUMNS) plus any JSONB
# feature keys we want exposed.
GUILD_FIELDS: tuple[GuildField, ...] = (
    GuildField("prefix", "string", "Command prefix", "1-5 chars; overrides the global prefix in this server.", max_len=5),
    GuildField("log_channel", "discord_channel", "Log channel", "Where the bot posts audit/event logs."),
)

FIELDS_BY_KEY: dict[str, GuildField] = {f.key: f for f in GUILD_FIELDS}


class GuildSettingError(ValueError):
    """A submitted guild-setting value failed validation."""


def coerce_guild_value(field: GuildField, raw: Any) -> Any:
    """Validate + coerce one value for ``field``. ``None``/empty clears it.

    Raises :class:`GuildSettingError` with a human message on a bad value."""
    if raw is None:
        return None
    if field.type == "string":
        s = str(raw).strip()
        if s == "":
            return None
        if field.max_len and len(s) > field.max_len:
            raise GuildSettingError(f"{field.label}: must be {field.max_len} characters or fewer.")
        return s
    if field.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if field.type == "number":
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise GuildSettingError(f"{field.label}: must be a whole number.")
        if field.min is not None and n < field.min:
            raise GuildSettingError(f"{field.label}: must be >= {field.min}.")
        if field.max is not None and n > field.max:
            raise GuildSettingError(f"{field.label}: must be <= {field.max}.")
        return n
    if field.type in ("discord_channel", "discord_role"):
        # Accept a raw id, a <#id>/<@&id> mention, or empty to clear.
        s = str(raw).strip()
        if s in ("", "none", "off", "clear", "unset"):
            return None
        digits = "".join(ch for ch in s if ch.isdigit())
        if not digits:
            raise GuildSettingError(f"{field.label}: give a channel/role id or mention.")
        return int(digits)
    raise GuildSettingError(f"{field.label}: unknown field type {field.type!r}.")


def validate_guild_settings(submitted: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate a dict of ``{key: value}`` against :data:`GUILD_FIELDS`.

    Returns ``(coerced, errors)``. Unknown keys are reported as errors so a
    typo fails loudly rather than silently writing into the JSONB blob."""
    coerced: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in submitted.items():
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            errors.append(f"unknown setting {key!r}")
            continue
        try:
            coerced[key] = coerce_guild_value(field, raw)
        except GuildSettingError as exc:
            errors.append(str(exc))
    return coerced, errors


def schema_json() -> dict[str, Any]:
    """Render the editable fields for a web UI."""
    return {
        "fields": [
            {"key": f.key, "type": f.type, "label": f.label, "help": f.help,
             "max_len": f.max_len, "min": f.min, "max": f.max}
            for f in GUILD_FIELDS
        ]
    }


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """Project a ``get_guild_settings`` row down to just the editable fields
    (so the API never leaks internal columns)."""
    out: dict[str, Any] = {"guild_id": row.get("guild_id")}
    for f in GUILD_FIELDS:
        out[f.key] = row.get(f.key)
    return out
