# Developer Guide

This page is for people running or contributing to Discoin. If you only
want to configure the bot inside your Discord server, see
[Server Administration](Server-Administration) instead.

Discoin is a Discord economy bot built with discord.py (prefix and slash
commands), asyncpg (PostgreSQL), and a FastAPI v2 REST API with a web
dashboard. It runs as a single Docker container against an external
PostgreSQL 18 and Redis instance.

## Running Discoin

### Docker (recommended)

Docker is the simplest way to run Discoin. The dashboard frontend is built
inside the image, so no Node.js is required on the host.

```bash
git clone https://github.com/HiLleywyn/Discoin.git
cd Discoin
cp .env.example .env        # fill in your values
docker compose up -d --build
```

The dashboard is served at `http://localhost:8080/dashboard` and the health
check at `http://localhost:8080/health`.

### Manual setup

Requirements: Python 3.11+, PostgreSQL, Redis, Node.js 18+ (for the
dashboard), and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/HiLleywyn/Discoin.git
cd Discoin
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install "playwright>=1.40.0"
playwright install chromium && playwright install-deps chromium
cp .env.example .env        # fill in your values
python main.py
```

Playwright with Chromium (about 500MB) powers the chart command. To build
the dashboard separately:

```bash
cd frontend && npm ci && npm run build && cd ..
```

If you skip the dashboard build, the bot and API still work -- only the web
UI is missing.

### Discord Developer Portal

Create an application in the Discord Developer Portal, add a bot, and copy
the token into `DISCORD_TOKEN`. Enable all three Privileged Gateway Intents
(Presence, Server Members, Message Content). For the dashboard, enable
OAuth2, add a redirect URL matching `DISCORD_REDIRECT_URI`, and copy the
Client ID and Client Secret into your `.env`.

## Environment variables

Copy `.env.example` to `.env` and fill in your values. The most important
variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | -- | Bot token |
| `PREFIX` | | `$` | Default command prefix (overridable per guild) |
| `DATABASE_URL` | yes | -- | PostgreSQL connection string |
| `REDIS_URL` | | `redis://localhost:6379` | Redis for the event bus and API pub/sub |
| `TX_SALT` | | -- | Salt for transaction hashes; set before first run, never change it |
| `API_PORT` | | `8080` | Port for the dashboard and REST API |
| `DISCORD_CLIENT_ID` | yes | -- | OAuth2 Client ID (dashboard login) |
| `DISCORD_CLIENT_SECRET` | yes | -- | OAuth2 Client Secret (dashboard login) |
| `DISCORD_REDIRECT_URI` | yes | -- | Must match the redirect URL in the Developer Portal exactly |
| `JWT_SECRET` | yes | -- | Secret used to sign dashboard session tokens |
| `OPENROUTER_API_KEY` | | -- | Enables AI features and scam detection; blank disables them |
| `SLASH_GUILD_ID` | | -- | Guild ID for instant slash sync during development |
| `HOST_GUILD_ID` | | -- | Home server, auto-unlocked from premium |
| `BOT_OWNER_ID` | | -- | Bot owner, can grant premium manually |
| `DEBUG` | | `false` | Enables debug commands; keep `false` in production |

See `.env.example` for the full list, including economy tuning, faucet,
backup, PayPal/premium, and AI toggles.

## Layered configuration

Discoin uses a layered config system. Knowing which layer owns a value
tells you where to change it.

| Layer | Location | What it controls | How to override |
|---|---|---|---|
| Business constants | `constants/` package | Rates, limits, fees, colors, game rules | Edit the Python file |
| Runtime config | `config.py` | Tokens, networks, pools, env-loaded settings | `.env` variables |
| Security thresholds | `security/config.py` | Abuse detection windows and scoring | `SEC_*` env variables |
| Per-server settings | Database `guild_settings` table | Prefix, channels, module toggles | `,admin` commands |

`config.py` stays at the repo root and holds `Config.TOKENS` (the canonical
token and network definitions) and `Config.SHOP_ITEMS`. Per-domain config
modules live in `configs/` (for example `configs/items_config.py`,
`configs/sage_config.py`, `configs/buddies_config.py`). Import them as
`from configs import items_config`.

## Repository layout

| Path | Contents |
|---|---|
| `main.py` | Entry point; loads the bot and starts the event loop |
| `config.py` | Tokens, networks, pools, env-loaded runtime settings |
| `constants/` | Pure Python business constants (zero framework imports) |
| `configs/` | Per-domain config modules |
| `framework/` | Core bot infrastructure (bot, context, embeds, UI, event bus) |
| `cogs/` | Feature cogs, one per system (each is a discord.py Cog) |
| `services/` | Shared business logic (net worth, swap, trade, transfer) |
| `database/` | Database layer via asyncpg |
| `database/migrations/` | Numbered SQL migrations run in order on startup |
| `api/v2/` | FastAPI REST API (routers, schemas, auth, services, WebSockets) |
| `frontend/` | Web dashboard |

## Framework conventions

Contributors must follow these conventions. They are enforced by the
project guidelines in `CLAUDE.md`.

### Use the framework, never reimplement it

Before writing any formatting, embed, or helper logic, check `framework/`
for an existing function. Never reimplement what the framework already
provides, and never copy-paste logic between cogs -- if a value or helper
is used in more than one place, it belongs in a shared location and is
imported everywhere.

### Embeds: always use `card()`

Never construct `discord.Embed()` directly. Use the `card()` builder from
`framework/embed.py`:

```python
from framework.embed import card
embed = card("Title", description="Body", color=C_INFO).field("K", "V", True).build()
```

### Formatting helpers

Use the formatting functions from `framework/ui.py` -- never roll your own.
These include `fmt_usd()`, `fmt_token()`, `fmt_ts()`, `fmt_pct()`,
`fmt_gas()`, and `fmt_bonus()`. Use the `C_*` color constants from
`framework/ui.py` rather than raw hex values.

### Context reply helpers

Commands receive a `DiscoContext`. Use its reply helpers
(`ctx.reply_error()`, `ctx.reply_success()`, `ctx.reply_cooldown()`,
`ctx.confirm()`, `ctx.paginate()`) instead of building raw embeds.

### Database access

Go through `ctx.db`: `fetch_one()`, `fetch_all()`, `fetch_val()`,
`execute()`. DB timestamps come back as epoch floats -- always render them
with `fmt_ts()`. Monetary columns are raw `NUMERIC(36,0)` scaled by
`10**18`; read them with `row.h("col")`.

### Net worth

Net worth is computed in exactly one place: `services/net_worth.py`. Every
display of net worth calls `compute_net_worth()` or
`compute_bulk_net_worth()`. Never re-derive it inline.

### ASCII-only source files

Use plain ASCII hyphens only. Em dashes, en dashes, and Unicode minus signs
have caused silent failures in string matching and shell scripts. Run a
check before committing.

### Branch naming

Use `type/Major.Minor.Patch.Hotfix`. The branch type determines which
segment increments: `major/2.0.0.0`, `minor/1.8.0.0`, `patch/1.7.4.0`,
`hotfix/1.7.3.3`. Always use all four segments; never use random slugs.

### CHANGELOG discipline

Every commit that touches user-visible behavior must also update
`CHANGELOG.md` in the same commit. The in-bot `,changelog` command reads
this file at runtime, so anything shipped without an entry is invisible to
players. Add one bullet per logical change under the appropriate heading.
Pure refactors, typo fixes, and non-runtime tweaks can skip the changelog.

## Adding a new feature or cog

1. Create `cogs/yourcog.py` with a `commands.Cog` subclass.
2. Add `"cogs.yourcog"` to the `COGS` list in `framework/bot.py`. Order
   matters for cogs that subscribe to bus events -- load subscribers after
   publishers.
3. Use `DiscoContext` (from `framework/context.py`) as the command context;
   it exposes `ctx.db`, `ctx.guild_id`, and the reply helpers.
4. Stack middleware decorators in order: `@guild_only`, `@no_bots`,
   `@ensure_registered`, `@user_cooldown(seconds)`.
5. Put pure business logic in `services/` (no discord imports there).
6. For background tasks, use `@tasks.loop`, wrap the tick body in
   `try/except`, wait for `bot.wait_until_ready()` in `before_loop`, call
   `heartbeat.pulse()` on success, and cancel the loop in `cog_unload`.
7. Cross-cog communication goes through the Redis event bus
   (`bot.bus.publish()` and `bot.bus.subscribe()`); always unsubscribe in
   `cog_unload`. Add events to `REDIS_BROADCAST_EVENTS` if they need to
   reach the API server.
8. New schema changes need a numbered migration in
   `database/migrations/`.

## See also

- [Server Administration](Server-Administration)
- [Getting Started](Getting-Started)
- [Commands](Commands)
- [FAQ](FAQ)
