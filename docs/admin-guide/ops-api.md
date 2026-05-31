# Admin Ops API

Remote management endpoints for Postgres, Redis, the Discord bot, and the API server itself.
All endpoints live under `/api/v2/admin/ops/` and require an admin JWT (`is_admin` or `is_owner` claim).

---

## Authentication

Include a Bearer token in every request:

```http
Authorization: Bearer <your_admin_jwt>
```

Admin accounts receive a 240 req/10s rate limit (vs 60 for regular users).

---

## Endpoints

### GET `/api/v2/admin/ops/overview`

Single-call status snapshot across all four services. Good for a health-check dashboard.

**Response:**
```json
{
  "ts": "2026-04-01T12:00:00Z",
  "postgres": {
    "status": "connected",
    "version": "PostgreSQL 16.2",
    "tables": 42,
    "size": "128 MB"
  },
  "redis": {
    "status": "connected",
    "memory_used": "14.50M",
    "memory_peak": "15.20M",
    "total_keys": 1024
  },
  "bot": {
    "status": "ready",
    "username": "Discoin#1234",
    "guilds": 3,
    "cogs": 12,
    "uptime_seconds": 86400,
    "latency_ms": 42.5
  },
  "api": {
    "status": "running",
    "version": "2.0.0",
    "routes": 87
  }
}
```

If a service is unavailable its object will contain `"status": "error"` or `"status": "unavailable"`  -  the endpoint itself always returns `200`.

---

### GET `/api/v2/admin/ops/postgres`

Detailed Postgres diagnostics.

**Response:**
```json
{
  "version": "PostgreSQL 16.2 on x86_64-pc-linux-gnu",
  "database_size": "128 MB",
  "connections": {
    "active": 5,
    "idle": 2,
    "idle in transaction": 0
  },
  "tables": [
    {"table": "transactions", "rows": 500000, "size": "48 MB"},
    {"table": "users", "rows": 12000, "size": "4096 kB"}
  ],
  "slow_queries": [
    {"query": "SELECT ...", "calls": 120, "avg_ms": 45.2, "total_ms": 5424.0}
  ],
  "applied_migrations": 34,
  "latest_migration": "0034_add_validator_slash_index.sql"
}
```

- `tables`  -  top 20 by total disk usage (table + indexes + TOAST)
- `slow_queries`  -  top 10 by average execution time; only populated if `pg_stat_statements` is enabled
- `connections`  -  grouped by `pg_stat_activity.state`

---

### POST `/api/v2/admin/ops/postgres/query`

Execute a read-only SQL query against the live database.

**Request body:**
```json
{"sql": "SELECT id, username, created_at FROM users ORDER BY created_at DESC LIMIT 5"}
```

**Response:**
```json
{
  "row_count": 5,
  "rows": [
    {"id": 1001, "username": "Alice", "created_at": "2026-03-30T10:00:00Z"}
  ]
}
```

**Safety rules:**
- Only `SELECT`, `EXPLAIN`, `WITH`, and `SHOW` are accepted as the first keyword
- Any query containing `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `CREATE`, `GRANT`, `REVOKE`, or `COPY` is rejected with `400`
- Results are capped at 500 rows

---

### GET `/api/v2/admin/ops/redis`

Detailed Redis diagnostics including memory, client stats, and key namespace sampling.

**Response:**
```json
{
  "version": "7.2.4",
  "uptime_seconds": 604800,
  "memory": {
    "used": "14.50M",
    "peak": "15.20M",
    "max": "0B",
    "fragmentation_ratio": 1.08
  },
  "clients": {
    "connected": 4,
    "blocked": 0
  },
  "stats": {
    "total_commands_processed": 9820341,
    "ops_per_sec": 142,
    "hit_rate": 94.3
  },
  "total_keys": 1024,
  "key_prefixes": {
    "price": 48,
    "user": 312,
    "session": 200
  },
  "sample_keys": ["price:ARC", "user:1001:portfolio"]
}
```

- `key_prefixes`  -  top 15 prefixes (text before the first `:`) from a 100-key scan
- `sample_keys`  -  up to 20 raw key names from the same scan
- `hit_rate`  -  `keyspace_hits / (keyspace_hits + keyspace_misses)` as a percentage

---

### POST `/api/v2/admin/ops/redis/command`

Execute a safe Redis command.

**Request body:**
```json
{"command": "HGETALL", "args": ["user:1001:portfolio"]}
```

**Response:**
```json
{
  "command": "HGETALL user:1001:portfolio",
  "result": {"ARC": "2.5", "MTA": "0.01"}
}
```

**Allowed commands:** `GET`, `MGET`, `KEYS`, `SCAN`, `TYPE`, `TTL`, `PTTL`, `EXISTS`, `STRLEN`, `LLEN`, `SCARD`, `ZCARD`, `HLEN`, `HGETALL`, `HKEYS`, `LRANGE`, `SMEMBERS`, `ZRANGE`, `INFO`, `DBSIZE`, `PING`, `MEMORY`, `OBJECT`, `DEBUG`

**Blocked commands (mutation/destructive):** `DEL`, `FLUSHDB`, `FLUSHALL`, `SET`, `MSET`, `HSET`, `LPUSH`, `RPUSH`, `SADD`, `ZADD`, `EXPIRE`, `PERSIST`, `RENAME`, `CONFIG`, `SHUTDOWN`, `SLAVEOF`, `REPLICAOF`, `BGSAVE`

Any command not on the allowed list returns `400`.

---

### GET `/api/v2/admin/ops/bot`

Full Discord bot diagnostics.

**Response:**
```json
{
  "status": "ready",
  "username": "Discoin#1234",
  "user_id": "987654321",
  "uptime": "1d 2h 15m",
  "uptime_seconds": 94500,
  "latency_ms": 42.5,
  "guild_count": 3,
  "guilds": [
    {"id": "111222333", "name": "My Server", "members": 500, "owner_id": "444555666"}
  ],
  "cog_count": 12,
  "cogs": [
    {
      "name": "Economy",
      "commands": 14,
      "tasks": [
        {"name": "price_tick", "running": true, "failed": false}
      ]
    }
  ],
  "command_count": 87,
  "recent_errors": [
    {
      "source": "command",
      "error": "User not found",
      "severity": "warning",
      "module": "trading",
      "timestamp": "2026-04-01T11:58:00Z"
    }
  ]
}
```

- `recent_errors`  -  last 20 entries from the bot's ErrorTracker
- `tasks`  -  background `discord.ext.tasks` loops within each cog, with running/failed state

Returns `503` if the API is running without an attached bot instance.

---

### POST `/api/v2/admin/ops/bot/reload-cog`

Hot-reload a cog without restarting the bot. Useful for applying code changes to a single module.

**Request body:**
```json
{"cog": "crypto"}
```

Both `"crypto"` and `"cogs.crypto"` are accepted  -  the handler normalizes to the full path automatically.

**Response:**
```json
{"status": "ok", "message": "Reloaded cogs.crypto"}
```

Returns `400` if the cog name is invalid or the reload fails (e.g. syntax error in the new code).

---

### POST `/api/v2/admin/ops/bot/sync-commands`

Force-sync slash commands with Discord's API. Run this after deploying changes to slash command definitions.

**Request body:** none required

**Response:**
```json
{"status": "ok", "synced": 24}
```

Returns `500` if Discord rejects the sync (e.g. rate limited or token issue).

---

### GET `/api/v2/admin/ops/api`

Introspect the live FastAPI application  -  enumerate all registered routes and the active middleware stack.

**Response:**
```json
{
  "version": "2.0.0",
  "route_count": 87,
  "routes": [
    {"path": "/api/v2/admin/ops/api", "methods": ["GET"], "name": "ops_api"},
    {"path": "/api/v2/market/prices", "methods": ["GET"], "name": "get_prices"}
  ],
  "middleware": [
    "RateLimitMiddleware",
    "CORSMiddleware",
    "EventPublishingMiddleware"
  ]
}
```

Routes and middleware reflect the running process  -  no server restart needed to see changes.

---

## Error Responses

| Status | Meaning |
|--------|---------|
| `400` | Bad request  -  invalid SQL, forbidden keyword, or unsafe Redis command |
| `403` | JWT missing `is_admin` / `is_owner` claim |
| `503` | Dependent service unavailable (bot not attached, Redis not connected) |
| `500` | Unexpected server error (e.g. Discord sync failure) |
