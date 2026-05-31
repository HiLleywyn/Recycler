"""Users & Profiles router  -  11 endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from core.framework.scale import to_human
from api.v2.dependencies import get_current_user, get_db
from api.v2.exceptions import NotFoundError
from api.v2.schemas.user import (
    Badge,
    GameStats,
    PnLSnapshot,
    UserProfile,
    UserSearchResult,
    UserSettings,
    UserSettingsUpdate,
)
from api.v2.schemas.notifications import NotificationPreferences, NotificationPreferencesUpdate
from api.v2.utils import to_iso

router = APIRouter(prefix="/users", tags=["users"])


# ---- helpers ---------------------------------------------------------------

def _str(val: Any) -> str | None:
    return str(val) if val is not None else None


# ---- guild module status (public, non-admin) ------------------------------

@router.get("/guild-modules", summary="Get enabled modules for current guild")
async def get_guild_modules(
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Return a dict of module_name → enabled boolean for the user's guild."""
    guild_id = user.get("guild_id")
    if not guild_id:
        return {"modules": {}}
    row = await conn.fetchrow(
        "SELECT * FROM guild_settings WHERE guild_id=$1", int(guild_id),
    )
    if not row:
        return {"modules": {}}
    prefix = "module_"
    return {
        "modules": {
            k[len(prefix):]: (v if v is not None else True)
            for k, v in dict(row).items()
            if k.startswith(prefix)
        }
    }


# ---- username resolver (must be before /{user_id} routes) -----------------

from pydantic import BaseModel, Field


class _ResolveRequest(BaseModel):
    user_ids: list[str] = Field(..., max_length=100, description="List of user IDs to resolve.")


@router.post("/resolve", summary="Resolve user IDs to usernames")
async def resolve_usernames(
    body: _ResolveRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Batch-resolve user IDs to display names. Returns {user_id: username} mapping."""
    guild_id = int(user["guild_id"])
    if not body.user_ids:
        return {}
    # Cast to bigint list for the query
    ids = []
    for uid in body.user_ids:
        try:
            ids.append(int(uid))
        except (ValueError, TypeError):
            continue
    if not ids:
        return {}
    rows = await db.fetch(
        "SELECT user_id, username FROM users WHERE guild_id = $1 AND user_id = ANY($2::bigint[])",
        guild_id, ids,
    )
    result = {}
    found = {r["user_id"]: r["username"] for r in rows}
    for uid_str in body.user_ids:
        try:
            uid_int = int(uid_str)
        except (ValueError, TypeError):
            continue
        name = found.get(uid_int, "")
        result[uid_str] = name if name else f"User {uid_str[:8]}"
    return result


# ---- public endpoints ------------------------------------------------------

@router.get("/{user_id}/profile", response_model=UserProfile, summary="Get user profile")
async def get_user_profile(user_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Return a user's trading profile including badges (guild-scoped)."""
    guild_id = int(user["guild_id"])
    row = await db.fetchrow(
        """
        SELECT u.user_id, u.guild_id, u.username, u.created_at,
               p.total_trades, p.total_trade_volume, p.realized_pnl,
               p.best_trade_pnl, p.worst_trade_pnl, p.win_count, p.loss_count,
               p.total_games, p.total_wagered, p.total_game_profit
        FROM users u
        LEFT JOIN user_profiles p ON p.user_id = u.user_id AND p.guild_id = u.guild_id
        WHERE u.user_id = $1 AND u.guild_id = $2
        LIMIT 1
        """,
        user_id, guild_id,
    )
    if not row:
        raise NotFoundError("User not found.")

    badges_rows = await db.fetch(
        """
        SELECT ub.badge_id, b.name, b.description, b.icon, b.category, ub.earned_at
        FROM user_badges ub
        JOIN badges b ON b.badge_id = ub.badge_id
        WHERE ub.user_id = $1 AND ub.guild_id = $2
        ORDER BY ub.earned_at DESC
        """,
        row["user_id"],
        row["guild_id"],
    )
    badges = [
        Badge(
            badge_id=b["badge_id"],
            name=b["name"],
            description=b["description"],
            icon=b["icon"],
            category=b["category"],
            earned_at=to_iso(b["earned_at"]),
        )
        for b in badges_rows
    ]

    total_trades = row["total_trades"] or 0
    win_count = row["win_count"] or 0
    loss_count = row["loss_count"] or 0
    total = win_count + loss_count
    win_rate = (win_count / total * 100) if total > 0 else 0.0

    # Include avatar from the authenticated user's JWT payload if viewing own profile
    avatar = None
    if int(user["user_id"]) == user_id:
        avatar = user.get("avatar")

    return UserProfile(
        user_id=str(row["user_id"]),
        username=row["username"] or None,
        avatar=avatar,
        total_trades=total_trades,
        total_trade_volume=to_human(int(row["total_trade_volume"] or 0)),
        realized_pnl=to_human(int(row["realized_pnl"] or 0)),
        best_trade_pnl=to_human(int(row["best_trade_pnl"] or 0)),
        worst_trade_pnl=to_human(int(row["worst_trade_pnl"] or 0)),
        win_count=win_count,
        loss_count=loss_count,
        win_rate=round(win_rate, 2),
        total_games=row["total_games"] or 0,
        total_wagered=to_human(int(row["total_wagered"] or 0)),
        total_game_profit=to_human(int(row["total_game_profit"] or 0)),
        badges=badges,
        member_since=to_iso(row["created_at"]),
    )


@router.get("/{user_id}/holdings", summary="Get user holdings")
async def get_user_holdings(user_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Return a user's portfolio snapshot (guild-scoped)."""
    holdings: list[dict] = []
    guild_id = int(user["guild_id"])

    # Wallet + bank balance as USD entry
    user_row = await db.fetchrow(
        "SELECT wallet, bank, guild_id FROM users WHERE user_id = $1 AND guild_id = $2 LIMIT 1",
        user_id, guild_id,
    )
    if user_row:
        wallet = to_human(int(user_row["wallet"] or 0))
        bank = to_human(int(user_row["bank"] or 0))
        guild_id = user_row["guild_id"]
        if wallet + bank > 0:
            holdings.append({
                "symbol": "USD",
                "amount": round(wallet + bank, 2),
                "value_usd": round(wallet + bank, 2),
                "price": 1.0,
            })
    else:
        guild_id = None

    # Crypto holdings (guild-scoped)
    rows = await db.fetch(
        """
        SELECT ch.symbol, ch.amount, cp.price
        FROM crypto_holdings ch
        LEFT JOIN crypto_prices cp ON cp.symbol = ch.symbol AND cp.guild_id = ch.guild_id
        WHERE ch.user_id = $1 AND ch.guild_id = $2 AND ch.amount > 0
        ORDER BY (ch.amount * COALESCE(cp.price, 0)) DESC
        """,
        user_id, guild_id,
    )
    for r in rows:
        _amt_h = to_human(int(r["amount"] or 0))
        holdings.append({
            "symbol": r["symbol"],
            "amount": _amt_h,
            "value_usd": round(_amt_h * float(r["price"] or 0), 8),
            "price": float(r["price"] or 0),
        })

    # Staked tokens
    if guild_id is not None:
        stake_rows = await db.fetch(
            """
            SELECT s.symbol, SUM(s.amount) as amount, COALESCE(cp.price, 0) as price
            FROM stakes s
            LEFT JOIN crypto_prices cp ON cp.symbol = s.symbol AND cp.guild_id = s.guild_id
            WHERE s.user_id = $1 AND s.guild_id = $2 AND s.amount > 0
            GROUP BY s.symbol, cp.price
            """,
            user_id,
            guild_id,
        )
        for r in stake_rows:
            amt = to_human(int(r["amount"] or 0))
            price = float(r["price"])
            holdings.append({
                "symbol": f"{r['symbol']} (staked)",
                "amount": amt,
                "value_usd": round(amt * price, 2),
                "price": price,
            })

    return {"user_id": str(user_id), "holdings": holdings}


@router.get("/{user_id}/pnl", response_model=list[PnLSnapshot], summary="Get PnL history")
async def get_user_pnl(
    user_id: int,
    limit: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return historical PnL snapshots for a user (guild-scoped)."""
    guild_id = int(user["guild_id"])
    rows = await db.fetch(
        """
        SELECT net_worth, ts
        FROM pnl_snapshots
        WHERE user_id = $1 AND guild_id = $2
        ORDER BY ts DESC
        LIMIT $3
        """,
        user_id, guild_id,
        limit,
    )
    return [
        PnLSnapshot(net_worth=float(r["net_worth"]), ts=to_iso(r["ts"]))
        for r in rows
    ]


@router.get("/{user_id}/badges", response_model=list[Badge], summary="Get user badges")
async def get_user_badges(user_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Return all badges earned by a user (guild-scoped)."""
    guild_id = int(user["guild_id"])
    rows = await db.fetch(
        """
        SELECT ub.badge_id, b.name, b.description, b.icon, b.category, ub.earned_at
        FROM user_badges ub
        JOIN badges b ON b.badge_id = ub.badge_id
        WHERE ub.user_id = $1 AND ub.guild_id = $2
        ORDER BY ub.earned_at DESC
        """,
        user_id, guild_id,
    )
    return [
        Badge(
            badge_id=r["badge_id"],
            name=r["name"],
            description=r["description"],
            icon=r["icon"],
            category=r["category"],
            earned_at=to_iso(r["earned_at"]),
        )
        for r in rows
    ]


@router.get("/{user_id}/game-stats", response_model=list[GameStats], summary="Get game stats")
async def get_user_game_stats(user_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Return gambling stats grouped by game type (guild-scoped)."""
    guild_id = int(user["guild_id"])
    rows = await db.fetch(
        """
        SELECT game_type,
               COUNT(*)::int AS games_played,
               COALESCE(SUM(bet_amount), 0) AS total_wagered,
               COALESCE(SUM(profit), 0) AS total_profit,
               COALESCE(MAX(profit), 0) AS best_win,
               COALESCE(AVG(bet_amount), 0) AS avg_bet
        FROM game_results
        WHERE user_id = $1 AND guild_id = $2
        GROUP BY game_type
        ORDER BY games_played DESC
        """,
        user_id, guild_id,
    )
    return [
        GameStats(
            game_type=r["game_type"],
            games_played=r["games_played"],
            total_wagered=float(r["total_wagered"]),
            total_profit=float(r["total_profit"]),
            best_win=float(r["best_win"]),
            avg_bet=float(r["avg_bet"]),
        )
        for r in rows
    ]


@router.get("/search", response_model=list[UserSearchResult], summary="Search users")
async def search_users(
    q: str = Query(..., min_length=1, max_length=100, description="Search query"),
    limit: int = Query(20, ge=1, le=50),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Search users by user ID prefix (guild-scoped)."""
    guild_id = int(user["guild_id"])
    rows = await db.fetch(
        """
        SELECT u.user_id, u.wallet, u.bank
        FROM users u
        WHERE u.user_id::TEXT LIKE $1 AND u.guild_id = $2
        LIMIT $3
        """,
        f"{q}%", guild_id,
        limit,
    )
    return [
        UserSearchResult(
            user_id=str(r["user_id"]),
            net_worth=to_human(int((r["wallet"] or 0) + (r["bank"] or 0))),
        )
        for r in rows
    ]


# ---- authenticated (me) endpoints -----------------------------------------

@router.get("/me", summary="Get my profile")
async def get_me(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the authenticated user's full profile data."""
    row = await db.fetchrow(
        """
        SELECT u.user_id, u.guild_id, u.wallet, u.bank, u.daily_streak,
               u.last_daily, u.last_work, u.created_at,
               p.total_trades, p.total_trade_volume, p.realized_pnl,
               p.best_trade_pnl, p.worst_trade_pnl, p.win_count, p.loss_count,
               p.total_games, p.total_wagered, p.total_game_profit
        FROM users u
        LEFT JOIN user_profiles p ON p.user_id = u.user_id AND p.guild_id = u.guild_id
        WHERE u.user_id = $1 AND u.guild_id = $2
        """,
        int(user["user_id"]),
        int(user["guild_id"]),
    )
    if not row:
        raise NotFoundError("User not found in this guild.")

    return {
        "user_id": str(row["user_id"]),
        "guild_id": str(row["guild_id"]),
        "username": user.get("username"),
        "avatar": user.get("avatar"),
        "wallet": to_human(int(row["wallet"] or 0)),
        "bank": to_human(int(row["bank"] or 0)),
        "daily_streak": row["daily_streak"],
        "last_daily": to_iso(row["last_daily"]),
        "last_work": to_iso(row["last_work"]),
        "member_since": to_iso(row["created_at"]),
        "trading": {
            "total_trades": row["total_trades"] or 0,
            "total_trade_volume": to_human(int(row["total_trade_volume"] or 0)),
            "realized_pnl": to_human(int(row["realized_pnl"] or 0)),
            "best_trade_pnl": to_human(int(row["best_trade_pnl"] or 0)),
            "worst_trade_pnl": to_human(int(row["worst_trade_pnl"] or 0)),
            "win_count": row["win_count"] or 0,
            "loss_count": row["loss_count"] or 0,
        },
        "gaming": {
            "total_games": row["total_games"] or 0,
            "total_wagered": to_human(int(row["total_wagered"] or 0)),
            "total_game_profit": to_human(int(row["total_game_profit"] or 0)),
        },
        "is_admin": user.get("is_admin", False),
    }


@router.get("/me/settings", response_model=UserSettings, summary="Get my settings")
async def get_my_settings(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the authenticated user's display settings."""
    row = await db.fetchrow(
        """
        SELECT theme, currency_format, price_precision, default_chart_tf, auto_levelup
        FROM user_settings
        WHERE user_id = $1 AND guild_id = $2
        """,
        int(user["user_id"]),
        int(user["guild_id"]),
    )
    if not row:
        return UserSettings()
    return UserSettings(
        theme=row["theme"] or "dark",
        currency_format=row["currency_format"] or "usd",
        price_precision=row["price_precision"] if row["price_precision"] is not None else 2,
        default_chart_tf=row["default_chart_tf"] or "1h",
        auto_levelup=row["auto_levelup"] if row["auto_levelup"] is not None else False,
    )


@router.patch("/me/settings", response_model=UserSettings, summary="Update my settings")
async def update_my_settings(
    body: UserSettingsUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Update the authenticated user's display settings."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])

    # Upsert user_settings row
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return await get_my_settings(user, db)

    set_parts = []
    values: list[Any] = [uid, gid]
    idx = 3
    for key, val in updates.items():
        set_parts.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    insert_cols = ", ".join(updates.keys())
    insert_vals = ", ".join(f"${i}" for i in range(3, idx))
    set_clause = ", ".join(set_parts)

    await db.execute(
        f"""
        INSERT INTO user_settings (user_id, guild_id, {insert_cols})
        VALUES ($1, $2, {insert_vals})
        ON CONFLICT (user_id, guild_id) DO UPDATE SET {set_clause}
        """,
        *values,
    )
    return await get_my_settings(user, db)


@router.get("/me/notifications", response_model=NotificationPreferences, summary="Get notification prefs")
async def get_my_notifications(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return DM notification preferences."""
    row = await db.fetchrow(
        "SELECT dm_mining, dm_transfer, dm_validator, dm_staking, dm_2fa "
        "FROM user_prefs WHERE user_id = $1 AND guild_id = $2",
        int(user["user_id"]),
        int(user["guild_id"]),
    )
    if not row:
        return NotificationPreferences()
    return NotificationPreferences(**dict(row))


@router.patch("/me/notifications", response_model=NotificationPreferences, summary="Update notification prefs")
async def update_my_notifications(
    body: NotificationPreferencesUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Toggle DM notification preferences."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return await get_my_notifications(user, db)

    set_parts = []
    values: list[Any] = [uid, gid]
    idx = 3
    for key, val in updates.items():
        set_parts.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    insert_cols = ", ".join(updates.keys())
    insert_vals = ", ".join(f"${i}" for i in range(3, idx))
    set_clause = ", ".join(set_parts)

    await db.execute(
        f"""
        INSERT INTO user_prefs (user_id, guild_id, {insert_cols})
        VALUES ($1, $2, {insert_vals})
        ON CONFLICT (user_id, guild_id) DO UPDATE SET {set_clause}
        """,
        *values,
    )
    return await get_my_notifications(user, db)
