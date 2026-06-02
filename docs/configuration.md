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
| `PREFIX` | `.` | Command prefix. Commands become `.backup`, `.template`, ... A per-guild override can be set with `.set prefix`. |
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

## Optional

| Variable | Default | What it is |
|---|---|---|
| `REDIS_URL` | -- | Enables the framework's Redis-backed features when present. |
| `DB_SSL_VERIFY` | `0` | Set to `1` to require full TLS certificate verification on the DB connection. |

## Build-time (Docker / Railway)

| Build arg | Default | What it is |
|---|---|---|
| `FRAMEWORK_REF` | `main` | Git ref of the framework to install. |
