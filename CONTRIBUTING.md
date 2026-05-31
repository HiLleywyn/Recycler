# Contributing to Discoin

## Project Structure

```
Discoin/
├── main.py                  # Entry point  -  loads bot and starts event loop
├── items_config.py          # Shop items configuration
├── requirements.txt
│
├── core/                    # Shared core layer (imported as core.*)
│   ├── config.py            # All tuneable constants (economy, tokens, mining, etc.)
│   ├── database.py          # asyncpg connection pool, PgRow result wrapper
│   └── framework/           # Core bot infrastructure
│       ├── bot.py               # Discoin class, cog loader, COGS list
│       ├── context.py           # DiscoContext  -  wraps commands.Context with db/guild helpers
│       ├── embed.py             # Shared embed helpers, color constants
│       ├── redis_bus.py         # Redis-backed pub/sub EventBus for real-time feeds
│       ├── ai/                  # OpenRouter AI client (market maker, chat, commentary)
│       ├── log.py               # Rich-formatted startup/runtime logging
│       ├── middleware.py        # Per-command rate limiting and error handling
│       ├── cooldowns.py         # Cooldown tracking
│       ├── antibot.py           # Anti-bot CAPTCHA system
│       ├── content_filter.py    # Content moderation
│       ├── internal_commands.py # Natural language command interpreter
│       └── session_log.py       # Structured session event log
│
├── cogs/                    # Feature cogs (each is a discord.py Cog)
│   ├── bank.py              # .balance, .deposit, .withdraw, .transfer, .leaderboard
│   ├── crypto.py            # GBM price engine, .buy, .sell, .price, .portfolio
│   ├── trades.py            # EventBus subscriber  -  posts all feed embeds to Discord
│   ├── stake.py             # .stake, .unstake, .staking
│   ├── validators.py        # PoS validator block production and rewards
│   ├── mining.py/chain_group.py  # PoW mining, rig management, .mine
│   ├── chain.py             # Chain block bundler, .block, .txinfo
│   ├── contracts.py         # Smart contract deploy/call system
│   ├── earn.py              # .daily, .work, job progression
│   ├── drops.py             # Auto money drops to channels
│   ├── gamble.py / play.py  # .coinflip, .dice, .blackjack, .slots, .roulette
│   ├── groups.py            # Mining/staking groups
│   ├── shop.py              # Item shop system
│   ├── admin.py             # .admin subcommands (give/take/setprice/log/bundle/etc.)
│   ├── help.py              # .help command and AI chat
│   └── health.py            # Internal health monitoring
│
├── database/                # Database layer (PostgreSQL via asyncpg)
│   ├── database.py          # Database class  -  aggregates all mixins
│   ├── schema.sql           # Full PostgreSQL schema
│   ├── base.py              # Base mixin with connection helpers
│   ├── users.py             # User/wallet CRUD
│   ├── markets.py           # Prices, candles, transactions
│   ├── pools.py             # AMM pool state
│   ├── validators.py        # Validator and staking records
│   ├── mining.py            # Mining rig and block records
│   ├── contracts.py         # Smart contract storage
│   ├── guilds.py            # Guild settings and seed helpers
│   └── transactions.py      # Mempool and transaction log
│
├── api/                     # REST API (FastAPI v2)
│   └── v2/
│       ├── main.py          # FastAPI app setup, mounts all routers
│       ├── config.py        # API settings (pydantic-settings)
│       ├── auth/            # Discord OAuth2 + JWT authentication
│       ├── routers/         # Route handlers (markets, users, admin, mining, etc.)
│       ├── schemas/         # Pydantic models
│       ├── middleware/      # Auth, logging, and rate-limiting middleware
│       ├── services/        # Business logic services
│       └── ws/              # WebSocket handlers
│
├── frontend/                # Next.js dashboard (TypeScript + Tailwind)
│   ├── app/                 # App Router pages and layouts
│   ├── components/          # Reusable UI components
│   ├── hooks/               # React hooks
│   ├── stores/              # Zustand state stores
│   └── package.json
│
├── services/                # Shared business logic
│   ├── net_worth.py         # Consistent net worth calculation (Discord + dashboard)
│   ├── swap.py              # DEX swap logic
│   ├── trade.py             # Trading logic
│   └── transfer.py          # Transfer logic
│
└── scripts/
    └── gen_docs.py          # Generates docs from cog docstrings
```

---

## Adding a New Cog

1. Create `cogs/yourcog.py` with a standard `commands.Cog` subclass
2. Add `"cogs.yourcog"` to the `COGS` list in `core/framework/bot.py`
3. Use `DiscoContext` (from `core/framework/context.py`) instead of `commands.Context`  -  it exposes `ctx.db`, `ctx.guild_id`, and reply helpers
4. Subscribe to events via `self.bot.bus.subscribe("event_name", self._handler)` in `cog_load`
5. Publish events via `await self.bot.bus.publish("event_name", guild=guild, **data)`

---

## EventBus Events

Key events published across cogs:

| Event | Published by | Subscribed by |
|---|---|---|
| `prices_updated` | `crypto.py` | `pools.py` (oracle rebalance) |
| `trade` | `crypto.py` | `trades.py` (feed embed) |
| `swap_trade` | `pools.py` | `trades.py` |
| `lp_added` / `lp_removed` | `pools.py` | `trades.py` |
| `staked` / `unstaked` | `stake.py` | `trades.py` |
| `validator_block` | `validators.py` | `trades.py` |
| `block_bundled` | `chain.py` | `trades.py` (PoS confirmation embed) |
| `block_mined` | `mining.py` | `trades.py` (SUN/MTA block embed) |
| `contract_event` | `contracts.py` | `trades.py` |

All feed embeds are centralized in `cogs/trades.py`  -  if you want to post to a Discord channel in response to an action, publish an event and handle it there.

---

## Database Access

Use `ctx.db` (a `Database` instance) inside cog commands. The database layer uses PostgreSQL via asyncpg with a connection pool. For direct SQL in API routes, use `db.pool` (the asyncpg pool exposed on the shared `Database` instance).

The schema lives in `database/schema.sql`. Migrations are applied via the `.admin migrate` command or the `cogs/migrate.py` cog.

---

## Config Changes

All economy constants are in `core/config.py` as class attributes on `Config`. They are read once at startup. To make a value runtime-configurable, read it from `.env` via `os.getenv()` like the existing API/token settings.
