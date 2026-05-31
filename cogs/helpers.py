"""
cogs/helpers.py  -  DRS Terminal (Discoin Revenue Service).

DRS operators are trusted auditors who can investigate player accounts and
economy health without players knowing. All actions are logged. Sensitive
results are DMed back to the operator for privacy.

Operators can:
  - View any player's full profile (balances, holdings, stakes, items)
  - View full transaction history for any player
  - View economy-wide stats (supply, top holders, GDP)
  - Flag / unflag suspicious players with reasons
  - Compare two players (detect suspicious P2P transfers)
  - Reset a player's command cooldowns
  - View recent reports

Operators CANNOT:
  - Edit balances, tokens, or economy settings
  - Grant/revoke permissions or roles
  - Access admin configuration
  - Create/delete tokens or validators

Access is granted via: .admin beta grant drs_commands @role  OR  .admin beta grant drs_commands @user
"""
from __future__ import annotations

import logging

import io

import discord
from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only, no_bots, ensure_registered
from core.framework.scale import to_human
from core.framework.ui import mention as _mention
from core.framework.staff_audit import (
    SCOPE_DRS,
    SEVERITY_INFO,
    SEVERITY_WARN,
    build_audit_embeds,
    log_staff_action,
    recent_staff_actions,
)
from core.framework.ui import (
    C_AMBER,
    C_ERROR,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_PINK,
    C_PURPLE,
    C_SUCCESS,
    C_TEAL,
    C_WARNING,
    CategoryPaginator,
    fmt_token,
    fmt_ts,
    fmt_usd,
)

log = logging.getLogger(__name__)

_MAX_TX = 30  # max transactions the txlog command will show


class _MemberOrID(commands.Converter):
    """Accept @mention, display name, or raw user ID in a single argument."""

    async def convert(self, ctx: "DiscoContext", argument: str) -> discord.Member | discord.User:
        try:
            return await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass
        try:
            uid = int(argument)
        except ValueError:
            raise commands.BadArgument(f"Could not find member or user ID: {argument!r}")
        if ctx.guild:
            member = ctx.guild.get_member(uid)
            if member:
                return member
        try:
            return await ctx.bot.fetch_user(uid)
        except discord.NotFound:
            raise commands.BadArgument(f"No user found with ID {uid}.")


async def _log_drs_action(bot: Discoin, guild_id: int, operator_id: int, action: str, target_id: int = None, details: str = "") -> None:
    """Log a DRS action to the audit log."""
    await bot.db.execute(
        "INSERT INTO helper_audit_log (guild_id, helper_id, action, target_id, details) VALUES ($1, $2, $3, $4, $5)",
        guild_id, operator_id, action, target_id, details,
    )


async def _send_private(ctx: DiscoContext, *embeds: discord.Embed) -> None:
    """DM results to the operator. If invoked in a server, delete the command message.
    Falls back to an ephemeral channel reply if DMs are closed."""
    # Try to DM the results
    try:
        for embed in embeds:
            await ctx.author.send(embed=embed)
        if ctx.guild:
            try:
                await ctx.message.delete()
            except Exception:
                pass
            try:
                await ctx.reply(
                    embed=card("DRS Terminal", description="Results sent to your DMs.", color=C_NAVY).build(),
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                    delete_after=5,
                )
            except Exception:
                pass
    except discord.Forbidden:
        # DMs closed - fall back to channel reply
        for embed in embeds:
            await ctx.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())


class Helpers(commands.Cog):
    """DRS Terminal commands  -  auditor tools for trusted operators."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_check(self, ctx: DiscoContext) -> bool:
        """Gate the entire .drs group behind the drs_commands beta feature."""
        if not ctx.guild:
            raise commands.CheckFailure("DRS Terminal commands can only be used in a server.")
        from core.framework.middleware import check_beta_access
        if not await check_beta_access(self.bot, ctx.guild, ctx.author, "drs_commands"):
            raise commands.CheckFailure(
                "You don't have **DRS Terminal** access.\n"
                "An admin can grant it with `.admin beta grant drs_commands @role` or `.admin beta grant drs_commands @user`."
            )
        return True

    # ── Help ──────────────────────────────────────────────────────────────────

    def _build_drs_categories(self, p: str) -> dict[str, list[discord.Embed]]:
        """Build the DRS help category dict for the dropdown paginator."""
        def _page(title: str, lines: list[str], color=C_NAVY) -> discord.Embed:
            _b = card(title, color=color)
            for i in range(0, len(lines), 10):
                chunk = lines[i:i + 10]
                _b.field("\u200b", "\n".join(chunk), False)
            _b.footer("All sensitive results are sent to your DMs")
            return _b.build()

        categories: dict[str, list[discord.Embed]] = {

            "\U0001F50D Investigation": [_page("\U0001F50D DRS - Investigation", [
                f"`{p}drs profile @user`  -  full player profile",
                f"  Aliases: `{p}drs lookup`, `{p}drs check`",
                f"`{p}drs txlog @user [limit]`  -  transaction history (up to {_MAX_TX})",
                f"  Aliases: `{p}drs tx`, `{p}drs transactions`, `{p}drs history`",
                f"`{p}drs timeline @user [days]`  -  full activity timeline + cumulative-wealth chart",
                f"  Aliases: `{p}drs activity`, `{p}drs wealthline`",
                f"`{p}drs compare @user1 @user2`  -  P2P transfer audit between two players",
                f"  Aliases: `{p}drs cmp`, `{p}drs p2p`",
                f"`{p}drs economy`  -  server economy snapshot",
                f"  Aliases: `{p}drs eco`, `{p}drs gdp`, `{p}drs supply`",
                f"`{p}drs fish @user`  -  fishing stats for a player",
                f"`{p}drs farm @user`  -  farming stats for a player",
            ], color=C_INFO)],

            "\U0001F4B0 Wealth Surfaces": [
                _page("\U0001F4B0 DRS - Wealth Surfaces (1/3)", [
                    f"`{p}drs networth @user`  -  full breakdown across 26 categories + chart",
                    f"  Aliases: `{p}drs nw`, `{p}drs wealth`, `{p}drs breakdown`",
                    f"`{p}drs stakes @user`  -  NPC yield-farm stakes + chart",
                    f"  Aliases: `{p}drs yield`, `{p}drs farm-stakes`",
                    f"`{p}drs validator @user`  -  PoS validator + delegations IN/OUT + chart",
                    f"  Aliases: `{p}drs pos`, `{p}drs blocks`",
                    f"`{p}drs mining @user`  -  rigs, hashrate, solo/pool mode",
                    f"  Aliases: `{p}drs rigs`, `{p}drs hashrate`",
                    f"`{p}drs lp @user`  -  LP positions priced from pool reserves",
                    f"  Aliases: `{p}drs liquidity`, `{p}drs pools`",
                ], color=C_GOLD),
                _page("\U0001F4B0 DRS - Wealth Surfaces (2/3)", [
                    f"`{p}drs stones @user`  -  all five stones (hash/lock/vault/gamba/liq) + level/XP",
                    f"  Alias: `{p}drs leaderboard-stones`",
                    f"`{p}drs gamba @user`  -  Gamba Network staked positions + pending GBC",
                    f"  Aliases: `{p}drs gambastake`, `{p}drs gamba-stakes`",
                    f"`{p}drs lunar @user`  -  Moon Network Tier-1 lunar mints + Tier-2 Moon Pool",
                    f"  Aliases: `{p}drs moon`, `{p}drs lunar-mint`",
                    f"`{p}drs safety @user`  -  Safety Module VTR/DSY positions",
                    f"  Aliases: `{p}drs sm`, `{p}drs safety-module`",
                    f"`{p}drs discfun @user`  -  Disc.Fun proto-token holdings + staked",
                    f"  Aliases: `{p}drs disc-fun`, `{p}drs dfun`",
                    f"`{p}drs nft @user`  -  owned NFTs grouped by collection + rarity",
                    f"  Aliases: `{p}drs nfts`, `{p}drs collection`",
                ], color=C_GOLD),
                _page("\U0001F4B0 DRS - Wealth Surfaces (3/3)", [
                    f"`{p}drs delve @user`  -  dungeon ore stakes + party + pending RUNE",
                    f"  Alias: `{p}drs dungeon`",
                    f"`{p}drs buddy @user`  -  FREN stake + slots + pending BUD",
                    f"  Aliases: `{p}drs buddies`, `{p}drs fren`",
                    f"`{p}drs crafting @user`  -  INGOT stake + pending FORGE + crafted inventory",
                    f"  Aliases: `{p}drs craft`, `{p}drs forge`",
                    f"`{p}drs savings @user`  -  all savings deposits across every symbol",
                    f"  Aliases: `{p}drs deposits`, `{p}drs savings-deposits`",
                    f"`{p}drs trades @user [limit]`  -  trade aggregates + recent BUY/SELL/SWAP",
                    f"  Aliases: `{p}drs pnl`, `{p}drs trade-history`",
                    f"`{p}drs work @user`  -  active job + WORK tx aggregates by symbol",
                    f"  Aliases: `{p}drs job`, `{p}drs career`",
                    f"`{p}drs games @user [limit]`  -  gambling session log + win/loss chart",
                    f"  Aliases: `{p}drs gambling`, `{p}drs play-history`",
                    f"`{p}drs loan @user`  -  outstanding loan + collateral + health ratio",
                    f"  Aliases: `{p}drs loans`, `{p}drs debt`",
                ], color=C_GOLD),
            ],

            "\U0001F6A9 Flags & Reports": [_page("\U0001F6A9 DRS - Flags & Reports", [
                f"`{p}drs flag @user <reason>`  -  flag a player as suspicious",
                f"  Aliases: `{p}drs mark`, `{p}drs suspicious`",
                f"`{p}drs unflag @user`  -  remove a flag",
                f"  Aliases: `{p}drs clear`, `{p}drs clearflag`",
                f"`{p}drs flagged`  -  view all currently flagged players",
                f"  Aliases: `{p}drs flags`, `{p}drs suspects`",
                f"`{p}drs reports`  -  open reports",
                f"  Alias: `{p}drs bugs`",
            ], color=C_WARNING)],

            "\U0001F4CB Audit": [_page("\U0001F4CB DRS - Audit Feed", [
                f"`{p}drs audit [limit]`  -  recent DRS audit entries",
                f"  Default limit: 50  -  maximum: 200",
                f"  Example: `{p}drs audit 20`",
                f"",
                f"`{p}drs log`  -  your personal DRS action history",
                f"",
                f"Audit feed pulls from the unified staff audit log scoped to",
                f"`drs`. Use `{p}admin audit` for the other staff surface.",
            ], color=C_NAVY)],

            "\U0001F465 Account": [_page("\U0001F465 DRS - Account & Activity", [
                f"`{p}drs daily @user`  -  daily-claim streak + last claim + eligibility",
                f"  Aliases: `{p}drs streak`, `{p}drs daily-streak`",
                f"`{p}drs items @user`  -  consumable inventory (validator/yield guards, gambling saves)",
                f"  Aliases: `{p}drs consumables`, `{p}drs inventory`",
                f"`{p}drs wallets @user`  -  all DeFi addresses + per-network holdings priced",
                f"  Aliases: `{p}drs addresses`, `{p}drs wallet-list`",
                f"`{p}drs eat @user`  -  Eat the Rich stats: eats made, survivals, USD flow",
                f"  Aliases: `{p}drs eattherich`, `{p}drs classwar`",
                f"`{p}drs guild @user`  -  mining-group membership + roster + founder role",
                f"  Aliases: `{p}drs group`, `{p}drs mining-group`",
                f"`{p}drs prefs @user`  -  DM opt-ins, muted-network lists",
                f"  Aliases: `{p}drs preferences`, `{p}drs dm-settings`",
                f"`{p}drs locks @user`  -  active locks: validator/delegation, SM cooldown",
                f"  Aliases: `{p}drs cooldowns`, `{p}drs active-locks`",
                f"`{p}drs token <SYMBOL>`  -  GUILD-WIDE: supply distribution + top holders + concentration",
                f"  Aliases: `{p}drs coin`, `{p}drs sym`",
            ], color=C_PURPLE)],

            "⚖️ Equalizer": [_page("⚖️ DRS - Equalizer x-ray", [
                f"`{p}drs equalizer`  -  overview: pool, lifetime flow, top payers/recipients, Gini",
                f"  Aliases: `{p}drs eq`, `{p}drs redistribution`, `{p}drs taxubi`",
                f"`{p}drs eq cycles [page]`  -  paginated history of every cycle",
                f"  Aliases: `{p}drs eq history`, `{p}drs eq list`",
                f"`{p}drs eq cycle <#>`  -  drill into one cycle (#1 = newest)",
                f"  Aliases: `{p}drs eq xray`, `{p}drs eq drill`",
                f"`{p}drs eq user @target`  -  per-player tax + UBI history + cumulative chart",
                f"  Aliases: `{p}drs eq player`, `{p}drs eq history`",
                f"`{p}drs eq chart [gini|pool]`  -  PNG chart of Gini trend or pool flow",
                f"  Aliases: `{p}drs eq graph`, `{p}drs eq plot`",
                f"`{p}drs eq export`  -  CSV dump of the full log (DMed)",
                f"  Aliases: `{p}drs eq dump`, `{p}drs eq csv`, `{p}drs eq download`",
            ], color=C_GOLD)],
        }
        return categories

    @commands.group(name="drs", aliases=["drsterminal"], invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def drs(self, ctx: DiscoContext) -> None:
        """DRS Terminal - Discoin Revenue Service audit tools."""
        p = ctx.prefix or "."
        categories = self._build_drs_categories(p)
        await CategoryPaginator.send(ctx, categories)

    @drs.command(name="help")
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_help(self, ctx: DiscoContext) -> None:
        """Full DRS Terminal command reference. Usage: ,drs help"""
        p = ctx.prefix or "."
        categories = self._build_drs_categories(p)
        await CategoryPaginator.send(ctx, categories)

    @drs.command(name="audit")
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_audit(self, ctx: DiscoContext, limit: int = 50) -> None:
        """Show recent DRS audit entries. Usage: ,drs audit [limit]"""
        if limit < 1 or limit > 200:
            await ctx.reply_error("limit must be between 1 and 200")
            return
        entries = await recent_staff_actions(
            ctx.db, guild_id=ctx.guild_id, scope=SCOPE_DRS, limit=limit,
        )
        pages = build_audit_embeds(entries, scope=SCOPE_DRS, guild=ctx.guild)
        if not pages:
            embed = (
                card("\U0001F4CB DRS Audit", color=C_NAVY)
                .description("No audit entries found for the DRS scope.")
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return
        if len(pages) > 1:
            await CategoryPaginator.send(ctx, {"\U0001F4CB DRS Audit": pages})
        else:
            await ctx.reply(embed=pages[0], mention_author=False)

    # ── Profile ───────────────────────────────────────────────────────────────

    @drs.command(name="profile", aliases=["lookup", "check"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_profile(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Full player profile - DMed to you for privacy."""
        from services.net_worth import compute_net_worth
        from configs.items_config import SHOP_ITEMS

        uid, gid = target.id, ctx.guild_id
        nw = await compute_net_worth(uid, gid, ctx.db)
        user_row = await ctx.db.get_user(uid, gid)
        if not user_row:
            await ctx.reply_error(f"**{target.display_name}** hasn't registered yet.")
            return

        wallet = nw.wallet
        bank = nw.bank
        net_worth = nw.total

        holding_lines: list[str] = []
        for h in nw.holdings:
            holding_lines.append(f"**{h['symbol']}**: {to_human(int(h['amount'])):,.4f} (${h['usd_value']:,.2f})")

        stake_lines: list[str] = []
        for s in nw.stakes:
            validator_name = s.get("name", s.get("validator_id", "?"))
            emoji = s.get("emoji", "")
            stake_lines.append(
                f"{emoji} **{validator_name}** - {fmt_token(to_human(int(s['amount'])), s['symbol'])} (${s['usd_value']:,.2f})"
            )

        # Safety Module positions (VTR/DSY) -- shown alongside yield-farm
        # stakes so the profile audit reflects all yield-bearing deposits.
        sm_lines: list[str] = []
        for sm_sym in ("VTR", "DSY"):
            sm_row = await ctx.db.get_sm_stake(uid, gid, sm_sym)
            if not sm_row or int(sm_row.get("amount", 0)) <= 0:
                continue
            sm_emoji = Config.TOKENS.get(sm_sym, {}).get("emoji", "")
            sm_staked_h = to_human(int(sm_row["amount"]))
            sm_price_row = await ctx.db.get_price(sm_sym, gid)
            sm_price = float(sm_price_row["price"]) if sm_price_row else 0.0
            sm_status = " (🔒 cooldown)" if sm_row.get("cooldown_at") else ""
            sm_lines.append(
                f"{sm_emoji} **{sm_sym}** -- {fmt_token(sm_staked_h, sm_sym)} "
                f"(${sm_staked_h * sm_price:,.2f}){sm_status}"
            )

        job_row = await ctx.db.get_user_job(uid, gid)
        job_name = job_row["job_id"] if job_row else "HOMELESS"
        work_count = job_row.get("work_count", 0) if job_row else 0
        total_earned = job_row.h("total_earned") if job_row else 0.0

        daily_streak = user_row.get("daily_streak", 0)
        created_at = user_row.get("created_at")

        eat_row = await ctx.db.fetch_one(
            "SELECT * FROM exploit_stats WHERE user_id=$1 AND guild_id=$2", uid, gid,
        )

        tx_history = await ctx.db.get_user_tx_history(uid, gid, limit=5)

        footer_text = f"DRS audit by {ctx.author.display_name} - ID: {uid}"

        # Embed 1: Balances & Account
        reg_str = fmt_ts(created_at, "%m/%d/%Y %H:%M") if created_at else ""
        e1 = (
            card(f"[DRS] Player Profile - {target.display_name}", color=C_INFO)
            .author(target.display_name, icon_url=target.display_avatar.url)
            .field("Wallet", fmt_usd(wallet), True)
            .field("Bank", fmt_usd(bank), True)
            .field("Net Worth", fmt_usd(net_worth), True)
            .field("Job", f"**{job_name}**\n{work_count} sessions | {fmt_usd(total_earned)} earned", True)
            .field("Daily Streak", str(daily_streak), True)
            .field("Discord ID", str(uid), True)
        )
        if reg_str:
            e1.field("Registered", reg_str, True)

        if eat_row:
            att = eat_row.get("heists_attempted", 0)
            won = eat_row.get("heists_won", 0)
            devoured = eat_row.h("total_stolen")
            targeted = eat_row.get("times_targeted", 0)
            survived = eat_row.get("times_defended", 0)
            lost = eat_row.h("total_lost")
            if att > 0 or targeted > 0:
                e1.field(
                    "Eat the Rich Stats",
                    f"Eats: **{won}**/{att} won | {fmt_usd(devoured)} devoured\n"
                    f"Hunted: **{survived}**/{targeted} survived | {fmt_usd(lost)} lost",
                    False,
                )

        e1.footer(footer_text)
        embeds: list[discord.Embed] = [e1.build()]

        # Embed 2: Holdings & Stakes
        has_holdings = len(holding_lines) > 0
        has_stakes = len(stake_lines) > 0
        has_sm = len(sm_lines) > 0
        if has_holdings or has_stakes or has_sm:
            e2 = card(f"[DRS] Holdings & Stakes - {target.display_name}", color=C_PURPLE)
            if has_holdings:
                crypto_text = "\n".join(holding_lines[:15])
                if len(holding_lines) > 15:
                    crypto_text += f"\n+{len(holding_lines) - 15} more..."
                e2.field(f"Crypto Holdings ({fmt_usd(nw.cefi_crypto)})", crypto_text, False)
            if has_stakes:
                stake_text = "\n".join(stake_lines[:15])
                if len(stake_lines) > 15:
                    stake_text += f"\n+{len(stake_lines) - 15} more..."
                e2.field(f"Staking Positions ({fmt_usd(nw.stake_value)})", stake_text, False)
            if has_sm:
                e2.field(
                    f"Safety Module ({fmt_usd(nw.safety_module_value)})",
                    "\n".join(sm_lines), False,
                )
            e2.footer(footer_text)
            embeds.append(e2.build())

        # Embed 3: Items
        _STONE_KEYS = [
            ("hashstone", nw.hashstone, SHOP_ITEMS.get("hashstone", {})),
            ("lockstone", nw.lockstone, SHOP_ITEMS.get("lockstone", {})),
            ("vaultstone", nw.vaultstone, SHOP_ITEMS.get("vaultstone", {})),
            ("liqstone", nw.liqstone, SHOP_ITEMS.get("liqstone", {})),
        ]
        stone_fields: list[tuple[str, str]] = []
        for key, stone, cfg in _STONE_KEYS:
            if not stone or not cfg:
                continue
            if cfg.get("disabled"):
                continue
            emoji = cfg.get("emoji", "")
            name = cfg.get("name", key)
            lv = stone["level"]
            xp = stone["xp"]
            staked = stone.h("staked_amount")
            max_lv = cfg.get("max_level", 100)
            base = cfg.get("xp_per_level_base", 80)
            fill = int(10 * min((xp - base * lv * (lv - 1) // 2) / max(1, base * lv), 1))
            bar = "\u2588" * fill + "\u2591" * (10 - fill)
            if lv < max_lv:
                xp_start = base * lv * (lv - 1) // 2
                xp_next = base * (lv + 1) * lv // 2
                xp_str = f"{xp - xp_start:,.0f}/{xp_next - xp_start:,.0f} XP"
                line = f"Lv **{lv}**/{max_lv} | {fmt_usd(staked)} staked\n`{bar}` {xp_str}"
            else:
                line = f"Lv **{lv}/{max_lv} MAX** | {fmt_usd(staked)} staked"
            stone_fields.append((f"{emoji} {name}", line))

        validator_guard_count = nw.validator_guard_count
        yield_guard_count = nw.yield_guard_count
        consumable_parts: list[str] = []
        if validator_guard_count:
            consumable_parts.append(f"Validator Guards: **{validator_guard_count}**")
        if yield_guard_count:
            consumable_parts.append(f"Yield Guards: **{yield_guard_count}**")

        if stone_fields or consumable_parts:
            e3 = card(f"[DRS] Items - {target.display_name}", color=C_AMBER)
            for fname, fval in stone_fields:
                e3.field(fname, fval, True)
            if consumable_parts:
                e3.field("Consumables", "\n".join(consumable_parts), False)
            e3.footer(footer_text)
            embeds.append(e3.build())

        # Embed 4: Recent Transactions
        if tx_history:
            tx_lines: list[str] = []
            for tx in tx_history:
                ts_str = fmt_ts(tx.get("ts"))
                tx_type = tx.get("tx_type", "?")
                parts: list[str] = []
                sym_in = tx.get("symbol_in")
                amt_in = tx.get("amount_in")
                sym_out = tx.get("symbol_out")
                amt_out = tx.get("amount_out")
                if sym_in and amt_in:
                    parts.append(f"{fmt_token(tx.h('amount_in'), sym_in)}")
                if sym_out and amt_out:
                    arrow = " -> " if parts else ""
                    parts.append(f"{arrow}{fmt_token(tx.h('amount_out'), sym_out)}")
                detail = "".join(parts) if parts else ""
                gas = tx.h("gas_fee")
                gas_str = f" (gas {fmt_usd(gas)})" if gas > 0 else ""
                tx_lines.append(f"`{tx_type}` {detail}{gas_str} {ts_str}")
            e4 = card(f"[DRS] Recent Transactions - {target.display_name}", color=C_NAVY)
            e4.description("\n".join(tx_lines))
            e4.footer(footer_text)
            embeds.append(e4.build())

        await _send_private(ctx, *embeds)
        await _log_drs_action(
            self.bot, ctx.guild_id, ctx.author.id, "profile", target.id,
            f"Viewed {target.display_name}'s full profile",
        )
        await log_staff_action(
            ctx.db,
            scope=SCOPE_DRS,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="profile",
            target_id=target.id,
            severity=SEVERITY_INFO,
            details=f"Viewed {target.display_name}'s full profile",
        )

    # ── TX Log ────────────────────────────────────────────────────────────────

    @drs.command(name="txlog", aliases=["tx", "transactions", "history"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_txlog(self, ctx: DiscoContext, target: _MemberOrID, limit: int = 20) -> None:
        """Full transaction history for a player - DMed to you."""
        limit = max(1, min(limit, _MAX_TX))
        uid, gid = target.id, ctx.guild_id

        rows = await ctx.db.get_user_tx_history(uid, gid, limit=limit)
        if not rows:
            await ctx.reply_error(f"No transactions found for **{target.display_name}**.")
            return

        lines: list[str] = []
        for tx in rows:
            ts_str = fmt_ts(tx.get("ts"))
            tx_type = tx.get("tx_type", "?")
            parts: list[str] = []
            sym_in = tx.get("symbol_in")
            amt_in = tx.get("amount_in")
            sym_out = tx.get("symbol_out")
            amt_out = tx.get("amount_out")
            if sym_in and amt_in:
                parts.append(fmt_token(tx.h("amount_in"), sym_in))
            if sym_out and amt_out:
                arrow = " -> " if parts else ""
                parts.append(f"{arrow}{fmt_token(tx.h('amount_out'), sym_out)}")
            detail = "".join(parts) if parts else ""
            gas = tx.h("gas_fee")
            gas_str = f" (gas {fmt_usd(gas)})" if gas > 0 else ""
            tx_hash = tx.get("tx_hash", "")
            hash_str = f" `{tx_hash[:8]}`" if tx_hash else ""
            lines.append(f"`{ts_str}` **{tx_type}**{hash_str} {detail}{gas_str}")

        # Chunk into pages of 15 lines each (embed field limit)
        pages: list[discord.Embed] = []
        chunk_size = 15
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            page_num = i // chunk_size + 1
            total_pages = (len(lines) + chunk_size - 1) // chunk_size
            e = card(
                f"[DRS] TX Log - {target.display_name}",
                description="\n".join(chunk),
                color=C_NAVY,
            ).footer(f"Page {page_num}/{total_pages} - {len(rows)} txs shown - audit by {ctx.author.display_name} (DRS) - ID: {uid}")
            pages.append(e.build())

        await _send_private(ctx, *pages)
        await _log_drs_action(
            self.bot, ctx.guild_id, ctx.author.id, "txlog", uid,
            f"Viewed {len(rows)} transactions for {target.display_name}",
        )
        await log_staff_action(
            ctx.db,
            scope=SCOPE_DRS,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="txlog",
            target_id=uid,
            severity=SEVERITY_INFO,
            details=f"Viewed {len(rows)} transactions for {target.display_name}",
        )

    # ── Compare ───────────────────────────────────────────────────────────────

    @drs.command(name="compare", aliases=["cmp", "p2p"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_compare(self, ctx: DiscoContext, user1: _MemberOrID, user2: _MemberOrID) -> None:
        """Audit P2P transfers between two players - DMed to you."""
        gid = ctx.guild_id

        # Check for transfers involving both users by time-proximity matching
        raw_rows = await ctx.db.fetch_all(
            """
            SELECT * FROM transactions
            WHERE guild_id = $1
              AND tx_type IN ('TRANSFER', 'SEND', 'P2P', 'GIFT')
              AND (user_id = $2 OR user_id = $3)
            ORDER BY ts DESC
            LIMIT 100
            """,
            gid, user1.id, user2.id,
        )
        # Filter to rows that might relate to each other by proximity in time
        # (same type, same amount, within 5 seconds of each other)
        u1_txs = [r for r in raw_rows if r["user_id"] == user1.id]
        u2_txs = [r for r in raw_rows if r["user_id"] == user2.id]
        matched: list[dict] = []
        for t1 in u1_txs:
            for t2 in u2_txs:
                t1_ts = float(t1.get("ts") or 0)
                t2_ts = float(t2.get("ts") or 0)
                if abs(t1_ts - t2_ts) <= 5:
                    matched.append(t1)
                    matched.append(t2)
        rows = matched

        u1_nw = await ctx.db.fetch_one("SELECT wallet, bank FROM users WHERE user_id=$1 AND guild_id=$2", user1.id, gid)
        u2_nw = await ctx.db.fetch_one("SELECT wallet, bank FROM users WHERE user_id=$1 AND guild_id=$2", user2.id, gid)

        u1_bal = to_human((u1_nw.get("wallet") or 0) + (u1_nw.get("bank") or 0)) if u1_nw else 0.0
        u2_bal = to_human((u2_nw.get("wallet") or 0) + (u2_nw.get("bank") or 0)) if u2_nw else 0.0

        desc_lines = [
            f"**{user1.display_name}** (`{user1.id}`) - wallet+bank: {fmt_usd(u1_bal)}",
            f"**{user2.display_name}** (`{user2.id}`) - wallet+bank: {fmt_usd(u2_bal)}",
            "",
        ]

        if rows:
            desc_lines.append(f"**{len(rows)} related transaction(s) found:**\n")
            for tx in rows:
                ts_str = fmt_ts(tx.get("ts"))
                who = user1.display_name if tx["user_id"] == user1.id else user2.display_name
                tx_type = tx.get("tx_type", "?")
                sym_out = tx.get("symbol_out")
                amt_out = tx.get("amount_out")
                val_str = fmt_token(tx.h("amount_out"), sym_out) if sym_out and amt_out else ""
                desc_lines.append(f"`{ts_str}` **{who}** {tx_type} {val_str}")
        else:
            desc_lines.append("No direct P2P transfers found between these two players.")

        embed = (
            card(f"[DRS] P2P Comparison - {user1.display_name} vs {user2.display_name}", color=C_WARNING)
            .description("\n".join(desc_lines))
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )

        await _send_private(ctx, embed)
        await _log_drs_action(
            self.bot, ctx.guild_id, ctx.author.id, "compare", user1.id,
            f"Compared {user1.display_name} vs {user2.display_name}",
        )
        await log_staff_action(
            ctx.db,
            scope=SCOPE_DRS,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="compare",
            target_id=user1.id,
            severity=SEVERITY_INFO,
            details=(
                f"Compared {user1.display_name} ({user1.id}) vs "
                f"{user2.display_name} ({user2.id})"
            ),
        )

    # ── Economy Snapshot ──────────────────────────────────────────────────────

    @drs.command(name="economy", aliases=["eco", "gdp", "supply"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_economy(self, ctx: DiscoContext) -> None:
        """Server economy snapshot - total supply, top holders, GDP - DMed to you."""
        from services.net_worth import compute_bulk_net_worth

        gid = ctx.guild_id
        all_users = await ctx.db.get_all_guild_users(gid)

        if not all_users:
            await ctx.reply_error("No users registered in this server.")
            return

        total_wallet = sum(u.h("wallet") for u in all_users)
        total_bank = sum(u.h("bank") for u in all_users)
        total_liquid = total_wallet + total_bank
        player_count = len(all_users)

        # Net worth leaderboard
        user_val = await compute_bulk_net_worth(gid, ctx.db, exclude_user_id=ctx.bot.user.id)
        ranked = sorted(user_val.items(), key=lambda x: x[1], reverse=True)
        total_gdp = sum(v for _, v in ranked)
        avg_nw = total_gdp / max(len(ranked), 1)

        top_lines: list[str] = []
        for rank, (uid, nw) in enumerate(ranked[:10], 1):
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            pct = (nw / total_gdp * 100) if total_gdp > 0 else 0.0
            top_lines.append(f"`#{rank}` **{name}** - {fmt_usd(nw)} ({pct:.1f}%)")

        # Wealth concentration: top 10% share
        top_10_count = max(1, len(ranked) // 10)
        top_10_wealth = sum(v for _, v in ranked[:top_10_count])
        concentration_pct = (top_10_wealth / total_gdp * 100) if total_gdp > 0 else 0.0

        desc = "\n".join(top_lines) if top_lines else "No data."

        embed = (
            card("[DRS] Economy Snapshot", color=C_GOLD)
            .field("Registered Players", str(player_count), True)
            .field("Total Liquid Supply", fmt_usd(total_liquid), True)
            .field("GDP (Total Net Worth)", fmt_usd(total_gdp), True)
            .field("Wallet Supply", fmt_usd(total_wallet), True)
            .field("Bank Supply", fmt_usd(total_bank), True)
            .field("Avg Net Worth", fmt_usd(avg_nw), True)
            .field(f"Wealth Concentration (top {top_10_count})", f"{concentration_pct:.1f}% of GDP", True)
            .field("Top 10 by Net Worth", desc, False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )

        await _send_private(ctx, embed)
        await _log_drs_action(self.bot, ctx.guild_id, ctx.author.id, "economy")
        await log_staff_action(
            ctx.db,
            scope=SCOPE_DRS,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="economy",
            target_id=None,
            severity=SEVERITY_INFO,
            details=(
                f"players={player_count} gdp={total_gdp:.2f} "
                f"liquid={total_liquid:.2f}"
            ),
        )

    # ── Flag ──────────────────────────────────────────────────────────────────

    @drs.command(name="flag", aliases=["mark", "suspicious"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_flag(self, ctx: DiscoContext, target: _MemberOrID, *, reason: str) -> None:
        """Flag a player as suspicious. Visible to all DRS operators."""
        await _log_drs_action(
            self.bot, ctx.guild_id, ctx.author.id, "flag", target.id,
            reason[:500],
        )
        await log_staff_action(
            ctx.db,
            scope=SCOPE_DRS,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="flag",
            target_id=target.id,
            severity=SEVERITY_WARN,
            details=f"reason={reason[:400]}",
        )
        embed = (
            card("[DRS] Player Flagged", color=C_ERROR)
            .field("Player", f"{target.display_name} (`{target.id}`)", True)
            .field("Flagged by", ctx.author.display_name, True)
            .field("Reason", reason[:500], False)
            .footer("Use .drs unflag @user to remove this flag")
            .build()
        )
        await _send_private(ctx, embed)

    @drs.command(name="unflag", aliases=["clear", "clearflag"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_unflag(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Remove the active flag on a player."""
        await _log_drs_action(
            self.bot, ctx.guild_id, ctx.author.id, "unflag", target.id,
            f"Flag cleared by {ctx.author.display_name}",
        )
        await log_staff_action(
            ctx.db,
            scope=SCOPE_DRS,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="unflag",
            target_id=target.id,
            severity=SEVERITY_INFO,
            details=f"Flag cleared by {ctx.author.display_name}",
        )
        embed = (
            card("[DRS] Flag Removed", color=C_SUCCESS)
            .description(f"Flag cleared for **{target.display_name}** (`{target.id}`).")
            .footer(f"Cleared by {ctx.author.display_name}")
            .build()
        )
        await _send_private(ctx, embed)

    @drs.command(name="flagged", aliases=["flags", "suspects"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_flagged(self, ctx: DiscoContext) -> None:
        """View all currently flagged players in this server - DMed to you."""
        gid = ctx.guild_id

        # Get the most recent flag/unflag action per target_id
        rows = await ctx.db.fetch_all(
            """
            SELECT DISTINCT ON (target_id)
                target_id, action, details, helper_id, created_at
            FROM helper_audit_log
            WHERE guild_id = $1
              AND action IN ('flag', 'unflag')
              AND target_id IS NOT NULL
            ORDER BY target_id, created_at DESC
            """,
            gid,
        )

        active_flags = [r for r in rows if r["action"] == "flag"]

        if not active_flags:
            embed = card("[DRS] Flagged Players", description="No players are currently flagged.", color=C_INFO).build()
            await _send_private(ctx, embed)
            return

        lines: list[str] = []
        for r in active_flags:
            member = ctx.guild.get_member(int(r["target_id"]))
            name = member.display_name if member else f"User {r['target_id']}"
            flagged_by = ctx.guild.get_member(int(r["helper_id"]))
            flagged_by_name = flagged_by.display_name if flagged_by else f"Operator {r['helper_id']}"
            ts_str = fmt_ts(r["created_at"])
            reason = str(r.get("details", ""))[:80]
            if len(str(r.get("details", ""))) > 80:
                reason += "..."
            lines.append(
                f"**{name}** (`{r['target_id']}`)\n"
                f"> {reason}\n"
                f"> Flagged by {flagged_by_name} - {ts_str}"
            )

        embed = (
            card(f"[DRS] Flagged Players ({len(active_flags)})", color=C_ERROR)
            .description("\n\n".join(lines))
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        await _send_private(ctx, embed)
        await _log_drs_action(self.bot, ctx.guild_id, ctx.author.id, "view_flagged")
        await log_staff_action(
            ctx.db,
            scope=SCOPE_DRS,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="view_flagged",
            target_id=None,
            severity=SEVERITY_INFO,
            details=f"Viewed {len(active_flags)} flagged players",
        )

    # ── Reports ───────────────────────────────────────────────────────────────

    @drs.command(name="reports", aliases=["bugs"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_reports(self, ctx: DiscoContext) -> None:
        """View recent open reports - DMed to you."""
        rows = await ctx.db.fetch_all(
            "SELECT id, guild_id, user_id, category, message, status, created_at "
            "FROM reports WHERE guild_id = $1 AND status = 'open' ORDER BY created_at DESC LIMIT 10",
            ctx.guild_id,
        )
        if not rows:
            embed = card("[DRS] Reports", description="No open reports.", color=C_INFO).build()
            await _send_private(ctx, embed)
            return

        lines = []
        for r in rows:
            member = ctx.guild.get_member(int(r["user_id"]))
            name = member.display_name if member else f"User {r['user_id']}"
            msg = str(r.get("message", ""))[:80]
            if len(str(r.get("message", ""))) > 80:
                msg += "..."
            lines.append(f"**#{r['id']}** [{r.get('category', '?')}] by **{name}**\n> {msg}")

        embed = (
            card(f"[DRS] Open Reports ({len(rows)})", description="\n\n".join(lines), color=C_INFO)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        await _send_private(ctx, embed)
        await _log_drs_action(self.bot, ctx.guild_id, ctx.author.id, "view_reports")
        await log_staff_action(
            ctx.db,
            scope=SCOPE_DRS,
            guild_id=ctx.guild_id,
            actor_id=ctx.author.id,
            action="view_reports",
            target_id=None,
            severity=SEVERITY_INFO,
            details=f"Viewed {len(rows)} open reports",
        )

    # ── DRS Audit Log ─────────────────────────────────────────────────────────

    @drs.command(name="log")
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_log(self, ctx: DiscoContext) -> None:
        """View your recent DRS actions."""
        rows = await ctx.db.fetch_all(
            "SELECT id, guild_id, helper_id, action, target_id, details, created_at "
            "FROM helper_audit_log WHERE guild_id = $1 AND helper_id = $2 ORDER BY created_at DESC LIMIT 20",
            ctx.guild_id, ctx.author.id,
        )
        if not rows:
            embed = card("[DRS] Audit Log", description="No actions recorded yet.", color=C_INFO).build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        lines = []
        for r in rows:
            ts_str = fmt_ts(r["created_at"])
            target_str = f" -> <@{int(r['target_id'])}>" if r.get("target_id") else ""
            det = f" - {str(r['details'])[:40]}" if r.get("details") else ""
            lines.append(f"`{ts_str}` **{r['action']}**{target_str}{det}")

        embed = card("[DRS] Your Audit Log", description="\n".join(lines), color=C_NAVY).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── DRS Fish ──────────────────────────────────────────────────────────────

    @drs.command(name="fish", aliases=["fishing"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_fish(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Fishing stats for a player. Results DMed for privacy."""
        uid, gid = target.id, ctx.guild_id
        row = await ctx.db.fetch_one(
            "SELECT guild_id, user_id,"
            " COALESCE(fish_level, 1) AS fish_level,"
            " COALESCE(fish_xp, 0) AS fish_xp,"
            " biggest_fish, biggest_lbs,"
            " COALESCE(longest_combo, 0) AS longest_combo,"
            " COALESCE(rod_tier, 1) AS rod_tier,"
            " total_lure_earned_raw,"
            " current_zone, updated_at"
            " FROM user_fishing WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        if not row:
            await ctx.reply_error(f"No fishing data found for **{target.display_name}**.")
            return

        from configs.fishing_config import FISH, zone_meta as _zone_meta
        zone_key = str(row.get("current_zone") or "lake")
        zone = _zone_meta(zone_key) or {}
        zone_label = f"{zone.get('emoji', '')} {zone.get('name', zone_key)}".strip()

        biggest_key = row.get("biggest_fish")
        biggest_name = (FISH.get(str(biggest_key) or "") or {}).get("name") or biggest_key or "None"
        biggest_lbs = float(row.get("biggest_lbs") or 0.0)

        payout_raw = int(row.get("total_lure_earned_raw") or 0)

        updated = row.get("updated_at")
        last_active = fmt_ts(updated) if updated else "never"

        embed = (
            card(f"[DRS] Fishing - {target.display_name}", color=C_INFO)
            .author(target.display_name, icon_url=target.display_avatar.url if hasattr(target, "display_avatar") else None)
            .field("Level", f"Lv. **{int(row['fish_level'])}** ({int(row['fish_xp']):,} XP)", True)
            .field("Rod Tier", str(int(row["rod_tier"])), True)
            .field("Current Zone", zone_label, True)
            .field(
                "Trophy Catch",
                f"{biggest_name}  {biggest_lbs:.2f} lbs" if biggest_key else "None",
                True,
            )
            .field("Longest Combo", str(int(row["longest_combo"])), True)
            .field("Total Payout", f"LURE {payout_raw / 10**18:,.4f}", True)
            .field("Last Active", last_active, True)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        await _log_drs_action(self.bot, gid, ctx.author.id, "DRS_FISH", target_id=uid)
        try:
            await ctx.author.send(embed=embed)
            await ctx.reply_success("Fishing stats sent to your DMs.", title="[DRS] Fish")
        except discord.Forbidden:
            await ctx.reply(embed=embed, mention_author=False)

    # ── DRS Farm ──────────────────────────────────────────────────────────────

    @drs.command(name="farm", aliases=["farming"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_farm(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Farming stats for a player. Results DMed for privacy."""
        uid, gid = target.id, ctx.guild_id
        row = await ctx.db.fetch_one(
            "SELECT guild_id, user_id,"
            " COALESCE(farm_level, 1) AS farm_level,"
            " COALESCE(farm_xp, 0) AS farm_xp,"
            " COALESCE(plot_tier, 1) AS plot_tier,"
            " COALESCE(total_planted, 0) AS total_planted,"
            " COALESCE(total_harvested, 0) AS total_harvested,"
            " biggest_harvest_crop, biggest_harvest_qty,"
            " total_crops_grown_raw,"
            " updated_at"
            " FROM user_farming WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        if not row:
            await ctx.reply_error(f"No farming data found for **{target.display_name}**.")
            return

        from configs.farming_config import CROPS as _CROPS
        bh_key = row.get("biggest_harvest_crop")
        bh_name = (_CROPS.get(str(bh_key) or "") or {}).get("name") or bh_key or "None"
        bh_qty = int(row.get("biggest_harvest_qty") or 0)

        total_grown_raw = int(row.get("total_crops_grown_raw") or 0)
        updated = row.get("updated_at")
        last_active = fmt_ts(updated) if updated else "never"

        embed = (
            card(f"[DRS] Farming - {target.display_name}", color=C_INFO)
            .author(target.display_name, icon_url=target.display_avatar.url if hasattr(target, "display_avatar") else None)
            .field("Level", f"Lv. **{int(row['farm_level'])}** ({int(row['farm_xp']):,} XP)", True)
            .field("Plot Tier", str(int(row["plot_tier"])), True)
            .field("Planted", f"{int(row['total_planted']):,}", True)
            .field("Harvested", f"{int(row['total_harvested']):,}", True)
            .field(
                "Biggest Harvest",
                f"{bh_name} x{bh_qty}" if bh_key else "None",
                True,
            )
            .field("Total Yield (raw)", f"{total_grown_raw / 10**18:,.4f} HRV", True)
            .field("Last Active", last_active, True)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        await _log_drs_action(self.bot, gid, ctx.author.id, "DRS_FARM", target_id=uid)
        try:
            await ctx.author.send(embed=embed)
            await ctx.reply_success("Farming stats sent to your DMs.", title="[DRS] Farm")
        except discord.Forbidden:
            await ctx.reply(embed=embed, mention_author=False)

    # ══════════════════════════════════════════════════════════════════════
    # DRS full-surface x-ray  --  one command per wealth surface
    # ══════════════════════════════════════════════════════════════════════
    #
    # Pattern across every subcommand below:
    #   1. resolve target -> pull data via existing DB / service helpers
    #   2. price every component at oracle so embed values match NW
    #   3. render a Pillow chart attachment where the surface has >1
    #      meaningful row (skipped for single-row surfaces like loans)
    #   4. DM the embed + chart to the operator; fall back to channel
    #      reply if DMs are closed
    #   5. log a DRS_<SURFACE> action so other operators can see who
    #      audited what via ,drs audit
    #
    # Shared helpers _price_of and _dm_audit live below the class.

    @drs.command(name="stakes", aliases=["yield", "farm-stakes"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_stakes(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """All NPC yield-farm stakes for a player, priced + charted."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        rows = await db.get_user_stakes(uid, gid)
        if not rows:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no NPC stakes.",
            )
            return
        # Price each stake at oracle.
        prices = await _prices_for_guild(db, gid)
        priced = []
        total_usd = 0.0
        for r in rows:
            sym = str(r.get("symbol") or "")
            amt = to_human(int(r.get("amount") or 0))
            price = float(prices.get(sym, 0.0))
            usd = amt * price
            total_usd += usd
            priced.append({
                "validator_id": str(r.get("validator_id") or ""),
                "name":  str(r.get("name") or r.get("validator_id") or "?"),
                "emoji": str(r.get("emoji") or ""),
                "symbol": sym,
                "amount": amt,
                "price":  price,
                "usd":    usd,
                "reward_rate": float(r.get("reward_rate") or 0.0),
                "staked_at":   r.get("staked_at"),
            })
        priced.sort(key=lambda x: x["usd"], reverse=True)
        # Embed: top 25 lines (Discord field char limit). Anything past
        # that goes into the chart.
        body_lines: list[str] = []
        for s in priced[:25]:
            body_lines.append(
                f"{s['emoji']} **{s['name']}**  --  "
                f"{fmt_token(s['amount'], s['symbol'])}  "
                f"({fmt_usd(s['usd'])} @ "
                f"{s['reward_rate'] * 100:.2f}%/hr)"
            )
        # Try to use display_name; raw ID fallback if MemberConverter
        # returned a discord.User.
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Yield-Farm Stakes  --  {tname}", color=C_INFO)
            .description(
                f"**{len(priced)}** active stakes  --  "
                f"total {fmt_usd(total_usd)}"
            )
            .field("Stakes", "\n".join(body_lines) or "-", False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Stakes by validator  --  {tname}",
            [(s["name"], s["usd"]) for s in priced],
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"stakes_{uid}.png", "DRS_STAKES", uid,
        )

    @drs.command(name="validator", aliases=["pos", "blocks"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_validator(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """PoS validator status: own stake, incoming delegations, outgoing."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        validators = await db.get_pos_validators_for_user(uid, gid)
        out_delegations = await db.get_user_delegations(uid, gid)
        in_delegations = await db.fetch_all(
            "SELECT delegator_id, network, token, amount, total_earned, "
            "locked_until, delegated_at "
            "FROM pos_delegations WHERE validator_user_id=$1 AND guild_id=$2 "
            "AND amount > 0 ORDER BY amount DESC",
            uid, gid,
        )
        if not validators and not out_delegations and not in_delegations:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no PoS activity.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        tname = getattr(target, "display_name", None) or str(target.id)
        builder = (
            card(f"[DRS] PoS Validator  --  {tname}", color=C_GOLD)
            .footer(f"DRS audit by {ctx.author.display_name}")
        )
        chart_rows: list[tuple[str, float]] = []
        if validators:
            lines: list[str] = []
            total_own = 0.0
            total_rewards = 0.0
            total_blocks = 0
            slash_count = 0
            for v in validators:
                net = str(v.get("network") or "")
                stake_token = str(v.get("stake_token") or "")
                stake_amt = to_human(int(v.get("stake_amount") or 0))
                price = float(prices.get(stake_token, 0.0))
                usd = stake_amt * price
                total_own += usd
                rewards = to_human(int(v.get("total_rewards_earned") or 0))
                total_rewards += rewards
                blocks = int(v.get("total_blocks_validated") or 0)
                total_blocks += blocks
                slash_count += int(v.get("slash_count") or 0)
                active = "[A]" if v.get("is_active") else "[INACTIVE]"
                lines.append(
                    f"{active} **{net.upper()}**  --  "
                    f"{fmt_token(stake_amt, stake_token)} "
                    f"({fmt_usd(usd)})\n"
                    f"  Blocks: **{blocks:,}**  Rewards: {fmt_usd(rewards)}  "
                    f"Slashes: {int(v.get('slash_count') or 0)}"
                )
                chart_rows.append(
                    (f"{stake_token}/{net.upper()} own", usd),
                )
            builder.field(
                f"Own Stake ({len(validators)} network{'s' if len(validators) != 1 else ''})",
                "\n".join(lines),
                False,
            )
            builder.field(
                "Validator Totals",
                f"Stake value: **{fmt_usd(total_own)}**\n"
                f"Rewards earned: **{fmt_usd(total_rewards)}**\n"
                f"Blocks validated: **{total_blocks:,}**\n"
                f"Slashes: **{slash_count}**",
                False,
            )
        if in_delegations:
            tot_in_usd = 0.0
            lines = []
            for d in in_delegations[:10]:
                amt = to_human(int(d.get("amount") or 0))
                price = float(prices.get(str(d.get("token") or ""), 0.0))
                usd = amt * price
                tot_in_usd += usd
                lines.append(
                    f"{_mention(int(d['delegator_id']), ctx.guild, ctx.bot)} "
                    f"-- {fmt_token(amt, str(d['token']))} "
                    f"({fmt_usd(usd)})"
                )
            chart_rows.append(("incoming delegations", tot_in_usd))
            builder.field(
                f"Incoming Delegations (top {min(10, len(in_delegations))} of "
                f"{len(in_delegations)})  --  "
                f"total {fmt_usd(tot_in_usd)}",
                "\n".join(lines),
                False,
            )
        if out_delegations:
            tot_out_usd = 0.0
            lines = []
            for d in out_delegations[:10]:
                amt = to_human(int(d.get("amount") or 0))
                tok = str(d.get("token") or "")
                price = float(prices.get(tok, 0.0))
                usd = amt * price
                tot_out_usd += usd
                earned = to_human(int(d.get("total_earned") or 0))
                lines.append(
                    f"-> {_mention(int(d['validator_user_id']), ctx.guild, ctx.bot)} "
                    f"-- {fmt_token(amt, tok)} ({fmt_usd(usd)})  "
                    f"earned {fmt_usd(earned)}"
                )
            chart_rows.append(("outgoing delegations", tot_out_usd))
            builder.field(
                f"Outgoing Delegations (top {min(10, len(out_delegations))} of "
                f"{len(out_delegations)})  --  "
                f"total {fmt_usd(tot_out_usd)}",
                "\n".join(lines),
                False,
            )
        png = _try_render_bars(
            f"PoS exposure  --  {tname}", chart_rows,
        )
        await self._dm_audit_attach(
            ctx, builder.build(), png, f"validator_{uid}.png",
            "DRS_VALIDATOR", uid,
        )

    @drs.command(name="mining", aliases=["rigs", "hashrate"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_mining(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Mining rigs, hashrate, mode (solo/pool/group)."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        rigs = await db.get_user_rigs(uid, gid)
        if not rigs:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** owns no rigs.",
            )
            return
        try:
            total_hash = float(await db.get_user_total_hashrate(uid, gid) or 0.0)
        except Exception:
            total_hash = 0.0
        try:
            in_pool = bool(await db.is_pool_miner(uid, gid))
        except Exception:
            in_pool = False
        chart_rows: list[tuple[str, float]] = []
        lines = []
        total_book = 0.0
        for r in rigs:
            rig_id = str(r.get("rig_id") or "")
            qty = int(r.get("quantity") or 0)
            cfg = Config.MINING_RIGS.get(rig_id, {})
            price_raw = int(cfg.get("price") or 0)
            book_per = to_human(price_raw) / 2.0  # 50% of cost per CLAUDE.md
            book = book_per * qty
            total_book += book
            label = cfg.get("name") or rig_id
            lines.append(
                f"**{label}** x{qty}  --  book {fmt_usd(book)} "
                f"({fmt_usd(book_per)} each)"
            )
            chart_rows.append((label, book))
        mode = "POOL" if in_pool else "SOLO"
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Mining  --  {tname}", color=C_AMBER)
            .description(
                f"Mode: **{mode}**   "
                f"Total hashrate: **{total_hash:,.0f} MH/s**   "
                f"Book value: **{fmt_usd(total_book)}**"
            )
            .field(
                f"Rigs ({len(rigs)} kinds)",
                "\n".join(lines) or "-",
                False,
            )
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Rig book value  --  {tname}", chart_rows,
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"mining_{uid}.png", "DRS_MINING", uid,
        )

    @drs.command(name="lp", aliases=["liquidity", "pools"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_lp(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """LP positions priced per-pool from current reserves."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        positions = await db.get_user_lp_positions(uid, gid)
        if not positions:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no LP positions.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        priced = []
        for p in positions:
            shares = int(p.get("lp_shares") or 0)
            total_lp = int(p.get("total_lp") or 0)
            if shares <= 0 or total_lp <= 0:
                continue
            share = shares / total_lp
            token_a = str(p.get("token_a") or "")
            token_b = str(p.get("token_b") or "")
            res_a = to_human(int(p.get("reserve_a") or 0))
            res_b = to_human(int(p.get("reserve_b") or 0))
            usd_a = share * res_a * float(prices.get(token_a, 0.0))
            usd_b = share * res_b * float(prices.get(token_b, 0.0))
            usd = usd_a + usd_b
            priced.append({
                "pool_id": str(p.get("pool_id") or ""),
                "pair": f"{token_a}/{token_b}",
                "share_pct": share * 100.0,
                "usd": usd,
                "is_group_pool": bool(p.get("is_group_pool", False)),
                "vault_locked": bool(p.get("vault_locked", False)),
            })
        priced.sort(key=lambda x: x["usd"], reverse=True)
        lines = []
        total_usd = 0.0
        for s in priced[:25]:
            total_usd += s["usd"]
            tags = []
            if s["is_group_pool"]:
                tags.append("group")
            if s["vault_locked"]:
                tags.append("vault-locked")
            tag = f" [{' '.join(tags)}]" if tags else ""
            lines.append(
                f"**{s['pair']}**{tag}  --  "
                f"{s['share_pct']:.4f}% of pool  ({fmt_usd(s['usd'])})"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] LP Positions  --  {tname}", color=C_TEAL)
            .description(
                f"**{len(priced)}** active pools  --  "
                f"total {fmt_usd(total_usd)}"
            )
            .field("Positions", "\n".join(lines) or "-", False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"LP value by pool  --  {tname}",
            [(s["pair"], s["usd"]) for s in priced],
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"lp_{uid}.png", "DRS_LP", uid,
        )

    @drs.command(name="stones", aliases=["leaderboard-stones"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_stones(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """All five stones (hash / lock / vault / gamba / liq)."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        rows = []
        # CLAUDE.md spec: hash + lock + vault + gamba + liq.
        for table, label in (
            ("hashstones",  "Hashstone"),
            ("lockstones",  "Lockstone"),
            ("vaultstones", "Vaultstone"),
            ("gambastones", "Gambastone"),
            ("liqstones",   "Liqstone"),
        ):
            r = await db.fetch_one(
                f"SELECT staked_amount, level, xp, acquired_at "
                f"FROM {table} WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
            staked = to_human(int((r or {}).get("staked_amount") or 0))
            level = int((r or {}).get("level") or 0)
            xp = int((r or {}).get("xp") or 0)
            acquired = (r or {}).get("acquired_at") if r else None
            if not r or (staked <= 0 and level == 0):
                continue
            rows.append({
                "label": label, "staked": staked, "level": level,
                "xp": xp, "acquired_at": acquired,
            })
        if not rows:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** owns no stones.",
            )
            return
        total = sum(r["staked"] for r in rows)
        lines = []
        for r in rows:
            ts = fmt_ts(r["acquired_at"]) if r["acquired_at"] else "-"
            lines.append(
                f"**{r['label']}**  --  Lv {r['level']} "
                f"({r['xp']:,} XP)  staked {fmt_usd(r['staked'])}  "
                f"acquired {ts}"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Stones  --  {tname}", color=C_AMBER)
            .description(
                f"**{len(rows)}/5** stone types owned  --  "
                f"total stake {fmt_usd(total)}"
            )
            .field("Stones", "\n".join(lines), False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Stone stake  --  {tname}",
            [(r["label"], r["staked"]) for r in rows],
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"stones_{uid}.png", "DRS_STONES", uid,
        )

    @drs.command(name="gamba", aliases=["gambastake", "gamba-stakes"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_gamba(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Gamba Network staked positions + pending GBC/BUD yield."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        # Migration 0234 renamed pending_gbc -> pending_yield_raw and
        # added yield_target. Each row's pending column is denominated
        # in whichever target the row points at (GBC default, BUD opt-in).
        try:
            rows = await db.fetch_all(
                "SELECT symbol, amount, pending_yield_raw, yield_target, "
                "total_claimed, auto_compound, total_compounded, staked_at "
                "FROM gamba_stakes WHERE user_id=$1 AND guild_id=$2 "
                "AND (amount > 0 OR pending_yield_raw > 0)",
                uid, gid,
            )
        except Exception:
            rows = []
        if not rows:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no gamba stakes.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        priced = []
        total_usd = 0.0
        for r in rows:
            sym = str(r.get("symbol") or "")
            amt = to_human(int(r.get("amount") or 0))
            pend = to_human(int(r.get("pending_yield_raw") or 0))
            target = str(r.get("yield_target") or "GBC")
            sym_price = float(prices.get(sym, 0.0))
            target_price = float(prices.get(target, 0.0))
            usd = amt * sym_price + pend * target_price
            total_usd += usd
            priced.append({
                "symbol": sym,
                "amount": amt,
                "pending_amount": pend,
                "yield_target": target,
                "auto_compound": bool(r.get("auto_compound", False)),
                "claimed": to_human(int(r.get("total_claimed") or 0)),
                "compounded": to_human(int(r.get("total_compounded") or 0)),
                "usd": usd,
            })
        priced.sort(key=lambda x: x["usd"], reverse=True)
        lines = []
        for s in priced:
            ac = " [AUTO]" if s["auto_compound"] else ""
            lines.append(
                f"**{s['symbol']}**{ac}  -> **{s['yield_target']}**  --  "
                f"{fmt_token(s['amount'], s['symbol'])} staked  "
                f"({fmt_usd(s['usd'])})\n"
                f"  Pending: {fmt_token(s['pending_amount'], s['yield_target'])}  "
                f"Claimed lifetime: {fmt_token(s['claimed'], s['yield_target'])}  "
                f"Compounded: {fmt_token(s['compounded'], s['symbol'])}"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Gamba Stakes  --  {tname}", color=C_PINK)
            .description(
                f"**{len(priced)}** positions  --  "
                f"total {fmt_usd(total_usd)}"
            )
            .field("Positions", "\n".join(lines) or "-", False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Gamba value by token  --  {tname}",
            [(s["symbol"], s["usd"]) for s in priced],
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"gamba_{uid}.png", "DRS_GAMBA", uid,
        )

    @drs.command(name="loan", aliases=["loans", "debt"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_loan(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Outstanding loan + collateral + interest age."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        loan = await db.get_loan(uid, gid)
        sun = await db.fetch_one(
            "SELECT * FROM sun_loans WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        if not loan and not sun:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no active loans.",
            )
            return
        tname = getattr(target, "display_name", None) or str(target.id)
        builder = (
            card(f"[DRS] Loans  --  {tname}", color=C_WARNING)
            .footer(f"DRS audit by {ctx.author.display_name}")
        )
        if loan:
            principal = to_human(int(loan.get("principal") or 0))
            outstanding = to_human(int(loan.get("outstanding") or 0))
            collateral = to_human(int(loan.get("collateral") or 0))
            created = fmt_ts(loan.get("created_at"))
            last_interest = fmt_ts(loan.get("last_interest"))
            paid = max(0.0, principal - outstanding)
            health = (
                collateral / outstanding if outstanding > 0 else float("inf")
            )
            builder.field(
                "Standard Loan",
                f"Outstanding: **{fmt_usd(outstanding)}**\n"
                f"Principal:   {fmt_usd(principal)}\n"
                f"Paid down:   {fmt_usd(paid)}\n"
                f"Collateral:  {fmt_usd(collateral)}\n"
                f"Health:      `{health:,.2f}x`\n"
                f"Opened:      {created}\n"
                f"Last interest: {last_interest}",
                False,
            )
        if sun:
            builder.field(
                "SUN Loan",
                f"Collateral: {to_human(int(sun.get('collateral_sun') or 0)):,.4f} SUN\n"
                f"Borrowed: {to_human(int(sun.get('borrow_amount') or 0)):,.4f} "
                f"{sun.get('borrow_symbol', '?')}\n"
                f"Opened: {fmt_ts(sun.get('created_at'))}",
                False,
            )
        await self._dm_audit_attach(
            ctx, builder.build(), None, None, "DRS_LOAN", uid,
        )

    @drs.command(name="games", aliases=["gambling", "play-history"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_games(
        self, ctx: DiscoContext, target: _MemberOrID, limit: int = 50,
    ) -> None:
        """Recent gambling sessions per game type, plus win/loss totals."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        if limit < 1 or limit > 200:
            await ctx.reply_error("limit must be between 1 and 200.")
            return
        # Per-game-type completed-session aggregates from game_sessions.
        agg = await db.fetch_all(
            """SELECT game_type, status, COUNT(*) AS n,
                      COALESCE(SUM(bet_amount), 0) AS total_bet
                 FROM game_sessions
                WHERE user_id=$1 AND guild_id=$2
                GROUP BY game_type, status
                ORDER BY game_type ASC, status ASC""",
            uid, gid,
        )
        # Pull GAMBLE-tagged transactions for win/loss USD totals.
        tx_rows = await db.fetch_all(
            """SELECT tx_type, symbol_in, amount_in, symbol_out, amount_out,
                      price_at, ts
                 FROM transactions
                WHERE user_id=$1 AND guild_id=$2
                  AND tx_type LIKE 'GAMBLE%'
                ORDER BY ts DESC
                LIMIT $3""",
            uid, gid, limit,
        )
        if not agg and not tx_rows:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no gambling history.",
            )
            return
        # Per-game aggregates
        per_game: dict[str, dict] = {}
        for r in agg:
            gt = str(r["game_type"])
            per_game.setdefault(gt, {"n": 0, "bet": 0.0, "status": {}})
            per_game[gt]["n"] += int(r.get("n") or 0)
            per_game[gt]["bet"] += to_human(int(r.get("total_bet") or 0))
            per_game[gt]["status"][str(r["status"])] = int(r.get("n") or 0)
        # Win/loss USD totals per game from tx history.
        wins_by_game: dict[str, float] = {}
        losses_by_game: dict[str, float] = {}
        for t in tx_rows:
            tt = str(t["tx_type"])
            # tx_type pattern e.g. "GAMBLE_BLACKJACK_WIN" / "_LOSS"; fall
            # back to "GAMBLE" if the tag is absent.
            game = "unknown"
            outcome = "unknown"
            parts = tt.split("_")
            if len(parts) >= 2:
                game = parts[1].lower() if parts[1] else "unknown"
            outcome = parts[-1].lower() if parts else "unknown"
            amt_in = to_human(int(t.get("amount_in") or 0))
            amt_out = to_human(int(t.get("amount_out") or 0))
            if outcome == "win":
                wins_by_game[game] = wins_by_game.get(game, 0.0) + amt_out
            elif outcome in ("loss", "lose"):
                losses_by_game[game] = losses_by_game.get(game, 0.0) + amt_in
        tname = getattr(target, "display_name", None) or str(target.id)
        body = []
        for gt, info in sorted(per_game.items()):
            statuses = ", ".join(
                f"{k}={v}" for k, v in sorted(info["status"].items())
            )
            body.append(
                f"**{gt}**  --  {info['n']} sessions  "
                f"({fmt_usd(info['bet'])} bet)  [{statuses}]"
            )
        total_won = sum(wins_by_game.values())
        total_lost = sum(losses_by_game.values())
        embed = (
            card(f"[DRS] Gambling  --  {tname}", color=C_PINK)
            .description(
                f"Won: **{fmt_usd(total_won)}**  --  "
                f"Lost: **{fmt_usd(total_lost)}**  --  "
                f"Net: **{fmt_usd(total_won - total_lost)}**"
            )
            .field(
                "Per-Game Sessions",
                "\n".join(body) or "-",
                False,
            )
            .footer(f"DRS audit by {ctx.author.display_name}  --  "
                    f"last {limit} GAMBLE tx scanned")
            .build()
        )
        try:
            from services import drs_charts as _ec
            png = _ec.render_winloss_bars(
                f"Wins vs Losses  --  {tname}",
                wins=list(wins_by_game.items()),
                losses=list(losses_by_game.items()),
            )
        except Exception:
            log.exception("DRS games chart render failed for uid=%s", uid)
            png = None
        await self._dm_audit_attach(
            ctx, embed, png, f"games_{uid}.png", "DRS_GAMES", uid,
        )

    @drs.command(name="timeline", aliases=["activity", "wealthline"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_timeline(
        self, ctx: DiscoContext, target: _MemberOrID, days: int = 30,
    ) -> None:
        """Chronological activity feed from transactions table + chart."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        if days < 1 or days > 365:
            await ctx.reply_error("days must be between 1 and 365.")
            return
        # We don't paginate further than 500 rows because the embed
        # would never finish rendering anyway -- the chart still uses
        # every row, the embed view shows the top 20.
        rows = await db.fetch_all(
            """SELECT tx_type, symbol_in, amount_in, symbol_out, amount_out,
                      price_at, gas_fee, gas_coin, ts
                 FROM transactions
                WHERE user_id=$1 AND guild_id=$2
                  AND ts > now() - make_interval(days => $3)
                ORDER BY ts DESC
                LIMIT 500""",
            uid, gid, days,
        )
        if not rows:
            await ctx.reply_error(
                f"No transactions in the last {days} days "
                f"for **{getattr(target, 'display_name', target.id)}**.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        # Per-event signed delta (amount_out for inflows, -amount_in for
        # outflows; both priced at oracle for USD comparability). We
        # treat WORK/MM_BUY proceeds and STAKE_REWARD/VALIDATOR_REWARD/
        # LP_YIELD/LUNAR_MINT as inflows; SELL/SEND outflows.
        INFLOW_TYPES = {
            "WORK", "STAKE_REWARD", "VALIDATOR_REWARD", "LP_YIELD",
            "LUNAR_MINT", "MOON_POOL_YIELD",
        }
        tline_rows: list[dict] = []
        for r in rows:
            ts = r["ts"]
            tt = str(r.get("tx_type") or "")
            sym_in = str(r.get("symbol_in") or "")
            sym_out = str(r.get("symbol_out") or "")
            a_in = to_human(int(r.get("amount_in") or 0))
            a_out = to_human(int(r.get("amount_out") or 0))
            # USD delta: positive = wealth in, negative = wealth out.
            inflow_usd = a_out * float(prices.get(sym_out, 0.0))
            outflow_usd = a_in * float(prices.get(sym_in, 0.0))
            if tt in INFLOW_TYPES:
                delta = inflow_usd
            elif tt in ("BUY", "MM_BUY", "ADD_LP", "SEND"):
                delta = -outflow_usd
            elif tt in ("SELL", "MM_SELL", "REMOVE_LP"):
                delta = inflow_usd - outflow_usd
            elif tt == "SWAP":
                delta = inflow_usd - outflow_usd
            elif tt.startswith("GAMBLE"):
                # outcome encoded in suffix; fall back to "out - in"
                delta = inflow_usd - outflow_usd
            else:
                delta = inflow_usd - outflow_usd
            tline_rows.append({
                "ts": ts, "tx_type": tt,
                "sym_in": sym_in, "amount_in": a_in,
                "sym_out": sym_out, "amount_out": a_out,
                "delta_usd": delta,
            })
        # Top-20 embed feed (newest first)
        body = []
        for e in tline_rows[:20]:
            emoji = _TX_EMOJI.get(e["tx_type"], "·")
            arrow_bits = []
            if e["amount_in"] > 0 and e["sym_in"]:
                arrow_bits.append(
                    f"-{fmt_token(e['amount_in'], e['sym_in'])}"
                )
            if e["amount_out"] > 0 and e["sym_out"]:
                arrow_bits.append(
                    f"+{fmt_token(e['amount_out'], e['sym_out'])}"
                )
            arrow = " -> ".join(arrow_bits) or "-"
            delta = e["delta_usd"]
            sign = "+" if delta > 0 else ("-" if delta < 0 else " ")
            body.append(
                f"{emoji} `{fmt_ts(e['ts'])}` **{e['tx_type']}**  "
                f"{arrow}  ({sign}{fmt_usd(abs(delta))})"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        # Paginate the timeline: 20 lines per page
        per_page = 20
        all_lines = []
        for e in tline_rows:
            emoji = _TX_EMOJI.get(e["tx_type"], "·")
            arrow_bits = []
            if e["amount_in"] > 0 and e["sym_in"]:
                arrow_bits.append(
                    f"-{fmt_token(e['amount_in'], e['sym_in'])}"
                )
            if e["amount_out"] > 0 and e["sym_out"]:
                arrow_bits.append(
                    f"+{fmt_token(e['amount_out'], e['sym_out'])}"
                )
            arrow = " -> ".join(arrow_bits) or "-"
            delta = e["delta_usd"]
            sign = "+" if delta > 0 else ("-" if delta < 0 else " ")
            all_lines.append(
                f"{emoji} `{fmt_ts(e['ts'])}` **{e['tx_type']}**  "
                f"{arrow}  ({sign}{fmt_usd(abs(delta))})"
            )
        # Build pages
        n_pages = max(1, (len(all_lines) + per_page - 1) // per_page)
        pages: list[discord.Embed] = []
        for p_idx in range(n_pages):
            chunk = all_lines[p_idx * per_page:(p_idx + 1) * per_page]
            pages.append(
                card(
                    f"[DRS] Timeline  --  {tname} "
                    f"({p_idx + 1}/{n_pages})",
                    color=C_NAVY,
                )
                .description(
                    f"Last **{days}** days  --  "
                    f"**{len(tline_rows)}** events  "
                    f"(showing newest first)"
                )
                .field("Events", "\n".join(chunk) or "-", False)
                .footer(f"DRS audit by {ctx.author.display_name}")
                .build()
            )
        # Cumulative wealth-flow chart (oldest -> newest)
        try:
            from services import drs_charts as _ec
            events_chrono = list(reversed(tline_rows))
            png = _ec.render_timeline(
                f"Wealth flow timeline  --  {tname}",
                events_chrono,
                subtitle=f"Last {days} days  --  {len(events_chrono)} events",
            )
        except Exception:
            log.exception("DRS timeline chart render failed for uid=%s", uid)
            png = None
        await _log_drs_action(
            self.bot, gid, ctx.author.id, "DRS_TIMELINE", target_id=uid,
            details=f"days={days} events={len(tline_rows)}",
        )
        # Send chart to channel (or DM); paginate the embed feed.
        if png is not None:
            try:
                file = discord.File(
                    io.BytesIO(png), filename=f"timeline_{uid}.png",
                )
                await ctx.reply(file=file, mention_author=False)
            except Exception:
                log.exception("DRS timeline chart send failed for uid=%s", uid)
        await ctx.paginate(pages)

    # ══════════════════════════════════════════════════════════════════════
    # Round 3 wealth surfaces  --  Moon Network, Safety Module, Disc.Fun,
    # NFTs, Delve, Buddy, Crafting, Farming-deep, Savings, Trades, Work,
    # plus the catch-all ,drs networth breakdown.
    # ══════════════════════════════════════════════════════════════════════

    @drs.command(name="lunar", aliases=["moon", "lunar-mint"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_lunar(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Moon Network: Tier-1 lunar_stakes (group tokens) + Tier-2 moon_stakes."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        try:
            lunar = await db.fetch_all(
                "SELECT symbol, amount, session_earned, total_earned, staked_at "
                "FROM lunar_stakes WHERE user_id=$1 AND guild_id=$2 "
                "AND (amount > 0 OR total_earned > 0)",
                uid, gid,
            )
        except Exception:
            lunar = []
        try:
            moon = await db.fetch_one(
                "SELECT amount, session_earned, total_earned, staked_at "
                "FROM moon_stakes WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
        except Exception:
            moon = None
        if not lunar and not (moon and int(moon.get("amount") or 0) > 0):
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no Moon Network activity.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        tname = getattr(target, "display_name", None) or str(target.id)
        builder = (
            card(f"[DRS] Moon Network  --  {tname}", color=C_PURPLE)
            .footer(f"DRS audit by {ctx.author.display_name}")
        )
        chart_rows: list[tuple[str, float]] = []
        if lunar:
            lines = []
            total_lunar = 0.0
            total_lifetime = 0.0
            for r in lunar:
                sym = str(r.get("symbol") or "")
                amt = to_human(int(r.get("amount") or 0))
                lifetime = to_human(int(r.get("total_earned") or 0))
                session = to_human(int(r.get("session_earned") or 0))
                price = float(prices.get(sym, 0.0))
                usd = amt * price
                total_lunar += usd
                total_lifetime += lifetime
                staked_at = fmt_ts(r.get("staked_at")) if r.get("staked_at") else "-"
                lines.append(
                    f"**{sym}**  --  {fmt_token(amt, sym)} ({fmt_usd(usd)})  "
                    f"staked {staked_at}\n"
                    f"  Lifetime MOON: {fmt_token(lifetime, 'MOON')}  "
                    f"Session: {fmt_token(session, 'MOON')}"
                )
                chart_rows.append((f"lunar {sym}", usd))
            builder.field(
                f"Tier-1 Lunar Mints ({len(lunar)} positions)  --  "
                f"total {fmt_usd(total_lunar)}",
                "\n".join(lines),
                False,
            )
        if moon and int(moon.get("amount") or 0) > 0:
            moon_amt = to_human(int(moon.get("amount") or 0))
            moon_lifetime = to_human(int(moon.get("total_earned") or 0))
            moon_session = to_human(int(moon.get("session_earned") or 0))
            moon_price = float(prices.get("MOON", 0.0))
            usd = moon_amt * moon_price
            staked_at = fmt_ts(moon.get("staked_at")) if moon.get("staked_at") else "-"
            builder.field(
                "Tier-2 Moon Pool",
                f"{fmt_token(moon_amt, 'MOON')} staked  ({fmt_usd(usd)})\n"
                f"Staked at: {staked_at}\n"
                f"Lifetime DSD earned: {fmt_usd(moon_lifetime)}\n"
                f"Session DSD: {fmt_usd(moon_session)}",
                False,
            )
            chart_rows.append(("Moon Pool", usd))
        png = _try_render_bars(
            f"Moon Network exposure  --  {tname}", chart_rows,
        )
        await self._dm_audit_attach(
            ctx, builder.build(), png, f"lunar_{uid}.png", "DRS_LUNAR", uid,
        )

    @drs.command(name="safety", aliases=["sm", "safety-module"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_safety(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Safety Module: VTR + DSY staked positions."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        prices = await _prices_for_guild(db, gid)
        positions = []
        chart_rows: list[tuple[str, float]] = []
        total_usd = 0.0
        for sym in ("VTR", "DSY"):
            try:
                row = await db.get_sm_stake(uid, gid, sym)
            except Exception:
                row = None
            if not row or int(row.get("amount") or 0) <= 0:
                continue
            amt = to_human(int(row["amount"]))
            price = float(prices.get(sym, 0.0))
            usd = amt * price
            total_usd += usd
            chart_rows.append((sym, usd))
            positions.append({
                "symbol": sym,
                "amount": amt,
                "usd": usd,
                "auto_compound": bool(row.get("auto_compound", False)),
                "last_yield": row.get("last_yield"),
                "cooldown_at": row.get("cooldown_at"),
                "staked_at": row.get("staked_at"),
            })
        if not positions:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no Safety Module positions.",
            )
            return
        lines = []
        for p in positions:
            ac = " [AUTO]" if p["auto_compound"] else ""
            cooldown = (
                f"  [COOLDOWN until {fmt_ts(p['cooldown_at'])}]"
                if p["cooldown_at"] else ""
            )
            lines.append(
                f"**{p['symbol']}**{ac}{cooldown}  --  "
                f"{fmt_token(p['amount'], p['symbol'])} ({fmt_usd(p['usd'])})\n"
                f"  Staked: {fmt_ts(p['staked_at']) if p['staked_at'] else '-'}  "
                f"Last yield: {fmt_ts(p['last_yield']) if p['last_yield'] else '-'}"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Safety Module  --  {tname}", color=C_INFO)
            .description(
                f"**{len(positions)}** positions  --  total {fmt_usd(total_usd)}"
            )
            .field("Positions", "\n".join(lines), False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Safety Module exposure  --  {tname}", chart_rows,
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"safety_{uid}.png", "DRS_SAFETY", uid,
        )

    @drs.command(name="discfun", aliases=["disc-fun", "dfun"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_discfun(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Disc.Fun: active proto-token holdings + staked positions."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        try:
            holdings = await db.fetch_all(
                "SELECT pth.symbol, pth.amount, pth.cost_basis "
                "FROM proto_token_holdings pth "
                "WHERE pth.user_id=$1 AND pth.guild_id=$2 AND pth.amount > 0",
                uid, gid,
            )
        except Exception:
            holdings = []
        try:
            stakes = await db.fetch_all(
                "SELECT symbol, amount, pending_dfun, total_claimed, "
                "auto_compound, total_compounded, last_accrue "
                "FROM discfun_stakes WHERE user_id=$1 AND guild_id=$2 "
                "AND (amount > 0 OR pending_dfun > 0)",
                uid, gid,
            )
        except Exception:
            stakes = []
        if not holdings and not stakes:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no Disc.Fun positions.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        dfun_price = float(prices.get("DFUN", 0.0))
        tname = getattr(target, "display_name", None) or str(target.id)
        builder = (
            card(f"[DRS] Disc.Fun  --  {tname}", color=C_PINK)
            .footer(f"DRS audit by {ctx.author.display_name}")
        )
        chart_rows: list[tuple[str, float]] = []
        if holdings:
            total_h_usd = 0.0
            lines = []
            for h in holdings:
                sym = str(h.get("symbol") or "")
                amt = to_human(int(h.get("amount") or 0))
                cost_basis = to_human(int(h.get("cost_basis") or 0))
                # Proto-token spot price defaults to oracle; if no oracle
                # entry, fall back to cost_basis so the line still shows.
                price = float(prices.get(sym, 0.0))
                usd = amt * price if price > 0 else cost_basis
                total_h_usd += usd
                lines.append(
                    f"**{sym}**  --  {fmt_token(amt, sym)} "
                    f"({fmt_usd(usd)})  cost-basis {fmt_usd(cost_basis)}"
                )
                chart_rows.append((f"hold {sym}", usd))
            builder.field(
                f"Active Holdings ({len(holdings)})  --  "
                f"total {fmt_usd(total_h_usd)}",
                "\n".join(lines),
                False,
            )
        if stakes:
            total_s_usd = 0.0
            lines = []
            for s in stakes:
                sym = str(s.get("symbol") or "")
                amt = to_human(int(s.get("amount") or 0))
                pend = to_human(int(s.get("pending_dfun") or 0))
                claimed = to_human(int(s.get("total_claimed") or 0))
                price = float(prices.get(sym, 0.0))
                usd = amt * price + pend * dfun_price
                total_s_usd += usd
                ac = " [AUTO]" if s.get("auto_compound") else ""
                lines.append(
                    f"**{sym}**{ac}  --  {fmt_token(amt, sym)} staked "
                    f"({fmt_usd(usd)})\n"
                    f"  Pending: {fmt_token(pend, 'DFUN')}  "
                    f"Claimed: {fmt_token(claimed, 'DFUN')}"
                )
                chart_rows.append((f"stake {sym}", usd))
            builder.field(
                f"Staked Positions ({len(stakes)})  --  "
                f"total {fmt_usd(total_s_usd)}",
                "\n".join(lines),
                False,
            )
        png = _try_render_bars(
            f"Disc.Fun exposure  --  {tname}", chart_rows,
        )
        await self._dm_audit_attach(
            ctx, builder.build(), png, f"discfun_{uid}.png", "DRS_DISCFUN", uid,
        )

    @drs.command(name="nft", aliases=["nfts", "collection"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_nft(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Owned NFTs grouped by collection + rarity."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        try:
            nfts = await db.get_user_nfts(uid, gid)
        except Exception:
            nfts = []
        if not nfts:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** owns no NFTs.",
            )
            return
        # Group by (collection, rarity); value each group with avg sale
        # price if available, else fall back to mint price * mint token
        # oracle. Avoid an N+1 against avg_sale_price by caching results
        # keyed on collection_id.
        prices = await _prices_for_guild(db, gid)
        avg_by_coll: dict[int, dict[str, float]] = {}
        groups: dict[tuple[str, str], dict] = {}
        for n in nfts:
            coll_id = int(n.get("collection_id") or 0)
            coll_name = str(n.get("name") or n.get("collection_name") or f"coll {coll_id}")
            rarity = str(n.get("rarity") or "common").lower()
            key = (coll_name, rarity)
            entry = groups.setdefault(key, {
                "count": 0, "estimated_usd": 0.0, "mint_token": "USD",
            })
            entry["count"] += 1
            # Estimate per-NFT value. ``get_avg_sale_price_by_rarity``
            # returns ``{rarity: avg_price}`` for a whole collection, so
            # one call per collection covers every rarity for that
            # collection without an N+1.
            if coll_id not in avg_by_coll:
                try:
                    avg_by_coll[coll_id] = await db.get_avg_sale_price_by_rarity(coll_id)
                except Exception:
                    avg_by_coll[coll_id] = {}
            per = float(avg_by_coll[coll_id].get(rarity, 0.0))
            if per <= 0:
                mint_price = float(n.get("mint_price") or 0.0)
                mint_token = str(n.get("mint_token") or "USD")
                tok_price = float(prices.get(mint_token, 1.0 if mint_token == "USD" else 0.0))
                per = mint_price * tok_price
            entry["estimated_usd"] += per
        # Render top 20 groups by USD value.
        rows = sorted(groups.items(), key=lambda x: x[1]["estimated_usd"], reverse=True)
        total_usd = sum(g["estimated_usd"] for _, g in rows)
        lines = []
        for (coll, rarity), g in rows[:20]:
            lines.append(
                f"**{coll}**  --  {rarity}  x{g['count']}  "
                f"(~ {fmt_usd(g['estimated_usd'])})"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] NFTs  --  {tname}", color=C_PURPLE)
            .description(
                f"**{len(nfts)}** NFTs across **{len(groups)}** "
                f"(collection, rarity) groups  --  est. total {fmt_usd(total_usd)}"
            )
            .field("Groups", "\n".join(lines) or "-", False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"NFT value by group  --  {tname}",
            [(f"{c} / {r}", v["estimated_usd"]) for (c, r), v in rows],
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"nft_{uid}.png", "DRS_NFT", uid,
        )

    @drs.command(name="delve", aliases=["dungeon"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_delve(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Delve: dungeon ore stakes + party + pending RUNE yield."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        try:
            row = await db.fetch_one(
                "SELECT * FROM user_dungeon WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
        except Exception:
            row = None
        try:
            party = await db.fetch_all(
                "SELECT * FROM dungeon_party "
                "WHERE user_id=$1 AND guild_id=$2 AND status='owned'",
                uid, gid,
            )
        except Exception:
            party = []
        if not row and not party:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no Delve activity.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        try:
            from delve_config import COPPER_SYMBOL, SILVER_SYMBOL, GOLD_SYMBOL
        except Exception:
            COPPER_SYMBOL = "COPPER"
            SILVER_SYMBOL = "SILVER"
            GOLD_SYMBOL = "GOLD"
        copper_raw = int((row or {}).get("copper_staked_raw") or 0)
        silver_raw = int((row or {}).get("silver_staked_raw") or 0)
        gold_raw = int((row or {}).get("gold_staked_raw") or 0)
        copper_h = to_human(copper_raw)
        silver_h = to_human(silver_raw)
        gold_h = to_human(gold_raw)
        cu_p = float(prices.get(COPPER_SYMBOL, 0.0))
        ag_p = float(prices.get(SILVER_SYMBOL, 0.0))
        au_p = float(prices.get(GOLD_SYMBOL, 0.0))
        usd_cu = copper_h * cu_p
        usd_ag = silver_h * ag_p
        usd_au = gold_h * au_p
        try:
            from services import dungeon as _ds
            pending_rune_raw = int(await _ds.accrued_stake_yield(db, gid, uid) or 0)
        except Exception:
            pending_rune_raw = 0
        pending_rune = to_human(pending_rune_raw)
        rune_price = float(prices.get("RUNE", 0.0))
        pending_usd = pending_rune * rune_price
        party_value_usd = float(len(party)) * 5.0  # NW values buddies at $5 flat
        level = int((row or {}).get("level") or 0)
        xp = int((row or {}).get("xp") or 0)
        deepest = int((row or {}).get("deepest_floor") or 0)
        kills = int((row or {}).get("total_kills") or 0)
        chart_rows = [
            (f"{COPPER_SYMBOL} stake", usd_cu),
            (f"{SILVER_SYMBOL} stake", usd_ag),
            (f"{GOLD_SYMBOL} stake", usd_au),
            ("pending RUNE", pending_usd),
            ("party (x$5)", party_value_usd),
        ]
        total = sum(v for _, v in chart_rows)
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Delve  --  {tname}", color=C_AMBER)
            .description(
                f"Lv **{level}** ({xp:,} XP)  --  deepest floor **{deepest}**  "
                f"--  total kills **{kills:,}**  --  total {fmt_usd(total)}"
            )
            .field(
                "Ore Stakes",
                f"{COPPER_SYMBOL}: {fmt_token(copper_h, COPPER_SYMBOL)} ({fmt_usd(usd_cu)})\n"
                f"{SILVER_SYMBOL}: {fmt_token(silver_h, SILVER_SYMBOL)} ({fmt_usd(usd_ag)})\n"
                f"{GOLD_SYMBOL}: {fmt_token(gold_h, GOLD_SYMBOL)} ({fmt_usd(usd_au)})",
                False,
            )
            .field(
                "Yield + Party",
                f"Pending RUNE: {fmt_token(pending_rune, 'RUNE')} ({fmt_usd(pending_usd)})\n"
                f"Party: **{len(party)}** buddies ({fmt_usd(party_value_usd)})",
                False,
            )
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(f"Delve breakdown  --  {tname}", chart_rows)
        await self._dm_audit_attach(
            ctx, embed, png, f"delve_{uid}.png", "DRS_DELVE", uid,
        )

    @drs.command(name="buddy", aliases=["buddies", "fren"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_buddy(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Buddy Network: FREN stake + slots + pending BUD + lifetime totals."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        try:
            row = await db.fetch_one(
                "SELECT * FROM user_buddy_economy "
                "WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
        except Exception:
            row = None
        if not row:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no Buddy economy state.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        fren_staked = to_human(int(row.get("fren_staked_raw") or 0))
        pending_bud = to_human(int(row.get("bud_yield_pending_raw") or 0))
        total_earned = to_human(int(row.get("total_bud_earned_raw") or 0))
        total_burned = to_human(int(row.get("total_bud_burned_raw") or 0))
        fren_price = float(prices.get("FREN", 0.0))
        bud_price = float(prices.get("BUD", 0.0))
        fren_usd = fren_staked * fren_price
        pending_usd = pending_bud * bud_price
        battle = int(row.get("battle_slots_purchased") or 0)
        storage = int(row.get("storage_slots_purchased") or 0)
        egg = int(row.get("egg_storage_slots_purchased") or 0)
        nest = int(row.get("nest_slots_purchased") or 0)
        # Slot USD value: pulled from config if available, else 0.
        try:
            from buddy_config import (
                BATTLE_SLOT_PRICE_USD,
                STORAGE_SLOT_PRICE_USD,
                EGG_STORAGE_PRICE_USD,
                NEST_SLOT_PRICE_USD,
            )
        except Exception:
            BATTLE_SLOT_PRICE_USD = STORAGE_SLOT_PRICE_USD = 0.0
            EGG_STORAGE_PRICE_USD = NEST_SLOT_PRICE_USD = 0.0
        slot_usd = (
            battle * float(BATTLE_SLOT_PRICE_USD)
            + storage * float(STORAGE_SLOT_PRICE_USD)
            + egg * float(EGG_STORAGE_PRICE_USD)
            + nest * float(NEST_SLOT_PRICE_USD)
        )
        chart_rows = [
            ("FREN stake", fren_usd),
            ("pending BUD", pending_usd),
            ("battle slots", battle * float(BATTLE_SLOT_PRICE_USD)),
            ("storage slots", storage * float(STORAGE_SLOT_PRICE_USD)),
            ("egg slots", egg * float(EGG_STORAGE_PRICE_USD)),
            ("nest slots", nest * float(NEST_SLOT_PRICE_USD)),
        ]
        total = sum(v for _, v in chart_rows)
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Buddy Network  --  {tname}", color=C_TEAL)
            .description(f"Total exposure: **{fmt_usd(total)}**")
            .field(
                "FREN + BUD",
                f"FREN staked: {fmt_token(fren_staked, 'FREN')} ({fmt_usd(fren_usd)})\n"
                f"Pending BUD: {fmt_token(pending_bud, 'BUD')} ({fmt_usd(pending_usd)})\n"
                f"Lifetime BUD earned: {fmt_token(total_earned, 'BUD')}\n"
                f"Lifetime BUD burned: {fmt_token(total_burned, 'BUD')}",
                False,
            )
            .field(
                "Slot Purchases (sunk cost)",
                f"Battle: **{battle}** ({fmt_usd(battle * float(BATTLE_SLOT_PRICE_USD))})\n"
                f"Storage: **{storage}** ({fmt_usd(storage * float(STORAGE_SLOT_PRICE_USD))})\n"
                f"Egg storage: **{egg}** ({fmt_usd(egg * float(EGG_STORAGE_PRICE_USD))})\n"
                f"Nest: **{nest}** ({fmt_usd(nest * float(NEST_SLOT_PRICE_USD))})\n"
                f"Slot total: **{fmt_usd(slot_usd)}**",
                False,
            )
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(f"Buddy exposure  --  {tname}", chart_rows)
        await self._dm_audit_attach(
            ctx, embed, png, f"buddy_{uid}.png", "DRS_BUDDY", uid,
        )

    @drs.command(name="crafting", aliases=["craft", "forge"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_crafting(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Crafting: INGOT stake + pending FORGE + crafted inventory."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        try:
            row = await db.fetch_one(
                "SELECT * FROM user_crafting WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
        except Exception:
            row = None
        if not row:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no Crafting state.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        ingot_staked = to_human(int(row.get("ingot_staked_raw") or 0))
        pending_forge = to_human(int(row.get("forge_yield_pending_raw") or 0))
        total_earned = to_human(int(row.get("total_forge_earned_raw") or 0))
        total_crafts = int(row.get("total_crafts") or 0)
        level = int(row.get("crafting_level") or 0)
        xp = int(row.get("crafting_xp") or 0)
        crafted = row.get("crafted_inventory") or {}
        if isinstance(crafted, str):
            try:
                import json as _json
                crafted = _json.loads(crafted)
            except Exception:
                crafted = {}
        ingot_p = float(prices.get("INGOT", 0.0))
        forge_p = float(prices.get("FORGE", 0.0))
        ingot_usd = ingot_staked * ingot_p
        pending_usd = pending_forge * forge_p
        # Crafted inventory value via crafting_config (FGD cost @ $1 stable).
        try:
            import configs.crafting_config as _cc
            craft_meta = getattr(_cc, "craft_meta", None) or (lambda k: {})
        except Exception:
            craft_meta = lambda k: {}
        inv_value = 0.0
        inv_lines = []
        for key, qty in (crafted.items() if isinstance(crafted, dict) else []):
            try:
                meta = craft_meta(key) or {}
            except Exception:
                meta = {}
            cost = float(meta.get("fgd_cost") or 0.0)
            v = float(qty or 0) * cost
            inv_value += v
            inv_lines.append(f"`{key}` x{qty}  --  {fmt_usd(v)}")
        chart_rows = [
            ("INGOT stake", ingot_usd),
            ("pending FORGE", pending_usd),
            ("crafted inventory", inv_value),
        ]
        total = sum(v for _, v in chart_rows)
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Crafting  --  {tname}", color=C_AMBER)
            .description(
                f"Lv **{level}** ({xp:,} XP)  --  total crafts **{total_crafts:,}**  "
                f"--  total {fmt_usd(total)}"
            )
            .field(
                "INGOT + FORGE",
                f"INGOT staked: {fmt_token(ingot_staked, 'INGOT')} ({fmt_usd(ingot_usd)})\n"
                f"Pending FORGE: {fmt_token(pending_forge, 'FORGE')} ({fmt_usd(pending_usd)})\n"
                f"Lifetime FORGE earned: {fmt_token(total_earned, 'FORGE')}",
                False,
            )
        )
        if inv_lines:
            text = "\n".join(inv_lines[:15])
            if len(inv_lines) > 15:
                text += f"\n... +{len(inv_lines) - 15} more"
            embed.field(
                f"Crafted Inventory ({len(inv_lines)} items)  --  "
                f"total {fmt_usd(inv_value)}",
                text, False,
            )
        embed.footer(f"DRS audit by {ctx.author.display_name}")
        png = _try_render_bars(f"Crafting exposure  --  {tname}", chart_rows)
        await self._dm_audit_attach(
            ctx, embed.build(), png, f"crafting_{uid}.png", "DRS_CRAFTING", uid,
        )

    @drs.command(name="savings", aliases=["deposits", "savings-deposits"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_savings(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """All savings deposits across every symbol the player holds."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        rows = await db.fetch_all(
            "SELECT symbol, amount, last_interest "
            "FROM savings_deposits WHERE user_id=$1 AND guild_id=$2 "
            "AND amount > 0 ORDER BY amount DESC",
            uid, gid,
        )
        if not rows:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no savings deposits.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        priced = []
        total_usd = 0.0
        for r in rows:
            sym = str(r["symbol"])
            amt = to_human(int(r["amount"]))
            price = 1.0 if sym == "USD" else float(prices.get(sym, 0.0))
            usd = amt * price
            total_usd += usd
            priced.append({
                "symbol": sym, "amount": amt, "usd": usd,
                "last_interest": r.get("last_interest"),
            })
        priced.sort(key=lambda x: x["usd"], reverse=True)
        lines = []
        for s in priced[:20]:
            ts = fmt_ts(s["last_interest"]) if s["last_interest"] else "-"
            lines.append(
                f"**{s['symbol']}**  --  {fmt_token(s['amount'], s['symbol'])} "
                f"({fmt_usd(s['usd'])})  last interest {ts}"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Savings  --  {tname}", color=C_INFO)
            .description(
                f"**{len(priced)}** deposit symbols  --  total {fmt_usd(total_usd)}"
            )
            .field("Deposits", "\n".join(lines), False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Savings by symbol  --  {tname}",
            [(s["symbol"], s["usd"]) for s in priced],
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"savings_{uid}.png", "DRS_SAVINGS", uid,
        )

    @drs.command(name="trades", aliases=["pnl", "trade-history"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_trades(
        self, ctx: DiscoContext, target: _MemberOrID, limit: int = 25,
    ) -> None:
        """Trade aggregates (user_profiles) + recent BUY/SELL/SWAP rows."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        if limit < 1 or limit > 200:
            await ctx.reply_error("limit must be between 1 and 200.")
            return
        profile = await db.fetch_one(
            "SELECT total_trades, total_trade_volume, realized_pnl "
            "FROM user_profiles WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        rows = await db.fetch_all(
            """SELECT tx_type, symbol_in, amount_in, symbol_out, amount_out,
                      price_at, ts
                 FROM transactions
                WHERE user_id=$1 AND guild_id=$2
                  AND tx_type IN ('BUY', 'SELL', 'SWAP', 'MM_BUY', 'MM_SELL', 'ARB')
                ORDER BY ts DESC LIMIT $3""",
            uid, gid, limit,
        )
        if not profile and not rows:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no trade history.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        # Aggregate volume by tx_type so the auditor sees where the
        # volume came from.
        vol_by_type: dict[str, float] = {}
        for r in rows:
            tt = str(r["tx_type"])
            # Volume estimate: take the larger of in/out priced sides.
            a_in = to_human(int(r.get("amount_in") or 0))
            a_out = to_human(int(r.get("amount_out") or 0))
            v_in = a_in * float(prices.get(str(r.get("symbol_in") or ""), 0.0))
            v_out = a_out * float(prices.get(str(r.get("symbol_out") or ""), 0.0))
            vol_by_type[tt] = vol_by_type.get(tt, 0.0) + max(v_in, v_out)
        total_trades = int((profile or {}).get("total_trades") or 0)
        total_volume = to_human(int((profile or {}).get("total_trade_volume") or 0))
        realized_pnl = to_human(int((profile or {}).get("realized_pnl") or 0))
        body = []
        for r in rows[:20]:
            sin = str(r.get("symbol_in") or "")
            sout = str(r.get("symbol_out") or "")
            ain = to_human(int(r.get("amount_in") or 0))
            aout = to_human(int(r.get("amount_out") or 0))
            body.append(
                f"`{fmt_ts(r['ts'])}` **{r['tx_type']}**  "
                f"-{fmt_token(ain, sin)} -> +{fmt_token(aout, sout)}"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Trades  --  {tname}", color=C_GOLD)
            .description(
                f"Lifetime trades: **{total_trades:,}**  --  "
                f"volume: **{fmt_usd(total_volume)}**  --  "
                f"realized PnL: **{fmt_usd(realized_pnl)}**"
            )
            .field(
                f"Recent {min(20, len(rows))} of {len(rows)} (last {limit} scan)",
                "\n".join(body) or "-", False,
            )
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Trade volume by type  --  {tname}",
            list(vol_by_type.items()),
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"trades_{uid}.png", "DRS_TRADES", uid,
        )

    @drs.command(name="work", aliases=["job", "career"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_work(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Active job + work sessions + WORK transaction aggregates."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        job_row = await db.get_user_job(uid, gid)
        # WORK transactions for cumulative + recent feed
        rows = await db.fetch_all(
            """SELECT symbol_out, amount_out, ts
                 FROM transactions
                WHERE user_id=$1 AND guild_id=$2 AND tx_type='WORK'
                ORDER BY ts DESC LIMIT 50""",
            uid, gid,
        )
        if not job_row and not rows:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no work history.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        # Aggregate earnings by payout symbol.
        by_sym: dict[str, float] = {}
        for r in rows:
            sym = str(r.get("symbol_out") or "")
            amt = to_human(int(r.get("amount_out") or 0))
            usd = amt * float(prices.get(sym, 0.0)) if sym else amt
            by_sym[sym] = by_sym.get(sym, 0.0) + usd
        total_usd = sum(by_sym.values())
        job_name = "HOMELESS"
        work_count = 0
        total_earned = 0.0
        if job_row:
            job_name = str(job_row.get("job_id") or "HOMELESS")
            work_count = int(job_row.get("work_count") or 0)
            total_earned = (
                job_row.h("total_earned") if hasattr(job_row, "h")
                else to_human(int(job_row.get("total_earned") or 0))
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        recent = "\n".join(
            f"`{fmt_ts(r['ts'])}` +"
            f"{fmt_token(to_human(int(r.get('amount_out') or 0)), str(r.get('symbol_out') or ''))}"
            for r in rows[:15]
        ) or "-"
        embed = (
            card(f"[DRS] Work  --  {tname}", color=C_GOLD)
            .description(
                f"Job: **{job_name}**  --  sessions: **{work_count:,}**  --  "
                f"job total: {fmt_usd(total_earned)}"
            )
            .field(
                f"WORK Tx Total (last 50)  --  {fmt_usd(total_usd)}",
                "  --  ".join(
                    f"{sym}: {fmt_usd(usd)}"
                    for sym, usd in sorted(by_sym.items(), key=lambda x: -x[1])
                ) or "-",
                False,
            )
            .field(f"Recent ({min(15, len(rows))} of {len(rows)})", recent, False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"WORK earnings by symbol  --  {tname}",
            list(by_sym.items()),
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"work_{uid}.png", "DRS_WORK", uid,
        )

    @drs.command(name="networth", aliases=["nw", "wealth", "breakdown"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_networth(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Full net-worth breakdown across every wealth category + chart."""
        from services.net_worth import compute_net_worth
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        try:
            nw = await compute_net_worth(uid, gid, db)
        except Exception:
            log.exception("DRS networth compute failed for uid=%s", uid)
            await ctx.reply_error(
                "Net-worth computation failed -- see logs.",
            )
            return
        if not nw or nw.total <= 0:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no positive net worth.",
            )
            return
        # All 26 contributing categories from NetWorthResult. Order
        # mirrors services/net_worth.py::NetWorthResult.total.
        categories: list[tuple[str, float]] = [
            ("Wallet",              float(getattr(nw, "wallet", 0.0) or 0.0)),
            ("Bank",                float(getattr(nw, "bank", 0.0) or 0.0)),
            ("CeFi Crypto",         float(getattr(nw, "cefi_crypto", 0.0) or 0.0)),
            ("DeFi Wallet",         float(getattr(nw, "defi_wallet", 0.0) or 0.0)),
            ("NPC Stakes",          float(getattr(nw, "stake_value", 0.0) or 0.0)),
            ("PoS Own Stake",       float(getattr(nw, "pos_stake_value", 0.0) or 0.0)),
            ("Lunar Mint",          float(getattr(nw, "moon_stake_value", 0.0) or 0.0)),
            ("Moon Pool",           float(getattr(nw, "moon_pool_stake_value", 0.0) or 0.0)),
            ("LP",                  float(getattr(nw, "lp_value", 0.0) or 0.0)),
            ("Mining Rigs",         float(getattr(nw, "rig_value", 0.0) or 0.0)),
            ("Delegations",         float(getattr(nw, "delegation_value", 0.0) or 0.0)),
            ("Savings",             float(getattr(nw, "savings_value", 0.0) or 0.0)),
            ("Items (Stones)",      float(getattr(nw, "items_value", 0.0) or 0.0)),
            ("Fishing",             float(getattr(nw, "fishing_stake_value", 0.0) or 0.0)),
            ("Delve Stakes",        float(getattr(nw, "delve_stake_value", 0.0) or 0.0)),
            ("Delve Party",         float(getattr(nw, "delve_party_value", 0.0) or 0.0)),
            ("Buddy Economy",       float(getattr(nw, "buddy_economy_value", 0.0) or 0.0)),
            ("Farming",             float(getattr(nw, "farming_stake_value", 0.0) or 0.0)),
            ("Farming Plot",        float(getattr(nw, "farming_plot_value", 0.0) or 0.0)),
            ("Farming Inv",         float(getattr(nw, "farming_inventory_value", 0.0) or 0.0)),
            ("Crafting",            float(getattr(nw, "crafting_stake_value", 0.0) or 0.0)),
            ("Crafting Inv",        float(getattr(nw, "crafting_inventory_value", 0.0) or 0.0)),
            ("Safety Module",       float(getattr(nw, "safety_module_value", 0.0) or 0.0)),
            ("Disc.Fun",            float(getattr(nw, "disc_fun_value", 0.0) or 0.0)),
            ("Gamba Stakes",        float(getattr(nw, "gamba_stake_value", 0.0) or 0.0)),
            ("Sage Stakes",         float(getattr(nw, "sage_stake_value", 0.0) or 0.0)),
            ("EatChain Stakes",     float(getattr(nw, "eat_stake_value", 0.0) or 0.0)),
            ("NFTs",                float(getattr(nw, "nft_value", 0.0) or 0.0)),
        ]
        loan_neg = -float(getattr(nw, "loan_liability", 0.0) or 0.0)
        # Build embed: top 15 contributors + summary
        positive = [(k, v) for k, v in categories if v > 0]
        positive.sort(key=lambda x: x[1], reverse=True)
        total = nw.total
        lines = []
        for label, v in positive[:20]:
            pct = (v / total * 100.0) if total > 0 else 0.0
            lines.append(
                f"**{label}**  --  {fmt_usd(v)}  ({pct:.1f}%)"
            )
        if loan_neg < 0:
            lines.append(f"**Loan Liability**  --  {fmt_usd(loan_neg)}")
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Net Worth  --  {tname}", color=C_NAVY)
            .description(
                f"Total: **{fmt_usd(total)}**  --  "
                f"**{len(positive)}** active categories"
            )
            .field("Breakdown", "\n".join(lines), False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Net worth breakdown  --  {tname}",
            positive,
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"networth_{uid}.png", "DRS_NETWORTH", uid,
        )

    # ══════════════════════════════════════════════════════════════════════
    # Round 4 surfaces  --  account flavour audits (daily streak,
    # consumable inventory, cross-network wallets, Eat the Rich history,
    # mining-group membership).
    # ══════════════════════════════════════════════════════════════════════

    @drs.command(name="daily", aliases=["streak", "daily-streak"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_daily(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Daily-claim streak + last claim timestamp + eligibility."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        user = await db.get_user(uid, gid)
        if not user:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** isn't registered.",
            )
            return
        streak = int(user.get("daily_streak") or 0)
        last_daily = user.get("last_daily")
        # Eligibility window comes from the daily cog; the bot enforces
        # one claim per UTC day. We compute "hours since last" against
        # the DB clock so the value matches what the live ,daily cog
        # would see.
        eligibility_row = await db.fetch_one(
            "SELECT EXTRACT(EPOCH FROM (NOW() - $1::timestamptz)) AS age "
            "WHERE $1::timestamptz IS NOT NULL",
            last_daily,
        ) if last_daily else None
        if eligibility_row is None:
            elig_text = "Never claimed -- eligible now."
        else:
            age_s = float(eligibility_row.get("age") or 0.0)
            age_h = age_s / 3600.0
            if age_h >= 24.0:
                elig_text = f"Eligible (last claim {age_h:.1f}h ago)."
            elif age_h >= 22.0:
                elig_text = f"Eligible soon ({age_h:.1f}h ago)."
            else:
                hrs_to_go = 24.0 - age_h
                elig_text = (
                    f"Not eligible. Next claim in {hrs_to_go:.1f}h "
                    f"(last claim {age_h:.1f}h ago)."
                )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Daily Streak  --  {tname}", color=C_AMBER)
            .description(
                f"Current streak: **{streak} days**"
            )
            .field("Last Claim", fmt_ts(last_daily) if last_daily else "Never", True)
            .field("Eligibility", elig_text, False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        await self._dm_audit_attach(
            ctx, embed, None, None, "DRS_DAILY", uid,
        )

    @drs.command(name="items", aliases=["consumables", "inventory"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_items(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Consumable inventory: validator guards, yield guards, gambling saves."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        vg = await db.get_validator_guard_count(uid, gid)
        yg = await db.get_yield_guard_count(uid, gid)
        try:
            gs_row = await db.fetch_one(
                "SELECT count FROM gambling_save_inventory "
                "WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
            gs = int((gs_row or {}).get("count") or 0)
        except Exception:
            gs = 0
        if vg <= 0 and yg <= 0 and gs <= 0:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** owns no consumable items.",
            )
            return
        _SI = Config.SHOP_ITEMS
        vg_cost = to_human(int(_SI.get("validator_guard", {}).get("cost_stable", 0)))
        yg_cost = to_human(int(_SI.get("yield_guard", {}).get("cost_stable", 0)))
        gs_cost = to_human(int(_SI.get("gambling_save", {}).get("cost_stable", 0)))
        vg_value = vg * vg_cost
        yg_value = yg * yg_cost
        gs_value = gs * gs_cost
        total = vg_value + yg_value + gs_value
        lines = []
        if vg > 0:
            lines.append(
                f"**Validator Guards** x{vg}  --  "
                f"{fmt_usd(vg_value)} ({fmt_usd(vg_cost)} each)"
            )
        if yg > 0:
            lines.append(
                f"**Yield Guards** x{yg}  --  "
                f"{fmt_usd(yg_value)} ({fmt_usd(yg_cost)} each)"
            )
        if gs > 0:
            lines.append(
                f"**Gambling Saves** x{gs}  --  "
                f"{fmt_usd(gs_value)} ({fmt_usd(gs_cost)} each)"
            )
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Consumables  --  {tname}", color=C_AMBER)
            .description(
                f"**{vg + yg + gs}** items  --  total {fmt_usd(total)}"
            )
            .field("Inventory", "\n".join(lines) or "-", False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Consumables  --  {tname}",
            [
                ("Validator Guards", vg_value),
                ("Yield Guards", yg_value),
                ("Gambling Saves", gs_value),
            ],
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"items_{uid}.png", "DRS_ITEMS", uid,
        )

    @drs.command(name="wallets", aliases=["addresses", "wallet-list"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_wallets(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """All DeFi wallet addresses + per-network holdings priced at oracle."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        addresses = await db.get_user_addresses(uid, gid)
        holdings = await db.get_all_wallet_holdings(uid, gid)
        if not addresses and not holdings:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no DeFi wallets.",
            )
            return
        prices = await _prices_for_guild(db, gid)
        # Group holdings by network and sum USD value.
        by_net_holdings: dict[str, list[dict]] = {}
        by_net_usd: dict[str, float] = {}
        for h in holdings:
            net = str(h.get("network") or "")
            sym = str(h.get("symbol") or "")
            amt = to_human(int(h.get("amount") or 0))
            price = float(prices.get(sym, 0.0))
            usd = amt * price
            by_net_holdings.setdefault(net, []).append({
                "symbol": sym, "amount": amt, "usd": usd, "price": price,
            })
            by_net_usd[net] = by_net_usd.get(net, 0.0) + usd
        # Group addresses by network.
        addrs_by_net: dict[str, list[dict]] = {}
        for a in addresses:
            net = str(a.get("network") or "")
            addrs_by_net.setdefault(net, []).append(a)
        all_nets = sorted(set(by_net_holdings) | set(addrs_by_net))
        tname = getattr(target, "display_name", None) or str(target.id)
        builder = (
            card(f"[DRS] Wallets  --  {tname}", color=C_NAVY)
            .description(
                f"**{len(addresses)}** addresses across **{len(all_nets)}** "
                f"networks  --  total holdings {fmt_usd(sum(by_net_usd.values()))}"
            )
            .footer(f"DRS audit by {ctx.author.display_name}")
        )
        # One field per network so the embed scans cleanly. Top 10 to
        # stay well under the 25-field embed limit.
        for net in all_nets[:10]:
            net_addrs = addrs_by_net.get(net, [])
            net_h = sorted(
                by_net_holdings.get(net, []),
                key=lambda x: x["usd"],
                reverse=True,
            )
            usd = by_net_usd.get(net, 0.0)
            addr_strs = []
            for a in net_addrs[:3]:
                addr = str(a.get("address") or "")
                label = str(a.get("label") or "")
                short = addr if len(addr) <= 22 else f"{addr[:10]}.{addr[-8:]}"
                addr_strs.append(f"`{short}`" + (f" ({label})" if label else ""))
            if len(net_addrs) > 3:
                addr_strs.append(f"... +{len(net_addrs) - 3} more")
            hold_strs = []
            for h in net_h[:6]:
                if h["amount"] <= 0:
                    continue
                hold_strs.append(
                    f"{fmt_token(h['amount'], h['symbol'])} "
                    f"({fmt_usd(h['usd'])})"
                )
            if len(net_h) > 6:
                hold_strs.append(f"... +{len(net_h) - 6} more")
            body = ""
            if addr_strs:
                body += "**Addrs:** " + ", ".join(addr_strs) + "\n"
            if hold_strs:
                body += "**Holdings:** " + " · ".join(hold_strs)
            builder.field(
                f"`{net or '(no network)'}`  --  {fmt_usd(usd)}",
                body or "-", False,
            )
        if len(all_nets) > 10:
            builder.field(
                "More networks not shown",
                f"+{len(all_nets) - 10} additional networks  --  "
                f"export via `,drs eq export` to see everything.",
                False,
            )
        png = _try_render_bars(
            f"Wallet value by network  --  {tname}",
            [(net or "(none)", v) for net, v in by_net_usd.items()],
        )
        await self._dm_audit_attach(
            ctx, builder.build(), png, f"wallets_{uid}.png", "DRS_WALLETS", uid,
        )

    @drs.command(name="eat", aliases=["eattherich", "classwar"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_eat(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Eat the Rich history: eats made, times survived, USD flow."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        row = await db.fetch_one(
            "SELECT * FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        if not row:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has never sat at the table.",
            )
            return
        att = int((row or {}).get("heists_attempted") or 0)
        won = int((row or {}).get("heists_won") or 0)
        devoured = to_human(int((row or {}).get("total_stolen") or 0))
        targeted = int((row or {}).get("times_targeted") or 0)
        survived = int((row or {}).get("times_defended") or 0)
        lost = to_human(int((row or {}).get("total_lost") or 0))
        win_pct = (won / att * 100.0) if att > 0 else 0.0
        survive_pct = (survived / targeted * 100.0) if targeted > 0 else 0.0
        net = devoured - lost
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Eat the Rich  --  {tname}", color=C_ERROR)
            .description(
                f"Net: **{fmt_usd(net)}** "
                f"({fmt_usd(devoured)} devoured vs {fmt_usd(lost)} lost)"
            )
            .field(
                "Eats Made",
                f"**{won}**/{att} won  ({win_pct:.1f}% win rate)\n"
                f"Devoured: **{fmt_usd(devoured)}**",
                True,
            )
            .field(
                "Hunted",
                f"**{survived}**/{targeted} survived  ({survive_pct:.1f}% survival rate)\n"
                f"Lost: **{fmt_usd(lost)}**",
                True,
            )
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        png = _try_render_bars(
            f"Eat the Rich flow  --  {tname}",
            [
                ("Devoured (eats won)", devoured),
                ("Lost (eats failed)", lost),
            ],
        )
        await self._dm_audit_attach(
            ctx, embed, png, f"eat_{uid}.png", "DRS_EAT", uid,
        )

    @drs.command(name="guild", aliases=["group", "mining-group"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_guild(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Mining-group membership: which group, founder, members, since-when."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        group = None
        try:
            group = await db.get_user_mining_group(uid, gid)
        except Exception:
            group = None
        if not group:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** isn't in a mining group.",
            )
            return
        group_id = str(group.get("group_id") or "")
        name = str(group.get("name") or group_id)
        founder_id = int(group.get("founder_id") or 0)
        created_at = group.get("created_at")
        joined_at = group.get("joined_at")
        # Roster + per-member contribution. Reads mining_group_members
        # directly so the audit view sees every active member without
        # depending on a higher-level "list_group_members" helper.
        members = []
        try:
            members = await db.fetch_all(
                "SELECT user_id, joined_at FROM mining_group_members "
                "WHERE group_id=$1 AND guild_id=$2 ORDER BY joined_at ASC",
                group_id, gid,
            )
        except Exception:
            members = []
        roster_lines = []
        for m in members[:15]:
            tag = ""
            if int(m["user_id"]) == founder_id:
                tag = " [FOUNDER]"
            if int(m["user_id"]) == uid:
                tag += " [TARGET]"
            roster_lines.append(
                f"{_mention(int(m['user_id']), ctx.guild, ctx.bot)} "
                f"-- joined {fmt_ts(m['joined_at'])}{tag}"
            )
        if len(members) > 15:
            roster_lines.append(f"... +{len(members) - 15} more")
        is_founder = (founder_id == uid)
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Mining Group  --  {tname}", color=C_TEAL)
            .description(
                f"Group: **{name}**  --  ID: `{group_id}`  --  "
                f"role: **{'FOUNDER' if is_founder else 'MEMBER'}**"
            )
            .field(
                "Membership",
                f"Founder: {_mention(founder_id, ctx.guild, ctx.bot)}\n"
                f"Created: {fmt_ts(created_at)}\n"
                f"Target joined: {fmt_ts(joined_at) if joined_at else '-'}\n"
                f"Roster size: **{len(members)}**",
                False,
            )
            .field(
                f"Roster ({min(15, len(members))} of {len(members)})",
                "\n".join(roster_lines) or "-",
                False,
            )
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        await self._dm_audit_attach(
            ctx, embed, None, None, "DRS_GUILD", uid,
        )

    # ══════════════════════════════════════════════════════════════════════
    # Round 5  --  account control surfaces (DM prefs + active locks) plus
    # the token-wide audit which steps away from per-player auditing to
    # look at one symbol across the guild.
    # ══════════════════════════════════════════════════════════════════════

    @drs.command(name="prefs", aliases=["preferences", "dm-settings"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_prefs(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """User preferences: DM opt-ins, muted-network lists."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        prefs = None
        try:
            prefs = await db.get_user_prefs(uid, gid)
        except Exception:
            prefs = None
        if not prefs:
            await ctx.reply_error(
                f"**{getattr(target, 'display_name', target.id)}** has no prefs row "
                f"(still on defaults).",
            )
            return
        dm_keys = [
            "dm_mining", "dm_transfer", "dm_validator", "dm_staking",
            "dm_2fa", "dm_events", "dm_nft", "dm_predictions", "dm_ape",
            "dm_itemlevelup", "dm_whale_alerts",
        ]
        muted_keys = [
            "muted_networks_mining", "muted_networks_staking",
            "muted_networks_validator", "muted_networks_whale",
        ]
        on_lines = []
        off_lines = []
        for k in dm_keys:
            label = k.replace("dm_", "").replace("_", " ")
            (on_lines if bool(prefs.get(k, False)) else off_lines).append(label)
        muted_lines = []
        for k in muted_keys:
            val = str(prefs.get(k, "") or "").strip()
            if val:
                muted_lines.append(f"**{k.replace('muted_networks_', '')}**: `{val}`")
        tname = getattr(target, "display_name", None) or str(target.id)
        builder = (
            card(f"[DRS] User Preferences  --  {tname}", color=C_INFO)
            .field(
                f"DM opt-ins ({len(on_lines)} on / {len(dm_keys)} total)",
                ", ".join(f"`{x}`" for x in on_lines) or "(none)",
                False,
            )
            .field(
                "DM opt-outs",
                ", ".join(f"`{x}`" for x in off_lines) or "(none)",
                False,
            )
        )
        if muted_lines:
            builder.field("Muted Networks", "\n".join(muted_lines), False)
        builder.footer(f"DRS audit by {ctx.author.display_name}")
        await self._dm_audit_attach(
            ctx, builder.build(), None, None, "DRS_PREFS", uid,
        )

    @drs.command(name="locks", aliases=["cooldowns", "active-locks"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_locks(self, ctx: DiscoContext, target: _MemberOrID) -> None:
        """Active locks/cooldowns: validator stake, SM cooldown, delegations."""
        uid, gid = int(target.id), ctx.guild_id
        db = ctx.db
        # Validator stake locked_until (per network).
        validators = []
        try:
            validators = await db.get_pos_validators_for_user(uid, gid)
        except Exception:
            validators = []
        val_locks = []
        for v in validators:
            until = v.get("stake_locked_until")
            net = str(v.get("network") or "")
            if until:
                val_locks.append(f"**{net}** stake locked until `{fmt_ts(until)}`")
        # Outgoing delegation locked_until (per delegation).
        del_locks = []
        try:
            dels = await db.get_user_delegations(uid, gid)
        except Exception:
            dels = []
        for d in dels:
            until = d.get("locked_until")
            net = str(d.get("network") or "")
            tok = str(d.get("token") or "")
            if until:
                del_locks.append(
                    f"**{net}**/{tok} delegation locked until `{fmt_ts(until)}`"
                )
        # Safety Module cooldowns (per symbol).
        sm_locks = []
        for sym in ("VTR", "DSY"):
            try:
                sm = await db.get_sm_stake(uid, gid, sym)
            except Exception:
                sm = None
            if sm and sm.get("cooldown_at"):
                sm_locks.append(
                    f"**{sym}** SM cooldown until `{fmt_ts(sm['cooldown_at'])}`"
                )
        # Loan: not really a lock but worth surfacing as an obligation.
        loan = await db.get_loan(uid, gid)
        if loan and int(loan.get("outstanding") or 0) > 0:
            loan_text = (
                f"loan outstanding "
                f"`{fmt_usd(to_human(int(loan['outstanding'])))}`"
            )
        else:
            loan_text = "no outstanding loan"
        tname = getattr(target, "display_name", None) or str(target.id)
        embed = (
            card(f"[DRS] Active Locks  --  {tname}", color=C_WARNING)
            .field(
                f"Validator stake locks ({len(val_locks)})",
                "\n".join(val_locks) or "(no validator locks)",
                False,
            )
            .field(
                f"Outgoing delegation locks ({len(del_locks)})",
                "\n".join(del_locks) or "(no delegation locks)",
                False,
            )
            .field(
                f"Safety Module cooldowns ({len(sm_locks)})",
                "\n".join(sm_locks) or "(none)",
                False,
            )
            .field("Loan obligation", loan_text, False)
            .footer(f"DRS audit by {ctx.author.display_name}")
            .build()
        )
        await self._dm_audit_attach(
            ctx, embed, None, None, "DRS_LOCKS", uid,
        )

    @drs.command(name="token", aliases=["coin", "sym"])
    @guild_only
    @no_bots
    @ensure_registered
    async def drs_token(self, ctx: DiscoContext, symbol: str) -> None:
        """Token-wide audit: supply, holders, price, top holders, market state.

        Unlike every other DRS audit, this one is keyed on a symbol
        instead of a player. Shows where the token's circulating supply
        actually sits across CeFi + DeFi + stakes + delegations.
        """
        gid = ctx.guild_id
        db = ctx.db
        sym = symbol.upper().strip()
        if not sym or not sym.isalnum():
            await ctx.reply_error("Symbol must be alphanumeric.")
            return
        # Price + config snapshot.
        price_row = await db.get_price(sym, gid)
        price = float((price_row or {}).get("price") or 0.0)
        # Holders split across CeFi (holdings), DeFi (wallet_holdings),
        # plus NPC stakes, delegations, gamba_stakes, etc. Each query is
        # cheap because it's filtered to one symbol.
        cefi_rows = await db.fetch_all(
            "SELECT user_id, amount FROM holdings "
            "WHERE guild_id=$1 AND symbol=$2 AND amount > 0",
            gid, sym,
        )
        defi_rows = await db.fetch_all(
            "SELECT user_id, network, amount FROM wallet_holdings "
            "WHERE guild_id=$1 AND symbol=$2 AND amount > 0",
            gid, sym,
        )
        npc_stake_rows = await db.fetch_all(
            "SELECT user_id, SUM(amount) AS amount FROM stakes "
            "WHERE guild_id=$1 AND symbol=$2 AND amount > 0 GROUP BY user_id",
            gid, sym,
        )
        del_rows = await db.fetch_all(
            "SELECT delegator_id AS user_id, SUM(amount) AS amount "
            "FROM pos_delegations WHERE guild_id=$1 AND token=$2 AND amount > 0 "
            "GROUP BY delegator_id",
            gid, sym,
        )
        try:
            gam_rows = await db.fetch_all(
                "SELECT user_id, amount FROM gamba_stakes "
                "WHERE guild_id=$1 AND symbol=$2 AND amount > 0",
                gid, sym,
            )
        except Exception:
            gam_rows = []
        # Per-user aggregate for top-holder list.
        per_user: dict[int, float] = {}
        bucket_totals = {"cefi": 0.0, "defi": 0.0, "npc": 0.0, "delegation": 0.0, "gamba": 0.0}
        for r in cefi_rows:
            v = to_human(int(r["amount"]))
            per_user[r["user_id"]] = per_user.get(r["user_id"], 0.0) + v
            bucket_totals["cefi"] += v
        for r in defi_rows:
            v = to_human(int(r["amount"]))
            per_user[r["user_id"]] = per_user.get(r["user_id"], 0.0) + v
            bucket_totals["defi"] += v
        for r in npc_stake_rows:
            v = to_human(int(r["amount"]))
            per_user[r["user_id"]] = per_user.get(r["user_id"], 0.0) + v
            bucket_totals["npc"] += v
        for r in del_rows:
            v = to_human(int(r["amount"]))
            per_user[r["user_id"]] = per_user.get(r["user_id"], 0.0) + v
            bucket_totals["delegation"] += v
        for r in gam_rows:
            v = to_human(int(r["amount"]))
            per_user[r["user_id"]] = per_user.get(r["user_id"], 0.0) + v
            bucket_totals["gamba"] += v
        circulating = sum(bucket_totals.values())
        if circulating <= 0:
            await ctx.reply_error(
                f"No `{sym}` supply found in this guild "
                f"(empty token or symbol mistyped).",
            )
            return
        top_holders = sorted(per_user.items(), key=lambda x: x[1], reverse=True)[:10]
        # Concentration ratios over the holder set.
        top1_pct = (top_holders[0][1] / circulating * 100.0) if top_holders else 0.0
        top10_pct = sum(v for _, v in top_holders) / circulating * 100.0
        n_holders = len(per_user)
        builder = (
            card(f"[DRS] Token  --  {sym}", color=C_NAVY)
            .description(
                f"Price: **{fmt_usd(price)}**  --  "
                f"Counted supply: **{fmt_token(circulating, sym)}**  --  "
                f"Holders: **{n_holders}**"
            )
            .field(
                "Supply by bucket",
                "\n".join(
                    f"**{k}**: {fmt_token(v, sym)} ({(v / circulating * 100.0):.2f}%)"
                    for k, v in sorted(
                        bucket_totals.items(), key=lambda x: -x[1],
                    ) if v > 0
                ),
                False,
            )
            .field(
                "Concentration",
                f"Top 1: **{top1_pct:.2f}%**  --  Top 10: **{top10_pct:.2f}%**",
                True,
            )
        )
        if top_holders:
            lines = []
            for i, (h_uid, amt) in enumerate(top_holders, start=1):
                lines.append(
                    f"`#{i:>2}` {_mention(h_uid, ctx.guild, ctx.bot)} -- "
                    f"{fmt_token(amt, sym)} ({amt / circulating * 100.0:.2f}%)"
                )
            builder.field(
                f"Top {len(top_holders)} Holders",
                "\n".join(lines), False,
            )
        builder.footer(f"DRS audit by {ctx.author.display_name}")
        png = _try_render_bars(
            f"`{sym}` supply by bucket",
            [(k, v * price) for k, v in bucket_totals.items() if v > 0],
        )
        # Log as DRS_TOKEN with the symbol in details (no per-user target).
        await _log_drs_action(
            self.bot, gid, ctx.author.id, "DRS_TOKEN",
            details=f"symbol={sym}",
        )
        kwargs: dict = {"embed": builder.build()}
        if png is not None:
            builder_embed = builder.build()
            builder_embed.set_image(url=f"attachment://token_{sym}.png")
            kwargs["embed"] = builder_embed
            kwargs["file"] = discord.File(
                io.BytesIO(png), filename=f"token_{sym}.png",
            )
        try:
            await ctx.author.send(**kwargs)
            await ctx.reply_success(
                f"`{sym}` audit sent to your DMs.",
                title="[DRS] Token",
            )
        except discord.Forbidden:
            if png is not None:
                kwargs["file"] = discord.File(
                    io.BytesIO(png), filename=f"token_{sym}.png",
                )
            await ctx.reply(mention_author=False, **kwargs)

    # ── shared internal: DM-or-fallback for an embed + optional chart ──
    async def _dm_audit_attach(
        self,
        ctx: DiscoContext,
        embed: discord.Embed,
        png: bytes | None,
        filename: str | None,
        action: str,
        target_id: int,
    ) -> None:
        """Send ``embed`` (+ optional PNG attachment) to the operator's DMs.

        Mirrors the existing ``drs_profile`` UX: sensitive audit views
        go to DMs by default, falling back to an in-channel reply if
        the operator has DMs closed. Always logs the DRS action.
        """
        await _log_drs_action(
            self.bot, ctx.guild_id, ctx.author.id, action,
            target_id=target_id,
        )
        kwargs: dict = {"embed": embed}
        if png is not None and filename:
            embed.set_image(url=f"attachment://{filename}")
            kwargs["file"] = discord.File(io.BytesIO(png), filename=filename)
        try:
            await ctx.author.send(**kwargs)
            await ctx.reply_success(
                "Audit sent to your DMs.",
                title=f"[DRS] {action.replace('DRS_', '').title()}",
            )
        except discord.Forbidden:
            # Need a fresh File object because the previous one's stream
            # is exhausted after the failed send attempt.
            if png is not None and filename:
                kwargs["file"] = discord.File(io.BytesIO(png), filename=filename)
            await ctx.reply(mention_author=False, **kwargs)



# ── Module-level helpers used by the per-surface DRS commands ──────────
# A single oracle-price snapshot per command is cheap and keeps every
# valuation consistent within one audit reply.


async def _prices_for_guild(db, gid: int) -> dict[str, float]:
    """Return ``{symbol: usd_price}`` for the guild's oracle prices."""
    try:
        rows = await db.get_all_prices(gid)
    except Exception:
        return {}
    return {str(r["symbol"]): float(r["price"]) for r in rows}


def _try_render_bars(title: str, items: list[tuple[str, float]]):
    """Pillow horizontal bar chart of (label, usd) rows -> PNG bytes or
    ``None`` if rendering is unavailable in this container."""
    rows = [(lbl, v) for lbl, v in items if v and v > 0]
    if not rows:
        return None
    try:
        from services import drs_charts as _ec
        return _ec.render_value_bars(title, rows)
    except Exception:
        log.exception("DRS chart render failed: %s", title)
        return None


# Emoji prefix per tx_type for ,drs timeline so the feed is scannable.
# Anything not listed falls back to "·".
_TX_EMOJI: dict[str, str] = {
    "WORK":              "💼",
    "BUY":               "🛒",
    "SELL":              "💵",
    "SWAP":              "🔁",
    "MM_BUY":            "🤖",
    "MM_SELL":           "🤖",
    "ARB":               "📈",
    "ORACLE_REBALANCE":  "⚖️",
    "SEND":              "📤",
    "TRANSFER":          "📤",
    "STAKE_REWARD":      "💰",
    "VALIDATOR_REWARD":  "🛡️",
    "LP_YIELD":          "💧",
    "LUNAR_MINT":        "🌙",
    "MOON_POOL_YIELD":   "🌙",
    "MOON_WRAP":         "🌗",
    "MOON_UNWRAP":       "🌘",
    "ADD_LP":            "➕",
    "REMOVE_LP":         "➖",
    "GAMBLE":            "🎰",
}


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Helpers(bot))
