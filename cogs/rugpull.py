"""
cogs/rugpull.py - King / Queen of Rugs minigame.

Players wager money for a chance to claim the "King of Rugs" (or "Queen of Rugs"
for female players) title (Discord role). The role gives bonuses to .work and
.ape that scale up the longer the monarch holds the throne. When a new player
wins, the role transfers from the old monarch to the new one. Only one monarch
exists at a time per guild -- King and Queen roles are mutually exclusive.

Tiers:
  Low    - 3% of balance (min $50)   ->  5% chance
  Medium - 15% of balance (min $250) -> 40% chance
  High   - 30% of balance (min $500) -> 75% chance

When a challenger fails:
  - Their wager is split by the king's tax_rate (default 100%)
  - King receives wager * tax_rate directly
  - Remainder (1 - tax_rate) feeds into the bounty pool

Defense streak:
  Each successful defense increments the king's defense_streak, reducing all
  challenger success chances by RUGPULL_DEFENSE_BONUS per streak (cap 15%).
  Sabotage pool (funded by other players) decays this defense bonus.

Bounty:
  Anyone can add to the bounty pool. If a challenger wins, they collect the full bounty.

Tax decree:
  The king can set their tax rate (min 25%). Lower tax = more bounty growth.
"""
from __future__ import annotations

import asyncio
import datetime
import random
import time

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.ai import complete as ai_complete
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.heartbeat import pulse, register_interval
from core.framework.middleware import ensure_registered, guild_only, module_allowed, no_bots
from core.framework.cooldowns import user_cooldown
from services.active_players import get_random_active_players
from services.net_worth import compute_net_worth
from services.rugpull_gender import (
    get_stored_gender,
    monarch_role_id,
    monarch_role_ids,
    monarch_title,
    resolve_gender,
    set_manual_gender,
)
from cogs.social_context import mark_hot_channel
from core.framework.scale import to_human
from core.framework.ui import (
    C_AMBER, C_CRIMSON, C_ERROR, C_GOLD, C_INFO, C_SUCCESS, C_SELL, fmt_ts,
    fmt_usd,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_dollar_amount(raw: str) -> float | None:
    """Parse a plain number with optional k/m suffix. Returns None on failure."""
    s = raw.strip().lstrip("$").replace(",", "")
    mult = 1.0
    if s.lower().endswith("m"):
        mult = 1_000_000.0
        s = s[:-1]
    elif s.lower().endswith("k"):
        mult = 1_000.0
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


async def _get_king(db, guild_id: int) -> dict | None:
    return await db.fetch_one(
        "SELECT * FROM rugpull_king WHERE guild_id=$1", guild_id
    )


async def _set_king(db, guild_id: int, user_id: int, vault_amount: float = 0.0) -> None:
    """Crown a new monarch. Resets every state field tied to the previous reign:
    defense_streak, tax_rate, sabotage_pool, bounty_pool, active_defense_*,
    and defense_last_used_at."""
    await db.execute(
        """INSERT INTO rugpull_king
               (guild_id, user_id, vault_amount, crowned_at,
                defense_streak, tax_rate, sabotage_pool, bounty_pool,
                active_defense_until, active_defense_bonus, defense_last_used_at)
           VALUES ($1, $2, $3, now(), 0, 1.00, 0.0, 0.0, NULL, 0, NULL)
           ON CONFLICT (guild_id) DO UPDATE SET
               user_id              = excluded.user_id,
               vault_amount         = excluded.vault_amount,
               crowned_at           = now(),
               defense_streak       = 0,
               tax_rate             = 1.00,
               sabotage_pool        = 0.0,
               bounty_pool          = 0.0,
               active_defense_until = NULL,
               active_defense_bonus = 0,
               defense_last_used_at = NULL""",
        guild_id, user_id, vault_amount,
    )


async def _apply_wager_loss(db, guild_id: int, wager: float) -> tuple[float, float]:
    """
    Apply a failed challenger's wager using the king's tax_rate.
    Returns (king_take, bounty_take) where king_take is paid to king's wallet
    and bounty_take is added to the bounty_pool.
    """
    king = await _get_king(db, guild_id)
    if not king:
        return wager, 0.0
    tax_rate = float(king.get("tax_rate", 1.0))
    king_take = round(wager * tax_rate, 8)
    bounty_take = round(wager - king_take, 8)
    new_vault = float(king["vault_amount"]) + king_take
    new_bounty = float(king.get("bounty_pool", 0)) + bounty_take
    await db.execute(
        "UPDATE rugpull_king SET vault_amount=$1, bounty_pool=$2 WHERE guild_id=$3",
        new_vault, new_bounty, guild_id,
    )
    return king_take, bounty_take


async def _increment_defense(db, guild_id: int) -> int:
    """Increment the king's defense streak. Returns the new streak value."""
    await db.execute(
        "UPDATE rugpull_king SET defense_streak = defense_streak + 1 WHERE guild_id=$1",
        guild_id,
    )
    king = await _get_king(db, guild_id)
    return int(king["defense_streak"]) if king else 1


async def _get_stats(db, user_id: int, guild_id: int) -> dict:
    row = await db.fetch_one(
        "SELECT * FROM rugpull_stats WHERE user_id=$1 AND guild_id=$2",
        user_id, guild_id,
    )
    if row:
        return dict(row)
    return {
        "user_id": user_id, "guild_id": guild_id,
        "wins": 0, "losses": 0, "total_wagered": 0.0,
        "total_hold_seconds": 0, "longest_hold_secs": 0,
        "last_crowned_at": None, "last_dethroned_at": None,
        "defenses": 0, "sabotages_done": 0, "bounties_placed": 0.0,
    }


async def _update_stats(db, user_id: int, guild_id: int, **kwargs) -> None:
    """Upsert rugpull_stats for a user."""
    await db.execute(
        """INSERT INTO rugpull_stats
               (user_id, guild_id, wins, losses, total_wagered,
                total_hold_seconds, longest_hold_secs, last_crowned_at, last_dethroned_at,
                defenses, sabotages_done, bounties_placed)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
           ON CONFLICT (user_id, guild_id) DO UPDATE SET
               wins               = rugpull_stats.wins + excluded.wins,
               losses             = rugpull_stats.losses + excluded.losses,
               total_wagered      = rugpull_stats.total_wagered + excluded.total_wagered,
               total_hold_seconds = rugpull_stats.total_hold_seconds + excluded.total_hold_seconds,
               longest_hold_secs  = GREATEST(rugpull_stats.longest_hold_secs, excluded.longest_hold_secs),
               last_crowned_at    = COALESCE(excluded.last_crowned_at, rugpull_stats.last_crowned_at),
               last_dethroned_at  = COALESCE(excluded.last_dethroned_at, rugpull_stats.last_dethroned_at),
               defenses           = rugpull_stats.defenses + excluded.defenses,
               sabotages_done     = rugpull_stats.sabotages_done + excluded.sabotages_done,
               bounties_placed    = rugpull_stats.bounties_placed + excluded.bounties_placed""",
        user_id, guild_id,
        kwargs.get("wins", 0), kwargs.get("losses", 0), kwargs.get("total_wagered", 0.0),
        kwargs.get("total_hold_seconds", 0), kwargs.get("longest_hold_secs", 0),
        kwargs.get("last_crowned_at"), kwargs.get("last_dethroned_at"),
        kwargs.get("defenses", 0), kwargs.get("sabotages_done", 0), kwargs.get("bounties_placed", 0.0),
    )


async def _transfer_role(
    guild: discord.Guild,
    old_user_id: int | None,
    new_user_id: int,
    new_gender: str,
    wager: float = 0.0,
    bounty_collected: float = 0.0,
    hold_secs: int = 0,
    king_earnings: float = 0.0,
) -> None:
    """Move the monarch role from the old holder to the new one, and send DMs.

    Honors gender: female winners get the Queen of Rugs role (if configured),
    males get the King of Rugs role. Whichever role any previous holder had,
    it is stripped first. Only one monarch role can be held at a time across
    both King and Queen, so this also strips the *other* gender's role from
    the new winner if for some reason they had it.
    """
    target_role_id = monarch_role_id(new_gender)
    all_monarch_ids = set(monarch_role_ids())

    # Remove every configured monarch role from the dethroned holder
    if old_user_id:
        old_member = guild.get_member(old_user_id)
        if old_member:
            to_remove = [r for r in old_member.roles if r.id in all_monarch_ids]
            for r in to_remove:
                try:
                    await old_member.remove_roles(r, reason="Rugpull: dethroned")
                except Exception:
                    pass

    new_member = guild.get_member(new_user_id)
    if new_member:
        # Strip the opposite-gender monarch role if the winner happens to have it
        for r in list(new_member.roles):
            if r.id in all_monarch_ids and r.id != target_role_id:
                try:
                    await new_member.remove_roles(r, reason="Rugpull: switching monarch role")
                except Exception:
                    pass
        # Grant the new monarch role
        if target_role_id:
            role = guild.get_role(target_role_id)
            if role and role not in new_member.roles:
                try:
                    await new_member.add_roles(role, reason=f"Rugpull: new {monarch_title(new_gender)}")
                except Exception:
                    pass

    title = monarch_title(new_gender)

    # DM the dethroned monarch
    if old_user_id:
        old_member = guild.get_member(old_user_id)
        if old_member:
            new_name = new_member.display_name if new_member else f"User {new_user_id}"
            hold_h = hold_secs // 3600
            hold_m = (hold_secs % 3600) // 60
            try:
                await old_member.send(
                    f"**👑 You've been dethroned in {guild.name}!**\n"
                    f"**{new_name}** has rugpulled you after {hold_h}h {hold_m}m on the throne.\n"
                    f"**Earnings this reign:** ${king_earnings:,.2f}\n"
                    f"*Better luck next time  -  use `,rugpull` to reclaim your crown.*"
                )
            except Exception:
                pass

    # DM the new monarch
    if new_member:
        bounty_line = f"\n**Bounty collected:** ${bounty_collected:,.2f} 💰" if bounty_collected > 0 else ""
        try:
            await new_member.send(
                f"**👑 You are now {title} in {guild.name}!**\n"
                f"**Wager paid:** ${wager:,.2f}{bounty_line}\n"
                f"*Your bonuses grow the longer you hold the throne.*\n"
                f"*Others can challenge you with `,rugpull`. Use `,taxdecree` to set your tax rate, "
                f"or `,rugdefend <amount>` to spend money on an active defense.*"
            )
        except Exception:
            pass


async def has_rugpull_role(member: discord.Member) -> bool:
    """Check if a member currently holds the King or Queen of Rugs role."""
    monarch_ids = set(monarch_role_ids())
    if not monarch_ids:
        return False
    return any(r.id in monarch_ids for r in member.roles)


def _compute_reign_perks(king: dict) -> tuple[float, float]:
    """Return (work_bonus, ape_bonus) linearly scaled by reign duration up to RUGPULL_PERK_HOURS."""
    _crowned_raw = king.get("crowned_at")
    crowned_ts = _crowned_raw.timestamp() if hasattr(_crowned_raw, "timestamp") else float(_crowned_raw or 0)
    hold_secs = max(0.0, time.time() - crowned_ts)
    perk_secs = Config.RUGPULL_PERK_HOURS * 3600
    t = min(1.0, hold_secs / perk_secs) if perk_secs > 0 else 1.0
    work = Config.RUGPULL_WORK_BONUS + t * (Config.RUGPULL_MAX_WORK_BONUS - Config.RUGPULL_WORK_BONUS)
    ape = Config.RUGPULL_APE_BONUS + t * (Config.RUGPULL_MAX_APE_BONUS - Config.RUGPULL_APE_BONUS)
    return work, ape


def _compute_defense_bonus(king: dict) -> float:
    """
    Return the effective defense bonus applied against all challengers (reduces their success chance).
    Raw bonus = min(streak * DEFENSE_BONUS, MAX_DEFENSE_BONUS).
    Sabotage decay = sabotage_pool * SABOTAGE_DECAY * DEFENSE_BONUS (per-streak unit).
    Active (paid) defense bonus stacks on top while ``active_defense_until`` is in the future.
    """
    streak = int(king.get("defense_streak", 0))
    raw = min(streak * Config.RUGPULL_DEFENSE_BONUS, Config.RUGPULL_MAX_DEFENSE_BONUS)
    sabotage = king.h("sabotage_pool")
    decay = sabotage * Config.RUGPULL_SABOTAGE_DECAY
    passive = max(0.0, raw - decay)

    active = 0.0
    until = king.get("active_defense_until")
    if until is not None:
        until_ts = until.timestamp() if hasattr(until, "timestamp") else float(until or 0)
        if until_ts > time.time():
            active = float(king.get("active_defense_bonus", 0) or 0)
    return min(passive + active, 0.99)


def _compute_crown_discount(king: dict | None) -> float:
    """
    Return the cost discount applied to all rugpull tier costs when a monarch
    is on the throne. Base discount is ``RUGPULL_CROWN_DISCOUNT`` (50%); it
    grows linearly with reign length up to ``RUGPULL_CROWN_MAX_DISCOUNT`` after
    ``RUGPULL_CROWN_DISCOUNT_HOURS`` hours. Empty throne = 0% discount.
    """
    if not king:
        return 0.0
    base = Config.RUGPULL_CROWN_DISCOUNT
    cap = Config.RUGPULL_CROWN_MAX_DISCOUNT
    hours = Config.RUGPULL_CROWN_DISCOUNT_HOURS
    _crowned_raw = king.get("crowned_at")
    crowned_ts = _crowned_raw.timestamp() if hasattr(_crowned_raw, "timestamp") else float(_crowned_raw or 0)
    hold_secs = max(0.0, time.time() - crowned_ts)
    t = min(1.0, hold_secs / (hours * 3600)) if hours > 0 else 1.0
    return min(cap, base + t * (cap - base))


async def _log_history(db, guild_id: int, user_id: int, tier: str,
                        wager: float, won: bool, king_id: int | None) -> None:
    try:
        await db.execute(
            """INSERT INTO rugpull_history (guild_id, user_id, tier, wager, won, king_id)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            guild_id, user_id, tier, wager, won, king_id,
        )
    except Exception:
        pass


# ── Rugpull flavor text ───────────────────────────────────────────────────────
# Supports {king} and {amount} substitution at runtime.

_RUGPULL_FAIL_FLAVORS: list[str] = [
    "You tried to rug **{king}** but they were standing on concrete. The contract audited clean and your wager evaporated on contact. You lost **{amount}**.",
    "**{king}** saw your approach from three blocks away on the mempool. Your timing was off, your gas was too low, and your conviction was misplaced. You lost **{amount}**.",
    "Amateur hour. **{king}** had 2FA enabled, a hardware wallet, and apparently eyes on the mempool at all times. You lost **{amount}**.",
    "The throne is defended by someone who's been rugged before and learned from it. Your play was textbook. Their defense was not. You lost **{amount}**.",
    "You fumbled the rug and **{king}** watched it happen in slow motion. The defense streak grows. Your wallet shrinks. You lost **{amount}**.",
    "Your social engineering script was good. **{king}**'s immunity to social engineering was better. You lost **{amount}**.",
    "The rug was bolted to the floor, the floor was on bedrock, and **{king}** was holding the deed. You lost **{amount}**.",
    "You called the play too early. **{king}** had the defense streak, the defense bonus, and apparently the defense mindset. You lost **{amount}**.",
    "Rug pull reverted. **{king}**'s contract had a re-entrancy guard you didn't check for. Due diligence is expensive. So is skipping it. You lost **{amount}**.",
    "The universe said no. **{king}** remains on the throne. Your wager fed the bounty pool for whoever comes next. You lost **{amount}**.",
]

_RUGPULL_WIN_FLAVORS: list[str] = [
    "**{king}** trusted the contract. You found the exploit. The throne transfer is complete and irreversible. You paid **{amount}** for the crown.",
    "You timed the wager correctly and executed when **{king}** was distracted. Long live the new King of Rugs. Cost of the crown: **{amount}**.",
    "They said the throne was unassailable. They hadn't accounted for you. **{king}** is dethroned and you are coronated. Price paid: **{amount}**.",
    "**{king}**'s defense streak ends tonight. Your conviction was correct. The bounty pool is yours. The throne is yours. You paid **{amount}** to make it happen.",
    "The rug has been pulled on the rug puller. **{king}** is experiencing the very thing they built their kingdom on. You paid **{amount}** to deliver the lesson.",
]


# ── King command embed text ───────────────────────────────────────────────────

def _rugpull_mechanic_blurb() -> str:
    """Return a concise description of the rugpull mechanic for the monarch embed."""
    return (
        f"Challengers wager a % of their balance (Low 3% / Med 15% / High 30%) for a "
        f"chance to steal the throne. If they **fail**, the monarch keeps `tax_rate%` of the "
        f"wager and the rest feeds the bounty pool. If they **win**, they claim the throne "
        f"*and* the entire bounty pool. Each defense builds a streak that reduces all "
        f"challengers' odds by {Config.RUGPULL_DEFENSE_BONUS*100:.1f}%/streak (cap 15%). "
        f"Other players can sabotage the monarch's defense by contributing to the sabotage "
        f"pool, which decays the streak bonus. The monarch earns passive bonuses to `,work` "
        f"and `,ape` that grow the longer they hold the throne (max at {Config.RUGPULL_PERK_HOURS}h). "
        f"While the throne is held, every challenger pays {Config.RUGPULL_CROWN_DISCOUNT*100:.0f}% "
        f"less by default, with the discount growing up to {Config.RUGPULL_CROWN_MAX_DISCOUNT*100:.0f}% "
        f"after {Config.RUGPULL_CROWN_DISCOUNT_HOURS}h on the throne. The monarch can also spend "
        f"USD with `,rugdefend <amt>` to buy a temporary active-defense bonus."
    )


# ── Cog ──────────────────────────────────────────────────────────────────────

class Rugpull(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        register_interval("rugpull_integrity", 300)
        self.king_integrity_tick.start()
        self._startup_cleanup.start()

    async def cog_load(self) -> None:
        pass

    def cog_unload(self) -> None:
        self.king_integrity_tick.cancel()
        self._startup_cleanup.cancel()

    @tasks.loop(count=1)
    async def _startup_cleanup(self) -> None:
        """On startup, immediately strip every monarch role (King + Queen of Rugs)
        from anyone who isn't the DB-recorded monarch. Only the member whose
        user_id matches the DB record keeps the role; everyone else is stripped.
        The integrity heartbeat handles ongoing enforcement; this fixes state on redeploy."""
        monarch_ids = set(monarch_role_ids())
        if not monarch_ids:
            return
        for guild in self.bot.guilds:
            roles = [guild.get_role(rid) for rid in monarch_ids]
            roles = [r for r in roles if r]
            if not roles:
                continue
            holders = [m for m in guild.members if any(r in m.roles for r in roles)]
            if not holders:
                continue
            try:
                king = await self.bot.db.fetch_one(
                    "SELECT user_id FROM rugpull_king WHERE guild_id=$1", guild.id
                )
                king_id = king["user_id"] if king else None
            except Exception:
                king_id = None
            if len(holders) == 1 and holders[0].id == king_id:
                continue
            for member in holders:
                if member.id != king_id:
                    for r in roles:
                        if r in member.roles:
                            try:
                                await member.remove_roles(r, reason="Startup: removed duplicate monarch role")
                            except Exception:
                                pass

    @_startup_cleanup.before_loop
    async def _before_startup_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    async def _do_startup_cleanup(self) -> None:
        """Manual version of the startup cleanup, kept for callers outside the
        task system. Same semantics: only the DB-recorded monarch keeps any
        monarch role; everyone else is stripped of both King and Queen roles."""
        await self.bot.wait_until_ready()
        monarch_ids = set(monarch_role_ids())
        if not monarch_ids:
            return
        for guild in self.bot.guilds:
            roles = [guild.get_role(rid) for rid in monarch_ids]
            roles = [r for r in roles if r]
            if not roles:
                continue
            holders = [m for m in guild.members if any(r in m.roles for r in roles)]
            if not holders:
                continue
            try:
                king = await self.bot.db.fetch_one(
                    "SELECT user_id FROM rugpull_king WHERE guild_id=$1", guild.id
                )
                king_id = king["user_id"] if king else None
            except Exception:
                king_id = None
            if len(holders) == 1 and holders[0].id == king_id:
                continue
            for member in holders:
                if member.id != king_id:
                    for r in roles:
                        if r in member.roles:
                            try:
                                await member.remove_roles(r, reason="Startup: removed duplicate monarch role")
                            except Exception:
                                pass

    async def cog_check(self, ctx) -> bool:
        # unlockrug must always be reachable even when the module is disabled
        if ctx.command and ctx.command.name == "unlockrug":
            return True
        if ctx.guild and not await module_allowed(ctx, "rugpull"):
            raise commands.CheckFailure(
                "The **King of Rugs** game is currently disabled on this server. "
                "An admin must use `,unlockrug` to re-enable it."
            )
        return True

    @tasks.loop(seconds=300)
    async def king_integrity_tick(self) -> None:
        """Every 5 minutes: ensure exactly 0 or 1 member holds *any* monarch
        role (King OR Queen of Rugs -- they're mutually exclusive). If multiple
        members hold a monarch role, disable the rugpull module for that guild
        and strip extras until an admin manually unlocks it with ,unlockrug."""
        monarch_ids = set(monarch_role_ids())
        if not monarch_ids:
            pulse("rugpull_integrity")
            return
        for guild in self.bot.guilds:
            roles = [guild.get_role(rid) for rid in monarch_ids]
            roles = [r for r in roles if r]
            if not roles:
                continue

            holders = [m for m in guild.members if any(r in m.roles for r in roles)]
            if len(holders) <= 1:
                continue

            # Multiple monarchs detected -- look up who the DB says is the real one
            king_row = await self.bot.db.fetch_one(
                "SELECT user_id FROM rugpull_king WHERE guild_id=$1", guild.id
            )
            real_king_id = king_row["user_id"] if king_row else None

            # Remove any monarch role from everyone who isn't the DB monarch
            for member in holders:
                if member.id != real_king_id:
                    for r in roles:
                        if r in member.roles:
                            try:
                                await member.remove_roles(r, reason="Rugpull integrity: duplicate monarch removed")
                            except Exception:
                                pass

            # Disable the rugpull module for this guild until admin unlocks
            await self.bot.db.execute(
                "UPDATE guild_settings SET module_rugpull = FALSE WHERE guild_id = $1",
                guild.id,
            )

            # Alert in the configured error channel if available
            try:
                settings = await self.bot.db.get_guild_settings(guild.id)
                err_ch_id = settings.get("error_channel")
                if err_ch_id:
                    ch = guild.get_channel(err_ch_id)
                    if ch:
                        await ch.send(
                            "⚠️ **King/Queen of Rugs  -  integrity violation detected!**\n"
                            f"Multiple members held a monarch role simultaneously. "
                            f"Extra roles have been stripped and the rugpull game has been **disabled**.\n"
                            f"Use `,unlockrug` to re-enable it after investigating."
                        )
            except Exception:
                pass

        pulse("rugpull_integrity")

    @king_integrity_tick.before_loop
    async def before_integrity_tick(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="unlockrug", with_app_command=False)
    @guild_only
    async def unlockrug(self, ctx: DiscoContext) -> None:
        """Re-enable the King of Rugs game after it was locked due to a role conflict.
        Requires Manage Server permission."""
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.reply_error("You need **Manage Server** permission to unlock the rugpull game.")
            return

        await self.bot.db.execute(
            "UPDATE guild_settings SET module_rugpull = TRUE WHERE guild_id = $1",
            ctx.guild_id,
        )
        embed = (
            card("👑 King of Rugs  -  Unlocked", color=C_SUCCESS)
            .description(
                "The King of Rugs game has been **re-enabled**.\n\n"
                "The role conflict has been resolved  -  players can challenge for the throne again."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="rugpull", aliases=["rug"], with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(Config.RUGPULL_COOLDOWN)
    async def rugpull(self, ctx: DiscoContext) -> None:
        """Attempt to claim the King of Rugs throne!
        Choose a bet tier to try your luck. Higher cost = higher chance.
        The current king's defense streak reduces your success chance.
        If you win, you collect the bounty pool and the throne."""
        uid, gid = ctx.author.id, ctx.guild_id
        user = await ctx.db.get_user(uid, gid)
        wallet = user.h("wallet")
        bank   = user.h("bank")
        liquid = wallet + bank
        nw = await compute_net_worth(uid, gid, ctx.db)
        net_worth = nw.total

        king = await _get_king(ctx.db, gid)
        king_id = king["user_id"] if king else None
        rug_earnings = king.h("vault_amount") if king else 0.0
        bounty_pool = king.h("bounty_pool") if king else 0.0
        defense_bonus = _compute_defense_bonus(king) if king else 0.0
        crown_discount = _compute_crown_discount(king)

        # Can't rugpull yourself
        if king_id == uid:
            self.rugpull.reset_cooldown(ctx)
            king_gender = await resolve_gender(ctx.db, ctx.author, gid)
            await ctx.reply_error(
                f"You're already the {monarch_title(king_gender)}! Defend your throne."
            )
            return

        # Get monarch display info + gender (drives King vs Queen title in flavor text)
        king_member = ctx.guild.get_member(king_id) if king_id else None
        king_name = king_member.display_name if king_member else ("Nobody" if not king_id else f"User {king_id}")
        king_gender: str | None = None
        if king_member:
            king_gender = await resolve_gender(ctx.db, king_member, gid)
        king_title = monarch_title(king_gender) if king_gender else "King of Rugs"

        # Job-level cost multiplier: lower levels pay dramatically less, higher levels pay more.
        # This prevents whales from trivially farming the throne and weights the mechanic
        # towards players earlier in the ladder.
        job = await ctx.db.get_user_job(uid, gid)
        job_id = job.get("job_id", "HOMELESS") if job else "HOMELESS"
        job_order = Config.JOB_ORDER
        level_idx = job_order.index(job_id) if job_id in job_order else 0
        level_frac = level_idx / max(len(job_order) - 1, 1)  # 0.0 → 1.0
        # Range: 0.4× (HOMELESS) to 2.0× (EXPLOITER)
        job_cost_mult = 0.4 + level_frac * 1.6

        cost_mult = job_cost_mult

        # Calculate costs and effective chances for each tier  -  scaled by job level + GDP.
        # Cost scales with net worth (wallet + bank + all holdings) so players can't hide
        # assets in staking/crypto to reduce their wager. Payment still comes from wallet.
        # Crown discount: when a monarch is on the throne, every tier costs 50% less,
        # scaling up to RUGPULL_CROWN_MAX_DISCOUNT the longer they've held the crown.
        tiers = Config.RUGPULL_TIERS
        tier_info = {}
        low_min_cost = None
        discount_mult = max(0.0, 1.0 - crown_discount)
        for tier_name, tier_cfg in tiers.items():
            effective_pct = tier_cfg["cost_pct"] * cost_mult
            effective_min = round(to_human(int(tier_cfg["min_cost"])) * cost_mult * discount_mult, 2)
            base_cost = max(
                to_human(int(tier_cfg["min_cost"])) * cost_mult,
                net_worth * effective_pct,
            )
            cost = round(base_cost * discount_mult, 2)
            base_chance = tier_cfg["success"]
            effective_chance = max(0.01, base_chance - defense_bonus)
            tier_info[tier_name] = (cost, base_chance, effective_chance)
            if tier_name == "low":
                low_min_cost = effective_min

        low_cost, _, low_eff = tier_info["low"]
        med_cost, _, med_eff = tier_info["medium"]
        high_cost, _, high_eff = tier_info["high"]

        # Check if user can afford any tier -- wager paid from wallet+bank combined
        if liquid < (low_min_cost or 0):
            self.rugpull.reset_cooldown(ctx)
            await ctx.reply_error(
                f"You need at least **${low_min_cost:,.0f}** liquid (wallet + bank) to attempt a rugpull.\n"
                f"Wallet: **{fmt_usd(wallet)}**  |  Bank: **{fmt_usd(bank)}**  |  Net worth: **{fmt_usd(net_worth)}**"
            )
            return

        # Build description
        if king_id:
            target_line = f"**Target:** {king_name} ({king_title}, Rug Earnings: {fmt_usd(rug_earnings)})"
        else:
            target_line = "**Target:** The throne is empty - claim it!"

        defense_line = ""
        if defense_bonus > 0:
            streak = int(king.get("defense_streak", 0)) if king else 0
            defense_line = f"**Monarch's Defense:** -{defense_bonus*100:.1f}% (streak: {streak})\n"

        bounty_line = f"**Bounty Pool:** {fmt_usd(bounty_pool)}\n" if bounty_pool > 0 else ""

        discount_line = ""
        if crown_discount > 0:
            discount_line = (
                f"**Crown Discount:** -{crown_discount*100:.0f}% cost "
                f"(grows the longer {king_name} holds the crown)\n"
            )

        desc = (
            f"👑 **{king_title}** - Rugpull\n"
            f"Choose your wager to attempt seizing the throne!\n\n"
            f"**Liquid:** {fmt_usd(liquid)} (wallet + bank)  |  **Net Worth:** {fmt_usd(net_worth)}\n"
            f"{target_line}\n"
            f"{bounty_line}"
            f"{discount_line}"
            f"{defense_line}\n"
            f"**Effective Success Chances:**\n"
            f"Low: {low_eff*100:.1f}% | Medium: {med_eff*100:.1f}% | High: {high_eff*100:.1f}%\n\n"
            f"*The throne grants scaling work/ape bonuses (up to "
            f"+{Config.RUGPULL_MAX_WORK_BONUS*100:.0f}% work, "
            f"+{Config.RUGPULL_MAX_APE_BONUS*100:.0f}% ape after {Config.RUGPULL_PERK_HOURS}h).*\n"
            f"*If you fail, your wager is split by the monarch's tax rate.*\n"
            f"*Use `,rugbounty <amt>` to add to the bounty pool.*"
        )

        class TierButton(discord.ui.Button):
            def __init__(self_btn, tier_name: str, cost: float,
                         base_chance: float, eff_chance: float) -> None:
                affordable = liquid >= cost
                label = f"${cost:,.0f} ({eff_chance*100:.1f}%)"
                super().__init__(
                    label=label,
                    style=discord.ButtonStyle.danger if affordable else discord.ButtonStyle.secondary,
                    disabled=not affordable,
                    custom_id=f"rugpull_{tier_name}",
                )
                self_btn.tier_name = tier_name
                self_btn.cost = cost
                self_btn.base_chance = base_chance
                self_btn.eff_chance = eff_chance

            async def callback(self_btn, interaction: discord.Interaction) -> None:
                if interaction.user.id != uid:
                    await interaction.response.send_message("Not your rugpull attempt.", ephemeral=True)
                    return

                view.stop()
                for item in view.children:
                    item.disabled = True  # type: ignore[attr-defined]

                # Defer immediately so the 3-second interaction window
                # does not expire while we run DB/AI/role operations.
                await interaction.response.defer()

                # Re-check balance (wallet + bank combined)
                from core.framework.scale import to_raw as _to_raw
                user_check = await ctx.db.get_user(uid, gid)
                cur_wallet_h = user_check.h("wallet")
                cur_bank_h   = user_check.h("bank")
                cur_liquid_h = cur_wallet_h + cur_bank_h
                if cur_liquid_h < self_btn.cost:
                    await interaction.edit_original_response(
                        embed=card("🪤 Insufficient Funds",
                                   description="You no longer have enough liquid funds.",
                                   color=C_ERROR).build(),
                        view=None,
                    )
                    return

                # Re-read king to get latest defense/bounty state
                current_king = await _get_king(ctx.db, gid)
                current_king_id = current_king["user_id"] if current_king else None
                current_bounty_h = current_king.h("bounty_pool") if current_king else 0.0
                current_def = _compute_defense_bonus(current_king) if current_king else 0.0
                final_chance = max(0.01, self_btn.base_chance - current_def)

                # Atomically deduct cost from wallet+bank combined
                cost_raw = _to_raw(self_btn.cost)
                await ctx.db.deduct_liquid(uid, gid, cost_raw)

                # Roll the dice against live defense
                won = random.random() < final_chance

                # Log attempt
                await _log_history(ctx.db, gid, uid, self_btn.tier_name,
                                    cost_raw, won, current_king_id)

                if won:
                    old_king_name_disp = king_name  # from outer scope (pre-click snapshot)

                    # Record dethroned king's hold time
                    hold_secs = 0
                    if current_king_id and current_king_id != uid:
                        _crowned_raw = current_king.get("crowned_at")
                        crowned_ts = (_crowned_raw.timestamp()
                                      if hasattr(_crowned_raw, "timestamp")
                                      else float(_crowned_raw or 0))
                        hold_secs = int(time.time() - crowned_ts) if crowned_ts > 0 else 0
                        await _update_stats(
                            ctx.db, current_king_id, gid,
                            total_hold_seconds=hold_secs,
                            longest_hold_secs=hold_secs,
                            last_dethroned_at=datetime.datetime.now(datetime.timezone.utc),
                        )

                    # Crown the new monarch (resets streak, tax, sabotage, bounty)
                    await _set_king(ctx.db, gid, uid, 0.0)
                    await _update_stats(
                        ctx.db, uid, gid,
                        wins=1, total_wagered=cost_raw,
                        last_crowned_at=datetime.datetime.now(datetime.timezone.utc),
                    )

                    # Resolve the winner's gender to decide King vs Queen role
                    new_gender = await resolve_gender(ctx.db, ctx.author, gid)

                    # Transfer Discord role (and send DMs)
                    await _transfer_role(
                        ctx.guild, current_king_id, uid, new_gender,
                        wager=self_btn.cost,
                        bounty_collected=current_bounty_h,
                        hold_secs=hold_secs,
                        king_earnings=current_king.h("vault_amount") if current_king else 0.0,
                    )

                    # Pay out bounty to the winner
                    bounty_line_txt = ""
                    if current_bounty_h > 0:
                        await ctx.db.update_wallet(uid, gid, _to_raw(current_bounty_h))
                        bounty_line_txt = f"\n**Bounty collected:** {fmt_usd(current_bounty_h)} 💰"

                    # Pick flavor text: use "the void" when throne was empty
                    king_label = old_king_name_disp if current_king_id else "the void"
                    win_flavor = random.choice(_RUGPULL_WIN_FLAVORS).format(
                        king=king_label, amount=fmt_usd(self_btn.cost)
                    )

                    new_title = monarch_title(new_gender)

                    # Try social AI flavor with bystander players
                    try:
                        _rug_ai = await ctx.db.get_ai_flags(gid)
                        if _rug_ai["flavor"] and Config.OPENROUTER_API_KEY:
                            _rug_others = await get_random_active_players(
                                ctx.guild, ctx.db, exclude_user_id=uid, count=2,
                            )
                            if _rug_others:
                                _others_str = ", ".join(f"@{n}" for n in _rug_others)
                                _rug_social = await ai_complete(
                                    [
                                        {"role": "system", "content":
                                            f"Write a 1-2 sentence story about @{ctx.author.display_name} successfully "
                                            f"rugging {king_label} and claiming the {new_title} throne. "
                                            f"Mention at least one of these bystanders: {_others_str}  -  "
                                            f"maybe they're shocked, cheering, or plotting revenge. "
                                            f"Include {{amount}} once for the wager. Max 40 words. No quotes."},
                                        {"role": "user", "content": "Generate."},
                                    ],
                                    max_tokens=80,
                                    temperature=1.1,
                                )
                                if _rug_social and "{amount}" in _rug_social:
                                    win_flavor = _rug_social.replace("{amount}", fmt_usd(self_btn.cost))
                    except Exception:
                        pass

                    # Log rugpull win as server event
                    try:
                        await ctx.db.log_server_event(
                            gid, ctx.channel.id, uid,
                            "rugpull_win",
                            f"{ctx.author.display_name} dethroned {king_label} as {new_title} (wager: {fmt_usd(self_btn.cost)})",
                            float(self_btn.cost),
                            {"command": "rugpull", "dethroned": king_label, "title": new_title},
                        )
                        mark_hot_channel(gid, ctx.channel.id)
                    except Exception:
                        pass

                    result = (
                        card(f"👑 RUGPULL SUCCESSFUL  -  HAIL THE NEW {new_title.upper()}!", color=C_GOLD)
                        .description(
                            f"**{ctx.author.display_name}** has claimed the throne!"
                            f"{bounty_line_txt}\n\n"
                            f"{win_flavor}\n\n"
                            f"*Bonuses start at +{Config.RUGPULL_WORK_BONUS*100:.0f}% work, "
                            f"+{Config.RUGPULL_APE_BONUS*100:.0f}% ape and scale up over "
                            f"{Config.RUGPULL_PERK_HOURS}h.*"
                        )
                        .build()
                    )
                else:
                    # Failed - apply tax split, pay king, grow bounty pool
                    king_take_raw, _ = await _apply_wager_loss(ctx.db, gid, cost_raw)
                    if current_king_id:
                        await ctx.db.update_wallet(current_king_id, gid, int(king_take_raw))
                    new_streak = await _increment_defense(ctx.db, gid)
                    await _update_stats(ctx.db, uid, gid, losses=1, total_wagered=cost_raw)
                    if current_king_id:
                        await _update_stats(ctx.db, current_king_id, gid, defenses=1)

                    fail_flavor = random.choice(_RUGPULL_FAIL_FLAVORS).format(
                        king=king_name, amount=fmt_usd(self_btn.cost)
                    )

                    # Try social AI flavor with bystanders
                    try:
                        _rug_ai = await ctx.db.get_ai_flags(gid)
                        if _rug_ai["flavor"] and Config.OPENROUTER_API_KEY:
                            _rug_others = await get_random_active_players(
                                ctx.guild, ctx.db, exclude_user_id=uid, count=2,
                            )
                            if _rug_others:
                                _others_str = ", ".join(f"@{n}" for n in _rug_others)
                                _rug_social = await ai_complete(
                                    [
                                        {"role": "system", "content":
                                            f"Write a 1-2 sentence story about @{ctx.author.display_name} failing to "
                                            f"rug {king_name}. The {king_title}'s defense held. "
                                            f"Mention at least one of: {_others_str}  -  "
                                            f"maybe they're laughing, relieved, or taking notes. "
                                            f"Include {{amount}} once for the wager lost. Max 40 words. No quotes."},
                                        {"role": "user", "content": "Generate."},
                                    ],
                                    max_tokens=80,
                                    temperature=1.1,
                                )
                                if _rug_social and "{amount}" in _rug_social:
                                    fail_flavor = _rug_social.replace("{amount}", fmt_usd(self_btn.cost))
                    except Exception:
                        pass

                    # Log rugpull fail as server event (only for high-wager fails)
                    if self_btn.cost >= 500:
                        try:
                            await ctx.db.log_server_event(
                                gid, ctx.channel.id, uid,
                                "rugpull_fail",
                                f"{ctx.author.display_name} failed to rug {king_name} - lost {fmt_usd(self_btn.cost)}",
                                float(self_btn.cost),
                                {"command": "rugpull", "king": king_name},
                            )
                            mark_hot_channel(gid, ctx.channel.id)
                        except Exception:
                            pass

                    king_take_line = f"{king_title} earned: **{fmt_usd(to_human(round(king_take_raw)))}**"
                    bounty_grow_line = ""
                    streak_line = f"\n*{king_name}'s defense streak: {new_streak}*"

                    result = (
                        card("🪤 Rugpull Failed", color=C_SELL)
                        .description(
                            f"{fail_flavor}\n\n"
                            f"{king_take_line}{bounty_grow_line}."
                            f"{streak_line}"
                        )
                        .build()
                    )

                await interaction.edit_original_response(embed=result, view=None)

                # Autonomous bot reaction to rugpull outcomes
                try:
                    _social_cog = self.bot.get_cog("SocialContext")
                    if _social_cog:
                        _result_msg = await interaction.original_response()
                        _evt = "rugpull_win" if won else "rugpull_fail"
                        asyncio.create_task(_social_cog.react_to_event(ctx.channel, _result_msg, _evt))
                except Exception:
                    pass

        view = discord.ui.View(timeout=30)
        view.message = None  # will be set after send

        for tier_name in ("low", "medium", "high"):
            cost, base_ch, eff_ch = tier_info[tier_name]
            view.add_item(TierButton(tier_name, cost, base_ch, eff_ch))

        async def _on_timeout():
            for item in view.children:
                item.disabled = True  # type: ignore[attr-defined]
            try:
                if view.message is not None:
                    await view.message.edit(view=view)
            except Exception:
                pass

        view.on_timeout = _on_timeout

        embed = card(f"🪤 {king_title} - Rugpull", description=desc, color=C_CRIMSON).build()
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    @commands.hybrid_command(name="king", aliases=["queen", "monarch"], with_app_command=False)
    @guild_only
    async def king(self, ctx: DiscoContext) -> None:
        """View the current King or Queen of Rugs, their throne stats, and active mechanics."""
        gid = ctx.guild_id
        king = await _get_king(ctx.db, gid)
        if not king:
            await ctx.reply(
                embed=card(
                    "👑 King / Queen of Rugs",
                    description="The throne is **empty**. Use `,rugpull` to claim it!",
                    color=C_INFO,
                ).build(),
                mention_author=False,
            )
            return

        king_id = king["user_id"]
        rug_earnings = king.h("vault_amount")
        bounty_pool = king.h("bounty_pool")
        sabotage_pool = king.h("sabotage_pool")
        tax_rate = float(king.get("tax_rate", 1.0))
        defense_streak = int(king.get("defense_streak", 0))
        defense_bonus = _compute_defense_bonus(king)
        crown_discount = _compute_crown_discount(king)

        _crowned_raw = king.get("crowned_at")
        crowned_ts = _crowned_raw.timestamp() if hasattr(_crowned_raw, "timestamp") else float(_crowned_raw or 0)
        hold_secs = int(time.time() - crowned_ts) if crowned_ts > 0 else 0
        hours = hold_secs // 3600
        mins = (hold_secs % 3600) // 60

        member = ctx.guild.get_member(king_id)
        name = member.display_name if member else f"User {king_id}"
        gender = await resolve_gender(ctx.db, member, gid) if member else "male"
        title = monarch_title(gender)

        stats = await _get_stats(ctx.db, king_id, gid)
        work_bonus, ape_bonus = _compute_reign_perks(king)

        # Perk progress
        perk_secs = Config.RUGPULL_PERK_HOURS * 3600
        perk_pct = min(100, int(hold_secs / perk_secs * 100)) if perk_secs > 0 else 100

        crowned_ts_int = int(crowned_ts) if crowned_ts > 0 else 0
        crowned_discord = fmt_ts(crowned_ts_int) if crowned_ts_int else "Unknown"
        throne_duration = fmt_ts(crowned_ts_int) if crowned_ts_int else f"{hours}h {mins}m"

        # Active paid-defense window
        active_until = king.get("active_defense_until")
        active_until_ts = (
            active_until.timestamp() if hasattr(active_until, "timestamp")
            else float(active_until or 0)
        )
        active_line = ""
        if active_until_ts > time.time():
            remaining = int(active_until_ts - time.time())
            h, m = remaining // 3600, (remaining % 3600) // 60
            active_bonus = float(king.get("active_defense_bonus", 0) or 0)
            active_line = (
                f"**Active Defense:** -{active_bonus*100:.1f}% to challengers "
                f"(expires in {h}h {m}m)\n"
            )

        desc = (
            f"**{name}** 👑  -  *{title}*\n\n"
            f"**Rug Earnings This Reign:** {fmt_usd(rug_earnings)} *(updated each challenge)*\n"
            f"**Crowned:** {crowned_discord}\n"
            f"**Time on Throne:** {throne_duration}\n"
            f"**Active Bonuses:** +{work_bonus*100:.1f}% work, +{ape_bonus*100:.1f}% ape "
            f"*(perk progress: {perk_pct}%)*\n\n"
            f"**Defense Streak:** {defense_streak} "
            f"(-{defense_bonus*100:.1f}% to all challengers)\n"
            f"{active_line}"
            f"**Sabotage Pool:** {fmt_usd(sabotage_pool)}\n"
            f"**Bounty Pool:** {fmt_usd(bounty_pool)}\n"
            f"**Tax Rate:** {tax_rate*100:.0f}%\n"
            f"**Crown Discount:** -{crown_discount*100:.0f}% off challenger costs "
            f"(grows with reign)\n\n"
            f"**All-Time Wins:** {stats['wins']} | **Defenses:** {stats['defenses']}\n"
            f"**Total Time on Throne:** {int(stats['total_hold_seconds'] + hold_secs) // 3600}h\n"
            f"**Longest Reign:** {int(max(stats['longest_hold_secs'], hold_secs)) // 3600}h\n\n"
            f"**⚙️ How Rugpull Works:**\n"
            f"{_rugpull_mechanic_blurb()}\n\n"
            f"*`,sabotage <amt>` to weaken the defense · `,taxdecree <pct>` to set tax rate · "
            f"`,rugdefend <amt>` (monarch only) to buy an active defense*"
        )

        embed = card(f"👑 {title}", description=desc, color=C_GOLD).build()
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="rugstats", aliases=["rugstat"], with_app_command=False)
    @guild_only
    async def rugstats(self, ctx: DiscoContext, user: discord.Member | None = None) -> None:
        """View rugpull stats for yourself or another user."""
        target = user or ctx.author
        gid = ctx.guild_id
        stats = await _get_stats(ctx.db, target.id, gid)

        total_secs = int(stats["total_hold_seconds"])
        longest_secs = int(stats["longest_hold_secs"])

        def _fmt_time(secs: int) -> str:
            if secs < 60:
                return f"{secs}s"
            if secs < 3600:
                return f"{secs // 60}m {secs % 60}s"
            return f"{secs // 3600}h {(secs % 3600) // 60}m"

        last_crowned = stats.get("last_crowned_at")
        last_dethroned = stats.get("last_dethroned_at")

        crown_str = fmt_ts(last_crowned, "%Y-%m-%d") if last_crowned else "Never"
        dethrone_str = fmt_ts(last_dethroned, "%Y-%m-%d") if last_dethroned else "Never"

        wins = int(stats["wins"])
        losses = int(stats["losses"])
        total = wins + losses
        winrate = f"{wins/total*100:.1f}%" if total > 0 else "N/A"

        desc = (
            f"**{target.display_name}**  -  Rugpull Stats\n\n"
            f"**Wins:** {wins} | **Losses:** {losses} | **Win Rate:** {winrate}\n"
            f"**Total Wagered:** {fmt_usd(stats.h('total_wagered'))}\n"
            f"**Defenses:** {int(stats['defenses'])}\n"
            f"**Sabotages Placed:** {int(stats['sabotages_done'])}\n"
            f"**Bounties Placed:** {fmt_usd(stats.h('bounties_placed'))}\n\n"
            f"**Total Time on Throne:** {_fmt_time(total_secs)}\n"
            f"**Longest Reign:** {_fmt_time(longest_secs)}\n"
            f"**Last Crowned:** {crown_str}\n"
            f"**Last Dethroned:** {dethrone_str}"
        )

        embed = card("🪤 Rugpull Stats", description=desc, color=C_INFO).build()
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="rugbounty", aliases=["bountyrug"], with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def bounty(self, ctx: DiscoContext, amount: str) -> None:
        """Place a bounty on the current King of Rugs.
        The next challenger who wins will collect the entire bounty pool."""
        uid, gid = ctx.author.id, ctx.guild_id

        king = await _get_king(ctx.db, gid)
        if not king:
            await ctx.reply_error("There is no king to place a bounty on. The throne is empty.")
            return
        if king["user_id"] == uid:
            await ctx.reply_error("You can't place a bounty on yourself.")
            return

        parsed = _parse_dollar_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply_error("Invalid amount. Usage: `,rugbounty 500` or `,rugbounty 2.5k`")
            return
        amt = round(parsed, 2)

        min_bounty_h = to_human(int(Config.RUGPULL_MIN_BOUNTY))
        if amt < min_bounty_h:
            await ctx.reply_error(
                f"Minimum bounty is **{fmt_usd(min_bounty_h)}**."
            )
            return

        from core.framework.scale import to_raw as _to_raw
        user = await ctx.db.get_user(uid, gid)
        wallet_h = user.h("wallet")
        if wallet_h < amt:
            await ctx.reply_error(
                f"Insufficient funds. You have **{fmt_usd(wallet_h)}**, need **{fmt_usd(amt)}**."
            )
            return

        amt_raw = _to_raw(amt)
        await ctx.db.update_wallet(uid, gid, -amt_raw)
        new_bounty_raw = int(king.get("bounty_pool", 0) or 0) + amt_raw
        await ctx.db.execute(
            "UPDATE rugpull_king SET bounty_pool=$1 WHERE guild_id=$2",
            new_bounty_raw, gid,
        )
        await _update_stats(ctx.db, uid, gid, bounties_placed=amt_raw)

        king_member = ctx.guild.get_member(king["user_id"])
        king_name = king_member.display_name if king_member else f"User {king['user_id']}"

        embed = (
            card("💰 Bounty Placed!", color=C_AMBER)
            .description(
                f"**{ctx.author.display_name}** placed a **{fmt_usd(amt)}** bounty on **{king_name}**!\n\n"
                f"**Total Bounty Pool:** {fmt_usd(to_human(new_bounty_raw))}\n\n"
                f"*The next challenger who successfully rugpulls the king will collect this bounty.*"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="sabotage", aliases=["rugsabotage"], with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(60)
    async def sabotage(self, ctx: DiscoContext, amount: str) -> None:
        """Contribute to the sabotage pool against the current king.
        Each dollar of sabotage reduces the king's defense streak bonus."""
        uid, gid = ctx.author.id, ctx.guild_id

        king = await _get_king(ctx.db, gid)
        if not king:
            await ctx.reply_error("There is no king to sabotage. The throne is empty.")
            return
        if king["user_id"] == uid:
            await ctx.reply_error("You can't sabotage yourself.")
            return

        parsed = _parse_dollar_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply_error("Invalid amount. Usage: `,sabotage 200`")
            return
        amt = round(parsed, 2)
        min_sabotage = 10.0
        if amt < min_sabotage:
            await ctx.reply_error(f"Minimum sabotage amount is **{fmt_usd(min_sabotage)}**.")
            return

        from core.framework.scale import to_raw as _to_raw
        user = await ctx.db.get_user(uid, gid)
        wallet_h = user.h("wallet")
        if wallet_h < amt:
            await ctx.reply_error(
                f"Insufficient funds. You have **{fmt_usd(wallet_h)}**, need **{fmt_usd(amt)}**."
            )
            return

        amt_raw = _to_raw(amt)
        old_defense = _compute_defense_bonus(king)
        await ctx.db.update_wallet(uid, gid, -amt_raw)
        new_sabotage_raw = int(king.get("sabotage_pool", 0) or 0) + amt_raw
        await ctx.db.execute(
            "UPDATE rugpull_king SET sabotage_pool=$1 WHERE guild_id=$2",
            new_sabotage_raw, gid,
        )
        await _update_stats(ctx.db, uid, gid, sabotages_done=1)

        # Show new defense after sabotage
        king_updated = await _get_king(ctx.db, gid)
        new_defense = _compute_defense_bonus(king_updated) if king_updated else 0.0

        king_member = ctx.guild.get_member(king["user_id"])
        king_name = king_member.display_name if king_member else f"User {king['user_id']}"

        embed = (
            card("🔪 Sabotage!", color=C_SELL)
            .description(
                f"**{ctx.author.display_name}** contributed **{fmt_usd(amt)}** to the sabotage pool "
                f"against **{king_name}**!\n\n"
                f"**King's Defense:** {old_defense*100:.1f}% → {new_defense*100:.1f}%\n"
                f"**Total Sabotage Pool:** {fmt_usd(to_human(new_sabotage_raw))}\n\n"
                f"*Sabotage reduces the king's defense bonus on all rugpull attempts.*"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="taxdecree", aliases=["settax", "rugtax"], with_app_command=False)
    @guild_only
    @ensure_registered
    async def taxdecree(self, ctx: DiscoContext, rate: float) -> None:
        """Set your tax rate as King of Rugs (king-only command).
        Rate is a percentage between {min}% and 100%. The remainder of each
        failed wager feeds the bounty pool."""
        uid, gid = ctx.author.id, ctx.guild_id
        king = await _get_king(ctx.db, gid)

        if not king or king["user_id"] != uid:
            await ctx.reply_error("Only the current King of Rugs can issue a tax decree.")
            return

        min_pct = Config.RUGPULL_MIN_TAX * 100  # e.g. 25.0
        if rate < min_pct or rate > 100.0:
            await ctx.reply_error(
                f"Tax rate must be between **{min_pct:.0f}%** and **100%**.\n"
                f"Lower tax = more bounty pool growth from failed challengers."
            )
            return

        new_rate = round(rate / 100.0, 4)
        await ctx.db.execute(
            "UPDATE rugpull_king SET tax_rate=$1 WHERE guild_id=$2",
            new_rate, gid,
        )

        bounty_share = (1.0 - new_rate) * 100
        embed = (
            card("📜 Tax Decree Issued!", color=C_SUCCESS)
            .description(
                f"**{ctx.author.display_name}** has decreed a **{rate:.0f}%** tax on all failed challengers.\n\n"
                f"**King keeps:** {rate:.0f}% of each failed wager\n"
                f"**Bounty pool grows by:** {bounty_share:.0f}% of each failed wager\n\n"
                f"*The new rate takes effect immediately.*"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="rugdefend", aliases=["kingdefend", "queendefend", "throne_defend"], with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def rugdefend(self, ctx: DiscoContext, amount: str) -> None:
        """Monarch-only: spend USD on an active defense buff against rugpull attempts.

        Works like the ``,fortify`` security detail in the Eat the Rich game -- the money is
        burned from your wallet and you gain a temporary success-chance debuff
        applied to every challenger. Each dollar buys
        ``RUGPULL_DEFEND_PCT_PER_USD`` defense (capped at ``RUGPULL_DEFEND_MAX_BONUS``).
        Has a cooldown enforced on the DB clock.
        """
        uid, gid = ctx.author.id, ctx.guild_id

        king = await _get_king(ctx.db, gid)
        if not king:
            await ctx.reply_error("There is no throne to defend  -  the crown is empty.")
            return
        if king["user_id"] != uid:
            await ctx.reply_error("Only the current King or Queen of Rugs can spend money on an active defense.")
            return

        parsed = _parse_dollar_amount(amount)
        if parsed is None or parsed <= 0:
            await ctx.reply_error("Invalid amount. Usage: `,rugdefend 500` or `,rugdefend 2.5k`")
            return
        amt = round(parsed, 2)

        min_defend_h = to_human(int(Config.RUGPULL_DEFEND_MIN_USD))
        if amt < min_defend_h:
            await ctx.reply_error(
                f"Minimum active-defense spend is **{fmt_usd(min_defend_h)}**."
            )
            return

        # DB-side cooldown clock (never compare wall time against Postgres ts)
        cooldown_row = await ctx.db.fetch_one(
            """SELECT defense_last_used_at,
                      EXTRACT(EPOCH FROM (NOW() - defense_last_used_at)) AS elapsed
               FROM rugpull_king WHERE guild_id=$1""",
            gid,
        )
        if cooldown_row and cooldown_row.get("defense_last_used_at") is not None:
            elapsed = float(cooldown_row.get("elapsed") or 0)
            if elapsed < Config.RUGPULL_DEFEND_COOLDOWN:
                remaining = int(Config.RUGPULL_DEFEND_COOLDOWN - elapsed)
                await ctx.reply_cooldown(remaining)
                return

        from core.framework.scale import to_raw as _to_raw
        user = await ctx.db.get_user(uid, gid)
        wallet_h = user.h("wallet")
        if wallet_h < amt:
            await ctx.reply_error(
                f"Insufficient funds. You have **{fmt_usd(wallet_h)}**, need **{fmt_usd(amt)}**."
            )
            return

        bonus = min(
            Config.RUGPULL_DEFEND_MAX_BONUS,
            amt * Config.RUGPULL_DEFEND_PCT_PER_USD,
        )
        duration = Config.RUGPULL_DEFEND_DURATION

        amt_raw = _to_raw(amt)
        async with ctx.db.atomic():
            await ctx.db.update_wallet(uid, gid, -amt_raw)
            await ctx.db.execute(
                """UPDATE rugpull_king
                       SET active_defense_until = NOW() + ($1 || ' seconds')::interval,
                           active_defense_bonus = $2,
                           defense_last_used_at = NOW()
                     WHERE guild_id = $3""",
                str(duration), round(bonus, 4), gid,
            )

        gender = await resolve_gender(ctx.db, ctx.author, gid)
        title = monarch_title(gender)
        hours = duration // 3600

        embed = (
            card(f"🛡️ {title}  -  Active Defense Engaged", color=C_SUCCESS)
            .description(
                f"**{ctx.author.display_name}** poured **{fmt_usd(amt)}** into hiring auditors, "
                f"buying multisigs and bribing the mempool.\n\n"
                f"**Active defense:** -{bonus*100:.1f}% to every challenger's success chance\n"
                f"**Duration:** {hours}h"
            )
            .footer(f"Cooldown: {Config.RUGPULL_DEFEND_COOLDOWN // 3600}h from activation")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="ruggender", aliases=["rugsex", "rugidentity"], with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def ruggender(self, ctx: DiscoContext, gender: str | None = None) -> None:
        """Set your gender for the rug minigame so wins grant the right role.

        Use ``,ruggender male`` to lock in the King of Rugs role, or
        ``,ruggender female`` to lock in the Queen of Rugs role on your next win.
        With no argument, shows the bot's current best guess (and whether it
        came from auto-detection or your own manual override).

        This override is sticky -- it beats the auto-detect heuristic and the
        AI fallback. Use ``,ruggender clear`` to delete the override and let
        the bot re-infer next time.
        """
        uid, gid = ctx.author.id, ctx.guild_id

        if gender is None:
            stored = await get_stored_gender(ctx.db, uid, gid)
            if stored is None:
                guessed = await resolve_gender(ctx.db, ctx.author, gid)
                src_note = "*(auto-detected just now from your profile)*"
                gender_val = guessed
            else:
                gender_val = stored["gender"]
                src_note = (
                    "*(manual override  -  you set this yourself)*"
                    if stored["source"] == "manual"
                    else "*(auto-detected from your profile)*"
                )
            title = monarch_title(gender_val)
            embed = (
                card("👑 Rug Identity", color=C_INFO)
                .description(
                    f"**{ctx.author.display_name}** -> **{gender_val}** -> *{title}* role on the next rug win.\n"
                    f"{src_note}\n\n"
                    f"Use `,ruggender male` or `,ruggender female` to lock it in. "
                    f"Use `,ruggender clear` to wipe the override."
                )
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        key = gender.strip().lower()
        aliases = {
            "m": "male", "male": "male", "man": "male", "boy": "male", "king": "male",
            "f": "female", "female": "female", "woman": "female", "girl": "female", "queen": "female",
        }
        if key in ("clear", "reset", "none", "unset"):
            await ctx.db.execute(
                "DELETE FROM rugpull_gender WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
            await ctx.reply_success(
                "Gender override cleared. The bot will re-infer it on your next rug win.",
                title="Rug Identity Reset",
            )
            return

        if key not in aliases:
            await ctx.reply_error_hint(
                "Gender must be `male` or `female`.",
                hint="Use `,ruggender male`, `,ruggender female`, or `,ruggender clear`.",
                command_name="ruggender",
            )
            return

        canonical = aliases[key]
        await set_manual_gender(ctx.db, uid, gid, canonical)
        title = monarch_title(canonical)
        embed = (
            card("👑 Rug Identity Set", color=C_SUCCESS)
            .description(
                f"**{ctx.author.display_name}** is now registered as **{canonical}**.\n"
                f"Your next rug-pull win will grant the **{title}** role.\n\n"
                f"*This override beats the bot's auto-detection.*"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="rughistory", aliases=["rughist"], with_app_command=False)
    @guild_only
    async def rughistory(self, ctx: DiscoContext) -> None:
        """View the 10 most recent rugpull attempts in this server."""
        gid = ctx.guild_id
        rows = await ctx.db.fetch_all(
            """SELECT user_id, tier, wager, won, king_id, created_at
               FROM rugpull_history
               WHERE guild_id=$1
               ORDER BY created_at DESC
               LIMIT 10""",
            gid,
        )
        if not rows:
            await ctx.reply(
                embed=card(
                    "📜 Rugpull History",
                    description="No rugpull attempts yet in this server.",
                    color=C_INFO,
                ).build(),
                mention_author=False,
            )
            return

        lines = []
        for row in rows:
            attacker = ctx.guild.get_member(row["user_id"])
            atk_name = attacker.display_name if attacker else f"User {row['user_id']}"
            target_id = row["king_id"]
            target = ctx.guild.get_member(target_id) if target_id else None
            tgt_name = target.display_name if target else (f"User {target_id}" if target_id else "empty throne")
            outcome = "✅ Won" if row["won"] else "❌ Lost"
    
            date_str = fmt_ts(row["created_at"])
            lines.append(
                f"`{date_str}` **{atk_name}** ({row['tier']}, {fmt_usd(row.h('wager'))}) "
                f"vs **{tgt_name}**  -  {outcome}"
            )

        embed = (
            card("📜 Rugpull History", description="\n".join(lines), color=C_INFO)
            .footer("Last 10 attempts")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Rugpull(bot))
