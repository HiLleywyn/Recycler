# Extending Discoin  -  Developer Guide

This guide covers every building block available when adding new features to Discoin. All example files live in `cogs/examples/` and are fully runnable once registered in `COGS`.

---

## Concepts at a glance

| Concept | Discoin equivalent | Lives in |
|---|---|---|
| Adapter (platform connection) | `commands.Cog` | `cogs/` |
| Profile (isolated identity) | Guild settings row | `database/` via `ctx.db` |
| Worker (background task) | `@tasks.loop` inside a Cog | `cogs/` |
| Context bus (events) | `bot.bus` (`RedisBus`) | `core/framework/redis_bus.py` |
| Request context | `DiscoContext` | `core/framework/context.py` |
| Service layer (business logic) | Functions in `services/` | `services/` |

---

## 1. Cog (Adapter)

A Cog is a self-contained module of commands. It's the entry point for a feature  -  handling user input, reading/writing the database, and publishing events.

### Minimal skeleton

```python
# cogs/my_feature.py
from discord.ext import commands
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import guild_only, ensure_registered

class MyFeature(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.command(name="mycommand")
    @guild_only
    @ensure_registered
    async def my_command(self, ctx: DiscoContext) -> None:
        await ctx.reply_success("It works!")

async def setup(bot: Discoin) -> None:
    await bot.add_cog(MyFeature(bot))
```

### Registering your cog

Add it to the `COGS` list in `core/framework/bot.py`:

```python
COGS = [
    ...
    "cogs.my_feature",  # ← add here
]
```

Order matters for cogs that subscribe to bus events  -  load subscribers **after** publishers.

### Middleware decorators

Stack these on any command in this order:

```python
@commands.command(name="cmd")
@guild_only          # 1. Reject DMs
@ensure_registered   # 2. Auto-register user, populate ctx.user_row
async def cmd(self, ctx: DiscoContext) -> None:
    ...
```

| Decorator | Effect |
|---|---|
| `@guild_only` | Rejects DMs  -  required on almost every economy command |
| `@ensure_registered` | Creates a user row if missing, sets `ctx.user_row` |
| `@commands.cooldown(1, 5, commands.BucketType.user)` | Standard discord.py cooldown |

### Command groups

```python
@commands.group(name="mygroup", invoke_without_command=True)
@guild_only
async def mygroup(self, ctx: DiscoContext) -> None:
    await ctx.send_group_help(self.mygroup)  # shows help if no subcommand given

@mygroup.command(name="sub")
@guild_only
async def mygroup_sub(self, ctx: DiscoContext) -> None:
    await ctx.reply_success("Subcommand ran.")
```

### Database access

All queries go through `ctx.db`:

```python
# Raw queries
rows = await ctx.db.fetch_all("SELECT * FROM users WHERE guild_id = $1", ctx.guild.id)
row  = await ctx.db.fetch_one("SELECT * FROM users WHERE user_id = $1", ctx.author.id)
val  = await ctx.db.fetch_val("SELECT COUNT(*) FROM transactions WHERE guild_id = $1", ctx.guild.id)
await ctx.db.execute("UPDATE users SET score = score + $1 WHERE user_id = $2", 10, ctx.author.id)

# Repository shortcuts
balance  = await ctx.db.users.get_balance(ctx.author.id, ctx.guild.id, "USD")
settings = await ctx.db.get_guild_settings(ctx.guild.id)
```

### Reply helpers

```python
await ctx.reply_success("Done!")                          # green embed
await ctx.reply_error("Something went wrong.")            # red embed
await ctx.reply_error_hint("Bad args.", hint=".buy 10 ARC")  # red + hint
await ctx.reply_error_action(                             # red + action button
    "You need a wallet first.",
    button_label="Create Wallet",
    command="wallet create",
)
await ctx.reply_cooldown(5.0)                             # amber cooldown notice

confirmed = await ctx.confirm("Are you sure?")            # yes/no prompt → bool
await ctx.paginate([embed1, embed2, embed3])              # paginated viewer
```

### Full example

See [`cogs/examples/example_cog.py`](../../cogs/examples/example_cog.py)

---

## 2. Guild Settings (Profile)

There is no `Profile` class. Each guild's configuration is a row in `guild_settings`, keyed by `guild_id`. Think of each guild as its own profile.

### Reading settings

```python
settings = await ctx.db.get_guild_settings(ctx.guild.id)

prefix   = settings.get("prefix") or Config.PREFIX
color    = settings.get("embed_color") or 0x2ecc71
currency = settings.get("currency_name") or "USD"

# Channel IDs for feed channels
trade_channel_id = settings.get("trade_channel")
error_channel_id = settings.get("error_channel")
```

### Writing settings

```python
await ctx.db.execute(
    "UPDATE guild_settings SET prefix = $1 WHERE guild_id = $2",
    ".new", ctx.guild.id,
)
```

### Adding a new setting

1. Add the column to `database/schema.sql`
2. Add a migration in `database/migrations/`
3. Read/write it with `ctx.db.execute` and `ctx.db.get_guild_settings`
4. Expose it in `.admin` commands if admins need to configure it

### Available settings keys

| Key | Type | Purpose |
|---|---|---|
| `prefix` | `text` | Command prefix for this guild |
| `embed_color` | `int` | Default embed accent color |
| `currency_name` | `text` | Display name for the base currency |
| `trade_channel` | `bigint` | Channel ID for trade feed |
| `error_channel` | `bigint` | Channel ID for error feed |
| `mine_channel` | `bigint` | Channel ID for mining feed |
| `error_feed_levels` | `text` | Comma-separated severity levels to post |
| `cmd_delete_after` | `int` | Seconds before command messages auto-delete |
| `bot_channels` | `text` | Comma-separated channel IDs for no-prefix mode |

Full list: see `database/schema.sql` → `guild_settings` table.

---

## 3. Worker (Background Task)

Workers are background loops that run on a fixed schedule. They use `discord.ext.tasks.loop` inside a Cog.

### Skeleton

```python
from discord.ext import commands, tasks
from framework import heartbeat
from core.framework.bot import Discoin

class MyWorker(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        heartbeat.expect("my_worker")
        heartbeat.register_interval("my_worker", 60.0)
        self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        try:
            await self._do_work()
        except Exception:
            _log.exception("Worker tick failed")

    async def _do_work(self) -> None:
        for guild in self.bot.guilds:
            await self.bot.db.execute("UPDATE ...", guild.id)
        heartbeat.pulse("my_worker")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

async def setup(bot: Discoin) -> None:
    await bot.add_cog(MyWorker(bot))
```

### Key rules

- **Always wrap tick body in `try/except`**  -  an unhandled exception stops the loop permanently. `self_heal` will restart it, but logging first is essential.
- **Always call `heartbeat.pulse("name")`** at the end of a successful tick  -  this is how `.health check` and the self-heal scheduler know the worker is alive.
- **Always call `bot.wait_until_ready()` in `before_loop`**  -  prevents the loop from firing during `setup_hook` before guilds are available.
- **Always cancel in `cog_unload`**  -  prevents orphaned asyncio tasks on cog reload.

### Loop interval options

```python
@tasks.loop(seconds=30)          # every 30 seconds
@tasks.loop(minutes=5)           # every 5 minutes
@tasks.loop(hours=1)             # every hour
@tasks.loop(time=datetime.time(hour=0, minute=0))  # daily at midnight UTC
```

### Heartbeat integration

The heartbeat system tracks whether workers are alive. Register once in `__init__`, pulse at the end of each tick:

```python
heartbeat.expect("my_worker")                  # declare it should pulse
heartbeat.register_interval("my_worker", 60.0) # expected interval in seconds
heartbeat.pulse("my_worker")                   # call at end of each tick
```

`.health check` will flag it as stale after 3× the interval (180s for a 60s worker). `.health heal` will call `tick.restart()` automatically.

### Full example

See [`cogs/examples/example_worker.py`](../../cogs/examples/example_worker.py)

---

## 4. Event Bus

`bot.bus` is a Redis-backed pub/sub bus. Events published by one cog are delivered to all subscribers, and (for events in `REDIS_BROADCAST_EVENTS`) broadcast to the FastAPI server and WebSocket clients.

### Publishing

```python
await self.bot.bus.publish(
    "trade_executed",          # event name
    guild=ctx.guild,           # discord.Guild object
    data={                     # arbitrary payload
        "user_id": ctx.author.id,
        "symbol":  "ARC",
        "amount":  10.0,
        "action":  "buy",
    },
)
```

### Subscribing

```python
class MyListener(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        bot.bus.subscribe("trade_executed", self._on_trade)

    def cog_unload(self) -> None:
        self.bot.bus.unsubscribe("trade_executed", self._on_trade)

    async def _on_trade(self, guild: discord.Guild, data: dict, **kwargs) -> None:
        # react to the trade
        print(f"Trade in {guild.name}: {data}")
```

!!! warning "Always unsubscribe in `cog_unload`"
    If you forget to unsubscribe, the callback remains registered after the cog is unloaded. On a cog reload this creates duplicate callbacks. Always pair `subscribe` with `unsubscribe`.

### Making an event cross to the API / WebSocket

Add it to `REDIS_BROADCAST_EVENTS` in `core/framework/redis_bus.py`:

```python
REDIS_BROADCAST_EVENTS = frozenset({
    ...
    "my_custom_event",  # ← add here to broadcast via Redis
})
```

Events **not** in this set are in-memory only (bot process only, not visible to the API server).

### Built-in events

| Event | Published by | Payload keys |
|---|---|---|
| `trade_executed` | `cogs/trade.py` | `user_id`, `symbol`, `amount`, `action` |
| `swap_executed` | `cogs/trade.py` | `user_id`, `from_symbol`, `to_symbol`, `amount_in`, `amount_out` |
| `stake_created` | `cogs/stake.py` | `user_id`, `symbol`, `amount` |
| `block_mined` | `cogs/earn.py` | `user_id`, `symbol`, `reward`, `block_height` |
| `game_result` | `cogs/play.py` | `user_id`, `game`, `won`, `pnl` |
| `transfer_sent` | `cogs/bank.py` | `from_user`, `to_user`, `amount`, `symbol` |
| `security_alert` | `cogs/security.py` | `user_id`, `type`, `detail` |
| `market_event_started` | `cogs/events.py` | `event_id`, `event_type`, `affected_tokens` |
| `prices_updated` | `cogs/crypto.py` | `guild_id`, `prices` dict |
| `drop_spawned` | `cogs/drops.py` | `guild_id`, `token`, `amount` |
| `badge_earned` | various | `user_id`, `badge_id`, `badge_name` |

### Full example

See [`cogs/examples/example_listener.py`](../../cogs/examples/example_listener.py)

---

## 5. DiscoContext

`DiscoContext` is automatically injected as `ctx` into every command. You never instantiate it yourself.

### Full API reference

```python
async def my_command(self, ctx: DiscoContext) -> None:

    # ── Database ──────────────────────────────────────────────────────
    ctx.db                                    # Database object
    await ctx.db.execute(sql, *args)          # run a query
    await ctx.db.fetch_all(sql, *args)        # → list[dict]
    await ctx.db.fetch_one(sql, *args)        # → dict | None
    await ctx.db.fetch_val(sql, *args)        # → scalar | None
    await ctx.db.get_guild_settings(id)       # ��� dict

    # Repository shortcuts
    ctx.db.users          # UserRepository
    ctx.db.markets        # MarketsRepository
    ctx.db.pools          # PoolsRepository
    ctx.db.validators     # ValidatorsRepository
    ctx.db.mining         # MiningRepository
    ctx.db.transactions   # TransactionsRepository

    # ── Identity ──────────────────────────────────────────────────────
    ctx.author            # discord.Member
    ctx.user_row          # dict  -  populated by @ensure_registered
    ctx.guild             # discord.Guild
    ctx.guild_id          # int shorthand for ctx.guild.id
    ctx.channel           # discord.TextChannel | Thread
    ctx.message           # discord.Message
    ctx.prefix            # str  -  prefix used to invoke this command
    ctx.is_chain_step     # bool  -  True inside a chain replay

    # ── Replies ───────────────────────────────────────────────────────
    await ctx.reply_success("Done!")
    await ctx.reply_error("Failed.")
    await ctx.reply_error_hint("Bad args.", hint=".buy 10 ARC")
    await ctx.reply_error_action("Need wallet.", button_label="Create", command="wallet create")
    await ctx.reply_cooldown(5.0)
    await ctx.send_embed(embed)
    await ctx.send_group_help(self.mygroup)

    # ── UI ────────────────────────────────────────────────────────────
    confirmed = await ctx.confirm("Are you sure?", timeout=30.0)  # bool
    await ctx.paginate([embed1, embed2, embed3])                   # paginated viewer

    # ── Misc ──────────────────────────────────────────────────────────
    prefix = await ctx.get_guild_prefix()     # guild-configured prefix
```

### Adding a property to DiscoContext

Edit `core/framework/context.py` and add a `@property` inside `DiscoContext`:

```python
@property
def my_service(self) -> "MyService":
    return self.bot.my_service
```

### Adding a service to the bot

1. Create your service in `services/my_service.py`
2. Attach it in `core/framework/bot.py` inside `__init__`:

```python
from services.my_service import MyService

def __init__(self) -> None:
    ...
    self.my_service = MyService()
```

3. Access it from commands via `ctx.bot.my_service` or add a `DiscoContext` property.

### Full example & annotations

See [`cogs/examples/example_context.py`](../../cogs/examples/example_context.py)

---

## Self-Heal Integration

Any background worker you create gets **automatic crash recovery** from `core/framework/self_heal.py` with no extra configuration  -  as long as you:

1. Use `@tasks.loop` (it scans all cog loops)
2. Call `heartbeat.pulse("name")` at the end of each tick

The self-heal scheduler runs every 60s and will call `.restart()` on any loop that has `failed()` or is not running. It also monitors Redis bus connectivity and reconnects with exponential backoff if Redis drops.

From Discord, run `.health heal` to trigger an immediate recovery scan. Run `.health check` for a read-only diagnostic.

---

## Quick Reference

### File checklist for a new feature

```
cogs/my_feature.py           ← commands + event handling
services/my_feature.py       ← pure business logic (no discord imports)
database/my_feature.py       ← SQL queries (add to Database class in database/database.py)
database/schema.sql          ← new tables / columns
database/migrations/         ← migration script for the schema change
```

### Checklist before opening a PR

- [ ] Cog registered in `COGS` in `core/framework/bot.py`
- [ ] All commands decorated with at minimum `@guild_only`
- [ ] Background loop has `before_loop` waiting for `bot.wait_until_ready()`
- [ ] Background loop has `try/except` in the tick body
- [ ] Background loop calls `heartbeat.pulse("name")` on success
- [ ] Background loop calls `tick.cancel()` in `cog_unload`
- [ ] Bus subscribers call `unsubscribe` in `cog_unload`
- [ ] New bus events added to `REDIS_BROADCAST_EVENTS` if they need to reach the API
- [ ] New schema columns have a migration in `database/migrations/`
