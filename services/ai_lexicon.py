"""
services/ai_lexicon.py  -  Live game-state lexicon for AI context.

Assembles a structured, real-time snapshot of the server's economy, markets,
chains, and activity into a single context block that any AI agent can pull from.
This replaces the static knowledge snippets in the system prompt with actual truth.

Usage:
    lexicon = await build_lexicon(db, guild_id, price_map)
    system_prompt += lexicon
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


async def build_lexicon(db, guild_id: int, price_map: dict[str, float]) -> str:
    """
    Build a comprehensive live lexicon block for AI context injection.

    Pulls in parallel:
      - Live token prices with 24h change
      - Active market event + phase modifiers
      - PoW mining network snapshots (SUN + MTA)
      - Top validators by stake
      - Top AMM pools by liquidity
      - Server economy overview (supply, median, leaderboard top-3)
      - Open prediction markets
    """
    try:
        # ── Parallel fetches ──────────────────────────────────────────────────
        (
            prices,
            validators,
            pools,
            leaderboard,
            open_markets,
            sun_net,
            btc_net,
            top_buddies,
            top_battlers,
            top_fishers,
            big_catches,
        ) = await asyncio.gather(
            db.get_all_prices(guild_id),
            db.get_validators(guild_id),
            db.get_all_pools(guild_id),
            db.get_leaderboard(guild_id, limit=5),
            _safe(db.predictions.get_open_markets(guild_id)),
            _safe(db.get_pow_network(guild_id, "SUN")),
            _safe(db.get_pow_network(guild_id, "MTA")),
            _safe(_top_buddies(db, guild_id)),
            _safe(_top_battle_buddies(db, guild_id)),
            _safe(_top_fishers(db, guild_id)),
            _safe(_recent_big_catches(db, guild_id)),
        )

        # Active market event (Redis-backed, non-critical)
        try:
            from services.market_event_engine import get_active_event, get_phase_modifiers
            _redis = getattr(getattr(db, "_bus", None), "_redis", None)
            active_event = await get_active_event(_redis, guild_id)
            event_mods = get_phase_modifiers(active_event)
        except Exception:
            active_event = None
            event_mods = {}

        sections: list[str] = []

        # ── 1. Token prices ───────────────────────────────────────────────────
        if prices:
            lines = []
            for r in prices:
                sym = r["symbol"]
                p = float(r["price"])
                chg = _pct_change(r)
                arrow = "▲" if chg > 0 else ("▼" if chg < 0 else " - ")
                lines.append(f"  {sym:<8} ${p:<12.4f} {arrow}{abs(chg):.2f}%/24h")
            sections.append("LIVE TOKEN PRICES:\n" + "\n".join(lines))

        # ── 2. Active market event ────────────────────────────────────────────
        if active_event:
            ev = active_event
            phase_name = getattr(getattr(ev, "current_phase", None), "name", "unknown phase")
            ev_name = getattr(ev, "event_type", "Unknown Event")
            vol_m = event_mods.get("vol_multiplier", 1.0)
            bias = event_mods.get("price_bias_pct_per_day", 0.0)
            diff_m = event_mods.get("mining_difficulty_mult", 1.0)
            apy_m = event_mods.get("staking_apy_mult", 1.0)
            sections.append(
                f"ACTIVE MARKET EVENT: {ev_name}  -  phase: {phase_name}\n"
                f"  Volatility: {vol_m:.2f}x | Price bias: {bias:+.2f}%/day | "
                f"Mining diff: {diff_m:.2f}x | Staking APY: {apy_m:.2f}x"
            )
        else:
            sections.append("ACTIVE MARKET EVENT: none  -  market is quiet, no directional bias")

        # ── 3. PoW chain snapshots ────────────────────────────────────────────
        chain_lines = []
        if sun_net:
            height = sun_net.get("block_height", 0)
            reward = float(sun_net.get("current_reward", 0))
            total_hr = float(sun_net.get("total_hashrate", 0))
            halvings_done = height // 210_000
            blocks_to_halving = (halvings_done + 1) * 210_000 - height
            chain_lines.append(
                f"  SUN  block #{height:,}  reward {reward:.4f} SUN/block  "
                f"network HR {total_hr:,.0f} MH/s  next halving in {blocks_to_halving:,} blocks"
            )
        if btc_net:
            height = btc_net.get("block_height", 0)
            reward = float(btc_net.get("current_reward", 0))
            total_hr = float(btc_net.get("total_hashrate", 0))
            halvings_done = height // 210_000
            blocks_to_halving = (halvings_done + 1) * 210_000 - height
            chain_lines.append(
                f"  MTA  block #{height:,}  reward {reward:.6f} MTA/block  "
                f"network HR {total_hr:,.0f} MH/s  next halving in {blocks_to_halving:,} blocks"
            )
        if chain_lines:
            sections.append("PoW CHAIN STATE:\n" + "\n".join(chain_lines))

        # ── 4. Top validators ─────────────────────────────────────────────────
        if validators:
            active_vals = [v for v in validators if v.get("active")][:5]
            if active_vals:
                val_lines = []
                for v in active_vals:
                    commission = v.get("commission_rate", 0.9)
                    slash = v.get("slash_count", 0)
                    staked = float(v.get("stake_amount", 0))
                    slash_note = f" ⚠ {slash} slashes" if slash > 0 else ""
                    val_lines.append(
                        f"  {v.get('name', v.get('user_id', '?')):<20} "
                        f"stake {staked:,.0f}  commission {commission*100:.0f}%{slash_note}"
                    )
                sections.append("ACTIVE VALIDATORS (top 5 by stake):\n" + "\n".join(val_lines))

        # ── 5. Top AMM pools ──────────────────────────────────────────────────
        if pools:
            # Sort by total liquidity (reserve_a * price_a + reserve_b * price_b approximation)
            def _pool_liq(p: dict) -> float:
                try:
                    ra = float(p.get("reserve_a") or 0)
                    rb = float(p.get("reserve_b") or 0)
                    pa = price_map.get(p.get("token_a", ""), 1.0)
                    pb = price_map.get(p.get("token_b", ""), 1.0)
                    return ra * pa + rb * pb
                except Exception:
                    return 0.0

            top_pools = sorted(pools, key=_pool_liq, reverse=True)[:5]
            pool_lines = []
            for p in top_pools:
                liq = _pool_liq(p)
                ta = p.get("token_a", "?")
                tb = p.get("token_b", "?")
                apy = float(p.get("apy_24h") or p.get("fee_apy") or 0)
                pool_lines.append(
                    f"  {ta}/{tb:<12} liq ${liq:>10,.0f}  APY {apy:.1f}%"
                )
            sections.append("TOP AMM POOLS:\n" + "\n".join(pool_lines))

        # ── 6. Server economy overview ────────────────────────────────────────
        if leaderboard:
            lb_lines = []
            for i, row in enumerate(leaderboard[:3], 1):
                name = row.get("display_name") or row.get("user_id", "?")
                worth = float(row.get("net_worth") or row.get("wallet", 0))
                lb_lines.append(f"  #{i} {name}: ${worth:,.0f}")
            sections.append("TOP 3 PLAYERS BY NET WORTH:\n" + "\n".join(lb_lines))

        # ── 7a. Top buddies by level ─────────────────────────────────────────
        # The "buddies" system is a per-user AI-backed pet companion -- users
        # hatch a species-rolled pet, feed / pet / talk to it, battle other
        # players' buddies, and it earns XP from chat activity. Each buddy
        # has its own personality, remembers its current owner, and keeps
        # a record of every past owner (including ones who were banned).
        if top_buddies:
            blines = []
            for i, b in enumerate(top_buddies[:5], 1):
                species = str(b.get("species") or "?")
                name = str(b.get("name") or "?")
                lvl = int(b.get("level") or 1)
                wins = int(b.get("wins") or 0)
                losses = int(b.get("losses") or 0)
                owner = str(b.get("owner_display_name") or f"user_{b.get('owner_user_id') or '?'}")
                rec = f"  {wins}W-{losses}L" if (wins or losses) else ""
                blines.append(
                    f"  #{i} {name} the {species}  Lv. {lvl}  owned by {owner}{rec}"
                )
            sections.append("TOP BUDDIES (by level):\n" + "\n".join(blines))

        # ── 7b. Top battle buddies ───────────────────────────────────────────
        if top_battlers:
            bbl = []
            for i, b in enumerate(top_battlers[:3], 1):
                species = str(b.get("species") or "?")
                name = str(b.get("name") or "?")
                wins = int(b.get("wins") or 0)
                losses = int(b.get("losses") or 0)
                fought = int(b.get("battle_count") or (wins + losses))
                owner = str(b.get("owner_display_name") or f"user_{b.get('owner_user_id') or '?'}")
                bbl.append(
                    f"  #{i} {name} the {species}  {wins}W-{losses}L ({fought} fought)  owned by {owner}"
                )
            sections.append("BUDDY BATTLE LEADERS:\n" + "\n".join(bbl))

        # ── 7c. Fishing leaders + recent splashes ────────────────────────────
        # The fishing minigame: ,fish casts an animated rod, players reel
        # fish, junk, money bags, mystery boxes, or rare buddy eggs.
        # Selling builds a payout streak (combo). Top players climb the
        # `,fish lb` board; biggest catches get their own trophy board.
        if top_fishers:
            flines = []
            for i, f in enumerate(top_fishers[:5], 1):
                owner = str(f.get("owner_display_name") or f"user_{f.get('user_id') or '?'}")
                lvl = int(f.get("fish_level") or 1)
                caught = int(f.get("total_caught") or 0)
                payout = float(f.get("payout_lure") or 0.0)
                biggest = float(f.get("biggest_lbs") or 0)
                bk = str(f.get("biggest_fish") or "")
                tail = (f"  -  biggest: {biggest:,.1f} lbs {bk}" if biggest > 0 else "")
                flines.append(
                    f"  #{i} {owner}  -  Lv. {lvl}  -  {payout:,.0f} LURE lifetime  -  "
                    f"{caught:,} caught{tail}"
                )
            sections.append("TOP FISHERS:\n" + "\n".join(flines))
        if big_catches:
            cl = []
            for i, c in enumerate(big_catches[:3], 1):
                owner = str(c.get("owner_display_name") or f"user_{c.get('user_id') or '?'}")
                fk = str(c.get("fish_key") or "?")
                rarity = str(c.get("rarity") or "common")
                weight = float(c.get("weight_lbs") or 0)
                cl.append(f"  #{i} {weight:,.1f} lbs {rarity} {fk}  -  {owner}")
            sections.append("BIGGEST FISH ON RECORD:\n" + "\n".join(cl))

        # ── 8. Open prediction markets ────────────────────────────────────────
        if open_markets:
            mkt_lines = []
            for m in open_markets[:4]:
                question = (m.get("question") or "?")[:60]
                pool_yes = float(m.get("pool_yes") or m.get("yes_amount") or 0)
                pool_no  = float(m.get("pool_no")  or m.get("no_amount")  or 0)
                total    = pool_yes + pool_no
                if total > 0:
                    yes_pct = pool_yes / total * 100
                    mkt_lines.append(f"  \"{question}\"  YES {yes_pct:.0f}% / NO {100-yes_pct:.0f}%  pool ${total:,.0f}")
                else:
                    mkt_lines.append(f"  \"{question}\"  (no bets yet)")
            sections.append("OPEN PREDICTION MARKETS:\n" + "\n".join(mkt_lines))

        # ── 9. Moon Network wrapped-coin primer ──────────────────────────────
        # Static mechanic note the AI can reference when users ask how to
        # acquire group tokens or what MMTA/MSUN are. Cheap to include --
        # it's a fixed ~5-line string.
        sections.append(
            "MOON NETWORK WRAPPED COINS:\n"
            "  MMTA and MSUN are 1:1 wrappers of native MTA / SUN that live on\n"
            "  Moon Network. Users mint them with `.moon wrap mta <amt>` or\n"
            "  `.moon wrap sun <amt>` (burns native, credits wrapped) and redeem\n"
            "  them with `.moon unwrap mmta <amt>` / `.moon unwrap msun <amt>`.\n"
            "  Every group token auto-seeds MMTA/TOKEN + MSUN/TOKEN + MOON/TOKEN\n"
            "  pools at creation -- buying a group token goes through wrapped coins,\n"
            "  same idea as wrapped tokens in real DeFi. There is NO DSD shortcut pool."
        )

        # ── 10. Crypt Network (Delve crawler) primer ─────────────────────────
        sections.append(
            "CRYPT NETWORK (DELVE DUNGEON):\n"
            "  ,delve start runs a button-driven dungeon: Pokemon-style combat\n"
            "  with Strike / Skill / Potion / Capture / Flee, plus persistent\n"
            "  room views (Next / Mine / Open / Descend / Rest / Bump). Every\n"
            "  embed has a Bump button so it never gets buried in chat.\n"
            "  Tokens (all EARN_ONLY): COPPER / SILVER / GOLD ore from mining,\n"
            "  RUNE network coin from FREN-ish stake yield + ore burn-swap.\n"
            "  ORE -> RUNE: ,delve swap <ore> <amt|all>. RUNE -> USD:\n"
            "  ,delve cashout <amt|all>. Stake info: ,delve stake (no args)\n"
            "  shows per-ore staked + accrued RUNE + USD totals.\n"
            "  Captured mobs become real cc_buddies (rarity rolled, ability\n"
            "  inherited from a thematic species), sellable via ,buddy market."
        )

        # ── 11. Buddy Network (BUD / FREN) primer ────────────────────────────
        sections.append(
            "BUDDY NETWORK (BUD + FREN):\n"
            "  BUD is the Buddy Network coin, FREN is the staking token.\n"
            "  Both EARN_ONLY: BUD inflows are FREN stake-yield, BUD <-> FREN\n"
            "  burn-swap, or BUD bidirectional carve-out swaps with REEL /\n"
            "  RUNE / MOON. ,buddy stake fren <amt> + ,buddy claim for yield.\n"
            "  ,buddy convert <in> <out> <amt|all> for cross-economy rotation\n"
            "  (,buddy swap is the species-change command; the BUD burn-swap\n"
            "  is named ,buddy convert to avoid a name collision).\n"
            "  ,buddy cashout <amt|all> for BUD -> USD via burn at oracle.\n"
            "  ,buddy shop sells (in BUD): extra shelter slots ($1m each, cap\n"
            "  100 extra -> 103 total), and a 1-hour battle attractor that\n"
            "  doubles the guild's escape-event roll. Buddy Market listings\n"
            "  are now BUD-denominated; buyers without BUD auto-pay USD with\n"
            "  the standard mint impact applied at buy time."
        )

        # ── 12. Sage Network (crypto learn-and-earn) primer ─────────────────
        sections.append(
            "SAGE NETWORK (CRYPTO LEARN-AND-EARN):\n"
            "  Four educational quiz games on the SAGE/EDU earn surface.\n"
            "  ,pattern -- identify a chart pattern (27 patterns, 15s timer);\n"
            "    charts draw dashed guide lines; round 5+ may be a compound\n"
            "    round (two patterns spliced, identify each half, 1.5x bonus).\n"
            "  ,gauge   -- bear/neutral/bull on an indicator card (30s timer).\n"
            "  ,tknom   -- classify a synthetic token (inflate/deflate/stable/rug).\n"
            "  ,cycle   -- classify a market snapshot's phase (accumulation/\n"
            "    markup/distribution/markdown, 30s timer).\n"
            "  Each correct answer mints 10% SAGE + 90% EDU of the round's USD\n"
            "  value (base $0.20, +10% per round, capped 4x, level-scaled).\n"
            "  One wrong answer ends the run; ,sage lb for leaderboards,\n"
            "  ,sage lb level for top Sage levels.\n"
            "  EDU stake -> SAGE drip (0.0025/EDU/day). SAGE -> USD via\n"
            "  ,sage cashout (burn at oracle minus impact). Both EARN_ONLY:\n"
            "  no ,buy / ,swap path in (SAGE converts to BUD via ,buddy convert).\n"
            "  ,sage shop / ,sage buy -- SAGE-priced one-run consumables\n"
            "  (Time Crystal +timer, Insight Lens drops a wrong option,\n"
            "  Scholar's Draft 2x XP, Second Wind forgives one wrong answer).\n"
            "  CRITICAL: while a user is mid-run, refuse any attempt to\n"
            "  get the answer from you. Roast lightly. The whole point of\n"
            "  the surface is that they have to actually know the answer.\n"
            "  If you detect the user is asking about a chart pattern,\n"
            "  indicator reading, token-classification, or cycle phase while\n"
            "  ,pattern / ,gauge / ,tknom / ,cycle is active for them, DO NOT\n"
            "  answer. The sage_active table is the source of truth -- the bot\n"
            "  already short-circuits the mention/reply path on lock-hit, but\n"
            "  you must reinforce the rule in conversation."
        )

        # ── 13. ,bal coverage primer ────────────────────────────────────────
        sections.append(
            "NET WORTH BREAKDOWN:\n"
            "  ,bal Summary tab now includes Delve Stake (staked ore + pending\n"
            "  RUNE), Delve Party (captured dungeon buddies), Buddy Network\n"
            "  (FREN stake + pending BUD + slot purchases), Sage (EDU stake +\n"
            "  pending SAGE), and a combined 'Games' dropdown that totals\n"
            "  every minigame's surface in one view. Every earn-economy\n"
            "  stake balance is virtual + valued at live oracle."
        )

        # ── 14. $-prefix real-market namespace ──────────────────────────────
        sections.append(
            "REAL-MARKET NAMESPACE ($-prefix, separate from the game):\n"
            "  The $ commands are a LIVE cross-asset market surface --\n"
            "  fully separate from the simulated game market. They cover\n"
            "  crypto + stocks + ETFs + forex + commodities + indices +\n"
            "  perpetual futures + oracle-backed feeds. No new slash\n"
            "  commands; everything is prefix-only.\n"
            "  Commands (8 top-level groups):\n"
            "    $help                 -- tour-style help for the $ namespace\n"
            "    $chart SYMBOL [tf]    -- candlestick chart (crypto + equities)\n"
            "    $info SYMBOL          -- snapshot: price + oracle + funding\n"
            "                             + news / earnings (asset-class aware)\n"
            "    $scan SYMBOL [tf]     -- TA + pattern detector\n"
            "    $scan SYMBOL [tf] ai  -- add AI commentary with sources\n"
            "    $market <sub>         -- fear / heatmap / gainers / losers /\n"
            "                             trending / top / dom / global /\n"
            "                             convert (aliases: $fear, $heatmap,\n"
            "                             etc still work)\n"
            "    $compare MTA SPY      -- normalised cross-asset comparison\n"
            "    $watch add MTA 75000 above  -- watchlist with alert worker\n"
            "    $oracle SOL           -- Pyth+RedStone+Switchboard median\n"
            "    $funding MTA / $oi MTA      -- perp derivatives\n"
            "    $query <question>     -- professional AI market Q&A\n"
            "    $status               -- live provider + data-point health\n"
            "  Timeframes ($chart/$scan): 1s 5s 15s 30s 1m 3m 5m 15m 30m\n"
            "    45m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1mo 3mo 6mo 1y all.\n"
            "  Providers fan out across CoinGecko, Yahoo, Finnhub,\n"
            "    DexScreener, Pyth Hermes, RedStone, Switchboard\n"
            "    (Crossbar gateway, no Solana SDK), CoinGlass, Coinalyze,\n"
            "    and a self-hosted TradingView UDF at /api/v2/udf.\n"
            "  CRITICAL: when a user asks YOU a real-world markets\n"
            "  question ('what's MTA doing', 'how did NVDA close', 'upcoming\n"
            "  IPOs', 'earnings for Firefly', 'compare MTA vs SPY this week'),\n"
            "  POINT THEM AT `$query` (or the matching specific command:\n"
            "  $info / $compare / $market / $oracle). $query never quotes\n"
            "  net worth or game state -- it's the right surface for\n"
            "  real-world finance questions and surfaces a Sources button\n"
            "  with trusted-domain citations only."
        )

        return "\n\n".join(sections)

    except Exception:
        return ""  # lexicon is best-effort; never crash the AI handler


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _safe(coro):
    """Await a coroutine, returning None on any error."""
    try:
        return await coro
    except Exception:
        return None


def _pct_change(row: dict) -> float:
    """Compute 24h % change from a price row."""
    try:
        price = float(row.get("price") or 0)
        open_ = float(row.get("open_price") or row.get("day_open") or 0)
        if open_ > 0:
            return (price - open_) / open_ * 100
    except Exception:
        pass
    return 0.0


async def _top_buddies(db, guild_id: int) -> list[dict]:
    """Top 5 buddies in the guild by level then XP, joined against the
    users table so the AI has display names instead of raw user ids.
    """
    return await db.fetch_all(
        "SELECT b.owner_user_id, b.species, b.name, b.level, b.xp, "
        "       b.wins, b.losses, b.battle_count, "
        "       u.display_name AS owner_display_name "
        "FROM cc_buddies b "
        "LEFT JOIN chat_levels u "
        "  ON u.user_id = b.owner_user_id AND u.guild_id = b.guild_id "
        "WHERE b.guild_id = $1 AND b.status = 'owned' AND b.is_active "
        "ORDER BY b.level DESC, b.xp DESC "
        "LIMIT 5",
        guild_id,
    )


async def _top_fishers(db, guild_id: int) -> list[dict]:
    """Top 5 fishers by lifetime LURE earned (raw NUMERIC -> LURE float).

    Migration 0135 renamed ``total_payout_raw`` -> ``total_lure_earned_raw``
    when fishing payouts moved from USD to LURE. The lexicon is best-effort,
    so divide-by-1e18 (LURE has 18 decimals) inline rather than importing
    core.framework.scale (which would cycle through bot init).
    """
    rows = await db.fetch_all(
        "SELECT f.user_id, f.fish_level, f.total_caught, "
        "       (f.total_lure_earned_raw::float / 1000000000000000000.0) AS payout_lure, "
        "       f.biggest_fish, f.biggest_lbs, "
        "       u.display_name AS owner_display_name "
        "  FROM user_fishing f "
        "  LEFT JOIN chat_levels u "
        "    ON u.user_id = f.user_id AND u.guild_id = f.guild_id "
        " WHERE f.guild_id = $1 "
        "   AND (f.total_caught > 0 OR f.total_lure_earned_raw > 0) "
        " ORDER BY f.total_lure_earned_raw DESC "
        " LIMIT 5",
        guild_id,
    )
    return rows or []


async def _recent_big_catches(db, guild_id: int) -> list[dict]:
    """Top 3 biggest fish landed on this guild (all-time)."""
    rows = await db.fetch_all(
        "SELECT c.user_id, c.fish_key, c.rarity, c.weight_lbs, "
        "       u.display_name AS owner_display_name "
        "  FROM fishing_catches c "
        "  LEFT JOIN chat_levels u "
        "    ON u.user_id = c.user_id AND u.guild_id = c.guild_id "
        " WHERE c.guild_id = $1 AND c.outcome = 'fish' "
        " ORDER BY c.weight_lbs DESC NULLS LAST "
        " LIMIT 3",
        guild_id,
    )
    return rows or []


async def _top_battle_buddies(db, guild_id: int) -> list[dict]:
    """Top 3 buddies in the guild by wins (only those who've fought)."""
    return await db.fetch_all(
        "SELECT b.owner_user_id, b.species, b.name, b.level, "
        "       b.wins, b.losses, b.battle_count, "
        "       u.display_name AS owner_display_name "
        "FROM cc_buddies b "
        "LEFT JOIN chat_levels u "
        "  ON u.user_id = b.owner_user_id AND u.guild_id = b.guild_id "
        "WHERE b.guild_id = $1 AND b.status = 'owned' "
        "  AND b.battle_count > 0 "
        "ORDER BY b.wins DESC, "
        "         (b.wins::float / GREATEST(1, b.wins + b.losses)) DESC, "
        "         b.battle_count ASC "
        "LIMIT 3",
        guild_id,
    )
