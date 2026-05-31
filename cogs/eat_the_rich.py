"""
cogs/eat_the_rich.py  -  EatChain: the Layer-2 DeFi class-warfare game.

EatChain is a satirical simulated Layer-2 the ,eat minigame runs on. You
play a predatory validator: scan the mempool, front-run wealthier wallets,
and devour their fortunes. You only ever WANT to punch UP -- a target's
success odds and the gross you steal both scale with how much richer they
are. The poorest active player is fully uneatable.

A successful eat removes a GROSS slice of the target. The tactic button you
pick (Type 1/2/3) splits that gross four ways: your cut, a burn, the
multi-currency salad bowl, and an airdrop to the poorest players. Every win
also mints $EAT -- EatChain's native earn-only token -- and Eat Ladder XP.

The prep -> cook powerup chain super-charges an eat. ,eat prep cases the
joint (intel + bypasses security); ,eat cook cooks the books (uncaps the
steal + redirects the burn into your cut) and unlocks ,eat rich.

$EAT can be staked for passive validator yield, burned for a timed odds
buff, or spent on insurance and audits. Successful eats climb the 100-level
Eat Ladder, unlocking ranks (Mempool Peasant -> Apex Validator), perks and
cosmetic titles.

All EatChain tuning lives in configs/eatchain_config.py; the original theft
engine still reads Config.EAT_* / Config.EAT_TACTICS.

Commands (all under the ,eat group; old top-level names kept as aliases):
    ,eat                   -  snipe a random wealthier wallet (no pinging)
    ,eat snipe             -  same, with a reduced cooldown
    ,eat @target           -  targeted strike: pick a tactic and eat a player
    ,eat bite @target [p]  -  precision strike on one balance pool
    ,eat nibble [@target]  -  quick, tiny, instant low-stakes eat
    ,eat feast             -  Lv100 multi-snipe of the wealthiest wallets
    ,eat rug               -  pull your own liquidity for instant $EAT
    ,eat prep / cook       -  the prep -> cook powerup chain
    ,eat chew              -  digest a recent win for bonus $EAT
    ,eat defend / insurance / audit / burn  -  DeFi defence + utility
    ,eat stake / unstake / bag               -  the $EAT economy
    ,eat salad / rich      -  the salad bowl + the 1% bowl gamble
    ,eat stats / history / lb / rank / help  -  info
    ,fortify / ,eatstats / ,eathistory       -  legacy aliases, still work
"""
from __future__ import annotations

import logging
import random
import time

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only, no_bots, ensure_registered
from core.framework.cooldowns import user_cooldown
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_SUCCESS, C_ERROR, C_INFO, C_AMBER, C_WARNING, C_CRIMSON, C_GOLD,
    C_TEAL, C_PURPLE, fmt_usd, fmt_token, fmt_ts, send_paginated,
)
from configs import eatchain_config as EC
from services.net_worth import compute_net_worth, compute_bulk_net_worth

try:
    from core.framework.heartbeat import pulse as _hb_pulse, register_interval as _hb_register
except Exception:  # heartbeat is optional infrastructure
    def _hb_pulse(_name: str) -> None:  # type: ignore
        pass

    def _hb_register(_name: str, _seconds: float) -> None:  # type: ignore
        pass

log = logging.getLogger(__name__)


# Mentions are fully suppressed on every Eat the Rich message: a victim
# should never get an audible ping just for being on the menu. Embeds never
# ping on their own, but AllowedMentions.none() makes the guarantee explicit.
# DMs to victims are sent silent=True for the same reason.
_SILENT = discord.AllowedMentions.none()

# ,eat bite balance pools and the aliases we accept for each.
_BITE_POOL_ALIASES = {
    "wallet": "wallet", "cash": "wallet", "usd": "wallet", "liquid": "wallet",
    "crypto": "crypto", "cefi": "crypto", "coins": "crypto", "portfolio": "crypto",
    "defi": "defi", "onchain": "defi", "chain": "defi",
    "bank": "bank", "banked": "bank", "savings": "bank",
    "stakes": "stakes", "stake": "stakes", "staked": "stakes", "staking": "stakes",
}
_BITE_POOL_LABEL = {
    "wallet": "wallet", "crypto": "CeFi crypto", "defi": "DeFi wallet",
    "bank": "bank", "stakes": "stakes",
}


# ── Flavor text ────────────────────────────────────────────────────────────

_EAT_WIN_FLAVORS = [
    "{target}'s portfolio was bloated and slow. You took a clean bite.",
    "While {target} sipped champagne, you cleaned out their hot wallet.",
    "The rich never count their change. {target} won't even notice.",
    "You cracked {target}'s vault open like a lobster.",
    "{target} had it coming. The market's guillotine does not discriminate.",
    "Their wealth was never really theirs. You put it back in circulation.",
    "{target}'s yacht money is now everyone's money. No apologies.",
    "You ate well tonight. {target}'s fortune was overripe.",
]

_EAT_FAIL_FLAVORS = [
    "{target}'s lawyers were faster than your fork. You walked away **{penalty}** lighter.",
    "Old money fights dirty. {target} swatted you off and it cost you **{penalty}**.",
    "You reached for the feast and grabbed a fistful of nothing. **{penalty}** gone.",
    "{target}'s accountant smelled you coming. The plan collapsed -- **{penalty}** lost.",
    "The rich stay rich for a reason. {target} bit back. **{penalty}** down the drain.",
    "You bit off more than you could chew. {target} is fine; you are out **{penalty}**.",
]

_FORTIFY_FLAVORS = [
    "**{target}**'s private security detail caught you at the gate. Not tonight.",
    "**{target}** keeps bodyguards on payroll. Your approach never landed.",
    "**{target}**'s panic room sealed before you got close. Nothing for you.",
    "**{target}** lawyered up hard. Every move you made hit a wall.",
]

_BITE_EMPTY_FLAVORS = {
    "wallet": [
        "You sink your teeth into {t}'s wallet... but it's bone dry. Not a "
        "loose dollar in sight. They must have seen you coming.",
        "{t}'s wallet flaps open like an empty lunchbox. Crumbs, maybe.",
    ],
    "crypto": [
        "You crack open {t}'s crypto bags expecting a feast -- they're empty.",
        "{t}'s CeFi portfolio is a ghost town. No coins, no candles, no meal.",
    ],
    "defi": [
        "You jack into {t}'s DeFi wallet and the on-chain balance reads zero.",
        "{t}'s DeFi wallet is swept clean -- not even dust on the contract.",
    ],
    "bank": [
        "You tunnel into {t}'s bank vault and find only echoes.",
        "{t}'s bank balance is a rounding error. The teller laughs you out.",
    ],
}

_PUNCH_DOWN_WARNING = (
    "Trying to eat the poor? How *gauche*. They taste bitter and the optics "
    "are terrible. You can still go through with it -- but your odds are "
    "slashed and a botched job costs a flat **class traitor fee**."
)

_PREP_FLAVORS = [
    "You spread the blueprints across the table and start casing the joint.",
    "Floor plans, guard rotations, blind spots -- you're studying your mark.",
    "You tail your next meal for a while. Soon you'll know their every move.",
]

_COOK_FLAVORS = [
    "Ledgers open, numbers sliding. You start cooking the books.",
    "You grease the right palms and rewrite the paper trail. Cooking.",
    "Off-shore shells, shredded receipts -- the books are going in the oven.",
]

_SALAD_WIN_FLAVORS = [
    "The bowl tips. You bury your face in it and come up with a fortune.",
    "Against every odd, the salad bowl is YOURS. Billions, in one bite.",
    "One in a hundred. You hit it. The bowl empties straight into your lap.",
]

_SALAD_LOSS_FLAVORS = [
    "You lunge at the bowl and miss. A slice of it burns away in the scramble.",
    "Not today. The bowl slips your grip and some of it goes up in smoke.",
    "So close. The bowl stays full -- minus the part you torched trying.",
]


class _TacticSelect(discord.ui.View):
    """The three Type buttons. Each is labelled with its exact stake so the
    player never has to guess what they are risking."""

    _STYLES = {
        "skim": discord.ButtonStyle.secondary,
        "shakedown": discord.ButtonStyle.primary,
        "guillotine": discord.ButtonStyle.danger,
    }

    def __init__(
        self, author_id: int, stakes_h: dict[str, float]
    ) -> None:
        super().__init__(timeout=30)
        self.author_id = author_id
        self.chosen: str | None = None
        for tactic_id, cfg in Config.EAT_TACTICS.items():
            btn = discord.ui.Button(
                label=f"{cfg['label']}  -  Stake {fmt_usd(stakes_h.get(tactic_id, 0.0))}",
                style=self._STYLES.get(tactic_id, discord.ButtonStyle.secondary),
                custom_id=f"eat_tactic_{tactic_id}",
            )
            btn.callback = self._make_callback(tactic_id)
            self.add_item(btn)

    def _make_callback(self, tactic_id: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("Not your meal.", ephemeral=True)
                return
            self.chosen = tactic_id
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=self)
            self.stop()
        return callback

    async def on_timeout(self) -> None:
        self.stop()


class _BowlEatView(discord.ui.View):
    """A persistent 'Eat the Bowl' button under the ,eat salad embed. Anyone
    may click it; it runs the ,eat rich gamble for whoever clicked."""

    def __init__(self, cog: "EatTheRich") -> None:
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="🥗 Eat the Bowl", style=discord.ButtonStyle.danger)
    async def eat_bowl(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer()
        embed = await self.cog._salad_attempt(
            interaction.client.db, interaction.guild_id, interaction.user.id,
            interaction.user.display_name,
        )
        await interaction.followup.send(embed=embed, allowed_mentions=_SILENT)


class _TitleSelectView(discord.ui.View):
    """A select menu under ,eat rank for equipping an unlocked title."""

    def __init__(self, author_id: int, unlocked: list[str], equipped: str) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        options = []
        for tid in unlocked[:25]:
            t = EC.TITLES.get(tid)
            if not t:
                continue
            options.append(discord.SelectOption(
                label=t["name"], value=tid, emoji=t["emoji"],
                description=t["desc"][:100], default=(tid == equipped),
            ))
        self._select = discord.ui.Select(
            placeholder="Equip a title...", options=options,
            min_values=1, max_values=1,
        )
        self._select.callback = self._on_select
        self.add_item(self._select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This isn't your ladder.", ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        tid = self._select.values[0]
        await interaction.client.db.execute(
            "INSERT INTO exploit_stats (user_id, guild_id, eat_title) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, guild_id) DO UPDATE SET eat_title=$3",
            interaction.user.id, interaction.guild_id, tid,
        )
        t = EC.TITLES.get(tid, EC.TITLES["fresh_meat"])
        await interaction.response.send_message(
            f"{t['emoji']} Title equipped: **{t['name']}**.", ephemeral=True,
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            try:
                child.disabled = True
            except Exception:
                pass


class EatTheRich(commands.Cog):
    """EatChain  -  the Layer-2 DeFi class-warfare game. Punch up, never down."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        _hb_register("eatchain_yield", EC.YIELD_TICK_SECONDS)
        self.eatchain_yield.start()

    def cog_unload(self) -> None:
        self.eatchain_yield.cancel()

    # ── Background validator-yield tick ───────────────────────────────────

    @tasks.loop(seconds=EC.YIELD_TICK_SECONDS)
    async def eatchain_yield(self) -> None:
        """Credit passive $EAT staking yield to every EatChain validator.
        The APY scales with the staker's rank tier -- higher validators
        earn fatter block rewards."""
        for guild in list(self.bot.guilds):
            try:
                rows = await self.bot.db.fetch_all(
                    "SELECT user_id, eat_staked, eat_xp FROM exploit_stats "
                    "WHERE guild_id=$1 AND eat_staked > 0",
                    guild.id,
                )
                for r in rows:
                    staked = int(r["eat_staked"])
                    apy = EC.stake_hourly_apy(EC.level_for_xp(float(r["eat_xp"])))
                    payout = int(staked * apy)
                    if payout <= 0:
                        continue
                    try:
                        await self.bot.db.update_wallet_holding(
                            r["user_id"], guild.id,
                            EC.EAT_NETWORK, EC.EAT_SYMBOL, payout,
                        )
                    except Exception:
                        continue
                await self.bot.db.execute(
                    "UPDATE exploit_stats SET eat_yield_at = now() "
                    "WHERE guild_id=$1 AND eat_staked > 0",
                    guild.id,
                )
            except Exception:
                log.debug("eatchain_yield: guild %s tick failed", guild.id,
                          exc_info=True)
        _hb_pulse("eatchain_yield")

    @eatchain_yield.before_loop
    async def _before_yield(self) -> None:
        await self.bot.wait_until_ready()

    # ── Math helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _gap_metrics(attacker_nw: float, target_nw: float) -> tuple[float, float]:
        """Return (gap, gap_bonus). gap_bonus is never negative -- punching
        down is penalised by the explicit anti-whale multiplier instead."""
        gap = target_nw / max(attacker_nw, 1.0)
        gap_bonus = max(
            0.0,
            min(Config.EAT_GAP_BONUS_CAP, (gap - 1.0) * Config.EAT_GAP_BONUS_PER_X),
        )
        return gap, gap_bonus

    @staticmethod
    def _base_steal_pct(gap: float) -> float:
        """Fraction of the target pool taken as the GROSS steal. Ramps from
        EAT_STEAL_PCT_MIN at parity to EAT_STEAL_PCT_MAX once the target is
        EAT_GAP_FULL_X times richer."""
        span = max(1e-9, Config.EAT_GAP_FULL_X - 1.0)
        factor = max(0.0, min(1.0, (gap - 1.0) / span))
        return (
            Config.EAT_STEAL_PCT_MIN
            + (Config.EAT_STEAL_PCT_MAX - Config.EAT_STEAL_PCT_MIN) * factor
        )

    # ── Powerup chain ─────────────────────────────────────────────────────

    async def _powerup_state(self, db, uid: int, gid: int) -> dict:
        """Return prep/cook state. Each is 'none', 'charging', or 'armed'.
        '*_left' is whole seconds until a charging powerup is ready."""
        row = await db.fetch_one(
            "SELECT prep_ready_at, cook_ready_at, "
            "EXTRACT(EPOCH FROM (prep_ready_at - now())) AS prep_left, "
            "EXTRACT(EPOCH FROM (cook_ready_at - now())) AS cook_left "
            "FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        if not row:
            return {"prep": "none", "cook": "none", "prep_left": 0, "cook_left": 0}

        def classify(ready_at, left):
            if ready_at is None:
                return "none", 0
            left_s = float(left) if left is not None else 0.0
            if left_s > 0:
                return "charging", int(left_s) + 1
            return "armed", 0

        prep, prep_left = classify(row.get("prep_ready_at"), row.get("prep_left"))
        cook, cook_left = classify(row.get("cook_ready_at"), row.get("cook_left"))
        return {"prep": prep, "cook": cook, "prep_left": prep_left, "cook_left": cook_left}

    async def _consume_powerups(self, db, uid: int, gid: int) -> tuple[bool, bool]:
        """Clear ARMED powerups (ready_at <= now). Charging ones are left
        untouched. Returns (prep_consumed, cook_consumed)."""
        prep_row = await db.fetch_one(
            "UPDATE exploit_stats SET prep_ready_at=NULL "
            "WHERE user_id=$1 AND guild_id=$2 "
            "AND prep_ready_at IS NOT NULL AND prep_ready_at <= now() "
            "RETURNING user_id",
            uid, gid,
        )
        cook_row = await db.fetch_one(
            "UPDATE exploit_stats SET cook_ready_at=NULL "
            "WHERE user_id=$1 AND guild_id=$2 "
            "AND cook_ready_at IS NOT NULL AND cook_ready_at <= now() "
            "RETURNING user_id",
            uid, gid,
        )
        return prep_row is not None, cook_row is not None

    # ── Shield / mastery helpers ──────────────────────────────────────────

    async def _is_fortified(self, db, uid: int, gid: int) -> bool:
        row = await db.fetch_one(
            "SELECT 1 FROM exploit_shields "
            "WHERE user_id=$1 AND guild_id=$2 AND active_until > now()",
            uid, gid,
        )
        return row is not None

    async def _mastery_def(self, db, uid: int, gid: int) -> float:
        """Apex Mastery Iron Firewall (combat.exploit_def) on the defender."""
        try:
            from services import mastery as _m
            mp = await _m.passives(db, uid, gid)
            return max(0.0, float(mp.get("combat.exploit_def") or 0.0))
        except Exception:
            return 0.0

    async def _award_raider(self, ctx: DiscoContext, won: bool, magnitude_h: float) -> None:
        """Raider mastery XP + clan-war Apex node contribution."""
        try:
            from services import mastery as _m
            xp = _m.xp_for_action(magnitude_h)
            await _m.add_mastery(ctx.db, ctx.author.id, ctx.guild_id, "raider", xp)
            if won:
                row = await ctx.db.fetch_one(
                    "SELECT group_id FROM group_members "
                    "WHERE guild_id=$1 AND user_id=$2 LIMIT 1",
                    ctx.guild_id, ctx.author.id,
                )
                if row and row.get("group_id"):
                    from services import clan_wars as _cw
                    await _cw.record(
                        ctx.db, ctx.guild_id, ctx.author.id,
                        int(row["group_id"]), "apex", xp,
                    )
        except Exception:
            pass

    async def _poorest_active_uids(self, ctx: DiscoContext, n: int) -> list[int]:
        """The n poorest players active within EAT_ACTIVE_DAYS, poorest first.

        "Active" = touched any economy command in the window (the
        DB-stamped users.last_activity). Dead accounts are skipped so both
        the uneatable shield and the airdrop land on real, playing people."""
        try:
            active = await ctx.db.get_active_players(
                ctx.guild_id, days=Config.EAT_ACTIVE_DAYS, limit=10000,
            )
            active_ids = {r["user_id"] for r in active}
            if not active_ids:
                return []
            nw_map = await compute_bulk_net_worth(ctx.guild_id, ctx.db)
            ranked = sorted(
                ((uid, nw) for uid, nw in nw_map.items() if uid in active_ids),
                key=lambda kv: kv[1],
            )
            return [uid for uid, _ in ranked[:max(0, n)]]
        except Exception as exc:
            log.warning("eat: poorest-active lookup failed (%s)", exc)
            return []

    # ── Salad bowl helpers ────────────────────────────────────────────────

    async def _bowl_add(self, db, gid: int, symbol: str, amount: int) -> None:
        """Add raw `amount` of `symbol` to the guild's salad bowl escrow."""
        if amount <= 0:
            return
        await db.execute(
            "INSERT INTO eat_salad_bowl (guild_id, symbol, amount) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, symbol) DO UPDATE SET "
            "amount = eat_salad_bowl.amount + $3",
            gid, symbol, amount,
        )

    async def _bowl_snapshot(self, db, gid: int) -> list[dict]:
        """All non-empty salad-bowl rows for a guild."""
        return await db.fetch_all(
            "SELECT symbol, amount FROM eat_salad_bowl "
            "WHERE guild_id=$1 AND amount > 0 ORDER BY symbol",
            gid,
        )

    async def _airdrop(
        self, ctx: DiscoContext, symbol: str, amount: int, exclude: set[int],
    ) -> tuple[int, int]:
        """Split raw `amount` of `symbol` equally among the poorest active
        players (excluding `exclude`). Returns (recipients, per_share)."""
        if amount <= 0:
            return 0, 0
        uids = await self._poorest_active_uids(ctx, Config.EAT_AIRDROP_RECIPIENTS + len(exclude))
        uids = [u for u in uids if u not in exclude][:Config.EAT_AIRDROP_RECIPIENTS]
        if not uids:
            return 0, 0
        share = amount // len(uids)
        if share <= 0:
            return 0, 0
        for uid in uids:
            try:
                if symbol == "USD":
                    await ctx.db.update_wallet(uid, ctx.guild_id, share)
                else:
                    await ctx.db.update_holding(uid, ctx.guild_id, symbol, share)
            except Exception:
                pass
        return len(uids), share

    # ── Shared pre-flight gate ────────────────────────────────────────────

    async def _gate(
        self, ctx: DiscoContext, target: discord.Member
    ) -> tuple[dict | None, str | None]:
        """Validate + classify an eat attempt. On failure `error` is a
        ready message (caller refunds the cooldown); on success `info`
        carries the net-worth result, wealth gap and punch direction."""
        if target.id == ctx.author.id:
            return None, "You can't eat yourself. Find someone fatter."
        if target.bot:
            return None, "Bots own nothing worth eating."

        target_row = await ctx.db.get_user(target.id, ctx.guild_id)
        if not target_row:
            return None, f"**{target.display_name}** hasn't registered yet."

        nw_attacker = (await compute_net_worth(ctx.author.id, ctx.guild_id, ctx.db)).total
        nw_target = await compute_net_worth(target.id, ctx.guild_id, ctx.db)
        punching_down = nw_target.total <= nw_attacker

        if punching_down:
            # The poorest ACTIVE player is the one person nobody gets to eat.
            # Only a punch-down can reach them, so the expensive bulk lookup
            # is skipped entirely on the common punch-up path.
            poorest = await self._poorest_active_uids(ctx, 1)
            if poorest and target.id == poorest[0]:
                return None, (
                    f"**{target.display_name}** is the poorest player still "
                    f"active at the table  -  the one person nobody is allowed "
                    f"to eat. Leave them be and go punch up."
                )

        gap, gap_bonus = self._gap_metrics(nw_attacker, nw_target.total)
        return {
            "nw_attacker": nw_attacker,
            "nw_target": nw_target,
            "gap": gap,
            "gap_bonus": gap_bonus,
            "punching_down": punching_down,
        }, None

    # ── ,eat / ,eat bite  -  present the tactic buttons ───────────────────

    async def _do_eat(
        self, ctx: DiscoContext, target: discord.Member, mode: str = "target",
    ) -> None:
        info, err = await self._gate(ctx, target)
        if err:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(err)
            return
        min_pool_h = to_human(int(Config.EAT_BITE_MIN_POOL))
        if info["nw_target"].wallet < min_pool_h:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"**{target.display_name}** keeps their fortune offshore  -  "
                f"nothing liquid in their wallet to grab. Try "
                f"`{ctx.prefix}eat bite {target.display_name} crypto`."
            )
            return
        await self._present_tactics(ctx, target, "wallet", info, mode)

    async def _do_bite(
        self, ctx: DiscoContext, target: discord.Member, pool_arg: str
    ) -> None:
        pool = _BITE_POOL_ALIASES.get((pool_arg or "wallet").lower().strip())
        if pool is None:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"**{pool_arg}** isn't a balance pool. Pick one of: "
                f"`wallet`, `crypto`, `defi`, `bank`, `stakes`."
            )
            return

        # The precision bite suite unlocks at the MEV Searcher rank (Lv 25).
        me = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        if not EC.perk_unlocked(EC.level_for_xp(float(me.get("eat_xp") or 0)), "bite"):
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"`,eat bite` is a precision tactic that unlocks at the "
                f"**MEV Searcher** rank (Level 25). Climb the Eat Ladder with "
                f"`{ctx.prefix}eat`, `{ctx.prefix}eat snipe` and "
                f"`{ctx.prefix}eat nibble` first."
            )
            return

        info, err = await self._gate(ctx, target)
        if err:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(err)
            return

        if pool == "stakes":
            ctx.command.reset_cooldown(ctx)
            await ctx.reply(
                embed=card(
                    "🦷 Nothing to Bite",
                    description=(
                        f"**{target.display_name}**'s staked wealth is locked "
                        f"inside validator contracts  -  bolted down, and "
                        f"*always* safe in Eat the Rich. Go after their "
                        f"**wallet**, **crypto**, **defi**, or **bank**."
                    ),
                    color=C_AMBER,
                ).build(),
                mention_author=False, allowed_mentions=_SILENT,
            )
            return

        nw_target = info["nw_target"]
        pool_value = {
            "wallet": nw_target.wallet, "crypto": nw_target.cefi_crypto,
            "defi": nw_target.defi_wallet, "bank": nw_target.bank,
        }[pool]
        min_pool_h = to_human(int(Config.EAT_BITE_MIN_POOL))
        if pool_value < min_pool_h:
            ctx.command.reset_cooldown(ctx)
            await self._reply_empty_pool(ctx, target, pool, nw_target, min_pool_h)
            return

        await self._present_tactics(ctx, target, pool, info, "bite")

    async def _present_tactics(
        self, ctx: DiscoContext, target: discord.Member, pool: str, info: dict,
        mode: str = "target",
    ) -> None:
        """Render the Type 1/2/3 selection embed + buttons, then resolve."""
        nw_target = info["nw_target"]
        gap = info["gap"]
        gap_bonus = info["gap_bonus"]
        punching_down = info["punching_down"]
        pd_mult = Config.EAT_PUNCH_DOWN_ODDS_MULT if punching_down else 1.0

        attacker_wallet_raw = int(ctx.user_row["wallet"])
        pw = await self._powerup_state(ctx.db, ctx.author.id, ctx.guild_id)
        prep_armed = pw["prep"] == "armed"
        cook_armed = pw["cook"] == "armed"

        # EatChain combat modifiers: rank odds bonus, an armed burn buff and
        # whether the target is rug-vulnerable all feed the displayed odds.
        mods = await self._combat_mods(
            ctx.db, ctx.author.id, target.id, ctx.guild_id,
        )
        rank_bonus = EC.odds_bonus_for_level(mods["attacker_level"])
        buff_bonus = mods["buff_bonus"]
        cost_mult = EC.cost_mult_for_level(mods["attacker_level"])

        pool_value = {
            "wallet": nw_target.wallet, "crypto": nw_target.cefi_crypto,
            "defi": nw_target.defi_wallet, "bank": nw_target.bank,
        }[pool]

        tactic_stakes = {
            tid: max(
                1,
                int(max(int(c["min_cost"]),
                        int(attacker_wallet_raw * c["cost_pct"])) * cost_mult),
            )
            for tid, c in Config.EAT_TACTICS.items()
        }

        gap_line = (
            f"🐋 **Wealth gap:** {gap:.1f}x richer (**+{gap_bonus*100:.0f}%** odds)"
            if not punching_down else
            f"⚖️ **Punching DOWN:** odds cut to {int(pd_mult*100)}%"
        )
        desc = (
            f"🎯 **Meal:** {target.mention}\n"
            f"🍴 **Pool:** {_BITE_POOL_LABEL[pool]}  -  {fmt_usd(pool_value)} liquid\n"
            f"{gap_line}\n"
        )

        # Prep intel: a cased mark has their whole liquid stack and security
        # status laid bare.
        if prep_armed:
            fortified = await self._is_fortified(ctx.db, target.id, ctx.guild_id)
            desc += (
                f"\n🔍 **Cased (prep armed):**\n"
                f"Wallet {fmt_usd(nw_target.wallet)} · Bank {fmt_usd(nw_target.bank)} · "
                f"CeFi {fmt_usd(nw_target.cefi_crypto)} · DeFi {fmt_usd(nw_target.defi_wallet)}\n"
                f"Security detail: {'**ACTIVE** (your prep walks right past it)' if fortified else 'none'}\n"
            )
        if cook_armed:
            desc += (
                "\n🔥 **Cook armed:** this eat is uncapped and the burn slice "
                "lands in *your* cut.\n"
            )
        if buff_bonus > 0:
            desc += (
                f"\n🔋 **$EAT buff armed:** +{buff_bonus*100:.0f}% odds on "
                f"this eat.\n"
            )
        if rank_bonus > 0:
            desc += f"\n{mods['rank_emoji']} **Rank bonus:** +{rank_bonus*100:.0f}% odds.\n"
        if mods["target_rugged"]:
            desc += (
                "\n🧶 **Target is rug-vulnerable:** odds surge and their "
                "security + insurance are bypassed.\n"
            )
        if punching_down:
            desc = f"⚖️ {_PUNCH_DOWN_WARNING}\n\n" + desc

        desc += "\n**Pick your tactic** -- it decides how the gross is split:\n"
        for tid, c in Config.EAT_TACTICS.items():
            odds = min(
                0.99,
                c["success"] + gap_bonus + rank_bonus + buff_bonus,
            ) * pd_mult
            if mods["target_rugged"]:
                odds = min(0.99, odds + EC.RUG_VULN_ODDS_BONUS)
            split = (
                f"keep {c['keep_pct']*100:.0f}% · burn {c['burn_pct']*100:.0f}% · "
                f"bowl {c['bowl_pct']*100:.0f}% · airdrop {c['airdrop_pct']*100:.0f}%"
            )
            desc += (
                f"\n{c['label']} (Type {c['type_no']})  -  Stake "
                f"**{fmt_usd(to_human(tactic_stakes[tid]))}** · Odds "
                f"**{odds*100:.0f}%**\n{split}"
            )

        title = "🥩 EatChain  -  Front-Running the Mempool"
        if mode == "snipe":
            title = "🛰️ EatChain  -  Mempool Snipe Locked"
        if punching_down:
            title += "  -  Punching Down"
        embed = (
            card(
                title,
                description=desc,
                color=C_WARNING if punching_down else C_CRIMSON,
            )
            .footer(
                "Win returns your stake in full. Lose: 30% gone. "
                "Cancelled eats refund the cooldown."
            )
            .build()
        )
        view = _TacticSelect(
            ctx.author.id, {tid: to_human(s) for tid, s in tactic_stakes.items()},
        )
        msg = await ctx.reply(
            embed=embed, view=view, mention_author=False, allowed_mentions=_SILENT,
        )
        await view.wait()

        if view.chosen is None:
            ctx.command.reset_cooldown(ctx)
            await msg.edit(
                embed=card(
                    description="Transaction dropped from the mempool  -  "
                    "timed out.", color=C_AMBER,
                ).build(),
                view=None,
            )
            return

        await self._resolve_eat(
            ctx, msg, target, pool, view.chosen,
            tactic_stakes[view.chosen], info, mode,
        )

    # ── The shared eat resolver ───────────────────────────────────────────

    async def _resolve_eat(
        self, ctx: DiscoContext, msg, target: discord.Member, pool: str,
        tactic_id: str, wager: int, info: dict, mode: str = "target",
    ) -> None:
        """Roll the eat, move the money, split the gross, fill the bowl,
        run the airdrop, write history/stats, mint $EAT + XP, log + announce."""
        tactic = Config.EAT_TACTICS[tactic_id]
        gap = info["gap"]
        gap_bonus = info["gap_bonus"]
        punching_down = info["punching_down"]
        nw_target = info["nw_target"]
        pd_mult = Config.EAT_PUNCH_DOWN_ODDS_MULT if punching_down else 1.0
        is_token = pool in ("crypto", "defi")

        # For a token pool, pick the target's single largest priced holding
        # from the gate snapshot.
        token_sym: str | None = None
        token_net = ""
        token_price = 0.0
        if is_token:
            holdings = (
                nw_target.holdings if pool == "crypto" else nw_target.wallet_holdings
            )
            priced = [
                h for h in holdings
                if float(h.get("price") or 0) > 0 and float(h.get("usd_value") or 0) > 0
            ]
            if priced:
                big = max(priced, key=lambda h: float(h["usd_value"]))
                token_sym = big["symbol"]
                token_net = str(big.get("network") or "").lower()
                token_price = float(big["price"])
        symbol = token_sym if is_token else "USD"

        # result accumulators (defined for both branches)
        won = False
        gross = keep = burn = bowl_total = airdrop_paid = 0
        airdrop_n = airdrop_share = 0
        total_loss = target_cut = 0
        steal_usd_h = 0.0
        prep_used = cook_used = fortified = security_dodged = False
        traitor_fee = Config.EAT_CLASS_TRAITOR_FEE if punching_down else 0
        # EatChain accumulators: $EAT block reward, XP, level-up, the rare
        # mempool windfall, and the insurance-block outcome.
        combo = insured_block = rare_hit = target_rugged = False
        eat_earned = leveled_to = 0

        async with ctx.db.atomic():
            attacker_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            target_row = await ctx.db.get_user(target.id, ctx.guild_id)
            if not attacker_row or not target_row:
                ctx.command.reset_cooldown(ctx)
                await msg.edit(
                    embed=card(
                        description="❌ Could not verify accounts.", color=C_ERROR,
                    ).build(),
                    view=None,
                )
                return
            attacker_wallet = int(attacker_row["wallet"])
            if attacker_wallet < wager:
                ctx.command.reset_cooldown(ctx)
                await msg.edit(
                    embed=card(
                        description=(
                            f"❌ You can no longer cover the "
                            f"**{fmt_usd(to_human(wager))}** stake."
                        ),
                        color=C_ERROR,
                    ).build(),
                    view=None,
                )
                return

            prep_used, cook_used = await self._consume_powerups(
                ctx.db, ctx.author.id, ctx.guild_id,
            )
            combo = prep_used and cook_used
            fortified = await self._is_fortified(ctx.db, target.id, ctx.guild_id)
            def_bonus = await self._mastery_def(ctx.db, target.id, ctx.guild_id)

            # EatChain combat modifiers.
            mods = await self._combat_mods(
                ctx.db, ctx.author.id, target.id, ctx.guild_id,
            )
            attacker_level = mods["attacker_level"]
            target_rugged = mods["target_rugged"]
            rank_bonus = EC.odds_bonus_for_level(attacker_level)
            buff_bonus = await self._consume_odds_buff(
                ctx.db, ctx.author.id, ctx.guild_id,
            )
            # A rare mempool windfall -- hidden until it lands.
            rare_hit = random.random() < EC.RARE_EVENT_CHANCE
            rare_bonus = EC.RARE_EVENT_ODDS_BONUS if rare_hit else 0.0

            # Roll. Base tactic odds + wealth-gap bonus + rank bonus + an
            # armed burn buff + the rare windfall, capped, then punch-down.
            chance = min(
                0.99,
                tactic["success"] + gap_bonus + rank_bonus
                + buff_bonus + rare_bonus,
            ) * pd_mult
            if target_rugged:
                # A rug-vulnerable mark is wide open: odds surge and neither
                # a security detail nor insurance can save them.
                chance = min(0.99, chance + EC.RUG_VULN_ODDS_BONUS)
            else:
                # A cased mark (prep used) walks past a security detail;
                # otherwise an active detail quarters the odds.
                security_dodged = fortified and not prep_used
                if security_dodged:
                    chance *= 0.25
                chance *= max(0.0, 1.0 - def_bonus)
            won = random.random() < chance

            # Insurance: a clean win against an insured, non-rugged target
            # is fully blocked -- a charge burns, the attacker keeps the stake.
            if won and not target_rugged:
                if await self._consume_insurance(ctx.db, target.id, ctx.guild_id):
                    won = False
                    insured_block = True

            # Pool's currently-available raw balance.
            if pool == "wallet":
                pool_raw = int(target_row["wallet"])
            elif pool == "bank":
                pool_raw = int(target_row["bank"])
            elif is_token and token_sym:
                if pool == "crypto":
                    fresh = await ctx.db.get_holding(target.id, ctx.guild_id, token_sym)
                else:
                    fresh = await ctx.db.get_wallet_holding(
                        target.id, ctx.guild_id, token_net, token_sym,
                    )
                pool_raw = int(fresh["amount"]) if fresh else 0
            else:
                pool_raw = 0

            if won and pool_raw > 0:
                # Gross: a gap-scaled slice of the pool, random swing, capped
                # by EAT_MAX_STEAL -- unless the books were cooked.
                base_pct = self._base_steal_pct(gap)
                swing = 1.0 + random.uniform(
                    -Config.EAT_STEAL_VARIANCE, Config.EAT_STEAL_VARIANCE,
                )
                gross = int(pool_raw * base_pct * swing)
                gross = min(gross, pool_raw)
                if not cook_used:
                    gross = min(gross, int(Config.EAT_MAX_STEAL))
                gross = max(0, gross)

            if won and gross > 0:
                # Split the gross four ways. The bowl absorbs integer dust so
                # keep + burn + airdrop + bowl == gross exactly.
                keep = int(gross * tactic["keep_pct"])
                burn = int(gross * tactic["burn_pct"])
                airdrop = int(gross * tactic["airdrop_pct"])
                if cook_used:
                    keep += burn  # cooked books: the burn slice is yours
                    burn = 0
                bowl = gross - keep - burn - airdrop

                # Debit the full gross from the target's pool.
                if pool == "wallet":
                    await ctx.db.update_wallet(target.id, ctx.guild_id, -gross)
                elif pool == "bank":
                    await ctx.db.update_bank(target.id, ctx.guild_id, -gross)
                elif pool == "crypto":
                    await ctx.db.update_holding(target.id, ctx.guild_id, token_sym, -gross)
                else:
                    await ctx.db.update_wallet_holding(
                        target.id, ctx.guild_id, token_net, token_sym, -gross,
                    )

                # Keep -> attacker (USD to wallet, tokens to a CeFi holding).
                if keep > 0:
                    if is_token:
                        await ctx.db.update_holding(
                            ctx.author.id, ctx.guild_id, token_sym, keep,
                        )
                    else:
                        await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, keep)

                # Airdrop -> poorest active players; any undistributed dust
                # falls through into the salad bowl.
                if airdrop > 0:
                    airdrop_n, airdrop_share = await self._airdrop(
                        ctx, symbol, airdrop, exclude={ctx.author.id, target.id},
                    )
                    airdrop_paid = airdrop_n * airdrop_share
                bowl_total = bowl + (airdrop - airdrop_paid)
                if bowl_total > 0:
                    await self._bowl_add(ctx.db, ctx.guild_id, symbol, bowl_total)
                # burn slice: simply not re-credited -- destroyed forever.

                steal_usd_h = to_human(gross) * (token_price if is_token else 1.0)

                # $EAT block reward + Eat Ladder XP for the winning eat.
                eat_earned = EC.eat_reward(steal_usd_h, gap, combo)
                xp_gained = EC.xp_for_eat(steal_usd_h, gap, combo)
                leveled_to = await self._award_progress(
                    ctx.db, ctx.author.id, ctx.guild_id, eat_earned, xp_gained,
                    chew_reward=int(eat_earned * EC.CHEW_BONUS_EAT_PCT),
                )
            elif not won and not insured_block:
                # Failure: lose 30% of the stake (plus the class-traitor fee
                # when punching down); the target pockets half the penalty.
                penalty = int(wager * Config.EAT_FAIL_PENALTY_PCT)
                total_loss = min(penalty + traitor_fee, attacker_wallet)
                target_cut = min(int(penalty * 0.5), total_loss)
                if total_loss > 0:
                    await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -total_loss)
                if target_cut > 0:
                    await ctx.db.update_wallet(target.id, ctx.guild_id, target_cut)

            steal_usd_raw = to_raw(steal_usd_h) if steal_usd_h > 0 else 0
            history_tier = tactic_id if pool == "wallet" else f"{tactic_id}_{pool}"
            await ctx.db.execute(
                "INSERT INTO exploit_history "
                "(guild_id, attacker_id, target_id, tier, wager, stolen, "
                " won, shielded, mode) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                ctx.guild_id, ctx.author.id, target.id, history_tier, wager,
                steal_usd_raw, won, fortified, mode,
            )
            await ctx.db.execute(
                "INSERT INTO exploit_stats "
                "(user_id, guild_id, heists_attempted, heists_won, total_stolen) "
                "VALUES ($1, $2, 1, $3, $4) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "heists_attempted = exploit_stats.heists_attempted + 1, "
                "heists_won = exploit_stats.heists_won + $3, "
                "total_stolen = exploit_stats.total_stolen + $4",
                ctx.author.id, ctx.guild_id, 1 if won else 0, steal_usd_raw,
            )
            # A target who is not eaten earns a little Eat Ladder XP for
            # weathering the attempt.
            defend_xp = 0 if won else EC.XP_DEFEND_BONUS
            await ctx.db.execute(
                "INSERT INTO exploit_stats "
                "(user_id, guild_id, times_targeted, times_defended, "
                " total_lost, eat_xp) "
                "VALUES ($1, $2, 1, $3, $4, $5) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "times_targeted = exploit_stats.times_targeted + 1, "
                "times_defended = exploit_stats.times_defended + $3, "
                "total_lost = exploit_stats.total_lost + $4, "
                "eat_xp = exploit_stats.eat_xp + $5",
                target.id, ctx.guild_id, 0 if won else 1, steal_usd_raw,
                defend_xp,
            )

        await self._award_raider(
            ctx, won, to_human(to_raw(steal_usd_h) if won else max(total_loss, 1)),
        )

        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "EAT" if pool == "wallet" else "EAT_BITE",
            symbol_in="USD", amount_in=0 if won else total_loss,
            symbol_out=symbol, amount_out=keep,
            network="usd",
        )

        result = self._eat_result_embed(
            target, tactic, pool, won, fortified, security_dodged, punching_down,
            is_token, token_sym, gross, keep, burn, bowl_total, airdrop_paid,
            airdrop_n, total_loss, target_cut, traitor_fee, to_human(wager),
            prep_used, cook_used,
            eat_earned=eat_earned, leveled_to=leveled_to, rare_hit=rare_hit,
            insured_block=insured_block, target_rugged=target_rugged,
        )
        await msg.edit(embed=result, view=None)
        if leveled_to:
            await self._announce_level_up(ctx, leveled_to)

        await ctx.bot.bus.publish(
            "exploit_completed",
            guild=ctx.guild, attacker=ctx.author, target=target,
            tier=tactic_id, wager=wager, won=won,
            stolen=to_raw(steal_usd_h) if won else 0,
            shielded=fortified, tx_hash=tx_hash,
        )
        await self._log_server_event(
            ctx, target, won, tactic["label"], steal_usd_h, to_human(total_loss),
        )

        # Silent DM to the victim.
        amt_str = (
            fmt_token(to_human(gross), token_sym) if is_token else fmt_usd(to_human(gross))
        )
        await self._dm_victim(
            ctx, target, won, amt_str, to_human(target_cut), _BITE_POOL_LABEL[pool],
        )

    def _eat_result_embed(
        self, target, tactic, pool, won, fortified, security_dodged, punching_down,
        is_token, token_sym, gross, keep, burn, bowl_total, airdrop_paid,
        airdrop_n, total_loss, target_cut, traitor_fee, wager_h, prep_used, cook_used,
        eat_earned: int = 0, leveled_to: int = 0, rare_hit: bool = False,
        insured_block: bool = False, target_rugged: bool = False,
    ) -> discord.Embed:
        """Build the post-eat result card with the full gross split."""

        def amt(raw: int) -> str:
            return (
                fmt_token(to_human(raw), token_sym) if is_token and token_sym
                else fmt_usd(to_human(raw))
            )

        powerup_bits = []
        if prep_used:
            powerup_bits.append("🔍 prep")
        if cook_used:
            powerup_bits.append("🔥 cook")
        powerup_note = (
            f"\n\nPowerups spent: {', '.join(powerup_bits)}." if powerup_bits else ""
        )

        if won:
            flavor = random.choice(
                (*_EAT_WIN_FLAVORS, *EC.CRYPTO_WIN_FLAVORS)
            ).format(target=target.display_name)
            extra = ""
            if rare_hit:
                extra = " " + random.choice(EC.RARE_EVENT_FLAVORS)
            if prep_used and fortified:
                extra += " You walked straight past their security."
            if target_rugged:
                extra += " They rugged their own pool -- you just cleaned up."
            if punching_down:
                extra += " The optics are *terrible*, by the way."
            if to_human(gross) >= 1_000_000 and random.random() < EC.LEGENDARY_CHANCE:
                extra += "\n\n" + EC.LEGENDARY_FLAVOR
            builder = (
                card(
                    "🍖 Eaten Alive!",
                    description=flavor + extra + powerup_note,
                    color=C_SUCCESS,
                )
                .field("🍖 Gross Devoured", amt(gross), True)
                .field("💰 Your Cut", f"+{amt(keep)}", True)
                .field("🍴 $EAT Earned", f"+{fmt_token(to_human(eat_earned), 'EAT')}", True)
                .field("🛡️ Stake", f"{fmt_usd(wager_h)} (returned)", True)
                .field("🥗 To Salad Bowl", amt(bowl_total), True)
                .field("🔥 Burned", amt(burn), True)
                .field(
                    "🪂 Airdropped",
                    f"{amt(airdrop_paid)} to {airdrop_n} player(s)"
                    if airdrop_paid > 0 else "none",
                    True,
                )
                .field("🎯 Meal", target.mention, True)
                .field("🍴 Tactic", f"{tactic['label']} (Type {tactic['type_no']})", True)
                .field_if(
                    leveled_to > 0, "📈 Level Up",
                    f"You reached **Lv {leveled_to}**!", True,
                )
            )
            return builder.build()

        # Insurance block: a clean miss with no penalty -- the policy paid out.
        if insured_block:
            return (
                card(
                    "🧾 Insurance Kicked In",
                    description=(
                        f"**{target.display_name}** had an active EatChain "
                        f"insurance policy. The eat was fully reverted, a "
                        f"charge was burned, and your "
                        f"**{fmt_usd(wager_h)}** stake is untouched."
                    ),
                    color=C_INFO,
                )
                .field("🎯 Insured", target.mention, True)
                .field("🛡️ Stake", f"{fmt_usd(wager_h)} (refunded)", True)
                .build()
            )

        # Loss
        if security_dodged:
            flavor = random.choice(_FORTIFY_FLAVORS).format(target=target.display_name)
        else:
            flavor = random.choice(
                (*_EAT_FAIL_FLAVORS, *EC.CRYPTO_FAIL_FLAVORS)
            ).format(
                target=target.display_name, penalty=fmt_usd(to_human(total_loss)),
            )
        return (
            card("❌ They Got Away!", description=flavor + powerup_note, color=C_ERROR)
            .field("💸 You Lost", fmt_usd(to_human(total_loss)), True)
            .field("🤑 They Pocketed", fmt_usd(to_human(target_cut)), True)
            .field("🎯 Escaped", target.mention, True)
            .field("🍴 Tactic", f"{tactic['label']} (Type {tactic['type_no']})", True)
            .field_if(
                punching_down and traitor_fee > 0,
                "⚖️ Class Traitor Fee", fmt_usd(to_human(traitor_fee)), True,
            )
            .build()
        )

    async def _reply_empty_pool(
        self, ctx, target, pool, nw_target, min_pool_h,
    ) -> None:
        """Flavoured graceful failure when a bite pool is bone dry."""
        flavor = random.choice(_BITE_EMPTY_FLAVORS[pool]).format(t=target.display_name)
        values = {
            "wallet": nw_target.wallet, "crypto": nw_target.cefi_crypto,
            "defi": nw_target.defi_wallet, "bank": nw_target.bank,
        }
        best = max(values, key=values.get)
        if values[best] >= min_pool_h and best != pool:
            hint = (
                f"Their **{best}** is where the money is  -  "
                f"{fmt_usd(values[best])} sitting right there."
            )
        elif values[best] >= min_pool_h:
            hint = "Try one of their other pools instead."
        else:
            hint = "Honestly? They're bone dry everywhere. Find a richer mark."
        await ctx.reply(
            embed=card(
                "🦷 Bone Dry",
                description=f"{flavor}\n\n{hint}",
                color=C_AMBER,
            ).footer("Cooldown refunded  -  no stake was charged.").build(),
            mention_author=False, allowed_mentions=_SILENT,
        )

    # ── Post-attempt side effects ─────────────────────────────────────────

    async def _log_server_event(
        self, ctx, target, won, label, steal_h, loss_h,
    ) -> None:
        try:
            if won and steal_h > 0:
                await ctx.db.log_server_event(
                    ctx.guild_id, ctx.channel.id, ctx.author.id, "exploit_win",
                    f"{ctx.author.display_name} ate {fmt_usd(steal_h)} of "
                    f"{target.display_name}'s fortune ({label})",
                    steal_h,
                )
            elif not won and loss_h >= 100:
                await ctx.db.log_server_event(
                    ctx.guild_id, ctx.channel.id, ctx.author.id, "exploit_fail",
                    f"{ctx.author.display_name} tried to eat "
                    f"{target.display_name} and lost {fmt_usd(loss_h)} ({label})",
                    loss_h,
                )
        except Exception:
            pass

    async def _dm_victim(
        self, ctx, target, won, taken_str, target_cut_h, pool_label,
    ) -> None:
        """Notify the victim by a SILENT DM -- never an audible ping."""
        try:
            if won:
                dm = (
                    card(
                        "🚨 You got eaten!",
                        description=(
                            f"**{ctx.author.display_name}** tore **{taken_str}** "
                            f"out of your {pool_label} in **{ctx.guild.name}**."
                        ),
                        color=C_ERROR,
                    )
                    .field(
                        "🛡️ Stay Fed",
                        f"`{ctx.prefix}eat defend` hires a security detail, or "
                        f"move cash into staking where it can't be grabbed.",
                        False,
                    )
                    .build()
                )
            else:
                dm = card(
                    "🛡️ You fought them off!",
                    description=(
                        f"**{ctx.author.display_name}** tried to eat your "
                        f"{pool_label} in **{ctx.guild.name}** but failed.\n"
                        f"You pocketed **{fmt_usd(target_cut_h)}** from the "
                        f"botched attempt."
                    ),
                    color=C_SUCCESS,
                ).build()
            await target.send(embed=dm, silent=True)
        except Exception:
            pass

    # ── ,eat prep / ,eat cook  -  the powerup chain ───────────────────────

    async def _do_prep(self, ctx: DiscoContext) -> None:
        """Stage 1: case the joint. Charges, then arms for the next eat."""
        uid, gid = ctx.author.id, ctx.guild_id
        st = await self._powerup_state(ctx.db, uid, gid)
        if st["prep"] == "armed":
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error("Your prep is already cased and armed  -  go eat someone.")
            return
        if st["prep"] == "charging":
            ctx.command.reset_cooldown(ctx)
            mins = max(1, st["prep_left"] // 60)
            await ctx.reply_error(
                f"You're already casing the joint  -  armed in about **{mins}m**."
            )
            return

        wallet = int(ctx.user_row["wallet"])
        cost = int(Config.EAT_PREP_COST)
        if wallet < cost:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"Casing the joint costs **{fmt_usd(to_human(cost))}**. "
                f"You have **{fmt_usd(to_human(wallet))}**."
            )
            return

        async with ctx.db.atomic():
            await ctx.db.update_wallet(uid, gid, -cost)
            await ctx.db.execute(
                "INSERT INTO exploit_stats (user_id, guild_id, prep_ready_at) "
                "VALUES ($1, $2, now() + $3 * interval '1 second') "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "prep_ready_at = now() + $3 * interval '1 second'",
                uid, gid, Config.EAT_PREP_DURATION,
            )
        try:
            await ctx.db.log_tx(
                gid, uid, "EAT_PREP", symbol_in="USD", amount_in=cost,
                symbol_out="USD", amount_out=0, network="usd",
            )
        except Exception:
            pass

        mins = max(1, Config.EAT_PREP_DURATION // 60)
        embed = (
            card("🔍 Casing the Joint", color=C_AMBER)
            .description(
                f"{random.choice(_PREP_FLAVORS)}\n\n"
                f"Your prep arms in **~{mins}m**. Once armed, your next "
                f"`{ctx.prefix}eat` reveals the target's full holdings and "
                f"walks straight past any security detail. Then run "
                f"`{ctx.prefix}eat cook` to escalate."
            )
            .field("💸 Cost", fmt_usd(to_human(cost)), True)
            .field("⏳ Arms in", f"~{mins}m", True)
            .footer("Prep -> Cook -> unleash. Powerups hold until an eat consumes them.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    async def _do_cook(self, ctx: DiscoContext) -> None:
        """Stage 2: cook the books. Requires an armed prep first."""
        uid, gid = ctx.author.id, ctx.guild_id
        st = await self._powerup_state(ctx.db, uid, gid)
        if st["prep"] != "armed":
            ctx.command.reset_cooldown(ctx)
            if st["prep"] == "charging":
                mins = max(1, st["prep_left"] // 60)
                await ctx.reply_error(
                    f"Your prep is still casing the joint  -  wait about "
                    f"**{mins}m** for it to arm, *then* cook."
                )
            else:
                await ctx.reply_error(
                    f"You have to `{ctx.prefix}eat prep` before you can cook. "
                    f"No prep, no cook."
                )
            return
        if st["cook"] == "armed":
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error("The books are already cooked and armed  -  go eat.")
            return
        if st["cook"] == "charging":
            ctx.command.reset_cooldown(ctx)
            mins = max(1, st["cook_left"] // 60)
            await ctx.reply_error(
                f"You're already cooking  -  armed in about **{mins}m**."
            )
            return

        wallet = int(ctx.user_row["wallet"])
        cost = int(Config.EAT_COOK_COST)
        if wallet < cost:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"Cooking the books costs **{fmt_usd(to_human(cost))}**. "
                f"You have **{fmt_usd(to_human(wallet))}**."
            )
            return

        async with ctx.db.atomic():
            await ctx.db.update_wallet(uid, gid, -cost)
            await ctx.db.execute(
                "INSERT INTO exploit_stats (user_id, guild_id, cook_ready_at) "
                "VALUES ($1, $2, now() + $3 * interval '1 second') "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "cook_ready_at = now() + $3 * interval '1 second'",
                uid, gid, Config.EAT_COOK_DURATION,
            )
        try:
            await ctx.db.log_tx(
                gid, uid, "EAT_COOK", symbol_in="USD", amount_in=cost,
                symbol_out="USD", amount_out=0, network="usd",
            )
        except Exception:
            pass

        mins = max(1, Config.EAT_COOK_DURATION // 60)
        embed = (
            card("🔥 Cooking the Books", color=C_AMBER)
            .description(
                f"{random.choice(_COOK_FLAVORS)}\n\n"
                f"Your cook arms in **~{mins}m**. A cooked eat is **uncapped** "
                f"and the slice that would burn lands in **your** cut instead. "
                f"It also unlocks `{ctx.prefix}eat rich`."
            )
            .field("💸 Cost", fmt_usd(to_human(cost)), True)
            .field("⏳ Arms in", f"~{mins}m", True)
            .footer("An eat consumes every armed powerup you are holding.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat defend  -  private security detail ───────────────────────────

    async def _do_fortify(self, ctx: DiscoContext) -> None:
        """Hire a private security detail against people trying to eat you."""
        uid, gid = ctx.author.id, ctx.guild_id
        existing = await ctx.db.fetch_one(
            "SELECT * FROM exploit_shields WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        if existing and existing.get("active_until"):
            active = existing["active_until"]
            active_ts = active.timestamp() if hasattr(active, "timestamp") else float(active or 0)
            if active_ts > time.time():
                remaining = int(active_ts - time.time())
                h, m = remaining // 3600, (remaining % 3600) // 60
                await ctx.reply_error(
                    f"Your security detail is already on duty for **{h}h {m}m**."
                )
                return
        if existing and existing.get("last_used_at"):
            last = existing["last_used_at"]
            last_ts = last.timestamp() if hasattr(last, "timestamp") else float(last or 0)
            if time.time() - last_ts < Config.EAT_FORTIFY_COOLDOWN:
                remaining = int(Config.EAT_FORTIFY_COOLDOWN - (time.time() - last_ts))
                h, m = remaining // 3600, (remaining % 3600) // 60
                await ctx.reply_error(
                    f"Your security firm needs to rest. Available again in **{h}h {m}m**."
                )
                return

        wallet = int(ctx.user_row["wallet"])
        cost = int(Config.EAT_FORTIFY_COST)
        if wallet < cost:
            await ctx.reply_error(
                f"A security detail costs **{fmt_usd(to_human(cost))}**. "
                f"You have **{fmt_usd(to_human(wallet))}**."
            )
            return

        duration_hrs = Config.EAT_FORTIFY_DURATION // 3600
        async with ctx.db.atomic():
            await ctx.db.update_wallet(uid, gid, -cost)
            await ctx.db.execute(
                "INSERT INTO exploit_shields (user_id, guild_id, active_until, last_used_at) "
                "VALUES ($1, $2, now() + $3 * interval '1 second', now()) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "active_until = now() + $3 * interval '1 second', last_used_at = now()",
                uid, gid, Config.EAT_FORTIFY_DURATION,
            )
        try:
            await ctx.db.log_tx(
                gid, uid, "EAT_DEFEND", symbol_in="USD", amount_in=cost,
                symbol_out="USD", amount_out=0, network="usd",
            )
        except Exception:
            pass

        embed = (
            card("🛡️ Security Detail Hired", color=C_SUCCESS)
            .description(
                f"Bodyguards on duty for **{duration_hrs}h**. A plain eat or a "
                f"targeted bite against you has its odds slashed **75%**.\n"
                f"Cost: **{fmt_usd(to_human(cost))}**\n\n"
                f"Careful: an attacker who has *cased the joint* "
                f"(`{ctx.prefix}eat prep`) walks straight past the detail."
            )
            .footer(f"Cooldown: {Config.EAT_FORTIFY_COOLDOWN // 3600}h from hiring")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat salad / ,eat rich  -  the salad bowl ────────────────────────

    async def _do_rich(self, ctx: DiscoContext) -> None:
        """Show the salad bowl and an Eat the Bowl button (,eat salad)."""
        rows = await self._bowl_snapshot(ctx.db, ctx.guild_id)
        if not rows:
            await ctx.reply(
                embed=card(
                    "🥗 The Salad Bowl",
                    description=(
                        "The bowl is empty. Every eat feeds it  -  go put "
                        "someone's fortune on the menu."
                    ),
                    color=C_INFO,
                ).build(),
                mention_author=False, allowed_mentions=_SILENT,
            )
            return

        lines = []
        total_usd = 0.0
        for r in rows:
            sym = r["symbol"]
            amt_raw = int(r["amount"])
            if amt_raw <= 0:
                continue
            if sym == "USD":
                usd = to_human(amt_raw)
                lines.append(f"💵 {fmt_usd(usd)}")
            else:
                price_row = await ctx.db.get_price(sym, ctx.guild_id)
                price = float(price_row["price"]) if price_row else 0.0
                usd = to_human(amt_raw) * price
                lines.append(f"🪙 {fmt_token(to_human(amt_raw), sym)}  (~{fmt_usd(usd)})")
            total_usd += usd

        win_cut = total_usd * Config.EAT_SALAD_WIN_PCT
        embed = (
            card("🥗 The Salad Bowl", color=C_GOLD)
            .description(
                "Every eat tips a slice of stolen wealth into the bowl  -  "
                "all currencies, all victims. Take the **1%** gamble to "
                "devour it.\n\n" + "\n".join(lines[:25])
            )
            .field("🍲 Bowl Value", f"~{fmt_usd(total_usd)}", True)
            .field("🎰 Win Chance", f"{Config.EAT_SALAD_WIN_CHANCE*100:.0f}%", True)
            .field(
                "🍽️ Win Takes",
                f"{Config.EAT_SALAD_WIN_PCT*100:.0f}% (~{fmt_usd(win_cut)})", True,
            )
            .footer(
                ",eat rich gambles 1% to eat the bowl -- needs an armed cook. "
                "Win: take your slice, the rest burns. Lose: 5% burns forever."
            )
            .build()
        )
        await ctx.reply(
            embed=embed, view=_BowlEatView(self),
            mention_author=False, allowed_mentions=_SILENT,
        )

    async def _salad_attempt(
        self, db, guild_id: int, user_id: int, display_name: str,
    ) -> discord.Embed:
        """Run one salad-bowl gamble for a user. Returns a result embed.
        Used by both the ,eat rich command and the ,eat salad button."""
        pw = await self._powerup_state(db, user_id, guild_id)
        if pw["cook"] != "armed":
            return card(
                "🥗 No Cook, No Salad",
                description=(
                    "Eating the salad bowl needs an **armed cook**. Run "
                    "`,eat prep`, wait for it, then `,eat cook` and wait again."
                ),
                color=C_AMBER,
            ).build()

        rows = await self._bowl_snapshot(db, guild_id)
        bowl = {r["symbol"]: int(r["amount"]) for r in rows if int(r["amount"]) > 0}
        if not bowl:
            return card(
                "🥗 Empty Bowl",
                description=(
                    "The salad bowl is empty  -  nothing to eat yet. Your "
                    "powerups are untouched."
                ),
                color=C_AMBER,
            ).build()

        won = random.random() < Config.EAT_SALAD_WIN_CHANCE
        payouts: dict[str, int] = {}
        async with db.atomic():
            await self._consume_powerups(db, user_id, guild_id)
            await db.execute(
                "INSERT INTO exploit_stats "
                "(user_id, guild_id, salad_attempts, salad_won) "
                "VALUES ($1, $2, 1, $3) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "salad_attempts = exploit_stats.salad_attempts + 1, "
                "salad_won = exploit_stats.salad_won + $3",
                user_id, guild_id, 1 if won else 0,
            )
            if won:
                for sym, amt_raw in bowl.items():
                    win_amt = int(amt_raw * Config.EAT_SALAD_WIN_PCT)
                    if win_amt <= 0:
                        continue
                    if sym == "USD":
                        await db.update_wallet(user_id, guild_id, win_amt)
                    else:
                        await db.update_holding(user_id, guild_id, sym, win_amt)
                    payouts[sym] = win_amt
                # The bowl is devoured -- whatever was not paid out burns.
                await db.execute(
                    "DELETE FROM eat_salad_bowl WHERE guild_id=$1", guild_id,
                )
            else:
                for sym, amt_raw in bowl.items():
                    burn = int(amt_raw * Config.EAT_SALAD_LOSS_BURN_PCT)
                    if burn > 0:
                        await db.execute(
                            "UPDATE eat_salad_bowl "
                            "SET amount = GREATEST(0, amount - $3) "
                            "WHERE guild_id=$1 AND symbol=$2",
                            guild_id, sym, burn,
                        )

        if won:
            lines = []
            for sym, amt_raw in payouts.items():
                lines.append(
                    f"💵 {fmt_usd(to_human(amt_raw))}" if sym == "USD"
                    else f"🪙 {fmt_token(to_human(amt_raw), sym)}"
                )
            return (
                card(
                    "🥗 THE BOWL IS YOURS!",
                    description=(
                        f"{random.choice(_SALAD_WIN_FLAVORS)}\n\n"
                        f"**{display_name}** devoured the salad bowl:\n"
                        + ("\n".join(lines) if lines else "a fortune")
                        + f"\n\nThe remaining "
                        f"{(1-Config.EAT_SALAD_WIN_PCT)*100:.0f}% burned to ash."
                    ),
                    color=C_GOLD,
                )
                .footer("Your prep and cook were consumed.")
                .build()
            )
        return (
            card(
                "🥗 The Bowl Holds",
                description=(
                    f"{random.choice(_SALAD_LOSS_FLAVORS)}\n\n"
                    f"**{display_name}** missed  -  "
                    f"**{Config.EAT_SALAD_LOSS_BURN_PCT*100:.0f}%** of the bowl "
                    f"burned away in the scramble."
                ),
                color=C_ERROR,
            )
            .footer("Your prep and cook were consumed.")
            .build()
        )

    async def _do_salad(self, ctx: DiscoContext) -> None:
        embed = await self._salad_attempt(
            ctx.db, ctx.guild_id, ctx.author.id, ctx.author.display_name,
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat stats / history / lb / help ──────────────────────────────────

    async def _do_stats(self, ctx: DiscoContext, user: discord.Member | None) -> None:
        target = user or ctx.author
        row = await ctx.db.fetch_one(
            "SELECT * FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            target.id, ctx.guild_id,
        )
        attempted = int(row["heists_attempted"]) if row else 0
        targeted = int(row["times_targeted"]) if row else 0
        salad_att = int(row.get("salad_attempts") or 0) if row else 0
        xp = float(row.get("eat_xp") or 0) if row else 0.0
        if not row or (attempted == 0 and targeted == 0 and salad_att == 0
                       and xp == 0):
            await ctx.reply(
                embed=card(
                    "🍽️ EatChain Dossier",
                    description=(
                        f"**{target.display_name}** has never plugged a "
                        f"wallet into EatChain."
                    ),
                    color=C_INFO,
                ).build(),
                mention_author=False, allowed_mentions=_SILENT,
            )
            return

        won = int(row["heists_won"])
        stolen = row.h("total_stolen")
        survived = int(row["times_defended"])
        lost = row.h("total_lost")
        salad_won = int(row.get("salad_won") or 0)
        rugs = int(row.get("rugs_pulled") or 0)
        winrate = f"{won/attempted*100:.1f}%" if attempted > 0 else "N/A"
        net = stolen - lost
        level = EC.level_for_xp(xp)
        rank = EC.rank_for_level(level)
        title = EC.TITLES.get(row.get("eat_title") or "fresh_meat",
                              EC.TITLES["fresh_meat"])
        liquid = await self._eat_balance(ctx.db, target.id, ctx.guild_id)
        staked = int(row.get("eat_staked") or 0)

        desc = (
            f"{rank['emoji']} **{rank['name']}**  -  Level **{level}**  ·  "
            f"{title['emoji']} *{title['name']}*\n"
            f"{self._xp_bar(xp, level)}\n\n"
            f"**🍴 As the Predator:**\n"
            f"Eats: **{attempted}** · Won: **{won}** · Rate: **{winrate}**\n"
            f"Wealth Devoured: **{fmt_usd(stolen)}** · Rugs: **{rugs}**\n\n"
            f"**🍖 As the Meal:**\n"
            f"Hunted: **{targeted}** · Survived: **{survived}**\n"
            f"Wealth Lost: **{fmt_usd(lost)}**\n\n"
            f"**🥗 Salad Bowl:** {salad_att} attempt(s), {salad_won} win(s)\n"
            f"**🍽️ $EAT:** {fmt_token(to_human(liquid), 'EAT')} liquid · "
            f"{fmt_token(to_human(staked), 'EAT')} staked\n\n"
            f"**📊 Net devoured:** **{'+' if net >= 0 else ''}{fmt_usd(net)}**"
        )
        if target.id == ctx.author.id:
            pw = await self._powerup_state(ctx.db, target.id, ctx.guild_id)
            desc += (
                f"\n\n**Powerups:** prep `{pw['prep']}` · cook `{pw['cook']}`"
            )
        embed = (
            card("🍽️ EatChain Dossier", description=desc, color=C_CRIMSON)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    async def _do_history(self, ctx: DiscoContext) -> None:
        rows = await ctx.db.fetch_all(
            "SELECT * FROM exploit_history WHERE guild_id=$1 "
            "ORDER BY created_at DESC LIMIT 12",
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply(
                embed=card(
                    "🍽️ The Mempool Feed",
                    description="No eats have been confirmed on EatChain here yet.",
                    color=C_INFO,
                ).build(),
                mention_author=False, allowed_mentions=_SILENT,
            )
            return

        # Players wearing "The Silent Devourer" are anonymised in the feed.
        silent_rows = await ctx.db.fetch_all(
            "SELECT user_id FROM exploit_stats "
            "WHERE guild_id=$1 AND eat_title='silent_devourer'",
            ctx.guild_id,
        )
        silent_ids = {r["user_id"] for r in silent_rows}
        _MODE_TAG = {
            "snipe": "🛰️", "target": "🎯", "bite": "🦷",
            "nibble": "🐁", "feast": "🍱", "rug": "🧶",
        }

        lines = []
        for r in rows:
            if r["attacker_id"] in silent_ids:
                atk_name = "an anonymous validator"
            else:
                attacker = ctx.guild.get_member(r["attacker_id"])
                atk_name = (
                    attacker.display_name if attacker
                    else f"User {r['attacker_id']}"
                )[:20]
            tgt_member = ctx.guild.get_member(r["target_id"])
            tgt_name = (
                tgt_member.display_name if tgt_member
                else f"User {r['target_id']}"
            )[:20]
            ts_str = fmt_ts(r["created_at"])
            tag = _MODE_TAG.get(str(r.get("mode") or "target"), "🎯")
            if r["won"]:
                outcome = f"🍖 ate {fmt_usd(r.h('stolen'))}"
            elif r["shielded"]:
                outcome = "🛡️ blocked"
            else:
                outcome = "❌ got away"
            lines.append(
                f"`{ts_str}` {tag} **{atk_name}** -> **{tgt_name}**  -  {outcome}"
            )

        description = "\n".join(lines)
        if len(description) > 3900:
            description = description[:3900] + "\n..."
        embed = card("🍽️ The Mempool Feed", description=description, color=C_CRIMSON)
        embed.footer("Last 12 eats")
        await ctx.reply(
            embed=embed.build(), mention_author=False, allowed_mentions=_SILENT,
        )

    async def _do_lb(self, ctx: DiscoContext) -> None:
        """The multi-tab EatChain leaderboard. ,lb eat delegates here too."""
        gid = ctx.guild_id
        stats = await ctx.db.fetch_all(
            "SELECT * FROM exploit_stats WHERE guild_id=$1", gid,
        )
        if not stats:
            await ctx.reply_error(
                f"Nobody has touched EatChain yet. Run `{ctx.prefix}eat` to start."
            )
            return

        def _name(uid: int) -> str:
            m = ctx.guild.get_member(uid)
            return (m.display_name if m else f"User {uid}")[:22]

        def _board(title: str, ranked: list, fmt) -> discord.Embed:
            if not ranked:
                body = "Nothing here yet."
            else:
                medals = ["🥇", "🥈", "🥉"]
                body = "\n".join(
                    f"{medals[i] if i < 3 else f'`#{i+1}`'} "
                    f"**{_name(uid)}**  -  {fmt(val)}"
                    for i, (uid, val) in enumerate(ranked[:15])
                )
            return (
                card(f"🍽️ EatChain Leaderboard  -  {title}",
                     description=body, color=C_CRIMSON)
                .build()
            )

        pages: list[discord.Embed] = []

        # Wealth Devoured (net).
        devoured = sorted(
            ((s["user_id"], s.h("total_stolen") - s.h("total_lost"))
             for s in stats if int(s["heists_attempted"]) > 0),
            key=lambda kv: kv[1], reverse=True,
        )
        pages.append(_board(
            "Wealth Devoured", devoured,
            lambda v: f"{'+' if v >= 0 else ''}{fmt_usd(v)}",
        ))

        # The Ladder (by XP).
        ladder = sorted(
            ((s["user_id"], float(s.get("eat_xp") or 0)) for s in stats),
            key=lambda kv: kv[1], reverse=True,
        )
        pages.append(_board(
            "The Eat Ladder", [(u, v) for u, v in ladder if v > 0],
            lambda v: f"Lv {EC.level_for_xp(v)}  -  "
                      f"{EC.rank_for_level(EC.level_for_xp(v))['name']}",
        ))

        # EatChain TVL (staked $EAT).
        tvl = sorted(
            ((s["user_id"], int(s.get("eat_staked") or 0)) for s in stats),
            key=lambda kv: kv[1], reverse=True,
        )
        pages.append(_board(
            "EatChain TVL", [(u, v) for u, v in tvl if v > 0],
            lambda v: fmt_token(to_human(v), "EAT"),
        ))

        # Iron Vaults (top defenders).
        vaults = sorted(
            ((s["user_id"], int(s.get("times_defended") or 0)) for s in stats),
            key=lambda kv: kv[1], reverse=True,
        )
        pages.append(_board(
            "Iron Vaults", [(u, v) for u, v in vaults if v > 0],
            lambda v: f"{v} attack(s) repelled",
        ))

        # Mempool Snipes (rolling 30-day).
        snipe_rows = await ctx.db.fetch_all(
            "SELECT attacker_id, COUNT(*) AS n FROM exploit_history "
            "WHERE guild_id=$1 AND won = TRUE AND mode='snipe' "
            "AND created_at > now() - interval '30 days' "
            "GROUP BY attacker_id ORDER BY n DESC LIMIT 15",
            gid,
        )
        pages.append(_board(
            "Mempool Snipes (30d)",
            [(r["attacker_id"], int(r["n"])) for r in snipe_rows],
            lambda v: f"{v} snipe(s)",
        ))

        # The Big Bite (single largest steal).
        big_rows = await ctx.db.fetch_all(
            "SELECT DISTINCT ON (attacker_id) attacker_id, stolen "
            "FROM exploit_history WHERE guild_id=$1 AND won = TRUE "
            "ORDER BY attacker_id, stolen DESC",
            gid,
        )
        big = sorted(
            ((r["attacker_id"], r.h("stolen")) for r in big_rows),
            key=lambda kv: kv[1], reverse=True,
        )
        pages.append(_board(
            "The Big Bite", [(u, v) for u, v in big if v > 0],
            lambda v: fmt_usd(v),
        ))

        await send_paginated(ctx, pages)

    async def _do_help(self, ctx: DiscoContext) -> None:
        p = ctx.prefix
        desc = (
            "*Class warfare meets decentralized predation. The blockchain "
            "never forgets who ate whom.*\n\n"
            "EatChain is a satirical Layer-2 where you play a predatory "
            "validator. No opt-in -- everyone is on the menu -- but you only "
            "ever *want* to punch **UP**: odds and payout both scale with how "
            "much richer the target is. The poorest active player is uneatable."
        )
        embed = (
            card("🥩 EatChain  -  Only Punch Up.", description=desc, color=C_CRIMSON)
            .field(
                "🎯 Targeting",
                f"**{p}eat** / **{p}eat snipe** -- scan the mempool and "
                f"front-run a random wealthier wallet (no pinging).\n"
                f"**{p}eat @user** -- a targeted on-chain strike.\n"
                f"**{p}eat bite @user [pool]** -- raid one pool "
                f"(wallet/crypto/defi/bank).\n"
                f"**{p}eat nibble [@user]** -- a quick, tiny, instant eat.\n"
                f"**{p}eat feast** -- Apex Validators only: multi-snipe the "
                f"wealthiest wallets at once.\n"
                f"**{p}eat rug** -- pull your own liquidity for instant $EAT.",
                False,
            )
            .field(
                "🍳 The Kitchen (stackable buffs)",
                f"**{p}eat prep** -- case the joint (intel + bypass security).\n"
                f"**{p}eat cook** -- cook the books (uncapped eat; needs prep).\n"
                f"**{p}eat chew** -- digest a recent win for bonus $EAT.",
                False,
            )
            .field(
                "🛡️ DeFi Tools",
                f"**{p}eat defend** -- hire 2h security (-75% attacker odds).\n"
                f"**{p}eat insurance** -- buy charges that fully block eats.\n"
                f"**{p}eat audit @user** -- on-chain recon before you strike.\n"
                f"**{p}eat burn <amt>** -- burn $EAT for a timed odds buff.",
                False,
            )
            .field(
                "🍴 The $EAT Economy",
                f"Every win mints **$EAT**, EatChain's earn-only token, and "
                f"Eat Ladder XP.\n"
                f"**{p}eat stake / unstake** -- stake $EAT for passive "
                f"validator yield.\n"
                f"**{p}eat bag** -- your $EAT, rank and yield.\n"
                f"**{p}eat rank** -- the 100-level Eat Ladder + cosmetic titles.",
                False,
            )
            .field(
                "🥗 Salad Bowl & Info",
                f"Every eat tips wealth into a shared bowl. **{p}eat salad** "
                f"views it; **{p}eat rich** is the 1% gamble (needs cook).\n"
                f"**{p}eat stats** · **{p}eat history** · **{p}eat lb**",
                False,
            )
            .footer(
                "Win returns your stake in full. Cancelled eats refund the cooldown."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── EatChain helpers: $EAT economy + progression ──────────────────────

    @staticmethod
    def _parse_amount(s: str | None, max_raw: int) -> int | None:
        """Parse a user amount string into raw units. 'all'/'max' -> max_raw.
        Accepts k/m suffixes. Returns None on a parse error."""
        if s is None:
            return None
        s = str(s).strip().lower().replace(",", "")
        if s in ("all", "max", "everything"):
            return max(0, int(max_raw))
        mult = 1.0
        if s and s[-1] in "km":
            mult = 1_000.0 if s[-1] == "k" else 1_000_000.0
            s = s[:-1]
        try:
            val = float(s) * mult
        except ValueError:
            return None
        if val <= 0:
            return None
        return to_raw(val)

    async def _eat_balance(self, db, uid: int, gid: int) -> int:
        """Liquid $EAT (raw) -- a wallet_holding on the `eat` network."""
        row = await db.get_wallet_holding(uid, gid, EC.EAT_NETWORK, EC.EAT_SYMBOL)
        return int(row["amount"]) if row else 0

    async def _credit_eat(self, db, uid: int, gid: int, raw: int) -> None:
        if raw > 0:
            await db.update_wallet_holding(
                uid, gid, EC.EAT_NETWORK, EC.EAT_SYMBOL, raw,
            )

    async def _debit_eat(self, db, uid: int, gid: int, raw: int) -> bool:
        if raw <= 0:
            return True
        try:
            await db.update_wallet_holding(
                uid, gid, EC.EAT_NETWORK, EC.EAT_SYMBOL, -raw,
            )
            return True
        except ValueError:
            return False

    async def _eat_stats_row(self, db, uid: int, gid: int) -> dict:
        return await db.fetch_one(
            "SELECT * FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        ) or {}

    def _xp_bar(self, xp: float, level: int) -> str:
        """A 10-segment XP progress bar toward the next Eat Ladder level."""
        if level >= EC.MAX_LEVEL:
            return "`██████████` **MAX** -- Apex Validator"
        base = EC.xp_for_level(level)
        nxt = EC.xp_for_level(level + 1)
        span = max(1, nxt - base)
        cur = max(0, int(float(xp) - base))
        filled = max(0, min(10, int(cur / span * 10)))
        bar = "█" * filled + "░" * (10 - filled)
        return f"`{bar}` {cur:,}/{span:,} XP to Lv {level + 1}"

    async def _combat_mods(
        self, db, attacker_id: int, target_id: int, gid: int,
    ) -> dict:
        """Read the EatChain combat modifiers for an eat: the attacker's
        rank level + any armed burn buff, and whether the target is
        rug-vulnerable. All time checks run on the DB clock."""
        arow = await db.fetch_one(
            "SELECT eat_xp, eat_buff_bonus, "
            "(eat_buff_until IS NOT NULL AND eat_buff_until > now()) AS buff_on "
            "FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            attacker_id, gid,
        )
        trow = await db.fetch_one(
            "SELECT (rug_vuln_until IS NOT NULL AND rug_vuln_until > now()) "
            "AS rugged FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            target_id, gid,
        )
        a_xp = float(arow["eat_xp"]) if arow else 0.0
        a_level = EC.level_for_xp(a_xp)
        buff = (
            float(arow["eat_buff_bonus"] or 0.0)
            if arow and arow.get("buff_on") else 0.0
        )
        return {
            "attacker_level": a_level,
            "rank_emoji": EC.rank_for_level(a_level)["emoji"],
            "buff_bonus": buff,
            "target_rugged": bool(trow["rugged"]) if trow else False,
        }

    async def _consume_odds_buff(self, db, uid: int, gid: int) -> float:
        """Read and clear an armed burn buff. Returns its odds bonus."""
        row = await db.fetch_one(
            "SELECT eat_buff_bonus FROM exploit_stats "
            "WHERE user_id=$1 AND guild_id=$2 "
            "AND eat_buff_until IS NOT NULL AND eat_buff_until > now()",
            uid, gid,
        )
        bonus = float(row["eat_buff_bonus"] or 0.0) if row else 0.0
        if bonus > 0:
            await db.execute(
                "UPDATE exploit_stats SET eat_buff_until=NULL, eat_buff_bonus=0 "
                "WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
        return bonus

    async def _consume_insurance(self, db, uid: int, gid: int) -> bool:
        """Burn one active insurance charge. Returns True if one was spent."""
        row = await db.fetch_one(
            "UPDATE exploit_stats SET insurance_charges = insurance_charges - 1 "
            "WHERE user_id=$1 AND guild_id=$2 AND insurance_charges > 0 "
            "AND insurance_until IS NOT NULL AND insurance_until > now() "
            "RETURNING insurance_charges",
            uid, gid,
        )
        return row is not None

    async def _award_progress(
        self, db, uid: int, gid: int, eat_raw: int, xp: int,
        chew_reward: int = 0,
    ) -> int:
        """Mint $EAT, add Eat Ladder XP and set the chew window. Returns the
        new level if the player leveled up, else 0."""
        if eat_raw > 0:
            try:
                await self._credit_eat(db, uid, gid, eat_raw)
            except Exception:
                pass
        row = await db.fetch_one(
            "INSERT INTO exploit_stats (user_id, guild_id, eat_xp, eat_level) "
            "VALUES ($1, $2, $3, 1) "
            "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
            "eat_xp = exploit_stats.eat_xp + $3 "
            "RETURNING eat_xp, eat_level",
            uid, gid, int(xp),
        )
        if not row:
            return 0
        old_level = int(row["eat_level"])
        new_level = EC.level_for_xp(float(row["eat_xp"]))
        if new_level != old_level:
            await db.execute(
                "UPDATE exploit_stats SET eat_level=$3 "
                "WHERE user_id=$1 AND guild_id=$2",
                uid, gid, new_level,
            )
        if chew_reward > 0:
            await db.execute(
                "UPDATE exploit_stats SET chew_at=now(), chew_reward=$3 "
                "WHERE user_id=$1 AND guild_id=$2",
                uid, gid, int(chew_reward),
            )
        return new_level if new_level > old_level else 0

    async def _announce_level_up(self, ctx: DiscoContext, level: int) -> None:
        rank = EC.rank_for_level(level)
        try:
            await ctx.send(
                embed=card(
                    f"📈 Eat Ladder  -  Level {level}",
                    description=(
                        f"**{ctx.author.display_name}** climbed to "
                        f"**Level {level}**.\n"
                        f"{rank['emoji']} New rank: **{rank['name']}**\n"
                        f"{rank['perk']}"
                    ),
                    color=C_GOLD,
                ).build(),
                allowed_mentions=_SILENT,
            )
        except Exception:
            pass

    # ── Snipe targeting ───────────────────────────────────────────────────

    async def _richer_actives(self, ctx: DiscoContext) -> list:
        """Live guild members who are active and strictly richer than the
        caller, ordered ascending by net worth."""
        try:
            active = await ctx.db.get_active_players(
                ctx.guild_id, days=Config.EAT_ACTIVE_DAYS, limit=10000,
            )
        except Exception:
            active = []
        active_ids = {r["user_id"] for r in active}
        active_ids.discard(ctx.author.id)
        if not active_ids:
            return []
        nw_map = await compute_bulk_net_worth(ctx.guild_id, ctx.db)
        me = nw_map.get(ctx.author.id, 0.0)
        richer = sorted(
            ((uid, nw) for uid, nw in nw_map.items()
             if uid in active_ids and nw > me),
            key=lambda kv: kv[1],
        )
        out = []
        for uid, _nw in richer:
            m = ctx.guild.get_member(uid)
            if m and not m.bot:
                out.append(m)
        return out

    async def _pick_snipe_target(
        self, ctx: DiscoContext, *, biggest: bool = False,
    ) -> tuple[discord.Member | None, str | None]:
        """Pick a random wealthier wallet from the mempool. Block Builders+
        (biggest=True) lock onto the fattest wallets instead of the nearest."""
        candidates = await self._richer_actives(ctx)
        if not candidates:
            return None, (
                "Mempool scan complete: every active wallet is poorer than "
                "yours. Nothing to punch up at -- lonely at the top, hm?"
            )
        pool_n = EC.SNIPE_CANDIDATE_POOL
        pool = candidates[-pool_n:] if biggest else candidates[:pool_n]
        return random.choice(pool), None

    # ── ,eat snipe ────────────────────────────────────────────────────────

    async def _do_snipe(self, ctx: DiscoContext) -> None:
        me = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        level = EC.level_for_xp(float(me.get("eat_xp") or 0))
        biggest = EC.perk_unlocked(level, "mostwanted")
        target, err = await self._pick_snipe_target(ctx, biggest=biggest)
        if err:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(err)
            return
        await ctx.reply(
            embed=card(
                "🛰️ Scanning the Mempool",
                description=(
                    f"{random.choice(EC.SNIPE_SCAN_FLAVORS)}\n\n"
                    f"**Target acquired:** {target.mention}"
                ),
                color=C_INFO,
            ).build(),
            mention_author=False, allowed_mentions=_SILENT,
        )
        await self._do_eat(ctx, target, "snipe")

    # ── ,eat nibble ───────────────────────────────────────────────────────

    async def _do_nibble(
        self, ctx: DiscoContext, target: discord.Member | None,
    ) -> None:
        if target is None:
            target, err = await self._pick_snipe_target(ctx)
            if err:
                ctx.command.reset_cooldown(ctx)
                await ctx.reply_error(err)
                return
        info, err = await self._gate(ctx, target)
        if err:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(err)
            return
        if info["punching_down"]:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                "A nibble still punches up only. Find someone richer."
            )
            return
        if info["nw_target"].wallet < to_human(int(Config.EAT_BITE_MIN_POOL)):
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"**{target.display_name}**'s wallet is too thin to bother "
                f"nibbling."
            )
            return

        stake = int(EC.NIBBLE_COST)
        mods = await self._combat_mods(
            ctx.db, ctx.author.id, target.id, ctx.guild_id,
        )
        won = False
        stolen = loss = eat_earned = 0
        async with ctx.db.atomic():
            arow = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            if not arow or int(arow["wallet"]) < stake:
                ctx.command.reset_cooldown(ctx)
                await ctx.reply_error(
                    f"A nibble stakes {fmt_usd(to_human(stake))}; your wallet "
                    f"can't cover even that."
                )
                return
            trow = await ctx.db.get_user(target.id, ctx.guild_id)
            pool_raw = int(trow["wallet"]) if trow else 0
            chance = min(
                0.95,
                EC.NIBBLE_SUCCESS
                + EC.odds_bonus_for_level(mods["attacker_level"])
                + info["gap_bonus"] * 0.5,
            )
            won = pool_raw > 0 and random.random() < chance
            if won:
                stolen = min(
                    int(pool_raw * EC.NIBBLE_STEAL_PCT),
                    int(EC.NIBBLE_MAX_STEAL), pool_raw,
                )
                if stolen > 0:
                    await ctx.db.update_wallet(target.id, ctx.guild_id, -stolen)
                    await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, stolen)
                eat_earned = int(EC.NIBBLE_EAT_REWARD)
                await self._award_progress(
                    ctx.db, ctx.author.id, ctx.guild_id, eat_earned, EC.XP_NIBBLE,
                )
            else:
                loss = min(
                    int(stake * EC.NIBBLE_FAIL_PENALTY_PCT), int(arow["wallet"]),
                )
                if loss > 0:
                    await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -loss)
            await ctx.db.execute(
                "INSERT INTO exploit_history (guild_id, attacker_id, "
                "target_id, tier, wager, stolen, won, shielded, mode) "
                "VALUES ($1,$2,$3,'nibble',$4,$5,$6,FALSE,'nibble')",
                ctx.guild_id, ctx.author.id, target.id, stake,
                stolen if won else 0, won,
            )
            await ctx.db.execute(
                "INSERT INTO exploit_stats (user_id, guild_id, "
                "heists_attempted, heists_won, total_stolen) "
                "VALUES ($1,$2,1,$3,$4) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "heists_attempted = exploit_stats.heists_attempted + 1, "
                "heists_won = exploit_stats.heists_won + $3, "
                "total_stolen = exploit_stats.total_stolen + $4",
                ctx.author.id, ctx.guild_id, 1 if won else 0,
                stolen if won else 0,
            )
            await ctx.db.execute(
                "INSERT INTO exploit_stats (user_id, guild_id, "
                "times_targeted, total_lost) VALUES ($1,$2,1,$3) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "times_targeted = exploit_stats.times_targeted + 1, "
                "total_lost = exploit_stats.total_lost + $3",
                target.id, ctx.guild_id, stolen if won else 0,
            )

        if won:
            flavor = random.choice(EC.NIBBLE_FLAVORS).format(
                target=target.display_name,
            )
            if random.random() < 0.3:
                flavor += " " + random.choice(EC.NIBBLE_SPAM_FLAVORS)
            embed = (
                card("🐁 Nibble", description=flavor, color=C_SUCCESS)
                .field("🍪 Skimmed", fmt_usd(to_human(stolen)), True)
                .field("🍴 $EAT", f"+{fmt_token(to_human(eat_earned), 'EAT')}", True)
                .build()
            )
        else:
            embed = (
                card(
                    "🐁 Nibble Swatted",
                    description=(
                        f"**{target.display_name}** flicked your nibble away "
                        f"like a crumb."
                    ),
                    color=C_ERROR,
                )
                .field("💸 Lost", fmt_usd(to_human(loss)), True)
                .build()
            )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat feast ────────────────────────────────────────────────────────

    async def _do_feast(self, ctx: DiscoContext) -> None:
        me = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        level = EC.level_for_xp(float(me.get("eat_xp") or 0))
        if not EC.perk_unlocked(level, "feast"):
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                "`,eat feast` is an Apex Validator move  -  reach **Level "
                "100** on the Eat Ladder first."
            )
            return
        cost = int(EC.FEAST_COST * EC.cost_mult_for_level(level))
        targets = await self._richer_actives(ctx)
        targets = targets[-EC.FEAST_TARGETS:]
        if not targets:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                "Mempool scan: no wealthier wallets to feast on."
            )
            return

        results: list[tuple[discord.Member, bool, int]] = []
        total_stolen = total_eat = 0
        leveled = 0
        async with ctx.db.atomic():
            arow = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            if not arow or int(arow["wallet"]) < cost:
                ctx.command.reset_cooldown(ctx)
                await ctx.reply_error(
                    f"A feast costs **{fmt_usd(to_human(cost))}** up front; "
                    f"your wallet can't cover it."
                )
                return
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -cost)
            for m in targets:
                trow = await ctx.db.get_user(m.id, ctx.guild_id)
                pool_raw = int(trow["wallet"]) if trow else 0
                chance = min(
                    0.95, EC.FEAST_SUCCESS + EC.odds_bonus_for_level(level),
                )
                hit = pool_raw > 0 and random.random() < chance
                stolen = 0
                if hit:
                    stolen = min(int(pool_raw * EC.FEAST_STEAL_PCT), pool_raw)
                    if stolen > 0:
                        await ctx.db.update_wallet(m.id, ctx.guild_id, -stolen)
                        await ctx.db.update_wallet(
                            ctx.author.id, ctx.guild_id, stolen,
                        )
                        total_stolen += stolen
                await ctx.db.execute(
                    "INSERT INTO exploit_history (guild_id, attacker_id, "
                    "target_id, tier, wager, stolen, won, shielded, mode) "
                    "VALUES ($1,$2,$3,'feast',$4,$5,$6,FALSE,'feast')",
                    ctx.guild_id, ctx.author.id, m.id,
                    cost // max(1, len(targets)), stolen, hit,
                )
                await ctx.db.execute(
                    "INSERT INTO exploit_stats (user_id, guild_id, "
                    "times_targeted, total_lost) VALUES ($1,$2,1,$3) "
                    "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                    "times_targeted = exploit_stats.times_targeted + 1, "
                    "total_lost = exploit_stats.total_lost + $3",
                    m.id, ctx.guild_id, stolen,
                )
                results.append((m, hit, stolen))
            wins = sum(1 for _m, h, _s in results if h)
            if total_stolen > 0:
                total_eat = EC.eat_reward(to_human(total_stolen), 2.0, False)
                leveled = await self._award_progress(
                    ctx.db, ctx.author.id, ctx.guild_id, total_eat,
                    EC.xp_for_eat(to_human(total_stolen), 2.5, False),
                )
            await ctx.db.execute(
                "INSERT INTO exploit_stats (user_id, guild_id, "
                "heists_attempted, heists_won, total_stolen) "
                "VALUES ($1,$2,$3,$4,$5) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "heists_attempted = exploit_stats.heists_attempted + $3, "
                "heists_won = exploit_stats.heists_won + $4, "
                "total_stolen = exploit_stats.total_stolen + $5",
                ctx.author.id, ctx.guild_id, len(results), wins, total_stolen,
            )

        lines = "\n".join(
            f"{'🍖' if h else '❌'} **{m.display_name}**  -  "
            + (fmt_usd(to_human(s)) if h else "got away")
            for m, h, s in results
        )
        embed = (
            card(
                "🍱 The Feast",
                description=(
                    f"You table-flip the order book and devour "
                    f"**{len(results)}** wallets at once.\n\n{lines}"
                ),
                color=C_GOLD if total_stolen > 0 else C_ERROR,
            )
            .field("🍖 Total Devoured", fmt_usd(to_human(total_stolen)), True)
            .field("🍴 $EAT Earned", f"+{fmt_token(to_human(total_eat), 'EAT')}", True)
            .field("💸 Feast Cost", fmt_usd(to_human(cost)), True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)
        if leveled:
            await self._announce_level_up(ctx, leveled)

    # ── ,eat rug ──────────────────────────────────────────────────────────

    async def _do_rug(self, ctx: DiscoContext) -> None:
        row = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        staked = int(row.get("eat_staked") or 0)
        if staked <= 0:
            await ctx.reply_error(
                f"You have no staked $EAT to rug. Stake some first with "
                f"`{ctx.prefix}eat stake`."
            )
            return
        bonus = int(staked * EC.RUG_EXIT_BONUS_PCT)
        async with ctx.db.atomic():
            await ctx.db.execute(
                "UPDATE exploit_stats SET eat_staked = 0, "
                "rugs_pulled = rugs_pulled + 1, "
                "rug_vuln_until = now() + $3 * interval '1 second' "
                "WHERE user_id=$1 AND guild_id=$2",
                ctx.author.id, ctx.guild_id, EC.RUG_VULN_DURATION,
            )
            await self._credit_eat(
                ctx.db, ctx.author.id, ctx.guild_id, staked + bonus,
            )
        embed = (
            card("🧶 Rug Pulled", description=random.choice(EC.RUG_FLAVORS),
                 color=C_AMBER)
            .field("💰 Liquidity Pulled", fmt_token(to_human(staked), "EAT"), True)
            .field("🎁 Exit Bonus", f"+{fmt_token(to_human(bonus), 'EAT')}", True)
            .field("⚠️ Wide Open For", f"{EC.RUG_VULN_DURATION // 3600}h", True)
            .footer(
                "Eats against you now surge -- security and insurance are bypassed."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat chew ─────────────────────────────────────────────────────────

    async def _do_chew(self, ctx: DiscoContext) -> None:
        row = await ctx.db.fetch_one(
            "SELECT chew_reward, (chew_at IS NOT NULL AND chew_at > "
            "now() - $3 * interval '1 second') AS fresh "
            "FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            ctx.author.id, ctx.guild_id, EC.CHEW_WINDOW,
        )
        reward = int(row["chew_reward"]) if row else 0
        fresh = bool(row["fresh"]) if row else False
        if reward <= 0 or not fresh:
            await ctx.reply_error(
                f"Nothing on the plate to chew. Land a winning eat, then "
                f"`{ctx.prefix}eat chew` within {EC.CHEW_WINDOW // 60} minutes "
                f"to digest it for bonus $EAT."
            )
            return
        leveled = 0
        async with ctx.db.atomic():
            await ctx.db.execute(
                "UPDATE exploit_stats SET chew_reward=0 "
                "WHERE user_id=$1 AND guild_id=$2",
                ctx.author.id, ctx.guild_id,
            )
            await self._credit_eat(ctx.db, ctx.author.id, ctx.guild_id, reward)
            leveled = await self._award_progress(
                ctx.db, ctx.author.id, ctx.guild_id, 0, EC.CHEW_BONUS_XP,
            )
        embed = (
            card(
                "😋 Fully Digested",
                description=(
                    "You chew through the last meal slowly, squeezing every "
                    "last gwei of value out of it."
                ),
                color=C_SUCCESS,
            )
            .field("🍴 Bonus $EAT", f"+{fmt_token(to_human(reward), 'EAT')}", True)
            .field("✨ Bonus XP", f"+{EC.CHEW_BONUS_XP}", True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)
        if leveled:
            await self._announce_level_up(ctx, leveled)

    # ── ,eat insurance ────────────────────────────────────────────────────

    async def _do_insurance(self, ctx: DiscoContext, charges: int) -> None:
        try:
            charges = int(charges)
        except (TypeError, ValueError):
            charges = 1
        charges = max(1, min(charges, EC.INSURANCE_MAX_CHARGES))
        row = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        have = int(row.get("insurance_charges") or 0)
        room = EC.INSURANCE_MAX_CHARGES - have
        if room <= 0:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"You already hold the maximum **{EC.INSURANCE_MAX_CHARGES}** "
                f"insurance charges."
            )
            return
        charges = min(charges, room)
        premium = int(EC.INSURANCE_PREMIUM_EAT) * charges
        bal = await self._eat_balance(ctx.db, ctx.author.id, ctx.guild_id)
        if bal < premium:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"Insurance for **{charges}** charge(s) costs "
                f"**{fmt_token(to_human(premium), 'EAT')}**; you hold "
                f"{fmt_token(to_human(bal), 'EAT')}."
            )
            return
        async with ctx.db.atomic():
            if not await self._debit_eat(
                ctx.db, ctx.author.id, ctx.guild_id, premium,
            ):
                await ctx.reply_error("Could not debit the premium.")
                return
            await ctx.db.execute(
                "INSERT INTO exploit_stats (user_id, guild_id, "
                "insurance_charges, insurance_until) "
                "VALUES ($1,$2,$3, now() + $4 * interval '1 second') "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "insurance_charges = LEAST($5, "
                "    exploit_stats.insurance_charges + $3), "
                "insurance_until = now() + $4 * interval '1 second'",
                ctx.author.id, ctx.guild_id, charges, EC.INSURANCE_DURATION,
                EC.INSURANCE_MAX_CHARGES,
            )
        embed = (
            card(
                "🧾 Insurance Underwritten",
                description=(
                    f"Your wallet is covered. The next **{have + charges}** "
                    f"successful eat(s) against you are fully reverted -- a "
                    f"charge burns each time. A rug-vulnerable wallet is not "
                    f"covered."
                ),
                color=C_INFO,
            )
            .field("📜 Charges", f"{have + charges}", True)
            .field("💸 Premium", fmt_token(to_human(premium), "EAT"), True)
            .field("⏳ Valid", f"{EC.INSURANCE_DURATION // 3600}h", True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat audit ────────────────────────────────────────────────────────

    async def _do_audit(
        self, ctx: DiscoContext, target: discord.Member | None,
    ) -> None:
        if target is None or target.bot or target.id == ctx.author.id:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"Audit who? Usage: `{ctx.prefix}eat audit @user`."
            )
            return
        me = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        level = EC.level_for_xp(float(me.get("eat_xp") or 0))
        if not EC.perk_unlocked(level, "bite"):
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                "`,eat audit` unlocks at the **MEV Searcher** rank "
                "(Level 25)."
            )
            return
        cost = int(EC.AUDIT_COST_EAT)
        bal = await self._eat_balance(ctx.db, ctx.author.id, ctx.guild_id)
        if bal < cost:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"An audit costs **{fmt_token(to_human(cost), 'EAT')}**; you "
                f"hold {fmt_token(to_human(bal), 'EAT')}."
            )
            return

        nw = await compute_net_worth(target.id, ctx.guild_id, ctx.db)
        fortified = await self._is_fortified(ctx.db, target.id, ctx.guild_id)
        flags = await ctx.db.fetch_one(
            "SELECT (insurance_charges > 0 AND insurance_until > now()) "
            "AS insured, (rug_vuln_until IS NOT NULL AND "
            "rug_vuln_until > now()) AS rugged, eat_xp "
            "FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            target.id, ctx.guild_id,
        )
        await self._debit_eat(ctx.db, ctx.author.id, ctx.guild_id, cost)

        liquid = nw.wallet + nw.bank + nw.cefi_crypto + nw.defi_wallet
        pct = (liquid / nw.total * 100.0) if nw.total > 0 else 0.0
        t_level = EC.level_for_xp(float(flags["eat_xp"]) if flags else 0.0)
        t_rank = EC.rank_for_level(t_level)
        defences = []
        if fortified:
            defences.append("🛡️ security detail ACTIVE")
        if flags and flags["insured"]:
            defences.append("🧾 insured")
        if flags and flags["rugged"]:
            defences.append("🧶 **RUG-VULNERABLE -- strike now**")
        if not defences:
            defences.append("none -- wide open")

        embed = (
            card(
                f"🔍 On-Chain Audit  -  {target.display_name}",
                description=(
                    f"Net worth: **{fmt_usd(nw.total)}**\n"
                    f"Liquid (eatable): **{fmt_usd(liquid)}** "
                    f"(**{pct:.0f}%** of stack)\n"
                    f"{t_rank['emoji']} Rank: **{t_rank['name']}** (Lv {t_level})"
                ),
                color=C_TEAL,
            )
            .field("👛 Wallet", fmt_usd(nw.wallet), True)
            .field("🏦 Bank", fmt_usd(nw.bank), True)
            .field("🪙 CeFi", fmt_usd(nw.cefi_crypto), True)
            .field("⛓️ DeFi", fmt_usd(nw.defi_wallet), True)
            .field("🔒 Locked (safe)", fmt_usd(max(0.0, nw.total - liquid)), True)
            .field("🛡️ Defences", "\n".join(defences), False)
            .footer(f"Audit fee: {fmt_token(to_human(cost), 'EAT')}")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat burn ─────────────────────────────────────────────────────────

    async def _do_burn(self, ctx: DiscoContext, amount: str | None) -> None:
        bal = await self._eat_balance(ctx.db, ctx.author.id, ctx.guild_id)
        raw = self._parse_amount(amount, bal)
        if raw is None:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}eat burn <amount|all>` -- burn $EAT for "
                f"a timed odds buff on your next eat."
            )
            return
        if raw < EC.BURN_MIN:
            await ctx.reply_error(
                f"Burn at least **{fmt_token(to_human(EC.BURN_MIN), 'EAT')}**."
            )
            return
        if raw > bal:
            await ctx.reply_error(
                f"You only hold **{fmt_token(to_human(bal), 'EAT')}** liquid $EAT."
            )
            return
        bonus = min(EC.BURN_MAX_BONUS, to_human(raw) * EC.BURN_ODDS_PER_UNIT)
        async with ctx.db.atomic():
            if not await self._debit_eat(ctx.db, ctx.author.id, ctx.guild_id, raw):
                await ctx.reply_error("Could not burn that amount.")
                return
            await ctx.db.execute(
                "INSERT INTO exploit_stats (user_id, guild_id, "
                "eat_buff_until, eat_buff_bonus) "
                "VALUES ($1,$2, now() + $3 * interval '1 second', $4) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "eat_buff_until = now() + $3 * interval '1 second', "
                "eat_buff_bonus = $4",
                ctx.author.id, ctx.guild_id, EC.BURN_BUFF_DURATION, bonus,
            )
        embed = (
            card(
                "🔥 $EAT Burned",
                description=(
                    f"You torch **{fmt_token(to_human(raw), 'EAT')}** into the "
                    f"EatChain burn address. Deflation feels great."
                ),
                color=C_AMBER,
            )
            .field("📈 Odds Buff", f"+{bonus*100:.1f}%", True)
            .field("⏳ Armed For", f"{EC.BURN_BUFF_DURATION // 60}m", True)
            .footer("Consumed by your next eat.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat stake / unstake / bag ────────────────────────────────────────

    async def _do_stake(self, ctx: DiscoContext, amount: str | None) -> None:
        bal = await self._eat_balance(ctx.db, ctx.author.id, ctx.guild_id)
        raw = self._parse_amount(amount, bal)
        if raw is None:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}eat stake <amount|all>`."
            )
            return
        if raw < EC.STAKE_MIN:
            await ctx.reply_error(
                f"Stake at least **{fmt_token(to_human(EC.STAKE_MIN), 'EAT')}**."
            )
            return
        if raw > bal:
            await ctx.reply_error(
                f"You only hold **{fmt_token(to_human(bal), 'EAT')}** liquid $EAT."
            )
            return
        async with ctx.db.atomic():
            if not await self._debit_eat(ctx.db, ctx.author.id, ctx.guild_id, raw):
                await ctx.reply_error("Could not move that amount.")
                return
            await ctx.db.execute(
                "INSERT INTO exploit_stats (user_id, guild_id, eat_staked, "
                "eat_yield_at) VALUES ($1,$2,$3, now()) "
                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                "eat_staked = exploit_stats.eat_staked + $3",
                ctx.author.id, ctx.guild_id, raw,
            )
        row = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        staked = int(row.get("eat_staked") or 0)
        level = EC.level_for_xp(float(row.get("eat_xp") or 0))
        apy = EC.stake_hourly_apy(level)
        embed = (
            card(
                "🥩 $EAT Staked",
                description=(
                    f"You lock **{fmt_token(to_human(raw), 'EAT')}** into an "
                    f"EatChain validator. Block rewards now drip in hourly."
                ),
                color=C_TEAL,
            )
            .field("🔒 Total Staked", fmt_token(to_human(staked), "EAT"), True)
            .field("💧 Hourly Yield", fmt_token(to_human(int(staked * apy)), "EAT"), True)
            .footer("Validator yield scales with your rank.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    async def _do_unstake(self, ctx: DiscoContext, amount: str | None) -> None:
        row = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        staked = int(row.get("eat_staked") or 0)
        if staked <= 0:
            await ctx.reply_error("You have no staked $EAT to withdraw.")
            return
        raw = self._parse_amount(amount, staked)
        if raw is None:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}eat unstake <amount|all>`."
            )
            return
        raw = min(raw, staked)
        async with ctx.db.atomic():
            await ctx.db.execute(
                "UPDATE exploit_stats SET eat_staked = eat_staked - $3 "
                "WHERE user_id=$1 AND guild_id=$2",
                ctx.author.id, ctx.guild_id, raw,
            )
            await self._credit_eat(ctx.db, ctx.author.id, ctx.guild_id, raw)
        embed = (
            card(
                "🥩 $EAT Unstaked",
                description=(
                    f"You pull **{fmt_token(to_human(raw), 'EAT')}** back into "
                    f"your liquid wallet."
                ),
                color=C_TEAL,
            )
            .field(
                "🔒 Still Staked",
                fmt_token(to_human(staked - raw), "EAT"), True,
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    async def _do_bag(self, ctx: DiscoContext) -> None:
        liquid = await self._eat_balance(ctx.db, ctx.author.id, ctx.guild_id)
        row = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        staked = int(row.get("eat_staked") or 0)
        xp = float(row.get("eat_xp") or 0)
        level = EC.level_for_xp(xp)
        rank = EC.rank_for_level(level)
        apy = EC.stake_hourly_apy(level)
        price_row = await ctx.db.get_price("EAT", ctx.guild_id)
        price = float(price_row["price"]) if price_row else 0.0
        usd = (to_human(liquid) + to_human(staked)) * price
        flags = await ctx.db.fetch_one(
            "SELECT insurance_charges, "
            "(insurance_until > now()) AS ins_on, "
            "(eat_buff_until IS NOT NULL AND eat_buff_until > now()) AS buff_on, "
            "(rug_vuln_until IS NOT NULL AND rug_vuln_until > now()) AS rugged "
            "FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
            ctx.author.id, ctx.guild_id,
        )
        status = []
        if flags and flags["buff_on"]:
            status.append("🔋 burn buff armed")
        if flags and flags["ins_on"] and int(flags["insurance_charges"] or 0) > 0:
            status.append(f"🧾 insured x{int(flags['insurance_charges'])}")
        if flags and flags["rugged"]:
            status.append("🧶 rug-vulnerable")
        embed = (
            card(
                f"🍴 EatChain Bag  -  {ctx.author.display_name}",
                description=(
                    f"{rank['emoji']} **{rank['name']}**  -  Level **{level}**\n"
                    f"{self._xp_bar(xp, level)}"
                ),
                color=C_TEAL,
            )
            .field("💵 Liquid $EAT", fmt_token(to_human(liquid), "EAT"), True)
            .field("🔒 Staked $EAT", fmt_token(to_human(staked), "EAT"), True)
            .field("💰 Bag Value", fmt_usd(usd), True)
            .field("💧 Hourly Yield", fmt_token(to_human(int(staked * apy)), "EAT"), True)
            .field("📈 Validator APY", f"{apy * 24 * 365 * 100:.0f}% /yr", True)
            .field_if(bool(status), "⚡ Status", " · ".join(status), True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_SILENT)

    # ── ,eat rank ─────────────────────────────────────────────────────────

    async def _do_rank(self, ctx: DiscoContext) -> None:
        row = await self._eat_stats_row(ctx.db, ctx.author.id, ctx.guild_id)
        xp = float(row.get("eat_xp") or 0)
        level = EC.level_for_xp(xp)
        cur = EC.rank_for_level(level)
        ladder_lines = []
        for r in EC.RANKS:
            mark = "▶ " if r["tier"] == cur["tier"] else "   "
            name = (
                f"**{r['name']}**" if r["tier"] == cur["tier"] else r["name"]
            )
            ladder_lines.append(
                f"{mark}{r['emoji']} `Lv {r['min_level']:>3}+` {name}\n"
                f"     {r['perk']}"
            )
        unlocked = EC.unlocked_titles(row, level)
        equipped = row.get("eat_title") or "fresh_meat"
        eq_title = EC.TITLES.get(equipped, EC.TITLES["fresh_meat"])
        embed = (
            card(
                "🪜 The Eat Ladder",
                description=(
                    f"{cur['emoji']} **{cur['name']}**  -  Level **{level}**\n"
                    f"{self._xp_bar(xp, level)}\n\n"
                    + "\n".join(ladder_lines)
                ),
                color=C_PURPLE,
            )
            .field(
                "🎖️ Title",
                f"Equipped: {eq_title['emoji']} **{eq_title['name']}**\n"
                f"Unlocked: **{len(unlocked)}** / {len(EC.TITLES)}"
                + ("\nPick one from the menu below." if len(unlocked) > 1 else ""),
                False,
            )
            .build()
        )
        view = (
            _TitleSelectView(ctx.author.id, unlocked, equipped)
            if len(unlocked) > 1 else None
        )
        await ctx.reply(
            embed=embed, view=view, mention_author=False,
            allowed_mentions=_SILENT,
        )

    # ── ,eat gm (Easter egg) ──────────────────────────────────────────────

    async def _do_gm(self, ctx: DiscoContext) -> None:
        try:
            await self._credit_eat(
                ctx.db, ctx.author.id, ctx.guild_id, int(EC.GM_TIP),
            )
        except Exception:
            pass
        await ctx.reply(
            embed=card(
                "☀️ gm", description=random.choice(EC.GM_LINES), color=C_GOLD,
            )
            .field("🍴 Vibes Tip", f"+{fmt_token(to_human(int(EC.GM_TIP)), 'EAT')}", True)
            .footer("few understand this")
            .build(),
            mention_author=False, allowed_mentions=_SILENT,
        )

    # ── ,eat command group ────────────────────────────────────────────────

    @commands.group(
        name="eat", aliases=["eattherich", "rob", "devour"],
        invoke_without_command=True, cooldown_after_parsing=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(Config.EAT_COOLDOWN)
    async def eat(self, ctx: DiscoContext, target: discord.Member = None) -> None:
        """Eat a player. With no target, snipe a random wealthier wallet."""
        if target is None:
            await self._do_snipe(ctx)
            return
        await self._do_eat(ctx, target, "target")

    @eat.command(name="bite", aliases=["pool", "snatch"], cooldown_after_parsing=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(Config.EAT_COOLDOWN)
    async def eat_bite(
        self, ctx: DiscoContext, target: discord.Member = None, pool: str = "wallet",
    ) -> None:
        """Bite a specific balance pool: wallet, crypto, defi, bank, or stakes."""
        if target is None:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"Bite who? Usage: `{ctx.prefix}eat bite @user "
                f"[wallet|crypto|defi|bank]`."
            )
            return
        await self._do_bite(ctx, target, pool)

    @eat.command(name="prep", aliases=["case"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(Config.EAT_PREP_COOLDOWN)
    async def eat_prep(self, ctx: DiscoContext) -> None:
        """Case the joint -- stage 1 of the powerup chain."""
        await self._do_prep(ctx)

    @eat.command(name="cook", aliases=["books", "scheme"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(Config.EAT_COOK_COOLDOWN)
    async def eat_cook(self, ctx: DiscoContext) -> None:
        """Cook the books -- stage 2 of the powerup chain (needs prep)."""
        await self._do_cook(ctx)

    @eat.command(name="salad", aliases=["bowl", "saladbowl"])
    @guild_only
    async def eat_salad(self, ctx: DiscoContext) -> None:
        """View the salad bowl and show the Eat the Bowl button."""
        await self._do_rich(ctx)

    @eat.command(name="rich", aliases=["eatbowl", "devour_bowl"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(Config.EAT_COOLDOWN)
    async def eat_rich(self, ctx: DiscoContext) -> None:
        """1% gamble to devour the whole salad bowl (needs an armed cook)."""
        await self._do_salad(ctx)

    @eat.command(name="help", aliases=["info", "guide"])
    @guild_only
    async def eat_help(self, ctx: DiscoContext) -> None:
        """How Eat the Rich works."""
        await self._do_help(ctx)

    @eat.command(name="defend", aliases=["fortify", "bunker", "shield"])
    @guild_only
    @no_bots
    @ensure_registered
    async def eat_defend(self, ctx: DiscoContext) -> None:
        """Hire a private security detail."""
        await self._do_fortify(ctx)

    @eat.command(name="stats", aliases=["record", "classwar", "richstats"])
    @guild_only
    async def eat_stats(self, ctx: DiscoContext, user: discord.Member = None) -> None:
        """View a class-war record."""
        await self._do_stats(ctx, user)

    @eat.command(name="history", aliases=["menu", "themenu", "recent"])
    @guild_only
    async def eat_history(self, ctx: DiscoContext) -> None:
        """The 10 most recent eats in this server."""
        await self._do_history(ctx)

    @eat.command(name="lb", aliases=["leaderboard", "top", "rankings"])
    @guild_only
    async def eat_lb(self, ctx: DiscoContext) -> None:
        """The multi-tab EatChain leaderboard."""
        await self._do_lb(ctx)

    # ── New EatChain subcommands ──────────────────────────────────────────

    @eat.command(name="snipe", aliases=["scan", "frontrun", "mempool"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(EC.SNIPE_COOLDOWN)
    async def eat_snipe(self, ctx: DiscoContext) -> None:
        """Scan the mempool and front-run a random wealthier wallet."""
        await self._do_snipe(ctx)

    @eat.command(name="nibble", aliases=["snack", "graze"], cooldown_after_parsing=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(EC.NIBBLE_COOLDOWN)
    async def eat_nibble(
        self, ctx: DiscoContext, target: discord.Member = None,
    ) -> None:
        """A quick, tiny, instant low-stakes eat."""
        await self._do_nibble(ctx, target)

    @eat.command(name="feast", aliases=["banquet"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(EC.FEAST_COOLDOWN)
    async def eat_feast(self, ctx: DiscoContext) -> None:
        """Apex Validator only: multi-snipe the wealthiest wallets at once."""
        await self._do_feast(ctx)

    @eat.command(name="rug", aliases=["rugpull", "exitscam"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(EC.RUG_COOLDOWN)
    async def eat_rug(self, ctx: DiscoContext) -> None:
        """Pull your own liquidity for instant $EAT -- at the cost of safety."""
        await self._do_rug(ctx)

    @eat.command(name="chew", aliases=["digest"])
    @guild_only
    @no_bots
    @ensure_registered
    async def eat_chew(self, ctx: DiscoContext) -> None:
        """Digest a recent winning eat for bonus $EAT."""
        await self._do_chew(ctx)

    @eat.command(name="insurance", aliases=["insure", "policy"], cooldown_after_parsing=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(EC.INSURANCE_COOLDOWN)
    async def eat_insurance(self, ctx: DiscoContext, charges: int = 1) -> None:
        """Buy insurance charges that fully block incoming eats."""
        await self._do_insurance(ctx, charges)

    @eat.command(name="audit", aliases=["analyse", "analyze", "recon"], cooldown_after_parsing=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(EC.AUDIT_COOLDOWN)
    async def eat_audit(
        self, ctx: DiscoContext, target: discord.Member = None,
    ) -> None:
        """On-chain analysis of a target's wealth and defences."""
        await self._do_audit(ctx, target)

    @eat.command(name="burn", aliases=["torch"], cooldown_after_parsing=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def eat_burn(self, ctx: DiscoContext, amount: str = None) -> None:
        """Burn $EAT to arm a timed odds buff for your next eat."""
        await self._do_burn(ctx, amount)

    @eat.command(name="stake", cooldown_after_parsing=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def eat_stake(self, ctx: DiscoContext, amount: str = None) -> None:
        """Stake $EAT as an EatChain validator for passive yield."""
        await self._do_stake(ctx, amount)

    @eat.command(name="unstake", aliases=["withdraw"], cooldown_after_parsing=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def eat_unstake(self, ctx: DiscoContext, amount: str = None) -> None:
        """Withdraw staked $EAT back to your wallet."""
        await self._do_unstake(ctx, amount)

    @eat.command(name="bag", aliases=["wallet", "balance"])
    @guild_only
    @no_bots
    @ensure_registered
    async def eat_bag(self, ctx: DiscoContext) -> None:
        """Your EatChain wallet: $EAT, rank, XP and yield."""
        await self._do_bag(ctx)

    @eat.command(name="rank", aliases=["ladder", "ranks", "title", "titles"])
    @guild_only
    @no_bots
    @ensure_registered
    async def eat_rank(self, ctx: DiscoContext) -> None:
        """The Eat Ladder: your rank, progress and cosmetic titles."""
        await self._do_rank(ctx)

    @eat.command(name="gm", aliases=["goodmorning"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(86400)
    async def eat_gm(self, ctx: DiscoContext) -> None:
        """gm."""
        await self._do_gm(ctx)

    # ── Legacy standalone commands ────────────────────────────────────────

    @commands.command(name="fortify", aliases=["bunker", "defend"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fortify(self, ctx: DiscoContext) -> None:
        """Hire a private security detail against people trying to eat you."""
        await self._do_fortify(ctx)

    @commands.command(name="eatstats", aliases=["richstats", "classwar"])
    @guild_only
    async def eatstats(self, ctx: DiscoContext, user: discord.Member = None) -> None:
        """View a player's Eat the Rich record."""
        await self._do_stats(ctx, user)

    @commands.command(name="eathistory", aliases=["themenu"])
    @guild_only
    async def eathistory(self, ctx: DiscoContext) -> None:
        """View the 10 most recent eats in this server."""
        await self._do_history(ctx)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(EatTheRich(bot))
