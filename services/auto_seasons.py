"""services/auto_seasons.py  -  rotate themed (season + 5 challenges) pairs.

A guild that sets ``guild_settings.auto_seasons_enabled = TRUE`` opts
into automatic pair rotation:

  * **Time schedule** -- when the active season's ``ends_at`` passes,
    the next pair is started immediately. Hooks the existing
    ``season_ended`` bus event published by ``services/seasons.end_season``.
  * **Completion schedule** -- when every challenge in the active pair
    has settled (succeeded or failed), the active season is ended early
    so the next pair can start. Hooks the existing ``challenge_succeeded``
    and ``challenge_failed`` bus events published by ``services/challenges``.

Pair templates live in ``seasons_pairs_config.PAIRS``. The cursor
``guild_settings.auto_seasons_pair_idx`` points at the next pair to
ship; rotation increments it mod ``len(PAIRS)`` so guilds keep cycling
indefinitely.

Tagging
-------
The challenges started for a pair carry a ``[auto:{pair_key}]`` suffix
on their description so the completion-schedule path can tell which
ones belong to the current rotation without piggy-backing on the
season_id (challenges aren't joined to seasons in the schema).

Public API
----------
    ``start_next_pair(bot, guild_id)``  -- create season + 5 challenges,
        advance the cursor. Skips if a season is already active.
    ``ensure_running(bot, guild_id)``   -- start a pair if none active
        and auto-rotation is enabled. Idempotent.
    ``maybe_rotate_on_completion(bot, guild_id)`` -- end the active
        season + queue the next pair when every paired challenge has
        settled.
    ``attach_listeners(bot)``           -- wire ``season_ended``,
        ``challenge_succeeded``, ``challenge_failed`` for every guild.
"""
from __future__ import annotations

import logging
from typing import Any

import configs.seasons_pairs_config as _pairs_cfg
from services import seasons as _season_svc
from services import challenges as _chal_svc

log = logging.getLogger(__name__)

# Marker we stamp into a challenge's description so we can locate the
# pair-cohort later without changing the schema. Format kept simple
# enough that ``,challenges`` won't render badly if the marker leaks
# into a player-facing line.
_PAIR_TAG_PREFIX = "[auto:"
_PAIR_TAG_SUFFIX = "]"


def _pair_tag(pair_key: str) -> str:
    return f"{_PAIR_TAG_PREFIX}{pair_key}{_PAIR_TAG_SUFFIX}"


def _has_pair_tag(description: str | None, pair_key: str | None = None) -> bool:
    if not description:
        return False
    if pair_key is None:
        return _PAIR_TAG_PREFIX in description and _PAIR_TAG_SUFFIX in description
    return _pair_tag(pair_key) in description


def _extract_pair_key(description: str | None) -> str | None:
    """Pull the ``pair_key`` out of a tagged description, or None."""
    if not description:
        return None
    i = description.rfind(_PAIR_TAG_PREFIX)
    if i < 0:
        return None
    j = description.find(_PAIR_TAG_SUFFIX, i + len(_PAIR_TAG_PREFIX))
    if j < 0:
        return None
    return description[i + len(_PAIR_TAG_PREFIX):j]


# ── Settings access ────────────────────────────────────────────────────────

async def _settings(db, guild_id: int) -> dict:
    """Always-fresh ``guild_settings`` row for ``guild_id``. Inserts the
    blank row if missing so the auto_seasons_* defaults apply.
    """
    return await db.get_guild_settings(int(guild_id)) or {}


async def is_enabled(db, guild_id: int) -> bool:
    s = await _settings(db, guild_id)
    return bool(s.get("auto_seasons_enabled"))


async def _read_config(db, guild_id: int) -> tuple[int, float, float, int]:
    """Return ``(days, season_pool, challenge_pool, pair_idx)`` for guild."""
    s = await _settings(db, guild_id)
    days = _pairs_cfg.clamp_days(s.get("auto_seasons_days"))
    # NUMERIC(36, 0) values come back as raw ints. Convert to dollars.
    raw_season = s.get("auto_seasons_pool_usd")
    raw_chal = s.get("auto_seasons_challenge_pool_usd")
    season_pool = _pairs_cfg.clamp_pool(
        _scaled_to_usd(raw_season),
        _pairs_cfg.DEFAULT_SEASON_POOL_USD,
    )
    challenge_pool = _pairs_cfg.clamp_pool(
        _scaled_to_usd(raw_chal),
        _pairs_cfg.DEFAULT_CHALLENGE_POOL_USD * _pairs_cfg.CHALLENGES_PER_PAIR,
    )
    idx = int(s.get("auto_seasons_pair_idx") or 0)
    return days, season_pool, challenge_pool, idx


def _scaled_to_usd(raw: Any) -> float | None:
    """Convert a NUMERIC(36, 0) raw int to a USD float, or None.

    The auto_seasons_pool_usd column stores dollars as a raw int (the
    1e18-scaled convention used everywhere else for monetary columns),
    so divide by 1e18 to get a human dollar amount. ``NULL`` falls
    through as ``None`` so the caller can apply the env default.
    """
    if raw is None:
        return None
    try:
        return float(int(raw)) / 1e18
    except (TypeError, ValueError):
        return None


def _usd_to_scaled(usd: float) -> int:
    """Inverse of ``_scaled_to_usd``. Caller supplies a dollar float."""
    return int(round(float(usd) * 1e18))


# ── Pair lifecycle ─────────────────────────────────────────────────────────

async def start_next_pair(bot, guild_id: int) -> dict | None:
    """Start the next pair (one season + N challenges). No-op if a
    season is already active for the guild. Returns a summary dict on
    success or ``None`` on no-op / pre-existing season.

    Caller is expected to have checked ``is_enabled(...)`` first; this
    function does NOT re-check the toggle so it can be invoked manually
    (``,admin season auto next``) regardless.
    """
    db = bot.db
    if await _season_svc.get_active(db, guild_id) is not None:
        return None
    days, season_pool, challenge_pool, idx = await _read_config(db, guild_id)
    pair = _pairs_cfg.get_pair(idx)
    season = await _season_svc.start(
        db, int(guild_id),
        name=pair.season.name,
        metric=pair.season.metric,
        prize_pool_usd=float(season_pool),
        duration_days=int(days),
        theme=pair.season.theme,
    )
    if season is None:
        # Race: another caller started a season between our check and
        # the insert. Don't advance the cursor -- next tick will retry.
        return None
    splits = _pairs_cfg.split_pool(pair, float(challenge_pool))
    started: list[dict] = []
    for ct, slice_usd in zip(pair.challenges, splits):
        try:
            row = await _chal_svc.start(
                db, int(guild_id),
                name=ct.name,
                trigger=ct.trigger,
                target=int(ct.target),
                reward_pool_usd=float(slice_usd),
                duration_days=int(days),
                description=f"{ct.description} {_pair_tag(pair.key)}".strip(),
            )
        except Exception as exc:
            log.exception(
                "auto_seasons: challenge start failed gid=%s pair=%s trigger=%s: %s",
                guild_id, pair.key, ct.trigger, exc,
            )
            row = None
        if row is not None:
            started.append(dict(row))
    # Advance the cursor. Even if some challenges collided (unique on
    # guild + trigger), keep moving so the next rotation tries a fresh
    # pair instead of looping on the same theme forever.
    await db.execute(
        "INSERT INTO guild_settings (guild_id, auto_seasons_pair_idx) "
        "VALUES ($1, $2) "
        "ON CONFLICT (guild_id) DO UPDATE SET auto_seasons_pair_idx=$2",
        int(guild_id), _pairs_cfg.next_idx(idx),
    )
    try:
        await bot.bus.publish(
            "auto_pair_started",
            guild=bot.get_guild(int(guild_id)),
            pair_key=pair.key,
            season_id=int(season["season_id"]),
            challenge_ids=[int(r["challenge_id"]) for r in started],
        )
    except Exception:
        log.debug("auto_pair_started publish failed gid=%s", guild_id, exc_info=True)
    return {
        "pair": pair,
        "season": dict(season),
        "challenges": started,
        "next_idx": _pairs_cfg.next_idx(idx),
    }


async def ensure_running(bot, guild_id: int) -> dict | None:
    """Start a pair if auto-rotation is enabled and no season is active.

    Used at bot start + when an admin first flips the toggle on.
    """
    if not await is_enabled(bot.db, guild_id):
        return None
    return await start_next_pair(bot, guild_id)


# ── Completion schedule ────────────────────────────────────────────────────

async def _active_pair_challenges(db, guild_id: int) -> list[dict]:
    """Active challenges for ``guild_id`` that carry an auto-pair tag."""
    rows = await _chal_svc.list_active(db, guild_id)
    return [r for r in rows if _has_pair_tag(r.get("description"))]


async def _all_pair_challenges(db, guild_id: int, pair_key: str) -> list[dict]:
    """Every challenge tagged for ``pair_key`` in ``guild_id`` (active or not)."""
    rows = await db.fetch_all(
        """
        SELECT challenge_id, guild_id, name, description, trigger,
               target, progress, reward_pool_usd,
               started_at, ends_at, completed_at, status
        FROM guild_challenges
        WHERE guild_id = $1
          AND description LIKE $2
        ORDER BY started_at DESC
        """,
        int(guild_id), f"%{_pair_tag(pair_key)}%",
    )
    return rows or []


async def maybe_rotate_on_completion(bot, guild_id: int) -> dict | None:
    """If every challenge in the active pair has settled, end the
    active season early and start the next pair.

    Returns the ``start_next_pair`` summary on rotation, ``None`` on no-op.
    """
    db = bot.db
    if not await is_enabled(db, guild_id):
        return None
    active_season = await _season_svc.get_active(db, guild_id)
    if active_season is None:
        # No active season -- defer to ``ensure_running`` to bootstrap.
        return await ensure_running(bot, guild_id)
    # Find the pair_key from the still-active challenges; if none are
    # active, look at the most recent settled cohort (fallback).
    active_pair_chals = await _active_pair_challenges(db, guild_id)
    if active_pair_chals:
        return None  # at least one paired challenge still running
    # Every paired challenge has settled. Find the pair_key from the
    # most recent settled cohort so we can sanity-check.
    recent = await db.fetch_one(
        """
        SELECT description
        FROM guild_challenges
        WHERE guild_id = $1
          AND description LIKE $2
          AND status <> 'active'
        ORDER BY completed_at DESC NULLS LAST, ends_at DESC
        LIMIT 1
        """,
        int(guild_id),
        f"%{_PAIR_TAG_PREFIX}%{_PAIR_TAG_SUFFIX}%",
    )
    pair_key = _extract_pair_key((recent or {}).get("description"))
    if pair_key is None:
        return None
    # Safety: only rotate if the season we're closing is the matching
    # auto-rotation season (theme matches the pair). An admin-started
    # season under a different theme shouldn't get cut short.
    try:
        expected_theme = _pairs_cfg.get_pair(0).season.theme  # placeholder, overridden below
        for p in _pairs_cfg.PAIRS:
            if p.key == pair_key:
                expected_theme = p.season.theme
                break
        if str(active_season.get("theme") or "") != expected_theme:
            return None
    except Exception:
        return None
    try:
        await _season_svc.end_season(bot, int(active_season["season_id"]))
    except Exception as exc:
        log.exception(
            "auto_seasons: end_season on completion failed gid=%s sid=%s: %s",
            guild_id, active_season["season_id"], exc,
        )
        return None
    # ``end_season`` published ``season_ended``; the listener attached
    # below will take it from there. Return None so the caller knows
    # the rotation will land via the bus, not a direct call.
    return None


# ── Bus listeners ──────────────────────────────────────────────────────────

def _extract_gid(guild: Any, fallback: Any = None) -> int | None:
    if guild is None:
        guild = fallback
    if guild is None:
        return None
    if isinstance(guild, int):
        return guild
    return int(getattr(guild, "id", 0) or 0) or None


def attach_listeners(bot) -> None:
    """Wire the bus events that drive auto-rotation. Idempotent at the
    cog level: ``cogs/seasons.cog_load`` calls this on every load and
    the bus tolerates duplicate subscribers gracefully.
    """
    bus = bot.bus

    async def _on_season_ended(**kw) -> None:
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not gid:
            return
        try:
            if await is_enabled(bot.db, gid):
                await start_next_pair(bot, gid)
        except Exception as exc:
            log.exception(
                "auto_seasons: season_ended hook failed gid=%s: %s",
                gid, exc,
            )

    async def _on_challenge_settled(**kw) -> None:
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not gid:
            return
        try:
            await maybe_rotate_on_completion(bot, gid)
        except Exception as exc:
            log.exception(
                "auto_seasons: challenge settle hook failed gid=%s: %s",
                gid, exc,
            )

    bus.subscribe("season_ended", _on_season_ended)
    bus.subscribe("challenge_succeeded", _on_challenge_settled)
    bus.subscribe("challenge_failed", _on_challenge_settled)
