"""Admin/mod command suite for Discoin.

All commands require Manage Guild permission.
$admin group with subcommands for currency management, user/server resets,
pool/stake/validator administration, dynamic networks/tokens, and whitelabeling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.scale import to_raw, to_human
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from database.reports import VALID_CATEGORIES, VALID_STATUSES
from core.framework.ui import CategoryPaginator, ConfirmView, C_AMBER, C_BEAR, C_BULL, C_ERROR, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PURPLE, C_SUCCESS, C_VOLATILE, C_WARNING, FormatKit, fmt_ts, fmt_usd, mention, send_paginated
from core.framework.fuzzy import suggest_subcommand
from core.framework.middleware import BETA_FEATURES
from core.framework.error_tracker import ErrorSource, Severity
from core.framework.staff_audit import (
    SCOPE_ADMIN,
    SEVERITY_DANGER,
    SEVERITY_WARN,
    build_audit_embeds,
    log_staff_action,
    recent_staff_actions,
)

log = logging.getLogger(__name__)

def _build_report_pages(title: str, lines: list[str]) -> list["discord.Embed"]:
    """Build paginated embeds from report lines, staying under Discord's char limits."""
    _LINES_PER_PAGE = 10
    pages: list[discord.Embed] = []
    for i in range(0, max(len(lines), 1), _LINES_PER_PAGE):
        chunk = lines[i:i + _LINES_PER_PAGE]
        page_num = i // _LINES_PER_PAGE + 1
        total_pages = max(1, (len(lines) + _LINES_PER_PAGE - 1) // _LINES_PER_PAGE)
        _b = card(f"{title} (Page {page_num}/{total_pages})", color=C_WARNING)
        _b.description("\n".join(chunk) if chunk else "No reports found.")
        _b.footer(f"{len(lines)} total reports")
        pages.append(_b.build())
    return pages

class _PumpTargetError(ValueError):
    """User-facing error from pump target resolution."""


# ── Pump target predicates ────────────────────────────────────────────────────
# Each takes (symbol, merged_token_cfg, price_row) and returns True/False.
# ``cfg`` is the merged result from ``get_all_tokens_for_guild`` -- it carries
# ``token_type`` for guild-side rows and the canonical fields for built-ins.

def _pump_filter_nonstable(sym: str, cfg: dict, row: dict) -> bool:
    return not (cfg.get("stablecoin") or cfg.get("consensus") == "Fiat")


def _pump_filter_stable(sym: str, cfg: dict, row: dict) -> bool:
    return bool(cfg.get("stablecoin") or cfg.get("consensus") == "Fiat")


def _pump_filter_network_coin(sym: str, cfg: dict, row: dict) -> bool:
    return sym in {v for v in Config.NETWORK_COINS.values() if v}


def _pump_filter_pow(sym: str, cfg: dict, row: dict) -> bool:
    return cfg.get("consensus") == "PoW"


def _pump_filter_pos(sym: str, cfg: dict, row: dict) -> bool:
    return cfg.get("consensus") == "PoS"


def _pump_filter_wrapped(sym: str, cfg: dict, row: dict) -> bool:
    return bool(cfg.get("peg_to"))


def _pump_filter_earn_only(sym: str, cfg: dict, row: dict) -> bool:
    return sym in Config.EARN_ONLY_TOKENS


_MEME_TOKENS: frozenset = frozenset({"STR"})


def _pump_filter_meme(sym: str, cfg: dict, row: dict) -> bool:
    return sym in _MEME_TOKENS


def _require_manage_guild():
    """Check decorator: requires Manage Guild permission."""
    async def predicate(ctx: DiscoContext) -> bool:
        if not ctx.guild:
            raise commands.CheckFailure("This command can only be used in a server.")
        if not ctx.author.guild_permissions.manage_guild:
            raise commands.CheckFailure("You need **Manage Server** permission to use admin commands.")
        return True
    return commands.check(predicate)

def _require_debug():
    """Check decorator: requires DEBUG=TRUE in the environment."""
    async def predicate(ctx: DiscoContext) -> bool:
        if not Config.DEBUG:
            raise commands.CheckFailure("This command is only available in debug mode.")
        return True
    return commands.check(predicate)


def _require_bot_owner():
    """Check decorator: only the bot owner may run this command.

    Resolves owner from ``Config.BOT_OWNER_ID`` first, then falls back
    to Discord's application owner. Used for premium grant/revoke so a
    server admin can NOT grant their own guild premium status.
    """
    async def predicate(ctx: DiscoContext) -> bool:
        if Config.BOT_OWNER_ID and int(ctx.author.id) == int(Config.BOT_OWNER_ID):
            return True
        try:
            if await ctx.bot.is_owner(ctx.author):
                return True
        except Exception:
            pass
        raise commands.CheckFailure("Only the bot owner may run this command.")
    return commands.check(predicate)

class Admin(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._rollback_locks: dict[int, asyncio.Lock] = {}
        # Auto-pump scheduler state -- per-guild epoch ts of the next event
        # roll. Populated lazily on first tick (jittered so guilds don't all
        # fire on the same minute) and respected by ``auto_pump_task``.
        self._auto_pump_next: dict[int, float] = {}
        self._auto_pump_rng = random.Random()
        # Per-guild pause flag (in-memory). Toggled via ``,admin pump auto off``;
        # cleared on bot restart -- treat as a soft, runtime-only mute. The
        # hard kill-switch is ``Config.AUTO_PUMP_ENABLED``.
        self._auto_pump_disabled_guilds: set[int] = set()
        if getattr(Config, "AUTO_PUMP_ENABLED", True):
            self.auto_pump_task.start()

    def cog_unload(self) -> None:
        try:
            self.auto_pump_task.cancel()
        except Exception:
            pass

    # ── Auto-pump scheduler ───────────────────────────────────────────────────

    @tasks.loop(seconds=60.0)
    async def auto_pump_task(self) -> None:
        """Once per ~hour per guild, fire a random pump on a random token.

        Reuses the same ``_admin_price_events`` slot that ``,admin pump``
        writes to, so the price-tick loop in ``cogs/trade.py`` drives the
        chart through the rolled pattern. ``.buy`` / ``.sell`` / pool
        swaps continue to apply per-trade impact and slippage on top of
        the pumped oracle -- the auto-pump only moves the *spot* price,
        it never bypasses the impact formula a trade pays.
        """
        if not getattr(Config, "AUTO_PUMP_ENABLED", True):
            return
        from cogs.trade import _admin_price_events
        from services.chart_patterns import (
            random_duration, random_magnitude, random_pattern,
        )

        rng = self._auto_pump_rng
        now = time.time()
        lo = float(getattr(Config, "AUTO_PUMP_INTERVAL_MIN_S", 3300.0))
        hi = float(getattr(Config, "AUTO_PUMP_INTERVAL_MAX_S", 4500.0))

        for guild in list(self.bot.guilds):
            if guild.id in self._auto_pump_disabled_guilds:
                continue
            try:
                if not await self.bot.db.module_enabled(guild.id, "crypto"):
                    continue
            except Exception:
                log.exception("auto_pump: module_enabled probe failed gid=%s", guild.id)
                continue

            next_ts = self._auto_pump_next.get(guild.id)
            if next_ts is None:
                # First tick after start: schedule the first roll within
                # one full interval window (don't fire immediately on boot).
                self._auto_pump_next[guild.id] = now + rng.uniform(lo, hi)
                continue
            if now < next_ts:
                continue
            self._auto_pump_next[guild.id] = now + rng.uniform(lo, hi)

            try:
                await self._auto_pump_fire(guild, now, rng, random_pattern,
                                           random_magnitude, random_duration,
                                           _admin_price_events)
            except Exception:
                log.exception("auto_pump: fire failed gid=%s", guild.id)

    @auto_pump_task.before_loop
    async def _before_auto_pump(self) -> None:
        await self.bot.wait_until_ready()

    # Delve tokens are tethered: when the auto-pump picks any one of them,
    # all four receive the same pattern/magnitude/duration so their relative
    # ordering (COPPER < SILVER < GOLD < RUNE) is preserved.
    _DELVE_TETHER: frozenset[str] = frozenset({"COPPER", "SILVER", "GOLD", "RUNE"})

    async def _auto_pump_fire(
        self, guild, now: float, rng: random.Random,
        random_pattern, random_magnitude, random_duration,
        events: dict,
    ) -> None:
        rows = await self.bot.db.get_all_prices(guild.id)
        if not rows:
            return
        all_tokens = await self.bot.db.get_all_tokens_for_guild(guild.id)
        delve_eligible: list[tuple[str, float]] = []
        regular_eligible: list[tuple[str, float]] = []
        for r in rows:
            sym = r["symbol"]
            cfg = all_tokens.get(sym, Config.TOKENS.get(sym, {}))
            # Skip stables (price-clamped in update_price -- pump would no-op)
            # and skip pegged wrappers (snapped to underlying every tick).
            if cfg.get("stablecoin") or cfg.get("consensus") == "Fiat":
                continue
            if cfg.get("peg_to"):
                continue
            try:
                price = float(r["price"])
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            # Skip tokens already in an active event so we don't trample a
            # live admin pump or another auto-pump still running.
            if (guild.id, sym) in events:
                continue
            if sym in self._DELVE_TETHER:
                delve_eligible.append((sym, price))
            else:
                regular_eligible.append((sym, price))

        # Treat the entire delve group as one pick so it gets the same
        # probability weight as a single regular token, not 4x.
        pool: list[tuple[str, float]] = regular_eligible[:]
        if delve_eligible:
            pool.append(delve_eligible[0])

        if not pool:
            return

        chosen_sym, chosen_price = rng.choice(pool)
        # If a delve token was chosen, pump all eligible delve tokens together
        # with identical pattern/magnitude/duration so the tier hierarchy holds.
        if chosen_sym in self._DELVE_TETHER:
            targets: list[tuple[str, float]] = delve_eligible
        else:
            targets = [(chosen_sym, chosen_price)]

        pattern = random_pattern(rng)
        magnitude = random_magnitude(pattern, rng)
        duration_min = random_duration(rng)
        seed = rng.randrange(1, 2**31)

        for t_sym, t_price in targets:
            ev = {
                "start_ts":      now,
                "end_ts":        now + duration_min * 60.0,
                "start_price":   t_price,
                "pattern":       pattern,
                "magnitude_pct": magnitude,
                "seed":          seed,
            }
            events[(guild.id, t_sym)] = ev
            try:
                await self.bot.db.upsert_admin_price_event(guild.id, t_sym, ev)
            except Exception:
                log.exception(
                    "auto_pump: persist failed gid=%s sym=%s", guild.id, t_sym,
                )
            log.info(
                "auto_pump: gid=%s sym=%s pattern=%s mag=%+.1f%% dur=%.0fmin",
                guild.id, t_sym, pattern, magnitude, duration_min,
            )

    # ── $admin group ──────────────────────────────────────────────────────────

    @commands.group(name="admin", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin(self, ctx: DiscoContext) -> None:
        """Admin command overview  -  select a category to browse."""
        if await suggest_subcommand(ctx, self.admin):
            return
        p = ctx.prefix or "."
        categories = self._build_admin_categories(p)
        await CategoryPaginator.send(ctx, categories)

    def _build_admin_categories(self, p: str) -> dict[str, list["discord.Embed"]]:
        """Build the admin help category dict (extracted for reuse by admin help + search)."""
        def _page(title: str, lines: list[str], color=C_ERROR) -> "discord.Embed":
            _b = card(title, color=color)
            for i in range(0, len(lines), 10):
                chunk = lines[i:i + 10]
                _b.field("\u200b", "\n".join(chunk), False)
            _b.footer(f"Use {p}admin <subcommand> to run any command")
            return _b.build()

        categories: dict[str, list["discord.Embed"]] = {

            "💰 Economy": [_page("💰 Economy", [
                f"`{p}admin give @user <amt> [token]`   -  give currency or tokens",
                f"  Example: `{p}admin give @Alice 500 USD`",
                f"`{p}admin take @user <amt> [token]`   -  remove currency or tokens",
                f"  Example: `{p}admin take @Bob 100 ARC`",
                f"`{p}admin setbal @user <amt> [token]`   -  set exact balance",
                f"  Example: `{p}admin setbal @Alice 1000 USD`",
                f"`{p}admin setprice <SYM> <price>`   -  override a token's price",
                f"  Example: `{p}admin setprice ARC 2000`",
                f"`{p}admin reset user @user`   -  wipe a single user's data",
                f"`{p}admin reset server`   -  wipe ALL server data (requires confirmation)",
                f"`{p}admin reset economy`   -  wipe user data, keep pools and prices",
                f"`{p}admin refresh <TOKEN|all>`   -  non-destructive price/chain refresh",
                f"`{p}admin reject <action_id>`   -  reject a pending action",
                f"`{p}admin log`   -  session log + activity summary (debug mode)",
            ])],

            "🔄 Reset & Refresh": [_page("🔄 Reset & Refresh", [
                f"**⚠️ Destructive  -  `.admin reset` (cannot be undone)**",
                f"`{p}admin reset user @user`   -  wipe all data for a single user",
                f"  Alias: `{p}admin resetuser @user`",
                f"`{p}admin reset server`   -  wipe ALL server data",
                f"  Alias: `{p}admin resetserver`",
                f"`{p}admin reset economy`   -  wipe user data, keep pools & prices",
                f"  Alias: `{p}admin reseteconomy`",
                f"`{p}admin reset chain <SYM>`   -  reset a PoW chain to block 0",
                f"  Alias: `{p}admin chain reset <SYM>`",
                f"`{p}admin reset chainall`   -  reset ALL PoW chains to block 0",
                f"  Alias: `{p}admin chain resetall`",
                f"`{p}admin reset supply <token>`   -  reset supply, wipe player balances",
                f"  Alias: `{p}admin supply reset <token>`",
            ]), _page("🔄 Reset & Refresh (cont.)", [
                f"**✅ Non-Destructive  -  `.admin refresh` (player assets untouched)**",
                f"`{p}admin refresh all`   -  reset ALL prices to defaults, all PoW chains",
                f"  to block 0, recalculate supply from player holdings",
                f"`{p}admin refresh <TOKEN>`   -  refresh price + chain for one token",
                f"  Example: `{p}admin refresh MTA`",
                f"  Example: `{p}admin refresh ARC`",
                f"",
                f"**Supply utilities** (non-destructive):",
                f"`{p}admin supply check [token]`   -  view circulating / max supply",
                f"`{p}admin supply recalculate`   -  recalc supply from player holdings",
            ])],

            "⛓ Chain & Blocks": [_page("⛓ Chain & Blocks", [
                f"`{p}admin blockstatus`   -  view block status per network",
                f"`{p}admin bundle`   -  force-seal PoS chain blocks (ARC, DSC...)",
                f"`{p}admin forcemining [SYM]`   -  force a PoW mining tick (SUN, MTA)",
                f"`{p}admin chain`   -  view all PoW chain stats",
                f"`{p}admin chain set <SYM> <key> <value>`   -  update a chain config value",
                f"`{p}admin halt`   -  view active network/token halts",
                f"`{p}admin halt network <arc|sol|bnb|sun> [on|off]`   -  halt a network",
                f"  Example: `{p}admin halt network sol on`",
                f"`{p}admin halt token <SYM> [on|off]`   -  halt a specific token",
                f"  Example: `{p}admin halt token ARC off`",
                f"",
                f"**Chain & supply resets/refreshes:** see `{p}admin help Reset & Refresh`",
            ])],

            "🏊 Pools & Tokens": [_page("🏊 Pools & Tokens", [
                f"`{p}admin removepool <A> <B>`   -  remove a liquidity pool",
                f"  Example: `{p}admin removepool ARC USD`",
                f"`{p}admin rebalancepool <A> <B> <price>`   -  rebalance a pool",
                f"`{p}admin unblockpool <A> <B>`   -  lift pool circuit-breaker halt",
                f"`{p}admin addlp <A> <B> <amt_a> <amt_b>`   -  inject raw liquidity (no whale cap)",
                f"`{p}admin removelp <A> <B> <amt_a> <amt_b>`   -  drain liquidity (no LP balance)",
                f"`{p}admin addtoken <SYM> <name> <emoji> <network|none> <consensus> <price> <vol>`",
                f"  Example: `{p}admin addtoken DOGE Dogecoin 🐕 none pow 0.08 1000000`",
                f"`{p}admin removetoken <SYM>`   -  remove a token",
                f"`{p}admin listtokens`   -  list all tokens on this server",
                f"`{p}admin contract <SYM>`   -  view contract params",
                f"`{p}admin setcontract <SYM> <transfer_fee|burn_rate|max_supply> <val>`",
                f"`{p}admin clearcontract <SYM>`   -  reset contract params to default",
                f"",
                f"**Price Events / Chart Patterns:**",
                f"`{p}admin pump <target> [pattern|pct] [magnitude%] [mins]`",
                f"  Drives prices through any chart pattern over a chosen timeframe.",
                f"  `{p}admin pump patterns`    list every pattern (linear pump moon dump",
                f"     crash bull bear volatile wave rugpull pumpdump vshape hns",
                f"     double_top double_bottom cup_handle bullflag bearflag chaos",
                f"     zigzag spike accumulate distribute stairstep fakeout)",
                f"  `{p}admin pump active`      show every running event on this guild",
                f"  Targets: `SYM`, `all`/`everything`, `each` (chaos per token),",
                f"           `coins` (network coins), `stables`, `group`, `earn`,",
                f"           `wrapped`, `pow`, `pos`, `builtin`, `meme`,",
                f"           `chain:<short>` (e.g. `chain:arc`, `chain:moon`),",
                f"           `each:<category>` (chaos within a category).",
                f"  `pattern` can be `random` to roll the dice. Negative magnitude flips",
                f"     the direction for `linear` only -- other patterns absorb the sign.",
                f"  Example: `{p}admin pump ARC 25 30`           (linear, +25%, 30 min, back-compat)",
                f"  Example: `{p}admin pump MTA moon 80 30`      (parabolic moonshot)",
                f"  Example: `{p}admin pump STR rugpull 60 90`  (pump then catastrophic dump)",
                f"  Example: `{p}admin pump everything bullflag 40`",
                f"  Example: `{p}admin pump each:group`          (chaos across group tokens)",
                f"  Example: `{p}admin pump chain:arc chaos 30 60`",
                f"  Example: `{p}admin pump coins random`        (network coins, dice roll)",
                f"  Cancel:  `{p}admin pump <target> 0` -- target can be `all`, `each`,",
                f"           `chain:arc`, `each:group`, or any single symbol.",
                f"",
                f"**Group Token Trading Control:**",
                f"`{p}admin grouptoken`  (alias: `{p}admin gt`)   -  list all group tokens + status",
                f"`{p}admin grouptoken enable <SYM>`   -  allow players to trade a group token",
                f"`{p}admin grouptoken disable <SYM>`   -  lock a group token (no buy/sell/swap)",
                f"`{p}admin grouptoken enableall`   -  enable trading for all group tokens",
                f"`{p}admin grouptoken disableall`   -  lock all group tokens",
                f"`{p}admin grouptoken network <SYM> <sun|mta>`   -  rebind a group token to a different PoW network",
            ])],

            "🔗 Validators & Networks": [_page("🔗 Validators & Networks", [
                f"`{p}admin clearstakes @user | VALIDATOR_ID`   -  clear user or validator stakes",
                f"  Example: `{p}admin clearstakes @Alice`",
                f"`{p}admin clearvalidator VALIDATOR_ID`   -  deactivate a validator",
                f"`{p}admin addvalidator <ID> <name> <net> <uptime%> <reward%> <slash%> [emoji]`",
                f"  Example: `{p}admin addvalidator lido LIDO arc 99.9 4.0 0.5 🔵`",
                f"`{p}admin removevalidator <ID>`   -  permanently remove a validator",
                f"`{p}admin updatevalidator <ID> <field> <value>`   -  update one field",
                f"  Example: `{p}admin updatevalidator lido reward_rate 0.00011`",
                f"`{p}admin addnetwork <name> <stake_token> [emoji]`   -  add a network",
                f"`{p}admin removenetwork <name>`   -  remove a network",
                f"`{p}admin listnetworks`   -  list all registered networks",
                f"`{p}admin recoverstakes`   -  recover orphaned stakes from migrations",
            ])],

            "📢 Channels & Modules": [_page("📢 Channels & Modules", [
                f"`{p}admin setchannel <type> #channel`   -  set a dedicated channel",
                f"  Types: trade, mine, staking, gambling, pools, crypto, drops,",
                f"         dropsspawn, faucet, wallet, validators, contracts, job,",
                f"         error, whale, reports, events, nft, predictions, ape",
                f"  Example: `{p}admin setchannel gambling #casino`",
                f"`{p}admin botchannel #channel`   -  toggle no-prefix mode for a channel",
                f"  Players just type `work`, `buy 10 arc`, etc.  -  no prefix needed",
                f"`{p}admin module <name> <on|off>`   -  enable/disable a module",
                f"  Modules: gambling, lending, staking, mining, faucet, drops, savings,",
                f"           validators, pools, contracts, groups, chart, crypto,",
                f"           daily, work, economy, chain, shop, games,",
                f"           ape, nft, predictions, events",
                f"  Example: `{p}admin module faucet off`",
                f"`{p}admin faucet multiplier <x>`   -  set faucet payout multiplier (default 1.0)",
                f"`{p}admin faucet tokens [sym,sym,...]`   -  whitelist tokens for random drops",
                f"  Leave blank to reset to all eligible tokens",
                f"  Example: `{p}admin faucet tokens MTA,DSC,ARC`",
                f"`{p}admin whalethreshold <amount>`   -  set whale alert USD threshold",
                f"`{p}admin reportsfeed [categories]`   -  configure reports feed categories",
            ])],

            "📡 Events": [_page("📡 Market Events", [
                f"**View & Control:**",
                f"`{p}admin event status`   -  view current event, settings, disabled list",
                f"`{p}admin event list`   -  list all 12 event types with disabled status",
                f"`{p}admin event trigger <type>`   -  manually start a market event",
                f"  Example: `{p}admin event trigger bull_run`",
                f"`{p}admin event clear`   -  end the current event early",
                f"",
                f"**Disable / Enable Individual Events:**",
                f"`{p}admin event disable <type>`   -  block an event from random triggers",
                f"  Example: `{p}admin event disable black_swan`  -  no more black swans",
                f"`{p}admin event enable <type>`   -  re-enable for random triggers",
                f"`{p}admin event disable all`   -  disable ALL events from random triggers",
                f"`{p}admin event enable all`   -  re-enable all events",
                f"  Note: disabled events can still be triggered manually",
                f"",
                f"**Frequency Control:**",
                f"`{p}admin event frequency`   -  view current frequency + presets",
                f"`{p}admin event frequency <preset|value>`",
                f"  Presets: `off` `low` `default` `high` `max` (chaos mode)",
                f"  Or a custom value like `0.001`",
                f"",
                f"**Module Toggle:**",
                f"`{p}admin module events on|off`   -  enable/disable events entirely",
            ])],

            "🔒 Permissions": [_page("🔒 Permissions", [
                f"`{p}admin perm`   -  list all command role restrictions",
                f"`{p}admin perm add <command> @role`   -  restrict command to role",
                f"  Example: `{p}admin perm add gamble @Adults`",
                f"`{p}admin perm remove <command> @role`   -  remove role restriction",
                f"`{p}admin perm clear <command>`   -  remove all restrictions",
                f"",
                f"When restrictions are set, only members with at least one allowed",
                f"role can use that command. Admins are always exempt.",
            ])],

            "🧪 Beta Features": [_page("🧪 Beta Features", [
                f"Beta features are gated behind per-user/role access.",
                f"Admins (Manage Server) always have access.",
                f"",
                f"`{p}admin beta`   -  view current beta grants",
                f"`{p}admin beta features`   -  list available features",
                f"`{p}admin beta grant <feature> @user/@role`   -  grant access",
                f"`{p}admin beta revoke <feature> @user/@role`   -  revoke access",
                f"`{p}admin beta clear <feature>`   -  clear all grants",
                f"",
                f"**Features:**",
                f"  `command_chains`  -  multi-command chains (&&, >, ;, etc.)",
                f"  `internal_commands`  -  bot/discoin internal commands",
            ])],

            "🗑 Auto-Delete": [_page("🗑 Auto-Delete", [
                f"`{p}admin autodelete`   -  view current auto-delete settings",
                f"`{p}admin autodelete commands <seconds|off>`   -  delete commands after N seconds",
                f"  Example: `{p}admin autodelete commands 10`",
                f"`{p}admin autodelete replies <seconds|off>`   -  delete bot replies after N seconds",
                f"  Example: `{p}admin autodelete replies 30`",
                f"`{p}admin autodelete aicommands <seconds|off>`   -  delete .ask commands",
                f"`{p}admin autodelete aireplies <seconds|off>`   -  delete AI replies",
            ])],

            "🎨 Server Config": [_page("🎨 Server Config", [
                f"`{p}admin setprefix <prefix>`   -  change the command prefix",
                f"  Example: `{p}admin setprefix !`",
                f"`{p}admin setcolor <#hex>`   -  set embed accent color",
                f"  Example: `{p}admin setcolor #FF6B00`",
                f"`{p}admin setname <name>`   -  set the server/bot display name",
                f"`{p}admin setcurrencyname <name>`   -  rename the currency label",
                f"`{p}admin settings`   -  view all current server settings",
            ])],

            "🤖 AI & Personas": [_page("🤖 AI & Personas", [
                f"`{p}admin ai status`   -  show AI feature flags",
                f"`{p}admin ai toggle <feature>`   -  toggle an AI feature on/off",
                f"  Features: chat, commentary, events, flavor, mm",
                f"  Example: `{p}admin ai toggle chat`",
                f"`{p}admin ai test`   -  send a test AI message",
                f"`{p}admin ai prompt <feature> <text>`   -  set custom AI prompt",
                f"`{p}admin ai persona <name>`   -  activate a persona by name",
                f"`{p}admin ai clearhistory [@user]`   -  clear AI conversation history",
                f"`{p}admin ai reloadtools`   -  hot-reload tools.json without restarting",
                f"`{p}admin persona list`   -  list all personas",
                f"`{p}admin persona create <name> <bias>`   -  create a market maker persona",
                f"`{p}admin persona setprompt <name> <prompt>`   -  set persona system prompt",
                f"`{p}admin persona setavatar <name> <url>`   -  set persona avatar",
                f"`{p}admin persona settradebias <name> <buy|sell|neutral>`",
                f"`{p}admin persona toggle <name>`   -  activate/deactivate persona",
                f"`{p}admin persona delete <name>`   -  delete a persona",
                f"`{p}admin mmwebhook status|create|delete`   -  market maker webhook",
            ])],

            "🔍 Reports": [_page("🔍 Reports", [
                f"`{p}admin reports`   -  all reports (sent to DMs)",
                f"`{p}admin reports bugs`   -  filter by category",
                f"  Categories: bugs, suggestions, users, other",
                f"`{p}admin reports open`   -  filter by status",
                f"  Statuses: open, accepted, in_progress, resolved, closed, rejected",
                f"`{p}admin reports bugs open`   -  filter by category AND status",
                f"`{p}admin reports search @user`   -  reports by a user",
                f"`{p}admin reports search <ID>`   -  view specific report by number",
                f"`{p}admin reports delete <ID>`   -  delete a report",
                f"`{p}admin reports dump [category] [status]`   -  full untruncated .md via DM",
                f"`{p}admin reports diagnose <ID>`   -  AI realness check (OpenRouter / Ollama)",
                f"`{p}admin reports auto on|off`   -  auto-diagnose on every new report",
                f"`{p}admin reports auto-close on|off`   -  auto-reject spam + auto-resolve merged PRs",
                f"`{p}admin reports autofix on|off`   -  Tier-A auto-PR for real reports (draft PRs)",
                f"`{p}admin reports autofix <ID>`   -  manual auto-fix trigger for one report",
                f"`{p}admin reports autofix test`   -  GitHub auth + repo access probe (read-only)",
                f"`{p}admin reports queue`   -  per-report autofix dashboard (status + links)",
                f"`{p}admin reports queue scan|add <ID>|status <ID>|cancel [ID]|resume|clear`   -  queue ops",
                f"`{p}admin reports clear [category] [status]`   -  bulk delete reports",
                f"`{p}admin reports close-old <days> [status]`   -  bulk-close stale reports (status=closed)",
                f"",
                f"Each report DM has: Accept/Reject, In Progress, Resolve, Close buttons,",
                f"a Message Reporter button (💬), and a tag selector dropdown.",
            ])],

            "🗄 Backup": [_page("🗄 Backup", [
                f"`{p}admin backup create`   -  create a manual database backup now",
                f"`{p}admin backup list`   -  list all existing backups",
                f"`{p}admin backup restore <filename>`   -  restore from a backup (restarts bot)",
                f"",
                f"Backups are stored as SQLite files. Only use restore in emergencies.",
                f"The bot will automatically restart after a successful restore.",
            ])],

            "🩺 Diagnostics": [_page("🩺 Diagnostics", [
                f"`{p}admin health`   -  full server health diagnostic",
                f"`{p}admin commandstats`   -  DM a text dump of command usage",
                f"  All-time / 7-day / 24-hour counts, broken down by",
                f"  command + subcommand + arguments. Aliases: cmdstats, usagestats.",
                f"`{p}admin blockstatus`   -  block/chain status summary",
                f"`{p}admin bundle`   -  force-seal PoS blocks (ARC/DSC only)",
                f"`{p}admin forcemining [SYM]`   -  force a PoW mining tick (SUN/MTA)",
                f"`{p}admin recoverstakes`   -  recover stakes orphaned by validator migrations",
                f"",
                f"**Developer tools** (`{p}dev`  -  bot developer only):",
                f"`{p}dev status`   -  comprehensive diagnostic DM (6+ pages)",
                f"`{p}dev heartbeat`   -  task loop health monitor",
                f"`{p}dev check <system>`   -  individual system checks",
                f"  Systems: events, mining, staking, prices, savings, security, faucet, pools, errors",
                f"`{p}dev log`   -  session log + activity summary",
                f"`{p}dev errors`   -  error tracker (summary, cmds, bot, export, clear)",
                f"`{p}dev config`   -  dev settings (auto-DM interval, etc.)",
                f"`{p}admin health cleanup [days]`   -  purge old DB rows",
            ])],

            "🛡 Security & Scam": [_page("🛡 Security & Scam", [
                f"Use `{p}admin security` or the full `{p}security` group.",
                f"",
                f"**Monitoring & Enforcement**",
                f"`{p}admin security status`  -  system health",
                f"`{p}admin security user <@user>`  -  profile & threat score",
                f"`{p}admin security threats [hours]`  -  recent events",
                f"`{p}admin security freeze/unfreeze <@user>`  -  freeze/lift",
                f"`{p}admin security clearscore <@user>`  -  reset score",
                f"`{p}admin security lockdown/lift <feature>`  -  circuit breaker",
                f"",
                f"**Scam Detection**",
                f"`{p}admin security scam`  -  scam settings overview",
                f"`{p}admin security scam on/off`  -  enable/disable",
                f"`{p}admin security scam channel #ch`  -  alert channel",
                f"`{p}admin security scam timeout <min>`  -  timeout duration",
                f"`{p}admin security scam notify @mod`  -  toggle DM alerts",
                f"`{p}admin security scam log [n]`  -  recent scam log",
                f"",
                f"**Audit & Exemptions**",
                f"`{p}admin security audit`  -  view security audit log",
                f"`{p}admin security exempt list`  -  owner-granted bypasses",
                f"`{p}admin security exempt add <user|role> <id>`  -  add bypass",
                f"`{p}admin security logchannel #ch`  -  set enforcement log channel",
                f"",
                f"**Configuration**",
                f"`{p}admin security settings`  -  all thresholds",
                f"`{p}admin security set <key> <value>`  -  adjust threshold",
                f"`{p}admin security hierarchy`  -  view the 6-tier hierarchy",
            ], color=C_INFO)],

            "🖼 NFTs & Predictions": [_page("🖼 NFTs & Predictions", [
                f"`{p}admin nft create <SYM> <name> <network> <price> <mint_token> [max_supply]`",
                f"  Create an NFT collection with ERC-721 contract. Network: ARC or DSC.",
                f"  Example: `{p}admin nft create PUNKS \"Discoin Punks\" ARC 0.05 ARC 100`",
                f"`{p}admin nft setimage <SYM> <url>`   -  set collection image URL",
                f"`{p}admin nft delete <SYM>`   -  delete a collection (only if 0 minted)",
                f"`{p}admin module nft on|off`   -  toggle NFT module for this server",
                f"",
                f"**Player NFT commands** use `<symbol> <token_id>` for list/buy/transfer.",
                f"",
                f"**Prediction Markets:**",
                f"`{p}admin predict create <question>`   -  create a new prediction market",
                f"`{p}admin predict resolve <id> <YES|NO>`   -  resolve with winning outcome",
                f"`{p}admin predict cancel <id>`   -  cancel and refund all bets",
                f"`{p}admin predict close <id>`   -  close a market to new bets",
                f"`{p}admin predict list`   -  list all markets (including resolved)",
                f"`{p}admin module predictions on|off`   -  toggle predictions module",
                f"",
                f"**Announcements & DMs:**",
                f"`{p}admin announce <message>`   -  broadcast an embed to current channel",
                f"`{p}admin dm @user <message>`   -  send a DM from the bot",
            ])],

            "🛠 Moderation": [_page("🛠 Moderation", [
                f"`{p}admin purge <count>`   -  bulk-delete up to 1000 messages in this channel",
                f"`{p}admin purge @user <count>`   -  delete only that user's messages",
                f"  Example: `{p}admin purge 20`         delete last 20 messages",
                f"  Example: `{p}admin purge @Alice 50`  delete last 50 messages from Alice",
                f"  Aliases: `{p}admin clear`, `{p}admin prune`",
                f"  Bot needs **Manage Messages**. Invoking message is deleted too.",
                f"",
                f"**Security Freeze (game-level):**",
                f"`{p}admin security freeze @user`   -  freeze a player's game account",
                f"`{p}admin security unfreeze @user`   -  lift the freeze",
                f"  See `{p}admin help Security & Scam` for the full security toolset.",
            ], color=C_WARNING)],

            "🤖 Internal Commands": [_page("🤖 Internal Commands", [
                f"Say **bot** (or @mention me) followed by a command.",
                f"Alternatively use `/discoin prompt: <command>`.",
                f"Access is gated by the **internal_commands** beta feature.",
                f"Admins are always allowed; others need access via `admin beta` commands.",
                f"",
                f"**Economy**: `bot balance` `bot daily` `bot portfolio`",
                f"**Trading**: `bot buy ARC 1` `bot sell SOL all` `bot prices`",
                f"**Market**: `bot top gainers` `bot market overview`",
                f"**Mining**: `bot mine status` `bot mine rigs`",
                f"**Staking**: `bot stake list` `bot validator list`",
                f"**Pools**: `bot pool list` `bot addlp ARC LINK 1 50`",
                f"**Chain**: `bot chain` `bot networks` `bot gas fees`",
                f"**Server**: `bot server stats` `bot treasury`",
                f"**Utility**: `bot ping` `bot dashboard` `bot ask <q>`",
                f"**Admin**: `bot admin settings` `bot admin audit log`",
                f"",
                f"Say `bot help commands` for the full list.",
            ], color=C_INFO)],
        }

        return categories

    @admin.command(name="help")
    @guild_only
    @_require_manage_guild()
    async def admin_help(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Browse admin help. Usage: admin help [category] [subcategory] | admin help search <phrase>"""
        p = ctx.prefix or "."
        parts = args.strip().split() if args.strip() else []
        categories = self._build_admin_categories(p)

        # ── admin help search <phrase> ────────────────────────────────────────
        if parts and parts[0].lower() == "search":
            query = " ".join(parts[1:]).lower()
            if not query:
                return await ctx.reply_error(f"Usage: `{p}admin help search <phrase>`")

            results: list[tuple[str, str]] = []  # (category_label, matching_line)
            # Search admin categories
            for cat_label, pages in categories.items():
                for page_embed in pages:
                    for field in page_embed.fields:
                        for line in (field.value or "").split("\n"):
                            if query in line.lower():
                                results.append((cat_label, line.strip()))
            # Search internal commands
            ic = getattr(self.bot, "internal_commands", None)
            if ic:
                from core.framework.internal_commands import registry
                for cmd_name, cmd_data in registry._commands.items():
                    hay = f"{cmd_name} {cmd_data.description}".lower()
                    if query in hay:
                        results.append(("Internal Commands", f"`bot {cmd_name}`  -  {cmd_data.description}"))

            if not results:
                return await ctx.reply_error(f"No admin help results for **{query}**.")

            b = card("Admin Help Search", color=C_ERROR)
            b.description(f"Results for **{query}**:")
            # Group by category, cap field values at 900 chars
            seen_cats: dict[str, list[str]] = {}
            for cat, line in results[:15]:
                seen_cats.setdefault(cat, []).append(line)
            for cat, lines in list(seen_cats.items())[:6]:
                val = "\n".join(lines[:5])
                if len(val) > 900:
                    val = val[:900] + "..."
                b.field(cat, val, False)
            b.footer(f"{p}admin help <category> to see full docs")
            return await ctx.reply(embed=b.build(), mention_author=False)

        # ── admin help <category> [subcategory] ──────────────────────────────
        if parts:
            query_cat = parts[0].lower()
            # Find matching category
            matched_key = None
            for cat_label in categories:
                # Match by first word or substring
                clean = cat_label.lower().replace("🤖", "").replace("🛡", "").strip()
                if query_cat in clean or clean.startswith(query_cat):
                    matched_key = cat_label
                    break
            if not matched_key:
                import difflib
                all_labels = list(categories.keys())
                clean_labels = [l.lower() for l in all_labels]
                matches = difflib.get_close_matches(query_cat, clean_labels, n=1, cutoff=0.4)
                if matches:
                    matched_key = all_labels[clean_labels.index(matches[0])]

            if not matched_key:
                available = "\n".join(f"- {k}" for k in categories)
                return await ctx.reply_error(f"Unknown category **{query_cat}**.\n\n{available}")

            # Subcategory drill-down
            if len(parts) > 1:
                sub_query = " ".join(parts[1:]).lower()
                for page_embed in categories[matched_key]:
                    for field in page_embed.fields:
                        if sub_query in (field.name or "").lower() or sub_query in (field.value or "").lower():
                            b = card(f"{matched_key} › Match", color=C_ERROR)
                            val = (field.value or "")[:1024]
                            b.field(field.name or "\u200b", val, False)
                            b.footer(f"{p}admin help  -  browse all categories")
                            return await ctx.reply(embed=b.build(), mention_author=False)
                return await ctx.reply_error(f"No match for **{sub_query}** in {matched_key}.")

            # Show the category page(s)
            pages = categories[matched_key]
            if len(pages) == 1:
                return await ctx.reply(embed=pages[0], mention_author=False)
            return await ctx.paginate(pages)

        # ── admin help (no args) → full category paginator ────────────────────
        await CategoryPaginator.send(ctx, categories)

    # ── Security proxy ────────────────────────────────────────────────────────

    @admin.command(name="security", rest_is_raw=True)
    @_require_manage_guild()
    async def admin_security(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Proxy for the security command group. Usage: admin security [subcommand ...]"""
        p = ctx.prefix or "."
        security_cog = self.bot.get_cog("Security")
        if not security_cog:
            return await ctx.reply_error("Security cog is not loaded.")

        args = args.strip()
        if not args:
            # Show the security admin help page
            categories = self._build_admin_categories(p)
            pages = categories.get("🛡 Security & Scam", [])
            if pages:
                return await ctx.reply(embed=pages[0], mention_author=False)
            return await ctx.reply_error("Security help unavailable.")

        # Re-invoke as: security <args>
        full_cmd = f"{p}security {args}"
        new_ctx = await self.bot.get_context(ctx.message)
        new_ctx.message.content = full_cmd
        new_ctx = await self.bot.get_context(new_ctx.message)
        if new_ctx.command is None:
            return await ctx.reply_error(
                f"Unknown security subcommand `{args.split()[0]}`.\n"
                f"Use `{p}admin help security` to see all options."
            )
        await self.bot.invoke(new_ctx)

    # ── Channel message purge ─────────────────────────────────────────────────

    @admin.command(name="purge", aliases=["clear", "prune"])
    @_require_manage_guild()
    async def admin_purge(
        self, ctx: DiscoContext, member: discord.Member | None = None, count: int = 0,
    ) -> None:
        """Bulk-delete the last N messages in this channel (max 1000).

        Optionally filter to messages from a specific user.
        Usage: ,admin purge <count>  |  ,admin purge @user <count>"""
        if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            await ctx.reply_error(
                "I need **Manage Messages** permission in this channel to purge messages."
            )
            return
        if count < 1 or count > 1000:
            await ctx.reply_error("Count must be between **1** and **1000**.")
            return

        purge_kwargs: dict = {"limit": count, "before": ctx.message}
        if member is not None:
            purge_kwargs["check"] = lambda m: m.author == member

        try:
            deleted = await ctx.channel.purge(**purge_kwargs)
        except discord.Forbidden:
            await ctx.reply_error("I don't have permission to delete messages here.")
            return
        except discord.HTTPException as exc:
            await ctx.reply_error(f"Purge failed: {exc}")
            return

        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        who = f" from **{member.display_name}**" if member else ""
        n = len(deleted)
        await log_staff_action(
            ctx.db,
            scope=SCOPE_ADMIN,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="purge",
            target_id=member.id if member else None,
            severity=SEVERITY_WARN,
            details=f"channel={ctx.channel.id} requested={count} deleted={n}",
        )
        conf = await ctx.send(
            embed=card("Purge Complete", color=C_SUCCESS)
            .description(f"Deleted **{n}** message{'s' if n != 1 else ''}{who}.")
            .footer(f"Requested by {ctx.author.display_name}")
            .build()
        )
        await asyncio.sleep(5)
        try:
            await conf.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

    # ── Per-unit item NFT layer admin ─────────────────────────────────────────
    # ``,admin items`` is the per-unit NFT layer (item_contracts /
    # item_instances) admin surface. The legacy ``,admin nft`` group at
    # the bottom of this file runs against the older smart_contracts /
    # token_contracts collection NFTs -- different schema, different
    # purpose, different commands.

    @admin.group(name="items", aliases=["nftlayer"], invoke_without_command=True)
    @_require_manage_guild()
    async def admin_items(self, ctx: DiscoContext) -> None:
        """Per-unit item-NFT layer admin tools (reconcile, etc.)."""
        prefix = ctx.prefix or "."
        body = (
            f"`{prefix}admin items reconcile`  -  diff JSONB vs NFT counts\n"
            f"`{prefix}admin items reconcile @user`  -  scope to one user\n"
        )
        await ctx.reply(
            embed=card("\U0001F4E6 Item-NFT admin", description=body, color=C_ERROR).build(),
            mention_author=False,
        )

    @admin_items.command(name="reconcile", aliases=["diff", "audit"])
    @_require_manage_guild()
    async def admin_items_reconcile(
        self, ctx: DiscoContext,
        target: discord.Member | None = None,
    ) -> None:
        """Diff the JSONB inventories against NFT counts.

        Empty drift report = NFT shadow is in sync with the canonical
        source of truth; safe to flip reads. Drift rows mean a write
        site somewhere isn't keeping the layers in sync -- fix that
        site, run again.
        """
        from services import nft_reconcile as _recon
        await ctx.defer() if hasattr(ctx, "defer") else None
        report = await _recon.reconcile_guild(ctx.db, ctx.guild_id)
        drifts = report.get("drifts") or []
        summary = report.get("summary") or {}
        if target is not None:
            drifts = [d for d in drifts if int(d["user_id"]) == int(target.id)]

        if not drifts:
            scope = f"<@{target.id}>" if target else "this guild"
            await ctx.reply_success(
                f"NFT layer is in sync with JSONB for {scope}. "
                f"Safe to flip reads.",
                title="✅ No Drift",
            )
            return

        # Sort by largest abs drift first so the worst offenders are visible.
        drifts.sort(key=lambda r: -abs(int(r["drift"])))

        # Render up to 30 drift rows. Discord caps a single embed field
        # value at 1024 chars, so chunk lines into multiple fields when
        # the cumulative length goes over 1000 (small headroom).
        rendered: list[str] = []
        for r in drifts[:30]:
            sign = "+" if r["drift"] > 0 else ""
            rendered.append(
                f"<@{int(r['user_id'])}>  ·  `{r['kind']}.{r['catalog_key']}`  ·  "
                f"jsonb=`{int(r['jsonb_count'])}` nft=`{int(r['nft_count'])}`  ·  "
                f"drift `{sign}{int(r['drift'])}`"
            )
        more = (
            f"\n-# +{len(drifts) - 30} more drift rows not shown."
            if len(drifts) > 30 else ""
        )
        # Bucket lines into <=1000 char chunks (each chunk -> one field).
        chunks: list[list[str]] = []
        cur: list[str] = []
        cur_len = 0
        for ln in rendered:
            if cur_len + len(ln) + 1 > 1000:
                chunks.append(cur)
                cur, cur_len = [], 0
            cur.append(ln)
            cur_len += len(ln) + 1
        if cur:
            chunks.append(cur)

        by_kind = summary.get("by_kind") or {}
        kind_summary = (
            ", ".join(f"`{k}`: {n}" for k, n in by_kind.items())
            if by_kind else "(none)"
        )
        builder = card(
            f"⚠ NFT Drift Report  ·  {len(drifts)} rows",
            color=C_ERROR,
        )
        builder = builder.field(
            "Summary",
            f"Total abs drift: **{int(summary.get('total_abs_drift') or 0)}**\n"
            f"By kind: {kind_summary}",
            False,
        )
        for i, chunk in enumerate(chunks):
            label = (
                "Top drifts (largest first)"
                if i == 0
                else f"Top drifts ({i + 1})"
            )
            value = "\n".join(chunk)
            if i == len(chunks) - 1 and more:
                value = value + more
            builder = builder.field(label, value, False)
        builder = builder.footer(
            "Drift > 0 means JSONB has more units than NFT (NFT layer "
            "missed a mint). Drift < 0 means NFT has more (missed a "
            "burn or an extra mint)."
        )
        await ctx.reply(embed=builder.build(), mention_author=False)

    @admin_items.command(name="backfill", aliases=["mint", "rerun"])
    @_require_manage_guild()
    async def admin_items_backfill(self, ctx: DiscoContext) -> None:
        """Force re-run the per-unit NFT backfill across every inventory.

        Mints a token for every owned unit that doesn't already have one
        in item_instances. Per-row idempotency guards prevent
        double-mints. Use this when ``,admin items reconcile`` shows
        drift > 0 (JSONB has units the NFT layer missed).
        """
        from services import nft_backfill as _bf
        await ctx.reply(
            embed=card(
                "\U0001F504 NFT backfill running...",
                description=(
                    "Walking every inventory shape and minting a token "
                    "per unminted unit. Will reply with the per-kind "
                    "row count when done."
                ),
                color=C_INFO,
            ).build(),
            mention_author=False,
        )
        try:
            summary = await _bf.run_backfill(ctx.db, force=True)
        except Exception as e:
            await ctx.reply_error(
                f"Backfill aborted: `{type(e).__name__}: {e}`."
            )
            return
        if not summary:
            await ctx.reply_success(
                "Backfill ran clean -- no new tokens needed minting "
                "(everything already has an item_instances row).",
                title="✅ NFT backfill complete",
            )
            return
        lines = [
            f"`{kind}`  ·  `+{n}` minted"
            for kind, n in sorted(
                summary.items(), key=lambda kv: -int(kv[1] or 0),
            )
            if int(n or 0) > 0
        ]
        if not lines:
            lines = ["(no kinds had any unminted units)"]
        embed = (
            card(
                "✅ NFT backfill complete",
                description="\n".join(lines),
                color=C_SUCCESS,
            )
            .footer("Run `,admin items reconcile` to re-verify drift.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── Chat threads ──────────────────────────────────────────────────────────

    @admin.group(name="thread", aliases=["threads"], invoke_without_command=True)
    @_require_manage_guild()
    async def admin_thread(self, ctx: DiscoContext) -> None:
        """Moderator tools for Disco chat threads."""
        p = ctx.prefix or "."
        await ctx.reply(
            embed=card(
                "\U0001F9F5 Thread admin",
                description=(
                    f"`{p}admin thread close`  -  close the Disco thread you're in\n"
                ),
                color=C_ERROR,
            ).build(),
            mention_author=False,
        )

    @admin_thread.command(name="close")
    @_require_manage_guild()
    async def admin_thread_close(self, ctx: DiscoContext) -> None:
        """Close the Disco chat thread this command is run in."""
        import services.chat_threads as chat_threads_svc

        ids = getattr(self.bot, "_ai_thread_ids", None) or set()
        ch = ctx.channel
        if not (isinstance(ch, discord.Thread) and ch.id in ids):
            await ctx.reply_error("Run this inside a Disco chat thread to close it.")
            return
        row = await chat_threads_svc.get_thread_row(self.bot.db, ch.id)
        if row is None:
            await ctx.reply_error("This isn't a tracked Disco chat thread.")
            return
        await ctx.reply_success("Closing this thread now.")
        await chat_threads_svc.close_thread(
            self.bot, dict(row), reason=f"Closed by admin {ctx.author}"
        )

    # ── Currency management ───────────────────────────────────────────────────

    @admin.command(name="give")
    @_require_manage_guild()
    async def admin_give(self, ctx: DiscoContext, target: discord.Member, amount: float, token: str = "USD") -> None:
        """Add balance/holding to a user."""
        if amount <= 0:
            await ctx.reply_error("Amount must be positive.")
            return
        gid = ctx.guild_id
        token = token.upper()
        await ctx.db.ensure_user(target.id, gid)
        if token == "USD":
            await ctx.db.update_wallet(target.id, gid, to_raw(amount))
            desc = f"Added **${amount:,.2f}** to {target.mention}'s wallet."
        else:
            await ctx.db.update_holding(target.id, gid, token, to_raw(amount))
            desc = f"Added **{amount:,.4f} {token}** to {target.mention}'s holdings."
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="give",
            target_id=target.id, severity=SEVERITY_WARN,
            details=f"{amount} {token}",
        )
        await ctx.reply_success(desc, title="✅ Give")

    @admin.command(name="take")
    @_require_manage_guild()
    async def admin_take(self, ctx: DiscoContext, target: discord.Member, amount: float, token: str = "USD") -> None:
        """Remove balance/holding from a user."""
        if amount <= 0:
            await ctx.reply_error("Amount must be positive.")
            return
        gid = ctx.guild_id
        token = token.upper()
        await ctx.db.ensure_user(target.id, gid)
        if token == "USD":
            row = await ctx.db.get_user(target.id, gid)
            take = min(amount, row.h("wallet") if row else 0.0)
            await ctx.db.update_wallet(target.id, gid, to_raw(-take))
            desc = f"Removed **${take:,.2f}** from {target.mention}'s wallet."
        else:
            h = await ctx.db.get_holding(target.id, gid, token)
            take = min(amount, to_human(h["amount"]) if h else 0.0)
            await ctx.db.update_holding(target.id, gid, token, to_raw(-take))
            desc = f"Removed **{take:,.4f} {token}** from {target.mention}'s holdings."
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="take",
            target_id=target.id, severity=SEVERITY_WARN,
            details=f"{take} {token}",
        )
        await ctx.reply_success(desc, title="✅ Take")

    @admin.command(name="setbal")
    @_require_manage_guild()
    async def admin_setbal(self, ctx: DiscoContext, target: discord.Member, amount: float, token: str = "USD") -> None:
        """Set exact balance/holding for a user."""
        if amount < 0:
            await ctx.reply_error("Amount cannot be negative.")
            return
        gid = ctx.guild_id
        token = token.upper()
        await ctx.db.ensure_user(target.id, gid)
        if token == "USD":
            await ctx.db.set_wallet(target.id, gid, to_raw(amount))
            desc = f"Set {target.mention}'s wallet to **${amount:,.2f}**."
        else:
            await ctx.db.set_holding(target.id, gid, token, to_raw(amount))
            desc = f"Set {target.mention}'s {token} holdings to **{amount:,.4f}**."
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="setbal",
            target_id=target.id, severity=SEVERITY_WARN,
            details=f"{amount} {token}",
        )
        await ctx.reply_success(desc, title="✅ Set Balance")

    @admin.command(name="setjob")
    @_require_manage_guild()
    async def admin_setjob(self, ctx: DiscoContext, target: discord.Member, *, job: str) -> None:
        """Set a user's job tier. Usage: .admin setjob @user <job name or id>
        Job IDs come from Config.JOB_ORDER (HOMELESS through SATOSHI)."""
        job_key = job.upper().replace(" ", "_")
        # Allow partial/friendly match (e.g. "trader" or "Trader" -> "TRADER")
        if job_key not in Config.JOBS:
            # Try matching by title
            job_key = next(
                (k for k, v in Config.JOBS.items() if v["title"].upper() == job.upper()),
                None,
            )
        if not job_key:
            valid = ", ".join(f"`{v['title']}`" for v in Config.JOBS.values())
            await ctx.reply_error(f"Unknown job. Valid: {valid}")
            return
        job_cfg = Config.JOBS[job_key]
        gid = ctx.guild_id
        await ctx.db.ensure_user(target.id, gid)
        current = await ctx.db.get_user_job(target.id, gid)
        # Set work_count to the job's minimum so they qualify, preserve total_earned
        new_work = max(current.get("work_count", 0), job_cfg["min_work"])
        await ctx.db.update_job(target.id, gid, job_key, new_work, current.get("total_earned", 0.0))
        await ctx.reply_success(
            f"Set {target.mention}'s job to **{job_cfg['title']}**.",
            title="Job Updated",
        )

    # ── Reset group (consolidates all destructive reset commands) ─────────────

    @admin.group(name="reset", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_reset(self, ctx: DiscoContext) -> None:
        """Destructive reset commands. Usage: .admin reset <user|server|economy|chain|chainall|supply>"""
        if await suggest_subcommand(ctx, self.admin_reset):
            return
        p = ctx.prefix or "."
        _b = card("🗑 Admin Reset", color=C_ERROR)
        _b.field("\u200b", (
            f"`{p}admin reset user @user`  -  wipe all data for a single user\n"
            f"`{p}admin reset server`  -  wipe ALL server data (confirmation required)\n"
            f"`{p}admin reset economy`  -  wipe user data, keep pools and prices\n"
            f"`{p}admin reset chain <SYM>`  -  reset a PoW chain to block 0\n"
            f"`{p}admin reset chainall`  -  reset ALL PoW chains to block 0\n"
            f"`{p}admin reset supply <token>`  -  reset supply and wipe player balances\n\n"
            f"⚠️ All reset commands are **destructive** and cannot be undone.\n"
            f"For non-destructive updates, use `{p}admin refresh`."
        ), False)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_reset.command(name="user")
    @_require_manage_guild()
    async def admin_reset_user(self, ctx: DiscoContext, target: discord.Member) -> None:
        """Wipe all economy data for a user. Alias for .admin resetuser."""
        await ctx.invoke(self.admin_resetuser, target)

    @admin_reset.command(name="server")
    @_require_manage_guild()
    async def admin_reset_server(self, ctx: DiscoContext) -> None:
        """Wipe all economy data for the entire server. Alias for .admin resetserver."""
        await ctx.invoke(self.admin_resetserver)

    @admin_reset.command(name="economy")
    @_require_manage_guild()
    async def admin_reset_economy(self, ctx: DiscoContext) -> None:
        """Reset user data but keep pools/prices. Alias for .admin reseteconomy."""
        await ctx.invoke(self.admin_reseteconomy)

    @admin_reset.command(name="chain")
    @_require_manage_guild()
    async def admin_reset_chain(self, ctx: DiscoContext, chain: str) -> None:
        """Reset a single PoW chain to block 0. Alias for .admin chain reset <chain>."""
        await ctx.invoke(self.admin_chain_reset, chain)

    @admin_reset.command(name="chainall")
    @_require_manage_guild()
    async def admin_reset_chainall(self, ctx: DiscoContext) -> None:
        """Reset ALL PoW chains to block 0. Alias for .admin chain resetall."""
        await ctx.invoke(self.admin_chain_resetall)

    @admin_reset.command(name="supply")
    @_require_manage_guild()
    async def admin_reset_supply(self, ctx: DiscoContext, token: str) -> None:
        """Reset supply and wipe player balances. Alias for .admin supply reset <token>."""
        await ctx.invoke(self.admin_supply_reset, token)

    # ── Cooldown reset ────────────────────────────────────────────────────────

    @admin.command(name="cooldown", aliases=["resetcd", "cd"])
    @_require_manage_guild()
    async def admin_cooldown(self, ctx: DiscoContext, target: discord.Member) -> None:
        """Reset all command cooldowns for a player. Usage: .admin cooldown @user"""
        import copy
        from core.framework.embed import card
        from core.framework.ui import C_SUCCESS
        fake_msg = copy.copy(ctx.message)
        object.__setattr__(fake_msg, "author", target)
        for cmd in self.bot.walk_commands():
            try:
                if hasattr(cmd, "_buckets") and cmd._buckets and cmd._buckets._cooldown:
                    bucket = cmd._buckets.get_bucket(fake_msg)
                    if bucket:
                        bucket.reset()
            except Exception:
                pass

        await ctx.db.execute(
            "UPDATE users SET last_daily = NULL, last_work = NULL "
            "WHERE user_id=$1 AND guild_id=$2",
            target.id, ctx.guild_id,
        )

        embed = (
            card("Cooldowns Reset", color=C_SUCCESS)
            .description(f"All command cooldowns cleared for **{target.display_name}**.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── Economy rollback ──────────────────────────────────────────────────────

    @admin.command(name="rollback")
    @_require_manage_guild()
    async def admin_rollback(self, ctx: DiscoContext, minutes: int = 60) -> None:
        """Roll back the server economy to a snapshot from ~N minutes ago (default 60).
        Restores: wallets, all token holdings, token prices, and pool reserves.
        Usage: .admin rollback [minutes]"""
        if minutes < 1 or minutes > 2880:
            await ctx.reply_error("Minutes must be between 1 and 2880 (48h).")
            return

        lock = self._rollback_locks.setdefault(ctx.guild_id, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("A rollback is already in progress for this server. Wait for it to finish.")
            return

        async with lock:
            await self._do_rollback(ctx, minutes)

    async def _do_rollback(self, ctx: DiscoContext, minutes: int) -> None:
        target_ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        snap = await ctx.db.snapshots.get_nearest_snapshot(ctx.guild_id, target_ts)
        if not snap:
            await ctx.reply_error(
                "No snapshots found. The bot takes snapshots every 30 minutes - "
                "wait for the first one before using rollback."
            )
            return

        taken_at = snap["taken_at"]  # epoch float from _coerce
        age_mins = int((datetime.now(timezone.utc).timestamp() - taken_at) / 60)

        def _ensure_list(val: object) -> list:
            if isinstance(val, str):
                val = json.loads(val)
            return val if isinstance(val, list) else []

        snap_wallets = _ensure_list(snap["wallets"])
        snap_prices = _ensure_list(snap["prices"])
        snap_pools = _ensure_list(snap["pools"])

        user_count = len(snap_wallets)
        price_count = len(snap_prices)
        pool_count = len(snap_pools)

        # Build a price diff for the top tokens
        current_prices = await ctx.db.get_all_prices(ctx.guild_id)
        snap_price_map = {r["symbol"]: float(r["price"]) for r in snap_prices}
        price_lines = []
        for cp in current_prices[:8]:
            sym = cp["symbol"]
            cur = float(cp["price"])
            old = snap_price_map.get(sym)
            if old is None:
                continue
            if abs(cur - old) / max(old, 1e-9) > 0.001:
                price_lines.append(f"**{sym}**: ${old:,.4f} (now ${cur:,.4f})")

        price_diff = "\n".join(price_lines) if price_lines else "No significant price changes."

        desc = (
            f"Snapshot taken **{age_mins} minutes ago** ({fmt_ts(taken_at, '%H:%M UTC')})\n\n"
            f"**Users affected:** {user_count}\n"
            f"**Prices restored:** {price_count}\n"
            f"**Pools restored:** {pool_count}\n\n"
            f"**Price changes to revert:**\n{price_diff}\n\n"
            f"This will overwrite current wallets, holdings, prices, and pool reserves.\n"
            f"⚠️ This **cannot be undone**."
        )
        confirmed = await ctx.confirm(desc)
        if not confirmed:
            await ctx.reply_error("Rollback cancelled.")
            return

        result = await ctx.db.snapshots.restore_snapshot(ctx.guild_id, snap["id"])
        await ctx.reply_success(
            f"Economy rolled back to snapshot from **{age_mins} minutes ago**.\n"
            f"Restored {result['users_restored']} users, "
            f"{result['prices_restored']} prices, "
            f"{result['pools_restored']} pools, "
            f"{result['stones_restored']} stones, "
            f"{result['lp_restored']} LP positions.",
            title="Economy Rollback Complete",
        )

    @admin.command(name="snapshots")
    @_require_manage_guild()
    async def admin_snapshots(self, ctx: DiscoContext) -> None:
        """List available economy snapshots for rollback."""

        snaps = await ctx.db.snapshots.list_snapshots(ctx.guild_id, limit=10)
        if not snaps:
            await ctx.reply_error("No snapshots yet. They are taken every 30 minutes.")
            return

        now_ts = datetime.now(timezone.utc).timestamp()
        lines = []
        for s in snaps:
            ta = s["taken_at"]  # epoch float from _coerce
            age_mins = int((now_ts - ta) / 60)
            lines.append(
                f"`#{s['id']}` - {fmt_ts(ta, '%H:%M UTC')} "
                f"({age_mins}m ago) - {s['user_count']} users, {s['price_count']} prices"
            )
        _b = card("📸 Economy Snapshots", color=C_INFO)
        _b.field("Available", "\n".join(lines), False)
        _b.field("\u200b", f"Use `.admin rollback <minutes>` to restore.", False)
        await ctx.reply(embed=_b.build(), mention_author=False)

    # ── .admin health ─────────────────────────────────────────────────────────

    @admin.group(name="health", invoke_without_command=True)
    @_require_manage_guild()
    async def admin_health(self, ctx: DiscoContext) -> None:
        """Health check, heal, and diagnostics tools."""
        if ctx.invoked_subcommand is not None:
            return
        p = ctx.prefix or Config.PREFIX
        _b = card("🏥 Admin Health", color=C_INFO)
        _b.description(
            f"`{p}admin health check`    -  full guild diagnostic\n"
            f"`{p}admin health heal`     -  auto-fix issues + cleanup old DB rows if needed\n"
            f"`{p}admin health diag`     -  system-level diagnostics\n"
            f"`{p}admin health test`     -  inject fault for heal testing\n"
            f"`{p}admin health notify`   -  toggle self-heal notifications\n"
            f"`{p}admin health analyze`  -  AI breakdown of issues\n"
            f"`{p}admin health cleanup [days]`  -  purge old DB rows (default 90d)\n"
        )
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_health.command(name="check")
    @_require_manage_guild()
    async def admin_health_check(self, ctx: DiscoContext) -> None:
        """Run a full health diagnostic for this guild."""
        cog = self.bot.get_cog("Health")
        if cog is None:
            await ctx.reply_error("Health cog not loaded.")
            return
        await cog.health_check(ctx)

    @admin_health.command(name="heal")
    @_require_manage_guild()
    async def admin_health_heal(self, ctx: DiscoContext) -> None:
        """Run health diagnostic and auto-fix what is possible. Automatically cleans up old DB rows if needed."""
        cog = self.bot.get_cog("Health")
        if cog is None:
            await ctx.reply_error("Health cog not loaded.")
            return
        await cog.health_heal(ctx)

    @admin_health.command(name="test")
    @_require_manage_guild()
    async def admin_health_test(self, ctx: DiscoContext, *, mode: str = "") -> None:
        """Inject a simulated task-loop failure. Optionally pass `autofix` to heal immediately."""
        cog = self.bot.get_cog("Health")
        if cog is None:
            await ctx.reply_error("Health cog not loaded.")
            return
        await cog.health_test(ctx, mode=mode)

    @admin_health.command(name="notify")
    @_require_manage_guild()
    async def admin_health_notify(self, ctx: DiscoContext, state: str = "") -> None:
        """Show or toggle self-heal error notifications (on/off)."""
        cog = self.bot.get_cog("Health")
        if cog is None:
            await ctx.reply_error("Health cog not loaded.")
            return
        await cog.health_notify(ctx, state=state)

    @admin_health.command(name="analyze")
    @_require_manage_guild()
    async def admin_health_analyze(self, ctx: DiscoContext) -> None:
        """Have AI explain every health issue with fix steps."""
        cog = self.bot.get_cog("Health")
        if cog is None:
            await ctx.reply_error("Health cog not loaded.")
            return
        await cog.health_analyze(ctx)

    @admin_health.command(name="diag")
    @_require_manage_guild()
    async def admin_health_diag(self, ctx: DiscoContext, target: str = "all") -> None:
        """Run system diagnostics. Targets: all, db, cogs, api, modules, services, commands, integrity."""
        cmd = self.bot.get_command("diagnose")
        if cmd is None:
            await ctx.reply_error("diagnose command not found.")
            return
        await ctx.invoke(cmd, target=target)

    @admin_health.command(name="cleanup")
    @_require_manage_guild()
    async def admin_health_cleanup(self, ctx: DiscoContext, days: int = 90) -> None:
        """Delete old transactions, game results, and price candles older than N days."""
        if days < 1:
            await ctx.reply_error("Days must be at least 1.")
            return

        tables = [
            ("transactions", "ts"),
            ("game_results", "played_at"),
            ("price_candles", "ts"),
        ]
        counts: dict[str, int] = {}
        for table, col in tables:
            try:
                result = await ctx.db.execute(
                    f"DELETE FROM {table} WHERE guild_id=$1 AND {col} < now() - $2::interval",
                    ctx.guild_id,
                    f"{days} days",
                )
                counts[table] = int(result.split()[-1])
            except Exception:
                counts[table] = 0

        total = sum(counts.values())
        _b = card("🗑️ DB Cleanup Complete", color=C_INFO)
        _b.description(
            f"Deleted records older than **{days} days**:\n\n"
            + "\n".join(f"• **{t}**: {c:,} rows" for t, c in counts.items())
            + f"\n\n**Total**: {total:,} rows deleted"
        )
        await ctx.reply(embed=_b.build(), mention_author=False)

    # ── User / server reset ───────────────────────────────────────────────────

    @admin.command(name="resetuser")
    @_require_manage_guild()
    async def admin_resetuser(self, ctx: DiscoContext, target: discord.Member) -> None:
        """Wipe all economy data for a user (with confirmation)."""
        confirmed = await ctx.confirm(
            f"This will wipe **all** economy data for {target.mention}. Are you sure?"
        )
        if confirmed:
            await ctx.db.reset_user(target.id, ctx.guild_id)
            await ctx.reply_success(f"All data for {target.mention} has been wiped.", title="✅ User Reset")
        else:
            await ctx.reply_error("Reset cancelled.")

    @admin.command(name="resetserver")
    @_require_manage_guild()
    async def admin_resetserver(self, ctx: DiscoContext) -> None:
        """Wipe all economy data for the entire server (with confirmation)."""
        confirmed = await ctx.confirm(
            f"This will wipe **ALL** economy data for **{ctx.guild.name}**  -  "
            "balances, crypto, stakes, loans, pools, mining  -  everything. **Cannot be undone.**"
        )
        if confirmed:
            totals = await ctx.db.reset_guild(ctx.guild_id)
            await ctx.db.seed_prices(ctx.guild_id)
            await ctx.db.seed_pools(ctx.guild_id)

            # Build summary lines for tables that had data
            _LABELS = {
                "users":               "Users",
                "crypto_holdings":     "CeFi Holdings",
                "wallet_holdings":     "DeFi Wallet Holdings",
                "stakes":              "Stakes",
                "loans":               "Loans",
                "mining_rigs":         "Mining Rigs",
                "mining_pool_members": "Pool Members",
                "lp_positions":        "LP Positions",
                "lp_snapshots":        "LP Snapshots",
                "user_jobs":           "User Jobs",
                "pools":               "Pools",
                "crypto_prices":       "Prices",
                "mining_network":      "Mining Network",
                "mining_blocks":       "Mining Blocks",
                "transactions":        "Transactions",
                "chain_blocks":        "Chain Blocks",
                "mining_groups":       "Mining Groups",
                "mining_group_members":"Group Members",
                "wallet_addresses":    "Wallet Addresses",
                "token_contracts":     "Token Contracts",
                "mempool":             "Mempool Entries",
                "validator_blocks":    "Validator Blocks",
                "pos_validators":      "PoS Validators",
                "guild_treasury":      "Treasury",
                "network_base_fees":   "Base Fees",
                "smart_contracts":     "Smart Contracts",
                "contract_events":     "Contract Events",
                "price_candles":       "Price Candles",
                "guild_tokens":        "Custom Tokens",
                "guild_networks":      "Custom Networks",
                "user_prefs":          "User Prefs",
                "user_mining_config":  "Mining Configs",
                "mining_group_weights":"Group Weights",
                "hashstones":           "Hashstones",
                "lockstones":          "Lockstones",
                "vaultstones":         "Vaultstones",
                "savings_deposits":    "Savings Deposits",
                "pos_delegations":     "PoS Delegations",
            }
            summary_lines = [
                f"`{_LABELS.get(t, t)}`: {n:,}"
                for t, n in sorted(totals.items(), key=lambda x: -x[1])
            ]
            description = (
                "\n".join(summary_lines) if summary_lines
                else "No data was present."
            ) + "\n\nDefault pools have been reseeded."

            await ctx.reply_success(description, title=f"✅ {ctx.guild.name} Reset")
        else:
            await ctx.reply_error("Reset cancelled.")

    @admin.command(name="reseteconomy")
    @_require_manage_guild()
    async def admin_reseteconomy(self, ctx: DiscoContext) -> None:
        """Reset user balances, holdings, items, stakes, validators, tokens, and settings."""
        confirmed = await ctx.confirm(
            f"This will wipe **all user data** for **{ctx.guild.name}**  -  "
            "balances, crypto holdings, stakes, items, loans, mining rigs, savings, "
            "validators, custom tokens, networks, and user settings  -  "
            "but **keeps** pools and prices. **Cannot be undone.**"
        )
        if confirmed:
            economy_tables = [
                "users", "crypto_holdings", "wallet_holdings", "stakes",
                "loans", "savings_deposits",
                "mining_rigs", "mining_pool_members", "lp_positions", "lp_snapshots",
                "user_jobs", "transactions", "mining_blocks",
                "mining_groups", "mining_group_members",
                "wallet_addresses", "mempool",
                "hashstones", "lockstones", "vaultstones",
                "user_prefs", "user_settings", "user_mining_config", "mining_group_weights",
                "group_invites", "group_upgrades",
                "pos_delegations", "pos_validators",
                "guild_tokens", "guild_networks",
            ]
            totals: dict[str, int] = {}
            for table in economy_tables:
                try:
                    row = await ctx.db.fetch_one(
                        f"SELECT COUNT(*) AS cnt FROM {table} WHERE guild_id=$1", ctx.guild_id,
                    )
                    count = row["cnt"] if row else 0
                    await ctx.db.execute(f"DELETE FROM {table} WHERE guild_id=$1", ctx.guild_id)
                    if count > 0:
                        totals[table] = count
                except Exception:
                    pass
            _LABELS = {
                "users": "Users", "crypto_holdings": "CeFi Holdings",
                "wallet_holdings": "DeFi Holdings", "stakes": "Stakes",
                "loans": "Loans",
                "savings_deposits": "Savings", "mining_rigs": "Rigs",
                "mining_pool_members": "Pool Members", "lp_positions": "LP Positions",
                "user_jobs": "Jobs", "transactions": "Transactions",
                "hashstones": "Hashstones", "lockstones": "Lockstones",
                "vaultstones": "Vaultstones", "pos_delegations": "Delegations",
                "pos_validators": "PoS Validators", "user_settings": "User Settings",
                "guild_tokens": "Custom Tokens", "guild_networks": "Custom Networks",
            }
            summary = [f"`{_LABELS.get(t, t)}`: {n:,}" for t, n in sorted(totals.items(), key=lambda x: -x[1])]
            desc = "\n".join(summary) if summary else "No user data was present."
            desc += "\n\nPools and prices are untouched."
            await ctx.reply_success(desc, title=f"✅ Economy Reset  -  {ctx.guild.name}")
        else:
            await ctx.reply_error("Reset cancelled.")

    # ── Refresh (non-destructive price / chain / supply update) ──────────────

    @admin.command(name="refresh")
    @_require_manage_guild()
    async def admin_refresh(self, ctx: DiscoContext, *, target: str = "") -> None:
        """Non-destructive price and chain data refresh. Player assets are never touched.
        Usage: .admin refresh <TOKEN|all>
          .admin refresh all    -  reset all prices to defaults, all PoW chains to block 0, recalculate supply
          .admin refresh MTA    -  refresh a single token's price + chain data"""
        target = target.strip()
        if not target:
            p = ctx.prefix or "."
            _b = card("🔄 Admin Refresh", color=C_INFO)
            _b.field("\u200b", (
                f"`{p}admin refresh all`  -  reset **all** prices to defaults, all PoW chains "
                f"to block 0, and recalculate circulating supply from player holdings\n"
                f"`{p}admin refresh <TOKEN>`  -  refresh a single token's price and chain data\n"
                f"  Example: `{p}admin refresh MTA`\n\n"
                "✅ **Player balances, stakes, rigs, validators, pools, and all other assets "
                "are never affected by refresh commands.**\n\n"
                f"For destructive resets, use `{p}admin reset`."
            ), False)
            await ctx.reply(embed=_b.build(), mention_author=False)
            return
        if target.lower() == "all":
            await self._do_refresh_all(ctx)
        else:
            await self._do_refresh_token(ctx, target.upper())

    async def _recalculate_token_supply(self, ctx: DiscoContext, symbol: str, max_supply: float) -> int:
        """Return the circulating supply for *symbol* as a raw NUMERIC(36,0) integer.

        Sums CeFi holdings, DeFi wallet holdings, staked amounts, and AMM pool
        reserves (all raw-scaled).  Caps at *max_supply* (human units) when > 0.
        Falls back to 50% of *max_supply* when no player holds the token yet.
        Returns a raw int suitable for direct storage in circulating_supply columns.
        """
        cefi = await ctx.db.fetch_val(
            "SELECT COALESCE(SUM(amount), 0) FROM crypto_holdings WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, symbol,
        )
        defi = await ctx.db.fetch_val(
            "SELECT COALESCE(SUM(amount), 0) FROM wallet_holdings WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, symbol,
        )
        staked = await ctx.db.fetch_val(
            "SELECT COALESCE(SUM(amount), 0) FROM stakes WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, symbol,
        )
        pool_a = await ctx.db.fetch_val(
            "SELECT COALESCE(SUM(reserve_a), 0) FROM pools WHERE guild_id=$1 AND token_a=$2",
            ctx.guild_id, symbol,
        )
        pool_b = await ctx.db.fetch_val(
            "SELECT COALESCE(SUM(reserve_b), 0) FROM pools WHERE guild_id=$1 AND token_b=$2",
            ctx.guild_id, symbol,
        )
        # All DB amounts are raw NUMERIC(36,0); convert to human for arithmetic
        in_pools_h = to_human(int(float(pool_a or 0))) + to_human(int(float(pool_b or 0)))
        player_held_h = (
            to_human(int(float(cefi or 0)))
            + to_human(int(float(defi or 0)))
            + to_human(int(float(staked or 0)))
        )
        circulating_h = player_held_h + in_pools_h
        if max_supply > 0:
            circulating_h = min(circulating_h, max_supply)
        initial_supply_h = max_supply * 0.5 if max_supply else 0.0
        final_h = circulating_h if circulating_h > 0 else initial_supply_h
        return to_raw(final_h)

    async def _do_refresh_all(self, ctx: DiscoContext) -> None:
        """Non-destructively refresh all prices, chains, and supply to defaults."""
        confirmed = await ctx.confirm(
            "**Full economic refresh.**\n"
            "This will:\n"
            "• Reset ALL token prices to their default starting values\n"
            "• Reset ALL PoW chains to block 0 (height, difficulty, reward)\n"
            "• Clear PoW block and mining history\n"
            "• Recalculate circulating supply from actual player holdings\n\n"
            "**Player balances, stakes, rigs, validators, pools, and all assets are NOT affected.**"
        )
        if not confirmed:
            await ctx.reply_error("Refresh cancelled.")
            return

        # 1. Reset all prices to config defaults
        price_lines = []
        for symbol, token_cfg in Config.TOKENS.items():
            start = token_cfg["start_price"]
            await ctx.db.execute(
                """UPDATE crypto_prices
                   SET price=$3, open_price=$3, day_high=$3, day_low=$3
                   WHERE guild_id=$1 AND symbol=$2""",
                ctx.guild_id, symbol, start,
            )
            price_lines.append(f"**{symbol}**: `${start:,.4f}`")

        # 2. Reset all PoW chains to block 0
        chain_lines = []
        for symbol, cfg in Config.POW_NETWORKS.items():
            await ctx.db.execute(
                """UPDATE pow_network_state
                   SET block_height=0, total_hashrate=0, current_reward=$3,
                       difficulty=$4, last_block_ts=now(), last_retarget_height=0,
                       last_retarget_ts=now()
                   WHERE guild_id=$1 AND chain_symbol=$2""",
                ctx.guild_id, symbol,
                cfg.get("initial_reward", 1.0),
                cfg.get("initial_difficulty", 60000.0),
            )
            chain_lines.append(
                f"**{symbol}**: block 0 "
                f"(diff `{cfg.get('initial_difficulty', 60000.0):,.0f}`, "
                f"reward `{cfg.get('initial_reward', 1.0)}`)"
            )
        # Reset legacy mining_network row
        await ctx.db.execute(
            "UPDATE mining_network SET block_height=0, total_hashrate=0, current_reward=50.0, "
            "last_block_ts=now() WHERE guild_id=$1",
            ctx.guild_id,
        )
        # Clear block history
        await ctx.db.execute("DELETE FROM chain_blocks WHERE guild_id=$1", ctx.guild_id)
        try:
            await ctx.db.execute("DELETE FROM mining_blocks WHERE guild_id=$1", ctx.guild_id)
        except Exception:
            pass

        # 3. Recalculate circulating supply from actual player holdings
        supply_lines = []
        for symbol, token_cfg in Config.TOKENS.items():
            max_supply = float(token_cfg.get("max_supply", 0))
            final_supply = await self._recalculate_token_supply(ctx, symbol, max_supply)
            await ctx.db.execute(
                "UPDATE crypto_prices SET circulating_supply=$1 WHERE guild_id=$2 AND symbol=$3",
                final_supply, ctx.guild_id, symbol,
            )
            await ctx.db.execute(
                "UPDATE guild_tokens SET circulating_supply=$1 WHERE guild_id=$2 AND symbol=$3",
                final_supply, ctx.guild_id, symbol,
            )
            final_supply_h = to_human(final_supply)
            pct = f" ({final_supply_h / max_supply * 100:.1f}%)" if max_supply > 0 else ""
            supply_lines.append(f"**{symbol}**: `{final_supply_h:,.2f}` / `{max_supply:,}`{pct}")

        description = (
            "**Prices reset to defaults:**\n" + "\n".join(price_lines) + "\n\n"
            "**PoW chains reset to block 0:**\n" + "\n".join(chain_lines) + "\n\n"
            "**Supply recalculated from player holdings:**\n" + "\n".join(supply_lines) + "\n\n"
            "Player balances, stakes, rigs, validators, and pools are untouched."
        )
        await ctx.reply_success(description, title="🔄 Full Economic Refresh Complete")

    async def _do_refresh_token(self, ctx: DiscoContext, symbol: str) -> None:
        """Non-destructively refresh a single token's price and chain data."""
        token_cfg = Config.TOKENS.get(symbol)
        if not token_cfg:
            await ctx.reply_error(
                f"Unknown token `{symbol}`.\n"
                f"Use `.admin refresh all` to refresh all tokens, "
                f"or check `.admin listtokens` for valid symbols."
            )
            return

        start_price = token_cfg["start_price"]
        max_supply = token_cfg.get("max_supply", 0)
        pow_cfg = Config.POW_NETWORKS.get(symbol)

        confirm_lines = [
            f"Refresh **{symbol}**?",
            f"• Price → `${start_price:,.4f}` (default from config)",
        ]
        if pow_cfg:
            confirm_lines.append(
                f"• Chain → block 0  "
                f"(difficulty: `{pow_cfg.get('initial_difficulty', 60000.0):,.0f}`, "
                f"reward: `{pow_cfg.get('initial_reward', 1.0)}`)"
            )
            confirm_lines.append("• Block history cleared")
        confirm_lines.append("• Circulating supply recalculated from player holdings")
        confirm_lines.append("\n**Player balances, stakes, and all assets are NOT affected.**")

        confirmed = await ctx.confirm("\n".join(confirm_lines))
        if not confirmed:
            await ctx.reply_error("Refresh cancelled.")
            return

        # Reset price to config default
        await ctx.db.execute(
            """UPDATE crypto_prices
               SET price=$3, open_price=$3, day_high=$3, day_low=$3
               WHERE guild_id=$1 AND symbol=$2""",
            ctx.guild_id, symbol, start_price,
        )
        result_lines = [f"Price reset to `${start_price:,.4f}`"]

        # If PoW, reset chain to block 0 and clear block history
        if pow_cfg:
            initial_reward = pow_cfg.get("initial_reward", 1.0)
            initial_diff = pow_cfg.get("initial_difficulty", 60000.0)
            await ctx.db.execute(
                """UPDATE pow_network_state
                   SET block_height=0, total_hashrate=0, current_reward=$3,
                       difficulty=$4, last_block_ts=now(), last_retarget_height=0,
                       last_retarget_ts=now()
                   WHERE guild_id=$1 AND chain_symbol=$2""",
                ctx.guild_id, symbol, initial_reward, initial_diff,
            )
            await ctx.db.execute(
                "DELETE FROM chain_blocks WHERE guild_id=$1 AND network=$2",
                ctx.guild_id, symbol.lower(),
            )
            result_lines.append(
                f"Chain reset to block 0 "
                f"(difficulty: `{initial_diff:,.0f}`, reward: `{initial_reward}`)"
            )
            result_lines.append("Block history cleared")

        # Recalculate circulating supply from actual player holdings
        max_supply = float(max_supply)
        final_supply = await self._recalculate_token_supply(ctx, symbol, max_supply)
        await ctx.db.execute(
            "UPDATE crypto_prices SET circulating_supply=$1 WHERE guild_id=$2 AND symbol=$3",
            final_supply, ctx.guild_id, symbol,
        )
        await ctx.db.execute(
            "UPDATE guild_tokens SET circulating_supply=$1 WHERE guild_id=$2 AND symbol=$3",
            final_supply, ctx.guild_id, symbol,
        )
        final_supply_h = to_human(final_supply)
        pct = f" ({final_supply_h / max_supply * 100:.1f}%)" if max_supply > 0 else ""
        result_lines.append(f"Supply recalculated: `{final_supply_h:,.2f}` / `{max_supply:,}`{pct}")
        result_lines.append("Player balances untouched.")

        await ctx.reply_success("\n".join(result_lines), title=f"🔄 {symbol} Refreshed")

    # ── Session log export ───────────────────────────────────────────────────

    @admin.command(name="log")
    @_require_manage_guild()
    @_require_debug()
    async def admin_log(self, ctx: DiscoContext) -> None:
        """Upload the session debug log with a parsed summary. Usage: .admin log"""
        from core.framework.session_log import LOG_PATH
        import io
        import re
        from collections import Counter, defaultdict

        if not LOG_PATH.exists():
            await ctx.reply_error("No session log found. The bot may have just started.")
            return

        loop = asyncio.get_event_loop()
        lines = await loop.run_in_executor(
            None, lambda: open(LOG_PATH, "r", encoding="utf-8", errors="replace").readlines()
        )

        # ── Parse ──────────────────────────────────────────────────────────
        session_start = None
        errors: list[dict] = []          # {ts, cmd, input, etype}
        discord_warns: list[str] = []    # raw warn lines
        cmd_counts: Counter = Counter()
        user_cmd_counts: Counter = Counter()
        valblock: dict = defaultdict(lambda: {"blocks": 0, "confirmed": 0, "rejected": 0, "gas": 0.0})
        chain_blocks: dict = defaultdict(lambda: {"blocks": 0, "txns": 0})
        mempool_counts: Counter = Counter()
        mining_blocks = 0
        mining_reward = 0.0
        event_counts: Counter = Counter()

        i = 0
        _line_re = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] \[(\w+\s*)\] (.*)$")
        _session_re = re.compile(r"SESSION STARTED\s+(\S+ \S+ UTC)")

        while i < len(lines):
            line = lines[i].rstrip()

            # Session start time
            m = _session_re.search(line)
            if m:
                try:
                    import datetime as _dt
                    session_start = _dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S UTC")
                except Exception:
                    pass
                i += 1
                continue

            m = _line_re.match(line)
            if not m:
                i += 1
                continue

            ts, cat, msg = m.group(1), m.group(2).strip(), m.group(3)

            if cat == "CMD":
                # "user (id)  in  guild (id)  →  .command ..."
                arrow = msg.find("→")
                content = msg[arrow + 1:].strip() if arrow != -1 else msg
                cmd_name = content.split()[0].lstrip(".") if content else "?"
                cmd_counts[cmd_name] += 1
                # extract username
                user_part = msg.split("(")[0].strip() if "(" in msg else "?"
                user_cmd_counts[user_part] += 1

            elif cat == "ERR":
                # Collect multi-line error block
                err_entry = {"ts": ts, "cmd": "?", "input": "?", "etype": "?", "tb_lines": []}
                if msg.startswith("cmd="):
                    parts = dict(p.split("=", 1) for p in msg.split("  ") if "=" in p)
                    err_entry["cmd"] = parts.get("cmd", "?")
                elif msg.startswith("input:"):
                    err_entry["input"] = msg[6:].strip()
                elif msg.startswith("type:"):
                    err_entry["etype"] = msg[5:].strip()
                # Read continuation lines (indented traceback)
                i += 1
                while i < len(lines):
                    next_line = lines[i].rstrip()
                    if next_line.startswith("           "):
                        err_entry["tb_lines"].append(next_line.strip())
                        i += 1
                    elif _line_re.match(next_line):
                        nm = _line_re.match(next_line)
                        if nm and nm.group(2).strip() == "ERR":
                            sub_msg = nm.group(3)
                            if sub_msg.startswith("input:"):
                                err_entry["input"] = sub_msg[6:].strip()
                            elif sub_msg.startswith("type:"):
                                err_entry["etype"] = sub_msg[5:].strip()
                            i += 1
                        else:
                            break
                    else:
                        break
                errors.append(err_entry)
                continue

            elif cat == "DISCORD":
                if "429" in msg or "rate limit" in msg.lower():
                    discord_warns.append(f"`{ts}` {msg[:120]}")

            elif cat == "VALBLOCK":
                # "guild=...  net=Arcadia Network  validator=...  actions=1  ✅=1  ❌=0  gas=..."
                net_m = re.search(r"net=([^\s]+(?:\s+[^\s]+)*?)  ", msg)
                ok_m  = re.search(r"✅=(\d+)", msg)
                bad_m = re.search(r"❌=(\d+)", msg)
                gas_m = re.search(r"gas=([\d.e+-]+)", msg)
                if net_m:
                    net = net_m.group(1).strip()
                    valblock[net]["blocks"] += 1
                    valblock[net]["confirmed"] += int(ok_m.group(1)) if ok_m else 0
                    valblock[net]["rejected"]  += int(bad_m.group(1)) if bad_m else 0
                    valblock[net]["gas"]       += float(gas_m.group(1)) if gas_m else 0.0

            elif cat == "CHAIN":
                # "guild=...  net=arc  block=#5  txns=11  ..."
                net_m = re.search(r"net=(\S+)", msg)
                txn_m = re.search(r"txns=(\d+)", msg)
                if net_m:
                    net = net_m.group(1)
                    chain_blocks[net]["blocks"] += 1
                    chain_blocks[net]["txns"] += int(txn_m.group(1)) if txn_m else 0

            elif cat == "MEMPOOL":
                # "... net=Discoin Network  gas=..."
                net_m = re.search(r"net=([^\s]+(?:\s+[^\s]+)*?)  ", msg)
                if net_m:
                    mempool_counts[net_m.group(1).strip()] += 1
                else:
                    mempool_counts["unknown"] += 1

            elif cat == "MINING":
                mining_blocks += 1
                rew_m = re.search(r"reward=([\d.]+)", msg)
                if rew_m:
                    mining_reward += float(rew_m.group(1))

            elif cat == "EVENT":
                # "event_name   -   ..."
                evt_name = msg.split("  ")[0].strip() if "  " in msg else msg.strip()
                event_counts[evt_name] += 1

            i += 1

        # ── Build embeds ───────────────────────────────────────────────────
        import datetime as _dt
        now_utc = _dt.datetime.utcnow()
        uptime_str = "unknown"
        if session_start:
            delta = now_utc - session_start
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m2, s = divmod(rem, 60)
            uptime_str = f"{h}h {m2}m {s}s" if h else f"{m2}m {s}s"

        from constants.ui import C_WARNING as C_WARN, C_SUCCESS as C_OK, C_ERROR as C_CRIT
        has_issues = bool(errors or discord_warns)

        # ── Embed 1: Overview ─────────────────────────────────────────────
        _e1 = (
            card(
                "📋 Session Log  -  Summary",
                color=C_CRIT if errors else (C_WARN if discord_warns else C_OK),
            )
            .field("Started",  fmt_ts(session_start, "%Y-%m-%d %H:%M UTC") if session_start else "?", True)
            .field("Uptime",   uptime_str,                                                               True)
            .field("Log Size", f"{LOG_PATH.stat().st_size / 1024:.1f} KB",                              True)
        )

        # Commands
        total_cmds = sum(cmd_counts.values())
        if cmd_counts:
            top_cmds = cmd_counts.most_common(8)
            cmd_lines = [f"`{cmd}` ×{n}" for cmd, n in top_cmds]
            if len(cmd_counts) > 8:
                cmd_lines.append(f"…+{len(cmd_counts) - 8} more")
            top_users = user_cmd_counts.most_common(5)
            user_lines = [f"**{u}** ×{n}" for u, n in top_users]
            _e1.field(f"Commands ({total_cmds} total)", "\n".join(cmd_lines), True)
            _e1.field("Top Users", "\n".join(user_lines), True)
        else:
            _e1.field("Commands", "None logged", True)

        # Errors
        if errors:
            err_lines = []
            for e in errors[-6:]:  # last 6 errors
                err_lines.append(f"`{e['ts']}` **{e['cmd']}**  -  {e['etype'][:80]}")
                if e['input'] and e['input'] != '?':
                    err_lines.append(f"-# input: `{e['input'][:60]}`")
            _e1.field(f"⚠️ Errors ({len(errors)})", "\n".join(err_lines)[:1020] or "none", False)

        # Discord warnings
        if discord_warns:
            _e1.field(
                f"🚦 Discord Warnings ({len(discord_warns)})",
                "\n".join(discord_warns[-6:])[:1020],
                False,
            )

        await ctx.reply(embed=_e1.build(), mention_author=False)

        # ── Embed 2: All Activity ─────────────────────────────────────────
        _e2 = card("📊 Session Activity", color=C_INFO)

        # Event groupings  -  map event name fragments to display categories
        _ECON_KEYS    = {"daily_claimed", "work_completed", "gamble_result", "deposited",
                         "withdrew", "balance_updated", "balance_transferred", "gift_sent",
                         "drop_claimed", "loan_taken", "loan_repaid", "savings_deposited",
                         "savings_withdrew", "savings_interest"}
        _TRADE_KEYS   = {"buy_executed", "sell_executed", "trade_executed", "swap_executed",
                         "arb_trade", "oracle_rebalance", "token_sent", "transfer"}
        _STAKE_KEYS   = {"staked", "unstaked", "validator_registered", "validator_slashed",
                         "reward_paid", "validator_action"}
        _POOL_KEYS    = {"lp_added", "lp_removed", "pool_created", "pool_seeded"}
        _CONTRACT_KEYS= {"contract_deployed", "contract_called", "contract_event"}
        _CHAIN_KEYS   = {"block_bundled", "validator_block", "mining_block", "block_found",
                         "mempool_submitted"}
        _SKIP_KEYS    = {"prices_updated"}

        econ_events: Counter    = Counter()
        trade_events: Counter   = Counter()
        stake_events: Counter   = Counter()
        pool_events: Counter    = Counter()
        contract_events: Counter= Counter()
        chain_events: Counter   = Counter()
        other_events: Counter   = Counter()

        for evt_name, count in event_counts.items():
            if evt_name in _SKIP_KEYS:
                continue
            elif evt_name in _ECON_KEYS:
                econ_events[evt_name] += count
            elif evt_name in _TRADE_KEYS:
                trade_events[evt_name] += count
            elif evt_name in _STAKE_KEYS:
                stake_events[evt_name] += count
            elif evt_name in _POOL_KEYS:
                pool_events[evt_name] += count
            elif evt_name in _CONTRACT_KEYS:
                contract_events[evt_name] += count
            elif evt_name in _CHAIN_KEYS:
                chain_events[evt_name] += count
            else:
                other_events[evt_name] += count

        def _fmt_event_group(counter: Counter, limit: int = 8) -> str:
            lines = [f"`{e}` ×{n}" for e, n in counter.most_common(limit)]
            if len(counter) > limit:
                lines.append(f"…+{len(counter) - limit} more")
            return "\n".join(lines)

        if econ_events:
            _e2.field(f"💰 Economy ({sum(econ_events.values())} events)", _fmt_event_group(econ_events), True)
        if trade_events:
            _e2.field(f"📈 Trading ({sum(trade_events.values())} events)", _fmt_event_group(trade_events), True)
        if stake_events:
            _e2.field(f"🔒 Staking ({sum(stake_events.values())} events)", _fmt_event_group(stake_events), True)
        if pool_events:
            _e2.field(f"🌊 Pools ({sum(pool_events.values())} events)", _fmt_event_group(pool_events), True)
        if contract_events:
            _e2.field(f"📜 Contracts ({sum(contract_events.values())} events)", _fmt_event_group(contract_events), True)

        # Blockchain structured data (from VALBLOCK/CHAIN/MINING/MEMPOOL categories)
        if valblock:
            vb_lines = []
            for net, d in sorted(valblock.items()):
                vb_lines.append(
                    f"**{net}**  -  {d['blocks']} block{'s' if d['blocks'] != 1 else ''} · "
                    f"✅ {d['confirmed']} / ❌ {d['rejected']}"
                )
            _e2.field("⛓ Validator Blocks", "\n".join(vb_lines), False)

        if chain_blocks:
            cb_lines = []
            for net, d in sorted(chain_blocks.items()):
                cb_lines.append(f"**{net}**  -  {d['blocks']} block{'s' if d['blocks'] != 1 else ''} · {d['txns']} txns")
            _e2.field("📦 Chain Bundles", "\n".join(cb_lines), True)

        if mempool_counts:
            mp_lines = [f"**{net}** ×{n}" for net, n in sorted(mempool_counts.items())]
            _e2.field(f"🕐 Mempool ({sum(mempool_counts.values())} total)", "\n".join(mp_lines), True)

        if mining_blocks:
            _e2.field(
                "⛏ SUN Mining",
                f"{mining_blocks} block{'s' if mining_blocks != 1 else ''} · {mining_reward:.2f} SUN total",
                True,
            )

        # Anything that didn't match a known category
        if other_events:
            _e2.field(f"🔹 Other Events ({sum(other_events.values())})", _fmt_event_group(other_events, limit=10), False)

        await ctx.send(embed=_e2.build())

        # ── Attach raw file ───────────────────────────────────────────────
        MAX_BYTES = 24 * 1024 * 1024
        raw = LOG_PATH.read_bytes()
        if len(raw) > MAX_BYTES:
            raw = raw[-MAX_BYTES:]
            fname = "bot_run_tail.log"
        else:
            fname = "bot_run.log"
        await ctx.send(file=discord.File(io.BytesIO(raw), filename=fname))

    # ── V3 Pillar 9: LP restoration ──────────────────────────────────────────

    @admin.command(name="lp_audit")
    @_require_manage_guild()
    async def admin_lp_audit(self, ctx: DiscoContext) -> None:
        """Preview the V3 LP-restoration plan without running it.

        Pre-V3 the wealth tax counted LP positions in the OWED amount
        even though LP shares themselves were never directly drained.
        That asymmetry effectively taxed LP holders harder against
        their non-LP surfaces. V3 makes LP a permanent tax-exempt
        asset class and ships a one-shot refund pass to apologise.
        This command dry-runs the pass and shows the planned diff.
        """
        from services import lp_restore
        diff = await lp_restore.audit_diff(ctx.db, ctx.guild_id)
        if diff["already_run"]:
            await ctx.reply_error(
                "LP restoration has already run for this guild. "
                f"It refunded ${diff['total_refund_usd']:,.2f} across the "
                f"affected players. Re-running is a no-op."
            )
            return
        if not diff["rows"]:
            await ctx.reply_success(
                "No historical tax events involve LP. Nothing to refund.",
                title="LP Restoration Audit",
            )
            return
        lines = [
            f"**Plan:** refund {len(diff['rows'])} players "
            f"a total of ${diff['total_refund_usd']:,.2f} "
            f"({diff['coverage_pct'] * 100:.1f}% covered by current budget "
            f"of ${diff['budget_usd']:,.2f}).",
            "",
            "**Top 10 by refund:**",
        ]
        for r in diff["rows"][:10]:
            lines.append(
                f"<@{r['user_id']}> -- ${r['refund_usd']:,.2f} "
                f"({r['lp_share'] * 100:.0f}% LP share, "
                f"avg NW ${r['avg_nw_usd']:,.0f})"
            )
        lines.append("")
        lines.append(f"Run `{ctx.prefix or '.'}admin lp_restore` to commit.")
        embed = card(
            "LP Restoration Audit",
            description="\n".join(lines),
            color=C_INFO,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── Manual block bundle ───────────────────────────────────────────────────

    @admin.command(name="bundle")
    @_require_manage_guild()
    async def admin_bundle(self, ctx: DiscoContext) -> None:
        """Force-seal a PoS chain block for all networks right now (ARC, DSC, etc.).
        Normally blocks auto-seal every 30 minutes when there are pending transactions.
        This command triggers the bundle immediately  -  useful for testing or forcing a
        mid-cycle seal. Only creates blocks for networks that have pending transactions.
        Note: SUN and MTA are PoW networks and are NOT affected by this command.
        Use ,admin forcemining to manually trigger a PoW mining tick instead."""
        chain_cog = self.bot.get_cog("ChainGroup") or self.bot.get_cog("Chain")
        if not chain_cog:
            await ctx.reply_error("Chain cog is not loaded.")
            return

        await ctx.reply(
            embed=card(description="⏳ Bundling blocks for all networks…", color=C_NAVY).build(),
            mention_author=False,
        )
        await chain_cog._bundle_block(ctx.guild)
        await ctx.send(
            embed=card(
                description="✅ Bundle complete  -  use `.chain block arc` / `.chain block sol` etc. to see new blocks.",
                color=C_SUCCESS,
            ).build()
        )

    @admin.command(name="forcemining")
    @_require_manage_guild()
    async def admin_forcemining(self, ctx: DiscoContext, symbol: str = "") -> None:
        """Force a PoW mining tick for SUN, MTA, or all PoW networks.
        Usage: ,admin forcemining [SUN|MTA]  (omit symbol to run all)"""
        from core.config import Config
        chain_cog = self.bot.get_cog("ChainGroup") or self.bot.get_cog("Chain")
        if not chain_cog:
            await ctx.reply_error("Chain cog is not loaded.")
            return

        targets: dict = {}
        if symbol:
            sym = symbol.upper()
            if sym not in Config.POW_NETWORKS:
                valid = ", ".join(Config.POW_NETWORKS)
                await ctx.reply_error(f"Unknown PoW network `{sym}`. Valid: {valid}")
                return
            targets = {sym: Config.POW_NETWORKS[sym]}
        else:
            targets = dict(Config.POW_NETWORKS)

        await ctx.reply(
            embed=card(
                description=f"Running mining tick for: **{', '.join(targets)}**...",
                color=C_NAVY,
            ).build(),
            mention_author=False,
        )

        lines = []
        for sym, cfg in targets.items():
            net_key = sym.lower()
            try:
                # Pre-tick diagnostics
                all_rigs = await ctx.db.get_all_guild_chain_rigs(ctx.guild_id, sym)
                from cogs.chain_group import _RIGS
                total_hr = sum(
                    _RIGS[r["rig_id"]]["hashrate"] * r["quantity"]
                    for r in all_rigs if r["rig_id"] in _RIGS
                )
                pending_blocks = await ctx.db.get_oldest_pending_chain_blocks(ctx.guild_id, limit=99, network=net_key)
                latest = await ctx.db.get_latest_chain_block(ctx.guild_id, network=net_key)
                height_before = latest["block_num"] if latest else 0

                await chain_cog._process_pow_guild(ctx.guild, sym, cfg)

                latest_after = await ctx.db.get_latest_chain_block(ctx.guild_id, network=net_key)
                height_after = latest_after["block_num"] if latest_after else 0
                blocks_mined = height_after - height_before

                status = "mined" if blocks_mined > 0 else ("no-miners" if total_hr == 0 else "no-block-this-tick")
                lines.append(
                    f"**{sym}** [{status}]\n"
                    f"  rigs on chain: `{len(all_rigs)}` | hashrate: `{total_hr:,.0f} MH/s`\n"
                    f"  pending bundle blocks: `{len(pending_blocks)}` | height before: `{height_before}` -> after: `{height_after}`"
                )
            except Exception as exc:
                lines.append(f"**{sym}**: error - {exc}")

        await ctx.send(
            embed=card(
                "Force Mining Diagnostics",
                description="\n\n".join(lines),
                color=C_SUCCESS,
            ).build()
        )

    # ── Block status overview ────────────────────────────────────────────────

    @admin.command(name="blockstatus")
    @_require_manage_guild()
    async def admin_blockstatus(self, ctx: DiscoContext) -> None:
        """Show a summary of the latest chain block for every network in one embed.
        Displays block number, status (PoS confirmed / pending), transaction count,
        block hash, and timestamp for all networks simultaneously.
        Use .chain block <network> for full transaction details on a specific block."""
        # Dynamically gather all known networks from token config and PoW networks
        net_set: set[str] = set()
        for sym, tcfg in Config.TOKENS.items():
            net = tcfg.get("network", "")
            short = {"Sun Network": "sun", "Moneta Chain": "mta", "Arcadia Network": "arc",
                     "Discoin Network": "dsc", "Solana Network": "sol", "BNB Network": "bnb",
                     "Avalanche Network": "avax", "Polygon Network": "pol", "Cosmos Network": "atom",
                     "Sui Network": "sui", "Aptos Network": "apt", "Near Network": "near"}.get(net, "")
            if short:
                net_set.add(short)
        for pow_net in getattr(Config, "POW_NETWORKS", {}):
            net_set.add(pow_net.lower())
        # Also include guild-specific custom tokens
        custom = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        for sym, tcfg in custom.items():
            net = tcfg.get("network", "")
            if net:
                short_key = net.lower().split()[0]  # "sun", "moneta", "arcadia" etc
                abbrevs = {"sun": "sun", "moneta": "mta", "arcadia": "arc", "discoin": "dsc",
                           "solana": "sol", "bnb": "bnb", "avalanche": "avax", "polygon": "pol",
                           "cosmos": "atom", "sui": "sui", "aptos": "apt", "near": "near"}
                if short_key in abbrevs:
                    net_set.add(abbrevs[short_key])
        networks = sorted(net_set) or ["arc", "sol", "bnb", "sun", "mta"]

        _b = card("⛓ Chain Block Status", color=C_NAVY)

        for net in networks:
            latest = await ctx.db.get_latest_chain_block(ctx.guild_id, network=net)
            if not latest:
                _b.field(f"**{net.upper()}**", "No blocks yet", True)
                continue

            ts_str = fmt_ts(latest["ts"], "%m/%d %H:%M UTC")
            status = latest.get("status", "pending")
            if status == "mined":
                miner = latest.get("miner_id")
                if miner:
                    status_icon = f"✅ Mined by {mention(miner, ctx.guild, self.bot)}"
                elif net not in ("sun", "mta"):
                    status_icon = "✅ PoS Confirmed"
                else:
                    status_icon = "✅ Mined"
            else:
                status_icon = "⏳ Pending"

            _b.field(
                f"**{net.upper()}**  -  Block #{latest['block_num']:,}",
                (
                    f"{status_icon}\n"
                    f"Txns: {latest['tx_count']} · {ts_str}\n"
                    f"`{latest['block_hash'][:20]}…`"
                ),
                True,
            )

        await ctx.reply(embed=_b.footer("Use .chain block <net> for full details  •  .admin bundle to force-bundle now").build(), mention_author=False)

    # ── Pool management ───────────────────────────────────────────────────────

    @admin.command(name="removepool")
    @_require_manage_guild()
    async def admin_removepool(self, ctx: DiscoContext, token_a: str, token_b: str) -> None:
        """Delete a liquidity pool."""
        pool_id, a, b = ctx.db.make_pool_id(token_a, token_b)
        pool = await ctx.db.delete_pool(pool_id, ctx.guild_id)
        if not pool:
            await ctx.reply_error(f"No pool found for **{a}/{b}**.")
            return
        await ctx.reply_success(
            f"Pool **{a}/{b}** deleted. Reserves have been removed.",
            title="✅ Pool Removed",
        )

    @admin.command(name="createpool", aliases=["addpool", "newpool"])
    @_require_manage_guild()
    async def admin_createpool(
        self, ctx: DiscoContext,
        token_a: str, token_b: str,
        seed_usd: float = 10_000.0,
    ) -> None:
        """Create an AMM pool for any token pair, auto-seeded with $seed_usd per side.

        Mirror of ``,admin removepool``. Pulls each side's price from the
        oracle and seeds equal USD value on both sides so the implied price
        matches the market. Defaults to **$10,000** per side; pass a third
        argument to override (e.g. ``,admin createpool ARC LINK 5000``).

        Skips silently if the pool already exists -- use ``removepool``
        first if you want to reset reserves.
        """
        if seed_usd <= 0 or not math.isfinite(seed_usd):
            await ctx.reply_error("Seed USD must be a positive finite number.")
            return
        sym_a = token_a.upper()
        sym_b = token_b.upper()
        if sym_a == sym_b:
            await ctx.reply_error("Cannot create a pool with two identical tokens.")
            return

        pool_id, ca, cb = ctx.db.make_pool_id(sym_a, sym_b)
        existing = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if existing:
            await ctx.reply_error(
                f"Pool **{ca}/{cb}** already exists. "
                f"Use `{ctx.prefix}admin removepool {ca} {cb}` first if you "
                f"want to reset its reserves."
            )
            return

        # Oracle prices for both sides; fall back to start_price from
        # Config.TOKENS, then to $1.00 if neither is available so we can
        # still seed an exotic pair without erroring out.
        async def _price(sym: str) -> float:
            row = await ctx.db.get_price(sym, ctx.guild_id)
            if row and float(row.get("price") or 0.0) > 0:
                return float(row["price"])
            cfg = Config.TOKENS.get(sym, {})
            sp = float(cfg.get("start_price") or 0.0)
            return sp if sp > 0 else 1.0

        price_a = await _price(ca)
        price_b = await _price(cb)
        amt_a = seed_usd / price_a
        amt_b = seed_usd / price_b
        try:
            await ctx.db.create_pool(pool_id, ctx.guild_id, ca, cb, amt_a, amt_b)
        except Exception as exc:
            log.exception("admin createpool failed")
            await ctx.reply_error(f"Pool creation failed: `{exc}`")
            return

        await ctx.reply_success(
            f"Pool **{ca}/{cb}** created.\n"
            f"Seeded **${seed_usd:,.0f}** per side at oracle prices "
            f"(`{amt_a:,.4f} {ca}` + `{amt_b:,.4f} {cb}`).",
            title="✅ Pool Created",
        )

    @admin.command(name="rebalancepool")
    @_require_manage_guild()
    async def admin_rebalancepool(
        self, ctx: DiscoContext, token_a: str, token_b: str, new_price: float
    ) -> None:
        """Rebalance a pool's reserves to set a new price (preserves k)."""
        if new_price <= 0:
            await ctx.reply_error("Price must be positive.")
            return
        pool_id, a, b = ctx.db.make_pool_id(token_a, token_b)
        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not pool:
            await ctx.reply_error(f"No pool found for **{a}/{b}**.")
            return
        await ctx.db.rebalance_pool(pool_id, ctx.guild_id, new_price)
        await ctx.reply_success(
            f"Pool **{a}/{b}** rebalanced. New implied price: **{new_price:,.4f}**.",
            title="✅ Pool Rebalanced",
        )

    @admin.command(name="addlp")
    @_require_manage_guild()
    async def admin_addlp(
        self, ctx: DiscoContext,
        token_a: str, token_b: str,
        amount_a: float, amount_b: float,
    ) -> None:
        """Inject raw liquidity into a pool unrestricted (no whale cap, no balance check).

        Usage: .admin addlp <token_a> <token_b> <amount_a> <amount_b>

        Example: .admin addlp MTA USDC 5 250000

        Adds the reserves directly to the pool and bumps total_lp by sqrt(a*b).
        Does NOT credit any user's LP position - this is platform-owned liquidity.
        """
        if not (math.isfinite(amount_a) and math.isfinite(amount_b)):
            await ctx.reply_error("Amounts must be finite numbers (no nan/inf).")
            return
        if amount_a <= 0 or amount_b <= 0:
            await ctx.reply_error("Both amounts must be positive.")
            return
        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)
        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not pool:
            await ctx.reply_error(f"No pool found for **{ca}/{cb}**.")
            return

        # Map admin-provided amounts to canonical (a, b) order
        if ca == token_a.upper():
            da, db = amount_a, amount_b
        else:
            da, db = amount_b, amount_a

        ra_h = pool.h("reserve_a")
        rb_h = pool.h("reserve_b")
        new_a = ra_h + da
        new_b = rb_h + db
        new_lp = math.sqrt(new_a * new_b)

        await ctx.db.execute(
            "UPDATE pools SET reserve_a=$1, reserve_b=$2, total_lp=$3 "
            "WHERE pool_id=$4 AND guild_id=$5",
            to_raw(new_a), to_raw(new_b), to_raw(new_lp), pool_id, ctx.guild_id,
        )

        log.info(
            "[admin_addlp] guild=%d pool=%s +%s %s +%s %s by uid=%d",
            ctx.guild_id, pool_id, da, ca, db, cb, ctx.author.id,
        )

        embed = (
            card("✅ Liquidity Added", color=C_SUCCESS)
            .field("Pool", f"`{ca}/{cb}`", True)
            .field("Added", f"+{da:,.6f} {ca}\n+{db:,.6f} {cb}", True)
            .field("New Reserves", f"{new_a:,.4f} {ca}\n{new_b:,.4f} {cb}", True)
            .footer("Platform-owned liquidity (no LP position credited).")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin.command(name="removelp")
    @_require_manage_guild()
    async def admin_removelp(
        self, ctx: DiscoContext,
        token_a: str, token_b: str,
        amount_a: float, amount_b: float,
    ) -> None:
        """Drain liquidity from a pool unrestricted (no whale cap, no LP balance).

        Usage: .admin removelp <token_a> <token_b> <amount_a> <amount_b>

        Reserves are clamped to >= 0 - withdrawals beyond available are capped.
        Does NOT credit anyone's wallet - the tokens are burned. Use this only
        to drain dead/orphaned pools or to force-correct a stuck reserve.
        """
        if not (math.isfinite(amount_a) and math.isfinite(amount_b)):
            await ctx.reply_error("Amounts must be finite numbers (no nan/inf).")
            return
        if amount_a < 0 or amount_b < 0:
            await ctx.reply_error("Amounts must be non-negative.")
            return
        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)
        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not pool:
            await ctx.reply_error(f"No pool found for **{ca}/{cb}**.")
            return

        # Map admin-provided amounts to canonical (a, b) order
        if ca == token_a.upper():
            da, db = amount_a, amount_b
        else:
            da, db = amount_b, amount_a

        ra_h = pool.h("reserve_a")
        rb_h = pool.h("reserve_b")
        new_a = max(0.0, ra_h - da)
        new_b = max(0.0, rb_h - db)
        new_lp = math.sqrt(new_a * new_b) if (new_a > 0 and new_b > 0) else 0.0

        await ctx.db.execute(
            "UPDATE pools SET reserve_a=$1, reserve_b=$2, total_lp=$3 "
            "WHERE pool_id=$4 AND guild_id=$5",
            to_raw(new_a), to_raw(new_b), to_raw(new_lp), pool_id, ctx.guild_id,
        )

        log.info(
            "[admin_removelp] guild=%d pool=%s -%s %s -%s %s by uid=%d",
            ctx.guild_id, pool_id, da, ca, db, cb, ctx.author.id,
        )

        embed = (
            card("✅ Liquidity Removed", color=C_WARNING)
            .field("Pool", f"`{ca}/{cb}`", True)
            .field("Removed", f"-{da:,.6f} {ca}\n-{db:,.6f} {cb}", True)
            .field("New Reserves", f"{new_a:,.4f} {ca}\n{new_b:,.4f} {cb}", True)
            .footer("Tokens were burned (no wallet credited).")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── Stake management ──────────────────────────────────────────────────────

    @admin.command(name="clearstakes")
    @_require_manage_guild()
    async def admin_clearstakes(self, ctx: DiscoContext, target: str) -> None:
        """Clear all stakes for @user or for a VALIDATOR_ID. Refunds holdings."""
        gid = ctx.guild_id

        # Try to parse as a member mention first
        member: discord.Member | None = None
        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.BadArgument:
            pass

        if member:
            rows = await ctx.db.clear_user_stakes(member.id, gid)
            if not rows:
                await ctx.reply_error(f"{member.mention} has no active stakes.")
                return
            for r in rows:
                await ctx.db.update_holding(member.id, gid, r["symbol"], r["amount"])
            total = sum(r["amount"] for r in rows)
            await ctx.reply_success(
                f"Cleared **{len(rows)}** stake(s) for {member.mention}. "
                f"Refunded **{total:,.4f}** tokens to holdings.",
                title="✅ Stakes Cleared",
            )
        else:
            vid = target.upper()
            rows = await ctx.db.clear_validator_stakes(vid, gid)
            if not rows:
                await ctx.reply_error(f"No stakes found on validator **{vid}**.")
                return
            for r in rows:
                await ctx.db.update_holding(r["user_id"], gid, r["symbol"], r["amount"])
            await ctx.reply_success(
                f"Cleared **{len(rows)}** stake(s) on **{vid}**. All holders have been refunded.",
                title="✅ Validator Stakes Cleared",
            )

    @admin.command(name="clearvalidator")
    @_require_manage_guild()
    async def admin_clearvalidator(self, ctx: DiscoContext, validator_id: str) -> None:
        """Clear all stakes on a validator, refund holders, then delete the validator."""
        gid = ctx.guild_id
        vid = validator_id.upper()
        v = await ctx.db.get_validator(vid, gid)
        if not v:
            await ctx.reply_error(f"Validator **{vid}** not found.")
            return
        rows = await ctx.db.clear_validator_stakes(vid, gid)
        for r in rows:
            await ctx.db.update_holding(r["user_id"], gid, r["symbol"], r["amount"])
        await ctx.db.delete_validator(vid, gid)
        await ctx.reply_success(
            f"Validator **{vid}** deleted. {len(rows)} stake(s) refunded.",
            title="✅ Validator Removed",
        )

    # ── Validator management ──────────────────────────────────────────────────

    @admin.command(name="addvalidator")
    @_require_manage_guild()
    async def admin_addvalidator(
        self, ctx: DiscoContext,
        validator_id: str, name: str, network: str,
        uptime: float, reward: float, slash: float,
        emoji: str = "🌐",
    ) -> None:
        """Add a new validator to this guild."""
        vid = validator_id.upper()
        if len(vid) > 20:
            await ctx.reply_error("Validator ID must be 20 characters or fewer.")
            return
        if len(name) > 50:
            await ctx.reply_error("Validator name must be 50 characters or fewer.")
            return
        if len(network) > 64:
            await ctx.reply_error("Network name must be 64 characters or fewer.")
            return
        # uptime/reward/slash entered as percentages (e.g. 95 → 0.95)
        await ctx.db.create_validator(
            vid, ctx.guild_id, name, network,
            uptime / 100, reward / 100, slash / 100, emoji,
        )
        await ctx.reply_success(
            f"Validator **{vid}** ({name}) added to **{network}**.\n"
            f"Uptime: {uptime}% | Reward: {reward}% | Slash: {slash}%",
            title="✅ Validator Added",
        )

    @admin.command(name="removevalidator")
    @_require_manage_guild()
    async def admin_removevalidator(self, ctx: DiscoContext, validator_id: str) -> None:
        """Remove a validator (does NOT refund stakes  -  use clearvalidator for that)."""
        vid = validator_id.upper()
        v = await ctx.db.get_validator(vid, ctx.guild_id)
        if not v:
            await ctx.reply_error(f"Validator **{vid}** not found.")
            return
        await ctx.db.delete_validator(vid, ctx.guild_id)
        await ctx.reply_success(f"Validator **{vid}** removed.", title="✅ Validator Removed")

    @admin.command(name="updatevalidator")
    @_require_manage_guild()
    async def admin_updatevalidator(
        self, ctx: DiscoContext, validator_id: str, field: str, *, value: str
    ) -> None:
        """Update a field on a validator. Rate fields (uptime_rate/reward_rate/slash_rate) accept %."""
        vid = validator_id.upper()
        v = await ctx.db.get_validator(vid, ctx.guild_id)
        if not v:
            await ctx.reply_error(f"Validator **{vid}** not found.")
            return
        rate_fields = {"uptime_rate", "reward_rate", "slash_rate"}
        parsed_value: float | str
        if field in rate_fields:
            try:
                parsed_value = float(value) / 100
            except ValueError:
                await ctx.reply_error(f"Value for `{field}` must be a number (percent).")
                return
        else:
            parsed_value = value
        try:
            await ctx.db.update_validator_field(vid, ctx.guild_id, field, parsed_value)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        await ctx.reply_success(
            f"Validator **{vid}**: `{field}` → `{value}`",
            title="✅ Validator Updated",
        )

    # ── Network management ────────────────────────────────────────────────────

    @admin.command(name="addnetwork")
    @_require_manage_guild()
    async def admin_addnetwork(
        self, ctx: DiscoContext, name: str, stake_token: str, emoji: str = "🌐"
    ) -> None:
        """Add a custom PoS network for this guild."""
        if len(name) > 64:
            await ctx.reply_error("Network name must be 64 characters or fewer.")
            return
        if len(stake_token) > 10:
            await ctx.reply_error("Stake token symbol must be 10 characters or fewer.")
            return
        token = stake_token.upper()
        await ctx.db.add_guild_network(ctx.guild_id, name, token, emoji)
        await ctx.reply_success(
            f"Network **{name}** added. Stake token: **{token}** {emoji}",
            title="✅ Network Added",
        )

    @admin.command(name="removenetwork")
    @_require_manage_guild()
    async def admin_removenetwork(self, ctx: DiscoContext, *, name: str) -> None:
        """Remove a custom PoS network."""
        await ctx.db.remove_guild_network(ctx.guild_id, name)
        await ctx.reply_success(f"Network **{name}** removed.", title="✅ Network Removed")

    @admin.command(name="listnetworks")
    @_require_manage_guild()
    async def admin_listnetworks(self, ctx: DiscoContext) -> None:
        """List all networks (built-in + custom)."""
        _b = card("🌐 Networks", color=C_INFO)
        # Built-in
        lines_builtin = [f"{t}  -  stake **{s}**" for t, s in Config.NETWORK_STAKE_TOKEN.items()]
        _b.field("Built-in", "\n".join(lines_builtin) or " - ", False)
        # Custom
        custom = await ctx.db.get_guild_networks(ctx.guild_id)
        if custom:
            lines_custom = [f"{n['emoji']} **{n['network_name']}**  -  stake **{n['stake_token']}**" for n in custom]
            _b.field("Custom", "\n".join(lines_custom), False)
        await ctx.reply(embed=_b.build(), mention_author=False)

    # ── Token management ──────────────────────────────────────────────────────

    @admin.command(name="addtoken")
    @_require_manage_guild()
    async def admin_addtoken(self, ctx: DiscoContext, *, raw: str = "") -> None:
        """Add a custom token using key=value syntax.
        Usage: .admin addtoken symbol=XYZ name="My Token" emoji=🌟 network="Arcadia Network" type=PoS price=1.0 vol=0.05
        Optional: max_supply=0 initial_supply=0 burn_rate=0 fee=0
        Multiline input supported  -  each key=value on its own line works too."""
        import re as _re
        if not raw.strip():
            usage = (
                "**Usage:**\n```\n.admin addtoken symbol=XYZ name=\"My Token\" emoji=🌟 "
                "network=\"Arcadia Network\" type=PoS price=1.00 vol=0.05\n```\n"
                "Keys: `symbol` `name` `emoji` `network` `type` `price` `vol` "
                "`max_supply` `burn_rate` `fee`\n"
                "Set `network=none` for orphan tokens."
            )
            await ctx.reply(usage, mention_author=False)
            return

        kv: dict[str, str] = {}
        flat = raw.replace("\n", " ")
        for m in _re.finditer(r'(\w+)=(?:"([^"]*)"|([\S]+))', flat):
            key = m.group(1).lower()
            val = m.group(2) if m.group(2) is not None else m.group(3)
            kv[key] = val

        sym       = kv.get("symbol", "").upper()
        name      = kv.get("name", "")
        emoji     = kv.get("emoji", "●")
        net_raw   = kv.get("network", "none")
        network   = None if net_raw.lower() in ("none", "") else net_raw
        consensus = kv.get("type", kv.get("consensus", "PoS"))
        # Token type field: stablecoin, utility, governance, security,
        # payment, asset-backed, wrapped, non-fungible, etc.
        _VALID_TOKEN_TYPES = {
            "stablecoin", "utility", "governance", "security", "payment",
            "asset-backed", "wrapped", "non-fungible", "meme", "defi", "pos", "pow",
        }
        token_type = kv.get("token_type", kv.get("ttype", "utility")).lower()
        if token_type not in _VALID_TOKEN_TYPES:
            token_type = "utility"
        try:
            start_price      = float(kv.get("price", "0"))
            daily_vol        = float(kv.get("vol",   "0.05"))
            max_supply       = float(kv.get("max_supply", "0"))
            initial_supply   = float(kv.get("initial_supply", kv.get("supply", "0")))
            burn_rate        = float(kv.get("burn_rate",  "0"))
            fee_rate         = float(kv.get("fee", "0"))
        except ValueError as exc:
            await ctx.reply_error(f"Number parsing error: {exc}")
            return

        net = network  # alias for the existing logic below

        if not sym:
            await ctx.reply_error("Missing required key: `symbol`")
            return
        if not name:
            await ctx.reply_error("Missing required key: `name`")
            return
        if len(sym) > 10:
            await ctx.reply_error("Symbol must be 10 characters or fewer.")
            return
        if sym == "ALL":
            await ctx.reply_error("Symbol cannot be `ALL`  -  it is a reserved keyword.")
            return
        if sym.isdigit():
            await ctx.reply_error("Symbol cannot be all numbers.")
            return
        if len(name) > 50:
            await ctx.reply_error("Token name must be 50 characters or fewer.")
            return
        if len(emoji) > 10:
            await ctx.reply_error("Emoji must be 10 characters or fewer.")
            return
        if net and len(net) > 64:
            await ctx.reply_error("Network name must be 64 characters or fewer.")
            return

        if not math.isfinite(start_price) or start_price <= 0:
            await ctx.reply_error("start_price must be a positive finite number.")
            return
        if not math.isfinite(daily_vol) or daily_vol < 0:
            await ctx.reply_error("daily_vol must be a non-negative finite number.")
            return
        if not math.isfinite(max_supply) or max_supply < 0:
            await ctx.reply_error("max_supply must be a non-negative finite number.")
            return
        if not math.isfinite(initial_supply) or initial_supply < 0:
            await ctx.reply_error("initial_supply must be a non-negative finite number.")
            return

        await ctx.db.add_guild_token(
            ctx.guild_id, sym, name, emoji, consensus, net, start_price, daily_vol
        )
        # Store token_type
        await ctx.db.execute(
            "UPDATE guild_tokens SET token_type=$1 WHERE guild_id=$2 AND symbol=$3",
            token_type, ctx.guild_id, sym,
        )
        # Seed its price row (INSERT ... ON CONFLICT DO NOTHING  -  won't overwrite existing price)
        await ctx.db.execute(
            "INSERT INTO crypto_prices (symbol, guild_id, price, open_price, day_high, day_low) VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT DO NOTHING",
            sym, ctx.guild_id, start_price, start_price, start_price, start_price,
        )
        # Set initial circulating supply if provided
        if initial_supply > 0:
            await ctx.db.execute(
                "UPDATE guild_tokens SET circulating_supply=$1 WHERE guild_id=$2 AND symbol=$3",
                initial_supply, ctx.guild_id, sym,
            )

        # Auto-register token in network's accepted wallet tokens
        if net:
            await ctx.db.add_token_to_network_wallet(ctx.guild_id, net, sym)

        # Store token contract params (max_supply, burn_rate, fee)
        needs_contract = max_supply > 0 or burn_rate > 0 or fee_rate > 0
        if needs_contract:
            existing_contract = await ctx.db.get_token_contract(ctx.guild_id, sym)
            if max_supply > 0:
                existing_contract["max_supply"] = max_supply
            if burn_rate > 0:
                existing_contract["burn_rate"] = burn_rate
            if fee_rate > 0:
                existing_contract["transfer_fee"] = fee_rate
            await ctx.db.set_token_contract(ctx.guild_id, sym, existing_contract)

        # Auto-seed TOKEN/STABLECOIN pool using $500k seed formula
        pool_line = ""
        if net:
            stablecoin = Config.NETWORK_STABLECOIN.get(net)
            if stablecoin:
                pool_id, ca, cb = ctx.db.make_pool_id(sym, stablecoin)
                existing_pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
                if not existing_pool:
                    seed_usd = Config.POOL_SEED_STABLECOIN
                    token_reserve = seed_usd / start_price
                    stable_reserve = seed_usd
                    ra = token_reserve if ca == sym else stable_reserve
                    rb = stable_reserve if ca == sym else token_reserve
                    await ctx.db.create_pool(pool_id, ctx.guild_id, ca, cb, ra, rb)
                    pool_line = f"\n🌊 Pool **{ca}/{cb}** seeded: **{ra:,.4f} {ca}** ↔ **{rb:,.4f} {cb}**"

        details = (
            f"Network: {net or 'None'} | Type: {token_type} | Consensus: {consensus}\n"
            f"Price: ${start_price:,.4f} | Vol: {daily_vol*100:.1f}%/day"
            + (f" | Max supply: {max_supply:,.0f}" if max_supply > 0 else "")
            + (f" | Circ. supply: {initial_supply:,.0f}" if initial_supply > 0 else "")
            + pool_line
        )
        await ctx.reply_success(
            f"Token **{emoji} {sym}** ({name}) added.\n{details}",
            title="✅ Token Added",
        )

    @admin.command(name="removetoken")
    @_require_manage_guild()
    async def admin_removetoken(self, ctx: DiscoContext, symbol: str) -> None:
        """Remove a custom token and wipe its price data."""
        sym = symbol.upper()
        await ctx.db.remove_guild_token(ctx.guild_id, sym)
        await ctx.db.execute(
            "DELETE FROM crypto_prices WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, sym,
        )
        await ctx.reply_success(f"Token **{sym}** removed and price data cleared.", title="✅ Token Removed")

    @admin.command(name="listtokens")
    @_require_manage_guild()
    async def admin_listtokens(self, ctx: DiscoContext) -> None:
        """List all tokens grouped by network, paginated.
        Custom tokens on built-in networks are shown under their network with [CUSTOM] tag."""

        _BUILTIN_NETS = set(Config.NETWORK_STAKE_TOKEN.keys()) | {"Sun Network"}

        # Group built-in tokens by network
        by_net: dict[str, list[tuple[str, dict, bool]]] = {}  # (sym, data, is_custom)
        for sym, t in Config.TOKENS.items():
            net = t.get("network") or "Orphan"
            by_net.setdefault(net, []).append((sym, t, False))

        # Overlay custom tokens  -  if they have a known network, put them there
        custom_rows = await ctx.db.get_guild_tokens(ctx.guild_id)
        orphan_customs: list[tuple[str, dict, bool]] = []
        for t in custom_rows:
            data = {
                "name": t["name"], "emoji": t["emoji"],
                "start_price": t["start_price"], "consensus": t["consensus"],
                "network": t["network"], "token_type": t.get("token_type", "utility"),
            }
            net = t.get("network") or ""
            if net:
                by_net.setdefault(net, []).append((t["symbol"], data, True))
            else:
                orphan_customs.append((t["symbol"], data, True))

        pages: list[discord.Embed] = []
        _b = card("📋 All Tokens by Network", color=C_INFO)
        field_count = 0

        for net in sorted(by_net.keys()):
            tokens = by_net[net]
            lines = []
            for sym, t, is_custom in tokens:
                tag = " `[CUSTOM]`" if is_custom else ""
                ttype = t.get("token_type", "")
                ttype_str = f" *({ttype})*" if ttype and ttype != "utility" else ""
                lines.append(
                    f"{t.get('emoji','●')} **{sym}**{tag} ({t['name']}){ttype_str}  `${t['start_price']:,.4f}`"
                )
            for i in range(0, len(lines), 10):
                chunk = lines[i:i+10]
                label = net + (" *(cont.)*" if i > 0 else "")
                _b.field(label, "\n".join(chunk), False)
                field_count += 1
                if field_count >= 5:
                    pages.append(_b.build())
                    _b = card("📋 All Tokens (cont.)", color=C_INFO)
                    field_count = 0

        if _b._embed.fields:
            pages.append(_b.build())

        # Orphan custom tokens (no network)
        if orphan_customs:
            _cb = card("📋 Custom / No Network", color=C_PURPLE)
            clines = [
                f"{t['emoji']} **{sym}** `[CUSTOM]` ({t['name']})  `${t['start_price']:,.4f}`"
                for sym, t, _ in orphan_customs
            ]
            for i in range(0, len(clines), 10):
                _cb.field(
                    "Custom Tokens" + (" *(cont.)*" if i > 0 else ""),
                    "\n".join(clines[i:i+10]),
                    False,
                )
            pages.append(_cb.build())

        if not pages:
            await ctx.reply_error("No tokens found.")
            return
        await send_paginated(ctx, pages)

    # ── Whitelabeling ─────────────────────────────────────────────────────────

    @admin.command(name="setprefix")
    @_require_manage_guild()
    async def admin_setprefix(self, ctx: DiscoContext, prefix: str) -> None:
        """Set a custom command prefix for this server."""
        if len(prefix) > 5:
            await ctx.reply_error("Prefix must be 5 characters or fewer.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "prefix", prefix)
        await ctx.reply_success(f"Prefix set to **{prefix}**", title="✅ Prefix Updated")

    @admin.command(name="setcolor")
    @_require_manage_guild()
    async def admin_setcolor(self, ctx: DiscoContext, hex_color: str) -> None:
        """Set custom embed color (e.g. #ff6600)."""
        hex_color = hex_color.lstrip("#")
        try:
            color_int = int(hex_color, 16)
        except ValueError:
            await ctx.reply_error("Invalid hex color. Example: `#ff6600`")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "embed_color", color_int)
        embed = card(
            "✅ Color Updated",
            description=f"Embed color set to **#{hex_color.upper()}**",
            color=color_int,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @admin.command(name="setname")
    @_require_manage_guild()
    async def admin_setname(self, ctx: DiscoContext, *, name: str) -> None:
        """Set a display name for this server (shown in $help)."""
        await ctx.db.update_guild_setting(ctx.guild_id, "server_name", name)
        await ctx.reply_success(f"Server name set to **{name}**", title="✅ Name Updated")

    @admin.command(name="setcurrencyname")
    @_require_manage_guild()
    async def admin_setcurrencyname(self, ctx: DiscoContext, *, name: str) -> None:
        """Rename the base currency (e.g. 'Credits' instead of 'USD')."""
        if len(name) > 20:
            await ctx.reply_error("Currency name must be 20 characters or fewer.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "currency_name", name)
        await ctx.reply_success(f"Currency name set to **{name}**", title="✅ Currency Name Updated")

    # ── Price management ──────────────────────────────────────────────────────

    @admin.command(name="setprice")
    @_require_manage_guild()
    async def admin_setprice(self, ctx: DiscoContext, symbol: str, price: float) -> None:
        """Manually set a token's market price. Usage: $admin setprice <SYM> <price>"""
        if price <= 0:
            await ctx.reply_error("Price must be positive.")
            return
        sym = symbol.upper()
        await ctx.db.execute(
            """INSERT INTO crypto_prices (symbol, guild_id, price, open_price, day_high, day_low)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT(symbol, guild_id) DO UPDATE SET price=excluded.price,
               day_high=GREATEST(crypto_prices.day_high, excluded.price), day_low=LEAST(crypto_prices.day_low, excluded.price)""",
            sym, ctx.guild_id, price, price, price, price,
        )
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="setprice",
            severity=SEVERITY_WARN,
            details=f"{sym}={price}",
        )
        await ctx.reply_success(
            f"**{sym}** price set to **${price:,.6f}**",
            title="Price Updated",
        )

    @admin.command(name="pump")
    @_require_manage_guild()
    async def admin_pump(self, ctx: DiscoContext, *args: str) -> None:
        """Drive token prices through arbitrary chart patterns over a chosen timeframe.

        Usage:
          ,admin pump <target> [pattern|pct] [magnitude%] [duration_min]
          ,admin pump patterns                  (list every pattern)
          ,admin pump active                    (list active events)
          ,admin pump auto [status|on|off|now]  (manage hourly auto-pump)
          ,admin pump <target> 0                (cancel target's event(s))

        Targets:
          MTA ARC ...        single symbol
          all everything *   all non-stablecoins (one shared pattern)
          each               chaos -- different random pattern per non-stable token
          coins              network coins (MTA SUN ARC DSC REEL RUNE BUD HRV FORGE)
          stables            stablecoins (USDC DSD FGD)
          group              group / community tokens
          earn               earn-only tokens (LURE COPPER MOON ...)
          wrapped            peg-to wrappers (MMTA MSUN)
          pow / pos          by consensus
          builtin            every Config.TOKENS symbol
          meme               meme tokens (STR)
          chain:<short>      every token on a network -- e.g. chain:arc, chain:moon
          each:<category>    chaos within a category -- e.g. each:group, each:earn

        Patterns:  linear pump moon dump crash bull bear volatile wave rugpull
                   pumpdump vshape hns double_top double_bottom cup_handle
                   bullflag bearflag chaos zigzag spike accumulate distribute
                   stairstep fakeout    (run `,admin pump patterns` for blurbs)
                   `random` picks one for you.

        Examples:
          ,admin pump ARC moon 80 30           moon-shape, +80% over 30 min
          ,admin pump MTA rugpull 50 90        +50% pump then catastrophic dump, 90 min
          ,admin pump everything bullflag 40   shared bull-flag breakout across non-stables
          ,admin pump each:group               every group token gets its own random chart
          ,admin pump chain:arc chaos 30 60    every Arcadia-Network token does a random walk
          ,admin pump STR random              degen mode -- whatever the dice say
          ,admin pump each 0                   cancel everything"""
        from cogs.trade import _admin_price_events
        from services.chart_patterns import (
            random_duration,
            random_magnitude,
            random_pattern,
            resolve_pattern,
        )

        if not args:
            await ctx.reply_error_hint(
                "Need a target. Try `,admin pump patterns` for the catalog or `,admin pump help`.",
                hint="Examples: `,admin pump ARC moon 80 30` · `,admin pump each:group` · `,admin pump everything 0`",
            )
            return

        first = args[0].strip()
        first_lower = first.lower()

        # ── Subcommands: patterns / active / auto ──────────────────────────
        if first_lower in {"patterns", "list", "help"}:
            await self._pump_show_patterns(ctx)
            return
        if first_lower in {"active", "status", "events", "running"}:
            await self._pump_show_active(ctx)
            return
        if first_lower in {"auto", "autopump", "scheduler"}:
            sub = (args[1].lower() if len(args) >= 2 else "status")
            await self._pump_auto_dispatch(ctx, sub)
            return

        # ── Resolve target → list of symbols + chaos flag ──────────────────
        try:
            target_syms, target_label, chaos_mode = await self._pump_resolve_target(
                ctx, first,
            )
        except _PumpTargetError as e:
            await ctx.reply_error(str(e))
            return

        if not target_syms:
            await ctx.reply_error(f"No tokens match target **{first}**.")
            return

        # ── Cancel path: second arg "0" cancels every event for target ────
        if len(args) >= 2 and args[1].strip() == "0":
            cancelled = [s for s in target_syms
                         if _admin_price_events.pop((ctx.guild_id, s), None)]
            if not cancelled:
                await ctx.reply_error(
                    f"No active price events for {target_label}.",
                )
                return
            for _sym in cancelled:
                try:
                    await ctx.db.delete_admin_price_event(ctx.guild_id, _sym)
                except Exception:
                    log.exception(
                        "admin pump cancel: failed to drop persisted event "
                        "gid=%s sym=%s", ctx.guild_id, _sym,
                    )
            preview = ", ".join(sorted(cancelled)[:12])
            if len(cancelled) > 12:
                preview += f", +{len(cancelled) - 12} more"
            await ctx.reply_success(
                f"Cancelled **{len(cancelled)}** event(s) on {target_label}: {preview}",
                title="Cancelled",
            )
            return

        # ── Parse pattern + magnitude + duration ──────────────────────────
        pattern_arg: str | None = args[1] if len(args) >= 2 else None
        mag_arg:     str | None = args[2] if len(args) >= 3 else None
        dur_arg:     str | None = args[3] if len(args) >= 4 else None

        # Detect numeric pattern_arg → linear, back-compat
        pattern: str | None = None
        magnitude: float | None = None
        duration: float | None = None
        is_random_pattern = False

        if pattern_arg is None:
            # No pattern specified at all -- default to a random pattern for
            # plural targets (more fun) and an error for single-symbol targets.
            if len(target_syms) == 1:
                await ctx.reply_error_hint(
                    f"Need a pattern or pct for **{target_label}**.",
                    hint="Try a pattern name (`moon`, `crash`, `wave`, ...) or a number "
                         "for a linear move. Run `,admin pump patterns` for the full list.",
                )
                return
            is_random_pattern = True
        else:
            try:
                _as_num = float(pattern_arg)
                pattern = "linear"
                magnitude = _as_num
            except ValueError:
                if pattern_arg.lower() in {"random", "?", "rand"}:
                    is_random_pattern = True
                else:
                    resolved = resolve_pattern(pattern_arg)
                    if resolved is None:
                        await ctx.reply_error_hint(
                            f"Unknown pattern **{pattern_arg}**.",
                            hint="Run `,admin pump patterns` for the catalog.",
                        )
                        return
                    pattern = resolved

        # Magnitude (only when pattern is fixed and arg given)
        if mag_arg is not None:
            try:
                magnitude = float(mag_arg)
            except ValueError:
                await ctx.reply_error(f"Magnitude must be a number, got `{mag_arg}`.")
                return

        # Duration
        if dur_arg is not None:
            try:
                duration = float(dur_arg)
            except ValueError:
                await ctx.reply_error(f"Duration must be a number, got `{dur_arg}`.")
                return

        # ── Validate ───────────────────────────────────────────────────────
        if magnitude is not None and not (0.0 < abs(magnitude) <= 100000.0):
            await ctx.reply_error("Magnitude must be between -100000% and +100000% (non-zero).")
            return
        if duration is not None and not (1.0 <= duration <= 1440.0):
            await ctx.reply_error("Duration must be 1-1440 minutes (up to 24 hours).")
            return

        # ── Schedule events ────────────────────────────────────────────────
        rng = random.Random()
        now = time.time()
        affected: list[dict] = []
        # Non-chaos `random` keyword: pick ONE pattern shared by all tokens.
        shared_random_pattern: str | None = (
            random_pattern(rng) if (is_random_pattern and not chaos_mode) else None
        )
        for symbol in target_syms:
            row = await ctx.db.get_price(symbol, ctx.guild_id)
            if not row:
                continue
            start_price = float(row["price"])
            if start_price <= 0:
                continue

            # Per-token pattern selection
            if chaos_mode:
                _pat = random_pattern(rng)
            elif shared_random_pattern is not None:
                _pat = shared_random_pattern
            else:
                assert pattern is not None
                _pat = pattern

            # Per-token magnitude (chaos randomizes per token; otherwise honor
            # any explicit value, else fall back to the pattern's default).
            if chaos_mode or magnitude is None:
                _mag = random_magnitude(_pat, rng)
            else:
                _mag = magnitude

            # Per-token duration (chaos randomizes per token if not explicit)
            if chaos_mode and duration is None:
                _dur = random_duration(rng)
            elif duration is None:
                _dur = 45.0
            else:
                _dur = duration

            seed = rng.randrange(1, 2**31)
            ev = {
                "start_ts":      now,
                "end_ts":        now + _dur * 60.0,
                "start_price":   start_price,
                "pattern":       _pat,
                "magnitude_pct": _mag,
                "seed":          seed,
            }
            _admin_price_events[(ctx.guild_id, symbol)] = ev
            try:
                await ctx.db.upsert_admin_price_event(ctx.guild_id, symbol, ev)
            except Exception:
                log.exception(
                    "admin pump: persist failed gid=%s sym=%s",
                    ctx.guild_id, symbol,
                )
            affected.append({
                "sym":      symbol,
                "start":    start_price,
                "pattern":  _pat,
                "mag":      _mag,
                "duration": _dur,
            })

        if not affected:
            await ctx.reply_error(f"Nothing eligible to pump for {target_label}.")
            return

        # Warn if any targeted symbol is a hard-pegged stablecoin -- the DB
        # layer's update_price snaps stablecoin prices back to start_price on
        # every tick, so the visible chart will not move.
        pegged: list[str] = []
        for info in affected:
            cfg = Config.TOKENS.get(info["sym"], {})
            if cfg.get("stablecoin") or cfg.get("consensus") == "Fiat":
                pegged.append(info["sym"])

        # ── Build response embed ───────────────────────────────────────────
        if len(affected) == 1:
            embed_b = self._pump_single_card(affected[0])
        else:
            embed_b = self._pump_mass_card(target_label, affected, chaos_mode)
        if pegged:
            embed_b.field(
                "⚠️ Pegged tokens included",
                f"`{', '.join(sorted(pegged)[:8])}` "
                + (f"+{len(pegged)-8} more" if len(pegged) > 8 else "")
                + " -- stablecoin prices are clamped in `update_price`, "
                "so the visible chart will not move.",
                False,
            )
        await ctx.reply(embed=embed_b.build(), mention_author=False)

    # ── pump helpers ──────────────────────────────────────────────────────

    async def _pump_resolve_target(
        self, ctx: DiscoContext, raw: str,
    ) -> tuple[list[str], str, bool]:
        """Resolve a pump target string → (symbols, display_label, chaos_mode).

        Raises :class:`_PumpTargetError` on user-facing errors. ``chaos_mode``
        is True when the caller asked for per-token randomization (``each`` or
        ``each:<category>``).
        """
        from core.framework.network import normalize_full

        s = raw.strip().lower()
        chaos = False
        category = s

        if s.startswith("each:"):
            chaos = True
            category = s.split(":", 1)[1] or "all"
        elif s in {"each", "chaos"}:
            chaos = True
            category = "all"

        # Network-scoped target: chain:<short> or network:<short>
        if category.startswith(("chain:", "network:", "net:")):
            short_in = category.split(":", 1)[1]
            full = normalize_full(short_in)
            if not full:
                raise _PumpTargetError(f"Unknown network `{short_in}`.")
            tokens = await self._pump_tokens_on_network(ctx, full)
            label = f"chain:{short_in}" + (" (chaos)" if chaos else "")
            return tokens, label, chaos

        # All / everything (default category)
        if category in {"all", "everything", "*", ""}:
            tokens = await self._pump_filtered_tokens(ctx, _pump_filter_nonstable)
            label = ("each (chaos -- all non-stables)" if chaos
                     else "everything (non-stablecoins)")
            return tokens, label, chaos

        # Named categories
        if category in {"coin", "coins", "networkcoins"}:
            tokens = await self._pump_filtered_tokens(ctx, _pump_filter_network_coin)
            return tokens, "network coins" + (" (chaos)" if chaos else ""), chaos
        if category in {"stable", "stables", "stablecoin", "stablecoins"}:
            tokens = await self._pump_filtered_tokens(ctx, _pump_filter_stable)
            return tokens, "stablecoins" + (" (chaos)" if chaos else ""), chaos
        if category in {"group", "groups", "community"}:
            rows = await ctx.db.get_group_tokens(ctx.guild_id)
            tokens = [r["symbol"] for r in rows]
            return tokens, "group tokens" + (" (chaos)" if chaos else ""), chaos
        if category in {"earn", "earnonly", "game", "games", "earnonlytokens"}:
            tokens = await self._pump_filtered_tokens(ctx, _pump_filter_earn_only)
            return tokens, "earn-only tokens" + (" (chaos)" if chaos else ""), chaos
        if category in {"wrapped", "wrap", "wraps", "pegged"}:
            tokens = await self._pump_filtered_tokens(ctx, _pump_filter_wrapped)
            return tokens, "wrapped coins" + (" (chaos)" if chaos else ""), chaos
        if category == "pow":
            tokens = await self._pump_filtered_tokens(ctx, _pump_filter_pow)
            return tokens, "PoW coins" + (" (chaos)" if chaos else ""), chaos
        if category == "pos":
            tokens = await self._pump_filtered_tokens(ctx, _pump_filter_pos)
            return tokens, "PoS tokens" + (" (chaos)" if chaos else ""), chaos
        if category in {"builtin", "core", "official"}:
            tokens = [s for s in Config.TOKENS.keys()]
            tokens = await self._pump_filter_to_existing(ctx, tokens)
            return tokens, "built-in tokens" + (" (chaos)" if chaos else ""), chaos
        if category in {"meme", "memes"}:
            tokens = await self._pump_filtered_tokens(ctx, _pump_filter_meme)
            return tokens, "meme tokens" + (" (chaos)" if chaos else ""), chaos

        # Otherwise treat as a single symbol
        if chaos:
            raise _PumpTargetError(
                f"`each:{category}` only works with a known category, not symbol `{category.upper()}`."
            )
        return [raw.upper()], f"**{raw.upper()}**", False

    async def _pump_filtered_tokens(self, ctx: DiscoContext, predicate) -> list[str]:
        """Return all symbols on this guild's price table where predicate(sym, cfg, row) is True."""
        rows = await ctx.db.get_all_prices(ctx.guild_id)
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        out: list[str] = []
        for r in rows:
            sym = r["symbol"]
            cfg = all_tokens.get(sym, Config.TOKENS.get(sym, {}))
            if predicate(sym, cfg, r):
                out.append(sym)
        return out

    async def _pump_filter_to_existing(self, ctx: DiscoContext, syms: list[str]) -> list[str]:
        rows = await ctx.db.get_all_prices(ctx.guild_id)
        present = {r["symbol"] for r in rows}
        return [s for s in syms if s in present]

    async def _pump_tokens_on_network(self, ctx: DiscoContext, full_network: str) -> list[str]:
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        rows = await ctx.db.get_all_prices(ctx.guild_id)
        present = {r["symbol"] for r in rows}
        out: list[str] = []
        for sym, cfg in all_tokens.items():
            if sym not in present:
                continue
            if (cfg.get("network") or "").strip() == full_network:
                out.append(sym)
        return sorted(out)

    def _pump_pattern_color(self, pattern: str) -> int:
        from services.chart_patterns import PATTERNS
        bias = PATTERNS.get(pattern, {}).get("bias", "vol")
        return {"bull": C_BULL, "bear": C_BEAR, "vol": C_VOLATILE}.get(bias, C_INFO)

    def _pump_single_card(self, info: dict):
        """Build the embed for a single-token pump start."""
        from services.chart_patterns import PATTERNS
        pat   = info["pattern"]
        mag   = info["mag"]
        dur   = info["duration"]
        sym   = info["sym"]
        start = info["start"]
        blurb = PATTERNS[pat]["blurb"]

        b = card(f"📈 {sym} -- {pat.replace('_',' ').title()} pattern engaged",
                 color=self._pump_pattern_color(pat))
        b.field("Token",     f"**{sym}**", True)
        b.field("Pattern",   f"`{pat}`",   True)
        b.field("Magnitude", f"`{mag:+.1f}%`", True)
        b.field("Duration",  f"`{dur:.0f} min`", True)
        b.field("Start",     f"`${start:,.6f}`", True)
        b.field("Cancel",    f"`,admin pump {sym} 0`", True)
        b.description(
            f"_{blurb}_\n"
            f"Oracle reversion is bypassed -- the chart will trace the "
            f"`{pat}` curve over the next **{dur:.0f} min** and snap to the "
            f"final value at the end."
        )
        return b

    def _pump_mass_card(self, label: str, affected: list[dict], chaos: bool):
        """Build the embed for a multi-token pump."""
        from collections import Counter

        n = len(affected)
        title_emoji = "🎲" if chaos else "📊"
        title = f"{title_emoji} {label} -- {n} token{'s' if n != 1 else ''} engaged"

        # Pick a color that matches the dominant bias of affected patterns
        biases = Counter()
        from services.chart_patterns import PATTERNS
        for info in affected:
            biases[PATTERNS.get(info["pattern"], {}).get("bias", "vol")] += 1
        dominant_bias = biases.most_common(1)[0][0] if biases else "vol"
        color = {"bull": C_BULL, "bear": C_BEAR, "vol": C_VOLATILE}[dominant_bias]

        b = card(title, color=color)

        if chaos:
            pat_counts = Counter(i["pattern"] for i in affected)
            top = ", ".join(f"`{p}`×{c}" for p, c in pat_counts.most_common(6))
            b.field("Mode",     "**Chaos** -- random per token", True)
            b.field("Patterns", f"{top}", False)
        else:
            shared = affected[0]
            b.field("Pattern",   f"`{shared['pattern']}`", True)
            b.field("Magnitude", f"`{shared['mag']:+.1f}%`", True)
            b.field("Duration", f"`{shared['duration']:.0f} min`", True)

        b.field("Tokens Affected", f"**{n}**", True)

        sample = affected[:10]
        if chaos:
            preview_lines = [
                f"`{i['sym']:<8}` {i['pattern']:<14} `{i['mag']:+.1f}%` over `{i['duration']:.0f}m`"
                for i in sample
            ]
        else:
            preview_lines = [
                f"`{i['sym']:<8}` start `${i['start']:,.6f}`" for i in sample
            ]
        if len(affected) > 10:
            preview_lines.append(f"… +{len(affected) - 10} more")
        b.field("Roster", "\n".join(preview_lines)[:1024], False)

        b.description(
            f"Hold onto your bags. {n} token chart{'s' if n != 1 else ''} just got rewritten."
            f"\nUse `,admin pump {label.split(' ')[0]} 0` to cancel "
            f"or `,admin pump active` to see what's running."
        )
        return b

    async def _pump_show_patterns(self, ctx: DiscoContext) -> None:
        """List every chart pattern in a paginated card."""
        from services.chart_patterns import PATTERNS
        bias_emoji = {"bull": "🟢", "bear": "🔴", "vol": "🟡"}
        lines = []
        for name, meta in PATTERNS.items():
            ico = bias_emoji.get(meta["bias"], "⚪")
            lines.append(
                f"{ico} `{name}` -- {meta['blurb']}  *(default mag `{meta['default_mag']:.0f}%`)*"
            )
        b = card("📚 Pump Pattern Catalog", color=C_INFO)
        b.description(
            "Pass any of these as the second arg.\n"
            "🟢 = bullish · 🔴 = bearish · 🟡 = neutral / volatility\n\n"
            + "\n".join(lines)
        )
        b.footer(
            "Aliases: rocket→moon · rug→rugpull · v→vshape · hs→hns · "
            "cup→cup_handle · flag→bullflag · chop→volatile · zz→zigzag"
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    async def _pump_show_active(self, ctx: DiscoContext) -> None:
        """List active pump events on this guild."""
        from cogs.trade import _admin_price_events

        now = time.time()
        rows = [
            (sym, ev) for (g, sym), ev in _admin_price_events.items()
            if g == ctx.guild_id
        ]
        if not rows:
            await ctx.reply_error("No active price events on this guild.")
            return

        # One compact line per event keeps the entire roster well under
        # Discord's 6000-char embed budget, and we paginate at 15 events
        # per page so we never run into the 25-field limit either.
        rows.sort(key=lambda x: x[1].get("end_ts", 0.0))
        lines: list[str] = []
        for sym, ev in rows:
            pat = ev.get("pattern", "linear")
            mag = ev.get("magnitude_pct", 0.0)
            remaining = max(0.0, ev.get("end_ts", now) - now) / 60.0
            total = (ev.get("end_ts", now) - ev.get("start_ts", now)) / 60.0
            elapsed = max(0.0, total - remaining)
            lines.append(
                f"`{sym:<8}` `{pat:<14}` `{mag:+6.1f}%` "
                f"{elapsed:5.1f}/{total:.0f}m left `{remaining:5.1f}m`"
            )

        per_page = 15
        pages: list[discord.Embed] = []
        total_pages = max(1, (len(lines) + per_page - 1) // per_page)
        for i in range(0, len(lines), per_page):
            page_no = i // per_page + 1
            chunk = lines[i:i + per_page]
            b = card(
                f"⚡ Active Price Events ({len(rows)})  ·  page {page_no}/{total_pages}",
                color=C_VOLATILE,
            )
            b.description("\n".join(chunk))
            b.footer("Cancel: ,admin pump <SYM> 0  |  Cancel all: ,admin pump all 0")
            pages.append(b.build())

        if len(pages) == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
        else:
            await send_paginated(ctx, pages)

    async def _pump_auto_dispatch(self, ctx: DiscoContext, sub: str) -> None:
        """Handle ``,admin pump auto <status|on|off|now>`` for one guild.

        Per-guild on/off lives in ``self._auto_pump_disabled_guilds`` (an
        in-memory set, since the auto-pump is informally configured -- if
        you want it permanently off, set ``Config.AUTO_PUMP_ENABLED = False``).
        ``now`` forces a fire on the current guild and resets the schedule.
        """
        from cogs.trade import _admin_price_events
        from services.chart_patterns import (
            random_duration, random_magnitude, random_pattern,
        )

        gid = ctx.guild_id
        disabled = self._auto_pump_disabled_guilds
        global_on = bool(getattr(Config, "AUTO_PUMP_ENABLED", True))

        if sub in {"on", "enable", "start", "resume"}:
            disabled.discard(gid)
            await ctx.reply_success(
                "Auto-pumps re-enabled on this guild." if global_on else
                "Auto-pumps re-enabled on this guild, but the global "
                "`Config.AUTO_PUMP_ENABLED` flag is **off** -- nothing will fire "
                "until that's flipped on too.",
                title="Auto-pump on",
            )
            return
        if sub in {"off", "disable", "stop", "pause"}:
            disabled.add(gid)
            await ctx.reply_success(
                "Auto-pumps paused on this guild. Existing live events keep "
                "running -- this only stops new rolls.",
                title="Auto-pump off",
            )
            return
        if sub in {"now", "fire", "trigger", "roll"}:
            try:
                await self._auto_pump_fire(
                    ctx.guild, time.time(), self._auto_pump_rng,
                    random_pattern, random_magnitude, random_duration,
                    _admin_price_events,
                )
            except Exception as exc:
                await ctx.reply_error(f"Forced auto-pump failed: {exc}")
                return
            await ctx.reply_success(
                "Forced an auto-pump roll for this guild. Check `,admin pump active`.",
                title="Auto-pump fired",
            )
            return
        if sub in {"status", ""}:
            now = time.time()
            next_ts = self._auto_pump_next.get(gid)
            paused = gid in disabled
            lo = float(getattr(Config, "AUTO_PUMP_INTERVAL_MIN_S", 60.0))
            hi = float(getattr(Config, "AUTO_PUMP_INTERVAL_MAX_S", 3600.0))
            state = (
                "**OFF** (global `Config.AUTO_PUMP_ENABLED` is False)"
                if not global_on else
                ("**PAUSED** (this guild)" if paused else "**ON**")
            )
            next_line = "(unscheduled -- task may not have ticked yet)"
            if next_ts is not None:
                delta = max(0.0, next_ts - now) / 60.0
                next_line = f"<t:{int(next_ts)}:R> (in **{delta:.1f} min**)"
            b = (
                card("\U0001F4C8 Auto-pump scheduler", color=C_INFO)
                .description(
                    f"State: {state}\n"
                    f"Interval: every **{lo / 60.0:.0f}-{hi / 60.0:.0f} min** per guild "
                    f"(jittered)\n"
                    f"Next fire: {next_line}"
                )
                .field(
                    "Controls",
                    "`,admin pump auto on`   -- resume on this guild\n"
                    "`,admin pump auto off`  -- pause on this guild\n"
                    "`,admin pump auto now`  -- force a roll right now\n"
                    "`,admin pump auto status` -- this view",
                    False,
                )
                .footer(
                    "Auto-pumps move the spot oracle through a chart pattern. "
                    "Per-trade impact + slippage still apply on top."
                )
            )
            await ctx.reply(embed=b.build(), mention_author=False)
            return

        await ctx.reply_error_hint(
            f"Unknown auto-pump subcommand `{sub}`.",
            hint="Try `,admin pump auto status` (or on / off / now).",
        )

    # ── Token contract management ──────────────────────────────────────────────

    @admin.command(name="contract")
    @_require_manage_guild()
    async def admin_contract(self, ctx: DiscoContext, symbol: str) -> None:
        """View current token contract parameters. Usage: $admin contract <SYM>"""
        sym = symbol.upper()
        params = await ctx.db.get_token_contract(ctx.guild_id, sym)
        _b = card(f"Token Contract: {sym}", color=C_PURPLE)
        if not params:
            _b.description("No contract rules set. Token transfers are fee-free.")
        else:
            max_supply = params.get("max_supply", 0)
            _b.field("Transfer Fee", f"**{float(params.get('transfer_fee', 0)) * 100:.2f}%**", True)
            _b.field("Burn Rate", f"**{float(params.get('burn_rate', 0)) * 100:.2f}%**", True)
            _b.field("Max Supply", f"**{float(max_supply):,.2f}**" if max_supply else "Unlimited", True)
        embed = _b.footer("Use $admin setcontract to modify  |  $admin clearcontract to remove").build()
        await ctx.reply(embed=embed, mention_author=False)

    @admin.command(name="setcontract")
    @_require_manage_guild()
    async def admin_setcontract(
        self, ctx: DiscoContext, symbol: str, field: str, value: str
    ) -> None:
        """Set a token contract parameter. Usage: $admin setcontract <SYM> <field> <value>
        Fields: transfer_fee (0 - 0.10), burn_rate (0 - 0.10), max_supply (0=unlimited)"""
        sym = symbol.upper()
        field = field.lower()
        allowed_fields = {"transfer_fee", "burn_rate", "max_supply"}
        if field not in allowed_fields:
            await ctx.reply_error(f"Unknown field `{field}`. Valid: {', '.join(sorted(allowed_fields))}")
            return

        try:
            fval = float(value)
        except ValueError:
            await ctx.reply_error("Value must be a number.")
            return

        if field in ("transfer_fee", "burn_rate"):
            if not (0.0 <= fval <= 0.10):
                await ctx.reply_error(f"`{field}` must be between 0 and 0.10 (0 - 10%).")
                return
        elif field == "max_supply" and fval < 0:
            await ctx.reply_error("`max_supply` cannot be negative. Use 0 for unlimited.")
            return

        # Merge into existing params
        existing = await ctx.db.get_token_contract(ctx.guild_id, sym) or {}
        existing[field] = fval
        await ctx.db.set_token_contract(ctx.guild_id, sym, existing)

        display = f"{fval * 100:.2f}%" if field in ("transfer_fee", "burn_rate") else str(fval)
        await ctx.reply_success(
            f"**{sym}** contract: `{field}` → **{display}**",
            title="Contract Updated",
        )

    @admin.command(name="clearcontract")
    @_require_manage_guild()
    async def admin_clearcontract(self, ctx: DiscoContext, symbol: str) -> None:
        """Remove all contract rules for a token. Usage: $admin clearcontract <SYM>"""
        sym = symbol.upper()
        await ctx.db.set_token_contract(ctx.guild_id, sym, {})
        await ctx.reply_success(
            f"All contract rules for **{sym}** cleared. Transfers are now fee-free.",
            title="Contract Cleared",
        )

    # ── Channel / feed management ─────────────────────────────────────────────

    _CHANNEL_COLS = {
        "trade":    ("trade_channel",    "Trade feed"),
        "mine":     ("mine_channel",     "Mining feed"),
        "staking":     ("staking_channel",     "Staking feed"),
        "validators":  ("validators_channel",  "Validator block feed"),
        "gambling":    ("gambling_channel",    "Gambling feed"),
        "pools":    ("pools_channel",    "Pools feed"),
        "crypto":   ("crypto_channel",   "Crypto feed"),
        "drops":       ("drops_channel",       "Drops feed (claimed events log)"),
        "dropsspawn":  ("drops_spawn_channel", "Drops spawn channel (where drops appear to claim)"),
        "faucet":      ("faucet_channel",      "Faucet spawn channel (where auto-faucet drops appear)"),
        "job":         ("job_channel",         "Job & career feed"),
        "contracts":   ("contracts_channel",   "Smart contract event feed"),
        "wallet":      ("wallet_channel",      "DeFi wallet event feed"),
        "error":       ("error_channel",       "Bot error log feed"),
        "whale":       ("whale_alerts_channel", "Whale alerts feed"),
        "reports":     ("reports_feed_channel", "Reports feed"),
        "nft":         ("nft_channel",          "NFT activity feed"),
        "predictions": ("predictions_channel",  "Prediction markets feed"),
        "events":      ("events_channel",       "Market events feed"),
        "ape":         ("ape_channel",          "Ape / degen feed"),
        "vault":       ("vault_feed_channel",   "Vault level-up feed"),
        "grouphall":   ("grouphall_channel",     "Group Hall parent channel"),
        "income":      ("income_channel",        "Silent chat income channel"),
        "changelog":   ("changelog_channel",     "Daily changelog auto-post channel"),
    }

    # Category groups for "setchannel economy #ch", "setchannel bot #ch", etc.
    _CHANNEL_CATEGORIES = {
        "economy": ["trade", "crypto", "pools", "wallet", "whale", "contracts", "vault"],
        "earning": ["mine", "staking", "job", "validators", "income"],
        "fun":     ["gambling", "drops", "dropsspawn", "faucet", "ape"],
        "bot":     ["error", "reports", "events", "changelog"],
        "collectibles": ["nft", "predictions"],
    }

    async def _resolve_channel(self, ctx: DiscoContext, channel_input: str):
        """Resolve a channel mention, ID, or name to a TextChannel or Thread.
        Returns the channel/thread object, or a string error message on failure."""
        raw = channel_input.strip().lstrip("<#").rstrip(">")
        ch = None
        if raw.isdigit():
            ch_id = int(raw)
            ch = ctx.guild.get_channel_or_thread(ch_id)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(ch_id)
                except Exception:
                    pass
        if ch is None:
            name_lower = raw.lower()
            for candidate in list(ctx.guild.channels) + list(ctx.guild.threads):
                if candidate.name.lower() == name_lower:
                    ch = candidate
                    break

        if ch is None:
            return (
                f"Cannot find channel or thread `{channel_input}`.\n"
                "Tip: paste the channel ID or thread ID directly. "
                "For forum posts, right-click the post → Copy ID."
            )
        if isinstance(ch, discord.ForumChannel):
            return (
                f"**{ch.name}** is a Forum Channel  -  you need to target a specific **post** inside it.\n"
                "Open the forum, right-click a post → **Copy ID**, then use that ID."
            )
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return "Target must be a text channel or thread (including forum posts)."
        return ch

    @admin.command(name="setchannel")
    @_require_manage_guild()
    async def admin_setchannel(
        self, ctx: DiscoContext, feed: str, channel_input: str
    ) -> None:
        """Set a feed channel or thread.

        Usage: .admin setchannel <type|category|all> #channel
        Types: trade, mine, staking, gambling, pools, crypto, drops, wallet, error, …
        Categories: economy, earning, fun, bot
        Use 'all' to point every feed to one channel.
        """
        key = feed.lower()

        # ── "all" shorthand: set every feed column to one channel ────────────
        if key == "all":
            result = await self._resolve_channel(ctx, channel_input)
            if isinstance(result, str):
                await ctx.reply_error(result)
                return
            ch = result
            for col, label in self._CHANNEL_COLS.values():
                await ctx.db.set_channel(ctx.guild_id, col, ch.id)
            count = len(self._CHANNEL_COLS)
            await ctx.reply_success(
                f"All **{count}** feed channels → {ch.mention}",
                title="All Channels Set",
            )
            return

        # ── Category shorthand: economy, earning, fun, bot ───────────────────
        if key in self._CHANNEL_CATEGORIES:
            result = await self._resolve_channel(ctx, channel_input)
            if isinstance(result, str):
                await ctx.reply_error(result)
                return
            ch = result
            feeds = self._CHANNEL_CATEGORIES[key]
            set_labels = []
            for f_key in feeds:
                mapping = self._CHANNEL_COLS.get(f_key)
                if mapping:
                    col, label = mapping
                    await ctx.db.set_channel(ctx.guild_id, col, ch.id)
                    set_labels.append(label)
            await ctx.reply_success(
                f"**{key.title()}** feeds ({len(set_labels)}) → {ch.mention}\n"
                + "\n".join(f"• {l}" for l in set_labels),
                title=f"{key.title()} Channels Set",
            )
            return

        # ── Single feed ───────────────────────────────────────────────────────
        mapping = self._CHANNEL_COLS.get(key)
        if not mapping:
            cats = ", ".join(f"`{k}`" for k in self._CHANNEL_CATEGORIES)
            valid = ", ".join(f"`{k}`" for k in self._CHANNEL_COLS)
            await ctx.reply_error(
                f"Unknown feed type `{feed}`.\n"
                f"**Categories:** {cats}, `all`\n"
                f"**Individual:** {valid}"
            )
            return

        result = await self._resolve_channel(ctx, channel_input)
        if isinstance(result, str):
            await ctx.reply_error(result)
            return
        ch = result

        col, label = mapping
        await ctx.db.set_channel(ctx.guild_id, col, ch.id)
        await ctx.reply_success(f"{label} → {ch.mention}", title="Channel Set")

    # ── Bot channel (no-prefix mode) ─────────────────────────────────────────

    @admin.command(name="botchannel")
    @_require_manage_guild()
    async def admin_botchannel(self, ctx: DiscoContext, channel_input: str = "") -> None:
        """Toggle no-prefix mode for a channel.

        In bot channels, players type commands without a prefix:
        ``work``, ``buy 10 arc``, ``help``, etc.

        Usage: .admin botchannel #channel
        Run again to remove. Run with no argument to list current bot channels.
        """
        if not channel_input:
            # List current bot channels
            ch_ids = await ctx.db.get_bot_channels(ctx.guild_id)
            if not ch_ids:
                await ctx.reply_error(
                    f"No bot channels set. Use `{ctx.prefix}admin botchannel #channel` to add one."
                )
                return
            lines = []
            for cid in ch_ids:
                ch = ctx.guild.get_channel(cid)
                lines.append(ch.mention if ch else f"`{cid}` (deleted?)")
            _b = card("🤖 Bot Channels (No-Prefix Mode)", color=C_INFO)
            _b.description("\n".join(lines))
            _b.footer(
                f"{ctx.prefix}admin botchannel clear  -  wipe the list  ·  "
                "players type commands without prefix in these channels"
            )
            await ctx.reply(embed=_b.build(), mention_author=False)
            return

        # ``clear`` / ``reset`` / ``none`` wipes the entire allowlist in one
        # shot -- every other path requires resolving a channel handle.
        if channel_input.strip().lower() in ("clear", "reset", "none", "wipe", "all"):
            removed = await ctx.db.clear_bot_channels(ctx.guild_id)
            await ctx.reply_success(
                f"Cleared **{removed}** bot channel(s). "
                f"Every channel now requires the `{ctx.prefix}` prefix again. "
                f"Add new ones with `{ctx.prefix}admin botchannel #channel`.",
                title="Bot Channels Cleared",
            )
            return

        result = await self._resolve_channel(ctx, channel_input)
        if isinstance(result, str):
            await ctx.reply_error(result)
            return
        ch = result

        is_set = await ctx.db.is_bot_channel(ctx.guild_id, ch.id)
        if is_set:
            await ctx.db.remove_bot_channel(ctx.guild_id, ch.id)
            await ctx.reply_success(
                f"Removed {ch.mention} from bot channels.\n"
                f"Players must use the `{ctx.prefix}` prefix there again.",
                title="Bot Channel Removed",
            )
        else:
            await ctx.db.add_bot_channel(ctx.guild_id, ch.id)
            await ctx.reply_success(
                f"Added {ch.mention} as a bot channel.\n"
                f"Players can now type commands without a prefix:\n"
                f"`work` · `buy 10 arc` · `help` · `status`",
                title="Bot Channel Added",
            )

    # ── AI ambient chat channels (allowlist) ─────────────────────────────────

    @admin.command(name="aichannel")
    @_require_manage_guild()
    async def admin_aichannel(self, ctx: DiscoContext, channel_input: str = "") -> None:
        """Allowlist channels where Disco may post unsolicited ambient chatter.

        Empty list = Disco can chime in anywhere the bot has send permission.
        Non-empty list = ambient chatter is restricted to the listed channels.
        Reactive paths (`,ask`, `@mention`, reply-to-bot) always work regardless.

        Usage: `,admin aichannel #channel` (toggle), `,admin aichannel` (list).
        """
        if not channel_input:
            ch_ids = await ctx.db.get_ai_chat_channels(ctx.guild_id)
            _b = card("AI Chat Channels (Ambient Allowlist)", color=C_INFO)
            if not ch_ids:
                _b.description(
                    "No channels set. Ambient chatter can fire in any channel "
                    f"the bot can see. Use `{ctx.prefix}admin aichannel #channel` "
                    "to restrict ambient chatter to specific channels."
                )
            else:
                lines = []
                for cid in ch_ids:
                    ch = ctx.guild.get_channel(cid)
                    lines.append(ch.mention if ch else f"`{cid}` (deleted?)")
                _b.description("\n".join(lines))
                _b.footer(
                    f"{ctx.prefix}admin aichannel clear  -  wipe the list  ·  "
                    "ambient Disco chatter is restricted to these channels."
                )
            await ctx.reply(embed=_b.build(), mention_author=False)
            return

        # ``clear`` / ``reset`` / ``none`` wipes the entire allowlist. Empty
        # list = ambient chatter allowed wherever the bot can post.
        if channel_input.strip().lower() in ("clear", "reset", "none", "wipe", "all"):
            removed = await ctx.db.clear_ai_chat_channels(ctx.guild_id)
            await ctx.reply_success(
                f"Cleared **{removed}** AI chat channel(s). "
                f"Ambient Disco chatter can now fire in any channel the bot "
                f"can post in. Add new ones with `{ctx.prefix}admin aichannel #channel`.",
                title="AI Chat Channels Cleared",
            )
            return

        result = await self._resolve_channel(ctx, channel_input)
        if isinstance(result, str):
            await ctx.reply_error(result)
            return
        ch = result

        is_set = await ctx.db.is_ai_chat_channel(ctx.guild_id, ch.id)
        if is_set:
            await ctx.db.remove_ai_chat_channel(ctx.guild_id, ch.id)
            remaining = await ctx.db.get_ai_chat_channels(ctx.guild_id)
            if remaining:
                tail = "Ambient chatter still restricted to the remaining allowlisted channels."
            else:
                tail = "Allowlist is now empty. Ambient chatter can fire in any channel again."
            await ctx.reply_success(
                f"Removed {ch.mention} from the AI chat allowlist.\n{tail}",
                title="AI Chat Channel Removed",
            )
        else:
            await ctx.db.add_ai_chat_channel(ctx.guild_id, ch.id)
            await ctx.reply_success(
                f"Added {ch.mention} to the AI chat allowlist.\n"
                "Ambient Disco chatter will now only fire in allowlisted channels.\n"
                "Reactive replies (`,ask`, mentions, replies-to-bot) are unaffected.",
                title="AI Chat Channel Added",
            )

    # ── Whale threshold ───────────────────────────────────────────────────────

    @admin.command(name="whalethreshold", description="Set whale alert USD threshold.")
    @_require_manage_guild()
    async def admin_whale_threshold(self, ctx: DiscoContext, amount: float) -> None:
        if amount < 0:
            await ctx.reply_error("Threshold must be positive.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "whale_alert_threshold", to_raw(amount))
        await ctx.reply_success(f"Whale alerts will trigger for transactions >= **${amount:,.0f}**")

    # ── Reports feed categories ───────────────────────────────────────────────

    @admin.command(name="reportsfeed", description="Configure which report categories appear in the reports feed.")
    @_require_manage_guild()
    async def admin_reports_feed(self, ctx: DiscoContext, categories: str = "") -> None:
        """Set which report categories post to the reports feed channel.

        Usage:
          .admin reportsfeed                     -  show current categories
          .admin reportsfeed bugs,suggestions    -  set specific categories
          .admin reportsfeed all                 -  enable all categories
        """
        valid = {"bugs", "suggestions", "users", "other"}
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        if not categories:
            current = settings.get("reports_feed_categories", "bugs,suggestions,users,other")
            ch_id = settings.get("reports_feed_channel", 0)
            ch_str = f"<#{ch_id}>" if ch_id else "Not set"
            await ctx.reply(
                embed=card("📋 Reports Feed Config", color=C_INFO)
                .field("Channel", ch_str, True)
                .field("Categories", current or "all", True)
                .footer(f"Set channel: {ctx.prefix}admin setchannel reports #channel\n"
                        f"Set categories: {ctx.prefix}admin reportsfeed bugs,suggestions")
                .build(),
                mention_author=False,
            )
            return
        if categories.lower() == "all":
            cats = "bugs,suggestions,users,other"
        else:
            parts = [c.strip().lower() for c in categories.split(",") if c.strip()]
            bad = [c for c in parts if c not in valid]
            if bad:
                await ctx.reply_error(f"Unknown categories: {', '.join(bad)}. Valid: {', '.join(sorted(valid))}")
                return
            cats = ",".join(parts)
        await ctx.db.update_guild_setting(ctx.guild_id, "reports_feed_categories", cats)
        await ctx.reply_success(f"Reports feed will show categories: **{cats}**")

    @admin.command(name="errorfeed", description="Configure which severity levels appear in the error feed.")
    @_require_manage_guild()
    async def admin_error_feed(self, ctx: DiscoContext, levels: str = "") -> None:
        """Set which error severity levels post to the error feed channel.

        Usage:
          .admin errorfeed                         -  show current levels
          .admin errorfeed HIGH,CRITICAL           -  only show errors and critical
          .admin errorfeed WARNING,MEDIUM,HIGH,CRITICAL  -  include warnings
          .admin errorfeed all                     -  show everything including info
        """
        valid = {"INFO", "WARNING", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        if not levels:
            current = settings.get("error_feed_levels", "INFO,WARNING,LOW,MEDIUM,HIGH,CRITICAL")
            ch_id = settings.get("error_channel", 0)
            ch_str = f"<#{ch_id}>" if ch_id else "Not set"
            await ctx.reply(
                embed=card("⚠️ Error Feed Config", color=C_INFO)
                .field("Channel", ch_str, True)
                .field("Severity Levels", current or "all", True)
                .description(
                    "**Available levels:**\n"
                    "🔵 `INFO`  -  missing args, bad syntax, tips\n"
                    "🟠 `WARNING`  -  cooldowns, check failures\n"
                    "🟢 `LOW`  -  minor user input errors\n"
                    "🟡 `MEDIUM`  -  command failures, service errors\n"
                    "🔴 `HIGH`  -  unhandled exceptions\n"
                    "💀 `CRITICAL`  -  database/connection failures"
                )
                .footer(f"Set channel: {ctx.prefix}admin setchannel error #channel\n"
                        f"Set levels: {ctx.prefix}admin errorfeed HIGH,CRITICAL")
                .build(),
                mention_author=False,
            )
            return
        if levels.lower() == "all":
            lvls = "INFO,WARNING,LOW,MEDIUM,HIGH,CRITICAL"
        else:
            parts = [c.strip().upper() for c in levels.split(",") if c.strip()]
            bad = [c for c in parts if c not in valid]
            if bad:
                await ctx.reply_error(f"Unknown levels: {', '.join(bad)}. Valid: {', '.join(sorted(valid))}")
                return
            lvls = ",".join(parts)
        await ctx.db.update_guild_setting(ctx.guild_id, "error_feed_levels", lvls)
        await ctx.reply_success(f"Error feed will show severity levels: **{lvls}**")

    # ── Module toggles ────────────────────────────────────────────────────────

    _MODULE_NAMES = {
        "gambling", "lending", "staking", "mining",
        "drops", "faucet", "savings", "validators", "pools",
        "contracts", "groups", "chart", "crypto",
        "daily", "work", "economy", "chain",
        "ape", "nft", "predictions", "events", "shop", "games",
        "moons",
    }

    @admin.command(name="module")
    @_require_manage_guild()
    async def admin_module(self, ctx: DiscoContext, module: str, state: str) -> None:
        """Enable or disable a module (or all at once).
        Usage: .admin module <name> <on|off>
               .admin module all <on|off>           - toggle every module
               .admin module allbutwork <on|off>    - toggle every module except work
        Modules: gambling, lending, staking, mining, faucet, drops, savings, validators, pools,
                 contracts, groups, chart, crypto, daily, work, economy, chain,
                 ape, nft, predictions, events, shop, games, moons"""
        module = module.lower()
        state_lower = state.lower()
        if state_lower in ("on", "enable", "1", "true"):
            value = 1
        elif state_lower in ("off", "disable", "0", "false"):
            value = 0
        else:
            await ctx.reply_error("State must be `on` or `off`.")
            return

        if module in ("all", "allbutwork"):
            exclude = {"work"} if module == "allbutwork" else set()
            targets = self._MODULE_NAMES - exclude
            for m in targets:
                await ctx.db.update_guild_setting(ctx.guild_id, f"module_{m}", value)
            status = "enabled" if value else "disabled"
            skip_note = " (work unchanged)" if exclude else ""
            await ctx.reply_success(
                f"All modules {status}{skip_note}.",
                title="Modules Updated",
            )
            return

        if module not in self._MODULE_NAMES:
            valid = ", ".join(f"`{m}`" for m in sorted(self._MODULE_NAMES))
            await ctx.reply_error(f"Unknown module `{module}`. Valid: {valid}")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, f"module_{module}", value)
        emoji = "✅" if value else "❌"
        status = "enabled" if value else "disabled"
        await ctx.reply_success(
            f"**{module.title()}** module is now **{status}** {emoji}",
            title="Module Updated",
        )

    # ── Faucet settings ───────────────────────────────────────────────────────

    @admin.group(name="faucet", invoke_without_command=True, with_app_command=False)
    @_require_manage_guild()
    async def admin_faucet(self, ctx: DiscoContext) -> None:
        """Faucet configuration. Subcommands: multiplier, tokens"""
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        multiplier = float(settings.get("faucet_multiplier") or 1.0)
        tokens_raw = settings.get("faucet_tokens") or ""
        tokens_display = tokens_raw if tokens_raw.strip() else "All eligible tokens (default)"
        embed = (
            card("🚰 Faucet Settings")
            .field("Payout Multiplier", f"**{multiplier}×**", True)
            .field("Enabled Tokens", tokens_display, True)
            .field("Module Enabled", "✅" if await ctx.db.module_enabled(ctx.guild_id, "faucet") else "❌", True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin_faucet.command(name="multiplier")
    @_require_manage_guild()
    async def admin_faucet_multiplier(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the faucet payout multiplier. Usage: .admin faucet multiplier <value>
        Example: .admin faucet multiplier 2.0   -  doubles all faucet payouts"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "faucet_multiplier", multiplier)
        await ctx.reply_success(f"Faucet multiplier set to **{multiplier}×**.", title="Faucet Updated")

    @admin_faucet.command(name="tokens")
    @_require_manage_guild()
    async def admin_faucet_tokens(self, ctx: DiscoContext, *, tokens: str = "") -> None:
        """Whitelist tokens for random faucet drops. Leave blank to reset to all eligible.
        Usage: .admin faucet tokens MTA,DSC,ARC
               .admin faucet tokens         (resets to all tokens)"""
        clean = tokens.strip()
        if clean:
            syms = [s.strip().upper() for s in clean.split(",") if s.strip()]
            stored = ",".join(syms)
            msg = f"Faucet will randomly drop from: **{', '.join(syms)}**"
        else:
            stored = ""
            msg = "Faucet will randomly drop from all eligible tokens (default)."
        await ctx.db.update_guild_setting(ctx.guild_id, "faucet_tokens", stored)
        await ctx.reply_success(msg, title="Faucet Tokens Updated")

    # ── Per-guild earnings multipliers ───────────────────────────────────────

    # All keys: (setting_column, display_label, description)
    _MULTIPLIER_SETTINGS: dict[str, tuple[str, str]] = {
        "work":      ("work_multiplier",      "Work earnings"),
        "daily":     ("daily_multiplier",     "Daily reward"),
        "gambling":  ("gambling_multiplier",  "Gambling winnings"),
        "faucet":    ("faucet_multiplier",    "Faucet drops"),
        "mining":    ("mining_multiplier",    "Mining block rewards"),
        "staking":   ("staking_multiplier",   "Staking rewards"),
        "validator": ("validator_multiplier", "Validator gas rewards"),
        "drops":     ("drops_multiplier",     "Manual drops"),
        "beg":       ("beg_multiplier",       "Beg gains"),
        "ape":       ("ape_multiplier",       "Ape payouts"),
        "savings":   ("savings_multiplier",   "Savings interest"),
    }

    @admin.group(name="multiplier", aliases=["mult"], invoke_without_command=True)
    @_require_manage_guild()
    async def admin_multiplier(self, ctx: DiscoContext) -> None:
        """View all per-guild earnings multipliers.
        Usage: .admin multiplier <type> <value>
        Types: work, daily, gambling, faucet, mining, staking, validator, drops, beg, ape, savings"""
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        from core.framework.embed import card
        from core.framework.ui import C_INFO
        _b = card("Earnings Multipliers", color=C_INFO)
        for key, (col, label) in self._MULTIPLIER_SETTINGS.items():
            val = float(settings.get(col) or 1.0)
            _b.field(label, f"**{val}x**", True)
        _b.footer("Set with: .admin multiplier <type> <value>  |  1.0 = no change")
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_multiplier.command(name="work")
    @_require_manage_guild()
    async def admin_multiplier_work(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the work earnings multiplier. Example: .admin multiplier work 1.5"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "work_multiplier", multiplier)
        await ctx.reply_success(f"Work earnings multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="daily")
    @_require_manage_guild()
    async def admin_multiplier_daily(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the daily reward multiplier. Example: .admin multiplier daily 2.0"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "daily_multiplier", multiplier)
        await ctx.reply_success(f"Daily reward multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="gambling")
    @_require_manage_guild()
    async def admin_multiplier_gambling(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the gambling winnings multiplier. Example: .admin multiplier gambling 1.25"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "gambling_multiplier", multiplier)
        await ctx.reply_success(f"Gambling winnings multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="faucet")
    @_require_manage_guild()
    async def admin_multiplier_faucet(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the faucet drop multiplier. Example: .admin multiplier faucet 2.0"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "faucet_multiplier", multiplier)
        await ctx.reply_success(f"Faucet multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="mining")
    @_require_manage_guild()
    async def admin_multiplier_mining(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the mining block reward multiplier. Example: .admin multiplier mining 1.5"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "mining_multiplier", multiplier)
        await ctx.reply_success(f"Mining block reward multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="staking")
    @_require_manage_guild()
    async def admin_multiplier_staking(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the staking reward multiplier. Example: .admin multiplier staking 1.5"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "staking_multiplier", multiplier)
        await ctx.reply_success(f"Staking reward multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="validator")
    @_require_manage_guild()
    async def admin_multiplier_validator(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the validator gas reward multiplier. Example: .admin multiplier validator 1.5"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "validator_multiplier", multiplier)
        await ctx.reply_success(f"Validator reward multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="drops")
    @_require_manage_guild()
    async def admin_multiplier_drops(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the manual drop amount multiplier. Example: .admin multiplier drops 2.0"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "drops_multiplier", multiplier)
        await ctx.reply_success(f"Drops multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="beg")
    @_require_manage_guild()
    async def admin_multiplier_beg(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the beg gains multiplier. Example: .admin multiplier beg 1.5"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "beg_multiplier", multiplier)
        await ctx.reply_success(f"Beg multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="ape")
    @_require_manage_guild()
    async def admin_multiplier_ape(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the ape payout multiplier. Example: .admin multiplier ape 1.5"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "ape_multiplier", multiplier)
        await ctx.reply_success(f"Ape payout multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    @admin_multiplier.command(name="savings")
    @_require_manage_guild()
    async def admin_multiplier_savings(self, ctx: DiscoContext, multiplier: float) -> None:
        """Set the savings interest multiplier. Example: .admin multiplier savings 1.5"""
        if multiplier <= 0 or multiplier > 100:
            await ctx.reply_error("Multiplier must be between 0 (exclusive) and 100.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "savings_multiplier", multiplier)
        await ctx.reply_success(f"Savings interest multiplier set to **{multiplier}x**.", title="Multiplier Updated")

    # ── Orphaned stake recovery ───────────────────────────────────────────────

    @admin.command(name="recoverstakes", aliases=["recstakes", "fixstakes"])
    @_require_manage_guild()
    async def admin_recover_stakes(self, ctx: DiscoContext) -> None:
        """Recover funds from stakes whose validator no longer exists (e.g. after a migration).
        Refunds each player's orphaned stake amount to their DeFi wallet (or CeFi as fallback)."""
        recovered = await ctx.db.recover_orphaned_stakes(ctx.guild_id)
        if not recovered:
            await ctx.reply_success("No orphaned stakes found  -  all stakes are linked to valid nodes.", title="Recovery Complete")
            return
        lines = []
        for r in recovered[:20]:
            m = ctx.guild.get_member(r["user_id"])
            name = m.display_name if m else f"User {r['user_id']}"
            amt_h = to_human(int(r["amount"] or 0))
            lines.append(
                f"• **{name}**  -  `{amt_h:,.6f} {r['symbol']}` from `{r['validator_id']}` → `{r['credited_to']}`"
            )
        if len(recovered) > 20:
            lines.append(f"*… and {len(recovered) - 20} more*")
        embed = (
            card(f"✅ Recovered {len(recovered)} Orphaned Stake(s)", color=C_SUCCESS)
            .field("Refunds Applied", "\n".join(lines), False)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── MM Webhook management ─────────────────────────────────────────────────

    @admin.group(name="mmwebhook", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_mmwebhook(self, ctx: DiscoContext) -> None:
        """MM webhook management. Usage: $admin mmwebhook <create|delete|status>"""
        if await suggest_subcommand(ctx, self.admin_mmwebhook):
            return
        await self.admin_mmwebhook_status(ctx)

    @admin_mmwebhook.command(name="status")
    @_require_manage_guild()
    async def admin_mmwebhook_status(self, ctx: DiscoContext) -> None:
        """Show MM webhook configuration for this server."""
        row = await ctx.db.get_mm_webhook(ctx.guild_id)
        _b = card("Market Maker Webhook", color=C_INFO)
        if not row:
            _b.description(f"Not configured. Use `{ctx.prefix}admin mmwebhook create` to set up.")
        else:
            channel = ctx.guild.get_channel(row["channel_id"])
            ch_display = channel.mention if channel else f"<#{row['channel_id']}>"
            _b.description(f"Active in {ch_display}")
            _b.field("Webhook ID", str(row["webhook_id"]), True)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_mmwebhook.command(name="create")
    @_require_manage_guild()
    async def admin_mmwebhook_create(self, ctx: DiscoContext) -> None:
        """Create MM webhook in the configured trade channel."""
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        channel_id = settings.get("trade_channel") if settings else None
        if not channel_id:
            await ctx.reply_error(f"No trade channel set. Use `{ctx.prefix}admin setchannel trade #channel` first.")
            return
        channel = ctx.guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            await ctx.reply_error("Trade channel not found or is not a text channel.")
            return
        existing = await ctx.db.get_mm_webhook(ctx.guild_id)
        if existing:
            await ctx.reply_error(f"A webhook is already configured. Use `{ctx.prefix}admin mmwebhook delete` first.")
            return
        try:
            webhook = await channel.create_webhook(name="Discoin MM")
            await ctx.db.save_mm_webhook(ctx.guild_id, str(webhook.id), webhook.token, channel.id)
            await ctx.db.seed_default_mm_personas(ctx.guild_id)
            await ctx.reply_success(
                f"MM webhook created in {channel.mention}. "
                "Market maker trades will now appear as persona messages.\n"
                "Use `.admin persona list` to view and customize personas.",
                title="Webhook Created",
            )
        except discord.Forbidden:
            await ctx.reply_error("Missing Manage Webhooks permission in that channel.")
        except Exception as e:
            await ctx.reply_error(f"Failed to create webhook: {e}")

    @admin_mmwebhook.command(name="delete")
    @_require_manage_guild()
    async def admin_mmwebhook_delete(self, ctx: DiscoContext) -> None:
        """Delete the MM webhook for this server."""
        row = await ctx.db.get_mm_webhook(ctx.guild_id)
        if not row:
            await ctx.reply_error("No MM webhook configured for this server.")
            return
        try:
            wh = await ctx.guild.fetch_webhook(int(row["webhook_id"]))
            await wh.delete()
        except Exception:
            pass  # best-effort  -  remove from DB regardless
        await ctx.db.delete_mm_webhook(ctx.guild_id)
        await ctx.reply_success("MM webhook removed. Trades will fall back to normal bot messages.", title="Webhook Deleted")

    # ── Custom Webhooks ────────────────────────────────────────────────────

    @admin.group(name="webhook", invoke_without_command=True)
    @_require_manage_guild()
    async def admin_webhook(self, ctx: DiscoContext) -> None:
        """Custom webhook management. Subcommands: create, list, delete, send"""
        if await suggest_subcommand(ctx, self.admin_webhook):
            return
        await ctx.send_group_help(self.admin_webhook, title="🔗 Webhook Commands")

    @admin_webhook.command(name="create")
    @_require_manage_guild()
    async def admin_webhook_create(
        self, ctx: DiscoContext, name: str, channel: discord.TextChannel, avatar_url: str = ""
    ) -> None:
        """Create a custom webhook. Usage: .admin webhook create <name> #channel [avatar_url]"""
        try:
            wh = await channel.create_webhook(name=name)
            await ctx.db.execute(
                """INSERT INTO custom_webhooks (guild_id, name, webhook_id, webhook_token, channel_id, avatar_url)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (guild_id, name) DO UPDATE
                   SET webhook_id=EXCLUDED.webhook_id, webhook_token=EXCLUDED.webhook_token,
                       channel_id=EXCLUDED.channel_id, avatar_url=EXCLUDED.avatar_url""",
                ctx.guild_id, name, str(wh.id), wh.token, channel.id, avatar_url,
            )
            await ctx.reply_success(f"Webhook **{name}** created in {channel.mention}.", title="✅ Webhook Created")
        except discord.Forbidden:
            await ctx.reply_error("Missing Manage Webhooks permission in that channel.")
        except Exception as e:
            await ctx.reply_error(f"Failed: {e}")

    @admin_webhook.command(name="list")
    @_require_manage_guild()
    async def admin_webhook_list(self, ctx: DiscoContext) -> None:
        """List all custom webhooks for this server."""
        rows = await ctx.db.fetch_all(
            "SELECT name, channel_id FROM custom_webhooks WHERE guild_id=$1 ORDER BY name",
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply_error("No custom webhooks. Create one with `.admin webhook create`.")
            return
        lines = [f"• **{r['name']}** → <#{r['channel_id']}>" for r in rows]
        _b = card(f"🔗 Custom Webhooks ({len(rows)})", color=C_INFO)
        _b.description = "\n".join(lines)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_webhook.command(name="delete")
    @_require_manage_guild()
    async def admin_webhook_delete(self, ctx: DiscoContext, name: str) -> None:
        """Delete a custom webhook. Usage: .admin webhook delete <name>"""
        row = await ctx.db.fetch_one(
            "SELECT webhook_id FROM custom_webhooks WHERE guild_id=$1 AND name=$2",
            ctx.guild_id, name,
        )
        if not row:
            await ctx.reply_error(f"Webhook **{name}** not found.")
            return
        try:
            wh = await ctx.guild.fetch_webhook(int(row["webhook_id"]))
            await wh.delete()
        except Exception:
            pass
        await ctx.db.execute(
            "DELETE FROM custom_webhooks WHERE guild_id=$1 AND name=$2",
            ctx.guild_id, name,
        )
        await ctx.reply_success(f"Webhook **{name}** deleted.", title="✅ Webhook Deleted")

    @admin_webhook.command(name="send")
    @_require_manage_guild()
    async def admin_webhook_send(self, ctx: DiscoContext, name: str, *, message: str) -> None:
        """Send a message through a custom webhook. Usage: .admin webhook send <name> <message>"""
        row = await ctx.db.fetch_one(
            "SELECT webhook_id, webhook_token, avatar_url FROM custom_webhooks WHERE guild_id=$1 AND name=$2",
            ctx.guild_id, name,
        )
        if not row:
            await ctx.reply_error(f"Webhook **{name}** not found.")
            return
        try:
            wh = await ctx.guild.fetch_webhook(int(row["webhook_id"]))
            avatar = row.get("avatar_url") or None
            await wh.send(message, username=name, avatar_url=avatar)
            await ctx.reply_success(f"Message sent via **{name}**.", title="📤 Sent")
        except Exception as e:
            await ctx.reply_error(f"Failed to send: {e}")

    @admin.command(name="settings")
    @_require_manage_guild()
    async def admin_settings(self, ctx: DiscoContext) -> None:
        """Show all current settings: server config, modules, and global admin settings."""
        s = await ctx.db.get_guild_settings(ctx.guild_id)
        p = s.get("prefix") or Config.PREFIX
        color = s.get("embed_color") or C_INFO

        def _ch(col: str) -> str:
            cid = s.get(col)
            return f"<#{cid}>" if cid else "❌ Not set"

        def _mod(key: str) -> str:
            return "✅" if s.get(f"module_{key}", 1) else "❌"

        # ── Page 1: Identity + Channels + AI ─────────────────────────────────
        color_val = s.get("embed_color")
        cmd_del   = s.get("cmd_delete_after", 0) or 0
        rep_del   = s.get("reply_delete_after", 0) or 0
        _p1 = (
            card("⚙️ Server Settings (1/3)", color=color)
            .field("Prefix",      f"`{p}`",                                           True)
            .field("Currency",    s.get("currency_name") or "USD",                   True)
            .field("Server Name", s.get("server_name") or ctx.guild.name,            True)
            .field("Embed Color", f"`#{color_val:06x}`" if color_val else "default", True)
            .field("🗑 Cmd Auto-Delete",  f"{cmd_del}s" if cmd_del else "off",        True)
            .field("🗑 Reply Auto-Delete", f"{rep_del}s" if rep_del else "off",       True)
            .field("\u200b", "**📡 Feed Channels**", False)
        )
        for label, col in [
            ("🔄 Trade",         "trade_channel"),
            ("⛏️ Mining",        "mine_channel"),
            ("💎 Staking",       "staking_channel"),
            ("🔐 Validators",    "validators_channel"),
            ("🎲 Gambling",      "gambling_channel"),
            ("🌊 Pools",         "pools_channel"),
            ("📈 Crypto",        "crypto_channel"),
            ("💰 Drops (feed)",  "drops_channel"),
            ("💰 Drops (spawn)", "drops_spawn_channel"),
            ("💼 Jobs",          "job_channel"),
            ("📜 Contracts",     "contracts_channel"),
            ("🔑 DeFi Wallet",   "wallet_channel"),
            ("🚨 Error Log",     "error_channel"),
        ]:
            _p1.field(label, _ch(col), True)

        # AI
        ai_flags = await ctx.db.get_ai_flags(ctx.guild_id)
        key_set = bool(Config.OPENROUTER_API_KEY)
        flag_str = (
            f"{'✅' if key_set else '❌'} API Key  •  "
            + "  ".join(f"{'✅' if v else '❌'} `{k}`" for k, v in ai_flags.items())
        )
        _p1.field("🤖 AI / OpenRouter", flag_str, False)
        p1 = _p1.footer(f"Page 1/3  •  {p}admin setchannel <type> #channel").build()

        # ── Page 2: Modules + Halts ───────────────────────────────────────────
        _p2 = card("⚙️ Server Settings (2/3)", color=color)

        _MOD_ROWS = [
            ("gambling", "🎲"), ("lending", "🏦"), ("staking", "⚡"), ("mining", "⛏"),
            ("drops", "💰"), ("savings", "💵"), ("validators", "🔐"), ("pools", "🌊"),
            ("contracts", "📜"), ("groups", "👥"), ("chart", "📊"), ("crypto", "📈"),
            ("daily", "📅"), ("work", "💼"), ("economy", "💰"), ("chain", "🔗"),
        ]
        for name, emoji in _MOD_ROWS:
            _p2.field(f"{emoji} {name.title()}", _mod(name), True)

        # Halted networks
        halted = await ctx.db.get_halted_networks(ctx.guild_id)
        _p2.field(
            "🚫 Halted Networks",
            ", ".join(f"`{n.upper()}`" for n in halted) if halted else "None",
            False,
        )
        # Disabled tokens
        disabled = await ctx.db.get_disabled_tokens(ctx.guild_id)
        _p2.field(
            "🚫 Disabled Tokens",
            ", ".join(f"`{t}`" for t in disabled) if disabled else "None",
            False,
        )
        p2 = _p2.footer(f"Page 2/3  •  {p}admin module <name> on|off  •  {p}admin halt network/token").build()

        # ── Page 3: Global Admin Settings ─────────────────────────────────────
        _p3 = card("⚙️ Global Admin Settings (3/3)", color=color)

        _p3.field(
            "💰 Economy",
            f"Starting balance: **{fmt_usd(to_human(Config.STARTING_BALANCE))}**\n"
            f"Daily amount: **{fmt_usd(to_human(Config.DAILY_AMOUNT))}** "
            f"(streak +{fmt_usd(to_human(Config.DAILY_STREAK_BONUS))}/day, max {Config.DAILY_MAX_STREAK})\n"
            f"Work cooldown: **{Config.WORK_COOLDOWN // 60}** min\n"
            f"Max leverage: **{Config.MAX_LEVERAGE}x**",
            False,
        )
        _p3.field(
            "💰 Drops",
            f"Interval: **{Config.AUTO_DROP_INTERVAL // 60}** min\n"
            f"Range: **${Config.DROP_MIN:,.0f}**  -  **${Config.DROP_MAX:,.0f}**\n"
            f"Collect window: **{Config.DROP_COLLECT_WINDOW}s**",
            True,
        )
        _p3.field(
            "🐋 Alerts",
            f"Whale threshold: **${to_human(Config.WHALE_ALERT_THRESHOLD_USD):,.0f}**\n"
            f"Chain block interval: **{Config.CHAIN_BLOCK_INTERVAL // 60}** min",
            True,
        )
        _p3.field(
            "🌊 Pools & Liquidity",
            f"Max swap fraction: **{Config.MAX_SWAP_FRACTION * 100:.0f}%**\n"
            f"Hourly swap limit: **${Config.USER_SWAP_HOURLY_LIMIT_USD:,.0f}**\n"
            f"Fee burn: **{Config.FEE_BURN_FRACTION * 100:.0f}%**\n"
            f"LP lock: **{Config.LP_LOCK_SECONDS // 3600}h** · Max concentration: **{Config.LP_MAX_CONCENTRATION * 100:.0f}%**",
            False,
        )
        _p3.field(
            "💸 Wallet Fees",
            f"Platform fee: **{Config.WALLET_PLATFORM_FEE_PCT * 100:.1f}%**\n"
            f"Range: **${Config.WALLET_PLATFORM_FEE_MIN:.2f}**  -  **${Config.WALLET_PLATFORM_FEE_MAX:.2f}**",
            True,
        )
        _p3.field(
            "🤖 Anti-Bot",
            f"CAPTCHA trigger: **{Config.ANTIBOT_MIN_GAMES}** - **{Config.ANTIBOT_MAX_GAMES}** games\n"
            f"Price tick: **{Config.PRICE_TICK_SECONDS}s**",
            True,
        )

        # Security system summary
        sec_status = "Not loaded"
        sec_engine = getattr(self.bot, "security_engine", None)
        if sec_engine:
            health = sec_engine.get_health()
            sec_status = (
                f"{'Running' if health.engine_running else 'Stopped'} · "
                f"**{health.detectors_active}** detectors · "
                f"**{health.events_processed_total:,}** events processed"
            )
        # Scam detection status
        scam_enabled = bool(s.get("scam_detection"))
        scam_str = "**enabled**" if scam_enabled else "disabled"

        _p3.field(
            "🛡 Security & Scam",
            f"{sec_status}\n"
            f"Scam detection: {scam_str}\n"
            f"`{p}security settings`  -  thresholds · `{p}security scam`  -  scam config",
            False,
        )

        _p3.field(
            "🔧 Infrastructure",
            f"Debug: **{'ON' if Config.DEBUG else 'OFF'}**\n"
            f"API port: **{Config.API_PORT}**\n"
            f"Dashboard: **{Config.DASHBOARD_URL or 'not set'}**\n"
            f"Redis: **{'configured' if Config.REDIS_URL else 'not set'}**\n"
            f"Backups: every **{Config.BACKUP_INTERVAL_HOURS}h**, keep **{Config.BACKUP_KEEP}**",
            False,
        )

        p3 = _p3.footer(f"Page 3/3  •  Global settings are controlled via env vars / .env file").build()

        await ctx.paginate([p1, p2, p3])

    # ── Halt management ───────────────────────────────────────────────────────

    @admin.group(name="halt", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_halt(self, ctx: DiscoContext) -> None:
        """Halt networks or disable tokens. Usage: .admin halt <network|token|list>"""
        if await suggest_subcommand(ctx, self.admin_halt):
            return
        halted  = await ctx.db.get_halted_networks(ctx.guild_id)
        disabled = await ctx.db.get_disabled_tokens(ctx.guild_id)
        embed = (
            card("🚫 Active Halts", color=C_ERROR)
            .field("Halted Networks", ", ".join(f"`{n.upper()}`" for n in halted) if halted else "None", False)
            .field("Disabled Tokens", ", ".join(f"`{t}`" for t in disabled) if disabled else "None", False)
            .footer(".admin halt network <net> on|off    .admin halt token <SYMBOL> on|off")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin_halt.command(name="network")
    @_require_manage_guild()
    async def admin_halt_network(self, ctx: DiscoContext, network: str, state: str = "on") -> None:
        """Halt or resume all transactions on a network. Usage: .admin halt network <network> [on|off]
        on/halt = block all txns  |  off/resume = allow txns"""
        net = network.lower()
        _VALID = {"arc", "sol", "bnb", "sun", "mta", "avax", "pol", "atom", "sui", "apt", "near"}
        if net not in _VALID:
            await ctx.reply_error(f"Unknown network `{net}`. Valid: {', '.join(sorted(_VALID))}")
            return
        halt = state.lower() in ("on", "halt", "1", "true", "yes")
        if halt:
            await ctx.db.halt_network(ctx.guild_id, net)
            await ctx.reply_success(
                f"All transactions on **{net.upper()}** are now **halted**.\n"
                "New buy/sell/swap/stake/contract actions will be rejected until resumed.",
                title="🚫 Network Halted",
            )
        else:
            await ctx.db.unhalt_network(ctx.guild_id, net)
            await ctx.reply_success(
                f"**{net.upper()}** network transactions are now **resumed**.",
                title="✅ Network Resumed",
            )

    @admin_halt.command(name="token")
    @_require_manage_guild()
    async def admin_halt_token(self, ctx: DiscoContext, symbol: str, state: str = "on") -> None:
        """Disable or re-enable trading of a specific token. Usage: .admin halt token <SYMBOL> [on|off]
        on/halt = disable token  |  off/resume = re-enable token"""
        sym = symbol.upper()
        disable = state.lower() in ("on", "halt", "disable", "1", "true", "yes")
        if disable:
            await ctx.db.disable_token(ctx.guild_id, sym)
            await ctx.reply_success(
                f"Token **{sym}** is now **disabled**  -  buy/sell/swap will be rejected.",
                title="🚫 Token Disabled",
            )
        else:
            await ctx.db.enable_token(ctx.guild_id, sym)
            await ctx.reply_success(
                f"Token **{sym}** is now **re-enabled**.",
                title="✅ Token Enabled",
            )

    @admin_halt.command(name="list")
    @_require_manage_guild()
    async def admin_halt_list(self, ctx: DiscoContext) -> None:
        """List all active network halts and disabled tokens."""
        await self.admin_halt(ctx)

    # ── Group token trading control ───────────────────────────────────────────

    @admin.group(name="grouptoken", aliases=["gt"], invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_grouptoken(self, ctx: DiscoContext) -> None:
        """Manage group token trading. Usage: .admin grouptoken <list|enable|disable|enableall|disableall>"""
        if await suggest_subcommand(ctx, self.admin_grouptoken):
            return
        await ctx.invoke(self.admin_grouptoken_list)

    @admin_grouptoken.command(name="list", aliases=["ls"])
    @_require_manage_guild()
    async def admin_grouptoken_list(self, ctx: DiscoContext) -> None:
        """List all group tokens and their trading status."""
        rows = await ctx.db.get_group_tokens(ctx.guild_id)
        if not rows:
            await ctx.reply_error("No group tokens exist on this server yet.")
            return
        lines = []
        for r in rows:
            icon = "✅" if r.get("trading_enabled") else "🔒"
            grp_name = r.get("group_name") or "?"
            circ = float(r.get("circulating_supply") or 0)
            lines.append(f"{icon} **{r['symbol']}** - {grp_name}  |  supply: `{circ:,.2f}`")
        embed = (
            card("⛏️ Group Tokens", color=C_NEUTRAL)
            .description("\n".join(lines))
            .footer("✅ = trading enabled  |  🔒 = locked  |  .admin grouptoken enable <SYM> to unlock")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin_grouptoken.command(name="enable")
    @_require_manage_guild()
    async def admin_grouptoken_enable(self, ctx: DiscoContext, symbol: str) -> None:
        """Enable player trading for a group token. Usage: .admin grouptoken enable <SYMBOL>"""
        sym = symbol.upper()
        row = await ctx.db.fetch_one(
            "SELECT token_type FROM guild_tokens WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, sym,
        )
        if not row:
            await ctx.reply_error(f"Token **{sym}** not found.")
            return
        if row.get("token_type") != "group":
            await ctx.reply_error(f"**{sym}** is not a group token.")
            return
        await ctx.db.enable_group_token_trading(ctx.guild_id, sym)
        await ctx.reply_success(
            f"Token **{sym}** is now **tradeable** - players can buy/sell/swap it.",
            title="✅ Group Token Enabled",
        )

    @admin_grouptoken.command(name="disable")
    @_require_manage_guild()
    async def admin_grouptoken_disable(self, ctx: DiscoContext, symbol: str) -> None:
        """Disable player trading for a group token. Usage: .admin grouptoken disable <SYMBOL>"""
        sym = symbol.upper()
        row = await ctx.db.fetch_one(
            "SELECT token_type FROM guild_tokens WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, sym,
        )
        if not row:
            await ctx.reply_error(f"Token **{sym}** not found.")
            return
        if row.get("token_type") != "group":
            await ctx.reply_error(f"**{sym}** is not a group token.")
            return
        await ctx.db.disable_group_token_trading(ctx.guild_id, sym)
        await ctx.reply_success(
            f"Token **{sym}** is now **locked** - buy/sell/swap are blocked.",
            title="🔒 Group Token Disabled",
        )

    @admin_grouptoken.command(name="enableall")
    @_require_manage_guild()
    async def admin_grouptoken_enableall(self, ctx: DiscoContext) -> None:
        """Enable trading for ALL group tokens on this server."""
        rows = await ctx.db.get_group_tokens(ctx.guild_id)
        if not rows:
            await ctx.reply_error("No group tokens exist.")
            return
        for r in rows:
            await ctx.db.enable_group_token_trading(ctx.guild_id, r["symbol"])
        syms = ", ".join(f"**{r['symbol']}**" for r in rows)
        await ctx.reply_success(f"Enabled trading for: {syms}", title="✅ All Group Tokens Enabled")

    @admin_grouptoken.command(name="disableall")
    @_require_manage_guild()
    async def admin_grouptoken_disableall(self, ctx: DiscoContext) -> None:
        """Disable trading for ALL group tokens on this server."""
        rows = await ctx.db.get_group_tokens(ctx.guild_id)
        if not rows:
            await ctx.reply_error("No group tokens exist.")
            return
        for r in rows:
            await ctx.db.disable_group_token_trading(ctx.guild_id, r["symbol"])
        syms = ", ".join(f"**{r['symbol']}**" for r in rows)
        await ctx.reply_success(f"Locked trading for: {syms}", title="🔒 All Group Tokens Disabled")

    # ── Network rebind for an existing group token ──────────────────────────
    # Map admin-friendly aliases to (canonical network name, network coin)
    _ADMIN_POW_NETWORK_ALIASES: dict = {
        "sun":     ("Sun Network",     "SUN"),
        "mta":     ("Moneta Chain", "MTA"),
        "moneta": ("Moneta Chain", "MTA"),
    }

    @admin_grouptoken.command(name="network", aliases=["net", "rebind"])
    @_require_manage_guild()
    async def admin_grouptoken_network(
        self, ctx: DiscoContext, symbol: str, network: str
    ) -> None:
        """Re-bind a group token to a different PoW network. Usage: .admin grouptoken network <SYM> <sun|mta>

        This will:
          - drain the existing vault pool back into the group's vault balance
          - delete the old vault pool
          - point the group's token at the new network
          - create a fresh vault pool on the new network using current prices
        """
        sym = symbol.upper().strip()
        net_arg = network.lower().strip()

        entry = self._ADMIN_POW_NETWORK_ALIASES.get(net_arg)
        if not entry:
            valid = ", ".join(f"`{k}`" for k in self._ADMIN_POW_NETWORK_ALIASES)
            await ctx.reply_error(f"Unknown network `{network}`. Valid: {valid}")
            return
        new_net_name, new_net_coin = entry

        # Locate the group that owns this token
        grp_row = await ctx.db.fetch_one(
            "SELECT * FROM mining_groups WHERE guild_id=$1 AND UPPER(token_symbol)=$2",
            ctx.guild_id, sym,
        )
        if not grp_row:
            await ctx.reply_error(f"No mining group on this server owns token **{sym}**.")
            return

        old_net_name = grp_row.get("token_network") or ""
        old_net_coin = Config.NETWORK_COINS.get(old_net_name, "") if old_net_name else ""

        if old_net_name == new_net_name:
            await ctx.reply_error(
                f"**{sym}** is already bound to **{new_net_name}**. Nothing to do."
            )
            return

        # Drain and delete the old vault pool (if one exists) so its reserves
        # don't get stranded. The pool id was built from the WRAPPED symbol
        # (MMTA / MSUN) post-migration-0118; fall back to the raw coin only
        # for guilds that somehow missed the migration.
        from constants.moons import wrapped_coin as _wrapped_coin_old
        coin_credit_label = ""
        coin_credit_value = 0.0
        if old_net_coin:
            old_wrapped = _wrapped_coin_old(old_net_coin)
            old_pool_id, _, _ = ctx.db.make_pool_id(sym, old_wrapped)
            if not await ctx.db.get_pool(old_pool_id, ctx.guild_id):
                # Legacy fallback: pre-wrapped pools used the raw coin.
                old_pool_id, _, _ = ctx.db.make_pool_id(sym, old_net_coin)
            old_pool = await ctx.db.get_pool(old_pool_id, ctx.guild_id)
            if old_pool:
                ra_h = old_pool.h("reserve_a")
                rb_h = old_pool.h("reserve_b")
                tok_side, coin_side = (ra_h, rb_h) if old_pool["token_a"] == sym else (rb_h, ra_h)

                # Return the token side to the vault balance.
                if tok_side > 0:
                    await ctx.db.mint_vault_tokens(
                        ctx.guild_id, grp_row["group_id"], float(tok_side),
                    )

                # Credit the network-coin side back to the group's reserves so
                # no value is destroyed when the pool is dropped.
                #   - MTA -> reserve_btc bucket
                #   - anything else (e.g. SUN) -> converted to USD at the
                #     current oracle price and added to reserve_usd, since no
                #     dedicated reserve bucket exists for non-MTA PoW coins.
                if coin_side > 0:
                    if old_net_coin == "MTA":
                        await ctx.db.add_group_reserve_btc(
                            ctx.guild_id, grp_row["group_id"], float(coin_side),
                        )
                        coin_credit_label = f"+{coin_side:,.8f} {old_net_coin} -> reserve_btc"
                        coin_credit_value = float(coin_side)
                    else:
                        coin_price_row = await ctx.db.get_price(old_net_coin, ctx.guild_id)
                        coin_price = float(coin_price_row["price"]) if coin_price_row else 0.0
                        usd_credit = coin_side * coin_price
                        if usd_credit > 0:
                            await ctx.db.add_group_reserve_usd(
                                ctx.guild_id, grp_row["group_id"], float(usd_credit),
                            )
                        coin_credit_label = (
                            f"+{coin_side:,.8f} {old_net_coin} ({fmt_usd(usd_credit)}) "
                            f"-> reserve_usd"
                        )
                        coin_credit_value = float(usd_credit)

                log.info(
                    "[admin_grouptoken_network] drained %s/%s pool: tok=%s coin=%s -> "
                    "vault credit %s",
                    sym, old_net_coin, tok_side, coin_side, coin_credit_label or "(none)",
                )
                await ctx.db.execute(
                    "DELETE FROM lp_positions WHERE guild_id=$1 AND pool_id=$2",
                    ctx.guild_id, old_pool_id,
                )
                await ctx.db.execute(
                    "DELETE FROM pools WHERE guild_id=$1 AND pool_id=$2",
                    ctx.guild_id, old_pool_id,
                )

        # Rebind the group's mining chain. The token's ``network`` column stays
        # on the bridged ``"Moon Network"`` pseudo-network so cross-group
        # swaps keep working; only the vault pair / block-reward coin changes.
        await ctx.db.set_group_token_network(ctx.guild_id, grp_row["group_id"], sym, new_net_name)
        await ctx.db.execute(
            "UPDATE guild_tokens SET vault_locked=FALSE, trading_enabled=TRUE, network='Moon Network' "
            "WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, sym,
        )

        # Seed the new vault pool with current prices. The pool pairs the
        # group token with the Moon-Network WRAPPED coin (MMTA / MSUN), not
        # the raw mining coin -- matches .group token network behavior.
        from constants.moons import wrapped_coin as _wrapped_coin
        wrapped_new = _wrapped_coin(new_net_coin)
        tok_price_row = await ctx.db.get_price(sym, ctx.guild_id)
        wrapped_price_row = await ctx.db.get_price(wrapped_new, ctx.guild_id)
        tok_price = float(tok_price_row["price"]) if tok_price_row else 0.01
        wrapped_price = float(wrapped_price_row["price"]) if wrapped_price_row else 0.10
        await ctx.db.create_vault_pool(
            ctx.guild_id, sym, wrapped_new, tok_price, wrapped_price,
        )

        embed = (
            card("🔄 Group Token Mining Chain Rebind", color=C_SUCCESS)
            .field("Token", f"**{sym}**", True)
            .field("Trades on", "Moon Network (bridged)", True)
            .field("Old Chain", f"{old_net_name or '_unset_'}", True)
            .field("New Chain", f"**{new_net_name}** ({new_net_coin})", True)
            .field("Vault Pool", f"`{sym}/{wrapped_new}` created", False)
            .field_if(
                bool(coin_credit_label),
                "Old Pool Drained",
                coin_credit_label or "_no coin side_",
                False,
            )
            .footer(
                "Token side returned to vault_token_bal; coin side credited to "
                "the group's reserve buckets."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── Auto-delete settings ──────────────────────────────────────────────────

    @admin.group(name="autodelete", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_autodelete(self, ctx: DiscoContext) -> None:
        """Configure auto-deletion of commands and bot replies."""
        if await suggest_subcommand(ctx, self.admin_autodelete):
            return
        s = await ctx.db.get_guild_settings(ctx.guild_id)
        cmd_d    = s.get("cmd_delete_after", 0) or 0
        rep_d    = s.get("reply_delete_after", 0) or 0
        ai_cmd_d = s.get("ai_cmd_delete_after", 0) or 0
        ai_rep_d = s.get("ai_reply_delete_after", 0) or 0
        embed = (
            card("🗑 Auto-Delete Settings", color=C_NEUTRAL)
            .field("Command Messages",  f"**{cmd_d}s**" if cmd_d else "**off**",     True)
            .field("Bot Replies",       f"**{rep_d}s**" if rep_d else "**off**",     True)
            .field("AI Commands (.ask)", f"**{ai_cmd_d}s**" if ai_cmd_d else "**off**", True)
            .field("AI Replies",         f"**{ai_rep_d}s**" if ai_rep_d else "**off**", True)
            .footer(
                ".admin autodelete commands <s|off>  ·  .admin autodelete replies <s|off>\n"
                ".admin autodelete aicommands <s|off>  ·  .admin autodelete aireplies <s|off>"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, delete_after=None)

    @admin_autodelete.command(name="commands")
    @_require_manage_guild()
    async def admin_autodelete_commands(self, ctx: DiscoContext, duration: str) -> None:
        """Set how long player command messages stay visible. Usage: .admin autodelete commands <seconds|off>"""
        if duration.lower() in ("off", "0", "none", "disable"):
            await ctx.db.update_guild_setting(ctx.guild_id, "cmd_delete_after", 0)
            await ctx.reply_success("Command auto-delete is now **off**.", title="Auto-Delete Updated", delete_after=None)
        else:
            try:
                secs = int(duration)
                if secs < 1 or secs > 3600:
                    raise ValueError
            except ValueError:
                await ctx.reply_error("Duration must be a number of seconds (1 - 3600) or `off`.", delete_after=None)
                return
            await ctx.db.update_guild_setting(ctx.guild_id, "cmd_delete_after", secs)
            await ctx.reply_success(
                f"Command messages will be deleted after **{secs}s**.",
                title="Auto-Delete Updated",
                delete_after=None,
            )

    @admin_autodelete.command(name="replies")
    @_require_manage_guild()
    async def admin_autodelete_replies(self, ctx: DiscoContext, duration: str) -> None:
        """Set how long bot reply messages stay visible. Usage: .admin autodelete replies <seconds|off>"""
        if duration.lower() in ("off", "0", "none", "disable"):
            await ctx.db.update_guild_setting(ctx.guild_id, "reply_delete_after", 0)
            await ctx.reply_success("Reply auto-delete is now **off**.", title="Auto-Delete Updated", delete_after=None)
        else:
            try:
                secs = int(duration)
                if secs < 1 or secs > 3600:
                    raise ValueError
            except ValueError:
                await ctx.reply_error("Duration must be a number of seconds (1 - 3600) or `off`.", delete_after=None)
                return
            await ctx.db.update_guild_setting(ctx.guild_id, "reply_delete_after", secs)
            await ctx.reply_success(
                f"Bot replies will be deleted after **{secs}s**.",
                title="Auto-Delete Updated",
                delete_after=None,
            )

    @admin_autodelete.command(name="aicommands")
    @_require_manage_guild()
    async def admin_autodelete_ai_commands(self, ctx: DiscoContext, duration: str) -> None:
        """Set how long .ask command messages stay visible (independent of global commands setting)."""
        if duration.lower() in ("off", "0", "none", "disable"):
            await ctx.db.update_guild_setting(ctx.guild_id, "ai_cmd_delete_after", 0)
            await ctx.reply_success("AI command auto-delete is now **off**.", title="Auto-Delete Updated", delete_after=None)
        else:
            try:
                secs = int(duration)
                if secs < 1 or secs > 3600:
                    raise ValueError
            except ValueError:
                await ctx.reply_error("Duration must be a number of seconds (1 - 3600) or `off`.", delete_after=None)
                return
            await ctx.db.update_guild_setting(ctx.guild_id, "ai_cmd_delete_after", secs)
            await ctx.reply_success(
                f"`.ask` command messages will be deleted after **{secs}s**.",
                title="Auto-Delete Updated",
                delete_after=None,
            )

    @admin_autodelete.command(name="aireplies")
    @_require_manage_guild()
    async def admin_autodelete_ai_replies(self, ctx: DiscoContext, duration: str) -> None:
        """Set how long AI bot replies stay visible (independent of global replies setting)."""
        if duration.lower() in ("off", "0", "none", "disable"):
            await ctx.db.update_guild_setting(ctx.guild_id, "ai_reply_delete_after", 0)
            await ctx.reply_success("AI reply auto-delete is now **off**.", title="Auto-Delete Updated", delete_after=None)
        else:
            try:
                secs = int(duration)
                if secs < 1 or secs > 3600:
                    raise ValueError
            except ValueError:
                await ctx.reply_error("Duration must be a number of seconds (1 - 3600) or `off`.", delete_after=None)
                return
            await ctx.db.update_guild_setting(ctx.guild_id, "ai_reply_delete_after", secs)
            await ctx.reply_success(
                f"AI replies will be deleted after **{secs}s**.",
                title="Auto-Delete Updated",
                delete_after=None,
            )

    # ── Chain / Mining admin ──────────────────────────────────────────────────

    @admin.group(name="chain", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_chain(self, ctx: DiscoContext) -> None:
        """Mining chain management. Usage: .admin chain <info|set|reset>"""
        if await suggest_subcommand(ctx, self.admin_chain):
            return
        # Default: show all chain info
        lines = []
        for symbol, cfg in Config.POW_NETWORKS.items():
            net = await ctx.db.mining.get_pow_network(ctx.guild_id, symbol)
            if not net:
                continue
            height = net["block_height"]
            diff = net.get("difficulty") or cfg["initial_difficulty"]
            hr = net.get("total_hashrate", 0)
            reward = float(net.get("current_reward", 0))
            warmup = cfg.get("warmup_blocks", 0)
            solo_cap = cfg.get("solo_share_cap", 1.0)
            lines.append(
                f"**{cfg.get('emoji', '')} {symbol}**\n"
                f"> Block `#{height:,}` · Difficulty `{diff:,.0f}` · Hashrate `{hr:,.0f} MH/s`\n"
                f"> Reward `{reward:.4f}` · Warmup `{warmup}` blocks (cubic) · Solo cap `{solo_cap*100:.0f}%`"
            )
        embed = card(
            "⛏ Chain Status",
            description="\n\n".join(lines) if lines else "No PoW networks found.",
            color=C_GOLD,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @admin_chain.command(name="set")
    @_require_manage_guild()
    async def admin_chain_set(self, ctx: DiscoContext, chain: str, key: str, value: str) -> None:
        """Set a chain config value. Usage: .admin chain set <SUN|MTA> <key> <value>
        Keys: warmup_blocks, solo_share_cap, initial_difficulty, initial_reward, electricity_rate"""
        chain = chain.upper()
        cfg = Config.POW_NETWORKS.get(chain)
        if not cfg:
            await ctx.reply_error(f"Unknown chain `{chain}`. Valid: {', '.join(Config.POW_NETWORKS)}")
            return
        allowed = {"warmup_blocks", "solo_share_cap", "initial_difficulty", "initial_reward",
                    "electricity_rate", "electricity_scaling", "target_block_time", "max_group_share"}
        if key not in allowed:
            await ctx.reply_error(f"Unknown key `{key}`. Valid: {', '.join(sorted(allowed))}")
            return
        try:
            parsed = int(value) if key in ("warmup_blocks", "target_block_time") else float(value)
        except ValueError:
            await ctx.reply_error("Value must be a number.")
            return
        cfg[key] = parsed
        await ctx.reply_success(f"**{chain}** `{key}` set to **{parsed}**.", title="Chain Config Updated")

    @admin_chain.command(name="reset")
    @_require_manage_guild()
    async def admin_chain_reset(self, ctx: DiscoContext, chain: str) -> None:
        """Reset a chain to block 0. Resets difficulty, height, supply tracking.
        Does NOT reset player balances. Usage: .admin chain reset <SUN|MTA>"""
        chain = chain.upper()
        cfg = Config.POW_NETWORKS.get(chain)
        if not cfg:
            await ctx.reply_error(f"Unknown chain `{chain}`. Valid: {', '.join(Config.POW_NETWORKS)}")
            return
        confirmed = await ctx.confirm(
            f"Reset **{chain}** to block 0?\n"
            f"This resets block height, difficulty, and circulating supply tracking.\n"
            f"Player balances are **NOT** affected."
        )
        if not confirmed:
            await ctx.reply_error("Reset cancelled.")
            return
        # Reset pow_network_state
        await ctx.db.execute(
            """UPDATE pow_network_state
               SET block_height = 0, total_hashrate = 0, current_reward = $3,
                   difficulty = $4, last_block_ts = now(), last_retarget_height = 0,
                   last_retarget_ts = now()
               WHERE guild_id = $1 AND chain_symbol = $2""",
            ctx.guild_id, chain, cfg.get("initial_reward", 1.0), cfg.get("initial_difficulty", 60000.0),
        )
        # Reset circulating supply to initial value (50% of max_supply)
        from core.config import Config as _Cfg
        token_cfg = _Cfg.TOKENS.get(chain, {})
        max_sup = token_cfg.get("max_supply", 0)
        initial_supply = max_sup * 0.5 if max_sup else 0.0
        initial_supply_raw = to_raw(initial_supply)
        await ctx.db.execute(
            "UPDATE crypto_prices SET circulating_supply = $3 WHERE guild_id = $1 AND symbol = $2",
            ctx.guild_id, chain, initial_supply_raw,
        )
        await ctx.db.execute(
            "UPDATE guild_tokens SET circulating_supply = $3 WHERE guild_id = $1 AND symbol = $2",
            ctx.guild_id, chain, initial_supply_raw,
        )
        # Clear mined chain blocks
        await ctx.db.execute(
            "DELETE FROM chain_blocks WHERE guild_id = $1 AND network = $2",
            ctx.guild_id, chain.lower(),
        )
        await ctx.reply_success(
            f"**{chain}** reset to block 0.\n"
            f"Difficulty: `{cfg.get('initial_difficulty', 60000.0):,.0f}`\n"
            f"Supply: `{initial_supply:,.2f}` (initial). Player balances untouched.",
            title="Chain Reset",
        )

    # ── Supply management ──────────────────────────────────────────────────

    @admin.group(name="supply", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_supply(self, ctx: DiscoContext) -> None:
        """Check or reset token supply. Usage: .admin supply [check|reset <token>]"""
        if await suggest_subcommand(ctx, self.admin_supply):
            return
        await self.admin_supply_check(ctx)

    @admin_supply.command(name="check")
    @_require_manage_guild()
    async def admin_supply_check(self, ctx: DiscoContext, token: str = "") -> None:
        """Show circulating supply and max supply for all or one token.
        Usage: .admin supply check [token]"""
        rows = await ctx.db.fetch_all(
            "SELECT symbol, circulating_supply FROM crypto_prices WHERE guild_id = $1 ORDER BY symbol",
            ctx.guild_id,
        )
        gt_rows = await ctx.db.fetch_all(
            "SELECT symbol, circulating_supply, max_supply FROM guild_tokens WHERE guild_id = $1 ORDER BY symbol",
            ctx.guild_id,
        )
        gt_map = {r["symbol"]: r for r in gt_rows}

        lines = []
        for r in rows:
            sym = r["symbol"]
            if token and sym.upper() != token.upper():
                continue
            circ = r.h("circulating_supply")
            cfg_tok = Config.TOKENS.get(sym, {})
            if sym in gt_map and gt_map[sym].get("max_supply"):
                max_sup = gt_map[sym].h("max_supply")
            else:
                max_sup = cfg_tok.get("max_supply")
            max_str = f"{max_sup:,.2f}" if max_sup else "unlimited"
            pct = f" ({circ/max_sup*100:.1f}%)" if max_sup and max_sup > 0 else ""
            lines.append(f"**{sym}**: `{circ:,.4f}` / `{max_str}`{pct}")

        embed = card(
            "📊 Token Supply",
            description="\n".join(lines) if lines else "No tokens found.",
            color=C_NAVY,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @admin_supply.command(name="reset")
    @_require_manage_guild()
    async def admin_supply_reset(self, ctx: DiscoContext, token: str) -> None:
        """Reset circulating supply for a token AND wipe all player balances of that token.
        Supply resets to initial value (50% of max_supply), not zero.
        Usage: .admin supply reset <token>"""
        token = token.upper()
        token_cfg = Config.TOKENS.get(token, {})
        max_sup = token_cfg.get("max_supply", 0)
        initial_supply = max_sup * 0.5 if max_sup else 0.0
        confirmed = await ctx.confirm(
            f"Reset **{token}** supply?\n"
            f"Circulating supply → **{initial_supply:,.0f}** (50% of max {max_sup:,.0f})\n"
            f"**All player balances** of {token} will be wiped.\n"
            f"This action cannot be undone."
        )
        if not confirmed:
            await ctx.reply_error("Reset cancelled.")
            return
        # Reset circulating supply to initial value (stored as raw NUMERIC(36,0))
        initial_supply_raw = to_raw(initial_supply)
        await ctx.db.execute(
            "UPDATE crypto_prices SET circulating_supply = $3 WHERE guild_id = $1 AND symbol = $2",
            ctx.guild_id, token, initial_supply_raw,
        )
        await ctx.db.execute(
            "UPDATE guild_tokens SET circulating_supply = $3 WHERE guild_id = $1 AND symbol = $2",
            ctx.guild_id, token, initial_supply_raw,
        )
        # Wipe all player balances of this token
        await ctx.db.execute(
            "DELETE FROM crypto_holdings WHERE guild_id = $1 AND symbol = $2",
            ctx.guild_id, token,
        )
        await ctx.db.execute(
            "DELETE FROM wallet_holdings WHERE guild_id = $1 AND symbol = $2",
            ctx.guild_id, token,
        )
        # Wipe stakes of this token
        await ctx.db.execute(
            "DELETE FROM stakes WHERE guild_id = $1 AND symbol = $2",
            ctx.guild_id, token,
        )
        await ctx.reply_success(
            f"**{token}** supply reset to **{initial_supply:,.0f}** (initial).\n"
            f"All player holdings, wallet holdings, and stakes of {token} wiped.",
            title="Supply Reset",
        )

    @admin_supply.command(name="recalculate", aliases=["recalc"])
    @_require_manage_guild()
    async def admin_supply_recalculate(self, ctx: DiscoContext) -> None:
        """Recalculate circulating supply from actual player holdings for ALL tokens.
        Does NOT change any balances  -  just fixes the supply tracker.
        Usage: .admin supply recalculate"""
        lines = []
        for symbol, token_cfg in Config.TOKENS.items():
            max_supply = token_cfg.get("max_supply", 0)
            cefi = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(amount), 0) FROM crypto_holdings WHERE guild_id = $1 AND symbol = $2",
                ctx.guild_id, symbol,
            )
            defi = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(amount), 0) FROM wallet_holdings WHERE guild_id = $1 AND symbol = $2",
                ctx.guild_id, symbol,
            )
            staked = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(amount), 0) FROM stakes WHERE guild_id = $1 AND symbol = $2",
                ctx.guild_id, symbol,
            )
            # Pool reserves (tokens locked in AMM pools as reserve_a or reserve_b)
            pool_a = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(reserve_a), 0) FROM pools WHERE guild_id = $1 AND token_a = $2",
                ctx.guild_id, symbol,
            )
            pool_b = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(reserve_b), 0) FROM pools WHERE guild_id = $1 AND token_b = $2",
                ctx.guild_id, symbol,
            )
            # All DB amounts are raw NUMERIC(36,0) - convert to human for comparison
            in_pools_h = to_human(int(float(pool_a or 0))) + to_human(int(float(pool_b or 0)))
            player_held_h = (
                to_human(int(float(cefi or 0)))
                + to_human(int(float(defi or 0)))
                + to_human(int(float(staked or 0)))
            )
            circulating_h = player_held_h + in_pools_h
            if max_supply > 0:
                circulating_h = min(circulating_h, max_supply)
            circulating_raw = to_raw(circulating_h)
            await ctx.db.execute(
                "UPDATE crypto_prices SET circulating_supply = $1 WHERE guild_id = $2 AND symbol = $3",
                circulating_raw, ctx.guild_id, symbol,
            )
            await ctx.db.execute(
                "UPDATE guild_tokens SET circulating_supply = $1 WHERE guild_id = $2 AND symbol = $3",
                circulating_raw, ctx.guild_id, symbol,
            )
            pct = f" ({circulating_h/max_supply*100:.1f}%)" if max_supply > 0 else ""
            pool_note = f" (pools: {in_pools_h:,.4f})" if in_pools_h > 0 else ""
            lines.append(f"**{symbol}**: `{circulating_h:,.4f}` / `{max_supply:,}`{pct}{pool_note}")
        embed = card(
            "📊 Supply Recalculated",
            description="\n".join(lines) if lines else "No tokens.",
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @admin_chain.command(name="resetall")
    @_require_manage_guild()
    async def admin_chain_resetall(self, ctx: DiscoContext) -> None:
        """Reset ALL chains to block 0 and recalculate supply from player holdings.
        Player balances are NOT touched. Usage: .admin chain resetall"""
        confirmed = await ctx.confirm(
            "**Full chain reset.**\n"
            "This resets ALL PoW chains to block 0, deletes mined block history, "
            "and recalculates circulating supply from actual player holdings.\n\n"
            "Player balances are **NOT** affected."
        )
        if not confirmed:
            await ctx.reply_error("Reset cancelled.")
            return
        # Reset all pow networks
        for symbol, cfg in Config.POW_NETWORKS.items():
            await ctx.db.execute(
                """UPDATE pow_network_state
                   SET block_height = 0, total_hashrate = 0, current_reward = $3,
                       difficulty = $4, last_block_ts = now(), last_retarget_height = 0,
                       last_retarget_ts = now()
                   WHERE guild_id = $1 AND chain_symbol = $2""",
                ctx.guild_id, symbol,
                cfg.get("initial_reward", 1.0),
                cfg.get("initial_difficulty", 60000.0),
            )
        # Reset legacy mining_network
        await ctx.db.execute(
            "UPDATE mining_network SET block_height = 0, total_hashrate = 0, current_reward = 50.0, "
            "last_block_ts = now() WHERE guild_id = $1",
            ctx.guild_id,
        )
        # Delete block history
        await ctx.db.execute("DELETE FROM chain_blocks WHERE guild_id = $1", ctx.guild_id)
        try:
            await ctx.db.execute("DELETE FROM mining_blocks WHERE guild_id = $1", ctx.guild_id)
        except Exception:
            pass
        # Recalculate supply
        lines = []
        for symbol, token_cfg in Config.TOKENS.items():
            max_supply = token_cfg.get("max_supply", 0)
            cefi = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(amount), 0) FROM crypto_holdings WHERE guild_id = $1 AND symbol = $2",
                ctx.guild_id, symbol,
            )
            defi = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(amount), 0) FROM wallet_holdings WHERE guild_id = $1 AND symbol = $2",
                ctx.guild_id, symbol,
            )
            staked = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(amount), 0) FROM stakes WHERE guild_id = $1 AND symbol = $2",
                ctx.guild_id, symbol,
            )
            pool_a = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(reserve_a), 0) FROM pools WHERE guild_id = $1 AND token_a = $2",
                ctx.guild_id, symbol,
            )
            pool_b = await ctx.db.fetch_val(
                "SELECT COALESCE(SUM(reserve_b), 0) FROM pools WHERE guild_id = $1 AND token_b = $2",
                ctx.guild_id, symbol,
            )
            in_pools_h = to_human(int(float(pool_a or 0))) + to_human(int(float(pool_b or 0)))
            player_held_h = (
                to_human(int(float(cefi or 0)))
                + to_human(int(float(defi or 0)))
                + to_human(int(float(staked or 0)))
            )
            circulating_h = player_held_h + in_pools_h
            if max_supply > 0:
                circulating_h = min(circulating_h, max_supply)
            circulating_raw = to_raw(circulating_h)
            await ctx.db.execute(
                "UPDATE crypto_prices SET circulating_supply = $1 WHERE guild_id = $2 AND symbol = $3",
                circulating_raw, ctx.guild_id, symbol,
            )
            await ctx.db.execute(
                "UPDATE guild_tokens SET circulating_supply = $1 WHERE guild_id = $2 AND symbol = $3",
                circulating_raw, ctx.guild_id, symbol,
            )
            pct = f" ({circulating_h/max_supply*100:.1f}%)" if max_supply > 0 else ""
            pool_note = f" (pools: {in_pools_h:,.4f})" if in_pools_h > 0 else ""
            lines.append(f"**{symbol}**: `{circulating_h:,.4f}` / `{max_supply:,}`{pct}{pool_note}")
        embed = card(
            "✅ Full Chain Reset Complete",
            description=(
                "All PoW chains reset to block 0.\n"
                "Block history cleared. Supply recalculated from player holdings.\n\n"
                + "\n".join(lines)
            ),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── MM Persona management ─────────────────────────────────────────────────

    @admin.group(name="persona", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_persona(self, ctx: DiscoContext) -> None:
        """MM persona management. Subcommands: list, create, setprompt, setavatar, tradebias, toggle, delete."""
        if await suggest_subcommand(ctx, self.admin_persona):
            return
        await self.admin_persona_list(ctx)

    @admin_persona.command(name="list")
    @_require_manage_guild()
    async def admin_persona_list(self, ctx: DiscoContext) -> None:
        """List all MM personas for this server."""
        personas = await ctx.db.get_mm_personas(ctx.guild_id)
        if not personas:
            await ctx.reply_error("No personas configured. Use `.admin mmwebhook create` to seed defaults, or `.admin persona create`.")
            return
        _b = card("🎭 MM Personas", color=C_PURPLE)
        for p in personas:
            status = "✅ Active" if p["active"] else "❌ Inactive"
            _b.field(
                f"{p['emoji']} **{p['name']}**  -  {status}",
                (
                    f"Bias: `{p['trade_bias']}`\n"
                    f"Avatar: {p['avatar_url'][:40] + '…' if len(p.get('avatar_url','')) > 40 else p.get('avatar_url',' - ')}\n"
                    f"Prompt: {p['system_prompt'][:80] + '…' if len(p['system_prompt']) > 80 else p['system_prompt'] or ' - '}"
                ),
                False,
            )
        embed = _b.footer(".admin persona create <name> <bias> [emoji] | bias: bull/bear/neutral/random").build()
        await ctx.reply(embed=embed, mention_author=False)

    @admin_persona.command(name="create")
    @_require_manage_guild()
    async def admin_persona_create(
        self, ctx: DiscoContext, name: str, trade_bias: str = "neutral", emoji: str = "🤖"
    ) -> None:
        """Create a new MM persona. Default prompt can be set with .admin persona setprompt."""
        _BIAS_OPTS = ("bull", "bear", "neutral", "random")
        bias = trade_bias.lower()
        if bias not in _BIAS_OPTS:
            valid = ", ".join(f"`{b}`" for b in _BIAS_OPTS)
            await ctx.reply_error(f"trade_bias must be: {valid}")
            return
        if len(name) > 32:
            await ctx.reply_error("Name must be 32 characters or fewer.")
            return
        avatar_url = f"https://robohash.org/{name.replace(' ', '')}?set=set3&size=80x80"
        default_prompt = f"You are {name}, a market maker in a Discord economy game. Trade with {bias} bias."
        await ctx.db.create_mm_persona(ctx.guild_id, name, default_prompt, avatar_url, bias, emoji)
        await ctx.reply_success(
            f"Persona **{emoji} {name}** created with `{bias}` bias.\n"
            "Set their personality with `.admin persona setprompt <name> <prompt...>`",
            title="✅ Persona Created",
        )

    @admin_persona.command(name="setprompt")
    @_require_manage_guild()
    async def admin_persona_setprompt(self, ctx: DiscoContext, name: str, *, prompt: str) -> None:
        """Set the AI personality prompt for a persona. This shapes how they trade and speak."""
        personas = await ctx.db.get_mm_personas(ctx.guild_id)
        if not any(p["name"] == name for p in personas):
            await ctx.reply_error(f"Persona **{name}** not found. Use `.admin persona list`.")
            return
        if len(prompt) > 500:
            await ctx.reply_error("Prompt must be 500 characters or fewer.")
            return
        await ctx.db.update_mm_persona_field(ctx.guild_id, name, "system_prompt", prompt)
        await ctx.reply_success(f"**{name}** prompt updated.", title="✅ Prompt Set")

    @admin_persona.command(name="setavatar")
    @_require_manage_guild()
    async def admin_persona_setavatar(self, ctx: DiscoContext, name: str, *, url: str) -> None:
        """Set the avatar URL for a persona (shown in Discord via webhook)."""
        personas = await ctx.db.get_mm_personas(ctx.guild_id)
        if not any(p["name"] == name for p in personas):
            await ctx.reply_error(f"Persona **{name}** not found.")
            return
        await ctx.db.update_mm_persona_field(ctx.guild_id, name, "avatar_url", url)
        await ctx.reply_success(f"**{name}** avatar updated.", title="✅ Avatar Set")

    @admin_persona.command(name="settradebias")
    @_require_manage_guild()
    async def admin_persona_settradebias(self, ctx: DiscoContext, name: str, bias: str) -> None:
        """Set trade bias for a persona: bull, bear, neutral, random."""
        _BIAS_OPTS = ("bull", "bear", "neutral", "random")
        bias = bias.lower()
        if bias not in _BIAS_OPTS:
            valid = ", ".join(f"`{b}`" for b in _BIAS_OPTS)
            await ctx.reply_error(f"Bias must be: {valid}")
            return
        personas = await ctx.db.get_mm_personas(ctx.guild_id)
        if not any(p["name"] == name for p in personas):
            await ctx.reply_error(f"Persona **{name}** not found.")
            return
        await ctx.db.update_mm_persona_field(ctx.guild_id, name, "trade_bias", bias)
        await ctx.reply_success(f"**{name}** trade bias → `{bias}`", title="✅ Bias Updated")

    @admin_persona.command(name="toggle")
    @_require_manage_guild()
    async def admin_persona_toggle(self, ctx: DiscoContext, name: str) -> None:
        """Enable or disable a persona."""
        personas = await ctx.db.get_mm_personas(ctx.guild_id)
        p = next((x for x in personas if x["name"] == name), None)
        if not p:
            await ctx.reply_error(f"Persona **{name}** not found.")
            return
        new_val = 0 if p["active"] else 1
        await ctx.db.update_mm_persona_field(ctx.guild_id, name, "active", new_val)
        status = "enabled ✅" if new_val else "disabled ❌"
        await ctx.reply_success(f"**{name}** → {status}", title="✅ Persona Updated")

    @admin_persona.command(name="delete")
    @_require_manage_guild()
    async def admin_persona_delete(self, ctx: DiscoContext, name: str) -> None:
        """Permanently delete a persona."""
        personas = await ctx.db.get_mm_personas(ctx.guild_id)
        if not any(p["name"] == name for p in personas):
            await ctx.reply_error(f"Persona **{name}** not found.")
            return
        await ctx.db.delete_mm_persona(ctx.guild_id, name)
        await ctx.reply_success(f"Persona **{name}** deleted.", title="✅ Persona Deleted")

    @admin.command(name="audit")
    @_require_manage_guild()
    async def admin_audit(self, ctx: DiscoContext, limit: int = 50) -> None:
        """Show the recent admin-scope staff audit feed. Usage: ,admin audit [limit]"""
        limit = max(1, min(250, int(limit)))
        entries = await recent_staff_actions(
            ctx.db, guild_id=ctx.guild_id, scope=SCOPE_ADMIN, limit=limit,
        )
        pages = build_audit_embeds(entries, scope=SCOPE_ADMIN, guild=ctx.guild)
        if not pages:
            b = card("\U0001F4CB Admin Audit", color=C_NAVY)
            b.description("No audit entries found for the admin scope.")
            await ctx.reply(embed=b.build(), mention_author=False)
            return
        if len(pages) > 1:
            await CategoryPaginator.send(ctx, {"📋 Admin Audit": pages})
        else:
            await ctx.reply(embed=pages[0], mention_author=False)

    @admin.command(name="reject")
    @_require_manage_guild()
    async def admin_reject(self, ctx: DiscoContext, action_id: int) -> None:
        """Reject a pending mempool action by ID. Refunds locked tokens but not gas fees.
        Usage: $admin reject <action_id>"""
        # Get the action
        action = await ctx.db.get_mempool_action(action_id)
        if not action:
            await ctx.reply_error(f"No mempool action found with ID `{action_id}`.")
            return
        if action["status"] != "pending":
            await ctx.reply_error(f"Action {action_id} is not pending (status: {action['status']}).")
            return
        if action["guild_id"] != ctx.guild_id:
            await ctx.reply_error("Action belongs to a different server.")
            return

        # Create a dummy block for the rejection
        block_id = await ctx.db.create_validator_block(ctx.guild_id, action["network"], 0)  # validator_id 0 for admin

        # Refund locked tokens (but not gas)
        payload = json.loads(action["payload"])
        action_type = action["action_type"]
        user_id = action["user_id"]
        try:
            if action_type == "send":
                symbol = payload.get("symbol", "").upper()
                amount = float(payload.get("amount", 0))
                if symbol and amount > 0:
                    await ctx.db.update_holding(user_id, ctx.guild_id, symbol, to_raw(amount))
            elif action_type == "swap":
                token_in = payload.get("token_in", "").upper()
                amount_in = float(payload.get("amount_in", 0))
                if token_in and amount_in > 0:
                    await ctx.db.update_holding(user_id, ctx.guild_id, token_in, to_raw(amount_in))
            # Stake/unstake don't lock tokens upfront, so no refund needed
        except Exception as e:
            log.error("[admin reject] Refund failed for action %s: %s", action_id, e)

        # Mark as rejected
        await ctx.db.resolve_mempool_action(action_id, "rejected", block_id)

        # Publish event for validator embed
        await ctx.bot.bus.publish(
            "validator_block",
            guild=ctx.guild,
            network=action["network"],
            validator=None,  # admin rejection
            block_id=block_id,
            results=[{
                "action": action,
                "success": False,
                "reason": "Rejected by admin",
                "gas": 0.0,  # no gas collected on rejection
            }],
            total_gas=0.0,
            gas_coin="",
            validator_reward=0.0,
            treasury_cut=0.0,
        )

        await ctx.reply_success(
            f"Mempool action **{action_id}** rejected. Locked tokens refunded to user.",
            title="✅ Action Rejected"
        )

    # ── Proxy: .admin backup ──────────────────────────────────────────────────

    @admin.group(name="backup", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_backup(self, ctx: DiscoContext) -> None:
        """Database backup commands. Usage: .admin backup <create|list|restore>"""
        if await suggest_subcommand(ctx, self.admin_backup):
            return
        await ctx.send_help(ctx.command)

    @admin_backup.command(name="create")
    @guild_only
    @_require_manage_guild()
    async def admin_backup_create(self, ctx: DiscoContext) -> None:
        """Manually trigger a database backup now."""
        cog = self.bot.get_cog("Backup")
        if cog is None:
            await ctx.reply_error("Backup cog not loaded.")
            return
        await ctx.invoke(cog.backup_create)

    @admin_backup.command(name="list")
    @guild_only
    @_require_manage_guild()
    async def admin_backup_list(self, ctx: DiscoContext) -> None:
        """List existing database backups."""
        cog = self.bot.get_cog("Backup")
        if cog is None:
            await ctx.reply_error("Backup cog not loaded.")
            return
        await ctx.invoke(cog.backup_list)

    @admin_backup.command(name="restore")
    @guild_only
    @_require_manage_guild()
    async def admin_backup_restore(self, ctx: DiscoContext, filename: str) -> None:
        """Restore from a backup and restart. Usage: .admin backup restore <filename>"""
        cog = self.bot.get_cog("Backup")
        if cog is None:
            await ctx.reply_error("Backup cog not loaded.")
            return
        await ctx.invoke(cog.backup_restore, filename=filename)

    # ── Permission management ──────────────────────────────────────────────

    @admin.group(name="perm", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_perm(self, ctx: DiscoContext) -> None:
        """Manage command role restrictions.

        Usage:
          -admin perm list                           -  show all restrictions
          -admin perm add <command> @role            -  restrict command to role
          -admin perm remove <command> @role         -  remove role restriction
          -admin perm clear <command>                -  unrestrict command (all roles)

        When a command has role restrictions only members with at least one of the
        allowed roles can use it. Admins with Manage Guild are always exempt.
        """
        if await suggest_subcommand(ctx, self.admin_perm):
            return
        all_perms = await self.bot.db.guilds.get_all_command_roles(ctx.guild.id)
        if not all_perms:
            await ctx.reply_success("No command restrictions set. All commands are open to everyone.")
            return

        _b = card("🔒 Command Restrictions", color=C_WARNING)
        lines = []
        for cmd_name, role_ids in sorted(all_perms.items()):
            role_mentions = ", ".join(
                f"<@&{rid}>" if ctx.guild.get_role(rid) else f"`{rid}`"
                for rid in role_ids
            )
            lines.append(f"**`{cmd_name}`** → {role_mentions}")
        for i in range(0, len(lines), 15):
            _b.field(
                f"Restrictions {i + 1} - {min(i + 15, len(lines))}",
                "\n".join(lines[i:i + 15]),
                False,
            )
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_perm.command(name="list")
    @guild_only
    @_require_manage_guild()
    async def admin_perm_list(self, ctx: DiscoContext) -> None:
        """Show all command role restrictions for this server."""
        await ctx.invoke(self.admin_perm)

    @admin_perm.command(name="add")
    @guild_only
    @_require_manage_guild()
    async def admin_perm_add(self, ctx: DiscoContext, command_name: str, role: discord.Role) -> None:
        """Restrict a command to a specific role. Usage: -admin perm add <command> @role"""
        await self.bot.db.guilds.add_command_role(ctx.guild.id, command_name.lower(), role.id)
        await ctx.reply_success(
            f"Command **`{command_name}`** is now restricted to {role.mention} (and admins)."
        )

    @admin_perm.command(name="remove")
    @guild_only
    @_require_manage_guild()
    async def admin_perm_remove(self, ctx: DiscoContext, command_name: str, role: discord.Role) -> None:
        """Remove a role from a command restriction. Usage: -admin perm remove <command> @role"""
        await self.bot.db.guilds.remove_command_role(ctx.guild.id, command_name.lower(), role.id)
        # Check if any restrictions remain
        remaining = await self.bot.db.guilds.get_command_allowed_roles(ctx.guild.id, command_name.lower())
        if remaining:
            role_mentions = ", ".join(f"<@&{rid}>" for rid in remaining)
            await ctx.reply_success(
                f"Removed {role.mention} from **`{command_name}`**. Still restricted to: {role_mentions}"
            )
        else:
            await ctx.reply_success(
                f"Removed {role.mention} from **`{command_name}`**. Command is now unrestricted."
            )

    @admin_perm.command(name="clear")
    @guild_only
    @_require_manage_guild()
    async def admin_perm_clear(self, ctx: DiscoContext, command_name: str) -> None:
        """Remove all role restrictions from a command, making it open to all users."""
        await self.bot.db.guilds.clear_command_roles(ctx.guild.id, command_name.lower())
        await ctx.reply_success(
            f"All restrictions removed from **`{command_name}`**. It is now open to everyone."
        )

    # ── DRS Terminal ----------------------------------------------------------

    @admin.group(name="helpers", aliases=["drsterminal"], invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_helpers(self, ctx: DiscoContext) -> None:
        """Manage DRS Terminal operators  -  trusted players who assist with game management."""
        if await suggest_subcommand(ctx, self.admin_helpers):
            return
        await self.admin_helpers_list(ctx)

    @admin_helpers.command(name="list", aliases=["ls"])
    @_require_manage_guild()
    async def admin_helpers_list(self, ctx: DiscoContext) -> None:
        """View all DRS Terminal operators and their recent activity."""
        rows = await ctx.db.fetch_all(
            "SELECT * FROM game_helpers WHERE guild_id = $1 ORDER BY created_at",
            ctx.guild_id,
        )
        if not rows:
            p = ctx.prefix or "."
            await ctx.reply(
                embed=card("🖥 DRS Terminal", description=f"No operators assigned.\nUse `{p}admin helpers add @user` to add one.", color=C_INFO).build(),
                mention_author=False,
            )
            return

        lines = []
        for r in rows:
            member = ctx.guild.get_member(int(r["user_id"]))
            name = member.display_name if member else f"User {r['user_id']}"
            ts = r["created_at"]
            try:
                ts_str = fmt_ts(ts, "%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                ts_str = str(ts)[:10]
            # Count recent actions
            action_count = await ctx.db.fetch_val(
                "SELECT COUNT(*) FROM helper_audit_log WHERE guild_id = $1 AND helper_id = $2",
                ctx.guild_id, int(r["user_id"]),
            )
            note_str = f"  -  *{r['notes']}*" if r.get("notes") else ""
            lines.append(f"**{name}** (since {ts_str}, {action_count} actions){note_str}")

        embed = card("🖥 DRS Terminal", description="\n".join(lines), color=C_NAVY).build()
        await ctx.reply(embed=embed, mention_author=False)

    @admin_helpers.command(name="add", aliases=["grant"])
    @_require_manage_guild()
    async def admin_helpers_add(self, ctx: DiscoContext, target: discord.Member, *, notes: str = "") -> None:
        """Add a DRS Terminal operator. Usage: .admin helpers add @user [notes]"""
        await ctx.db.execute(
            "INSERT INTO game_helpers (guild_id, user_id, granted_by, notes) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET notes = $4",
            ctx.guild_id, target.id, ctx.author.id, notes or None,
        )
        await ctx.reply(
            embed=card("✅ Helper Added", description=f"**{target.display_name}** is now a DRS Terminal operator.\nThey can use `.drs` commands to assist players.", color=C_SUCCESS).build(),
            mention_author=False,
        )

    @admin_helpers.command(name="remove", aliases=["revoke"])
    @_require_manage_guild()
    async def admin_helpers_remove(self, ctx: DiscoContext, target: discord.Member) -> None:
        """Remove a DRS Terminal operator. Usage: .admin helpers remove @user"""
        await ctx.db.execute(
            "DELETE FROM game_helpers WHERE guild_id = $1 AND user_id = $2",
            ctx.guild_id, target.id,
        )
        await ctx.reply(
            embed=card("🗑️ Helper Removed", description=f"**{target.display_name}** is no longer a DRS Terminal operator.", color=C_AMBER).build(),
            mention_author=False,
        )

    @admin_helpers.command(name="announce_role", aliases=["announcer", "annrole"])
    @_require_manage_guild()
    async def admin_helpers_announce_role(self, ctx: DiscoContext, role: discord.Role = None) -> None:
        """Set (or clear) the role pinged by .drs announce. Usage: .admin helpers announce_role @role"""
        if role:
            await ctx.db.execute(
                "UPDATE guild_settings SET gm_announce_role_id = $2 WHERE guild_id = $1",
                ctx.guild_id, role.id,
            )
            await ctx.reply(
                embed=card("Announce Role Set", description=f"`.drs announce` will now ping {role.mention}.", color=C_SUCCESS).build(),
                mention_author=False,
            )
        else:
            await ctx.db.execute(
                "UPDATE guild_settings SET gm_announce_role_id = NULL WHERE guild_id = $1",
                ctx.guild_id,
            )
            await ctx.reply(
                embed=card("Announce Role Cleared", description="`.drs announce` will no longer ping a role.", color=C_AMBER).build(),
                mention_author=False,
            )

    @admin_helpers.command(name="audit", aliases=["log"])
    @_require_manage_guild()
    async def admin_helpers_audit(self, ctx: DiscoContext, target: discord.Member = None) -> None:
        """View helper audit log. Optionally filter by helper."""
        if target:
            rows = await ctx.db.fetch_all(
                "SELECT * FROM helper_audit_log WHERE guild_id = $1 AND helper_id = $2 ORDER BY created_at DESC LIMIT 20",
                ctx.guild_id, target.id,
            )
        else:
            rows = await ctx.db.fetch_all(
                "SELECT * FROM helper_audit_log WHERE guild_id = $1 ORDER BY created_at DESC LIMIT 20",
                ctx.guild_id,
            )
        if not rows:
            await ctx.reply(
                embed=card("📜 Helper Audit Log", description="No helper actions recorded.", color=C_INFO).build(),
                mention_author=False,
            )
            return

        lines = []
        for r in rows:
            helper = ctx.guild.get_member(int(r["helper_id"]))
            h_name = helper.display_name if helper else f"User {r['helper_id']}"
            ts = r["created_at"]
            try:
                ts_str = fmt_ts(ts, "%m/%d %H:%M")
            except (TypeError, ValueError, OSError):
                ts_str = str(ts)[:16]
            target_str = f" -> <@{int(r['target_id'])}>" if r.get("target_id") else ""
            detail = f" ({r['details'][:60]})" if r.get("details") else ""
            lines.append(f"`{ts_str}` **{h_name}**  -  {r['action']}{target_str}{detail}")

        embed = card("📜 Helper Audit Log", description="\n".join(lines), color=C_NAVY).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── Quick Lists: Helpers & Beta Testers ────────────────────────────────

    @admin.command(name="testers", aliases=["betatesters"])
    @_require_manage_guild()
    async def admin_testers(self, ctx: DiscoContext) -> None:
        """Quick view of all beta testers and their feature access."""
        grants = await self.bot.db.guilds.get_beta_grants(ctx.guild_id)
        if not grants:
            await ctx.reply(
                embed=card("🧪 Beta Testers", description="No beta access grants.", color=C_INFO).build(),
                mention_author=False,
            )
            return

        # Group by user/role
        by_target: dict[str, list[str]] = {}
        for g in grants:
            if g["grant_type"] == "user":
                member = ctx.guild.get_member(g["grant_id"])
                key = f"👤 {member.display_name}" if member else f"👤 User {g['grant_id']}"
            else:
                role = ctx.guild.get_role(g["grant_id"])
                key = f"🏷️ @{role.name}" if role else f"🏷️ Role {g['grant_id']}"
            by_target.setdefault(key, []).append(f"`{g['feature_name']}`")

        lines = [f"{target}: {', '.join(features)}" for target, features in by_target.items()]
        embed = card("🧪 Beta Testers", description="\n".join(lines), color=C_PURPLE).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── Beta Feature Access ──────────────────────────────────────────────────

    @admin.group(name="beta", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_beta(self, ctx: DiscoContext) -> None:
        """Manage beta feature access per user or role."""
        if await suggest_subcommand(ctx, self.admin_beta):
            return
        await self.admin_beta_list(ctx)

    @admin_beta.command(name="features")
    @_require_manage_guild()
    async def admin_beta_features(self, ctx: DiscoContext) -> None:
        """List all available beta features."""

        p = ctx.prefix or "."
        lines = [f"**`{name}`**  -  {desc}" for name, desc in BETA_FEATURES.items()]
        b = card("Beta Features", color=C_PURPLE)
        b.description("\n".join(lines))
        b.footer(f"{p}admin beta grant <feature> @user/@role  |  {p}admin beta revoke <feature> @user/@role")
        await ctx.reply(embed=b.build(), mention_author=False)

    @admin_beta.command(name="list")
    @_require_manage_guild()
    async def admin_beta_list(self, ctx: DiscoContext) -> None:
        """Show all current beta access grants for this server."""

        grants = await self.bot.db.guilds.get_beta_grants(ctx.guild_id)
        p = ctx.prefix or "."
        if not grants:
            b = card("Beta Access", color=C_PURPLE)
            b.description(
                "No beta access grants configured.\n"
                "Admins (Manage Server) always have access.\n\n"
                f"Use `{p}admin beta features` to see available features.\n"
                f"Use `{p}admin beta grant <feature> @user/@role` to grant access."
            )
            return await ctx.reply(embed=b.build(), mention_author=False)

        b = card("Beta Access Grants", color=C_PURPLE)
        # Group by feature
        by_feature: dict[str, list[str]] = {}
        for g in grants:
            feat = g["feature_name"]
            gtype = g["grant_type"]
            gid = g["grant_id"]
            if gtype == "user":
                label = f"<@{gid}>"
            else:
                label = f"<@&{gid}>"
            by_feature.setdefault(feat, []).append(label)
        for feat, labels in by_feature.items():
            desc = BETA_FEATURES.get(feat, "")
            b.field(f"`{feat}`{f'  -  {desc}' if desc else ''}", "\n".join(labels[:10]), False)
        b.footer(f"Admins always have access  •  {p}admin beta grant/revoke <feature> @user/@role")
        await ctx.reply(embed=b.build(), mention_author=False)

    @admin_beta.command(name="grant")
    @_require_manage_guild()
    async def admin_beta_grant(self, ctx: DiscoContext, feature: str, target: str) -> None:
        """Grant beta access. Usage: .admin beta grant <feature> @user or @role"""

        feature = feature.lower()
        if feature not in BETA_FEATURES:
            available = ", ".join(f"`{k}`" for k in BETA_FEATURES)
            return await ctx.reply_error(f"Unknown feature `{feature}`. Available: {available}")

        # Parse target  -  could be user mention, role mention, or ID
        import re
        user_match = re.match(r"<@!?(\d+)>", target)
        role_match = re.match(r"<@&(\d+)>", target)
        if user_match:
            grant_type, grant_id = "user", int(user_match.group(1))
            label = f"<@{grant_id}>"
        elif role_match:
            grant_type, grant_id = "role", int(role_match.group(1))
            label = f"<@&{grant_id}>"
        else:
            # Try as raw ID  -  check if it's a role or user in the guild
            try:
                raw_id = int(target)
                role = ctx.guild.get_role(raw_id)
                if role:
                    grant_type, grant_id = "role", raw_id
                    label = role.mention
                else:
                    grant_type, grant_id = "user", raw_id
                    label = f"<@{raw_id}>"
            except ValueError:
                return await ctx.reply_error("Provide a @user mention, @role mention, or ID.")

        await self.bot.db.guilds.grant_beta(ctx.guild_id, feature, grant_type, grant_id, ctx.author.id)
        await ctx.reply_success(
            f"Granted **`{feature}`** beta access to {label}.",
            title="Beta Access Granted",
        )

    @admin_beta.command(name="revoke")
    @_require_manage_guild()
    async def admin_beta_revoke(self, ctx: DiscoContext, feature: str, target: str) -> None:
        """Revoke beta access. Usage: .admin beta revoke <feature> @user or @role"""

        feature = feature.lower()
        if feature not in BETA_FEATURES:
            available = ", ".join(f"`{k}`" for k in BETA_FEATURES)
            return await ctx.reply_error(f"Unknown feature `{feature}`. Available: {available}")

        import re
        user_match = re.match(r"<@!?(\d+)>", target)
        role_match = re.match(r"<@&(\d+)>", target)
        if user_match:
            grant_type, grant_id = "user", int(user_match.group(1))
            label = f"<@{grant_id}>"
        elif role_match:
            grant_type, grant_id = "role", int(role_match.group(1))
            label = f"<@&{grant_id}>"
        else:
            try:
                raw_id = int(target)
                role = ctx.guild.get_role(raw_id)
                grant_type = "role" if role else "user"
                grant_id = raw_id
                label = role.mention if role else f"<@{raw_id}>"
            except ValueError:
                return await ctx.reply_error("Provide a @user mention, @role mention, or ID.")

        await self.bot.db.guilds.revoke_beta(ctx.guild_id, feature, grant_type, grant_id)
        await ctx.reply_success(f"Revoked **`{feature}`** beta access from {label}.", title="Beta Access Revoked")

    @admin_beta.command(name="clear")
    @_require_manage_guild()
    async def admin_beta_clear(self, ctx: DiscoContext, feature: str) -> None:
        """Remove all beta grants for a feature. Usage: .admin beta clear <feature>"""

        feature = feature.lower()
        if feature not in BETA_FEATURES:
            available = ", ".join(f"`{k}`" for k in BETA_FEATURES)
            return await ctx.reply_error(f"Unknown feature `{feature}`. Available: {available}")
        await self.bot.db.guilds.clear_beta_feature(ctx.guild_id, feature)
        await ctx.reply_success(f"All beta grants for **`{feature}`** cleared.", title="Beta Access Cleared")

    # ── Reports ────────────────────────────────────────────────────────────

    @admin.command(name="reports")
    @_require_manage_guild()
    async def admin_reports(self, ctx: DiscoContext, *, args: str = "") -> None:
        """View reports with optional filters.

        Usage:
          -admin reports                          -  all reports (all categories/statuses)
          -admin reports CATEGORY                 -  filter by category (bugs/suggestions/users/other)
          -admin reports STATUS                   -  filter by status (open/accepted/rejected/in_progress/resolved/closed)
          -admin reports CATEGORY STATUS          -  filter by both
          -admin reports search @user             -  all reports by a user
          -admin reports search NUMBER            -  view a specific report by ID
          -admin reports delete ID                -  delete a specific report
          -admin reports export [CATEGORY] [STATUS]  -  export reports as CSV
          -admin reports dump [CATEGORY] [STATUS]    -  full untruncated Markdown via DM
          -admin reports diagnose ID                 -  AI realness check on one report
          -admin reports auto on|off                 -  toggle AI auto-diagnose on every new report
          -admin reports auto                        -  show current toggle state
          -admin reports close-old DAYS [STATUS]     -  bulk-close stale reports (status=closed, audit kept)
          -admin reports auto-close on|off           -  auto-reject spam / auto-resolve merged-PR reports
          -admin reports autofix on|off              -  toggle AI auto-PR (Tier A) on real reports
          -admin reports autofix ID                  -  manually trigger auto-fix for one report
          -admin reports autofix test                -  probe GitHub auth + repo access (read-only)
          -admin reports autofix                     -  show auto-fix toggle + GitHub config status
          -admin reports queue                       -  per-report autofix dashboard + recent entries
          -admin reports queue scan                  -  enqueue every open report not in flight
          -admin reports queue add ID                -  enqueue a single report
          -admin reports queue status ID             -  show one report's autofix lifecycle + links
          -admin reports queue cancel [ID]           -  flip active row(s) to discarded; bulk also turns autofix OFF
          -admin reports queue resume                -  flip autofix back ON after a bulk cancel
          -admin reports queue clear                 -  drop terminal rows (failed / discarded / pr_open)
          -admin reports clear                    -  delete ALL reports (with confirmation)
          -admin reports clear CATEGORY           -  delete reports in a category
          -admin reports clear STATUS             -  delete reports with a status
          -admin reports clear CATEGORY STATUS    -  delete reports matching both filters
          -admin reports dm @user|ID              -  set report DM notification recipient
          -admin reports dm reset                 -  reset DM recipient to bot default
          -admin reports dm                       -  show current DM recipient
        """
        STATUS_EMOJI = {
            "open": "📩", "accepted": "✅", "in_progress": "🔧",
            "resolved": "✅", "closed": "🔒", "rejected": "❌",
        }

        parts = args.strip().split() if args.strip() else []

        # ── delete subcommand ──
        if parts and parts[0].lower() == "delete" and len(parts) >= 2:
            try:
                report_id = int(parts[1])
            except ValueError:
                await ctx.reply_error("Usage: `-admin reports delete <ID>`")
                return
            deleted = await ctx.db.reports.delete_report(report_id)
            if deleted:
                await ctx.reply_success(f"Report **#{report_id}** deleted.")
            else:
                await ctx.reply_error(f"Report #{report_id} not found.")
            return

        # ── export subcommand ──
        if parts and parts[0].lower() == "export":
            import io, csv as _csv
            cat_filter = None
            status_filter = None
            for p in parts[1:]:
                p_lower = p.lower()
                if p_lower in VALID_CATEGORIES:
                    cat_filter = p_lower
                elif p_lower in VALID_STATUSES:
                    status_filter = p_lower
            rows = await self.bot.db.reports.get_reports_filtered(
                guild_id=ctx.guild.id,
                category=cat_filter,
                status=status_filter,
            )
            if not rows:
                await ctx.reply_error("No reports match that filter to export.")
                return
            buf = io.StringIO()
            writer = _csv.writer(buf)
            writer.writerow(["ID", "User ID", "Category", "Status", "Message", "Tags", "Admin Note", "Created At", "Updated At"])
            for r in rows:
                writer.writerow([
                    r["id"], r["user_id"], r.get("category", ""), r["status"],
                    r["message"], r.get("tags", ""), r.get("admin_note", ""),
                    str(r["created_at"]), str(r["updated_at"]),
                ])
            buf.seek(0)
            fname = "reports"
            if cat_filter:
                fname += f"_{cat_filter}"
            if status_filter:
                fname += f"_{status_filter}"
            file = discord.File(io.BytesIO(buf.getvalue().encode()), filename=f"{fname}.csv")
            await ctx.reply(f"Exported **{len(rows)}** reports.", file=file, mention_author=False)
            return

        # ── queue subcommand ──
        # Per-report auto-fix lifecycle dashboard. Lists every report
        # currently in the autofix queue with its status + links, lets
        # the admin batch-add reports, scan all open reports at once, or
        # clear terminal entries. Heavy-lifting lives on the Report cog
        # (``_process_queued_autofix`` / ``_dm_status_update``).
        if parts and parts[0].lower() == "queue":
            sub = (parts[1].lower() if len(parts) >= 2 else "")
            counts = await self.bot.db.reports.autofix_status_counts(ctx.guild_id)

            # ── queue scan ──
            if sub in ("scan", "all"):
                # Pull every open / accepted / in_progress report and
                # enqueue any that aren't already in the queue. Reports
                # already in a non-terminal queue state are skipped so
                # we don't disturb in-flight work.
                rows = await self.bot.db.reports.get_reports_filtered(
                    guild_id=ctx.guild_id, status=None, category=None,
                )
                added = 0
                skipped = 0
                for r in rows or []:
                    if str(r.get("status")) in ("rejected", "closed"):
                        continue
                    existing = await self.bot.db.reports.get_autofix_entry(int(r["id"]))
                    if existing and existing.get("status") not in (
                        "failed", "unfixable", "discarded", "pr_open",
                    ):
                        skipped += 1
                        continue
                    await self.bot.db.reports.queue_autofix(
                        int(r["id"]), ctx.guild_id, requested_by=ctx.author.id,
                    )
                    added += 1
                await ctx.reply_success(
                    f"Queued **{added}** report(s); skipped **{skipped}** "
                    f"already in flight. Worker picks up the next row "
                    f"every 30s -- DMs land as each step completes.",
                    title="\U0001F551 Queue scan",
                )
                return

            # ── queue add <id> ──
            if sub in ("add", "queue"):
                if len(parts) < 3 or not parts[2].isdigit():
                    await ctx.reply_error_hint(
                        "Specify a report ID.",
                        hint="admin reports queue add 42",
                        command_name="admin reports queue add",
                    )
                    return
                rid = int(parts[2])
                report = await self.bot.db.reports.get_report(rid)
                if not report or int(report.get("guild_id") or 0) != int(ctx.guild_id):
                    await ctx.reply_error(f"Report #{rid} not found in this guild.")
                    return
                row = await self.bot.db.reports.queue_autofix(
                    rid, ctx.guild_id, requested_by=ctx.author.id,
                )
                if row is None:
                    await ctx.reply_error(
                        f"Report #{rid} is already in flight in the queue."
                    )
                    return
                await ctx.reply_success(
                    f"Report **#{rid}** queued. Worker picks it up shortly.",
                    title="\U0001F551 Queued",
                )
                return

            # ── queue status [id] ──
            if sub in ("status", "show"):
                if len(parts) >= 3 and parts[2].isdigit():
                    rid = int(parts[2])
                    entry = await self.bot.db.reports.get_autofix_entry(rid)
                    if not entry:
                        await ctx.reply_error(
                            f"Report #{rid} isn't in the autofix queue."
                        )
                        return
                    e = (
                        card(
                            f"\U0001F527 Autofix #{rid}",
                            color=C_INFO,
                            description=(
                                f"**Status:** `{entry.get('status')}`\n"
                                f"**Requested by:** <@{entry.get('requested_by')}>\n"
                                f"**Updated:** {fmt_ts(entry.get('updated_at'))}"
                            ),
                        )
                        .field_if(
                            bool(entry.get("issue_url")),
                            "Tracking issue", str(entry.get("issue_url") or ""), False,
                        )
                        .field_if(
                            bool(entry.get("pr_url")),
                            "Pull request", str(entry.get("pr_url") or ""), False,
                        )
                        .field_if(
                            bool(entry.get("proposed_path")),
                            "File", f"`{entry.get('proposed_path') or ''}`", True,
                        )
                        .field_if(
                            entry.get("proposed_lines") is not None,
                            "Lines", f"~{int(entry.get('proposed_lines') or 0)}", True,
                        )
                        .field_if(
                            bool(entry.get("last_error")),
                            "Last error", str(entry.get("last_error") or "")[:1000], False,
                        )
                        .build()
                    )
                    await ctx.reply(embed=e, mention_author=False)
                    return
                # No id => same as bare ``queue``; fall through.

            # ── queue resume ──
            # Counterpart to bulk cancel. Flips ``reports_auto_fix`` back
            # ON so newly-submitted reports start auto-enqueuing again.
            # Doesn't repopulate the queue from existing reports -- run
            # ,admin reports queue scan separately if you want that.
            if sub == "resume":
                pre_settings = await ctx.db.get_guild_settings(ctx.guild_id)
                was_off = not bool(pre_settings.get("reports_auto_fix"))
                if was_off:
                    await ctx.db.update_guild_setting(
                        ctx.guild_id, "reports_auto_fix", True,
                    )
                await ctx.reply_success(
                    (
                        "Auto-fix re-armed. New submissions will start "
                        "auto-enqueuing again. To re-process every open "
                        "report right now, run `,admin reports queue scan`."
                        if was_off else
                        "Auto-fix was already on. Nothing changed."
                    ),
                    title="\U0001F501 Autofix resumed" if was_off else "Autofix already on",
                )
                return

            # ── queue clear ──
            if sub == "clear":
                deleted = await self.bot.db.reports.clear_terminal_autofixes(
                    ctx.guild_id,
                )
                await ctx.reply_success(
                    f"Removed **{deleted}** terminal autofix row(s) "
                    f"(failed / unfixable / discarded / pr_open).",
                    title="\U0001F9F9 Queue cleaned",
                )
                return

            # ── queue cancel [id] ──
            # Stop in-flight work without dropping the audit trail.
            # ``cancel <id>`` flips one non-terminal row to discarded;
            # ``cancel`` (bare) bulk-cancels every active row in the
            # guild. The auto-fix worker picks the next 'queued' row at
            # the top of each tick so once these are flipped, nothing
            # new starts. In-memory patches for any 'proposed' rows get
            # dropped from the Report cog so the Open PR button can't
            # ship a cancelled patch by mistake.
            if sub == "cancel":
                report_cog = self.bot.get_cog("Report")
                pending_map = (
                    getattr(report_cog, "_pending_autofixes", None)
                    if report_cog is not None else None
                )
                # Single-id form.
                if len(parts) >= 3 and parts[2].isdigit():
                    rid = int(parts[2])
                    row = await self.bot.db.reports.cancel_autofix(
                        rid, ctx.guild_id,
                        reason=f"Cancelled by <@{ctx.author.id}> via ,admin reports queue cancel.",
                    )
                    if not row:
                        await ctx.reply_error(
                            f"Report #{rid} isn't in a cancellable state "
                            f"(no row, or already terminal). "
                            f"`,admin reports queue status {rid}` to inspect."
                        )
                        return
                    if pending_map is not None:
                        pending_map.pop(rid, None)
                    await ctx.reply_success(
                        f"Cancelled autofix for report **#{rid}**. "
                        f"Status flipped to `discarded`. "
                        f"`,admin reports queue clear` will drop it.",
                        title="\U0001F6D1 Autofix cancelled",
                    )
                    return
                # Bulk form: only proceed after explicit confirmation
                # because cancelling 200 in-flight rows is annoying to
                # undo (you'd have to re-run ,admin reports queue scan).
                view = ConfirmView(ctx.author.id)
                preview_count = await self.bot.db.reports.autofix_status_counts(
                    ctx.guild_id,
                )
                active = sum(
                    int(preview_count.get(s, 0))
                    for s in ("queued", "generating", "proposed")
                )
                if active == 0:
                    await ctx.reply_success(
                        "Nothing to cancel -- queue has no active rows.",
                        title="\U0001F6D1 Queue cancel",
                    )
                    return
                msg = await ctx.reply(
                    f"Cancel **{active}** active autofix row(s) "
                    f"(queued / generating / proposed)? They flip to "
                    f"`discarded` and stay in the queue for audit. "
                    f"`,admin reports queue clear` afterwards drops them.",
                    view=view, mention_author=False,
                )
                await view.wait()
                if not view.value:
                    await msg.edit(content="Cancelled (the cancel was cancelled).", view=None)
                    return
                rows = await self.bot.db.reports.cancel_active_autofixes(
                    ctx.guild_id,
                    reason=f"Bulk-cancelled by <@{ctx.author.id}>.",
                )
                # Drop any in-memory patches for the cancelled rows so
                # a leftover Open PR button on a DM can't ship them.
                if pending_map is not None:
                    for r in rows:
                        pending_map.pop(int(r["report_id"]), None)
                # KILL SWITCH semantics: bulk cancel ALSO flips
                # reports_auto_fix off so new ,report submit calls don't
                # immediately repopulate the queue with fresh rows. The
                # admin's intent is "stop all autofix activity now",
                # not "wait for the next 100 submissions to retry."
                # ,admin reports queue resume undoes both at once.
                turned_off_autofix = False
                try:
                    pre_settings = await ctx.db.get_guild_settings(ctx.guild_id)
                    if bool(pre_settings.get("reports_auto_fix")):
                        await ctx.db.update_guild_setting(
                            ctx.guild_id, "reports_auto_fix", False,
                        )
                        turned_off_autofix = True
                except Exception:
                    log.exception(
                        "queue cancel: failed to flip reports_auto_fix off",
                    )
                tail = (
                    " Auto-fix toggle flipped **OFF** so new submissions "
                    "stop enqueuing. `,admin reports queue resume` to "
                    "re-arm." if turned_off_autofix
                    else " (Auto-fix toggle was already off.)"
                )
                await msg.edit(
                    content=(
                        f"\U0001F6D1 Cancelled **{len(rows)}** active "
                        f"autofix row(s).{tail}"
                    ),
                    view=None,
                )
                return

            # ── No subcommand: overall dashboard ──
            entries = await self.bot.db.reports.list_autofix_entries(
                ctx.guild_id, limit=15,
            )
            total = sum(counts.values())
            order = ("queued", "generating", "proposed", "pr_open",
                     "discarded", "failed", "unfixable")
            summary_lines = []
            for s in order:
                n = int(counts.get(s, 0))
                if n:
                    summary_lines.append(f"`{s:<11}` **{n}**")
            if not summary_lines:
                summary_lines.append("_(empty -- run `,admin reports queue scan`)_")
            row_lines: list[str] = []
            for e in entries[:10]:
                bits = [
                    f"`#{e['report_id']:<5}`",
                    f"`{e['status']:<10}`",
                ]
                if e.get("proposed_path"):
                    bits.append(f"`{e['proposed_path']}`")
                if e.get("pr_url"):
                    bits.append(f"[PR]({e['pr_url']})")
                elif e.get("issue_url"):
                    bits.append(f"[issue]({e['issue_url']})")
                row_lines.append(" ".join(bits))
            embed = (
                card(
                    "\U0001F527 Autofix queue",
                    color=C_INFO,
                    description=(
                        f"Total tracked: **{total}** report(s)\n"
                        + "  ·  ".join(summary_lines)
                    ),
                )
                .field(
                    "Recent (most-recent first)",
                    "\n".join(row_lines) or "_(none)_",
                    False,
                )
                .footer(
                    f"`,admin reports queue scan` to enqueue every open report\n"
                    f"`,admin reports queue add <id>` to add one\n"
                    f"`,admin reports queue status <id>` for details + links\n"
                    f"`,admin reports queue cancel [id]` to kill in-flight rows (bulk also pauses autofix)\n"
                    f"`,admin reports queue resume` to re-arm autofix after a bulk cancel\n"
                    f"`,admin reports queue clear` to drop terminal rows"
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # ── autofix subcommand ──
        # Toggle / manually trigger Tier-A AI auto-fix. ``autofix on/off``
        # flips the per-guild ``reports_auto_fix`` setting; ``autofix
        # <id>`` runs the pipeline against a single report regardless of
        # the toggle state. PRs always open as drafts -- never auto-merged.
        if parts and parts[0].lower() == "autofix":
            sub = (parts[1].lower() if len(parts) >= 2 else "")
            settings = await ctx.db.get_guild_settings(ctx.guild_id)
            cur = bool(settings.get("reports_auto_fix"))
            if sub in ("on", "enable", "true", "1"):
                await ctx.db.update_guild_setting(
                    ctx.guild_id, "reports_auto_fix", True,
                )
                await ctx.reply_success(
                    "Auto-fix **enabled**. New reports whose AI realness "
                    "verdict is `real` / `likely_real` with non-low "
                    "confidence will spawn a draft GitHub PR. Requires "
                    "`GITHUB_TOKEN` + `AUTOFIX_REPO_OWNER` + "
                    "`AUTOFIX_REPO_NAME` env vars.",
                    title="Auto-fix ON",
                )
                return
            if sub in ("off", "disable", "false", "0"):
                await ctx.db.update_guild_setting(
                    ctx.guild_id, "reports_auto_fix", False,
                )
                await ctx.reply_success(
                    "Auto-fix **disabled**. New reports stop opening PRs. "
                    "`,admin reports autofix <id>` still works as a "
                    "per-report manual trigger.",
                    title="Auto-fix OFF",
                )
                return
            # ── autofix test: live GitHub auth + repo access probe ──
            # Read-only call to GET /repos/{owner}/{repo}. Tells the
            # admin whether the configured token can actually see the
            # configured repo, with a specific reason when it can't.
            if sub in ("test", "ping", "check"):
                from core.framework.ai import github_pr as _gh
                async with ctx.typing():
                    info = await _gh.check_auth()
                color = C_SUCCESS if info.get("ok") else C_ERROR
                desc_parts: list[str] = []
                desc_parts.append(
                    f"**Owner:** `{Config.AUTOFIX_REPO_OWNER or '(unset)'}`  "
                    f"**Repo:** `{Config.AUTOFIX_REPO_NAME or '(unset)'}`  "
                    f"**Base:** `{Config.AUTOFIX_BASE_BRANCH or 'main'}`"
                )
                desc_parts.append(
                    f"**HTTP:** `{int(info.get('status') or 0)}`"
                )
                if info.get("private") is True:
                    desc_parts.append("**Visibility:** private (auth required, OK)")
                elif info.get("private") is False:
                    desc_parts.append("**Visibility:** public")
                if info.get("scopes"):
                    desc_parts.append(
                        f"**Token scopes:** `{', '.join(info['scopes'])}`"
                    )
                else:
                    desc_parts.append(
                        "**Token scopes:** _(not in response header -- "
                        "fine-grained tokens are normal here)_"
                    )
                desc_parts.append(f"**Reason:** {info.get('reason', '?')}")
                if not info.get("ok"):
                    desc_parts.append(
                        "\nIf you need scopes, the token needs at least: "
                        "`repo` (classic) or `contents:write + issues:write "
                        "+ pull-requests:write` (fine-grained) on this repo."
                    )
                embed = (
                    card(
                        ("\U00002705 Auto-fix GitHub probe"
                         if info.get("ok") else
                         "\U0000274C Auto-fix GitHub probe failed"),
                        color=color,
                        description="\n".join(desc_parts),
                    )
                    .build()
                )
                await ctx.reply(embed=embed, mention_author=False)
                return
            # Numeric id => manual trigger. Always asks for confirmation
            # before opening the PR -- the cog's _AutoFixConfirmView posts
            # Open PR / Discard buttons in the channel where the command
            # ran. Mirrors the auto path so behaviour is consistent.
            if sub.isdigit():
                from core.framework.ai.heal_ai import get_heal_ai_config
                from core.framework.ai import report_ai as _rai
                from core.framework.ai import auto_fix as _af
                from core.framework.ai import github_pr as _gh
                from pathlib import Path as _Path
                rid = int(sub)
                report = await self.bot.db.reports.get_report(rid)
                if not report or int(report.get("guild_id") or 0) != int(ctx.guild_id):
                    await ctx.reply_error(
                        f"Report #{rid} not found in this guild."
                    )
                    return
                if not _gh.is_configured():
                    await ctx.reply_error(
                        "Auto-fix isn't configured. Set `GITHUB_TOKEN`, "
                        "`AUTOFIX_REPO_OWNER`, `AUTOFIX_REPO_NAME` env vars."
                    )
                    return
                report_cog = self.bot.get_cog("Report")
                if report_cog is None:
                    await ctx.reply_error(
                        "Report cog not loaded; can't stash the patch for "
                        "the confirm step."
                    )
                    return
                ai_cfg = await get_heal_ai_config(ctx.db, ctx.guild_id)
                signals = await _rai.gather_signals(
                    ctx.db, ctx.guild_id, dict(report),
                )
                async with ctx.typing():
                    proposal = await _af.propose_fix(
                        report_text=str(report.get("message") or ""),
                        signals=signals,
                        config=ai_cfg,
                        repo_root=_Path(__file__).resolve().parent.parent,
                    )
                if isinstance(proposal, _af.PatchRejection):
                    await ctx.reply_error(
                        f"Auto-fix declined at the **{proposal.stage}** "
                        f"stage:\n```\n{proposal.reason[:600]}\n```"
                    )
                    return
                # Stash the patch on the Report cog so the confirm view
                # can pick it up the same way the auto path does.
                report_cog._pending_autofixes[rid] = {
                    "proposal":   proposal,
                    "report_row": dict(report),
                    "verdict":    "",  # manual trigger has no verdict context
                }
                # Local import dodges the circular import in the module
                # docstring -- Report cog defines the view.
                from cogs.report import _AutoFixConfirmView
                view = _AutoFixConfirmView(report_cog, rid)
                embed = (
                    card(
                        "\U0001F527 Auto-fix proposed",
                        color=C_INFO,
                        description=(
                            f"Report **#{rid}**\n"
                            f"File: `{proposal.rel_path}`\n"
                            f"~{proposal.lines_changed} lines changed "
                            f"(cap: {_af.MAX_DIFF_LINES})\n"
                            f"Rationale: {proposal.rationale or '(none)'}\n\n"
                            f"Click **Open PR** to push the patch as a "
                            f"draft PR, or **Discard** to drop it. "
                            f"Buttons time out in 24h."
                        ),
                    )
                    .build()
                )
                await ctx.reply(embed=embed, view=view, mention_author=False)
                return
            # No subcommand -> status display.
            label = "**enabled**" if cur else "**disabled**"
            gh_state = "configured" if (Config.GITHUB_TOKEN and Config.AUTOFIX_REPO_OWNER and Config.AUTOFIX_REPO_NAME) else "**NOT configured**"
            embed = (
                card("\U0001F527 Auto-fix status", color=C_INFO)
                .description(
                    f"Tier-A auto-fix is currently {label} for this guild.\n"
                    f"GitHub: {gh_state}\n\n"
                    f"`,admin reports autofix on` to enable\n"
                    f"`,admin reports autofix off` to disable\n"
                    f"`,admin reports autofix <id>` to manually fix one report\n\n"
                    f"PRs always open as **drafts**. No auto-merge ever."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # ── auto-close subcommand ──
        # Toggle auto-rejecting spam/fake reports + auto-resolving
        # reports whose auto-fix PR merges. Two behaviours, one switch
        # because they're both "let the AI close the loop".
        if parts and parts[0].lower() in ("auto-close", "autoclose"):
            sub = (parts[1].lower() if len(parts) >= 2 else "")
            settings = await ctx.db.get_guild_settings(ctx.guild_id)
            cur = bool(settings.get("reports_auto_close"))
            if sub in ("on", "enable", "true", "1"):
                await ctx.db.update_guild_setting(
                    ctx.guild_id, "reports_auto_close", True,
                )
                await ctx.reply_success(
                    "Auto-close **enabled**. Reports with an AI verdict "
                    "of `spam` / `likely_fake` at high confidence will "
                    "auto-reject; reports whose auto-fix PR merges will "
                    "auto-resolve. Manual triage still works either way.",
                    title="\U0001F6AB Auto-close ON",
                )
                return
            if sub in ("off", "disable", "false", "0"):
                await ctx.db.update_guild_setting(
                    ctx.guild_id, "reports_auto_close", False,
                )
                await ctx.reply_success(
                    "Auto-close **disabled**. Status changes happen on "
                    "human click only.",
                    title="\U0001F6AB Auto-close OFF",
                )
                return
            label = "**enabled**" if cur else "**disabled**"
            embed = (
                card("\U0001F6AB Auto-close status", color=C_INFO)
                .description(
                    f"Auto-close is currently {label} for this guild.\n\n"
                    f"When ON, two things change:\n"
                    f"  • Reports w/ AI verdict `spam` or `likely_fake` "
                    f"at **high** confidence are auto-rejected on "
                    f"submit.\n"
                    f"  • Reports whose auto-fix PR is merged on GitHub "
                    f"are auto-resolved (PR watcher polls every 5 min).\n\n"
                    f"`,admin reports auto-close on` to enable\n"
                    f"`,admin reports auto-close off` to disable"
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # ── auto subcommand ──
        # Toggle auto-AI-diagnose on every newly-submitted report. Same
        # provider config as ,admin ai heal -- one backend per guild
        # powers every AI feature.
        if parts and parts[0].lower() == "auto":
            sub = (parts[1].lower() if len(parts) >= 2 else "")
            settings = await ctx.db.get_guild_settings(ctx.guild_id)
            cur = bool(settings.get("reports_auto_diagnose"))
            if sub in ("on", "enable", "true", "1"):
                await ctx.db.update_guild_setting(
                    ctx.guild_id, "reports_auto_diagnose", True,
                )
                await ctx.reply_success(
                    "Auto-diagnose **enabled**. Every new report will be "
                    "AI-diagnosed and the verdict appended to the admin DM. "
                    "Provider follows `,ai heal` settings -- check "
                    "`,admin ai heal status` if results stop arriving.",
                    title="Auto-diagnose ON",
                )
                return
            if sub in ("off", "disable", "false", "0"):
                await ctx.db.update_guild_setting(
                    ctx.guild_id, "reports_auto_diagnose", False,
                )
                await ctx.reply_success(
                    "Auto-diagnose **disabled**. New reports will be "
                    "delivered without an AI verdict; you can still run "
                    "`,admin reports diagnose <id>` manually.",
                    title="Auto-diagnose OFF",
                )
                return
            label = "**enabled**" if cur else "**disabled**"
            embed = (
                card("\U0001F50D Auto-diagnose status", color=C_INFO)
                .description(
                    f"Auto-AI-diagnose is currently {label} for this guild.\n"
                    f"`,admin reports auto on` to enable, "
                    f"`,admin reports auto off` to disable.\n"
                    f"Manual `,admin reports diagnose <id>` always works "
                    f"regardless of this toggle."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # ── dump subcommand ──
        # Like ``export`` but outputs a fully untruncated Markdown file
        # delivered via DM. Useful when the admin wants to read every
        # report end-to-end without Discord embed truncation.
        if parts and parts[0].lower() == "dump":
            import io
            cat_filter = None
            status_filter = None
            for p in parts[1:]:
                p_lower = p.lower()
                if p_lower in VALID_CATEGORIES:
                    cat_filter = p_lower
                elif p_lower in VALID_STATUSES:
                    status_filter = p_lower
            rows = await self.bot.db.reports.get_reports_filtered(
                guild_id=ctx.guild.id,
                category=cat_filter,
                status=status_filter,
            )
            if not rows:
                await ctx.reply_error("No reports match that filter.")
                return
            out: list[str] = []
            out.append(f"# Discoin reports dump")
            out.append("")
            out.append(f"- Guild: `{ctx.guild.name}` (`{ctx.guild.id}`)")
            out.append(f"- Generated: `{discord.utils.utcnow().isoformat()}`")
            out.append(
                f"- Filter: category=`{cat_filter or 'any'}` "
                f"status=`{status_filter or 'any'}`"
            )
            out.append(f"- Reports included: **{len(rows)}**")
            out.append("")
            out.append("---")
            out.append("")
            for r in rows:
                emoji = STATUS_EMOJI.get(str(r["status"]), "❓")
                created_at = r.get("created_at")
                updated_at = r.get("updated_at")
                out.append(
                    f"## {emoji} #{r['id']}  -  [{r.get('category','?')}] "
                    f"({r['status']})"
                )
                out.append("")
                out.append(f"- **Reporter:** `{r.get('user_id', '?')}`")
                if r.get("tags"):
                    out.append(f"- **Tags:** `{r['tags']}`")
                if r.get("reward_amount"):
                    out.append(
                        f"- **Reward:** "
                        f"${to_human(int(r['reward_amount'] or 0)):,.2f}"
                    )
                if created_at:
                    out.append(f"- **Created:** `{created_at}`")
                if updated_at and updated_at != created_at:
                    out.append(f"- **Updated:** `{updated_at}`")
                out.append("")
                out.append("**Message:**")
                out.append("")
                # Block-quote each line so empty lines in the report don't
                # break out of the section.
                msg = str(r.get("message") or "")
                for line in msg.splitlines() or [""]:
                    out.append(f"> {line}")
                if r.get("admin_note"):
                    out.append("")
                    out.append("**Admin note:**")
                    out.append("")
                    for line in str(r["admin_note"]).splitlines() or [""]:
                        out.append(f"> {line}")
                out.append("")
                out.append("---")
                out.append("")
            buf = io.BytesIO("\n".join(out).encode("utf-8"))
            fname = "reports_dump"
            if cat_filter:
                fname += f"_{cat_filter}"
            if status_filter:
                fname += f"_{status_filter}"
            fname += ".md"
            file = discord.File(buf, filename=fname)
            try:
                await ctx.author.send(
                    content=(
                        f"\U0001F4DD **Reports dump** for **{ctx.guild.name}** "
                        f"-- {len(rows)} report(s) attached."
                    ),
                    file=file,
                )
                await ctx.reply_success(
                    f"Sent **{len(rows)}** reports to your DMs.",
                    title="Dump delivered",
                )
            except discord.Forbidden:
                await ctx.reply_error(
                    "I couldn't DM you. Enable DMs from server members and "
                    "rerun, or use `,admin reports export` for a CSV here."
                )
            except Exception:
                log.exception(
                    "admin reports dump: DM send failed gid=%s actor=%s",
                    ctx.guild_id, ctx.author.id,
                )
                await ctx.reply_error(
                    "Failed to deliver the dump -- try `,admin reports export` instead."
                )
            return

        # ── diagnose subcommand ──
        # Run an AI realness check on a single report. Reuses the per-guild
        # heal_ai_* provider settings via core.framework.ai.heal_ai so admins
        # only configure ONE provider for every AI feature.
        if parts and parts[0].lower() == "diagnose":
            if len(parts) < 2:
                await ctx.reply_error_hint(
                    "Specify a report ID.",
                    hint="admin reports diagnose 42",
                    command_name="admin reports diagnose",
                )
                return
            try:
                report_id = int(parts[1])
            except ValueError:
                await ctx.reply_error("Report ID must be a number.")
                return
            report = await self.bot.db.reports.get_report(report_id)
            if not report or int(report.get("guild_id") or 0) != int(ctx.guild_id):
                await ctx.reply_error(f"Report #{report_id} not found in this guild.")
                return
            from core.framework.ai.heal_ai import get_heal_ai_config
            from core.framework.ai import report_ai as _rai
            ai_cfg = await get_heal_ai_config(ctx.db, ctx.guild_id)
            signals = await _rai.gather_signals(ctx.db, ctx.guild_id, dict(report))
            async with ctx.typing():
                verdict = await _rai.complete_report_diagnosis(
                    dict(report), signals, ai_cfg,
                )
            if not verdict:
                await ctx.reply_error(
                    "AI diagnosis failed. Check `,admin ai heal status` -- "
                    "the provider may be misconfigured or out of quota."
                )
                return
            backend = (ai_cfg.get("backend") or "openrouter").lower()
            embed = (
                card(
                    f"\U0001F50D Report #{report_id} -- AI diagnosis",
                    color=C_INFO,
                )
                .field(
                    "Verdict (AI)",
                    f"```\n{verdict[:1000]}\n```",
                    False,
                )
                .field(
                    "Signals fed",
                    "\n".join(f"- {k}: `{v}`" for k, v in signals.items()),
                    False,
                )
                .footer(
                    f"backend={backend}  -  this is an AI guess, not a verdict. "
                    f"Treat as a triage hint."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # ── clear subcommand ──
        # ── close-old subcommand ──
        # Bulk-close reports older than N days. Status-only mutation
        # (no DELETE), so the row stays for audit and the existing
        # cleanup_closed_report_dms loop sweeps the admin DMs after
        # REPORT_DM_CLEANUP_DAYS. Mirrors the ``clear`` ergonomics:
        # confirm preview, optional category/status filter, returns the
        # affected count.
        if parts and parts[0].lower() in ("close-old", "closeold", "stale"):
            if len(parts) < 2 or not parts[1].isdigit():
                await ctx.reply_error_hint(
                    "Specify a day count.",
                    hint="admin reports close-old 30",
                    command_name="admin reports close-old",
                )
                return
            days = int(parts[1])
            if days < 1 or days > 3650:
                await ctx.reply_error("`days` must be between 1 and 3650.")
                return
            # Optional filters: status only (open / accepted / etc).
            # Category filter doesn't really fit "stale" semantics so
            # we skip it; closing only "open" reports older than 60d
            # is the canonical use case.
            status_filter: tuple[str, ...] | None = None
            if len(parts) >= 3:
                requested = parts[2].lower()
                if requested in VALID_STATUSES:
                    status_filter = (requested,)
                else:
                    await ctx.reply_error(
                        f"`{requested}` isn't a valid status. "
                        f"Valid: {', '.join(sorted(VALID_STATUSES))}."
                    )
                    return
            count = await self.bot.db.reports.count_reports_older_than(
                ctx.guild_id, days, status_in=status_filter,
            )
            if count == 0:
                filt = (
                    f" with status `{status_filter[0]}`"
                    if status_filter else ""
                )
                await ctx.reply_success(
                    f"No reports older than **{days}** day(s){filt} found.",
                    title="\U0001F5D3 Nothing to close",
                )
                return
            view = ConfirmView(ctx.author.id)
            filt_text = (
                f" status=`{status_filter[0]}`"
                if status_filter else " (all non-terminal statuses)"
            )
            msg = await ctx.reply(
                f"Close **{count}** report(s) older than **{days}** day(s)"
                f"{filt_text}? Status flips to `closed` with an "
                f"`admin_note` so the audit trail is preserved. The "
                f"rows themselves are NOT deleted -- run "
                f"`,admin reports clear closed` later if you want "
                f"those gone too.",
                view=view, mention_author=False,
            )
            await view.wait()
            if not view.value:
                await msg.edit(content="Cancelled. No reports closed.", view=None)
                return
            note = (
                f"[bulk-closed by <@{ctx.author.id}>: "
                f"older than {days} day(s)]"
            )
            rows = await self.bot.db.reports.bulk_close_reports_older_than(
                ctx.guild_id, days,
                status_in=status_filter,
                admin_note=note,
            )
            await msg.edit(
                content=(
                    f"\U0001F5D3 Closed **{len(rows)}** stale report(s). "
                    f"Status set to `closed`. Reporters are NOT DM'd "
                    f"on bulk close (would be too noisy)."
                ),
                view=None,
            )
            return

        if parts and parts[0].lower() == "clear":
            cat_filter = None
            status_filter = None
            for p in parts[1:]:
                p_lower = p.lower()
                if p_lower in VALID_CATEGORIES:
                    cat_filter = p_lower
                elif p_lower in VALID_STATUSES:
                    status_filter = p_lower

            count = await ctx.db.reports.count_reports_filtered(
                ctx.guild_id, category=cat_filter, status=status_filter,
            )
            if count == 0:
                await ctx.reply_error("No reports match that filter.")
                return

            filter_desc = ""
            if cat_filter:
                filter_desc += f" category=`{cat_filter}`"
            if status_filter:
                filter_desc += f" status=`{status_filter}`"
            if not filter_desc:
                filter_desc = " (ALL reports)"

            view = ConfirmView(ctx.author.id)
            msg = await ctx.reply(
                f"Delete **{count}** reports{filter_desc}? This cannot be undone.",
                view=view, mention_author=False,
            )
            await view.wait()
            if not view.value:
                await msg.edit(content="Cancelled.", view=None)
                return
            deleted = await ctx.db.reports.delete_reports_filtered(
                ctx.guild_id, category=cat_filter, status=status_filter,
            )
            await msg.edit(content=f"Deleted **{deleted}** reports.", view=None)
            return

        # ── dm subcommand ──
        if parts and parts[0].lower() == "dm":
            p = ctx.prefix or Config.PREFIX
            if len(parts) < 2:
                # Show current recipient
                val = await ctx.db.get_bot_config("report_dm_recipient_id")
                current_id = int(val) if val else 0
                if current_id:
                    desc = f"<@{current_id}> (`{current_id}`)"
                else:
                    default_id = Config.REPORT_TARGET_USER_ID
                    desc = f"Default - <@{default_id}> (`{default_id}`)" if default_id else "Not configured"
                embed = card("Report DM Recipient", description=desc, color=C_INFO).build()
                await ctx.reply(embed=embed, mention_author=False)
                return

            query = parts[1]
            if query.lower() in ("reset", "off", "clear"):
                await ctx.db.set_bot_config("report_dm_recipient_id", "0")
                default_id = Config.REPORT_TARGET_USER_ID
                await ctx.reply_success(
                    f"Report DM recipient reset to default (`{default_id}`)."
                )
                return

            mention_match = re.match(r"<@!?(\d+)>", query)
            if mention_match:
                user_id = int(mention_match.group(1))
            elif query.isdigit():
                user_id = int(query)
            else:
                await ctx.reply_error(
                    f"Usage: `{p}admin reports dm @user` or `{p}admin reports dm <user_id>`"
                )
                return

            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            except discord.NotFound:
                await ctx.reply_error(f"No user found with ID `{user_id}`.")
                return
            except Exception:
                await ctx.reply_error(f"Could not resolve user `{user_id}`.")
                return

            await ctx.db.set_bot_config("report_dm_recipient_id", str(user_id))
            await ctx.reply_success(
                f"Report DM notifications will now be sent to **{user}** (`{user_id}`)."
            )
            return

        # ── search subcommand ──
        if parts and parts[0].lower() == "search":
            if len(parts) < 2:
                await ctx.reply_error("Usage: `-admin reports search @user` or `-admin reports search <report#>`")
                return
            query = parts[1]

            # Check for user mention or raw user ID
            mention_match = re.match(r"<@!?(\d+)>", query)
            if mention_match:
                user_id = int(mention_match.group(1))
                rows = await self.bot.db.reports.get_reports_by_user(user_id)
                title = f"Reports by <@{user_id}>"
            elif query.isdigit():
                report = await self.bot.db.reports.get_report(int(query))
                if not report:
                    await ctx.reply_error(f"Report #{query} not found.")
                    return
                rows = [report]
                title = f"Report #{query}"
            else:
                await ctx.reply_error("Search by `@user` or report number.")
                return

            if not rows:
                await ctx.reply_success("No reports found.")
                return

            lines = []
            for r in rows:
                _ca = r["created_at"]
                _ca_ts = _ca.timestamp() if hasattr(_ca, 'timestamp') else _ca
                age = int(time.time() - _ca_ts)
                emoji = STATUS_EMOJI.get(r["status"], "❓")
                cat = r.get("category", "other")
                preview = r["message"][:60] + ("..." if len(r["message"]) > 60 else "")
                user = mention(r["user_id"], ctx.guild, self.bot)
                lines.append(
                    f"{emoji} **#{r['id']}** | {user} | `{cat}` | *{r['status']}* | {preview} | {FormatKit.time_ago(age)}"
                )

            pages = _build_report_pages(f"📋 {title} ({len(rows)})", lines)

            # Send potentially sensitive report details via DM to the requesting admin
            try:
                for page in pages:
                    await ctx.author.send(embed=page)
            except discord.Forbidden:
                await ctx.reply_error("I couldn't DM you the report results. Please check your privacy settings.")
                return

            await ctx.reply_success("Sent the report results to your DMs.")
            return

        # ── filter by category and/or status ──
        category_filter = None
        status_filter = None

        for p in parts:
            p_lower = p.lower()
            if p_lower in VALID_CATEGORIES:
                category_filter = p_lower
            elif p_lower in VALID_STATUSES:
                status_filter = p_lower
            else:
                await ctx.reply_error(
                    f"Unknown filter `{p}`. "
                    f"Categories: {', '.join(sorted(VALID_CATEGORIES))}  -  "
                    f"Statuses: {', '.join(sorted(VALID_STATUSES))}"
                )
                return

        rows = await self.bot.db.reports.get_reports_filtered(
            category=category_filter, status=status_filter,
        )

        if not rows:
            desc = []
            if category_filter:
                desc.append(f"category={category_filter}")
            if status_filter:
                desc.append(f"status={status_filter}")
            label = f" matching {', '.join(desc)}" if desc else ""
            await ctx.reply_success(f"No reports{label}.")
            return

        # Build title
        title_parts = []
        if category_filter:
            title_parts.append(category_filter.capitalize())
        if status_filter:
            title_parts.append(status_filter.replace("_", " ").title())
        title = " ".join(title_parts) + " " if title_parts else ""

        lines = []
        for r in rows:
            _ca = r["created_at"]
            _ca_ts = _ca.timestamp() if hasattr(_ca, 'timestamp') else _ca
            age = int(time.time() - _ca_ts)
            emoji = STATUS_EMOJI.get(r["status"], "❓")
            cat = r.get("category", "other")
            preview = r["message"][:60] + ("..." if len(r["message"]) > 60 else "")
            user = mention(r["user_id"], ctx.guild, self.bot)
            lines.append(
                f"{emoji} **#{r['id']}** | {user} | `{cat}` | *{r['status']}* | {preview} | {FormatKit.time_ago(age)}"
            )

        pages = _build_report_pages(f"📋 {title}Reports ({len(rows)})", lines)
        # Send the report list privately to the invoking admin instead of the channel.
        try:
            for page in pages:
                await ctx.author.send(embed=page)
        except discord.Forbidden:
            await ctx.reply_error(
                "I couldn't DM you the report list. Please enable DMs from server members or try again in a different server."
            )
        else:
            await ctx.reply_success("I've sent you the report list in DMs.")

    # ══════════════════════════════════════════════════════════════════════════
    #  Error tracker subgroup  -  admin errors
    # ══════════════════════════════════════════════════════════════════════════

    @admin.group(name="errors", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_errors(self, ctx: DiscoContext) -> None:
        """Unified error tracker across all bot subsystems.

        Usage:
          -admin errors summary           -  overview of all error sources
          -admin errors cmds [keyword]    -  recent command errors
          -admin errors cmdchains         -  recent command chain errors
          -admin errors bot               -  recent bot/event errors
          -admin errors module [name]     -  recent module/cog errors
          -admin errors search <keyword>  -  search all errors
          -admin errors export            -  export all errors as CSV
          -admin errors clear             -  clear all tracked errors
        """
        if ctx.invoked_subcommand is not None:
            return
        p = ctx.prefix or Config.PREFIX
        embed = card("🔍 Error Tracker", color=C_INFO)
        embed.description(
            "Unified error tracking across all bot subsystems.\n\n"
            f"`{p}admin errors summary`  -  error overview by source & severity\n"
            f"`{p}admin errors cmds [keyword]`  -  recent command errors\n"
            f"`{p}admin errors cmdchains [keyword]`  -  command chain errors\n"
            f"`{p}admin errors bot`  -  internal bot/event errors\n"
            f"`{p}admin errors module [name]`  -  module/cog errors\n"
            f"`{p}admin errors search <keyword>`  -  search all errors\n"
            f"`{p}admin errors clear`  -  clear all tracked errors"
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @admin_errors.command(name="summary")
    @guild_only
    @_require_manage_guild()
    async def errors_summary(self, ctx: DiscoContext) -> None:
        """Show error summary grouped by source and severity."""

        tracker = self.bot.errors
        stats = tracker.summary(ctx.guild.id)
        total_count = tracker.total_count(ctx.guild.id)

        if not stats or total_count == 0:
            await ctx.reply_error("No errors tracked for this server.")
            return

        _SEV_ICONS = {"info": "🔵", "warning": "🟠", "low": "🟢", "medium": "🟡", "high": "🔴", "critical": "💀"}
        _SRC_ICONS = {
            "cmd": "⌨️", "cmdchain": "⛓️", "bot": "🤖",
            "module": "📦", "service": "⚙️", "task": "🔄",
        }

        lines: list[str] = []
        for src in ErrorSource:
            if src.value not in stats:
                continue
            counts = stats[src.value]
            icon = _SRC_ICONS.get(src.value, "❓")
            total = sum(counts.values())
            sev_parts = []
            for sev in Severity:
                c = counts.get(sev.value, 0)
                if c > 0:
                    sev_parts.append(f"{_SEV_ICONS[sev.value]} {c}")
            line = f"{icon} **{src.value}**  -  {total} error{'s' if total != 1 else ''}"
            if sev_parts:
                line += f"\n-# {' '.join(sev_parts)}"
            lines.append(line)

        # Module breakdown
        mod_stats = tracker.module_summary(ctx.guild.id)
        if mod_stats:
            top_modules = list(mod_stats.items())[:5]
            mod_line = " · ".join(f"`{m}` ({c})" for m, c in top_modules)
            lines.append(f"\n📦 **Top modules:** {mod_line}")

        # Command breakdown
        cmd_stats = tracker.command_summary(ctx.guild.id)
        if cmd_stats:
            top_cmds = list(cmd_stats.items())[:5]
            cmd_line = " · ".join(f"`{c}` ({n})" for c, n in top_cmds)
            lines.append(f"⌨️ **Top commands:** {cmd_line}")

        embed = (
            card("📊 Error Summary", description="\n".join(lines), color=C_INFO)
            .footer(f"{total_count} total errors tracked this session")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin_errors.command(name="cmds")
    @guild_only
    @_require_manage_guild()
    async def errors_cmds(self, ctx: DiscoContext, *, keyword: str = "") -> None:
        """Show recent command execution errors."""

        await self._show_errors(ctx, ErrorSource.CMD, keyword, title="⌨️ Command Errors")

    @admin_errors.command(name="cmdchains")
    @guild_only
    @_require_manage_guild()
    async def errors_cmdchains(self, ctx: DiscoContext, *, keyword: str = "") -> None:
        """Show recent command chain errors."""

        await self._show_errors(ctx, ErrorSource.CMDCHAIN, keyword, title="⛓️ Chain Errors")

    @admin_errors.command(name="bot")
    @guild_only
    @_require_manage_guild()
    async def errors_bot(self, ctx: DiscoContext, *, keyword: str = "") -> None:
        """Show recent internal bot/event errors."""

        await self._show_errors(ctx, ErrorSource.BOT, keyword, title="🤖 Bot Errors")

    @admin_errors.command(name="module")
    @guild_only
    @_require_manage_guild()
    async def errors_module(self, ctx: DiscoContext, *, name: str = "") -> None:
        """Show recent module/cog errors, optionally filtered by module name."""

        tracker = self.bot.errors
        results = tracker.recent(
            ctx.guild.id, source=ErrorSource.MODULE,
            module=name, limit=10,
        )
        if not results:
            msg = f"No module errors" + (f" for `{name}`" if name else "") + "."
            await ctx.reply_error(msg)
            return

        lines = self._format_error_list(results, show_module=True)
        embed = (
            card(f"📦 Module Errors" + (f"  -  {name}" if name else ""), description="\n\n".join(lines), color=C_ERROR)
            .footer(f"{len(results)} error(s)")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin_errors.command(name="search")
    @guild_only
    @_require_manage_guild()
    async def errors_search(self, ctx: DiscoContext, *, keyword: str) -> None:
        """Search all errors by keyword."""
        tracker = self.bot.errors
        results = tracker.recent(ctx.guild.id, keyword=keyword, limit=10)
        if not results:
            await ctx.reply_error(f"No errors matching `{keyword}`.")
            return

        lines = self._format_error_list(results, show_source=True)
        embed = (
            card(f"🔍 Errors matching \"{keyword}\"", description="\n\n".join(lines), color=C_INFO)
            .footer(f"{len(results)} result(s)")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin_errors.command(name="export")
    @guild_only
    @_require_manage_guild()
    async def errors_export(self, ctx: DiscoContext) -> None:
        """Export all tracked errors for this server as a CSV file."""
        import io, csv as _csv, datetime as _dt

        tracker = self.bot.errors
        results = tracker.recent(ctx.guild.id, limit=500)
        if not results:
            await ctx.reply_error("No errors to export.")
            return
        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["Timestamp", "Source", "Severity", "Command", "Module", "Error Type", "Message", "User ID"])
        for e in results:
            ts = _dt.datetime.fromtimestamp(e.timestamp, tz=_dt.timezone.utc).isoformat()
            writer.writerow([
                ts, e.source.value, e.severity.value, e.command,
                e.module, e.error_type, e.message[:500], e.user_id or "",
            ])
        buf.seek(0)
        file = discord.File(io.BytesIO(buf.getvalue().encode()), filename="errors_export.csv")
        await ctx.reply(f"Exported **{len(results)}** errors.", file=file, mention_author=False)

    @admin_errors.command(name="clear")
    @guild_only
    @_require_manage_guild()
    async def errors_clear(self, ctx: DiscoContext) -> None:
        """Clear all tracked errors for this server."""
        tracker = self.bot.errors
        count = tracker.clear(ctx.guild.id)
        if count == 0:
            await ctx.reply_error("No errors to clear.")
            return
        embed = card("", description=f"🗑️ Cleared **{count}** tracked error{'s' if count != 1 else ''}.").color(C_INFO).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── Error display helpers ────────────────────────────────────────────

    async def _show_errors(
        self,
        ctx: DiscoContext,
        source,
        keyword: str,
        title: str,
    ) -> None:
        """Shared helper to display recent errors for a specific source."""
        tracker = self.bot.errors
        results = tracker.recent(
            ctx.guild.id, source=source, keyword=keyword or "", limit=10,
        )
        if not results:
            msg = "No errors" + (f" matching `{keyword}`" if keyword else "") + "."
            await ctx.reply_error(msg)
            return

        lines = self._format_error_list(results)
        embed = (
            card(title, description="\n\n".join(lines), color=C_ERROR)
            .footer(f"{len(results)} error(s)")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @staticmethod
    def _format_error_list(
        results,
        *,
        show_source: bool = False,
        show_module: bool = False,
    ) -> list[str]:
        """Format a list of ErrorRecord objects into display lines."""
        _SEV_ICONS = {"info": "🔵", "warning": "🟠", "low": "🟢", "medium": "🟡", "high": "🔴", "critical": "💀"}
        lines: list[str] = []
        for entry in results:
            sev_icon = _SEV_ICONS.get(entry.severity.value, "❓")
            parts = [f"{sev_icon}"]

            if show_source:
                parts.append(f"**[{entry.source.value}]**")
            if show_module and entry.module:
                parts.append(f"**{entry.module}**")
            if entry.command:
                parts.append(f"`{entry.command}`")

            parts.append(f" -  {entry.age_str}")

            line = " ".join(parts)
            line += f"\n-# `{entry.short_message}`"

            if entry.error_type:
                line += f"\n-# Type: `{entry.error_type}`"
            if entry.user_id:
                line += f" · User: <@{entry.user_id}>"

            lines.append(line)
        return lines

    # ════════════════════════════════════════════════════════════════════════
    #  Admin List  -  comprehensive listing subgroup
    # ════════════════════════════════════════════════════════════════════════

    @admin.group(name="list", invoke_without_command=True)
    @_require_manage_guild()
    async def admin_list(self, ctx: DiscoContext) -> None:
        """List server data. Subcommands: networks, chains, tokens, users, validators, groups, pools, items."""

        p = ctx.prefix or "."
        _b = card("📋 Admin List Commands", color=C_INFO)
        _b.field("Available Lists", "\n".join([
            f"`{p}admin list networks`  -  all networks (PoS + PoW + custom)",
            f"`{p}admin list chains`  -  all chain/blockchain configurations",
            f"`{p}admin list tokens`  -  all tokens with prices and networks",
            f"`{p}admin list users`  -  paginated user list with balances",
            f"`{p}admin list validators`  -  all validators with stats",
            f"`{p}admin list groups`  -  all mining/economy groups",
            f"`{p}admin list pools`  -  all liquidity pools with TVL",
            f"`{p}admin list items`  -  all shop items",
        ]), False)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_list.command(name="networks")
    @_require_manage_guild()
    async def admin_list_networks(self, ctx: DiscoContext) -> None:
        """List all networks  -  PoS, PoW, and custom."""

        pages = []
        lines: list[str] = []

        # Built-in PoS networks
        lines.append("**Built-in Networks (PoS/PoW):**")
        for sym, tcfg in Config.TOKENS.items():
            net = tcfg.get("network", "")
            consensus = tcfg.get("consensus", "pos")
            if net and f"• {net}" not in "\n".join(lines):
                stake_tok = Config.NETWORK_STAKE_TOKEN.get(net, " - ")
                lines.append(f"• **{net}**  -  Stake: `{stake_tok}` · Consensus: `{consensus}`")

        # PoW networks
        for net_key, ncfg in getattr(Config, "POW_NETWORKS", {}).items():
            name = ncfg.get("name", net_key)
            reward = ncfg.get("block_reward", "?")
            lines.append(f"• **{name}**  -  PoW · Block Reward: `{reward}`")

        # Custom guild networks
        custom_nets = await ctx.db.get_guild_networks(ctx.guild_id) if hasattr(ctx.db, 'get_guild_networks') else []
        if custom_nets:
            lines.append("\n**Custom Networks:**")
            for n in custom_nets:
                lines.append(f"• **{n['name']}**  -  Stake: `{n.get('stake_token', ' - ')}` [CUSTOM]")

        _b = card("🌐 All Networks", color=C_INFO)
        _b.description = "\n".join(lines)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_list.command(name="chains")
    @_require_manage_guild()
    async def admin_list_chains(self, ctx: DiscoContext) -> None:
        """List all chain configurations with block heights."""
        lines: list[str] = []
        # Gather all known network short codes
        net_shorts = set()
        for sym, tcfg in Config.TOKENS.items():
            net = tcfg.get("network", "")
            short = {"Sun Network": "sun", "Moneta Chain": "mta", "Arcadia Network": "arc",
                     "Discoin Network": "dsc"}.get(net)
            if short:
                net_shorts.add(short)
        for pow_net in getattr(Config, "POW_NETWORKS", {}):
            net_shorts.add(pow_net.lower())

        for net in sorted(net_shorts):
            latest = await ctx.db.get_latest_chain_block(ctx.guild_id, network=net)
            if latest:
                block = latest["block_num"]
                status = latest.get("status", "pending")
                lines.append(f"**{net.upper()}**  -  Block #{block:,} · Status: {status}")
            else:
                lines.append(f"**{net.upper()}**  -  No blocks yet")

        _b = card("⛓ Chain Status", color=C_INFO)
        _b.description = "\n".join(lines) or "No chain data found."
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_list.command(name="tokens")
    @_require_manage_guild()
    async def admin_list_tokens(self, ctx: DiscoContext) -> None:
        """List all tokens with prices (same as admin listtokens but via list subgroup)."""
        await self.admin_listtokens(ctx)

    @admin_list.command(name="users")
    @_require_manage_guild()
    async def admin_list_users(self, ctx: DiscoContext, network: str = "") -> None:
        """List users, optionally filtered by network. Usage: .admin list users [network]"""

        if network:
            net = network.lower()
            # Users with wallets on this network
            addrs = await ctx.db.fetch_all(
                "SELECT DISTINCT user_id FROM wallet_addresses WHERE guild_id=$1 AND LOWER(address) LIKE $2||':%'",
                ctx.guild_id, net,
            )
            user_ids = [r["user_id"] for r in addrs]
            title = f"👥 Users on {network.upper()} ({len(user_ids)})"
        else:
            all_users = await ctx.db.fetch_all(
                "SELECT user_id, wallet, bank FROM users WHERE guild_id=$1 ORDER BY wallet + bank DESC LIMIT 100",
                ctx.guild_id,
            )
            user_ids = None
            title = f"👥 All Users ({len(all_users)})"

        pages = []
        lines: list[str] = []
        items = user_ids if user_ids is not None else all_users
        for i, item in enumerate(items):
            if user_ids is not None:
                uid = item
                u = await ctx.db.get_user(uid, ctx.guild_id)
                bal = fmt_usd(u.h('wallet')) if u else fmt_usd(0)
                lines.append(f"{i+1}. <@{uid}>  -  {bal}")
            else:
                uid = item["user_id"]
                total = item.h("wallet") + item.h("bank")
                lines.append(f"{i+1}. <@{uid}>  -  {fmt_usd(total)}")

            if len(lines) >= 20:
                _b = card(title, color=C_INFO)
                _b.description = "\n".join(lines)
                pages.append(_b.build())
                lines = []

        if lines:
            _b = card(title, color=C_INFO)
            _b.description = "\n".join(lines)
            pages.append(_b.build())

        if not pages:
            await ctx.reply_error("No users found.")
            return
        await send_paginated(ctx, pages)

    @admin_list.command(name="validators")
    @_require_manage_guild()
    async def admin_list_validators(self, ctx: DiscoContext) -> None:
        """List all validators with APY and uptime."""
        validators = await ctx.db.get_validators(ctx.guild_id)
        if not validators:
            await ctx.reply_error("No validators registered.")
            return
        lines: list[str] = []
        for v in validators:
            apy = v.get("reward_rate", 0) * 365 * 24 * 100  # hourly rate → annual %
            uptime = v.get("uptime_rate", 0) * 100
            network = v.get("network", "?")
            lines.append(
                f"• **{v['validator_id']}** ({v.get('name', '?')}) "
                f" -  {network} · APY: {apy:.1f}% · Uptime: {uptime:.0f}%"
            )
        _b = card(f"🏛 Validators ({len(validators)})", color=C_INFO)
        _b.description = "\n".join(lines[:30])
        if len(lines) > 30:
            _b.footer(f"Showing first 30 of {len(lines)}")
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_list.command(name="groups")
    @_require_manage_guild()
    async def admin_list_groups(self, ctx: DiscoContext) -> None:
        """List all mining/economy groups."""
        groups = await ctx.db.fetch_all(
            "SELECT * FROM mining_groups WHERE guild_id=$1 ORDER BY created_at DESC",
            ctx.guild_id,
        )
        if not groups:
            await ctx.reply_error("No groups found.")
            return
        lines: list[str] = []
        for g in groups:
            members = await ctx.db.fetch_one(
                "SELECT COUNT(*) AS cnt FROM mining_group_members WHERE group_id=$1 AND guild_id=$2",
                g["group_id"], ctx.guild_id,
            )
            mc = members["cnt"] if members else 0
            lines.append(f"• **{g['name']}** (ID: `{g['group_id']}`)  -  {mc} members · Founder: <@{g['founder_id']}>")
        _b = card(f"👥 Groups ({len(groups)})", color=C_INFO)
        _b.description = "\n".join(lines[:25])
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_list.command(name="pools")
    @_require_manage_guild()
    async def admin_list_pools(self, ctx: DiscoContext) -> None:
        """List all liquidity pools with TVL."""
        pools = await ctx.db.get_all_pools(ctx.guild_id)
        if not pools:
            await ctx.reply_error("No pools found.")
            return
        lines: list[str] = []
        for p in pools:
            a, b = p.get("token_a", "?"), p.get("token_b", "?")
            ra, rb = p.get("reserve_a", 0), p.get("reserve_b", 0)
            lines.append(f"• **{a}/{b}**  -  Reserves: {ra:,.2f} {a} / {rb:,.2f} {b}")
        _b = card(f"💧 Pools ({len(pools)})", color=C_INFO)
        _b.description = "\n".join(lines[:25])
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_list.command(name="items")
    @_require_manage_guild()
    async def admin_list_items(self, ctx: DiscoContext) -> None:
        """List all shop items."""
        items = list(Config.SHOP_ITEMS.items()) if hasattr(Config, 'SHOP_ITEMS') else []
        if not items:
            await ctx.reply_error("No shop items configured.")
            return
        lines: list[str] = []
        for key, item in items:
            price = item.get("price", "?")
            currency = item.get("currency", "USD")
            lines.append(f"• **{item.get('name', key)}**  -  {price} {currency}")
        _b = card(f"🛒 Shop Items ({len(items)})", color=C_INFO)
        _b.description = "\n".join(lines[:25])
        await ctx.reply(embed=_b.build(), mention_author=False)

    # ========================================================================
    # Announcements - send messages from the bot to channels or DMs
    # ========================================================================

    @admin.command(name="announce")
    @guild_only
    @_require_manage_guild()
    async def announce(self, ctx: DiscoContext, channel: discord.TextChannel, *, message: str) -> None:
        """Send an official announcement to a channel as the bot.
        Usage: .admin announce #channel Your message here"""
        embed = card("Discoin Announcement", color=C_GOLD)
        embed.description(message)
        embed.footer(f"From {ctx.author.display_name}")
        built = embed.build()
        built.timestamp = discord.utils.utcnow()
        await channel.send(embed=built)
        await ctx.reply_success(f"Announcement sent to {channel.mention}.")

    @admin.command(name="dm")
    @guild_only
    @_require_manage_guild()
    async def dm_user(self, ctx: DiscoContext, member: discord.Member, *, message: str) -> None:
        """Send a DM to a player from the bot.
        Usage: .admin dm @user Your message here"""
        embed = card("Message from Discoin", color=C_GOLD)
        embed.description(message)
        embed.footer(f"From {ctx.guild.name}")
        built = embed.build()
        built.timestamp = discord.utils.utcnow()
        try:
            await member.send(embed=built)
            await ctx.reply_success(f"DM sent to {member.display_name}.")
        except discord.Forbidden:
            await ctx.reply_error(f"Can't DM {member.display_name}. They may have DMs disabled.")

    @admin.command(name="commandstats", aliases=["cmdstats", "usagestats"])
    @guild_only
    @_require_manage_guild()
    async def admin_commandstats(self, ctx: DiscoContext) -> None:
        """DM a text dump of command usage stats for this server.

        Sections: all-time totals (persisted across resets), last 7 days,
        last 24 hours. Each window groups by command path (top-level
        command + subcommand) and lists the most popular argument
        variants underneath, so admins can see how active each game /
        feature is at a glance."""
        import io
        gid = ctx.guild.id

        totals_rows = await ctx.db.fetch_all(
            "SELECT command_path, args_text, total_count, first_seen, last_seen "
            "FROM command_usage_totals WHERE guild_id = $1 "
            "ORDER BY total_count DESC",
            gid,
        )
        week_rows = await ctx.db.fetch_all(
            "SELECT command_path, args_text, COUNT(*)::BIGINT AS cnt "
            "FROM command_usage "
            "WHERE guild_id = $1 AND used_at >= NOW() - INTERVAL '7 days' "
            "GROUP BY command_path, args_text "
            "ORDER BY cnt DESC",
            gid,
        )
        day_rows = await ctx.db.fetch_all(
            "SELECT command_path, args_text, COUNT(*)::BIGINT AS cnt "
            "FROM command_usage "
            "WHERE guild_id = $1 AND used_at >= NOW() - INTERVAL '24 hours' "
            "GROUP BY command_path, args_text "
            "ORDER BY cnt DESC",
            gid,
        )

        if not totals_rows and not week_rows and not day_rows:
            return await ctx.reply_error(
                "No command usage data has been recorded yet. "
                "Run a few commands and try again."
            )

        def _count(r: dict) -> int:
            return int(r.get("total_count") or r.get("cnt") or 0)

        def _section(rows: list[dict], heading: str) -> list[str]:
            lines: list[str] = ["", "=" * 60, heading, "=" * 60]
            if not rows:
                lines.append("(no data in window)")
                return lines
            total = sum(_count(r) for r in rows)
            uniq_cmds = len({str(r.get("command_path") or "?") for r in rows})
            lines.append(f"Total invocations: {total}")
            lines.append(f"Unique command paths: {uniq_cmds}")
            lines.append("")
            by_cmd: dict[str, list[dict]] = {}
            for r in rows:
                by_cmd.setdefault(str(r.get("command_path") or "?"), []).append(r)
            cmd_totals = [(cmd, sum(_count(r) for r in rs), rs) for cmd, rs in by_cmd.items()]
            cmd_totals.sort(key=lambda x: x[1], reverse=True)
            lines.append("By command path (top-level + subcommands):")
            for cmd, ctot, _rs in cmd_totals:
                lines.append(f"  {ctot:>8}   {cmd}")
            lines.append("")
            lines.append("By command + arguments (top variants per command):")
            for cmd, ctot, rs in cmd_totals:
                rs.sort(key=lambda r: _count(r), reverse=True)
                lines.append(f"  {cmd}  (total {ctot})")
                for r in rs[:10]:
                    args = (r.get("args_text") or "").strip()
                    label = f"{cmd} {args}".rstrip()
                    lines.append(f"      {_count(r):>6}   {label}")
                if len(rs) > 10:
                    lines.append(f"      ... {len(rs) - 10} more variant(s)")
                lines.append("")
            return lines

        out: list[str] = []
        out.append("Discoin Command Usage Report")
        out.append(f"Generated:  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        out.append(f"Server:     {ctx.guild.name} ({ctx.guild.id})")
        out.append(f"Requested by: {ctx.author} ({ctx.author.id})")
        out.extend(_section(totals_rows, "ALL-TIME (persistent across resets)"))
        out.extend(_section(week_rows, "LAST 7 DAYS"))
        out.extend(_section(day_rows, "LAST 24 HOURS"))

        buf = io.BytesIO("\n".join(out).encode("utf-8"))
        fname = f"command_usage_{ctx.guild.id}.txt"
        file = discord.File(buf, filename=fname)
        try:
            await ctx.author.send(
                content=(
                    f"\U0001F4CA **Command usage** for **{ctx.guild.name}** "
                    "-- all-time / 7 days / 24 hours dump attached."
                ),
                file=file,
            )
            await ctx.reply_success(
                "Sent the command usage dump to your DMs.",
                title="Dump delivered",
            )
        except discord.Forbidden:
            await ctx.reply_error(
                "I couldn't DM you. Enable DMs from server members and rerun."
            )
        except Exception:
            log.exception(
                "admin commandstats: DM send failed gid=%s actor=%s",
                ctx.guild_id, ctx.author.id,
            )
            await ctx.reply_error("Failed to deliver the dump -- try again shortly.")

    # ========================================================================
    # NFT Collection Management
    # ========================================================================

    @admin.group(name="nft", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_nft(self, ctx: DiscoContext) -> None:
        """Manage NFT collections."""
        p = ctx.prefix or "."
        await ctx.reply(
            f"**NFT Admin Commands:**\n"
            f"`{p}admin nft create <symbol> <name> <network> <mint_price> <mint_token> [max_supply]`\n"
            f"`{p}admin nft setimage <symbol> <image_url>`\n"
            f"`{p}admin nft delete <symbol>`  -  delete empty collection\n"
            f"`{p}admin nft purge <symbol>`  -  force-delete collection + all minted NFTs",
            mention_author=False,
        )

    @admin_nft.command(name="create")
    @guild_only
    @_require_manage_guild()
    async def admin_nft_create(
        self, ctx: DiscoContext, symbol: str, name: str, network: str,
        mint_price: float, mint_token: str, max_supply: int = None,
    ) -> None:
        """Create an NFT collection.
        Usage: .admin nft create PUNKS "Discoin Punks" ARC 0.05 ARC 100"""
        symbol = symbol.upper()
        network = network.upper()
        mint_token = mint_token.upper()

        if network not in ("ARC", "DSC"):
            await ctx.reply_error("Network must be ARC or DSC.")
            return
        if mint_price < 0:
            await ctx.reply_error("Mint price can't be negative.")
            return
        if max_supply is not None and max_supply < 1:
            await ctx.reply_error("Max supply must be at least 1.")
            return

        existing = await ctx.db.get_collection_by_symbol(ctx.guild_id, symbol)
        if existing:
            await ctx.reply_error(f"A collection with symbol `{symbol}` already exists.")
            return

        col = await ctx.db.create_collection(
            guild_id=ctx.guild_id,
            name=name,
            symbol=symbol,
            network=network,
            description="",
            image_url="",
            max_supply=max_supply,
            mint_price=mint_price,
            mint_token=mint_token,
            creator_id=ctx.author.id,
        )

        supply_str = str(max_supply) if max_supply else "Unlimited"
        contract_addr = col.get("contract_address", "")
        _b = card("NFT Collection Created", color=C_SUCCESS)
        _b.field("Name", name, True)
        _b.field("Symbol", symbol, True)
        _b.field("Network", network, True)
        _b.field("Mint Price", f"{mint_price:,.4f} {mint_token}", True)
        _b.field("Max Supply", supply_str, True)
        _b.field("Contract", "ERC-721", True)
        if contract_addr:
            _b.field("Address", f"`{contract_addr}`", False)
        _b.footer(f"Collection ID: {col['id']} | Players mint with {ctx.prefix}nft mint {symbol}")
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_nft.command(name="setimage")
    @guild_only
    @_require_manage_guild()
    async def admin_nft_setimage(self, ctx: DiscoContext, symbol: str, *, image_url: str) -> None:
        """Set the image for an NFT collection.
        Usage: .admin nft setimage PUNKS https://example.com/image.png"""
        col = await ctx.db.get_collection_by_symbol(ctx.guild_id, symbol.upper())
        if not col:
            await ctx.reply_error(f"Collection `{symbol.upper()}` not found.")
            return
        await ctx.db.update_collection_image(col["id"], image_url.strip())
        await ctx.reply_success(f"Image updated for **{col['name']}**.")

    @admin_nft.command(name="delete")
    @guild_only
    @_require_manage_guild()
    async def admin_nft_delete(self, ctx: DiscoContext, symbol: str) -> None:
        """Delete an NFT collection (only if no NFTs have been minted).
        Usage: .admin nft delete PUNKS"""
        col = await ctx.db.get_collection_by_symbol(ctx.guild_id, symbol.upper())
        if not col:
            await ctx.reply_error(f"Collection `{symbol.upper()}` not found.")
            return
        if col["minted_count"] > 0:
            await ctx.reply_error(
                f"Can't delete **{col['name']}** - {col['minted_count']} NFTs have been minted.\n"
                f"Use `{ctx.prefix}admin nft purge {symbol.upper()}` to force-delete with all NFTs."
            )
            return
        await ctx.db.execute(
            "DELETE FROM nft_collections WHERE id = $1", col["id"],
        )
        await ctx.reply_success(f"Deleted collection **{col['name']}** ({symbol.upper()}).")

    @admin_nft.command(name="purge")
    @guild_only
    @_require_manage_guild()
    async def admin_nft_purge(self, ctx: DiscoContext, symbol: str) -> None:
        """Force-delete an NFT collection and ALL its minted NFTs, listings, and sales.
        Usage: .admin nft purge PUNKS"""
        col = await ctx.db.get_collection_by_symbol(ctx.guild_id, symbol.upper())
        if not col:
            await ctx.reply_error(f"Collection `{symbol.upper()}` not found.")
            return

        minted = col["minted_count"]
        confirmed = await ctx.confirm(
            f"**This will permanently delete:**\n"
            f"Collection **{col['name']}** (`{symbol.upper()}`)\n"
            f"**{minted}** minted NFT{'s' if minted != 1 else ''}, all listings, and all sale history.\n\n"
            f"This **cannot be undone**. Players will lose their NFTs with no refund."
        )
        if not confirmed:
            await ctx.reply_error("Purge cancelled.")
            return

        counts = await ctx.db.delete_collection_with_nfts(col["id"])
        nft_count = counts.get("nfts", 0)
        listing_count = counts.get("nft_listings", 0)
        sale_count = counts.get("nft_sales", 0)

        _b = card("Collection Purged", color=C_ERROR)
        _b.field("Collection", f"{col['name']} ({symbol.upper()})", False)
        _b.field("NFTs Deleted", str(nft_count), True)
        _b.field("Listings Cleared", str(listing_count), True)
        _b.field("Sales Records", str(sale_count), True)
        await ctx.reply(embed=_b.build(), mention_author=False)

    # ========================================================================
    # Prediction Market Management
    # ========================================================================

    @admin.group(name="predict", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_predict(self, ctx: DiscoContext) -> None:
        """Manage prediction markets."""
        p = ctx.prefix or "."
        await ctx.reply(
            f"**Prediction Admin Commands:**\n"
            f'`{p}admin predict create "<question>" <days> [prize_pool] [description]`\n'
            f"`{p}admin predict generate [count=3] [days=7]`  -  AI auto-generate markets\n"
            f"`{p}admin predict setpool <id> <amount>`  -  update prize pool\n"
            f"`{p}admin predict settime <id> <days_from_now>`  -  update end time\n"
            f"`{p}admin predict resolve <id> <YES|NO>`\n"
            f"`{p}admin predict cancel <id>`\n"
            f"`{p}admin predict close <id>`",
            mention_author=False,
        )

    @admin_predict.command(name="create")
    @guild_only
    @_require_manage_guild()
    async def admin_predict_create(
        self, ctx: DiscoContext, question: str, days: int, prize_pool: float = 0.0, *, description: str = "",
    ) -> None:
        """Create a prediction market.
        Usage: .admin predict create "Question?" <days> [prize_pool] [description]
        Example: .admin predict create "Will MTA hit 100k?" 30 5000 Optional description"""
        if days < 1 or days > 365:
            await ctx.reply_error("Duration must be between 1 and 365 days.")
            return
        if len(question) < 10:
            await ctx.reply_error("Question is too short. Make it descriptive.")
            return
        if prize_pool < 0:
            await ctx.reply_error("Prize pool cannot be negative.")
            return

        end_time = datetime.now(timezone.utc) + timedelta(days=days)

        market = await ctx.db.create_market(
            guild_id=ctx.guild_id,
            question=question,
            description=description,
            category="general",
            options=["YES", "NO"],
            end_time=end_time,
            created_by=ctx.author.id,
            prize_pool=prize_pool,
        )

        _b = card("Prediction Market Created", color=C_SUCCESS)
        _b.field("Question", question, False)
        if description:
            _b.field("Description", description, False)
        _b.field("Options", "YES / NO", True)
        _b.field("Ends", fmt_ts(end_time, "%b %d, %Y %H:%M UTC"), True)
        _b.field("Market ID", str(market["id"]), True)
        if prize_pool > 0:
            _b.field("Prize Pool", fmt_usd(prize_pool), True)
        _b.footer(f"Players can bet with {ctx.prefix}predict bet {market['id']} <YES|NO> <amount>")
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_predict.command(name="setpool")
    @guild_only
    @_require_manage_guild()
    async def admin_predict_setpool(self, ctx: DiscoContext, market_id: int, prize_pool: float) -> None:
        """Update the prize pool of an open prediction market.
        Usage: .admin predict setpool <id> <amount>"""
        market = await ctx.db.get_market(market_id)
        if not market or market["guild_id"] != ctx.guild_id:
            await ctx.reply_error("Market not found.")
            return
        if market["status"] not in ("open", "closed"):
            await ctx.reply_error(f"Market is already {market['status']}.")
            return
        if prize_pool < 0:
            await ctx.reply_error("Prize pool cannot be negative.")
            return
        await ctx.db.update_market_pool(market_id, prize_pool)
        new_total = float(market["total_pool"]) - float(market.get("prize_pool") or 0) + prize_pool
        _b = card("Prize Pool Updated", color=C_SUCCESS)
        _b.field("Market", market["question"][:80], False)
        _b.field("New Prize Pool", fmt_usd(prize_pool), True)
        _b.field("New Total Pool", fmt_usd(new_total), True)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_predict.command(name="settime")
    @guild_only
    @_require_manage_guild()
    async def admin_predict_settime(self, ctx: DiscoContext, market_id: int, days_from_now: int) -> None:
        """Update the end time of an open prediction market.
        Usage: .admin predict settime <id> <days_from_now>"""
        market = await ctx.db.get_market(market_id)
        if not market or market["guild_id"] != ctx.guild_id:
            await ctx.reply_error("Market not found.")
            return
        if market["status"] != "open":
            await ctx.reply_error(f"Market is {market['status']}  -  can only change time for open markets.")
            return
        if days_from_now < 1 or days_from_now > 365:
            await ctx.reply_error("Days must be between 1 and 365.")
            return
        new_end = datetime.now(timezone.utc) + timedelta(days=days_from_now)
        await ctx.db.update_market_end_time(market_id, new_end)
        _b = card("Market End Time Updated", color=C_SUCCESS)
        _b.field("Market", market["question"][:80], False)
        _b.field("New End Time", fmt_ts(new_end, "%b %d, %Y %H:%M UTC"), True)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_predict.command(name="resolve")
    @guild_only
    @_require_manage_guild()
    async def admin_predict_resolve(self, ctx: DiscoContext, market_id: int, winning_option: str) -> None:
        """Resolve a prediction market and pay out winners.
        Usage: .admin predict resolve 1 YES"""
        market = await ctx.db.get_market(market_id)
        if not market or market["guild_id"] != ctx.guild_id:
            await ctx.reply_error("Market not found.")
            return
        if market["status"] not in ("open", "closed"):
            await ctx.reply_error(f"Market is already {market['status']}.")
            return

        options = json.loads(market["options"]) if isinstance(market["options"], str) else market["options"]
        winning_option = winning_option.upper()
        if winning_option not in [o.upper() for o in options]:
            await ctx.reply_error(f"Invalid option. Choose from: {', '.join(options)}")
            return
        winning_option = next(o for o in options if o.upper() == winning_option.upper())

        # Calculate payouts BEFORE resolving (so we can abort on error)
        pools = await ctx.db.get_market_pools(market_id)
        total_pool = sum(pools.values())
        winning_pool = pools.get(winning_option, 0.0)

        house_amount = total_pool * 0.05  # 5% house cut
        payout_pool = total_pool - house_amount

        # Resolve the market first (prevents new bets)
        await ctx.db.resolve_market(market_id, winning_option)

        # Distribute payouts to winners
        winners_paid = 0
        payout_errors = 0
        if winning_pool > 0:
            winning_bets = await ctx.db.get_winning_bets(market_id, winning_option)
            for bet in winning_bets:
                share = float(bet["amount"]) / winning_pool
                payout = round(share * payout_pool, 2)
                try:
                    await ctx.db.update_wallet(bet["user_id"], ctx.guild_id, to_raw(payout))
                    winners_paid += 1
                except Exception:
                    payout_errors += 1

        # DM users who had bets on this market (if dm_predictions is True)
        all_bets = await ctx.db.get_all_bets_for_market(market_id)
        notified_users = set()
        for bet in all_bets:
            uid = bet["user_id"]
            if uid in notified_users:
                continue
            notified_users.add(uid)
            try:
                prefs = await ctx.db.get_user_prefs(uid, ctx.guild_id)
                if not prefs.get("dm_predictions", 0):
                    continue
                member = ctx.guild.get_member(uid)
                if not member:
                    continue
                bet_option = bet["option"]
                won = bet_option.upper() == winning_option.upper()
                if won:
                    share = float(bet["amount"]) / winning_pool if winning_pool > 0 else 0
                    payout = round(share * payout_pool, 2)
                    dm_msg = (
                        f"**Prediction Resolved** in {ctx.guild.name}\n"
                        f"**{market['question']}**\n"
                        f"Result: **{winning_option}** -- You won **${payout:,.2f}**!"
                    )
                else:
                    dm_msg = (
                        f"**Prediction Resolved** in {ctx.guild.name}\n"
                        f"**{market['question']}**\n"
                        f"Result: **{winning_option}** -- Your bet on **{bet_option}** lost."
                    )
                await member.send(dm_msg)
            except Exception:
                pass

        # Send house cut to treasury
        if house_amount > 0:
            try:
                await ctx.db.add_to_treasury(ctx.guild_id, house_amount)
            except Exception:
                pass  # treasury might not exist yet

        _b = card("Market Resolved", color=C_SUCCESS)
        _b.field("Question", market["question"], False)
        _b.field("Winner", winning_option, True)
        _b.field("Total Pool", fmt_usd(total_pool), True)
        _b.field("Winners Paid", str(winners_paid), True)
        _b.field("House Cut (5%)", fmt_usd(house_amount), True)
        if payout_errors > 0:
            _b.field("⚠️ Errors", f"{payout_errors} payouts failed  -  check with `.admin health`", False)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_predict.command(name="cancel")
    @guild_only
    @_require_manage_guild()
    async def admin_predict_cancel(self, ctx: DiscoContext, market_id: int) -> None:
        """Cancel a prediction market and refund all bets.
        Usage: .admin predict cancel 1"""
        market = await ctx.db.get_market(market_id)
        if not market or market["guild_id"] != ctx.guild_id:
            await ctx.reply_error("Market not found.")
            return
        if market["status"] not in ("open", "closed"):
            await ctx.reply_error(f"Market is already {market['status']}.")
            return

        # Refund all bets first, then mark cancelled
        all_bets = await ctx.db.get_all_bets_for_market(market_id)
        refunded = 0
        refund_errors = 0
        total_refunded = 0.0
        for bet in all_bets:
            amt = float(bet["amount"])
            try:
                await ctx.db.update_wallet(bet["user_id"], ctx.guild_id, to_raw(amt))
                refunded += 1
                total_refunded += amt
            except Exception:
                refund_errors += 1

        # Only cancel after all refunds are attempted
        await ctx.db.cancel_market(market_id)

        _b = card("Market Cancelled", color=C_WARNING)
        _b.field("Question", market["question"], False)
        _b.field("Refunded", f"{refunded} bets", True)
        _b.field("Total Refunded", fmt_usd(total_refunded), True)
        if refund_errors > 0:
            _b.field("⚠️ Errors", f"{refund_errors} refunds failed  -  check user accounts manually", False)
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_predict.command(name="close")
    @guild_only
    @_require_manage_guild()
    async def admin_predict_close(self, ctx: DiscoContext, market_id: int) -> None:
        """Close a prediction market (no new bets, awaiting resolution).
        Usage: .admin predict close 1"""
        market = await ctx.db.get_market(market_id)
        if not market or market["guild_id"] != ctx.guild_id:
            await ctx.reply_error("Market not found.")
            return
        if market["status"] != "open":
            await ctx.reply_error(f"Market is already {market['status']}.")
            return
        await ctx.db.close_market(market_id)
        await ctx.reply_success(f"Market #{market_id} closed. No new bets. Use `{ctx.prefix}admin predict resolve {market_id} <option>` to resolve.")

    @admin_predict.command(name="generate", aliases=["ai", "auto"])
    @guild_only
    @_require_manage_guild()
    async def admin_predict_generate(self, ctx: DiscoContext, count: int = 3, days: int = 7) -> None:
        """Auto-generate prediction markets using AI based on current trends.

        The AI generates YES/NO questions about crypto, finance, gaming, or current events.
        Each market is created with the given duration in days.

        Usage: .admin predict generate [count=3] [days=7]
        Example: .admin predict generate 5 14  -- generate 5 markets closing in 14 days"""
        if not 1 <= count <= 5:
            await ctx.reply_error("Count must be between 1 and 5.")
            return
        if not 1 <= days <= 90:
            await ctx.reply_error("Days must be between 1 and 90.")
            return

        thinking_msg = await ctx.reply(
            embed=card(
                "Generating Prediction Markets",
                description=f"Asking AI to generate {count} market{'s' if count != 1 else ''}...",
                color=C_INFO,
            ).build(),
            mention_author=False,
        )

        _PREDICT_SYSTEM = (
            "You are generating prediction market questions for a Discord economy game. "
            "Players bet on YES or NO outcomes using in-game currency. "
            "Create questions that are clear, verifiable, specific, and interesting. "
            "Topics: cryptocurrency prices, market events, gaming outcomes, finance, or trending news. "
            "Questions must be answerable with YES or NO within the time window given. "
            "Do not reference exact dates - use relative language like 'by end of this month'. "
            "Respond ONLY with a JSON array, no other text."
        )
        prompt = (
            f"Generate exactly {count} YES/NO prediction market questions "
            f"closeable within {days} days from today. "
            f"Format: "
            f'[{{"question": "...", "description": "Brief context (1-2 sentences).", "category": "crypto|finance|gaming|general"}}]'
        )

        from core.framework.ai.client import complete_tools
        raw = await complete_tools(
            [
                {"role": "system", "content": _PREDICT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=800,
            temperature=0.9,
        )

        if not raw:
            await thinking_msg.edit(
                embed=card(
                    "AI Unavailable",
                    description=(
                        "Could not reach the AI backend. Check that `OPENROUTER_API_KEY` "
                        "is set, or configure a local model with `.admin ai heal`.\n\n"
                        "Use `.admin predict create` to create markets manually."
                    ),
                    color=C_ERROR,
                ).build(),
            )
            return

        # Strip markdown code fences if the model wrapped in ```json
        import re as _re
        raw = _re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("`").strip()

        try:
            proposals = json.loads(raw)
            if not isinstance(proposals, list):
                raise ValueError("Expected a JSON array")
        except Exception:
            await thinking_msg.edit(
                embed=card(
                    "Parse Error",
                    description=(
                        "The AI returned an unexpected format. Try again or use "
                        "`.admin predict create` to create markets manually.\n\n"
                        f"Raw response (first 300 chars):\n```{raw[:300]}```"
                    ),
                    color=C_ERROR,
                ).build(),
            )
            return

        end_time = datetime.now(timezone.utc) + timedelta(days=days)
        created = []
        errors = []
        for p_data in proposals[:count]:
            question = str(p_data.get("question", "")).strip()
            description = str(p_data.get("description", "")).strip()
            category = str(p_data.get("category", "general")).strip().lower()
            if category not in ("crypto", "finance", "gaming", "general"):
                category = "general"
            if not question or len(question) < 10:
                errors.append(f"Skipped: question too short - {question[:50]!r}")
                continue
            if len(question) > 200:
                question = question[:200]
            try:
                market = await ctx.db.create_market(
                    guild_id=ctx.guild_id,
                    question=question,
                    description=description,
                    category=category,
                    options=["YES", "NO"],
                    end_time=end_time,
                    created_by=ctx.author.id,
                    prize_pool=0.0,
                )
                created.append((market["id"], question, category))
            except Exception as exc:
                errors.append(f"DB error for {question[:50]!r}: {exc}")

        if not created:
            await thinking_msg.edit(
                embed=card(
                    "No Markets Created",
                    description=(
                        "The AI responded but no valid questions could be parsed.\n\n"
                        + ("\n".join(errors) if errors else "Unknown error.")
                    ),
                    color=C_ERROR,
                ).build(),
            )
            return

        _b = card(
            f"Generated {len(created)} Prediction Market{'s' if len(created) != 1 else ''}",
            color=C_SUCCESS,
        )
        _b.field("Closes In", f"{days} days", True)
        _b.field("Created By", "AI auto-generate", True)
        for mid, question, category in created:
            _b.field(f"#{mid} [{category}]", question[:100], False)
        if errors:
            _b.field("Skipped", "\n".join(errors[:3]), False)
        _b.footer(f"Players bet with {ctx.prefix}predict list  |  Resolve with {ctx.prefix}admin predict resolve <id> YES/NO")
        await thinking_msg.edit(embed=_b.build())

        # Post to predictions channel if configured
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        pred_ch_id = settings.get("predictions_channel")
        if pred_ch_id:
            pred_ch = ctx.guild.get_channel(pred_ch_id)
            if pred_ch:
                try:
                    ann_embed = card(
                        f"New Prediction Markets Open ({len(created)})",
                        color=C_GOLD,
                    )
                    for mid, question, category in created:
                        ann_embed.field(f"#{mid}", question[:100], False)
                    ann_embed.footer(f"Bet with {ctx.prefix}predict bet <id> <YES|NO> <amount>  |  Closes in {days} days")
                    await pred_ch.send(embed=ann_embed.build())
                except Exception:
                    pass

    # ── admin event ───────────────────────────────────────────────────────────

    @admin.group(name="event", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_event(self, ctx: DiscoContext) -> None:
        """Manage market events. Usage: .admin event <subcommand>"""
        p = ctx.prefix
        await ctx.reply_error(
            f"Usage:\n"
            f"`{p}admin event status`  -  current event + settings\n"
            f"`{p}admin event trigger <type>`  -  start an event\n"
            f"`{p}admin event clear`  -  end current event\n"
            f"`{p}admin event list`  -  all event types\n"
            f"`{p}admin event disable <type>`  -  block an event from random triggers\n"
            f"`{p}admin event enable <type>`  -  re-enable a disabled event\n"
            f"`{p}admin event frequency <value>`  -  set random trigger chance per tick"
        )

    @admin_event.command(name="trigger", aliases=["start"])
    @guild_only
    @_require_manage_guild()
    async def admin_event_trigger(self, ctx: DiscoContext, event_type: str) -> None:
        """Trigger a market event. Usage: .admin event trigger bull_run"""
        from configs.market_events_config import EVENT_REGISTRY
        from cogs.events import trigger_event, _event_embed
        from services.market_event_engine import end_event
        event_type = event_type.lower().replace(" ", "_")
        if event_type not in EVENT_REGISTRY:
            valid = ", ".join(f"`{k}`" for k in EVENT_REGISTRY)
            await ctx.reply_error(f"Unknown event type `{event_type}`. Valid: {valid}")
            return

        # Clear any existing event first
        _redis = getattr(self.bot.bus, "_redis", None) if hasattr(self.bot, "bus") else None
        await end_event(_redis, ctx.guild_id, cancelled=True)
        await ctx.db.clear_guild_event(ctx.guild_id)
        await trigger_event(ctx.db, ctx.guild, event_type, bot=self.bot)

        ev = EVENT_REGISTRY[event_type]
        embed = _event_embed(event_type, ev.total_duration_seconds)
        await ctx.reply(
            content=f"**{ev.emoji} Market event triggered: {ev.display_name}**",
            embed=embed, mention_author=False,
        )
        # Drop a fresh server calendar in the bot channel so players see
        # the new event alongside any active challenges + recurring
        # resets without running ,calendar themselves. Best-effort.
        try:
            from cogs.calendar import post_calendar_to_bot_channel
            await post_calendar_to_bot_channel(self.bot, ctx.guild)
        except Exception:
            log.debug("admin event trigger: calendar auto-post failed",
                      exc_info=True)

    @admin_event.command(name="clear", aliases=["stop", "end"])
    @guild_only
    @_require_manage_guild()
    async def admin_event_clear(self, ctx: DiscoContext) -> None:
        """End the current market event early."""
        from services.market_event_engine import get_active_event, end_event
        _redis = getattr(self.bot.bus, "_redis", None) if hasattr(self.bot, "bus") else None
        ae = await get_active_event(_redis, ctx.guild_id)
        if ae is None:
            await ctx.reply_error("No active market event to clear.")
            return
        event_key = ae.event_id
        end_prices: dict[str, float] = {}
        try:
            prices = await ctx.db.get_all_prices(ctx.guild_id)
            end_prices = {r["symbol"]: float(r["price"]) for r in prices}
        except Exception:
            pass
        await end_event(_redis, ctx.guild_id, cancelled=True, end_prices=end_prices)
        await ctx.db.clear_guild_event(ctx.guild_id)
        await ctx.reply_success(f"Market event **{event_key}** cleared. Markets returning to normal.")

    @admin_event.command(name="list", aliases=["types"])
    @guild_only
    @_require_manage_guild()
    async def admin_event_list(self, ctx: DiscoContext) -> None:
        """List all available market event types with disabled status."""
        from configs.market_events_config import EVENT_REGISTRY
        disabled = await ctx.db.get_disabled_events(ctx.guild_id)
        lines = []
        for key, ev in EVENT_REGISTRY.items():
            dur_m = ev.total_duration_seconds // 60
            tag = " \U0001f6ab **disabled**" if key in disabled else ""
            lines.append(
                f"{ev.emoji} **{ev.display_name}** (`{key}`) \u2014 {len(ev.phases)} phases, "
                f"{dur_m}min, rarity {ev.rarity_weight}{tag}"
            )
        embed = card("\U0001f4cb Market Event Types", description="\n".join(lines), color=C_INFO)
        if disabled:
            embed.footer(f"{len(disabled)} event(s) disabled from random triggers")
        else:
            embed.footer(f"Trigger with: {ctx.prefix}admin event trigger <type>")
        await ctx.reply(embed=embed.build(), mention_author=False)

    @admin_event.command(name="status")
    @guild_only
    @_require_manage_guild()
    async def admin_event_status(self, ctx: DiscoContext) -> None:
        """View current event state and settings."""
        from configs.market_events_config import EVENT_REGISTRY
        from services.market_event_engine import (
            get_active_event, get_current_phase, event_time_remaining, phase_time_remaining,
        )

        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        disabled = await ctx.db.get_disabled_events(ctx.guild_id)
        freq = await ctx.db.get_event_frequency(ctx.guild_id)
        module_on = settings.get("module_events", True)

        _b = card("\U0001f4e1 Event System Status", color=C_INFO)

        # Module status
        _b.field("Module", "\U0001f7e2 Enabled" if module_on else "\U0001f534 Disabled", True)
        # Frequency
        approx_hours = (1.0 / freq / 3600 * int(Config.PRICE_TICK_SECONDS)) if freq > 0 else float("inf")
        freq_str = f"{freq:.6f}/tick (~{approx_hours:.1f}h)" if freq > 0 else "Disabled (0)"
        _b.field("Random Frequency", freq_str, True)
        _b.field("Disabled Events", f"{len(disabled)}/{len(EVENT_REGISTRY)}" if disabled else "None", True)

        # Current event  -  read from Redis for phase-level detail
        _redis = getattr(self.bot.bus, "_redis", None) if hasattr(self.bot, "bus") else None
        ae = await get_active_event(_redis, ctx.guild_id)
        if ae is not None:
            ev = EVENT_REGISTRY.get(ae.event_id)
            if ev:
                phase = get_current_phase(ae)
                phase_label = phase.name.replace("_", " ").title() if phase else "?"
                total_rem = event_time_remaining(ae)
                phase_rem = phase_time_remaining(ae)
                m, s = divmod(int(total_rem), 60)
                pm, ps = divmod(int(phase_rem), 60)
                _b.field(
                    "Active Event",
                    f"{ev.emoji} **{ev.display_name}**\n"
                    f"Phase {ae.phase_index + 1}/{len(ev.phases)}: **{phase_label}** ({pm}m {ps}s left)\n"
                    f"Total remaining: {m}m {s}s",
                    False,
                )
                if phase:
                    mods = []
                    mods.append(f"Vol {phase.vol_multiplier:.1f}x")
                    mods.append(f"Bias {phase.price_bias_pct_per_day:+.1f}%/day")
                    if phase.fee_multiplier != 1.0:
                        mods.append(f"Fees {phase.fee_multiplier:.1f}x")
                    if phase.slippage_mult != 1.0:
                        mods.append(f"Slip {phase.slippage_mult:.1f}x")
                    if phase.liquidity_drain_pct != 0.0:
                        mods.append(f"Liq {phase.liquidity_drain_pct:+.0f}%")
                    _b.field("Phase Modifiers", " \u2022 ".join(mods), False)
            else:
                _b.field("Active Event", f"Unknown: `{ae.event_id}`", False)
        else:
            _b.field("Active Event", "None \u2014 markets calm", False)

        # Disabled list
        if disabled:
            disabled_names = []
            for k in sorted(disabled):
                ev = EVENT_REGISTRY.get(k)
                if ev:
                    disabled_names.append(f"\U0001f6ab {ev.emoji} {ev.display_name} (`{k}`)")
                else:
                    disabled_names.append(f"\U0001f6ab `{k}` (unknown)")
            _b.field("Disabled Event List", "\n".join(disabled_names), False)

        p = ctx.prefix
        _b.footer(f"{p}admin event disable/enable <type> | {p}admin event frequency <val>")
        await ctx.reply(embed=_b.build(), mention_author=False)

    @admin_event.command(name="disable")
    @guild_only
    @_require_manage_guild()
    async def admin_event_disable(self, ctx: DiscoContext, event_type: str) -> None:
        """Disable an event from random triggers. Admin can still trigger manually."""
        from configs.market_events_config import EVENT_REGISTRY
        event_type = event_type.lower().replace(" ", "_")
        if event_type == "all":
            await ctx.db.set_disabled_events(ctx.guild_id, set(EVENT_REGISTRY.keys()))
            await ctx.reply_success(f"All **{len(EVENT_REGISTRY)}** events disabled from random triggers.")
            return
        if event_type not in EVENT_REGISTRY:
            valid = ", ".join(f"`{k}`" for k in EVENT_REGISTRY)
            await ctx.reply_error(f"Unknown event `{event_type}`. Valid: {valid}")
            return
        disabled = await ctx.db.get_disabled_events(ctx.guild_id)
        if event_type in disabled:
            await ctx.reply_error(f"`{event_type}` is already disabled.")
            return
        disabled.add(event_type)
        await ctx.db.set_disabled_events(ctx.guild_id, disabled)
        ev = EVENT_REGISTRY[event_type]
        await ctx.reply_success(
            f"{ev.emoji} **{ev.display_name}** (`{event_type}`) disabled from random triggers.\n"
            f"You can still trigger it manually with `{ctx.prefix}admin event trigger {event_type}`."
        )

    @admin_event.command(name="enable")
    @guild_only
    @_require_manage_guild()
    async def admin_event_enable(self, ctx: DiscoContext, event_type: str) -> None:
        """Re-enable a disabled event for random triggers."""
        from configs.market_events_config import EVENT_REGISTRY
        event_type = event_type.lower().replace(" ", "_")
        if event_type == "all":
            await ctx.db.set_disabled_events(ctx.guild_id, set())
            await ctx.reply_success("All events re-enabled for random triggers.")
            return
        if event_type not in EVENT_REGISTRY:
            valid = ", ".join(f"`{k}`" for k in EVENT_REGISTRY)
            await ctx.reply_error(f"Unknown event `{event_type}`. Valid: {valid}")
            return
        disabled = await ctx.db.get_disabled_events(ctx.guild_id)
        if event_type not in disabled:
            await ctx.reply_error(f"`{event_type}` is already enabled.")
            return
        disabled.discard(event_type)
        await ctx.db.set_disabled_events(ctx.guild_id, disabled)
        ev = EVENT_REGISTRY[event_type]
        await ctx.reply_success(f"{ev.emoji} **{ev.display_name}** (`{event_type}`) re-enabled for random triggers.")

    @admin_event.command(name="frequency", aliases=["freq", "chance"])
    @guild_only
    @_require_manage_guild()
    async def admin_event_frequency(self, ctx: DiscoContext, value: str = "") -> None:
        """Set random event trigger probability per price tick.

        Default: 0.0005 (~once per 2 hours). Set to 0 to disable random events entirely.
        Presets: off, low, default, high, max.
        """
        presets = {
            "off": 0.0, "none": 0.0, "0": 0.0,
            "low": 0.0002, "rare": 0.0002,
            "default": 0.0005, "normal": 0.0005,
            "high": 0.001, "frequent": 0.001,
            "max": 0.005, "chaos": 0.005,
        }

        if not value:
            freq = await ctx.db.get_event_frequency(ctx.guild_id)
            approx_hours = (1.0 / freq / 3600 * int(Config.PRICE_TICK_SECONDS)) if freq > 0 else float("inf")
            p = ctx.prefix
            await ctx.reply(embed=card("📊 Event Frequency", color=C_INFO)
                .field("Current", f"`{freq:.6f}` per tick (~{approx_hours:.1f}h between events)" if freq > 0 else "`0` (random events disabled)", False)
                .field("Presets", f"`{p}admin event frequency off`  -  disable random events\n"
                       f"`{p}admin event frequency low`  -  ~once per 5 hours\n"
                       f"`{p}admin event frequency default`  -  ~once per 2 hours\n"
                       f"`{p}admin event frequency high`  -  ~once per hour\n"
                       f"`{p}admin event frequency max`  -  ~every 12 minutes (chaos mode)", False)
                .footer(f"Or set a custom value: {p}admin event frequency 0.001")
                .build(), mention_author=False)
            return

        value_l = value.lower()
        if value_l in presets:
            freq = presets[value_l]
        else:
            try:
                freq = float(value)
            except ValueError:
                _unique_keys = []
                _seen_vals = set()
                for k, v in presets.items():
                    if v not in _seen_vals:
                        _unique_keys.append(k)
                        _seen_vals.add(v)
                await ctx.reply_error(f"Invalid value `{value}`. Use a number or preset: {', '.join(f'`{k}`' for k in _unique_keys)}")
                return
            if freq < 0:
                await ctx.reply_error("Frequency cannot be negative.")
                return
            if freq > 0.01:
                await ctx.reply_error("Max frequency is `0.01` (events almost every tick). Use `max` for chaos mode.")
                return

        await ctx.db.set_event_frequency(ctx.guild_id, freq)
        if freq == 0:
            await ctx.reply_success("Random events **disabled**. You can still trigger events manually.")
        else:
            approx_hours = (1.0 / freq / 3600 * int(Config.PRICE_TICK_SECONDS)) if freq > 0 else float("inf")
            await ctx.reply_success(f"Event frequency set to `{freq:.6f}` per tick (~**{approx_hours:.1f}h** between random events).")

    # ── ,admin buddy ──────────────────────────────────────────────────────────

    @admin.group(name="buddy", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_buddy(self, ctx: DiscoContext) -> None:
        """Admin knobs for the buddy subsystem."""
        if await suggest_subcommand(ctx, self.admin_buddy):
            return
        p = ctx.prefix or "."
        cur = await ctx.db.fetch_val(
            "SELECT buddy_message_delete_after FROM guild_settings "
            "WHERE guild_id=$1",
            ctx.guild_id,
        )
        ad_state = f"{int(cur)}s" if cur else "off"
        bot_channels = await ctx.db.get_bot_channels(ctx.guild_id)
        bc_state = (
            ", ".join(f"<#{c}>" for c in bot_channels[:5])
            if bot_channels else "*none configured*"
        )
        b = card("🐣 Buddy Admin", color=C_NAVY)
        b.description(
            "Admin controls for the CC Buddy subsystem. "
            "Requires Manage Server."
        )
        b.field(
            "Subcommands",
            (
                f"`{p}admin buddy autodelete <secs|off>`  -  set how long battle / "
                f"escape-event embeds stay in chat\n"
                f"`{p}admin buddy spawn`  -  manually trigger a wild "
                f"(escaped) buddy event in this server right now\n"
                f"`{p}admin buddy recover <msg_id|link>`  -  look up which "
                f"buddy a wild-capture message produced (post-mig-0193)"
            ),
            inline=False,
        )
        b.field(
            "Current state",
            (
                f"Autodelete: **{ad_state}**\n"
                f"World-event channels: {bc_state}\n"
                f"Spawn cadence: every 30min, 15% chance per guild"
            ),
            inline=False,
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @admin_buddy.command(name="autodelete", aliases=["ad"])
    @guild_only
    @_require_manage_guild()
    async def admin_buddy_autodelete(
        self, ctx: DiscoContext, seconds: str | None = None,
    ) -> None:
        """How long buddy embeds stick around before auto-deleting.

        Applies to battle challenges, battle results, and escaped-buddy
        world events. Pass an integer number of seconds to enable, or
        ``off`` / ``0`` to disable.
        """
        p = ctx.prefix or "."
        if seconds is None:
            cur = await ctx.db.fetch_val(
                "SELECT buddy_message_delete_after FROM guild_settings "
                "WHERE guild_id=$1",
                ctx.guild_id,
            )
            state = f"{int(cur)}s" if cur else "off"
            await ctx.reply_success(
                f"Current buddy-message autodelete: **{state}**\n"
                f"Use `{p}admin buddy autodelete <seconds>` to change "
                f"(e.g. `600` for 10 minutes), or `off` to disable.",
                title="Buddy Autodelete",
            )
            return

        raw = seconds.strip().lower()
        if raw in ("off", "0", "none", "disable", "disabled"):
            new_val: int | None = None
        else:
            try:
                new_val = int(raw)
            except ValueError:
                await ctx.reply_error(
                    f"Seconds must be an integer or `off`. Example: "
                    f"`{p}admin buddy autodelete 600`.",
                )
                return
            if new_val < 30:
                await ctx.reply_error(
                    "Minimum autodelete is 30 seconds (below that battle "
                    "embeds disappear before users can read them).",
                )
                return
            if new_val > 14 * 24 * 3600:
                await ctx.reply_error(
                    "Maximum autodelete is 14 days (Discord's delete-after "
                    "scheduling gets unreliable past that).",
                )
                return

        await ctx.db.execute(
            "UPDATE guild_settings SET buddy_message_delete_after=$1 "
            "WHERE guild_id=$2",
            new_val, ctx.guild_id,
        )
        state = f"{new_val}s" if new_val else "off"
        await ctx.reply_success(
            f"Buddy-message autodelete set to **{state}**.",
            title="Buddy Autodelete",
        )

    @admin_buddy.command(name="spawn")
    @guild_only
    @_require_manage_guild()
    async def admin_buddy_spawn(self, ctx: DiscoContext) -> None:
        """Force-spawn a wild (escaped) buddy event in this server now.

        Calls into the Buddy cog's escape spawner on demand. Same code
        path the 30-minute background loop uses, just triggered
        manually. Fails fast with a clear reason if the guild has no
        bot_channels configured or the shelter is empty.
        """
        buddy_cog = self.bot.get_cog("Buddy")
        if buddy_cog is None:
            await ctx.reply_error(
                "Buddy cog is not loaded, so escape events are disabled."
            )
            return

        channel_ids = await ctx.db.get_bot_channels(ctx.guild_id)
        if not channel_ids:
            p = ctx.prefix or "."
            await ctx.reply_error(
                f"This server has no bot channels configured. "
                f"Run `{p}settings bot_channels add #channel` in the channel "
                f"you want world events to post in, then retry."
            )
            return

        shelter_count = await ctx.db.fetch_val(
            "SELECT COUNT(*) FROM cc_buddies "
            "WHERE guild_id=$1 AND status='shelter' "
            "  AND (adoptable_after IS NULL OR adoptable_after <= NOW())",
            ctx.guild_id,
        )
        if not shelter_count:
            await ctx.reply_error(
                "The shelter is empty (or every buddy is still in the "
                "reclaim grace window). Nothing to escape right now."
            )
            return

        try:
            await buddy_cog._try_spawn_escape(ctx.guild)
        except Exception:
            log.exception(
                "admin buddy spawn: escape trigger failed gid=%s",
                ctx.guild_id,
            )
            await ctx.reply_error(
                "Escape spawn raised an internal error. "
                "Check the bot logs for details."
            )
            return

        await ctx.reply_success(
            "Escape event triggered. If a bot channel is reachable and a "
            "shelter buddy was available, a challenge prompt was just posted."
        )

    @admin_buddy.command(name="recover", aliases=["lookup", "find"])
    @guild_only
    @_require_manage_guild()
    async def admin_buddy_recover(
        self, ctx: DiscoContext, message_or_link: str | None = None,
    ) -> None:
        """Look up which buddy a player captured from a Discord message.

        Pass either a raw Discord message id (snowflake) or a full
        message link (`https://discord.com/channels/<g>/<c>/<m>`). Reads
        cc_buddies.capture_message_id (stamped at wild-capture time --
        see migration 0193) and prints the buddy id, owner, species,
        rarity, level, current status, and which channel the capture
        was announced in. Use this to answer "I caught a buddy in this
        message but I can not find it" reports without trawling the
        table by hatched_at.
        """
        p = ctx.prefix or ","
        if not message_or_link:
            await ctx.reply_error(
                f"Pass a message id or link, e.g. "
                f"`{p}admin buddy recover 123456789012345678` or "
                f"`{p}admin buddy recover https://discord.com/channels/.../.../<msg_id>`."
            )
            return

        raw = message_or_link.strip()
        msg_id: int | None = None
        # Accept full message links: pull the trailing id segment.
        if "/" in raw:
            tail = raw.rstrip("/").rsplit("/", 1)[-1]
            try:
                msg_id = int(tail)
            except ValueError:
                msg_id = None
        else:
            try:
                msg_id = int(raw)
            except ValueError:
                msg_id = None

        if not msg_id or msg_id <= 0:
            await ctx.reply_error(
                f"Could not parse a Discord message id from `{raw}`."
            )
            return

        row = await ctx.db.fetch_one(
            "SELECT id, guild_id, owner_user_id, species, name, status, "
            "       level, rarity_tier, gender, is_active, "
            "       capture_channel_id, hatched_at "
            "FROM cc_buddies "
            "WHERE capture_message_id = $1",
            msg_id,
        )
        if not row:
            await ctx.reply_error(
                f"No buddy in cc_buddies has `capture_message_id = "
                f"{msg_id}`. Either the capture pre-dates migration 0193, "
                f"the message did not announce a wild capture, or the "
                f"buddy has been deleted from the table."
            )
            return

        try:
            from configs.buddies_config import (
                rarity_meta as _rarity_meta,
                gender_glyph as _gender_glyph,
            )
            tier_label = str(_rarity_meta(int(row.get("rarity_tier") or 1))
                             .get("name") or "Common")
            glyph = _gender_glyph(row.get("gender"))
        except Exception:
            tier_label = "Common"
            glyph = ""
        glyph_part = f" {glyph}" if glyph else ""
        owner_id = int(row.get("owner_user_id") or 0)
        guild_id = int(row.get("guild_id") or 0)
        ch_id = row.get("capture_channel_id")
        ch_part = f"<#{int(ch_id)}>" if ch_id else "*(channel not recorded)*"
        active_tag = " (active)" if bool(row.get("is_active")) else ""
        embed = (
            card(
                f"Buddy Capture Lookup  -  msg #{msg_id}",
                color=C_NAVY,
            )
            .field(
                "Buddy",
                f"`#{int(row['id'])}`  **{row.get('name') or 'Unnamed'}**"
                f"{glyph_part}\nLv. {int(row.get('level') or 1)} "
                f"{tier_label} {row.get('species') or '?'}",
                False,
            )
            .field(
                "Owner",
                f"<@{owner_id}> (`{owner_id}`)",
                True,
            )
            .field(
                "Status",
                f"`{row.get('status') or '?'}`{active_tag}",
                True,
            )
            .field(
                "Captured in",
                ch_part,
                True,
            )
            .field(
                "Captured at",
                fmt_ts(row.get("hatched_at")) if row.get("hatched_at") else "?",
                True,
            )
            .field(
                "Server",
                f"`{guild_id}`",
                True,
            )
            .footer(
                f"Tell the player: ',buddy find {row.get('species') or ''}' "
                f"to locate it in their collection."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── Achievement moderation ───────────────────────────────────────────────
    #
    # Grant, revoke, and backfill for the progression system in
    # services/achievements.py. Intended for mod rulings (revoke cheated
    # badges), event giveaways (grant a custom badge), and one-shot
    # population when rolling out the achievement system on an existing
    # server. All subcommands require Manage Guild via the parent group.

    @admin.group(name="achievement", aliases=["ach"], invoke_without_command=True)
    @guild_only
    async def admin_achievement(self, ctx: DiscoContext) -> None:
        """Moderate achievement grants. See ,admin achievement help."""
        if await suggest_subcommand(ctx, self.admin_achievement):
            return
        p = ctx.prefix or "."
        embed = (
            card(
                "\U0001F3C6 Admin: Achievements",
                description=(
                    "Moderation + rollout tools for the achievement system. "
                    "Requires Manage Guild."
                ),
                color=C_NAVY,
            )
            .field(
                "Grant / Revoke",
                f"`{p}admin achievement grant @user <badge_id>`\n"
                f"`{p}admin achievement revoke @user <badge_id>`\n"
                f"Grant pays the catalog reward + DMs the user; revoke "
                f"does NOT refund.",
                inline=False,
            )
            .field(
                "Backfill",
                f"`{p}admin achievement backfill @user`\n"
                f"`{p}admin achievement backfill all`\n"
                f"Rebuilds counters from transactions history and grants "
                f"any achievements the user already qualifies for. "
                f"Idempotent; safe to re-run.",
                inline=False,
            )
            .build()
        )
        await ctx.send_embed(embed)

    @admin_achievement.command(name="grant")
    @guild_only
    async def admin_achievement_grant(
        self, ctx: DiscoContext, member: discord.Member, badge_id: str,
    ) -> None:
        """Grant a badge to a user. Pays the catalog reward."""
        from services import achievements as _ach
        import configs.achievements_config as _ach_cfg
        entry = _ach_cfg.get(badge_id)
        if entry is None:
            await ctx.reply_error(
                f"Unknown badge id **{badge_id}**. Use "
                f"`,achievements` to see valid ids."
            )
            return
        ok = await _ach.grant(self.bot, member.id, ctx.guild_id, badge_id)
        if not ok:
            await ctx.reply_error(
                f"{member.display_name} already has **{entry['name']}**."
            )
            return
        await ctx.reply_success(
            f"Granted **{entry['name']}** to {member.display_name}. "
            f"Reward: {FormatKit.usd(float(entry.get('reward_usd', 0.0)))}.",
            title="Achievement Granted",
        )

    @admin_achievement.command(name="revoke")
    @guild_only
    async def admin_achievement_revoke(
        self, ctx: DiscoContext, member: discord.Member, badge_id: str,
    ) -> None:
        """Remove a badge from a user. Does NOT refund the reward."""
        from services import achievements as _ach
        import configs.achievements_config as _ach_cfg
        entry = _ach_cfg.get(badge_id)
        ok = await _ach.revoke(ctx.db, member.id, ctx.guild_id, badge_id)
        if not ok:
            await ctx.reply_error(
                f"{member.display_name} does not have **{badge_id}**."
            )
            return
        name = entry["name"] if entry else badge_id
        await ctx.reply_success(
            f"Removed **{name}** from {member.display_name}.",
            title="Achievement Revoked",
        )

    @admin_achievement.command(name="backfill")
    @guild_only
    async def admin_achievement_backfill(
        self, ctx: DiscoContext, target: str,
    ) -> None:
        """Rebuild achievement counters from transaction history.

        ``target`` is a user mention / id, or ``all`` for the whole guild.
        """
        from services import achievements as _ach
        if target.lower() == "all":
            status_msg = await ctx.send_embed(
                card(
                    "\U000023F3 Backfilling achievements...",
                    description="Scanning every registered user in this "
                                "guild. This may take a moment.",
                    color=C_INFO,
                ).build()
            )
            summary = await _ach.backfill_guild(self.bot, ctx.guild_id)
            lines = [
                f"Users scanned: **{summary['users']}**",
                f"Badges granted: **{summary['granted_total']}**",
            ]
            if summary["by_badge"]:
                top = sorted(
                    summary["by_badge"].items(),
                    key=lambda kv: kv[1], reverse=True,
                )[:10]
                lines.append("")
                lines.append("**Top grants:**")
                for bid, n in top:
                    lines.append(f"- `{bid}`: {n}")
            done = card(
                "\U00002705 Backfill Complete",
                description="\n".join(lines),
                color=C_SUCCESS,
            ).build()
            try:
                await status_msg.edit(embed=done)
            except Exception:
                await ctx.send_embed(done)
            return

        # Single-user path: accept a mention or a raw id.
        member: discord.Member | None = None
        ref = target.strip("<@!>")
        try:
            uid = int(ref)
            member = ctx.guild.get_member(uid)
        except ValueError:
            member = None
        if member is None:
            await ctx.reply_error(
                "Target must be a user mention, user id, or `all`."
            )
            return
        summary = await _ach.backfill_user(self.bot, member.id, ctx.guild_id)
        granted = summary.get("granted", [])
        updated = summary.get("updated", [])
        if not granted and not updated:
            await ctx.reply_success(
                f"{member.display_name}: no transactions to backfill.",
                title="Backfill Complete",
            )
            return
        body = (
            f"Counters updated: {', '.join(updated) if updated else 'none'}\n"
            f"Badges granted: {len(granted)}"
        )
        if granted:
            body += "\n\n" + "\n".join(f"- `{b}`" for b in granted[:15])
        embed = card(
            f"\U00002705 Backfill: {member.display_name}",
            description=body, color=C_SUCCESS,
        ).build()
        await ctx.send_embed(embed)


    # ── Guild challenges ─────────────────────────────────────────────────
    #
    # Start/end server-wide collective goals that split a reward pool on
    # completion. See services/challenges.py for the engine and
    # cogs/challenges.py for the player-facing commands.

    @admin.group(
        name="challenge", aliases=["ch"], invoke_without_command=True,
    )
    @guild_only
    async def admin_challenge(self, ctx: DiscoContext) -> None:
        """Manage server challenges. Use ,admin challenge help."""
        if await suggest_subcommand(ctx, self.admin_challenge):
            return
        p = ctx.prefix or "."
        from services import challenges as _ch_svc
        triggers = ", ".join(f"`{t}`" for t in _ch_svc.TRIGGERS)
        embed = (
            card(
                "\U0001F3AF Admin: Guild Challenges",
                description=(
                    "Start and finalize server-wide challenges. Each "
                    "challenge ticks on qualifying bus activity and pays "
                    "the pool out proportionally on success. Failures "
                    "(deadline passed) pay nothing."
                ),
                color=C_NAVY,
            )
            .field(
                "Start",
                f"`{p}admin challenge start <trigger> <target> <days> "
                f"<pool_usd> <name...>`\n"
                f"Example: `{p}admin challenge start block_mined 1000 7 "
                f"50000 Mining Blitz`\n"
                f"Only one active challenge per trigger at a time.",
                inline=False,
            )
            .field(
                "Finalize",
                f"`{p}admin challenge end <id>`  -  force a challenge to "
                f"complete. If progress >= target it succeeds and pays "
                f"the pool; otherwise it fails and no one is paid.",
                inline=False,
            )
            .field("Valid triggers", triggers, inline=False)
            .build()
        )
        await ctx.send_embed(embed)

    @admin_challenge.command(name="start")
    @guild_only
    async def admin_challenge_start(
        self, ctx: DiscoContext,
        trigger: str, target: int, duration_days: int,
        reward_pool_usd: float, *, name: str,
    ) -> None:
        """Start a server-wide challenge. See ,admin challenge help."""
        from services import challenges as _ch_svc
        if trigger not in _ch_svc.TRIGGERS:
            await ctx.reply_error(
                f"Unknown trigger **{trigger}**. Valid: "
                f"{', '.join(_ch_svc.TRIGGERS)}"
            )
            return
        if target < 1:
            await ctx.reply_error("Target must be at least 1.")
            return
        if duration_days < 1 or duration_days > 90:
            await ctx.reply_error("Duration must be between 1 and 90 days.")
            return
        if reward_pool_usd < 0:
            await ctx.reply_error("Reward pool cannot be negative.")
            return
        if not name.strip():
            await ctx.reply_error("Provide a challenge name.")
            return
        try:
            row = await _ch_svc.start(
                ctx.db, ctx.guild_id, name.strip(), trigger,
                target, reward_pool_usd, duration_days,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if row is None:
            await ctx.reply_error(
                f"A challenge with trigger **{trigger}** is already "
                f"active. End it first with `,admin challenge end <id>`."
            )
            return
        cid = int(row["challenge_id"])
        embed = (
            card(
                f"\U0001F3AF Challenge Started: {row['name']}",
                description=(
                    f"Goal: **{int(row['target']):,} "
                    f"{_ch_svc.trigger_label(trigger).lower()}** in "
                    f"{duration_days} day{'s' if duration_days != 1 else ''}.\n"
                    f"Pool: **{FormatKit.usd(float(row['reward_pool_usd']))}**\n"
                    f"Ends: **{fmt_ts(row['ends_at'])}**\n\n"
                    f"Every contribution counts. Rewards split "
                    f"proportionally on success."
                ),
                color=C_SUCCESS,
            )
            .footer(f"Challenge id: {cid}. Players: ,challenge")
            .build()
        )
        await ctx.send_embed(embed)
        # Auto-post the server calendar so the new challenge appears on
        # the public schedule alongside any other active items + the
        # next daily / weekly resets. Best-effort.
        try:
            from cogs.calendar import post_calendar_to_bot_channel
            await post_calendar_to_bot_channel(self.bot, ctx.guild)
        except Exception:
            log.debug("admin challenge start: calendar auto-post failed",
                      exc_info=True)

    @admin_challenge.command(name="end")
    @guild_only
    async def admin_challenge_end(
        self, ctx: DiscoContext, challenge_id: int,
    ) -> None:
        """Force-finalize a challenge now. Succeeds if target met, else fails."""
        from services import challenges as _ch_svc
        row = await _ch_svc.get(ctx.db, challenge_id)
        if row is None or int(row["guild_id"]) != ctx.guild_id:
            await ctx.reply_error(f"No challenge with id **{challenge_id}**.")
            return
        if row["status"] != "active":
            await ctx.reply_error(
                f"Challenge #{challenge_id} is already **{row['status']}**."
            )
            return
        if int(row["progress"]) >= int(row["target"]):
            result = await _ch_svc.succeed(self.bot, challenge_id)
            paid = result.get("paid", [])
            total = sum(float(r) for _, r in paid)
            await ctx.reply_success(
                f"#{challenge_id} **{row['name']}** succeeded!\n"
                f"Paid out {FormatKit.usd(total)} to "
                f"{len(paid)} contributor{'s' if len(paid) != 1 else ''}.",
                title="Challenge Finalized",
            )
        else:
            await _ch_svc.fail(ctx.db, challenge_id)
            await ctx.reply_success(
                f"#{challenge_id} **{row['name']}** failed "
                f"({int(row['progress']):,}/{int(row['target']):,}). No payout.",
                title="Challenge Finalized",
            )


    # ── ,admin fishing ────────────────────────────────────────────────────────

    @admin.group(name="fishing", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_fishing(self, ctx: DiscoContext) -> None:
        """Admin knobs for the fishing minigame.

        Subcommands: enable / disable / channel / reset / givebait /
        giverod / announce. Requires Manage Server.
        """
        if await suggest_subcommand(ctx, self.admin_fishing):
            return
        p = ctx.prefix or "."
        s = await ctx.db.get_guild_settings(ctx.guild_id)
        enabled = s.get("module_fishing")
        enabled_str = (
            "enabled (default)" if enabled is None
            else "enabled" if enabled else "**disabled**"
        )
        ch = s.get("fishing_channel")
        ch_str = f"<#{int(ch)}>" if ch else "*not set (uses events_channel)*"
        b = card("\U0001F3A3 Fishing Admin", color=C_NAVY)
        b.description(
            "Admin controls for the fishing minigame. "
            "Requires Manage Server."
        )
        b.field("Module", enabled_str, True)
        b.field("Splash channel", ch_str, True)
        b.field(
            "Subcommands",
            (
                f"`{p}admin fishing enable`  -  turn the module on\n"
                f"`{p}admin fishing disable`  -  turn it off (admins still bypass)\n"
                f"`{p}admin fishing channel #ch`  -  set splash channel for legendary catches\n"
                f"`{p}admin fishing channel off`  -  clear it (falls back to events_channel)\n"
                f"`{p}admin fishing reset @user`  -  wipe a user's fishing row + history\n"
                f"`{p}admin fishing givebait @user <bait_key> <qty>`  -  gift bait\n"
                f"`{p}admin fishing giverod @user <tier>`  -  set rod tier (0-{max(__import__('configs.fishing_config', fromlist=['RODS']).RODS.keys())})\n"
                f"`{p}admin fishing announce <fish_key> [@user]`  -  manual splash"
            ),
            inline=False,
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @admin_fishing.command(name="enable", aliases=["on"])
    @_require_manage_guild()
    async def admin_fishing_enable(self, ctx: DiscoContext) -> None:
        """Turn the fishing module on for this guild."""
        await ctx.db.update_guild_setting(ctx.guild_id, "module_fishing", True)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="fishing_module_enable",
            severity=SEVERITY_WARN,
        )
        await ctx.reply_success("Fishing module **enabled**.", title="\U0001F3A3 Fishing")

    @admin_fishing.command(name="disable", aliases=["off"])
    @_require_manage_guild()
    async def admin_fishing_disable(self, ctx: DiscoContext) -> None:
        """Turn the fishing module off (admins still bypass)."""
        await ctx.db.update_guild_setting(ctx.guild_id, "module_fishing", False)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="fishing_module_disable",
            severity=SEVERITY_WARN,
        )
        await ctx.reply_success(
            "Fishing module **disabled**. Players will get a "
            "module-disabled error; admins still bypass.",
            title="\U0001F3A3 Fishing",
        )

    @admin_fishing.command(name="channel")
    @_require_manage_guild()
    async def admin_fishing_channel(
        self, ctx: DiscoContext,
        target: discord.TextChannel | str | None = None,
    ) -> None:
        """Set or clear the splash channel for rare/legendary catches.

        Usage: ``,admin fishing channel #fish-feed`` to set, or
        ``,admin fishing channel off`` to clear.
        """
        if target is None:
            await ctx.reply_error_hint(
                "Pick a channel or `off`.",
                hint="admin fishing channel #fish-feed",
                command_name="admin fishing channel",
            )
            return
        if isinstance(target, str):
            if target.lower() in ("off", "none", "clear", "unset"):
                await ctx.db.set_channel(ctx.guild_id, "fishing_channel", None)
                await ctx.reply_success(
                    "Splash channel cleared. Splashes fall back to events_channel.",
                    title="\U0001F3A3 Fishing",
                )
                return
            await ctx.reply_error(
                "Pass an actual channel mention, not a string. "
                "Try `,admin fishing channel #channel-name`.",
            )
            return
        await ctx.db.set_channel(ctx.guild_id, "fishing_channel", target.id)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="fishing_channel_set",
            severity=SEVERITY_WARN, details=str(target.id),
        )
        await ctx.reply_success(
            f"Splash channel set to {target.mention}.",
            title="\U0001F3A3 Fishing",
        )

    @admin_fishing.command(name="reset")
    @_require_manage_guild()
    async def admin_fishing_reset(
        self, ctx: DiscoContext, target: discord.Member,
    ) -> None:
        """Wipe a user's user_fishing row and their fishing_catches history.

        Destructive: clears rod, bait, inventory, combo, level, biggest
        catch -- everything. Use to unbreak a stuck account or as a
        moderation tool. Confirms first.
        """
        confirmed = await ctx.confirm(
            f"Wipe **all** fishing data for {target.mention}? "
            f"This deletes their rod, bait, caught fish, junk, combo, "
            f"level, biggest catch, and entire catch history. "
            f"Cannot be undone.",
        )
        if not confirmed:
            return
        async with ctx.db.atomic():
            await ctx.db.execute(
                "DELETE FROM fishing_catches WHERE guild_id=$1 AND user_id=$2",
                ctx.guild_id, target.id,
            )
            await ctx.db.execute(
                "DELETE FROM user_fishing WHERE guild_id=$1 AND user_id=$2",
                ctx.guild_id, target.id,
            )
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="fishing_reset_user",
            target_id=target.id, severity=SEVERITY_DANGER,
        )
        await ctx.reply_success(
            f"Reset all fishing data for {target.mention}.",
            title="\U0001F3A3 Fishing",
        )

    @admin_fishing.command(name="givebait")
    @_require_manage_guild()
    async def admin_fishing_givebait(
        self, ctx: DiscoContext,
        target: discord.Member, bait_key: str, qty: int = 1,
    ) -> None:
        """Gift bait to a player. Bypasses the cost; respects max_stack."""
        import configs.fishing_config as fc
        from services import fishing as fish_svc
        bait_key = (bait_key or "").lower()
        if bait_key not in fc.BAIT:
            keys = ", ".join(f"`{k}`" for k in fc.BAIT.keys())
            await ctx.reply_error(f"Unknown bait `{bait_key}`. Choices: {keys}")
            return
        if qty <= 0:
            await ctx.reply_error("Quantity must be positive.")
            return
        cfg = fc.BAIT[bait_key]
        state = await fish_svc.ensure_state(ctx.db, ctx.guild_id, target.id)
        cur_inv = dict(state.get("bait_inventory") or {})
        cur = int(cur_inv.get(bait_key, 0))
        cap = int(cfg.get("max_stack") or 1_000_000)
        actual = min(qty, max(0, cap - cur))
        if actual <= 0:
            await ctx.reply_error(
                f"{target.display_name} already holds the cap ({cap}) of {cfg['name']}."
            )
            return
        cur_inv[bait_key] = cur + actual
        import json as _json_mod
        await ctx.db.execute(
            """
            UPDATE user_fishing
               SET bait_inventory = $3::jsonb, updated_at = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            ctx.guild_id, target.id, _json_mod.dumps(cur_inv),
        )
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="fishing_givebait",
            target_id=target.id, severity=SEVERITY_WARN,
            details=f"{actual}x{bait_key}",
        )
        await ctx.reply_success(
            f"Gave {target.mention} **{actual}× {cfg['emoji']} {cfg['name']}** "
            f"(now holding **{cur + actual}/{cap}**).",
            title="\U0001F3A3 Fishing",
        )

    @admin_fishing.command(name="giverod")
    @_require_manage_guild()
    async def admin_fishing_giverod(
        self, ctx: DiscoContext, target: discord.Member, tier: int,
    ) -> None:
        """Set a player's rod tier (bypasses upgrade ladder + cost)."""
        import configs.fishing_config as fc
        from services import fishing as fish_svc
        if tier not in fc.RODS:
            choices = ", ".join(str(k) for k in sorted(fc.RODS.keys()))
            await ctx.reply_error(f"Rod tier must be one of: {choices}.")
            return
        await fish_svc.ensure_state(ctx.db, ctx.guild_id, target.id)
        await ctx.db.execute(
            """
            UPDATE user_fishing
               SET rod_tier = $3, updated_at = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            ctx.guild_id, target.id, int(tier),
        )
        rod = fc.rod_meta(tier)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="fishing_giverod",
            target_id=target.id, severity=SEVERITY_WARN,
            details=f"tier{tier}",
        )
        await ctx.reply_success(
            f"Set {target.mention}'s rod to "
            f"**tier {tier}: {rod['emoji']} {rod['name']}**.",
            title="\U0001F3A3 Fishing",
        )

    @admin_fishing.command(name="announce")
    @_require_manage_guild()
    async def admin_fishing_announce(
        self, ctx: DiscoContext, fish_key: str,
        target: discord.Member | None = None,
    ) -> None:
        """Manually post a splash announcement for a fish.

        Useful for in-server events ("the kraken has been spotted!")
        without actually rolling a catch. Posts to the configured
        fishing_channel, falling back to events_channel.
        """
        import configs.fishing_config as fc
        fish_key = (fish_key or "").lower()
        meta = fc.fish_meta(fish_key)
        if not meta:
            keys = ", ".join(f"`{k}`" for k in fc.FISH.keys())
            await ctx.reply_error(f"Unknown fish `{fish_key}`. Choices: {keys}")
            return
        rarity = str(meta.get("rarity") or "common")
        rmeta = fc.rarity_meta(rarity)
        emoji = str(meta.get("emoji") or "\U0001F420")
        name = str(meta.get("name") or fish_key)
        weight = float(meta.get("max_lbs") or 0.0)
        who = (target.mention if target else "Someone")
        desc = (
            f"{who} just hooked a "
            f"**{rmeta.get('label', 'Rare')} {emoji} {name}** "
            f"weighing **{weight:,.2f} lbs**!"
        )
        embed = card("\U0001F3A3 Big Catch!", description=desc,
                     color=int(rmeta.get("color_hex") or C_GOLD)).build()

        s = await ctx.db.get_guild_settings(ctx.guild_id)
        ch_id = s.get("fishing_channel") or s.get("events_channel")
        if not ch_id:
            await ctx.reply_error(
                "No fishing_channel or events_channel configured. "
                "Set one with `,admin fishing channel #ch` first.",
            )
            return
        ch = ctx.guild.get_channel(int(ch_id))
        if not isinstance(ch, discord.TextChannel):
            await ctx.reply_error("Configured channel is not a text channel.")
            return
        await ch.send(embed=embed)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="fishing_announce",
            severity=SEVERITY_WARN, details=f"{fish_key} -> #{ch.id}",
        )
        await ctx.reply_success(
            f"Posted splash for {emoji} **{name}** in {ch.mention}.",
            title="\U0001F3A3 Fishing",
        )


    # ── ,admin farming ───────────────────────────────────────────────────────

    @admin.group(name="farming", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_farming(self, ctx: DiscoContext) -> None:
        """Admin knobs for the farming minigame.

        Subcommands: enable / disable. Requires Manage Server.
        """
        if await suggest_subcommand(ctx, self.admin_farming):
            return
        p = ctx.prefix or "."
        s = await ctx.db.get_guild_settings(ctx.guild_id)
        enabled = s.get("module_farming")
        enabled_str = (
            "enabled (default)" if enabled is None
            else "enabled" if enabled else "**disabled**"
        )
        b = card("\U0001F33E Farming Admin", color=C_NAVY)
        b.description("Admin controls for the farming minigame. Requires Manage Server.")
        b.field("Module", enabled_str, True)
        b.field(
            "Subcommands",
            (
                f"`{p}admin farming enable`  -  turn the module on\n"
                f"`{p}admin farming disable`  -  turn it off (admins still bypass)"
            ),
            inline=False,
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @admin_farming.command(name="enable", aliases=["on"])
    @_require_manage_guild()
    async def admin_farming_enable(self, ctx: DiscoContext) -> None:
        """Turn the farming module on for this guild."""
        await ctx.db.update_guild_setting(ctx.guild_id, "module_farming", True)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="farming_module_enable",
            severity=SEVERITY_WARN,
        )
        await ctx.reply_success("Farming module **enabled**.", title="\U0001F33E Farming")

    @admin_farming.command(name="disable", aliases=["off"])
    @_require_manage_guild()
    async def admin_farming_disable(self, ctx: DiscoContext) -> None:
        """Turn the farming module off (admins still bypass)."""
        await ctx.db.update_guild_setting(ctx.guild_id, "module_farming", False)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="farming_module_disable",
            severity=SEVERITY_WARN,
        )
        await ctx.reply_success(
            "Farming module **disabled**. Players will get a "
            "module-disabled error; admins still bypass.",
            title="\U0001F33E Farming",
        )

    # ── ,admin crafting ──────────────────────────────────────────────────────

    @admin.group(name="crafting", aliases=["forge"], invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def admin_crafting(self, ctx: DiscoContext) -> None:
        """Admin knobs for the crafting minigame.

        Subcommands: enable / disable. Requires Manage Server.
        """
        if await suggest_subcommand(ctx, self.admin_crafting):
            return
        p = ctx.prefix or "."
        s = await ctx.db.get_guild_settings(ctx.guild_id)
        enabled = s.get("module_crafting")
        enabled_str = (
            "enabled (default)" if enabled is None
            else "enabled" if enabled else "**disabled**"
        )
        b = card("\U0001F528 Crafting Admin", color=C_NAVY)
        b.description("Admin controls for the crafting minigame. Requires Manage Server.")
        b.field("Module", enabled_str, True)
        b.field(
            "Subcommands",
            (
                f"`{p}admin crafting enable`  -  turn the module on\n"
                f"`{p}admin crafting disable`  -  turn it off (admins still bypass)"
            ),
            inline=False,
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @admin_crafting.command(name="enable", aliases=["on"])
    @_require_manage_guild()
    async def admin_crafting_enable(self, ctx: DiscoContext) -> None:
        """Turn the crafting module on for this guild."""
        await ctx.db.update_guild_setting(ctx.guild_id, "module_crafting", True)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="crafting_module_enable",
            severity=SEVERITY_WARN,
        )
        await ctx.reply_success("Crafting module **enabled**.", title="\U0001F528 Crafting")

    @admin_crafting.command(name="disable", aliases=["off"])
    @_require_manage_guild()
    async def admin_crafting_disable(self, ctx: DiscoContext) -> None:
        """Turn the crafting module off (admins still bypass)."""
        await ctx.db.update_guild_setting(ctx.guild_id, "module_crafting", False)
        await log_staff_action(
            ctx.db, scope=SCOPE_ADMIN, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="crafting_module_disable",
            severity=SEVERITY_WARN,
        )
        await ctx.reply_success(
            "Crafting module **disabled**. Players will get a "
            "module-disabled error; admins still bypass.",
            title="\U0001F528 Crafting",
        )


    # ── Joke commands (admin-only, intentionally do nothing) ────────────────

    @admin.command(name="nukeeconomy", aliases=["nuke"])
    @_require_manage_guild()
    async def admin_nukeeconomy(self, ctx: DiscoContext) -> None:
        """Detonate the entire economy. (Spoiler: no it doesn't.)"""
        confirmed = await ctx.confirm(
            f"This will **permanently nuke** the economy of "
            f"**{ctx.guild.name}**. Every balance, every stake, every rig "
            f"will be vaporized. **Cannot be undone.** Are you sure?"
        )
        if not confirmed:
            await ctx.reply_error("Nuke cancelled. Cowardice noted.")
            return
        embed = (
            card("Nuke Deployed", color=C_GOLD)
            .description(
                "Pressing the big red button...\n"
                "Charging plutonium core...\n"
                "Calculating fallout radius...\n\n"
                "...\n\n"
                "**Just kidding.** The button is plastic. "
                "No one was nuked in the making of this command."
            )
            .footer("This command does absolutely nothing.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin.command(name="deleteeveryone", aliases=["wipeusers"])
    @_require_manage_guild()
    async def admin_deleteeveryone(self, ctx: DiscoContext) -> None:
        """Permanently delete every single user. (No it doesn't.)"""
        confirmed = await ctx.confirm(
            "This will **permanently delete every user** in the database "
            "across **every server**. Their balances, items, and dignity "
            "will be erased forever. Are you sure?"
        )
        if not confirmed:
            await ctx.reply_error("Mass deletion cancelled. Mercy logged.")
            return
        embed = (
            card("Deleting Every User...", color=C_PURPLE)
            .description(
                "`[##........]`  17%  -  Locating Alice...\n"
                "`[#####.....]`  51%  -  Shredding Bob's portfolio...\n"
                "`[#########.]`  92%  -  Releasing Carol into the void...\n\n"
                "Wait, where did everyone go?\n\n"
                "**Oh right, nowhere.** This command was a prank. "
                "Everyone is fine."
            )
            .footer("0 users were harmed.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin.command(name="factoryreset")
    @_require_manage_guild()
    async def admin_factoryreset(self, ctx: DiscoContext) -> None:
        """Restore the bot to factory defaults. (Lies.)"""
        confirmed = await ctx.confirm(
            "Factory-resetting **the entire bot**. All servers, all guilds, "
            "all data, all friendships  -  gone. **Continue?**"
        )
        if not confirmed:
            await ctx.reply_error("Factory reset cancelled. Universe still intact.")
            return
        embed = (
            card("Factory Reset", color=C_NAVY)
            .description(
                "Wiping firmware...\n"
                "Restoring 1.0.0.0...\n"
                "Returning bot to its original packaging...\n\n"
                "...\n\n"
                "Couldn't find the box. **Reset aborted.** "
                "The bot is fine. You are fine. Everything is fine."
            )
            .footer("Nothing happened. Probably.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin.command(name="rmrf", aliases=["formatdisk"])
    @_require_manage_guild()
    async def admin_rmrf(self, ctx: DiscoContext) -> None:
        """rm -rf / --no-preserve-root. (Don't worry, it's fake.)"""
        confirmed = await ctx.confirm(
            "Running `rm -rf / --no-preserve-root` on the production host. "
            "This will erase the OS, the database, the bot, and possibly "
            "the laws of physics. **Are you absolutely sure?**"
        )
        if not confirmed:
            await ctx.reply_error("Aborted. Filesystem breathes a sigh of relief.")
            return
        embed = (
            card("rm -rf /", color=C_ERROR)
            .description(
                "```\n"
                "$ sudo rm -rf / --no-preserve-root\n"
                "rm: refusing to remove 'something cool'\n"
                "rm: nice try though\n"
                "```\n"
                "**Permission denied by your friendly bot operator.** "
                "Try a less catastrophic command."
            )
            .footer("This command intentionally does nothing.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin.command(name="printmoney")
    @_require_manage_guild()
    async def admin_printmoney(self, ctx: DiscoContext) -> None:
        """Print infinite money for everyone. (Sadly, no.)"""
        confirmed = await ctx.confirm(
            "Spinning up the **infinite money printer**. Every user will "
            "receive **$999,999,999,999** to their wallet. Hyperinflation "
            "incoming. **Confirm?**"
        )
        if not confirmed:
            await ctx.reply_error("Printer turned off. The Fed thanks you.")
            return
        embed = (
            card("Money Printer Go BRRR", color=C_GOLD)
            .description(
                "*brrrrrrrrrrrrrrr*\n"
                "*brrrrrrrrrrrrrrr*\n"
                "*brrrrrrrrrrrrrrr*\n\n"
                "...the printer is out of toner.\n\n"
                "**No money was printed.** Your economy remains exactly as "
                "balanced (or unbalanced) as it was before."
            )
            .footer("Inflation rate: 0.00%")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,admin cosmetic ────────────────────────────────────────────────────────

    @admin.group(name="cosmetic", aliases=["cosmetics"], invoke_without_command=True)
    @_require_manage_guild()
    async def admin_cosmetic(self, ctx: DiscoContext) -> None:
        """Configure which Discord role each cosmetic item grants in this server."""
        prefix = ctx.prefix or "."
        body = (
            f"`{prefix}admin cosmetic list`  -  show all cosmetics and their configured roles\n"
            f"`{prefix}admin cosmetic set <item> <role name>`  -  point an item to a specific role\n"
            f"`{prefix}admin cosmetic clear <item>`  -  reset to the default role name\n"
        )
        await ctx.reply(
            embed=card(
                "\U0001F3A8 Cosmetic Role Config",
                description=body,
                color=C_INFO,
            ).build(),
            mention_author=False,
        )

    @admin_cosmetic.command(name="list", aliases=["ls", "show"])
    @_require_manage_guild()
    async def admin_cosmetic_list(self, ctx: DiscoContext) -> None:
        """Show all cosmetic items and the role each one grants in this server."""
        from configs.items_config import SHOP_ITEMS
        overrides = await ctx.db.get_cosmetic_role_overrides(ctx.guild_id)
        cosmetic_keys = [k for k, v in SHOP_ITEMS.items() if v.get("category") == "cosmetic"]
        if not cosmetic_keys:
            await ctx.reply_error("No cosmetic items are configured in items_config.py.")
            return

        lines = []
        for key in cosmetic_keys:
            cfg = SHOP_ITEMS[key]
            default_role = cfg.get("role_name", key)
            override = overrides.get(key)
            active_role = override or default_role
            source = "(override)" if override else "(default)"
            lines.append(
                f"{cfg.get('emoji', '')} `{key}` -- **{active_role}** {source}"
            )

        embed = (
            card("\U0001F3A8 Cosmetic Role Config", color=C_INFO)
            .description("\n".join(lines))
            .footer(
                f"Use {ctx.prefix or '.'}admin cosmetic set <item> <role> to override"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @admin_cosmetic.command(name="set", aliases=["edit", "map"])
    @_require_manage_guild()
    async def admin_cosmetic_set(
        self, ctx: DiscoContext, item_key: str, *, role_name: str
    ) -> None:
        """Map a cosmetic item to a specific Discord role name.

        Usage: ,admin cosmetic set glamour_kit VIP Glam
        The role must already exist in this server.
        """
        from configs.items_config import SHOP_ITEMS
        key = item_key.lower().strip()
        cfg = SHOP_ITEMS.get(key)
        if not cfg or cfg.get("category") != "cosmetic":
            valid = ", ".join(
                f"`{k}`" for k, v in SHOP_ITEMS.items() if v.get("category") == "cosmetic"
            )
            await ctx.reply_error(f"Unknown cosmetic item `{key}`. Valid: {valid}")
            return

        role_name = role_name.strip()
        existing = discord.utils.get(ctx.guild.roles, name=role_name)
        if not existing:
            await ctx.reply_error(
                f"No role named **{role_name}** found in this server.\n"
                f"Create it in Discord first, then run this command again."
            )
            return

        await ctx.db.set_cosmetic_role_override(ctx.guild_id, key, role_name)
        await ctx.reply_success(
            f"{cfg.get('emoji', '')} **{cfg['name']}** will now grant the **{role_name}** role.",
            title="\U0001F3A8 Cosmetic Updated",
        )

    @admin_cosmetic.command(name="clear", aliases=["reset", "remove"])
    @_require_manage_guild()
    async def admin_cosmetic_clear(self, ctx: DiscoContext, item_key: str) -> None:
        """Reset a cosmetic item to its default role name (from items_config.py).

        Usage: ,admin cosmetic clear glamour_kit
        """
        from configs.items_config import SHOP_ITEMS
        key = item_key.lower().strip()
        cfg = SHOP_ITEMS.get(key)
        if not cfg or cfg.get("category") != "cosmetic":
            valid = ", ".join(
                f"`{k}`" for k, v in SHOP_ITEMS.items() if v.get("category") == "cosmetic"
            )
            await ctx.reply_error(f"Unknown cosmetic item `{key}`. Valid: {valid}")
            return

        await ctx.db.set_cosmetic_role_override(ctx.guild_id, key, None)
        default_role = cfg.get("role_name", key)
        await ctx.reply_success(
            f"{cfg.get('emoji', '')} **{cfg['name']}** reset to default role **{default_role}**.",
            title="\U0001F3A8 Cosmetic Reset",
        )


    # ── Premium subscription admin ────────────────────────────────
    # Owner-only. Server admins can NOT grant their own guild premium.

    @admin.group(name="premium", aliases=["sub", "subscription"], invoke_without_command=True)
    @_require_bot_owner()
    async def admin_premium(self, ctx: DiscoContext) -> None:
        """Premium subscription admin (bot owner only)."""
        prefix = ctx.prefix or ","
        body = (
            f"`{prefix}admin premium grant <guild_id> [days]`  -  unlock a guild\n"
            f"`{prefix}admin premium revoke <guild_id> [reason]`  -  lock it back\n"
            f"`{prefix}admin premium list`  -  every premium guild\n"
            f"`{prefix}admin premium status [guild_id]`  -  one guild's status\n"
            f"`{prefix}admin premium expire`  -  sweep overdue rows now\n"
        )
        await ctx.reply(
            embed=card("\U0001F511 Premium Admin", description=body, color=C_GOLD).build(),
            mention_author=False,
        )

    @admin_premium.command(name="grant", aliases=["unlock", "give"])
    @_require_bot_owner()
    async def admin_premium_grant(
        self, ctx: DiscoContext,
        guild_id: int,
        days: int | None = None,
        *, notes: str = "",
    ) -> None:
        """Grant premium. ``days`` omitted = indefinite.

        Examples:
            ,admin premium grant 1234567890 30
            ,admin premium grant 1234567890        (no expiry)
        """
        from services import entitlements
        if days is not None and days <= 0:
            await ctx.reply_error("`days` must be > 0 (omit for indefinite).")
            return
        try:
            status = await entitlements.grant_premium(
                guild_id, ctx.db,
                days=days, granted_by=ctx.author.id,
                source="admin", notes=notes or None,
            )
        except Exception as exc:
            log.exception("admin premium grant failed")
            await ctx.reply_error(f"Grant failed: `{exc}`")
            return
        when = (
            fmt_ts(status.expires_at) if status.expires_at else "never (indefinite)"
        )
        await ctx.reply_success(
            f"Guild `{guild_id}` is now **premium**.\nExpires: {when}",
            title="\U0001F511 Premium granted",
        )

    @admin_premium.command(name="revoke", aliases=["lock", "remove"])
    @_require_bot_owner()
    async def admin_premium_revoke(
        self, ctx: DiscoContext,
        guild_id: int,
        *, reason: str = "",
    ) -> None:
        """Revoke premium for a guild."""
        from services import entitlements
        await entitlements.revoke_premium(
            guild_id, ctx.db,
            revoked_by=ctx.author.id, reason=reason or None,
        )
        await ctx.reply_success(
            f"Guild `{guild_id}` premium revoked.",
            title="\U0001F512 Premium revoked",
        )

    @admin_premium.command(name="list", aliases=["all"])
    @_require_bot_owner()
    async def admin_premium_list(self, ctx: DiscoContext) -> None:
        """Show every row in guild_premium."""
        from services import entitlements
        rows = await entitlements.list_premium_guilds(ctx.db)
        if not rows:
            await ctx.reply(
                embed=card(
                    "\U0001F511 Premium guilds",
                    description="No guilds have premium yet.",
                    color=C_NEUTRAL,
                ).build(),
                mention_author=False,
            )
            return
        lines: list[str] = []
        for r in rows:
            gid = r.get("guild_id")
            status = r.get("status")
            source = r.get("source")
            exp = r.get("exp_epoch")
            exp_str = fmt_ts(exp) if exp else "never"
            sub = r.get("paypal_subscription_id") or "-"
            lines.append(
                f"`{gid}`  -  **{status}** via {source}  -  expires {exp_str}  -  sub `{sub}`"
            )
        await ctx.reply(
            embed=card(
                f"\U0001F511 Premium guilds ({len(rows)})",
                description="\n".join(lines)[:4000],
                color=C_GOLD,
            ).build(),
            mention_author=False,
        )

    @admin_premium.command(name="status", aliases=["check"])
    @_require_bot_owner()
    async def admin_premium_status(
        self, ctx: DiscoContext,
        guild_id: int | None = None,
    ) -> None:
        """Show one guild's premium status. Defaults to the current guild."""
        from services import entitlements
        gid = int(guild_id) if guild_id else ctx.guild_id
        s = await entitlements.get_status(gid, ctx.db)
        b = card(
            f"\U0001F511 Premium status -- guild {gid}",
            color=C_GOLD if s.is_premium else C_NEUTRAL,
        )
        b.field("Active?", "yes" if s.is_premium else "no", True)
        b.field("Source", s.source, True)
        b.field("Status", s.status, True)
        if s.expires_at:
            b.field("Expires", fmt_ts(s.expires_at), True)
        if s.current_period_end:
            b.field("Period end", fmt_ts(s.current_period_end), True)
        if s.paypal_subscription_id:
            b.field("PayPal sub", f"`{s.paypal_subscription_id}`", False)
        if s.subscriber_user_id:
            b.field("Subscriber", f"<@{s.subscriber_user_id}>", True)
        if s.notes:
            b.field("Notes", s.notes[:1000], False)
        await ctx.reply(embed=b.build(), mention_author=False)

    @admin_premium.command(name="expire", aliases=["sweep"])
    @_require_bot_owner()
    async def admin_premium_expire(self, ctx: DiscoContext) -> None:
        """Run the overdue-expiry sweep now (also runs in the background)."""
        from services import entitlements
        n = await entitlements.expire_overdue(ctx.db)
        await ctx.reply_success(
            f"Swept {n} overdue subscription(s) to `expired`.",
            title="⏱ Premium sweep",
        )

    @admin_premium.command(name="gift", aliases=["bestow"])
    @_require_bot_owner()
    async def admin_premium_gift(
        self, ctx: DiscoContext,
        guild_id: int,
        days: int,
        *, message: str = "",
    ) -> None:
        """Gift premium to a guild + announce it in their server.

        Identical to ``,admin premium grant`` for the entitlement write,
        but ALSO posts a celebratory embed in the recipient guild's
        system channel (or first writable text channel; falls back to
        DMing the owner) so the gifted server knows premium is active
        and who they have to thank. Use this for giveaways, friend-of-
        the-bot perks, or apologies for outages.
        """
        from services import entitlements
        if days <= 0:
            await ctx.reply_error("`days` must be > 0.")
            return
        try:
            status = await entitlements.grant_premium(
                guild_id, ctx.db,
                days=days, granted_by=ctx.author.id,
                source="gift", notes=message or None,
            )
        except Exception as exc:
            log.exception("admin premium gift failed")
            await ctx.reply_error(f"Gift failed: `{exc}`")
            return

        # ── recipient notification ───────────────────────────────────
        delivered_to: str = "owner DM (no writable channel)"
        target_guild = ctx.bot.get_guild(int(guild_id))
        if target_guild is None:
            delivered_to = "no notification (bot not in guild)"
        else:
            note = (
                card(
                    "🎁 You've been gifted Discoin Premium!",
                    description=(
                        f"This server has been gifted **{days} day{'s' if days != 1 else ''}** "
                        f"of Discoin Premium by the bot owner. Every premium feature is "
                        f"unlocked for everyone here -- AI chat, fishing, farming, crafting, "
                        f"delves, expeditions, buddy battles + breeding + market.\n\n"
                        f"Run `{Config.PREFIX}premium status` to see the new tier."
                    ),
                    color=C_GOLD,
                )
                .field_if(bool(message), "Message from the bot owner", message[:1000], False)
                .footer("Premium auto-expires; renew via ,premium subscribe.")
                .build()
            )
            target = target_guild.system_channel
            if target is None or not target.permissions_for(target_guild.me).send_messages:
                for ch in target_guild.text_channels:
                    if ch.permissions_for(target_guild.me).send_messages:
                        target = ch
                        break
            sent = False
            try:
                if target is not None:
                    await target.send(embed=note)
                    delivered_to = f"#{target.name}"
                    sent = True
            except Exception:
                log.debug("gift notify: send to %s failed", target, exc_info=True)
            if not sent:
                try:
                    owner = target_guild.owner or await self.bot.fetch_user(target_guild.owner_id)
                    if owner is not None:
                        await owner.send(embed=note)
                        delivered_to = f"DM to {owner}"
                except Exception:
                    log.debug("gift notify: DM owner of %s failed", guild_id, exc_info=True)
                    delivered_to = "delivery failed (no permission anywhere)"

        # entitlements.grant_premium already wrote the audit row via _audit;
        # we don't need a second one here.
        when = fmt_ts(status.expires_at) if status.expires_at else "never"
        await ctx.reply_success(
            f"Gifted **{days} day{'s' if days != 1 else ''}** of premium to guild "
            f"`{guild_id}`.\nExpires: {when}\nNotification delivered: **{delivered_to}**",
            title="🎁 Premium gifted",
        )

    @admin_premium.command(name="link", aliases=["attach"])
    @_require_bot_owner()
    async def admin_premium_link(
        self, ctx: DiscoContext,
        guild_id: int,
        subscription_id: str,
    ) -> None:
        """Manually link a PayPal subscription to a guild.

        Use when a webhook delivery was missed (PayPal outage, server
        restart, etc.) and a guild's payment has cleared but they're
        still showing as non-premium. Pulls the live state from PayPal
        and writes it through to ``guild_premium``.
        """
        from services import entitlements
        from services.paypal import paypal_client, parse_iso8601_to_epoch
        client = paypal_client()
        if not client.configured:
            await ctx.reply_error("PayPal is not configured on this instance.")
            return
        try:
            sub = await client.get_subscription(subscription_id)
        except Exception as exc:
            await ctx.reply_error(f"PayPal lookup failed: `{exc}`")
            return
        paypal_status = (sub.get("status") or "").upper()
        status = {
            "ACTIVE":    "active",
            "APPROVED":  "active",
            "SUSPENDED": "suspended",
            "CANCELLED": "cancelled",
            "EXPIRED":   "expired",
        }.get(paypal_status, "active")
        billing_info = sub.get("billing_info") or {}
        period_end_epoch = parse_iso8601_to_epoch(billing_info.get("next_billing_time"))
        expires_epoch = period_end_epoch
        if status not in ("active", "cancelled"):
            import time as _time
            expires_epoch = _time.time() - 1.0
        await entitlements.link_paypal_subscription(
            int(guild_id), ctx.db,
            subscription_id=subscription_id,
            plan_id=sub.get("plan_id"),
            subscriber_user_id=None,
            status=status,
            current_period_end_epoch=period_end_epoch,
            expires_at_epoch=expires_epoch,
        )
        await ctx.reply_success(
            f"Linked PayPal subscription `{subscription_id}` ({paypal_status}) "
            f"to guild `{guild_id}`.",
            title="\U0001F517 Subscription linked",
        )

    @admin_premium.command(name="sync", aliases=["refresh"])
    @_require_bot_owner()
    async def admin_premium_sync(
        self, ctx: DiscoContext,
        guild_id: int | None = None,
    ) -> None:
        """Re-fetch PayPal state for one guild (or every PayPal-linked guild).

        Useful after a PayPal-side change that didn't fire a webhook (e.g.
        plan migration, support-ticket reactivation). For each row with a
        ``paypal_subscription_id`` we hit PayPal, write the current state
        through, and report the deltas.
        """
        from services import entitlements
        from services.paypal import paypal_client, parse_iso8601_to_epoch
        client = paypal_client()
        if not client.configured:
            await ctx.reply_error("PayPal is not configured on this instance.")
            return
        rows = await entitlements.list_premium_guilds(ctx.db)
        targets = [
            r for r in rows
            if r.get("paypal_subscription_id")
            and (guild_id is None or int(r["guild_id"]) == int(guild_id))
        ]
        if not targets:
            await ctx.reply_error("No PayPal-linked guilds found to sync.")
            return
        synced: list[str] = []
        for r in targets:
            sub_id = r["paypal_subscription_id"]
            gid = int(r["guild_id"])
            try:
                sub = await client.get_subscription(sub_id)
            except Exception as exc:
                synced.append(f"`{gid}` -- ❌ {exc}")
                continue
            paypal_status = (sub.get("status") or "").upper()
            status = {
                "ACTIVE":    "active",
                "APPROVED":  "active",
                "SUSPENDED": "suspended",
                "CANCELLED": "cancelled",
                "EXPIRED":   "expired",
            }.get(paypal_status, "active")
            billing_info = sub.get("billing_info") or {}
            period_end_epoch = parse_iso8601_to_epoch(billing_info.get("next_billing_time"))
            expires_epoch = period_end_epoch
            if status not in ("active", "cancelled"):
                import time as _time
                expires_epoch = _time.time() - 1.0
            await entitlements.link_paypal_subscription(
                gid, ctx.db,
                subscription_id=sub_id,
                plan_id=sub.get("plan_id"),
                subscriber_user_id=None,
                status=status,
                current_period_end_epoch=period_end_epoch,
                expires_at_epoch=expires_epoch,
            )
            synced.append(f"`{gid}` -- {paypal_status} -> {status}")
        await ctx.reply(
            embed=card(
                f"\U0001F504 Synced {len(synced)} subscription(s)",
                description="\n".join(synced)[:4000] or "Nothing to sync.",
                color=C_GOLD,
            ).build(),
            mention_author=False,
        )


    # ── Group fixup ───────────────────────────────────────────────
    # Admin tooling for groups that ended up half-configured -- no
    # token_network bound, or no tradable pools created. Targeted
    # fix-up so existing healthy groups stay untouched.

    @admin.group(name="groups", aliases=["grp", "groupfix"], invoke_without_command=True)
    @_require_manage_guild()
    async def admin_groups(self, ctx: DiscoContext) -> None:
        """Admin tools for repairing broken mining groups."""
        prefix = ctx.prefix or ","
        body = (
            f"`{prefix}admin groups fixpools` -- for groups missing a "
            f"network or any pools, force a token symbol, randomize "
            f"the network, and create the standard MMTA + MSUN + MOON "
            f"vault pools. **Skips groups that already have at least one "
            f"pool**, so healthy groups are never touched.\n\n"
            f"`{prefix}admin groups fixpools dry` -- preview what would "
            f"change, no writes.\n"
        )
        await ctx.reply(
            embed=card("\U0001F527 Group Admin", description=body, color=C_INFO).build(),
            mention_author=False,
        )

    @admin_groups.command(name="fixpools", aliases=["fix", "reseed"])
    @_require_manage_guild()
    async def admin_groups_fixpools(
        self, ctx: DiscoContext,
        mode: str = "",
    ) -> None:
        """Fix groups that are missing token_network or any pools.

        Pass ``dry`` to preview without writing. The command:
            1. Lists every mining_group in this guild.
            2. Skips groups that already have token_network bound AND
               at least one pool whose token_a/token_b matches their
               token_symbol -- these are healthy, leave them alone.
            3. For broken groups: forces token_symbol if missing
               (uses the group name's first 4 alpha chars uppercased,
               or a random 4-letter code), randomises token_network
               (Sun Network / Moneta Chain), then creates vault
               pools against MMTA, MSUN, and MOON, seeded at
               $GROUP_VAULT_POOL_SEED_USD per side at oracle prices.
            4. Pools that already exist are left alone (idempotent).
        """
        import random
        import string

        dry = (mode or "").lower() in ("dry", "preview", "test")
        groups = await ctx.db.get_all_mining_groups(ctx.guild_id)
        if not groups:
            await ctx.reply_error("No mining groups in this guild.")
            return

        # Pre-fetch all pools once -- O(1) DB call beats O(N*M) per-group
        # gets when the guild has lots of groups.
        all_pools = await ctx.db.get_all_pools(ctx.guild_id)
        pool_keys: set[str] = set()
        for p in all_pools:
            ta = (p.get("token_a") or "").upper()
            tb = (p.get("token_b") or "").upper()
            if ta and tb:
                pool_keys.add(ctx.db.make_pool_id(ta, tb)[0])

        # Standard pair targets for every group token. mMTA and mSUN cover
        # the wrapped-mining-coin trade routes; MOON gives a Lunar-Mint
        # exit so the token isn't stranded if its mining chain dies.
        STANDARD_PAIRS = ("MMTA", "MSUN", "MOON")
        NETWORK_CHOICES = ("Sun Network", "Moneta Chain")

        async def _price(sym: str) -> float:
            row = await ctx.db.get_price(sym, ctx.guild_id)
            if row and float(row.get("price") or 0.0) > 0:
                return float(row["price"])
            cfg = Config.TOKENS.get(sym, {})
            sp = float(cfg.get("start_price") or 0.0)
            return sp if sp > 0 else 1.0

        def _gen_symbol(name: str) -> str:
            """Pull the first 4 alpha chars from the name, uppercase, fallback random."""
            stripped = "".join(ch for ch in (name or "") if ch.isalpha()).upper()
            if len(stripped) >= 3:
                return stripped[:5]
            return "GRP" + "".join(random.choices(string.ascii_uppercase, k=2))

        plan: list[dict[str, Any]] = []
        for grp in groups:
            sym = (grp.get("token_symbol") or "").upper()
            net = grp.get("token_network") or ""
            # Healthy groups have BOTH a network AND at least one pool
            # whose pair is their token_symbol. Skip those.
            if sym and net:
                has_pool = any(
                    sym in (
                        (p.get("token_a") or "").upper(),
                        (p.get("token_b") or "").upper(),
                    )
                    for p in all_pools
                )
                if has_pool:
                    continue

            # Otherwise this group is broken -- queue a fix.
            new_sym = sym or _gen_symbol(str(grp.get("name") or ""))
            new_net = net or random.choice(NETWORK_CHOICES)
            missing_pairs = []
            for pair in STANDARD_PAIRS:
                pid, _, _ = ctx.db.make_pool_id(new_sym, pair)
                if pid not in pool_keys:
                    missing_pairs.append(pair)

            plan.append({
                "group_id": grp["group_id"],
                "name": grp.get("name") or grp["group_id"],
                "old_symbol": sym, "old_network": net,
                "new_symbol": new_sym, "new_network": new_net,
                "missing_pairs": missing_pairs,
            })

        if not plan:
            await ctx.reply_success(
                "Every group in this guild already has a network and at "
                "least one tradable pool -- nothing to fix.",
                title="✅ Groups OK",
            )
            return

        # ── render the plan / write the changes ──────────────────────
        lines: list[str] = []
        for p in plan[:20]:  # cap visible rows for embed limits
            tag = []
            if p["old_symbol"] != p["new_symbol"]:
                tag.append(f"sym `{p['old_symbol'] or '<none>'}` -> `{p['new_symbol']}`")
            if p["old_network"] != p["new_network"]:
                tag.append(f"net `{p['old_network'] or '<none>'}` -> `{p['new_network']}`")
            if p["missing_pairs"]:
                tag.append("pools: " + ", ".join(f"`{p['new_symbol']}/{x}`" for x in p["missing_pairs"]))
            lines.append(f"**{p['name']}** -- " + (" · ".join(tag) or "no changes"))
        if len(plan) > 20:
            lines.append(f"_…and {len(plan) - 20} more_")

        if dry:
            await ctx.reply(
                embed=card(
                    f"\U0001F50D Dry run -- {len(plan)} group(s) would be fixed",
                    description="\n".join(lines)[:4000],
                    color=C_WARNING,
                ).footer(f"Re-run without 'dry' to apply.").build(),
                mention_author=False,
            )
            return

        # ── apply ────────────────────────────────────────────────────
        seed_usd = float(Config.GROUP_VAULT_POOL_SEED_USD)
        applied = 0
        pools_created = 0
        for p in plan:
            try:
                if p["old_symbol"] != p["new_symbol"] or p["old_network"] != p["new_network"]:
                    await ctx.db.set_group_token_network(
                        ctx.guild_id, p["group_id"],
                        p["new_symbol"], p["new_network"],
                    )
                # Make sure the token row exists in guild_tokens so
                # get_price / oracle code doesn't choke on the pair.
                await ctx.db.execute(
                    """
                    INSERT INTO guild_tokens (guild_id, symbol, name, emoji, consensus, network, start_price)
                    VALUES ($1, $2, $3, '\U0001F4B0', 'PoW', 'Moon Network', 0.01)
                    ON CONFLICT (guild_id, symbol) DO NOTHING
                    """,
                    ctx.guild_id, p["new_symbol"], p["name"],
                )
                tok_price = await _price(p["new_symbol"])
                for pair in p["missing_pairs"]:
                    pair_price = await _price(pair)
                    # create_vault_pool seeds reserves directly + locks
                    # the pool, matching how create-token-network seeds
                    # the original mining-chain vault pool.
                    pool = await ctx.db.create_vault_pool(
                        ctx.guild_id, p["new_symbol"], pair,
                        tok_price, pair_price,
                    )
                    if pool:
                        pools_created += 1
                applied += 1
            except Exception as exc:
                log.exception(
                    "admin groups fixpools: %s failed: %s", p["group_id"], exc,
                )
                lines.append(f"❌ **{p['name']}** -- {exc}")

        await ctx.reply(
            embed=card(
                f"✅ Fixed {applied} / {len(plan)} group(s)",
                description=(
                    f"Created **{pools_created}** new vault pool(s) "
                    f"(${seed_usd:,.0f} per side at oracle prices).\n\n"
                    + "\n".join(lines)[:3500]
                ),
                color=C_SUCCESS,
            ).build(),
            mention_author=False,
        )


    # ── Fight lock admin ──────────────────────────────────────────
    # Companion to services/fight_lock.py. Used when a player's lock
    # gets wedged past the 8-minute TTL safety valve (rare -- usually
    # a bot-side bug rather than user error).

    @admin.group(name="fightlock", aliases=["flock", "fight"], invoke_without_command=True)
    @_require_manage_guild()
    async def admin_fightlock(self, ctx: DiscoContext) -> None:
        """Inspect / clear active fight locks (one-fight-at-a-time blocker)."""
        prefix = ctx.prefix or ","
        body = (
            f"`{prefix}admin fightlock peek <user>`  -  show their current lock\n"
            f"`{prefix}admin fightlock clear <user>` -  force-clear a stuck lock\n\n"
            "Locks normally self-clear after 8 minutes. Use `clear` only "
            "when a player reports being stuck longer than that."
        )
        await ctx.reply(
            embed=card("\U0001F510 Fight Lock Admin", description=body, color=C_INFO).build(),
            mention_author=False,
        )

    @admin_fightlock.command(name="peek", aliases=["status", "show"])
    @_require_manage_guild()
    async def admin_fightlock_peek(
        self, ctx: DiscoContext, target: discord.Member,
    ) -> None:
        """Show a player's active fight lock, if any."""
        from services import fight_lock as _fl
        row = await _fl.peek(ctx.db, ctx.guild_id, target.id)
        if not row:
            await ctx.reply_success(
                f"{target.mention} has no active fight lock.",
                title="\U0001F513 Free",
            )
            return
        kind = str(row.get("lock_kind") or "?")
        label = _fl.KIND_LABELS.get(kind, kind)
        rem = int(row.get("seconds_remaining") or 0)
        ref = row.get("lock_ref") or "-"
        b = card(
            f"\U0001F510 Fight Lock -- {target.display_name}",
            color=C_WARNING,
        )
        b.field("Kind", f"`{kind}` ({label})", True)
        b.field("Remaining", f"~{rem}s" if rem > 0 else "expired", True)
        b.field("Ref", f"`{ref}`", False)
        await ctx.reply(embed=b.build(), mention_author=False)

    @admin_fightlock.command(name="clear", aliases=["release", "free"])
    @_require_manage_guild()
    async def admin_fightlock_clear(
        self, ctx: DiscoContext, target: discord.Member,
    ) -> None:
        """Force-clear a player's fight lock."""
        from services import fight_lock as _fl
        n = await _fl.clear_user(ctx.db, ctx.guild_id, target.id)
        if n > 0:
            await ctx.reply_success(
                f"Cleared {target.mention}'s active fight lock.",
                title="\U0001F513 Cleared",
            )
        else:
            await ctx.reply_error(
                f"{target.mention} had no lock to clear (maybe already expired?)."
            )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Admin(bot))
