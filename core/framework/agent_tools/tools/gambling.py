"""
core/framework/agent_tools/tools/gambling.py -- gambling tools.

    gambling.odds    house edge, payout ratios, and bet limits for all
                     games (READ).
    gambling.stats   caller's gambling history and win/loss summary (READ).
    gambling.play    execute a simple single-round game: coinflip, dice,
                     roulette, or slots (MUTATE).
                     Blackjack and mines are multi-step interactive games
                     that cannot be driven as a single tool call.
"""
from __future__ import annotations

import logging
import math
import random

from core.config import Config
from core.framework.scale import to_human, to_raw

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.gambling")


# -- helpers -------------------------------------------------------------------

def _slot_spin() -> tuple[list[str], bool, float]:
    """Run one slot pull. Returns (symbols, won, multiplier)."""
    _SLOT_SYMBOLS = ["7", "BAR", "MTA", "ARC", "DSC", "lemon", "cherry", "bell"]
    _SLOT_WEIGHTS = [1, 2, 3, 4, 5, 8, 10, 12]
    _SLOT_PAYS = {"7": 20.0, "BAR": 10.0, "MTA": 8.0, "ARC": 6.0,
                  "DSC": 5.0, "bell": 3.0, "cherry": 2.5, "lemon": 2.0}
    reels = random.choices(_SLOT_SYMBOLS, weights=_SLOT_WEIGHTS, k=3)
    if reels[0] == reels[1] == reels[2]:
        mult = _SLOT_PAYS.get(reels[0], 2.0)
        return reels, True, mult
    if reels[0] == reels[1] or reels[1] == reels[2]:
        return reels, False, 0.0
    return reels, False, 0.0


# -- gambling.odds -------------------------------------------------------------

@tool(
    name="gambling.odds",
    summary=(
        "Return house edge, payout ratios, and bet limits for every "
        "supported gambling game. Use this before recommending a game "
        "or explaining risk to a player."
    ),
    risk=RiskLevel.READ,
    category="gambling",
    params=[],
)
async def gambling_odds(ctx: ToolContext, args: dict) -> ToolResult:
    min_bet = round(to_human(Config.MIN_BET), 2)

    return ToolResult.success({
        "bet_limits": {"min_usd": min_bet, "max_usd": None},
        "games": {
            "coinflip": {
                "description": "Heads or tails. Pick correctly, win 2x your bet.",
                "house_edge_pct": 0.0,
                "win_chance_pct": 50.0,
                "payout_on_win": "2x bet (net +1x)",
                "supported_tokens": "USD or any tradeable token",
                "interactive": False,
            },
            "dice": {
                "description": "Pick a target number 2-12. Roll wins on exact match.",
                "house_edge_pct": 9.1,
                "win_chance_pct": round(1 / 11 * 100, 2),
                "payout_on_win": "6x bet (net +5x)",
                "supported_tokens": "USD only",
                "interactive": False,
            },
            "roulette": {
                "description": (
                    "Bet on red/black (1x payout, ~47% win) or a specific "
                    "number (35x payout, ~2.7% win). American wheel (0+00)."
                ),
                "bets": {
                    "red_black": {"win_chance_pct": 47.4, "payout_on_win": "2x (net +1x)"},
                    "number": {"win_chance_pct": 2.63, "payout_on_win": "36x (net +35x)"},
                },
                "house_edge_pct": 5.26,
                "supported_tokens": "USD only",
                "interactive": False,
            },
            "slots": {
                "description": "3-reel slot machine. Match 3 symbols for multiplied payout.",
                "payouts": {
                    "7 7 7": "20x", "BAR BAR BAR": "10x", "MTA MTA MTA": "8x",
                    "ARC ARC ARC": "6x", "DSC DSC DSC": "5x", "bell bell bell": "3x",
                    "cherry cherry cherry": "2.5x", "lemon lemon lemon": "2x",
                },
                "house_edge_pct": "~70% (high variance)",
                "supported_tokens": "USD only",
                "interactive": False,
            },
            "blackjack": {
                "description": "Classic 21. Interactive multi-round game (hit/stand/double).",
                "house_edge_pct": "~0.5",
                "note": "Cannot be executed as a single AI tool call -- requires Discord UI.",
                "interactive": True,
            },
            "mines": {
                "description": "Click tiles to reveal rewards; avoid mines. Multiplier scales with risk.",
                "note": "Cannot be executed as a single AI tool call -- requires Discord UI.",
                "interactive": True,
            },
        },
    })


# -- gambling.stats ------------------------------------------------------------

@tool(
    name="gambling.stats",
    summary=(
        "Return the caller's gambling history summary: total wagered, "
        "net P&L, win rate, and per-game breakdown. Use to give "
        "personalised gambling advice."
    ),
    risk=RiskLevel.READ,
    category="gambling",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
        ParamSpec("game", "str", required=False, default=None,
                  description="Filter to one game: coinflip, dice, roulette, slots, blackjack, mines."),
    ],
)
async def gambling_stats(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)
    game_filter = args.get("game") or None

    try:
        stats = await ctx.db.get_gambling_stats(uid, gid, since=None, game_type=game_filter)
    except Exception as exc:
        log.warning("[gambling.stats] db error: %s", exc)
        return ToolResult.fail(f"db_error: {exc}")

    if not stats:
        return ToolResult.success({
            "target_id": uid,
            "total_wagered_usd": 0.0,
            "net_pnl_usd": 0.0,
            "win_rate_pct": None,
            "games_played": 0,
            "breakdown": [],
        })

    # stats is list[dict] with per-game rows; aggregate totals.
    total_wagered_raw = 0
    total_pnl_raw = 0
    total_games = 0
    total_wins = 0

    breakdown = []
    for row in stats:
        g_games = int(row.get("total_games") or 0)
        g_wins = int(row.get("wins") or 0)
        wagered_raw = int(row.get("total_wagered") or 0)
        pnl_raw = int(row.get("net_pnl") or 0)

        total_games += g_games
        total_wins += g_wins
        total_wagered_raw += wagered_raw
        total_pnl_raw += pnl_raw

        breakdown.append({
            "game": row.get("game", "unknown"),
            "games_played": g_games,
            "wins": g_wins,
            "win_rate_pct": round(g_wins / g_games * 100, 1) if g_games > 0 else None,
            "wagered_usd": round(to_human(wagered_raw), 2),
            "net_pnl_usd": round(to_human(pnl_raw), 2),
        })

    total_wagered = round(to_human(total_wagered_raw), 2)
    net_pnl = round(to_human(total_pnl_raw), 2)
    win_rate = round(total_wins / total_games * 100, 1) if total_games > 0 else None

    return ToolResult.success({
        "target_id": uid,
        "total_wagered_usd": total_wagered,
        "net_pnl_usd": net_pnl,
        "win_rate_pct": win_rate,
        "games_played": total_games,
        "wins": total_wins,
        "breakdown": breakdown,
    })


# -- gambling.play -------------------------------------------------------------

@tool(
    name="gambling.play",
    summary=(
        "Execute a single-round gambling game for the caller. Supported games: "
        "coinflip, dice, roulette, slots. "
        "Returns outcome, delta, and new balance. "
        "For coinflip: pick 'heads' or 'tails'. "
        "For dice: pick a target number 2-12. "
        "For roulette: pick 'red', 'black', or a number 0-36. "
        "For slots: no choice needed, just a random pull. "
        "Blackjack and mines require Discord UI and cannot be played here."
    ),
    risk=RiskLevel.MUTATE,
    category="gambling",
    params=[
        ParamSpec("game", "str", choices=["coinflip", "dice", "roulette", "slots"],
                  description="Which game to play."),
        ParamSpec("amount", "float", min=0.0,
                  description="Bet amount in USD."),
        ParamSpec("choice", "str", required=False, default=None,
                  description=(
                      "Game-specific choice: "
                      "coinflip: 'heads' or 'tails'; "
                      "dice: target number as string e.g. '7'; "
                      "roulette: 'red', 'black', or a number '0'-'36'."
                  )),
    ],
)
async def gambling_play(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)
    game = str(args.get("game") or "").lower()
    amount = float(args.get("amount") or 0)
    choice = str(args.get("choice") or "").lower().strip()

    # Validate amount
    if math.isnan(amount) or math.isinf(amount) or amount <= 0:
        return ToolResult.fail("amount must be positive")
    min_bet = to_human(Config.MIN_BET)
    if amount < min_bet:
        return ToolResult.fail(f"minimum bet is ${min_bet:,.2f}")

    # Check USD balance
    row = await ctx.db.get_user(uid, gid)
    if not row:
        return ToolResult.fail("user_not_found")
    wallet_h = row.h("wallet")
    if amount > wallet_h + 0.005:
        return ToolResult.fail(f"insufficient_balance: have ${wallet_h:,.2f}, need ${amount:,.2f}")

    # Run the game
    won = False
    delta = 0.0
    outcome_desc = ""

    if game == "coinflip":
        if choice not in ("heads", "tails"):
            return ToolResult.fail("choice must be 'heads' or 'tails' for coinflip")
        result = random.choice(["heads", "tails"])
        won = result == choice
        delta = amount if won else -amount
        outcome_desc = f"Flipped {result}. {'WIN' if won else 'LOSS'}."

    elif game == "dice":
        try:
            target = int(choice)
        except (ValueError, TypeError):
            return ToolResult.fail("choice must be a number 2-12 for dice (e.g. '7')")
        if not 2 <= target <= 12:
            return ToolResult.fail("dice target must be between 2 and 12")
        d1 = random.randint(1, 6)
        d2 = random.randint(1, 6)
        roll = d1 + d2
        won = roll == target
        delta = amount * 5.0 if won else -amount
        outcome_desc = f"Rolled {d1}+{d2}={roll}. Target was {target}. {'WIN' if won else 'LOSS'}."

    elif game == "roulette":
        if not choice:
            return ToolResult.fail("choice required for roulette: 'red', 'black', or a number 0-36")
        # American roulette (0, 00, 1-36)
        _RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
        _BLACK = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}
        spin = random.randint(0, 37)  # 37 = "00"
        if choice in ("red", "black"):
            spin_is_red = spin in _RED
            spin_is_black = spin in _BLACK
            spin_label = str(spin) if spin <= 36 else "00"
            if choice == "red":
                won = spin_is_red
            else:
                won = spin_is_black
            delta = amount if won else -amount
            outcome_desc = f"Landed {spin_label}. {'WIN' if won else 'LOSS'}."
        else:
            try:
                num = int(choice)
            except ValueError:
                return ToolResult.fail("roulette choice must be 'red', 'black', or a number 0-36")
            if not 0 <= num <= 36:
                return ToolResult.fail("roulette number must be 0-36")
            spin_num = spin if spin <= 36 else -1  # 37="00" never matches 0-36
            won = spin_num == num
            delta = amount * 35.0 if won else -amount
            spin_label = str(spin) if spin <= 36 else "00"
            outcome_desc = f"Landed {spin_label}. Target was {num}. {'WIN' if won else 'LOSS'}."

    elif game == "slots":
        reels, won, mult = _slot_spin()
        if won:
            delta = amount * (mult - 1.0)
        else:
            delta = -amount
        outcome_desc = f"Reels: {' | '.join(reels)}. {'WIN x' + str(mult) if won else 'LOSS'}."

    else:
        return ToolResult.fail(f"unsupported game: {game!r}. Use coinflip, dice, roulette, or slots.")

    # Apply wallet delta
    delta_raw = to_raw(delta)
    new_wallet_raw = await ctx.db.update_wallet(uid, gid, delta_raw)
    new_wallet = to_human(int(new_wallet_raw)) if new_wallet_raw is not None else None

    # Log tx
    try:
        payout = max(0.0, amount + delta)
        tx_hash = await ctx.db.log_tx(
            gid, uid, f"GAMBLE_{game.upper()}",
            symbol_in="USD", amount_in=to_raw(amount),
            symbol_out="USD", amount_out=to_raw(payout),
            network="usd",
        )
    except Exception:
        tx_hash = ""

    # Publish bus event
    if ctx.bus:
        try:
            await ctx.bus.publish(
                "gamble_result",
                guild_id=gid, user_id=uid,
                game=game, token="USD",
                bet=amount, delta=delta, won=won,
                tx_hash=tx_hash,
            )
        except Exception:
            pass

    return ToolResult.success({
        "game": game,
        "bet_usd": amount,
        "choice": choice or None,
        "outcome": outcome_desc,
        "won": won,
        "delta_usd": round(delta, 2),
        "new_wallet_usd": round(new_wallet, 2) if new_wallet is not None else None,
        "tx_hash": tx_hash,
    })
