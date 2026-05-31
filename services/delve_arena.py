"""services/delve_arena.py -- ranked + duel matchmaking for the delve arena.

Owns: ELO math, rank/tier resolution, profile snapshot caching, season
windowing, reward grants, leaderboards, duel invites.

The combat engine lives in ``services.delve_arena_battle``; the ASCII
renderer lives in ``services.delve_arena_render``; the cog
(``cogs/dungeon.py`` ``,delve arena`` subgroup) wires the two together.

Per CLAUDE.md DB-side clocks (`EXTRACT(EPOCH FROM (NOW() - col))`) are
used for cooldown + invite-expiry checks rather than Python clocks.
"""
from __future__ import annotations

import json as _json_mod
import logging
import random
from dataclasses import dataclass
from typing import Any

import configs.dungeon_config as dc
from core.framework.scale import to_raw

from services.delve_arena_battle import (
    ArenaProfile,
    BattleReplay,
    LiveDuel,
    profile_from_state,
    simulate_match,
)

log = logging.getLogger(__name__)


# ELO bands. The label is also the reward currency.
ELO_BAND_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (0, "copper"),
    (800, "silver"),
    (1500, "gold"),
    (2200, "rune"),
)
RANK_DIVISIONS: int = 5
ELO_PER_DIVISION: int = 160         # 5 divisions per 800 band
START_ELO: int = 100
K_FACTOR_LOW: int = 32              # Copper / Silver
K_FACTOR_HIGH: int = 24             # Gold / Rune
ASYNC_COOLDOWN_S: int = 300
DUEL_INVITE_TTL_S: int = 30
DUEL_INVITE_COOLDOWN_S: int = 60
SEASON_LENGTH_DAYS: int = 14
SEASON_RESET_FLOOR_ELO: dict[str, int] = {
    "copper": 0, "silver": 800, "gold": 1500, "rune": 2200,
}

# Reward = base ore/rune scaled by rank band + opponent ELO delta + player level.
BAND_REWARD_BASE: dict[str, float] = {
    "copper": 0.20,
    "silver": 0.10,
    "gold":   0.05,
    "rune":   0.01,
}


@dataclass
class ArenaRank:
    rank_key: str = "copper"
    division: int = 1
    elo: int = START_ELO
    peak_elo: int = START_ELO
    wins: int = 0
    losses: int = 0
    streak: int = 0


@dataclass
class ArenaSettlement:
    winner_uid: int | None
    loser_uid: int | None
    p1_elo_before: int
    p2_elo_before: int
    p1_elo_after: int
    p2_elo_after: int
    p1_rank_before: ArenaRank
    p2_rank_before: ArenaRank
    p1_rank_after: ArenaRank
    p2_rank_after: ArenaRank
    reward_symbol: str | None
    reward_qty_human: float
    flawless: bool
    rounds: int
    ranked: bool = True
    match_id: int | None = None


@dataclass
class DuelInvite:
    invite_id: int
    challenger_uid: int
    target_uid: int
    season_id: int
    ranked: bool = True
    expired: bool = False
    accepted: bool = False
    declined: bool = False


# ELO + rank math -------------------------------------------------------

def band_for_elo(elo: int) -> str:
    band = "copper"
    for threshold, name in ELO_BAND_THRESHOLDS:
        if int(elo) >= threshold:
            band = name
    return band


def rank_from_elo(elo: int) -> ArenaRank:
    """Map raw ELO to ``(rank_key, division)`` with a 5-division split."""
    band = band_for_elo(elo)
    band_floor = SEASON_RESET_FLOOR_ELO.get(band, 0)
    # Top band ("rune") has open-ended divisions; cap at V.
    div = min(RANK_DIVISIONS, max(1, ((int(elo) - band_floor) // ELO_PER_DIVISION) + 1))
    return ArenaRank(rank_key=band, division=int(div), elo=int(elo), peak_elo=int(elo))


def _k_factor(elo: int) -> int:
    band = band_for_elo(elo)
    return K_FACTOR_HIGH if band in ("gold", "rune") else K_FACTOR_LOW


def _expected(elo_a: int, elo_b: int) -> float:
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def compute_elo_change(
    winner_elo: int, loser_elo: int,
) -> tuple[int, int]:
    """Return ``(winner_new, loser_new)`` after a 1v1 match."""
    exp_w = _expected(winner_elo, loser_elo)
    k = _k_factor((winner_elo + loser_elo) // 2)
    delta = int(round(k * (1.0 - exp_w)))
    delta = max(1, delta)
    return winner_elo + delta, max(0, loser_elo - delta)


def reward_for_rank(rank: ArenaRank, level: int) -> tuple[str, float]:
    """Return the ore/rune ``(symbol, qty_human)`` for a single arena win.

    Currency is the rank band name in uppercase (matches
    ``dungeon_config.COPPER_SYMBOL`` etc.). Quantity scales with player
    level (1 + level/20) and division (multiplier from 1.0 at I to 1.5 at V).
    """
    band = rank.rank_key.lower()
    symbol_map = {
        "copper": dc.COPPER_SYMBOL,
        "silver": dc.SILVER_SYMBOL,
        "gold":   dc.GOLD_SYMBOL,
        "rune":   dc.RUNE_SYMBOL,
    }
    sym = symbol_map.get(band, dc.COPPER_SYMBOL)
    base = BAND_REWARD_BASE.get(band, 0.20)
    div_mult = 1.0 + (max(0, int(rank.division) - 1)) * 0.125  # I=1.0 .. V=1.5
    lvl_mult = 1.0 + max(0, int(level)) / 20.0
    return sym, round(base * div_mult * lvl_mult, 4)


# DB helpers ------------------------------------------------------------

def _json(d: Any) -> str:
    try:
        return _json_mod.dumps(d, default=str)
    except Exception:
        return "{}"


async def current_season(db: Any) -> dict:
    """Return the active season row, creating one if none open."""
    row = await db.fetch_one(
        "SELECT season_id, start_ts, end_ts, settled FROM delve_arena_seasons "
        "WHERE settled = FALSE ORDER BY start_ts DESC LIMIT 1"
    )
    if row:
        return dict(row)
    new = await db.fetch_one(
        """
        INSERT INTO delve_arena_seasons (start_ts, end_ts)
        VALUES (NOW(), NOW() + ($1::int || ' days')::interval)
        RETURNING season_id, start_ts, end_ts, settled
        """,
        SEASON_LENGTH_DAYS,
    )
    return dict(new or {})


async def get_or_init_row(db: Any, gid: int, uid: int, season_id: int) -> dict:
    row = await db.fetch_one(
        "SELECT * FROM user_delve_arena "
        "WHERE user_id = $1 AND guild_id = $2 AND season_id = $3",
        uid, gid, season_id,
    )
    if row:
        return dict(row)
    await db.execute(
        """
        INSERT INTO user_delve_arena
            (user_id, guild_id, season_id, elo, peak_elo)
        VALUES ($1, $2, $3, $4, $4)
        ON CONFLICT (user_id, guild_id, season_id) DO NOTHING
        """,
        uid, gid, season_id, START_ELO,
    )
    row = await db.fetch_one(
        "SELECT * FROM user_delve_arena "
        "WHERE user_id = $1 AND guild_id = $2 AND season_id = $3",
        uid, gid, season_id,
    )
    return dict(row or {})


async def get_rank(db: Any, gid: int, uid: int) -> ArenaRank:
    season = await current_season(db)
    row = await get_or_init_row(db, gid, uid, int(season.get("season_id") or 0))
    elo = int(row.get("elo") or START_ELO)
    r = rank_from_elo(elo)
    r.peak_elo = int(row.get("peak_elo") or elo)
    r.wins = int(row.get("wins") or 0)
    r.losses = int(row.get("losses") or 0)
    r.streak = int(row.get("streak") or 0)
    return r


async def get_profile(db: Any, gid: int, uid: int, *, name: str = "") -> ArenaProfile:
    """Build an ``ArenaProfile`` from the user's delve state."""
    from services.dungeon import list_state
    state = await list_state(db, gid, uid)
    if not state:
        state = {"class_key": "warrior", "level": 1}
    return profile_from_state(state, uid=uid, name=name or f"Player {uid}")


async def queue_match(
    db: Any, gid: int, uid: int, *, name: str = "",
) -> tuple[ArenaProfile, ArenaProfile, BattleReplay, ArenaSettlement]:
    """Find an opponent and run a ranked async simulation.

    Matchmaking: prefer real opponents within +/-200 ELO from the same
    guild + season; if none, synthesise a CPU profile from the player's
    own stats with a small noise factor so the queue is always playable.
    """
    season = await current_season(db)
    season_id = int(season.get("season_id") or 0)
    row = await get_or_init_row(db, gid, uid, season_id)
    cd_s = await db.fetch_val(
        """
        SELECT COALESCE(
            EXTRACT(EPOCH FROM (NOW() - last_fight_at))::int,
            $4::int + 1
        )
          FROM user_delve_arena
         WHERE user_id = $1 AND guild_id = $2 AND season_id = $3
        """,
        uid, gid, season_id, ASYNC_COOLDOWN_S,
    )
    if int(cd_s or 0) < ASYNC_COOLDOWN_S:
        raise ValueError(
            f"Arena cooldown: {ASYNC_COOLDOWN_S - int(cd_s or 0)}s remaining."
        )
    my_elo = int(row.get("elo") or START_ELO)
    # Player's delve level + ELO together determine the matchmaking bracket.
    # The previous ±200-ELO-only filter let a freshly-onboarded Lv 12
    # copper player get matched against a Lv 39 mage with the same starting
    # ELO -- the mage one-shots them every single fight. Adding a level
    # guardrail (±25% of player level, minimum ±3) keeps matches inside
    # a survivable stat band even when ELO clusters at the floor.
    p1 = await get_profile(db, gid, uid, name=name)
    _level_span = max(3, int(round(p1.level * 0.25)))
    opponent_row = await db.fetch_one(
        """
        SELECT a.* FROM user_delve_arena a
         LEFT JOIN user_dungeon d
           ON d.user_id = a.user_id AND d.guild_id = a.guild_id
         WHERE a.guild_id = $1 AND a.season_id = $2
           AND a.user_id != $3
           AND ABS(a.elo - $4) <= 200
           AND ABS(COALESCE(d.level, 1) - $5) <= $6
         ORDER BY RANDOM() LIMIT 1
        """,
        gid, season_id, uid, my_elo, int(p1.level), int(_level_span),
    )
    if opponent_row:
        opp_uid = int(opponent_row.get("user_id") or 0)
        opp_name = f"Rival {opp_uid % 10_000}"
        p2 = await get_profile(db, gid, opp_uid, name=opp_name)
        opp_elo = int(opponent_row.get("elo") or START_ELO)
        # Belt-and-braces: even with the SQL level filter, profile_from_state
        # can yield a stat block that one-shots p1 if their delve_level
        # column lagged behind their actual XP. Fall through to the synth
        # path when the resolved profile's level still exceeds the
        # guardrail (re-checked here against the live profile, not the
        # arena row).
        if abs(int(p2.level) - int(p1.level)) > _level_span:
            opponent_row = None
    if not opponent_row:
        # Synthesised opponent -- mirror stats with light noise. Clamp the
        # synth level tightly to the player's so a freshly-promoted copper
        # player doesn't get fed to a Lv 30 dummy.
        p2 = ArenaProfile(
            uid=-1, name="Training Dummy",
            class_key=p1.class_key, class_name=p1.class_name,
            level=max(1, p1.level + random.randint(-1, 1)),
            atk=p1.atk * random.uniform(0.85, 1.10),
            defense=p1.defense * random.uniform(0.85, 1.10),
            spd=min(0.95, p1.spd * random.uniform(0.90, 1.10)),
            int_stat=p1.int_stat * random.uniform(0.80, 1.10),
            hp_max=max(1, int(p1.hp_max * random.uniform(0.85, 1.15))),
            weapon_kind=p1.weapon_kind,
            abilities=list(p1.abilities),
        )
        opp_elo = my_elo

    replay = simulate_match(p1, p2)
    settlement = await _settle_match(
        db, gid, season_id, p1, p2, replay,
        my_elo=my_elo, opp_elo=opp_elo,
        mode="async", ranked=True,
    )
    return p1, p2, replay, settlement


async def _settle_match(
    db: Any, gid: int, season_id: int,
    p1: ArenaProfile, p2: ArenaProfile, replay: BattleReplay,
    *, my_elo: int, opp_elo: int, mode: str = "async", ranked: bool = True,
) -> ArenaSettlement:
    """Apply ELO + reward credits + match row insert. Returns settlement."""
    p1_rank_before = rank_from_elo(my_elo)
    p2_rank_before = rank_from_elo(opp_elo)
    if replay.winner_uid == p1.uid:
        winner_elo, loser_elo = compute_elo_change(my_elo, opp_elo)
        new_p1_elo, new_p2_elo = winner_elo, loser_elo
        winner_uid = p1.uid
    elif replay.winner_uid == p2.uid:
        winner_elo, loser_elo = compute_elo_change(opp_elo, my_elo)
        new_p2_elo, new_p1_elo = winner_elo, loser_elo
        winner_uid = p2.uid
    else:
        new_p1_elo, new_p2_elo = my_elo, opp_elo
        winner_uid = None
    p1_rank_after = rank_from_elo(new_p1_elo)
    p2_rank_after = rank_from_elo(new_p2_elo)

    reward_sym, reward_qty = None, 0.0
    if ranked and winner_uid == p1.uid:
        sym, qty = reward_for_rank(p1_rank_after, p1.level)
        reward_sym = sym
        reward_qty = qty
        try:
            await db.update_wallet_holding(
                p1.uid, gid, "dsc", sym, int(to_raw(qty)),
            )
        except Exception:
            log.debug("delve_arena reward credit failed", exc_info=True)
    elif ranked and winner_uid == p2.uid and p2.uid > 0:
        sym, qty = reward_for_rank(p2_rank_after, p2.level)
        reward_sym = sym
        reward_qty = qty
        try:
            await db.update_wallet_holding(
                p2.uid, gid, "dsc", sym, int(to_raw(qty)),
            )
        except Exception:
            log.debug("delve_arena reward credit (p2) failed", exc_info=True)

    if ranked:
        await db.execute(
            """
            UPDATE user_delve_arena
               SET elo = $4,
                   peak_elo = GREATEST(peak_elo, $4),
                   wins = wins + CASE WHEN $5 THEN 1 ELSE 0 END,
                   losses = losses + CASE WHEN $5 THEN 0 ELSE 1 END,
                   streak = CASE WHEN $5 THEN streak + 1 ELSE 0 END,
                   best_streak = GREATEST(best_streak, CASE WHEN $5 THEN streak + 1 ELSE streak END),
                   last_fight_at = NOW()
             WHERE user_id = $1 AND guild_id = $2 AND season_id = $3
            """,
            p1.uid, gid, season_id, int(new_p1_elo),
            bool(winner_uid == p1.uid),
        )
        if p2.uid > 0:
            await db.execute(
                """
                UPDATE user_delve_arena
                   SET elo = $4,
                       peak_elo = GREATEST(peak_elo, $4),
                       wins = wins + CASE WHEN $5 THEN 1 ELSE 0 END,
                       losses = losses + CASE WHEN $5 THEN 0 ELSE 1 END,
                       streak = CASE WHEN $5 THEN streak + 1 ELSE 0 END,
                       best_streak = GREATEST(best_streak, CASE WHEN $5 THEN streak + 1 ELSE streak END),
                       last_fight_at = NOW()
                 WHERE user_id = $1 AND guild_id = $2 AND season_id = $3
                """,
                p2.uid, gid, season_id, int(new_p2_elo),
                bool(winner_uid == p2.uid),
            )

    match_row = await db.fetch_one(
        """
        INSERT INTO delve_arena_matches
            (season_id, guild_id, p1_uid, p2_uid, winner_uid,
             p1_elo_before, p1_elo_after, p2_elo_before, p2_elo_after,
             rounds, flawless, mode, ranked, replay)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb)
        RETURNING match_id
        """,
        season_id, gid, p1.uid, p2.uid, winner_uid,
        int(my_elo), int(new_p1_elo),
        int(opp_elo), int(new_p2_elo),
        len(replay.rounds), bool(replay.flawless),
        mode, ranked,
        _json({
            "rounds": len(replay.rounds),
            "winner": winner_uid,
            "p1_name": p1.name, "p2_name": p2.name,
        }),
    )
    match_id = int((match_row or {}).get("match_id") or 0)

    return ArenaSettlement(
        winner_uid=winner_uid,
        loser_uid=(p2.uid if winner_uid == p1.uid else p1.uid),
        p1_elo_before=int(my_elo), p2_elo_before=int(opp_elo),
        p1_elo_after=int(new_p1_elo), p2_elo_after=int(new_p2_elo),
        p1_rank_before=p1_rank_before, p2_rank_before=p2_rank_before,
        p1_rank_after=p1_rank_after, p2_rank_after=p2_rank_after,
        reward_symbol=reward_sym, reward_qty_human=float(reward_qty),
        flawless=bool(replay.flawless),
        rounds=len(replay.rounds),
        ranked=ranked,
        match_id=match_id,
    )


# Duel invites ----------------------------------------------------------

async def open_duel(
    db: Any, gid: int, challenger: int, target: int,
    *, ranked: bool = True,
) -> DuelInvite:
    if challenger == target:
        raise ValueError("You cannot duel yourself.")
    season = await current_season(db)
    season_id = int(season.get("season_id") or 0)
    # Per-challenger cooldown. fetch_val returns None when the challenger
    # has no prior invites -- treat that as "cooldown elapsed", not "0
    # seconds elapsed", or the user is permanently locked behind a 60s
    # timer they can never burn down because no row exists to age.
    busy_s = await db.fetch_val(
        """
        SELECT EXTRACT(EPOCH FROM (NOW() - created_at))::int
          FROM delve_arena_duel_invites
         WHERE challenger_uid = $1 AND guild_id = $2
        ORDER BY created_at DESC LIMIT 1
        """,
        challenger, gid,
    )
    if busy_s is not None and int(busy_s) < DUEL_INVITE_COOLDOWN_S:
        raise ValueError(
            f"Duel invite cooldown: {DUEL_INVITE_COOLDOWN_S - int(busy_s)}s left."
        )
    row = await db.fetch_one(
        """
        INSERT INTO delve_arena_duel_invites
            (season_id, guild_id, challenger_uid, target_uid, ranked, status)
        VALUES ($1, $2, $3, $4, $5, 'pending')
        RETURNING invite_id
        """,
        season_id, gid, challenger, target, bool(ranked),
    )
    return DuelInvite(
        invite_id=int(row.get("invite_id") or 0),
        challenger_uid=int(challenger),
        target_uid=int(target),
        season_id=season_id,
        ranked=bool(ranked),
    )


async def fetch_invite(db: Any, invite_id: int) -> DuelInvite | None:
    row = await db.fetch_one(
        """
        SELECT invite_id, challenger_uid, target_uid, season_id, ranked, status,
               EXTRACT(EPOCH FROM (NOW() - created_at))::int AS age_s
          FROM delve_arena_duel_invites
         WHERE invite_id = $1
        """,
        invite_id,
    )
    if not row:
        return None
    expired = (
        int(row.get("age_s") or 0) > DUEL_INVITE_TTL_S
        and str(row.get("status") or "") == "pending"
    )
    return DuelInvite(
        invite_id=int(row.get("invite_id") or 0),
        challenger_uid=int(row.get("challenger_uid") or 0),
        target_uid=int(row.get("target_uid") or 0),
        season_id=int(row.get("season_id") or 0),
        ranked=bool(row.get("ranked")),
        expired=expired,
        accepted=(str(row.get("status") or "") == "accepted"),
        declined=(str(row.get("status") or "") == "declined"),
    )


async def mark_invite(db: Any, invite_id: int, status: str) -> None:
    await db.execute(
        """
        UPDATE delve_arena_duel_invites
           SET status = $2, resolved_at = NOW()
         WHERE invite_id = $1
        """,
        invite_id, status,
    )


async def begin_duel(
    db: Any, gid: int, invite: DuelInvite, *, names: dict[int, str] | None = None,
) -> tuple[ArenaProfile, ArenaProfile, LiveDuel]:
    """Build profiles for both duelists and return the LiveDuel."""
    names = names or {}
    p1 = await get_profile(
        db, gid, invite.challenger_uid,
        name=names.get(invite.challenger_uid, "Challenger"),
    )
    p2 = await get_profile(
        db, gid, invite.target_uid,
        name=names.get(invite.target_uid, "Defender"),
    )
    return p1, p2, LiveDuel(p1, p2)


async def settle_duel(
    db: Any, gid: int, invite: DuelInvite, replay: BattleReplay,
) -> ArenaSettlement:
    """Apply ELO + rewards for a finished live duel."""
    season_id = invite.season_id
    p1_row = await get_or_init_row(db, gid, invite.challenger_uid, season_id)
    p2_row = await get_or_init_row(db, gid, invite.target_uid, season_id)
    p1 = replay.p1
    p2 = replay.p2
    settlement = await _settle_match(
        db, gid, season_id, p1, p2, replay,
        my_elo=int(p1_row.get("elo") or START_ELO),
        opp_elo=int(p2_row.get("elo") or START_ELO),
        mode="duel", ranked=bool(invite.ranked),
    )
    await mark_invite(db, invite.invite_id, "accepted")
    return settlement


# Leaderboards ----------------------------------------------------------

async def list_leaderboard(
    db: Any, gid: int, *, season_id: int | None = None, limit: int = 25,
) -> list[dict]:
    season_id = season_id or int((await current_season(db)).get("season_id") or 0)
    rows = await db.fetch_all(
        """
        SELECT user_id, elo, peak_elo, wins, losses, best_streak, streak
          FROM user_delve_arena
         WHERE guild_id = $1 AND season_id = $2
        ORDER BY elo DESC, peak_elo DESC
         LIMIT $3
        """,
        gid, season_id, int(limit),
    )
    return [dict(r) for r in rows or []]


__all__ = [
    "ArenaRank", "ArenaSettlement", "DuelInvite",
    "ELO_BAND_THRESHOLDS", "RANK_DIVISIONS",
    "ASYNC_COOLDOWN_S", "DUEL_INVITE_TTL_S", "DUEL_INVITE_COOLDOWN_S",
    "SEASON_LENGTH_DAYS",
    "band_for_elo", "rank_from_elo",
    "compute_elo_change", "reward_for_rank",
    "current_season", "get_or_init_row", "get_rank", "get_profile",
    "queue_match", "open_duel", "fetch_invite", "mark_invite",
    "begin_duel", "settle_duel",
    "list_leaderboard",
]
