"""Economy dashboard cog -- server-wide stats and metrics."""
from __future__ import annotations


from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only
from core.framework.scale import to_human
from core.framework.ui import (
    C_AMBER,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_PURPLE,
    C_TEAL,
    CategoryPaginator,
    fmt_usd,
)
from services import bottleneck as bn_svc


class EconomyDashboard(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.command(name="economy", aliases=["econ", "stats", "serverstats"])
    @guild_only
    @ensure_registered
    async def economy(self, ctx: DiscoContext) -> None:
        """Server-wide economy dashboard."""
        gid = ctx.guild_id
        db = ctx.db

        # ── Gather data ──────────────────────────────────────────────
        snap = await db.get_economy_snapshot(gid)
        prices = await db.get_all_prices(gid)
        pools = await db.get_all_pools(gid)
        validators = await db.get_validators(gid)
        pos_vals = await db.get_pos_validators(gid)
        pow_chains = await db.get_all_guild_pow_networks(gid)
        all_rigs = await db.get_all_guild_rigs(gid)

        price_map = {p["symbol"]: float(p["price"]) for p in (prices or [])}
        categories: dict[str, list] = {}

        # ── Money Supply ─────────────────────────────────────────────
        # ``get_economy_snapshot`` returns raw NUMERIC(36,0) sums (10^18-
        # scaled per migration 0075) for every monetary column. Convert at
        # the display boundary -- printing the raw int via fmt_usd gave
        # quintillion-dollar reads.
        wallet_h = to_human(int(snap["total_wallet"]))
        bank_h = to_human(int(snap["total_bank"]))
        outstanding_h = to_human(int(snap["total_outstanding"]))
        total_cash = wallet_h + bank_h
        p_money = (
            card("\U0001f4b5 Money Supply", color=C_GOLD)
            .field("Users", f"**{snap['user_count']:,}**", True)
            .field("Total Cash", fmt_usd(total_cash), True)
            .field("Wallets", fmt_usd(wallet_h), True)
            .field("Banks", fmt_usd(bank_h), True)
            .field(
                "Active Loans",
                f"**{snap['active_loans']:,}** ({fmt_usd(outstanding_h)} outstanding)",
                False,
            )
            .build()
        )
        categories["\U0001f4b5 Money"] = [p_money]

        # ── Trading ──────────────────────────────────────────────────
        movers = []
        for p in (prices or []):
            sym = p["symbol"]
            prc = float(p["price"])
            opn = float(p.get("open_price") or prc)
            if opn > 0:
                chg = (prc - opn) / opn
                tok = Config.TOKENS.get(sym, {})
                emoji = tok.get("emoji", "")
                movers.append((sym, emoji, prc, chg))
        movers.sort(key=lambda x: abs(x[3]), reverse=True)

        top_movers_lines = []
        for sym, emoji, prc, chg in movers[:10]:
            arrow = "\u2191" if chg >= 0 else "\u2193"
            sign = "+" if chg >= 0 else ""
            top_movers_lines.append(f"{emoji}{sym}: {fmt_usd(prc)} {arrow} {sign}{chg:.1%}")

        # ``volume_usd_24h`` is a raw NUMERIC(36,0) sum of amount_in across
        # 24h of BUY/SELL/SWAP txs -- descale at the display boundary.
        volume_h = to_human(int(snap["volume_usd_24h"]))
        p_trade = card("\U0001f4ca Trading", color=C_INFO).field(
            "24h Activity",
            f"Trades: **{snap['trade_count_24h']:,}**\nVolume: **{fmt_usd(volume_h)}**",
            False,
        )
        if top_movers_lines:
            p_trade.field("Top Movers (today)", "\n".join(top_movers_lines), False)
        categories["\U0001f4ca Trading"] = [p_trade.build()]

        # ── Pools ────────────────────────────────────────────────────
        total_tvl = 0.0
        pool_lines: list[str] = []
        for pool in (pools or []):
            ra = pool.h("reserve_a")
            rb = pool.h("reserve_b")
            tvl_a = ra * price_map.get(pool["token_a"], 0)
            tvl_b = rb * price_map.get(pool["token_b"], 0)
            tvl = tvl_a + tvl_b
            total_tvl += tvl
            pool_lines.append(
                f"**{pool['token_a']}/{pool['token_b']}** - TVL {fmt_usd(tvl)} ({ra:,.2f} / {rb:,.2f})"
            )

        p_pools = card("\U0001f30a Liquidity Pools", color=C_TEAL).field(
            "Total TVL",
            f"**{fmt_usd(total_tvl)}** across **{len(pools or [])}** pools",
            False,
        )
        if pool_lines:
            for line in pool_lines[:10]:
                p_pools.field("", line, False)
        pool_pages = [p_pools.build()]

        if len(pool_lines) > 10:
            p_pools2 = card("\U0001f30a Liquidity Pools (cont.)", color=C_TEAL)
            for line in pool_lines[10:20]:
                p_pools2.field("", line, False)
            pool_pages.append(p_pools2.build())

        categories["\U0001f30a Pools"] = pool_pages

        # ── Mining ───────────────────────────────────────────────────
        total_hashrate = sum(
            Config.MINING_RIGS.get(r["rig_id"], {}).get("hashrate", 0) * r["quantity"]
            for r in (all_rigs or [])
        )
        miner_count = len(set(r["user_id"] for r in (all_rigs or [])))

        chain_lines: list[str] = []
        for ch in (pow_chains or []):
            sym = ch.get("chain_symbol", "?")
            height = ch.get("block_height", 0)
            diff = ch.get("difficulty", 0)
            # The pow_network_state column is ``current_reward`` (raw
            # NUMERIC(36,0); migration 0075 line 121). Reading the missing
            # ``block_reward`` key always returned 0 here.
            reward_h = to_human(int(ch.get("current_reward", 0) or 0))
            chain_lines.append(
                f"**{sym}** - Block #{height:,} | Diff {diff:,.0f} | "
                f"Reward {reward_h:,.4f} {sym}"
            )

        p_mine = card("\u26cf\ufe0f Mining", color=C_AMBER).field(
            "Network",
            f"Miners: **{miner_count:,}** | Hashrate: **{total_hashrate:,.0f} MH/s**",
            False,
        )
        if chain_lines:
            p_mine.field("PoW Chains", "\n".join(chain_lines), False)
        categories["\u26cf\ufe0f Mining"] = [p_mine.build()]

        # ── Staking ──────────────────────────────────────────────────
        # The ``validators`` table has no ``total_staked`` column -- it has
        # to be aggregated from the ``stakes`` table (one row per staker
        # per validator, ``amount`` is raw NUMERIC(36,0) per migration
        # 0075). Reading the missing key always rendered "0 staked" for
        # every validator. PoS's ``stake_amount`` is raw too.
        active_vals = [v for v in (validators or []) if v.get("is_active", True)]
        active_pos = [v for v in (pos_vals or []) if v.get("is_active", True)]

        # One bulk aggregate keeps this O(validators) instead of O(stakes).
        stake_rows = await db.fetch_all(
            "SELECT validator_id, COALESCE(SUM(amount), 0) AS total_raw "
            "FROM stakes WHERE guild_id=$1 AND amount > 0 GROUP BY validator_id",
            gid,
        )
        stake_by_vid: dict[str, int] = {
            r["validator_id"]: int(r["total_raw"] or 0)
            for r in (stake_rows or [])
        }

        total_staked_pow = sum(stake_by_vid.values())
        total_staked_pos = sum(int(v.get("stake_amount", 0) or 0) for v in active_pos)

        # Sort top validators by their actual aggregated stake.
        ranked_vals = sorted(
            active_vals,
            key=lambda v: stake_by_vid.get(v["validator_id"], 0),
            reverse=True,
        )
        val_lines: list[str] = []
        for v in ranked_vals[:8]:
            vid = v["validator_id"]
            staked_h = to_human(stake_by_vid.get(vid, 0))
            val_lines.append(f"**{vid}** - {staked_h:,.2f} staked")

        p_stake = (
            card("\U0001f510 Staking", color=C_PURPLE)
            .field(
                "PoW Validators",
                f"**{len(active_vals)}** active | Total: "
                f"{to_human(total_staked_pow):,.2f}",
                False,
            )
            .field(
                "PoS Validators",
                f"**{len(active_pos)}** active | Total staked: "
                f"{to_human(total_staked_pos):,.2f}",
                False,
            )
        )
        if val_lines:
            p_stake.field("Top Validators", "\n".join(val_lines), False)
        categories["\U0001f510 Staking"] = [p_stake.build()]

        # ── Gambling ─────────────────────────────────────────────────
        # ``wagered_24h`` is the raw sum of ``transactions.amount_in`` for
        # GAMBLE_* tx types -- descale at the display boundary. (The
        # snapshot query was previously reading a write-empty
        # ``game_results`` table, so the count itself was always zero;
        # fixed in services/transactions.get_economy_snapshot.)
        wagered_h = to_human(int(snap["wagered_24h"]))
        p_gamble = (
            card("\U0001f3b0 Gambling", color=C_NAVY)
            .field(
                "24h Activity",
                f"Games: **{snap['games_24h']:,}**\nWagered: **{fmt_usd(wagered_h)}**",
                False,
            )
            .build()
        )
        categories["\U0001f3b0 Gambling"] = [p_gamble]

        # ── Health: Wealth Bottleneck pool, curve, holder distribution ──
        # Single source of truth: bulk net worth via services.bottleneck,
        # same path the bottleneck uses to scale every credit. The
        # ,economy and ,bottleneck commands therefore agree on every
        # number.
        nw_map = await bn_svc.cached_bulk_net_worth(db, gid)
        positive = sorted((float(v) for v in nw_map.values() if v > 0))
        n_holders = len(positive)
        total_supply = sum(positive) if positive else 0.0
        median = positive[n_holders // 2] if n_holders else 0.0
        pool_state = await bn_svc.get_pool_state(db, gid)

        p_health = (
            card("\U0001f4a1 Economy Health", color=C_INFO)
            .description(
                "Wealth Bottleneck snapshot. Drag taken off the top of the "
                "leaderboard funds the per-guild pool; boost paid to the "
                "bottom is drawn from the same pool."
            )
            .field(
                "Holders",
                f"**{n_holders:,}** with positive net worth\n"
                f"Total: {fmt_usd(total_supply)}\n"
                f"Median: {fmt_usd(median)}",
                True,
            )
            .field(
                "Community Pool",
                f"**{fmt_usd(pool_state['pool_usd'])}** carrying\n"
                f"Bottom-tier credits draw from this until empty.",
                True,
            )
        )
        # Bottleneck curve so admins / players can see exactly how the
        # multiplier bites at any leaderboard rank.
        curve_lines = []
        for pctile, mult in (
            getattr(Config, "BOTTLENECK_CURVE", bn_svc.BOTTLENECK_DEFAULT_CURVE) or []
        ):
            tag = bn_svc.percentile_label(float(pctile))
            curve_lines.append(
                f"`{float(pctile)*100:>5.1f}%` ({tag}) -> **x{float(mult):.2f}**"
            )
        if curve_lines:
            p_health.field("Bottleneck Curve", "\n".join(curve_lines), False)
        if Config.FAUCET_ADAPTIVE_ENABLED:
            _adapt = await bn_svc.adaptive_faucet_multiplier(db, gid)
            _per_cap = await bn_svc._supply_per_active(db, gid)
            p_health.field(
                "Adaptive Faucet",
                (
                    f"Per-active-player supply: **{fmt_usd(_per_cap)}**\n"
                    f"Auto-faucet multiplier: **x{_adapt:.2f}** "
                    f"(range x{Config.FAUCET_ADAPTIVE_MIN_MULT:.2f} - "
                    f"x{Config.FAUCET_ADAPTIVE_MAX_MULT:.2f})"
                ),
                False,
            )
        p_health.footer(",bottleneck for player-side breakdown of the curve.")
        categories["\U0001f4a1 Health"] = [p_health.build()]

        await CategoryPaginator.send(ctx, categories)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(EconomyDashboard(bot))
