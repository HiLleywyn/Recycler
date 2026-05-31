"""Admin ops router  -  remote management of Postgres, Redis, Bot, and API.

All endpoints require admin authentication (API key or JWT with is_admin/is_owner).
Access via the dashboard or any HTTP client:

    GET /api/v2/admin/ops/overview     -  full system status at a glance
    GET /api/v2/admin/ops/postgres     -  Postgres stats, connections, table sizes
    GET /api/v2/admin/ops/redis        -  Redis memory, keys, connected clients
    GET /api/v2/admin/ops/bot          -  guilds, cogs, uptime, tasks, errors
    GET /api/v2/admin/ops/api          -  routes, middleware, request stats
    POST /api/v2/admin/ops/postgres/query  -  run read-only SQL (SELECT only)
    POST /api/v2/admin/ops/redis/command   -  run safe Redis commands
    POST /api/v2/admin/ops/bot/reload-cog  -  hot-reload a cog without restart
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.v2.dependencies import require_admin, get_db, get_redis
from api.v2.exceptions import ValidationError

router = APIRouter(prefix="/ops", tags=["admin"])


# ── Helpers ───────────────────────────────────────────────────────────────

def _get_bot(request: Request):
    bot = getattr(request.app.state, "bot", None)
    if not bot:
        raise HTTPException(503, "Bot not available (API running standalone)")
    return bot


# ── Overview ──────────────────────────────────────────────────────────────

@router.get("/overview", dependencies=[Depends(require_admin)])
async def ops_overview(request: Request, conn=Depends(get_db), redis=Depends(get_redis)):
    """Single-call system status for all services."""
    result: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat()}

    # Postgres
    try:
        pg_version = await conn.fetchval("SELECT version()")
        table_count = await conn.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        )
        db_size = await conn.fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        )
        result["postgres"] = {
            "status": "connected",
            "version": pg_version.split(",")[0] if pg_version else "unknown",
            "tables": table_count,
            "size": db_size,
        }
    except Exception as exc:
        result["postgres"] = {"status": "error", "error": str(exc)}

    # Redis
    if redis:
        try:
            info = await redis.info("memory")
            keys = await redis.dbsize()
            result["redis"] = {
                "status": "connected",
                "memory_used": info.get("used_memory_human", "?"),
                "memory_peak": info.get("used_memory_peak_human", "?"),
                "total_keys": keys,
            }
        except Exception as exc:
            result["redis"] = {"status": "error", "error": str(exc)}
    else:
        result["redis"] = {"status": "unavailable"}

    # Bot
    bot = getattr(request.app.state, "bot", None)
    if bot:
        uptime_s = time.time() - getattr(bot, "_start_time", time.time())
        result["bot"] = {
            "status": "ready" if bot.is_ready() else "connecting",
            "username": str(bot.user) if bot.user else None,
            "guilds": len(bot.guilds) if bot.is_ready() else 0,
            "cogs": len(bot.cogs),
            "uptime_seconds": round(uptime_s),
            "latency_ms": round(bot.latency * 1000, 1) if bot.latency else None,
        }
    else:
        result["bot"] = {"status": "unavailable"}

    # API
    result["api"] = {
        "status": "running",
        "version": "2.0.0",
        "routes": len(request.app.routes),
    }

    return result


# ── Postgres ──────────────────────────────────────────────────────────────

@router.get("/postgres", dependencies=[Depends(require_admin)])
async def ops_postgres(conn=Depends(get_db)):
    """Detailed Postgres diagnostics."""
    version = await conn.fetchval("SELECT version()")
    db_size = await conn.fetchval(
        "SELECT pg_size_pretty(pg_database_size(current_database()))"
    )

    # Active connections
    connections = await conn.fetch("""
        SELECT state, count(*) as count
        FROM pg_stat_activity
        WHERE datname = current_database()
        GROUP BY state
    """)

    # Table sizes (top 20)
    tables = await conn.fetch("""
        SELECT
            relname AS table,
            n_live_tup AS rows,
            pg_size_pretty(pg_total_relation_size(c.oid)) AS size
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT 20
    """)

    # Slow query stats (if pg_stat_statements is available)
    slow_queries = []
    try:
        slow_queries = [dict(r) for r in await conn.fetch("""
            SELECT query, calls, mean_exec_time::numeric(10,2) as avg_ms,
                   total_exec_time::numeric(10,2) as total_ms
            FROM pg_stat_statements
            WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
            ORDER BY mean_exec_time DESC
            LIMIT 10
        """)]
    except Exception:
        pass  # pg_stat_statements not enabled

    # Pending migrations
    applied_migrations = []
    try:
        applied_migrations = [r["filename"] for r in await conn.fetch(
            "SELECT filename FROM schema_migrations ORDER BY filename"
        )]
    except Exception:
        pass

    return {
        "version": version,
        "database_size": db_size,
        "connections": {r["state"] or "null": r["count"] for r in connections},
        "tables": [
            {"table": r["table"], "rows": r["rows"], "size": r["size"]}
            for r in tables
        ],
        "slow_queries": slow_queries,
        "applied_migrations": len(applied_migrations),
        "latest_migration": applied_migrations[-1] if applied_migrations else None,
    }


class SqlQuery(BaseModel):
    sql: str


@router.post("/postgres/query", dependencies=[Depends(require_admin)])
async def ops_postgres_query(body: SqlQuery, conn=Depends(get_db)):
    """Run a read-only SQL query. Only SELECT/EXPLAIN/WITH are allowed."""
    sql = body.sql.strip()

    # Whitelist: only read-only statements
    first_word = sql.split()[0].upper() if sql else ""
    if first_word not in ("SELECT", "EXPLAIN", "WITH", "SHOW"):
        raise ValidationError("Only SELECT, EXPLAIN, WITH, and SHOW queries are allowed.")

    # Extra safety: reject anything that looks like mutation
    upper = sql.upper()
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
                       "CREATE", "GRANT", "REVOKE", "COPY"):
        if forbidden in upper:
            raise ValidationError(f"Query contains forbidden keyword: {forbidden}")

    try:
        rows = await conn.fetch(sql)
        return {
            "row_count": len(rows),
            "rows": [dict(r) for r in rows[:500]],  # cap at 500 rows
        }
    except Exception as exc:
        raise ValidationError(f"Query error: {exc}")


# ── Redis ─────────────────────────────────────────────────────────────────

@router.get("/redis", dependencies=[Depends(require_admin)])
async def ops_redis(redis=Depends(get_redis)):
    """Detailed Redis diagnostics."""
    if not redis:
        raise HTTPException(503, "Redis not connected")

    info_mem = await redis.info("memory")
    info_srv = await redis.info("server")
    info_stats = await redis.info("stats")
    info_clients = await redis.info("clients")
    keys = await redis.dbsize()

    # Sample some keys to show namespaces
    sample_keys = []
    cursor = 0
    cursor, batch = await redis.scan(cursor=cursor, count=100)
    prefixes: dict[str, int] = {}
    for key in batch:
        k = key if isinstance(key, str) else key.decode("utf-8", errors="replace")
        prefix = k.split(":")[0] if ":" in k else k
        prefixes[prefix] = prefixes.get(prefix, 0) + 1
        if len(sample_keys) < 20:
            sample_keys.append(k)

    return {
        "version": info_srv.get("redis_version", "?"),
        "uptime_seconds": info_srv.get("uptime_in_seconds", 0),
        "memory": {
            "used": info_mem.get("used_memory_human", "?"),
            "peak": info_mem.get("used_memory_peak_human", "?"),
            "max": info_mem.get("maxmemory_human", "?"),
            "fragmentation_ratio": info_mem.get("mem_fragmentation_ratio", 0),
        },
        "clients": {
            "connected": info_clients.get("connected_clients", 0),
            "blocked": info_clients.get("blocked_clients", 0),
        },
        "stats": {
            "total_commands_processed": info_stats.get("total_commands_processed", 0),
            "ops_per_sec": info_stats.get("instantaneous_ops_per_sec", 0),
            "hit_rate": (
                round(
                    info_stats.get("keyspace_hits", 0)
                    / max(info_stats.get("keyspace_hits", 0) + info_stats.get("keyspace_misses", 1), 1)
                    * 100, 1,
                )
            ),
        },
        "total_keys": keys,
        "key_prefixes": dict(sorted(prefixes.items(), key=lambda x: -x[1])[:15]),
        "sample_keys": sample_keys,
    }


class RedisCommand(BaseModel):
    command: str
    args: list[str] = []


@router.post("/redis/command", dependencies=[Depends(require_admin)])
async def ops_redis_command(body: RedisCommand, redis=Depends(get_redis)):
    """Run a safe Redis command. Mutation commands are blocked."""
    if not redis:
        raise HTTPException(503, "Redis not connected")

    cmd = body.command.upper()

    # Whitelist safe commands
    safe = {
        "GET", "MGET", "KEYS", "SCAN", "TYPE", "TTL", "PTTL", "EXISTS",
        "STRLEN", "LLEN", "SCARD", "ZCARD", "HLEN", "HGETALL", "HKEYS",
        "LRANGE", "SMEMBERS", "ZRANGE", "INFO", "DBSIZE", "PING",
        "MEMORY", "OBJECT", "DEBUG",
    }
    # Explicit blocklist for dangerous commands
    dangerous = {
        "DEL", "FLUSHDB", "FLUSHALL", "SET", "MSET", "HSET", "LPUSH",
        "RPUSH", "SADD", "ZADD", "EXPIRE", "PERSIST", "RENAME",
        "CONFIG", "SHUTDOWN", "SLAVEOF", "REPLICAOF", "BGSAVE",
    }

    if cmd in dangerous:
        raise ValidationError(f"Command '{cmd}' is not allowed (mutation)")
    if cmd not in safe:
        raise ValidationError(f"Command '{cmd}' is not in the safe-list")

    try:
        result = await redis.execute_command(cmd, *body.args)
        # Convert bytes to strings for JSON
        if isinstance(result, bytes):
            result = result.decode("utf-8", errors="replace")
        elif isinstance(result, list):
            result = [
                x.decode("utf-8", errors="replace") if isinstance(x, bytes) else x
                for x in result
            ]
        return {"command": f"{cmd} {' '.join(body.args)}", "result": result}
    except Exception as exc:
        raise ValidationError(f"Redis error: {exc}")


# ── Bot ───────────────────────────────────────────────────────────────────

@router.get("/bot", dependencies=[Depends(require_admin)])
async def ops_bot(request: Request):
    """Detailed bot diagnostics  -  guilds, cogs, tasks, errors."""
    bot = _get_bot(request)

    uptime_s = time.time() - getattr(bot, "_start_time", time.time())
    hours = int(uptime_s // 3600)
    mins = int((uptime_s % 3600) // 60)

    # Cog details
    cogs = []
    for name, cog in bot.cogs.items():
        # Count commands in this cog
        cmd_count = sum(1 for c in bot.commands if getattr(c, "cog_name", None) == name)
        # Check background tasks
        tasks = []
        for attr_name in dir(cog):
            attr = getattr(cog, attr_name, None)
            if hasattr(attr, "is_running") and callable(getattr(attr, "is_running", None)):
                tasks.append({
                    "name": attr_name,
                    "running": attr.is_running(),
                    "failed": attr.failed() if hasattr(attr, "failed") else False,
                })
        cogs.append({
            "name": name,
            "commands": cmd_count,
            "tasks": tasks,
        })

    # Guild details
    guilds = []
    if bot.is_ready():
        for g in bot.guilds:
            guilds.append({
                "id": str(g.id),
                "name": g.name,
                "members": g.member_count,
                "owner_id": str(g.owner_id) if g.owner_id else None,
            })

    # Recent errors from ErrorTracker
    recent_errors = []
    if hasattr(bot, "errors") and hasattr(bot.errors, "recent"):
        for err in bot.errors.recent[-20:]:
            recent_errors.append({
                "source": err.get("source", "?"),
                "error": err.get("message", "?"),
                "severity": err.get("severity", "?"),
                "module": err.get("module"),
                "timestamp": err.get("timestamp"),
            })

    return {
        "status": "ready" if bot.is_ready() else "connecting",
        "username": str(bot.user) if bot.user else None,
        "user_id": str(bot.user.id) if bot.user else None,
        "uptime": f"{hours}h {mins}m",
        "uptime_seconds": round(uptime_s),
        "latency_ms": round(bot.latency * 1000, 1) if bot.latency else None,
        "guilds": guilds,
        "guild_count": len(guilds),
        "cogs": sorted(cogs, key=lambda c: c["name"]),
        "cog_count": len(cogs),
        "command_count": len(list(bot.commands)),
        "recent_errors": recent_errors,
    }


class CogAction(BaseModel):
    cog: str


@router.post("/bot/reload-cog", dependencies=[Depends(require_admin)])
async def ops_reload_cog(body: CogAction, request: Request):
    """Hot-reload a cog without restarting the bot."""
    bot = _get_bot(request)
    cog_path = body.cog

    # Normalize: accept both "crypto" and "cogs.crypto"
    if not cog_path.startswith("cogs."):
        cog_path = f"cogs.{cog_path}"

    try:
        await bot.reload_extension(cog_path)
        return {"status": "ok", "message": f"Reloaded {cog_path}"}
    except Exception as exc:
        raise ValidationError(f"Failed to reload {cog_path}: {exc}")


@router.post("/bot/sync-commands", dependencies=[Depends(require_admin)])
async def ops_sync_commands(request: Request):
    """Force-sync slash commands to Discord."""
    bot = _get_bot(request)
    try:
        synced = await bot.tree.sync()
        return {"status": "ok", "synced": len(synced)}
    except Exception as exc:
        raise ValidationError(f"Sync failed: {exc}")


# ── API ───────────────────────────────────────────────────────────────────

@router.get("/api", dependencies=[Depends(require_admin)])
async def ops_api(request: Request):
    """API server diagnostics  -  routes, middleware."""
    app = request.app

    # Enumerate routes
    routes = []
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            routes.append({
                "path": route.path,
                "methods": sorted(route.methods) if route.methods else [],
                "name": route.name,
            })

    # Middleware stack
    middleware = []
    mw = app.middleware_stack
    while mw:
        cls_name = type(mw).__name__
        if cls_name not in ("ServerErrorMiddleware",):
            middleware.append(cls_name)
        mw = getattr(mw, "app", None)

    return {
        "version": "2.0.0",
        "route_count": len(routes),
        "routes": sorted(routes, key=lambda r: r["path"]),
        "middleware": middleware,
    }
