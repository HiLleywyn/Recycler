# Discoin -- Claude Code Guidelines

## Hard rules -- never break these

### Use the existing framework -- no ad-hoc reimplementations
Before writing any formatting, utility, or helper logic, check `core/framework/ui.py`,
`core/framework/embed.py`, and other `core/framework/` modules for existing functions.
Never reimplement what the framework already provides. Use the exact same patterns
and formatting conventions used in other cogs. Inconsistent code formatting and
one-off reimplementations of existing utilities are not acceptable.

### Git commit hygiene
- **Never** include `https://claude.ai/code/session_*` links in commit messages.
- **Always** set both author AND committer to `HiLleywyn <lleywyn@proton.me>` on every commit.
  Use `git -c user.name="HiLleywyn" -c user.email="lleywyn@proton.me" commit` or equivalent.
- **Never** leave `Claude <noreply@anthropic.com>` as author or committer on any commit.

### CHANGELOG.md is updated on every commit
Every commit that touches user-visible behaviour MUST also update `CHANGELOG.md`
in the same commit. The in-bot `,changelog` command reads this file at runtime,
so anything shipped without a CHANGELOG entry is invisible to players. Workflow:
- Add ONE bullet per logical change, sorted under the appropriate heading
  (`### New Features`, `### Bug Fixes`, `### Documentation`, `### Changes`).
- Format: `**Short title**: 1-2 sentence description focused on the WHY and the
  player-visible effect.` Do not append `(commit_hash)` -- the hash gets stamped
  in on the next commit pass once it exists.
- If today's date doesn't have a `## [main] -- YYYY-MM-DD` header yet, add one
  immediately under the `# Changelog` line above the previous date.
- Pure refactors / typo fixes / non-runtime tweaks (tests, formatting, dev
  scripts, AI prompts) can skip CHANGELOG.

### burn_rate in Config.TOKENS is applied to ALL trades for built-in tokens
`Config.TOKENS[symbol]["burn_rate"]` is the canonical burn rate for each token.
The trade flow in `cogs/trade.py` reads this for built-in tokens via:
`_buy_burn_rate = _buy_params.get("burn_rate", 0.0) or Config.TOKENS.get(symbol, {}).get("burn_rate", 0.0)`
Never set burn_rate to 0 in config and rely on a separate mechanism -- update the config directly.

### No legacy naming or code
Never leave legacy names, column names, variable names, or dead code in place.
If something was named for a system that no longer exists (e.g. `staked_sun` when
the shop now uses stablecoins), rename it immediately with a migration.
Stale names cause confusion, hide bugs, and make future work harder.

When removing a feature, item, or system: search the ENTIRE codebase for every
reference -- shop listings, inventory embeds, profile displays, help text, docs,
stat labels, config lookups, function parameters, error messages, embed fields,
select menus, aliases, and any other mention. Remove ALL of them. Never leave
orphaned code, dead parameters, commented-out blocks, stale help text, or
partial references. If it's removed, it's gone from everywhere. Grep for every
variant of the name before considering the removal complete.

### No SUN references in stablecoin/shop/savings context
The shop uses DSD and USDC (stablecoins). SUN is a separate tradeable network token
on Sun Network and is NOT used for shop purchases, item stakes, upgrades, or savings.
SUN savings do not exist. Never refer to SUN deposits, SUN savings, or SUN interest.

### Net worth is computed in ONE place -- services/net_worth.py
`compute_net_worth(uid, gid, db)` returns a `NetWorthResult` with `.total` and
per-category breakdowns. `compute_bulk_net_worth(gid, db)` returns `{uid: float}`
for leaderboards and GDP.

**Every** display of net worth MUST call one of these two functions. Never
re-derive net worth inline with `wallet + bank + ...` in any cog, API endpoint,
or service. If the formula changes, it changes in ONE file.

Components included (and ONLY these):
wallet + bank + cefi_crypto + defi_wallet + stake_value + pos_stake_value
+ lp_value + rig_value + delegation_value + savings_value + items_value
+ disc_fun_value + nft_value - loan_liability

Items = all stone staked_amounts (hash, lock, vault, gamba, liq) + consumables
at cost_stable (charms, gambling_saves, validator_guards, yield_guards).

### Single source of truth -- never duplicate logic
If a value, formula, list, config, color, or helper is used in more than one
place, it MUST live in a shared location (core/framework/, services/, core/config.py,
configs/) and be imported everywhere it is needed. Never copy-paste
logic between cogs, API endpoints, or services. If you need to change behavior,
change it in the one shared location.

### Network key for Discoin Network is "dsc" not "discoin"
- wallet_addresses use prefix `dsc:...`
- wallet_holdings.network = 'dsc'
- transactions.network = 'dsc'
- `_STABLE_NETWORK["DSD"]` must resolve to `"dsc"` (use `_NET_NORMALIZE`)
- Never hardcode `"discoin"` as a network key in wallet operations

### Discord API constraints -- always enforce
- Select menus: max 25 options per select
- Embed field values: max 1024 characters
- Select option descriptions: max 100 characters
- Paginate or split when approaching limits -- do not silently truncate

### DB-side clocks for time comparisons
Never compare Python `datetime.now()` against a Postgres timestamp for cooldowns
or lock checks. Use `EXTRACT(EPOCH FROM (NOW() - column))` so everything runs on
the DB clock and avoids container/DB skew.

### Timestamp formatting -- always use fmt_ts()
DB timestamps come back as epoch floats (via `_coerce`) not datetime objects.
Never use `ts.strftime()`, `<t:...>` Discord format, or `datetime.utcfromtimestamp(ts)`
inline. Always use `fmt_ts(ts)` from `core/framework/ui.py` which handles both epoch
floats and datetime objects with a `%m/%d %H:%M` default format.

### Raw monetary DB columns -- always use PgRow.h() for display
All monetary/token-amount columns are stored as raw `NUMERIC(36,0)` scaled by `10**18`.
When reading one for display, use `row.h("col")` (defined on `PgRow` in
`core/database.py`) instead of `to_human(int(row.get("col", 0) or 0))`.
`row.h()` encapsulates the pattern and keeps the conversion in one place.
Only call `to_human()` directly when the source is NOT a DB row (e.g. config values,
computed intermediates). Never pass a raw DB integer directly to `fmt_usd()` or
`fmt_token()` -- always convert first.

### gm and admin commands are prefix-only
The `gm` and `admin` command groups must use `commands.group()`, never
`commands.hybrid_group()`. They must require the bot prefix (e.g. `,gm`, `,admin`)
and must never be invocable as slash commands or bare words.

### No em dashes, en dashes, or Unicode minus signs in source files
Use plain ASCII hyphens only. These characters have caused silent failures in
string matching and shell scripts. Run a check before committing.

### Branch naming convention
Use `type/Major.Minor.Patch.Hotfix` format. The branch type determines which
version segment increments:
- `major/2.0.0.0` -- breaking/major release (bumps major, resets rest)
- `minor/1.8.0.0` -- new feature (bumps minor, resets patch+hotfix)
- `patch/1.7.4.0` -- bug fix release (bumps patch, resets hotfix)
- `hotfix/1.7.3.3` -- urgent production fix (bumps hotfix only)
Always use all four segments. Never use random slugs, UUIDs, or auto-generated
branch names.

## Framework conventions -- follow exactly

### Embeds -- always use card()
Never use `discord.Embed()` directly. Always use:
```python
from core.framework.embed import card
embed = card("Title", description="Body", color=C_INFO).field("K", "V", True).build()
```

CardBuilder methods (all return self for chaining):
`.description()`, `.color()`, `.url()`, `.field(name, value, inline=False)`,
`.field_if(condition, name, value, inline=False)`, `.blank(inline=False)`,
`.footer(text, icon_url=None)`, `.author(name, icon_url=None, url=None)`,
`.thumbnail(url)`, `.image(url)`, `.timestamp(dt=None)`, `.build()`

### Formatting functions -- use these, never roll your own
All from `core/framework/ui.py`:

| Function | Signature | Output |
|---|---|---|
| `fmt_usd` | `(amount)` | `$1,234.50` |
| `fmt_token` | `(amount, symbol, emoji="")` | `1.500000 ARC` (6 dec, 2 for USD) |
| `fmt_ts` | `(ts, fmt="%m/%d %H:%M")` | `12/25 14:30` (handles epoch float or datetime) |
| `fmt_pct` | `(pct)` | `+4.50%` or `-3.25%` |
| `fmt_gas` | `(fee, coin, emoji="")` | `0.00000150 ARC` |
| `fmt_bonus` | `(base_value, bonus_pct, label="")` | `1,200 MH/s +13%` |

Also available: `FormatKit.usd()`, `.token()`, `.pct()`, `.delta()`, `.bar()`,
`.time_ago()`, `.short_hash()`, `.gas()`, `.mkt_cap()`.

### Color constants
All from `core/framework/ui.py` (re-exported from `constants/ui.py`):

| Constant | Hex | Usage |
|---|---|---|
| `C_SUCCESS` | `0x2ecc71` | Green -- confirmations |
| `C_ERROR` | `0xe74c3c` | Red -- errors |
| `C_WARNING` | `0xe67e22` | Orange -- warnings |
| `C_INFO` | `0x3498db` | Blue -- informational |
| `C_GOLD` | `0xf1c40f` | Yellow -- highlights |
| `C_PURPLE` | `0x9b59b6` | Purple -- profiles |
| `C_TEAL` | `0x1abc9c` | Teal -- LP/DeFi |
| `C_NAVY` | `0x2c3e50` | Dark blue -- panels |
| `C_AMBER` | `0xf39c12` | Amber -- items |
| `C_PINK` | `0xe91e63` | Pink |
| `C_NEUTRAL` | `0x95a5a6` | Gray |

Extended palette (also in `constants/ui.py`):

| Constant | Hex | Usage |
|---|---|---|
| `C_BLURPLE` | `0x5865F2` | Discord brand -- dev tools, admin |
| `C_GRAY` | `0x808080` | Muted -- vault defaults, phase ends |
| `C_SUBTLE` | `0x72767D` | Very muted Discord gray |
| `C_CHART_BG` | `0x161B22` | Trade chart canvas background |
| `C_CRIMSON` | `0x8B0000` | Critical / fatal severity |
| `C_BULL` | `0x00FF88` | Bullish rally phases |
| `C_BEAR` | `0xFF4444` | Bearish / crash phases |
| `C_VOLATILE` | `0xFFAA00` | High-volatility phases |
| `C_CATASTROPHE` | `0xFF0000` | Black-swan events |

Never use raw hex values. If a color doesn't have a constant, add one to
`constants/ui.py` and re-export it through `core/framework/ui.py`.

### Context reply helpers
Use `DiscoContext` methods instead of raw `ctx.reply(embed=discord.Embed(...))`:
- `ctx.reply_error(msg)` -- red error embed
- `ctx.reply_success(msg, title="")` -- green success embed
- `ctx.reply_cooldown(seconds)` -- amber cooldown embed
- `ctx.reply_error_hint(msg, hint="", command_name="")` -- error with suggestions
- `ctx.reply_error_action(msg, button_label, command, rerun_original=False)` -- error with action button
- `ctx.confirm(prompt, timeout=30.0)` -- yes/no confirmation dialog
- `ctx.paginate(pages, timeout=120.0)` -- multi-page embed navigator

### Decorator stacking order
```python
@commands.command(name="foo")
@guild_only                    # outermost
@no_bots
@ensure_registered             # sets ctx.user_row
@user_cooldown(seconds)        # innermost
async def foo(self, ctx: DiscoContext) -> None:
```

### Variable naming conventions
| Variable | Meaning |
|---|---|
| `uid` | `ctx.author.id` or `target.id` |
| `gid` | `ctx.guild_id` |
| `amt` | numeric amount |
| `sym` | token symbol (`"MTA"`, `"ARC"`) |
| `row` | single DB result dict |
| `rows` | list of DB result dicts |

### Database access patterns
```python
ctx.db.fetch_one(query, *args)   # -> dict | None
ctx.db.fetch_all(query, *args)   # -> list[dict]
ctx.db.fetch_val(query, *args)   # -> single value
ctx.db.execute(query, *args)     # -> status string
```
DB timestamps are returned as **epoch floats** via `_coerce()`. Always use `fmt_ts()`.

### Import order
1. `from __future__ import annotations`
2. Standard library (`asyncio`, `logging`, `math`, etc.)
3. Third-party (`discord`, `discord.ext.commands`)
4. `from core.config import Config`
5. Framework (`core.framework.bot`, `core.framework.context`, `core.framework.embed`, `core.framework.ui`, etc.)
6. Module-level constants (prefixed with `_`)

### Transaction footers
```python
from core.framework.tx import set_tx
set_tx(embed, guild_id, tx_hash, footer_extra="Rate: 2.5%")
```

### Logging -- always use `log`, never `logger` or `_log`
Every module uses:
```python
import logging
log = logging.getLogger(__name__)
```
Variable is always `log`. Never `logger`, `_log`, or `_logger`.
Use structured fields where possible: `log.info("msg", extra={"uid": uid})`.
Never use `print()` in production code.

### Error replies -- use the right method
| Situation | Method |
|---|---|
| Simple user error | `ctx.reply_error(msg)` |
| Error with actionable fix | `ctx.reply_error_action(msg, label, command)` |
| Error with hint/suggestions | `ctx.reply_error_hint(msg, hint, command_name)` |
| Cooldown notice | `ctx.reply_cooldown(seconds)` |
| Success confirmation | `ctx.reply_success(msg, title="")` |
| Structured error (title + fields) | `card(title, color=C_ERROR).field(...).build()` |

Never build a raw `discord.Embed(color=C_ERROR)` for errors. Use `ctx.reply_error()`
for simple messages or `card(..., color=C_ERROR)` when you need titles/fields.

### API errors -- use AppError subclasses, never HTTPException
All API error responses use custom exceptions from `api/v2/exceptions.py`:
```python
raise NotFoundError("Resource not found")     # 404
raise ValidationError("Invalid input")        # 422
raise ForbiddenError("Access denied")         # 403
raise UnauthorizedError("Not authenticated")  # 401
raise InsufficientBalanceError("msg")         # 400
raise RateLimitedError("msg")                 # 429
raise ModuleDisabledError("module_name")      # 403
raise TokenHaltedError("msg")                 # 403
```
Never use `raise HTTPException(status_code, detail)` in API routers.
Only exception: 503 for infrastructure unavailability (DB pool, Redis, bot offline).

## Project overview

Discoin is a Discord economy bot built with discord.py (prefix + slash commands),
asyncpg (PostgreSQL), and a FastAPI v2 REST API. It runs as a single Docker
container on Railway with an external PostgreSQL 18 and Redis instance.

### Key files
- `core/config.py` -- all token/network/fee configuration (`Config.TOKENS`, `Config.SHOP_ITEMS`).
- `configs/` -- per-domain config modules (`configs/items_config.py`, `configs/sage_config.py`, `configs/buddies_config.py`, etc.). Import as `from configs import items_config` or `from configs.items_config import ...`.
- `configs/items_config.py` -- stone and consumable item definitions (prices, stats, XP rates)
- `cogs/shop.py` -- shop buy/sell/transfer/levelup commands
- `cogs/bank.py` -- balances, wallet, move, profile
- `cogs/eat_the_rich.py` -- Eat the Rich: class-warfare wealth game
- `database/users.py` -- user/wallet/stone DB operations
- `database/migrations/` -- numbered SQL migrations run in order on startup
- `docker-entrypoint.sh` -- startup script; contains hotfix SQL for existing DBs
- `services/vault.py` -- network vault progression (deposit fees -> level-up embeds)

### Stone tables
Each stone type has its own table: `hashstones`, `lockstones`, `vaultstones`,
`liqstones`, `gambastones`. All have:
- `staked_amount` -- stablecoin paid at purchase + accumulated levelup costs
- `level`, `xp`, `acquired_at`

### Eat the Rich -- no opt-in, punch up only
`cogs/eat_the_rich.py` has no PvP opt-in flag. Everyone is fair game, but `,eat`
only lets you target a player whose net worth is strictly higher than yours
(`services/net_worth.py`). The poorest player is uneatable; the richest is
everyone's meal. The wider the wealth gap, the larger the success bonus.
Persistence still lives in the `exploit_shields` / `exploit_stats` /
`exploit_history` tables and the `exploit_completed` / `exploit_win` bus
events -- those internal names predate the rename and are kept so live player
records and cross-system triggers (achievements, quests, seasons, mastery)
are not disturbed.

### Network short keys
| Network | Short key | Stablecoin |
|---|---|---|
| Discoin Network | `dsc` | DSD |
| Arcadia Network | `arc` | USDC |
| Moneta Chain | `mta` | -- |
| Sun Network | `sun` | -- |
