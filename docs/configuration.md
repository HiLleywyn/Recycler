# Configuration

Every setting is an environment variable (locally via `.env`, on Railway via
service variables, on Sojourns via the settings UI generated from
`sojourns.json`). Defaults are shown where they exist.

## Required

| Variable | What it is |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Discord developer portal. |
| `DATABASE_URL` | PostgreSQL DSN, e.g. `postgresql://user:pass@host:5432/recycler`. Migrations run on boot. |

## Core

| Variable | Default | What changes if you flip it |
|---|---|---|
| `PREFIX` | `.` | Command prefix. Commands become `.backup`, `.clank`, ... A per-guild override can be set with `.set prefix`. |
| `API_PORT` | `8080` | Port for the embedded REST API + `/health`. |
| `DEBUG` | `false` | Verbose logging and relaxed production guards. |
| `DISCORD_CLIENT_ID` | -- | Used only to build the invite URL before the bot is logged in. |

## REST API

| Variable | Default | What changes if you flip it |
|---|---|---|
| `CLANK_API_KEY` | -- | When set, enables `/api/v2/*`; requests must send `X-API-Key`. When unset, only `/health` is served. |

## Backups

| Variable | Default | What changes if you flip it |
|---|---|---|
| `BACKUP_MAX_PER_USER` | `50` | Soft cap on stored backups per user (abuse prevention, not a paywall). |

## Containment (`.clank`)

These mirror the ported Discoin behaviour; channel/role values are Discord ids.
Per-guild overrides for the containment channels can also be set with
`.set containment` / `.set containmentlog`.

| Variable | Default | What it is |
|---|---|---|
| `CLANKER_ROLE_ID` | -- | The role applied to contained accounts. |
| `CLANKTANK_CHANNEL_ID` | -- | The "tank" channel contained users are limited to. |
| `CLANKTANK_LOG_CHANNEL_ID` | -- | Mod log channel for containment events (optional). |
| `CLANK_ESCAPE_THREAD_ID` | -- | Shared escape-room thread (optional). Can be set live with `.clank er setthread`. |
| `CLANK_ESCAPE_WAIT_MINUTES` | `8` | Reflection wait before the escape room opens. |

## Optional

| Variable | Default | What it is |
|---|---|---|
| `REDIS_URL` | -- | Enables the framework's Redis-backed features when present. |
| `DB_SSL_VERIFY` | `0` | Set to `1` to require full TLS certificate verification on the DB connection. |

## Build-time (Docker / Railway)

| Build arg | Default | What it is |
|---|---|---|
| `FRAMEWORK_REF` | `main` | Git ref of the framework to install. |
