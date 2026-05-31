"""Games router -- mini-games and gambling endpoints for Discoin v2."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_current_user, get_db, get_redis, require_module
from api.v2.exceptions import (
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from api.v2.schemas.game import (
    BlackjackAction,
    BlackjackStartRequest,
    CoinflipOptions,
    CrashCashoutRequest,
    CrashJoinRequest,
    CrashState,
    DiceOptions,
    GameLeaderboardEntry,
    GamePlayRequest,
    GameResult,
    GameSession,
    GameStats,
    MinesCashout,
    MinesReveal,
    MinesStartRequest,
    PlinkoOptions,
    ProvablyFairData,
    RouletteOptions,
    WheelOptions,
)

router = APIRouter(prefix="/games", tags=["games"], dependencies=[require_module("gambling", "games")])

# ---------------------------------------------------------------------------
# Provably-fair helpers
# ---------------------------------------------------------------------------

def _generate_server_seed() -> str:
    """Generate a cryptographic server seed."""
    return secrets.token_hex(32)


def _hash_seed(seed: str) -> str:
    """SHA-256 hash of a seed."""
    return hashlib.sha256(seed.encode()).hexdigest()


def _provably_fair_roll(server_seed: str, client_seed: str, nonce: int) -> float:
    """Return a provably-fair float in [0, 1) using HMAC-SHA256."""
    message = f"{client_seed}:{nonce}"
    h = hmac.new(server_seed.encode(), message.encode(), hashlib.sha256).hexdigest()
    # Use first 8 hex chars (32 bits) to derive a float
    return int(h[:8], 16) / 0x100000000


def _provably_fair_int(server_seed: str, client_seed: str, nonce: int, max_val: int) -> int:
    """Return a provably-fair int in [0, max_val)."""
    roll = _provably_fair_roll(server_seed, client_seed, nonce)
    return int(roll * max_val)


# ---------------------------------------------------------------------------
# Balance helpers
# ---------------------------------------------------------------------------

async def _check_game_lockout(conn: asyncpg.Connection, user_id: int, guild_id: int) -> None:
    """Anti-bot lockout check  -  currently disabled. Always passes."""
    return


async def _debit_wallet(conn: asyncpg.Connection, user_id: int, guild_id: int, amount: float) -> float:
    """Debit user wallet atomically. Returns new balance in human units. Raises on insufficient funds.

    Also checks for anti-bot game lockout before allowing any wager.
    Uses a single UPDATE ... WHERE wallet >= amount to prevent race conditions
    where two concurrent requests both pass a balance check then both deduct.
    This mirrors the bot-side pattern in database/users.py:update_wallet().
    amount is human (e.g. 10.0 for $10); converted to raw NUMERIC(36,0) before DB write.
    """
    from core.framework.scale import to_raw, to_human
    amount_raw = to_raw(amount)
    await _check_game_lockout(conn, user_id, guild_id)
    row = await conn.fetchrow(
        "UPDATE users SET wallet = wallet - $1 "
        "WHERE user_id = $2 AND guild_id = $3 AND wallet >= $1 "
        "RETURNING wallet",
        amount_raw, user_id, guild_id,
    )
    if row is None:
        raise InsufficientBalanceError("Insufficient wallet balance for this bet.")
    return to_human(int(row["wallet"]))


async def _credit_wallet(conn: asyncpg.Connection, user_id: int, guild_id: int, amount: float) -> float:
    """Credit user wallet. Returns new balance in human units.
    amount is human (e.g. 10.0 for $10); converted to raw NUMERIC(36,0) before DB write.
    """
    from core.framework.scale import to_raw, to_human
    amount_raw = to_raw(amount)
    await conn.execute(
        "UPDATE users SET wallet = wallet + $1 WHERE user_id = $2 AND guild_id = $3",
        amount_raw, user_id, guild_id,
    )
    # Fetch the new balance
    post = await conn.fetchrow(
        "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    return to_human(int(post["wallet"])) if post else 0.0


async def _record_game(
    conn: asyncpg.Connection,
    guild_id: int,
    user_id: int,
    game_type: str,
    bet_amount: float,
    payout: float,
    profit: float,
    multiplier: float,
    result_data: dict,
    server_seed: str,
    client_seed: str,
    nonce: int,
) -> int:
    """Insert a game_results row and update user_profiles. Returns the new game_id.
    bet_amount, payout, profit are human floats; converted to raw NUMERIC(36,0) before insertion.
    """
    from core.framework.scale import to_raw
    bet_raw    = to_raw(bet_amount)
    payout_raw = to_raw(payout)
    profit_raw = to_raw(profit)
    row = await conn.fetchrow(
        """INSERT INTO game_results
               (guild_id, user_id, game_type, bet_amount, payout, profit, multiplier,
                result_data, server_seed, client_seed, nonce)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
           RETURNING id""",
        guild_id, user_id, game_type, bet_raw, payout_raw, profit_raw, multiplier,
        json.dumps(result_data), server_seed, client_seed, nonce,
    )
    if row is not None:
        # PostgreSQL path -- RETURNING worked
        game_id = row["id"]
    else:
        # SQLite path -- RETURNING was stripped; fetch the last inserted rowid
        game_id = await conn.fetchval("SELECT last_insert_rowid()")

    # Update aggregated profile stats
    win_inc = 1 if profit > 0 else 0
    loss_inc = 1 if profit < 0 else 0
    await conn.execute(
        """INSERT INTO user_profiles (user_id, guild_id, total_games, total_wagered, total_game_profit, win_count, loss_count)
           VALUES ($1, $2, 1, $3, $4, $5, $6)
           ON CONFLICT (user_id, guild_id) DO UPDATE SET
               total_games = user_profiles.total_games + 1,
               total_wagered = user_profiles.total_wagered + $3,
               total_game_profit = user_profiles.total_game_profit + $4,
               win_count = user_profiles.win_count + $5,
               loss_count = user_profiles.loss_count + $6""",
        user_id, guild_id, bet_raw, profit_raw, win_inc, loss_inc,
    )
    return game_id


# ---------------------------------------------------------------------------
# Card / deck helpers (for blackjack)
# ---------------------------------------------------------------------------

SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


def _card_value(rank: str) -> int:
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def _hand_value(hand: list[dict]) -> int:
    total = sum(_card_value(c["rank"]) for c in hand)
    aces = sum(1 for c in hand if c["rank"] == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def _deal_card(deck: list[dict]) -> dict:
    return deck.pop()


def _new_deck(server_seed: str, client_seed: str, nonce: int) -> list[dict]:
    """Create a shuffled deck using provably-fair seeding."""
    deck = [{"rank": r, "suit": s} for s in SUITS for r in RANKS]
    # Fisher-Yates shuffle using HMAC-based random
    for i in range(len(deck) - 1, 0, -1):
        j = _provably_fair_int(server_seed, client_seed, nonce + i, i + 1)
        deck[i], deck[j] = deck[j], deck[i]
    return deck


def _visible_hand(hand: list[dict]) -> list[str]:
    return [f"{c['rank']}{c['suit'][0].upper()}" for c in hand]


# ============================================================================
# 1. COINFLIP
# ============================================================================

@router.post("/coinflip/play", response_model=GameResult, summary="Play coinflip")
async def play_coinflip(
    body: GamePlayRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Flip a coin -- pick heads or tails. Pays 1.96x on win (2% house edge)."""
    opts = CoinflipOptions(**body.options)
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)

    roll = _provably_fair_roll(server_seed, client_seed, nonce)
    outcome = "heads" if roll < 0.5 else "tails"
    won = outcome == opts.choice

    multiplier = 1.96 if won else 0.0
    payout = body.bet_amount * multiplier
    profit = payout - body.bet_amount

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        if payout > 0:
            await _credit_wallet(conn, user_id, guild_id, payout)
        game_id = await _record_game(
            conn, guild_id, user_id, "coinflip", body.bet_amount, payout, profit, multiplier,
            {"choice": opts.choice, "outcome": outcome, "won": won},
            server_seed, client_seed, nonce,
        )

    return GameResult(
        game_id=game_id, game_type="coinflip", bet_amount=body.bet_amount,
        payout=payout, profit=profit, multiplier=multiplier,
        result_data={"choice": opts.choice, "outcome": outcome, "won": won},
    )


# ============================================================================
# 2. SLOTS
# ============================================================================

SLOT_SYMBOLS = ["cherry", "lemon", "orange", "plum", "bell", "bar", "seven"]
# Consistent with Discord bot: 5x for three-of-a-kind, 1.5x for pairs
from constants.games import (
    SLOT_THREE_OF_A_KIND_PAYOUT as SLOT_THREE_OF_A_KIND,
    SLOT_PAIR_PAYOUT as SLOT_PAIR,
)


@router.post("/slots/play", response_model=GameResult, summary="Play slots")
async def play_slots(
    body: GamePlayRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Spin the slot machine. Three matching symbols pay out multiplied bet."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)

    reels = []
    for i in range(3):
        idx = _provably_fair_int(server_seed, client_seed, nonce + i, len(SLOT_SYMBOLS))
        reels.append(SLOT_SYMBOLS[idx])

    # Three of a kind: 5x, pair: 1.5x (consistent with Discord bot)
    if reels[0] == reels[1] == reels[2]:
        multiplier = SLOT_THREE_OF_A_KIND
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        multiplier = SLOT_PAIR
    else:
        multiplier = 0.0

    payout = body.bet_amount * multiplier
    profit = payout - body.bet_amount

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        if payout > 0:
            await _credit_wallet(conn, user_id, guild_id, payout)
        game_id = await _record_game(
            conn, guild_id, user_id, "slots", body.bet_amount, payout, profit, multiplier,
            {"reels": reels, "won": profit > 0},
            server_seed, client_seed, nonce,
        )

    is_jackpot = reels[0] == reels[1] == reels[2] == "seven"
    return GameResult(
        game_id=game_id, game_type="slots", bet_amount=body.bet_amount,
        payout=payout, profit=profit, multiplier=multiplier,
        result_data={"reels": reels, "won": profit > 0, "jackpot": is_jackpot},
    )


# ============================================================================
# 3. DICE
# ============================================================================

@router.post("/dice/play", response_model=GameResult, summary="Play dice")
async def play_dice(
    body: GamePlayRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Roll a dice (1-100). Bet over or under a target number.

    Payout multiplier = 99 / win_chance (1% house edge).
    """
    opts = DiceOptions(**body.options)
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)

    roll_value = _provably_fair_int(server_seed, client_seed, nonce, 100) + 1  # 1-100

    if opts.over_under == "over":
        win_chance = 100 - opts.target
        won = roll_value > opts.target
    else:
        win_chance = opts.target - 1
        won = roll_value < opts.target

    if win_chance <= 0 or win_chance >= 100:
        raise ValidationError("Invalid target -- win chance must be between 1% and 99%.")

    multiplier = round(99.0 / win_chance, 4) if won else 0.0
    payout = round(body.bet_amount * multiplier, 8)
    profit = round(payout - body.bet_amount, 8)

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        if payout > 0:
            await _credit_wallet(conn, user_id, guild_id, payout)
        game_id = await _record_game(
            conn, guild_id, user_id, "dice", body.bet_amount, payout, profit, multiplier,
            {"target": opts.target, "over_under": opts.over_under, "roll": roll_value, "won": won, "win_chance": win_chance},
            server_seed, client_seed, nonce,
        )

    return GameResult(
        game_id=game_id, game_type="dice", bet_amount=body.bet_amount,
        payout=payout, profit=profit, multiplier=multiplier,
        result_data={"target": opts.target, "over_under": opts.over_under, "roll": roll_value, "won": won},
    )


# ============================================================================
# 4. ROULETTE
# ============================================================================

ROULETTE_REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_BLACKS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}


def _roulette_multiplier(spin: int, bet_type: str, bet_value: str) -> float:
    """Calculate roulette payout multiplier for a given spin result."""
    bet_value_lower = bet_value.lower()

    if bet_type == "number":
        return 36.0 if spin == int(bet_value) else 0.0
    elif bet_type == "color":
        if bet_value_lower == "red" and spin in ROULETTE_REDS:
            return 2.0
        if bet_value_lower == "black" and spin in ROULETTE_BLACKS:
            return 2.0
        return 0.0
    elif bet_type == "parity":
        if spin == 0:
            return 0.0
        if bet_value_lower == "even" and spin % 2 == 0:
            return 2.0
        if bet_value_lower == "odd" and spin % 2 == 1:
            return 2.0
        return 0.0
    elif bet_type == "half":
        if spin == 0:
            return 0.0
        if bet_value_lower == "low" and 1 <= spin <= 18:
            return 2.0
        if bet_value_lower == "high" and 19 <= spin <= 36:
            return 2.0
        return 0.0
    elif bet_type == "dozen":
        if spin == 0:
            return 0.0
        if bet_value_lower == "1st" and 1 <= spin <= 12:
            return 3.0
        if bet_value_lower == "2nd" and 13 <= spin <= 24:
            return 3.0
        if bet_value_lower == "3rd" and 25 <= spin <= 36:
            return 3.0
        return 0.0
    elif bet_type == "column":
        if spin == 0:
            return 0.0
        col = ((spin - 1) % 3) + 1
        if str(col) == bet_value:
            return 3.0
        return 0.0
    return 0.0


@router.post("/roulette/play", response_model=GameResult, summary="Play roulette")
async def play_roulette(
    body: GamePlayRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Spin the roulette wheel. Supports number, color, parity, half, dozen, column bets."""
    opts = RouletteOptions(**body.options)
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    if opts.bet_type == "number":
        try:
            num = int(opts.bet_value)
            if num < 0 or num > 36:
                raise ValueError
        except ValueError:
            raise ValidationError("bet_value for 'number' must be 0-36.")

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)

    spin = _provably_fair_int(server_seed, client_seed, nonce, 37)  # 0-36
    multiplier = _roulette_multiplier(spin, opts.bet_type, opts.bet_value)
    payout = round(body.bet_amount * multiplier, 8)
    profit = round(payout - body.bet_amount, 8)

    color = "green" if spin == 0 else ("red" if spin in ROULETTE_REDS else "black")

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        if payout > 0:
            await _credit_wallet(conn, user_id, guild_id, payout)
        game_id = await _record_game(
            conn, guild_id, user_id, "roulette", body.bet_amount, payout, profit, multiplier,
            {"spin": spin, "color": color, "bet_type": opts.bet_type, "bet_value": opts.bet_value, "won": profit > 0},
            server_seed, client_seed, nonce,
        )

    return GameResult(
        game_id=game_id, game_type="roulette", bet_amount=body.bet_amount,
        payout=payout, profit=profit, multiplier=multiplier,
        result_data={"spin": spin, "color": color, "bet_type": opts.bet_type, "bet_value": opts.bet_value, "won": profit > 0},
    )


# ============================================================================
# 5. BLACKJACK (stateful)
# ============================================================================

@router.post("/blackjack/start", response_model=GameSession, summary="Start blackjack")
async def blackjack_start(
    body: BlackjackStartRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Start a new blackjack session. Deals two cards to player and dealer."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)
    deck = _new_deck(server_seed, client_seed, nonce)

    player_hand = [_deal_card(deck), _deal_card(deck)]
    dealer_hand = [_deal_card(deck), _deal_card(deck)]

    state = {
        "deck": deck,
        "player_hand": player_hand,
        "dealer_hand": dealer_hand,
        "server_seed": server_seed,
        "client_seed": client_seed,
        "nonce": nonce,
    }

    # Check for natural blackjack
    player_val = _hand_value(player_hand)
    status = "active"
    if player_val == 21:
        status = "completed"

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        session_id = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO game_sessions (id, guild_id, user_id, game_type, bet_amount, state, status, expires_at)
               VALUES ($1, $2, $3, 'blackjack', $4, $5, $6, now() + interval '10 minutes')""",
            session_id, guild_id, user_id, body.bet_amount, json.dumps(state), status,
        )

    # If natural blackjack, auto-complete
    if status == "completed":
        dealer_val = _hand_value(dealer_hand)
        if dealer_val == 21:
            # Push  -  both have blackjack
            multiplier, payout = 1.0, body.bet_amount
        else:
            # Natural blackjack pays 2.5x (standard 3:2 + original bet)
            multiplier, payout = 2.5, body.bet_amount * 2.5
        profit = payout - body.bet_amount
        async with conn.transaction():
            if payout > 0:
                await _credit_wallet(conn, user_id, guild_id, payout)
            await _record_game(
                conn, guild_id, user_id, "blackjack", body.bet_amount, payout, profit, multiplier,
                {"player_hand": _visible_hand(player_hand), "dealer_hand": _visible_hand(dealer_hand),
                 "player_value": player_val, "dealer_value": dealer_val, "outcome": "blackjack" if multiplier > 1 else "push"},
                server_seed, client_seed, nonce,
            )
            await conn.execute("UPDATE game_sessions SET status = 'completed' WHERE id = $1", session_id)

    # Visible state: player sees their cards and dealer's first card
    visible_state = {
        "player_hand": _visible_hand(player_hand),
        "player_value": player_val,
        "dealer_showing": _visible_hand(dealer_hand[:1]),
        "dealer_value_showing": _card_value(dealer_hand[0]["rank"]),
    }
    if status == "completed":
        visible_state["dealer_hand"] = _visible_hand(dealer_hand)
        visible_state["dealer_value"] = _hand_value(dealer_hand)

    return GameSession(
        session_id=session_id, game_type="blackjack", bet_amount=body.bet_amount,
        state=visible_state, status=status,
    )


@router.post("/blackjack/action", response_model=GameSession, summary="Blackjack action")
async def blackjack_action(
    body: BlackjackAction,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Take an action (hit, stand, double) in an active blackjack session."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Lock the session row to prevent race conditions from concurrent requests
    row = await conn.fetchrow(
        "SELECT * FROM game_sessions WHERE id = $1 AND user_id = $2 AND guild_id = $3 AND status = 'active' FOR UPDATE",
        body.session_id, user_id, guild_id,
    )
    if not row:
        raise NotFoundError("Active blackjack session not found.")

    state = json.loads(row["state"]) if isinstance(row["state"], str) else row["state"]
    deck = state["deck"]
    player_hand = state["player_hand"]
    dealer_hand = state["dealer_hand"]
    from core.framework.scale import to_human as _to_human_g
    bet_amount = _to_human_g(int(row["bet_amount"]))

    if body.action == "double":
        # Double down: take one more card, double the bet (inside transaction for safety)
        async with conn.transaction():
            await _debit_wallet(conn, user_id, guild_id, bet_amount)
        bet_amount *= 2
        player_hand.append(_deal_card(deck))
        # Must stand after double
        body.action = "stand"

    elif body.action == "hit":
        player_hand.append(_deal_card(deck))

    player_val = _hand_value(player_hand)

    # Check bust
    if player_val > 21:
        # Player busts
        multiplier, payout, profit = 0.0, 0.0, -bet_amount
        async with conn.transaction():
            await _record_game(
                conn, guild_id, user_id, "blackjack", bet_amount, payout, profit, multiplier,
                {"player_hand": _visible_hand(player_hand), "dealer_hand": _visible_hand(dealer_hand),
                 "player_value": player_val, "dealer_value": _hand_value(dealer_hand), "outcome": "bust"},
                state["server_seed"], state["client_seed"], state["nonce"],
            )
            await conn.execute(
                "UPDATE game_sessions SET status = 'completed', bet_amount = $2, state = $3 WHERE id = $1",
                body.session_id, bet_amount, json.dumps(state),
            )
        return GameSession(
            session_id=body.session_id, game_type="blackjack", bet_amount=bet_amount,
            state={"player_hand": _visible_hand(player_hand), "player_value": player_val,
                   "dealer_hand": _visible_hand(dealer_hand), "dealer_value": _hand_value(dealer_hand),
                   "outcome": "bust"},
            status="completed",
        )

    if body.action == "stand" or player_val == 21:
        # Dealer plays
        dealer_val = _hand_value(dealer_hand)
        while dealer_val < 17:
            dealer_hand.append(_deal_card(deck))
            dealer_val = _hand_value(dealer_hand)

        if dealer_val > 21 or player_val > dealer_val:
            outcome = "win"
            multiplier = 2.0
        elif player_val == dealer_val:
            outcome = "push"
            multiplier = 1.0
        else:
            outcome = "lose"
            multiplier = 0.0

        payout = round(bet_amount * multiplier, 8)
        profit = round(payout - bet_amount, 8)

        async with conn.transaction():
            if payout > 0:
                await _credit_wallet(conn, user_id, guild_id, payout)
            await _record_game(
                conn, guild_id, user_id, "blackjack", bet_amount, payout, profit, multiplier,
                {"player_hand": _visible_hand(player_hand), "dealer_hand": _visible_hand(dealer_hand),
                 "player_value": player_val, "dealer_value": dealer_val, "outcome": outcome},
                state["server_seed"], state["client_seed"], state["nonce"],
            )
            await conn.execute(
                "UPDATE game_sessions SET status = 'completed', bet_amount = $2 WHERE id = $1",
                body.session_id, bet_amount,
            )

        return GameSession(
            session_id=body.session_id, game_type="blackjack", bet_amount=bet_amount,
            state={"player_hand": _visible_hand(player_hand), "player_value": player_val,
                   "dealer_hand": _visible_hand(dealer_hand), "dealer_value": dealer_val,
                   "outcome": outcome, "multiplier": multiplier, "payout": payout},
            status="completed",
        )

    # Still active -- save state
    state["deck"] = deck
    state["player_hand"] = player_hand
    state["dealer_hand"] = dealer_hand
    await conn.execute(
        "UPDATE game_sessions SET state = $2, bet_amount = $3 WHERE id = $1",
        body.session_id, json.dumps(state), bet_amount,
    )

    return GameSession(
        session_id=body.session_id, game_type="blackjack", bet_amount=bet_amount,
        state={"player_hand": _visible_hand(player_hand), "player_value": player_val,
               "dealer_showing": _visible_hand(dealer_hand[:1]),
               "dealer_value_showing": _card_value(dealer_hand[0]["rank"])},
        status="active",
    )


# ============================================================================
# 6. MINES (stateful)
# ============================================================================

@router.post("/mines/start", response_model=GameSession, summary="Start mines game")
async def mines_start(
    body: MinesStartRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Start a new mines game. A 5x5 grid with hidden mines."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)

    # Place mines on 5x5 grid
    total_tiles = 25
    mine_positions = set()
    i = 0
    while len(mine_positions) < body.mine_count:
        pos = _provably_fair_int(server_seed, client_seed, nonce + 1000 + i, total_tiles)
        mine_positions.add(pos)
        i += 1

    state = {
        "mine_positions": list(mine_positions),
        "mine_count": body.mine_count,
        "revealed": [],
        "server_seed": server_seed,
        "client_seed": client_seed,
        "nonce": nonce,
        "current_multiplier": 1.0,
    }

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        session_id = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO game_sessions (id, guild_id, user_id, game_type, bet_amount, state, status, expires_at)
               VALUES ($1, $2, $3, 'mines', $4, $5, 'active', now() + interval '30 minutes')""",
            session_id, guild_id, user_id, body.bet_amount, json.dumps(state),
        )

    return GameSession(
        session_id=session_id, game_type="mines", bet_amount=body.bet_amount,
        state={"grid_size": 5, "mine_count": body.mine_count, "revealed": [], "current_multiplier": 1.0},
        status="active",
    )


@router.post("/mines/reveal", response_model=GameSession, summary="Reveal mines tile")
async def mines_reveal(
    body: MinesReveal,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Reveal a tile in the mines grid. Hit a mine and lose your bet."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Lock the session row to prevent race conditions from concurrent requests
    row = await conn.fetchrow(
        "SELECT * FROM game_sessions WHERE id = $1 AND user_id = $2 AND guild_id = $3 AND status = 'active' AND game_type = 'mines' FOR UPDATE",
        body.session_id, user_id, guild_id,
    )
    if not row:
        raise NotFoundError("Active mines session not found.")

    state = json.loads(row["state"]) if isinstance(row["state"], str) else row["state"]
    from core.framework.scale import to_human as _to_human_g
    bet_amount = _to_human_g(int(row["bet_amount"]))

    tile_index = body.row * 5 + body.col
    if tile_index in state["revealed"]:
        raise ValidationError("Tile already revealed.")

    hit_mine = tile_index in state["mine_positions"]

    if hit_mine:
        # Game over - lost
        state["revealed"].append(tile_index)
        async with conn.transaction():
            await _record_game(
                conn, guild_id, user_id, "mines", bet_amount, 0.0, -bet_amount, 0.0,
                {"mine_positions": state["mine_positions"], "revealed": state["revealed"],
                 "mine_count": state["mine_count"], "hit_mine": True},
                state["server_seed"], state["client_seed"], state["nonce"],
            )
            await conn.execute("UPDATE game_sessions SET status = 'completed', state = $2 WHERE id = $1",
                               body.session_id, json.dumps(state))

        return GameSession(
            session_id=body.session_id, game_type="mines", bet_amount=bet_amount,
            state={"mine_positions": state["mine_positions"], "revealed": state["revealed"],
                   "hit_mine": True, "payout": 0.0},
            status="completed",
        )

    # Safe tile
    state["revealed"].append(tile_index)
    safe_tiles = 25 - state["mine_count"]
    revealed_safe = len(state["revealed"])

    # Multiplier: product of (total_remaining / safe_remaining) for each pick
    # Simplified: safe_tiles! / (safe_tiles - revealed_safe)! / (total_tiles! / (total_tiles - revealed_safe)!)
    # Using a simpler formula: multiplier grows based on risk
    multiplier = 1.0
    total = 25
    mines = state["mine_count"]
    for k in range(revealed_safe):
        multiplier *= total / (total - mines)
        total -= 1
    multiplier = round(multiplier * 0.95, 4)  # 5% house edge (matches Discord bot)
    state["current_multiplier"] = multiplier

    # Check if all safe tiles revealed
    if revealed_safe >= safe_tiles:
        # Auto cashout
        payout = round(bet_amount * multiplier, 8)
        profit = round(payout - bet_amount, 8)
        async with conn.transaction():
            await _credit_wallet(conn, user_id, guild_id, payout)
            await _record_game(
                conn, guild_id, user_id, "mines", bet_amount, payout, profit, multiplier,
                {"mine_positions": state["mine_positions"], "revealed": state["revealed"],
                 "mine_count": state["mine_count"], "hit_mine": False, "auto_cashout": True},
                state["server_seed"], state["client_seed"], state["nonce"],
            )
            await conn.execute("UPDATE game_sessions SET status = 'completed', state = $2 WHERE id = $1",
                               body.session_id, json.dumps(state))

        return GameSession(
            session_id=body.session_id, game_type="mines", bet_amount=bet_amount,
            state={"revealed": state["revealed"], "current_multiplier": multiplier,
                   "payout": payout, "hit_mine": False, "auto_cashout": True},
            status="completed",
        )

    await conn.execute("UPDATE game_sessions SET state = $2 WHERE id = $1",
                       body.session_id, json.dumps(state))

    return GameSession(
        session_id=body.session_id, game_type="mines", bet_amount=bet_amount,
        state={"revealed": state["revealed"], "current_multiplier": multiplier,
               "safe_revealed": revealed_safe, "remaining_safe": safe_tiles - revealed_safe},
        status="active",
    )


@router.post("/mines/cashout", response_model=GameResult, summary="Cash out mines")
async def mines_cashout(
    body: MinesCashout,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Cash out of an active mines session at the current multiplier."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Lock the session row to prevent race conditions from concurrent requests
    row = await conn.fetchrow(
        "SELECT * FROM game_sessions WHERE id = $1 AND user_id = $2 AND guild_id = $3 AND status = 'active' AND game_type = 'mines' FOR UPDATE",
        body.session_id, user_id, guild_id,
    )
    if not row:
        raise NotFoundError("Active mines session not found.")

    state = json.loads(row["state"]) if isinstance(row["state"], str) else row["state"]
    from core.framework.scale import to_human as _to_human_g
    bet_amount = _to_human_g(int(row["bet_amount"]))

    if not state["revealed"]:
        raise ValidationError("Must reveal at least one tile before cashing out.")

    multiplier = state["current_multiplier"]
    payout = round(bet_amount * multiplier, 8)
    profit = round(payout - bet_amount, 8)

    async with conn.transaction():
        await _credit_wallet(conn, user_id, guild_id, payout)
        game_id = await _record_game(
            conn, guild_id, user_id, "mines", bet_amount, payout, profit, multiplier,
            {"mine_positions": state["mine_positions"], "revealed": state["revealed"],
             "mine_count": state["mine_count"], "cashout": True},
            state["server_seed"], state["client_seed"], state["nonce"],
        )
        await conn.execute("UPDATE game_sessions SET status = 'completed' WHERE id = $1", body.session_id)

    return GameResult(
        game_id=game_id, game_type="mines", bet_amount=bet_amount,
        payout=payout, profit=profit, multiplier=multiplier,
        result_data={"mine_positions": state["mine_positions"], "revealed": state["revealed"], "cashout": True},
    )


# ============================================================================
# 7. CRASH (session-based)
# ============================================================================

@router.post("/crash/join", response_model=GameSession, summary="Join crash round")
async def crash_join(
    body: CrashJoinRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
    redis=Depends(get_redis),
):
    """Join the current crash round with a bet. The multiplier rises until it crashes."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)

    # Determine crash point using provably fair method
    roll = _provably_fair_roll(server_seed, client_seed, nonce)
    # crash_point formula: house edge ~3%
    if roll < 0.03:
        crash_point = 1.0  # Instant crash
    else:
        crash_point = round(0.97 / (1.0 - roll), 2)
    crash_point = min(crash_point, 1000.0)  # Cap at 1000x

    state = {
        "crash_point": crash_point,
        "server_seed": server_seed,
        "client_seed": client_seed,
        "nonce": nonce,
        "cashed_out": False,
        "cashout_multiplier": None,
    }

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        session_id = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO game_sessions (id, guild_id, user_id, game_type, bet_amount, state, status, expires_at)
               VALUES ($1, $2, $3, 'crash', $4, $5, 'active', now() + interval '5 minutes')""",
            session_id, guild_id, user_id, body.bet_amount, json.dumps(state),
        )

    return GameSession(
        session_id=session_id, game_type="crash", bet_amount=body.bet_amount,
        state={"status": "running", "server_seed_hash": _hash_seed(server_seed)},
        status="active",
    )


@router.post("/crash/cashout", response_model=GameResult, summary="Cash out of crash")
async def crash_cashout(
    body: CrashCashoutRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Cash out of an active crash round at a specified multiplier.

    In a real-time implementation, the server tracks the current multiplier.
    For the API version, we simulate with a random cashout point.
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Lock the session row to prevent double-cashout race conditions
    row = await conn.fetchrow(
        "SELECT * FROM game_sessions WHERE id = $1 AND user_id = $2 AND guild_id = $3 AND status = 'active' AND game_type = 'crash' FOR UPDATE",
        body.session_id, user_id, guild_id,
    )
    if not row:
        raise NotFoundError("Active crash session not found.")

    state = json.loads(row["state"]) if isinstance(row["state"], str) else row["state"]
    from core.framework.scale import to_human as _to_human_g
    bet_amount = _to_human_g(int(row["bet_amount"]))

    if state.get("cashed_out"):
        raise ValidationError("Already cashed out.")

    # Simulate a cashout at a multiplier between 1.0 and crash_point
    # In a real WebSocket-based implementation, this would be the server's current multiplier
    crash_point = state["crash_point"]

    # Use a new provably fair roll to determine what multiplier the user catches
    roll = _provably_fair_roll(state["server_seed"], state["client_seed"], state["nonce"] + 1)
    cashout_at = round(1.0 + roll * (crash_point - 1.0), 2)
    cashout_at = max(1.01, min(cashout_at, crash_point))

    if cashout_at >= crash_point:
        # Crashed before cashout
        multiplier = 0.0
        payout = 0.0
    else:
        multiplier = cashout_at
        payout = round(bet_amount * multiplier, 8)

    profit = round(payout - bet_amount, 8)

    async with conn.transaction():
        if payout > 0:
            await _credit_wallet(conn, user_id, guild_id, payout)
        game_id = await _record_game(
            conn, guild_id, user_id, "crash", bet_amount, payout, profit, multiplier,
            {"crash_point": crash_point, "cashout_at": cashout_at if multiplier > 0 else None,
             "won": multiplier > 0},
            state["server_seed"], state["client_seed"], state["nonce"],
        )
        await conn.execute("UPDATE game_sessions SET status = 'completed' WHERE id = $1", body.session_id)

    return GameResult(
        game_id=game_id, game_type="crash", bet_amount=bet_amount,
        payout=payout, profit=profit, multiplier=multiplier,
        result_data={"crash_point": crash_point, "cashout_at": cashout_at if multiplier > 0 else None, "won": multiplier > 0},
    )


@router.get("/crash/current", response_model=CrashState, summary="Current crash round")
async def crash_current(
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get the current state of the crash game round for a guild."""
    guild_id = int(user["guild_id"])
    row = await conn.fetchrow(
        """SELECT COUNT(*) as players,
                  MIN(created_at) as started
           FROM game_sessions
           WHERE guild_id = $1 AND game_type = 'crash' AND status = 'active'""",
        guild_id,
    )
    player_count = row["players"] if row else 0
    return CrashState(
        round_id=str(uuid.uuid4())[:8],
        status="running" if player_count > 0 else "waiting",
        multiplier=1.0,
        players=player_count,
        crash_point=None,
    )


# ============================================================================
# 8. PLINKO
# ============================================================================

# Plinko multiplier tables by risk level and rows
PLINKO_MULTIPLIERS = {
    "low": {
        8: [5.6, 2.1, 1.1, 1.0, 0.5, 1.0, 1.1, 2.1, 5.6],
        12: [10.0, 3.0, 1.6, 1.4, 1.1, 1.0, 0.5, 1.0, 1.1, 1.4, 1.6, 3.0, 10.0],
        16: [16.0, 9.0, 2.0, 1.4, 1.4, 1.2, 1.1, 1.0, 0.5, 1.0, 1.1, 1.2, 1.4, 1.4, 2.0, 9.0, 16.0],
    },
    "medium": {
        8: [13.0, 3.0, 1.3, 0.7, 0.4, 0.7, 1.3, 3.0, 13.0],
        12: [33.0, 11.0, 4.0, 2.0, 1.1, 0.6, 0.3, 0.6, 1.1, 2.0, 4.0, 11.0, 33.0],
        16: [110.0, 41.0, 10.0, 5.0, 3.0, 1.5, 1.0, 0.5, 0.3, 0.5, 1.0, 1.5, 3.0, 5.0, 10.0, 41.0, 110.0],
    },
    "high": {
        8: [29.0, 4.0, 1.5, 0.3, 0.2, 0.3, 1.5, 4.0, 29.0],
        12: [170.0, 24.0, 8.7, 2.0, 0.7, 0.2, 0.2, 0.2, 0.7, 2.0, 8.7, 24.0, 170.0],
        16: [1000.0, 130.0, 26.0, 9.0, 4.0, 2.0, 0.2, 0.2, 0.2, 0.2, 0.2, 2.0, 4.0, 9.0, 26.0, 130.0, 1000.0],
    },
}


@router.post("/plinko/play", response_model=GameResult, summary="Play plinko")
async def play_plinko(
    body: GamePlayRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Drop a ball through plinko pegs. Where it lands determines the multiplier."""
    opts = PlinkoOptions(**body.options)
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Find closest supported row count
    supported_rows = [8, 12, 16]
    rows = min(supported_rows, key=lambda x: abs(x - opts.rows))
    multipliers = PLINKO_MULTIPLIERS[opts.risk][rows]

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)

    # Simulate ball path: at each row it goes left or right
    position = 0
    path = []
    for i in range(rows):
        direction = _provably_fair_int(server_seed, client_seed, nonce + i, 2)
        position += direction
        path.append("R" if direction else "L")

    # position is now 0..rows, which maps to multipliers index
    bucket = min(position, len(multipliers) - 1)
    multiplier = multipliers[bucket]
    payout = round(body.bet_amount * multiplier, 8)
    profit = round(payout - body.bet_amount, 8)

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        if payout > 0:
            await _credit_wallet(conn, user_id, guild_id, payout)
        game_id = await _record_game(
            conn, guild_id, user_id, "plinko", body.bet_amount, payout, profit, multiplier,
            {"risk": opts.risk, "rows": rows, "path": path, "bucket": bucket, "won": profit > 0},
            server_seed, client_seed, nonce,
        )

    return GameResult(
        game_id=game_id, game_type="plinko", bet_amount=body.bet_amount,
        payout=payout, profit=profit, multiplier=multiplier,
        result_data={"risk": opts.risk, "rows": rows, "path": path, "bucket": bucket, "won": profit > 0},
    )


# ============================================================================
# 9. WHEEL
# ============================================================================

@router.post("/wheel/play", response_model=GameResult, summary="Spin the wheel")
async def play_wheel(
    body: GamePlayRequest,
    user: dict = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Spin a prize wheel with configurable segments."""
    opts = WheelOptions(**body.options)
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    server_seed = _generate_server_seed()
    client_seed = secrets.token_hex(16)
    nonce = secrets.randbelow(2**31)

    # Generate wheel segments with varying multipliers
    segments = []
    for i in range(opts.segments):
        # Distribution: mostly low, few high
        if i == 0:
            segments.append({"index": i, "multiplier": float(opts.segments), "label": f"{opts.segments}x"})
        elif i < 3:
            segments.append({"index": i, "multiplier": 5.0, "label": "5x"})
        elif i < 8:
            segments.append({"index": i, "multiplier": 2.0, "label": "2x"})
        elif i < 13:
            segments.append({"index": i, "multiplier": 1.5, "label": "1.5x"})
        else:
            segments.append({"index": i, "multiplier": 0.0, "label": "0x"})

    landing = _provably_fair_int(server_seed, client_seed, nonce, opts.segments)
    multiplier = segments[landing]["multiplier"]
    payout = round(body.bet_amount * multiplier, 8)
    profit = round(payout - body.bet_amount, 8)

    async with conn.transaction():
        await _debit_wallet(conn, user_id, guild_id, body.bet_amount)
        if payout > 0:
            await _credit_wallet(conn, user_id, guild_id, payout)
        game_id = await _record_game(
            conn, guild_id, user_id, "wheel", body.bet_amount, payout, profit, multiplier,
            {"segments": opts.segments, "landing": landing, "label": segments[landing]["label"], "won": profit > 0},
            server_seed, client_seed, nonce,
        )

    return GameResult(
        game_id=game_id, game_type="wheel", bet_amount=body.bet_amount,
        payout=payout, profit=profit, multiplier=multiplier,
        result_data={"segments": opts.segments, "landing": landing, "label": segments[landing]["label"], "won": profit > 0},
    )


# ============================================================================
# 10-14. STATS, LEADERBOARD, HISTORY, PROVABLY-FAIR
# ============================================================================

@router.get("/stats", response_model=list[GameStats], summary="Server-wide game stats")
async def game_stats(
    guild_id: str = Query(..., description="Guild ID."),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get aggregated game statistics across all game types for a guild."""
    rows = await conn.fetch(
        """SELECT game_type,
                  COUNT(*) as total_played,
                  COALESCE(SUM(bet_amount), 0) as total_wagered,
                  COALESCE(SUM(profit), 0) as total_profit
           FROM game_results
           WHERE guild_id = $1
           GROUP BY game_type
           ORDER BY total_played DESC""",
        int(guild_id),
    )
    from core.framework.scale import to_human as _to_human_g
    result_stats = []
    for r in rows:
        tw = _to_human_g(int(r["total_wagered"]))
        tp = _to_human_g(int(r["total_profit"]))
        result_stats.append(GameStats(
            game_type=r["game_type"],
            total_played=r["total_played"],
            total_wagered=tw,
            total_profit=tp,
            house_edge=round(-tp / tw * 100, 2) if tw > 0 else 0.0,
        ))
    return result_stats


@router.get("/stats/{game}", response_model=GameStats, summary="Stats for a specific game")
async def game_stats_by_type(
    game: str,
    guild_id: str = Query(..., description="Guild ID."),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get statistics for a specific game type."""
    row = await conn.fetchrow(
        """SELECT COUNT(*) as total_played,
                  COALESCE(SUM(bet_amount), 0) as total_wagered,
                  COALESCE(SUM(profit), 0) as total_profit
           FROM game_results
           WHERE guild_id = $1 AND game_type = $2""",
        int(guild_id), game,
    )
    from core.framework.scale import to_human as _to_human_g
    total_wagered = _to_human_g(int(row["total_wagered"]))
    total_profit = _to_human_g(int(row["total_profit"]))
    return GameStats(
        game_type=game,
        total_played=row["total_played"],
        total_wagered=total_wagered,
        total_profit=total_profit,
        house_edge=round(-total_profit / total_wagered * 100, 2) if total_wagered > 0 else 0.0,
    )


@router.get("/leaderboard", response_model=list[GameLeaderboardEntry], summary="Game leaderboard")
async def game_leaderboard(
    guild_id: str = Query(..., description="Guild ID."),
    game: str | None = Query(None, description="Filter by game type."),
    sort: str = Query("profit", description="Sort by: profit, wins, volume."),
    limit: int = Query(20, ge=1, le=100, description="Number of entries."),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get the gambling leaderboard for a guild, optionally filtered by game."""
    sort_col_map = {
        "profit": "total_profit",
        "wins": "win_count",
        "volume": "total_wagered",
    }
    sort_col = sort_col_map.get(sort, "total_profit")

    game_filter = ""
    params: list[Any] = [int(guild_id)]
    if game:
        game_filter = "AND gr.game_type = $2"
        params.append(game)

    # Build safe ORDER BY -- sort_col is always from our whitelist
    query = f"""
        SELECT gr.user_id,
               COALESCE(u.wallet, 0) as _ignore,
               COALESCE(SUM(gr.bet_amount), 0) as total_wagered,
               COALESCE(SUM(gr.profit), 0) as total_profit,
               COUNT(*) FILTER (WHERE gr.profit > 0) as win_count,
               COUNT(*) FILTER (WHERE gr.profit <= 0) as loss_count
        FROM game_results gr
        JOIN users u ON u.user_id = gr.user_id AND u.guild_id = gr.guild_id
        WHERE gr.guild_id = $1 {game_filter}
        GROUP BY gr.user_id, u.wallet
        ORDER BY {sort_col} DESC
        LIMIT ${len(params) + 1}
    """
    params.append(limit)
    rows = await conn.fetch(query, *params)

    from core.framework.scale import to_human as _to_human_g
    return [
        GameLeaderboardEntry(
            user_id=r["user_id"],
            username="",  # Username would come from Discord API or a cache
            total_wagered=_to_human_g(int(r["total_wagered"])),
            total_profit=_to_human_g(int(r["total_profit"])),
            win_count=r["win_count"],
            loss_count=r["loss_count"],
        )
        for r in rows
    ]


@router.get("/history", response_model=list[GameResult], summary="User game history")
async def game_history(
    user: dict = Depends(get_current_user),
    game: str | None = Query(None, description="Filter by game type."),
    limit: int = Query(20, ge=1, le=100, description="Number of results."),
    offset: int = Query(0, ge=0, description="Offset for pagination."),
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get the authenticated user's game history."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    params: list[Any] = [user_id, guild_id]
    game_filter = ""
    if game:
        game_filter = "AND game_type = $3"
        params.append(game)

    query = f"""
        SELECT id, game_type, bet_amount, payout, profit, multiplier, result_data
        FROM game_results
        WHERE user_id = $1 AND guild_id = $2 {game_filter}
        ORDER BY played_at DESC
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """
    params.extend([limit, offset])
    rows = await conn.fetch(query, *params)

    results = []
    for r in rows:
        rd = r["result_data"]
        if isinstance(rd, str):
            rd = json.loads(rd)
        elif rd is None:
            rd = {}
        from core.framework.scale import to_human as _to_human_g
        results.append(GameResult(
            game_id=r["id"],
            game_type=r["game_type"],
            bet_amount=_to_human_g(int(r["bet_amount"])),
            payout=_to_human_g(int(r["payout"])),
            profit=_to_human_g(int(r["profit"])),
            multiplier=float(r["multiplier"]) if r["multiplier"] else 0.0,
            result_data=rd,
        ))
    return results


@router.get("/provably-fair/{game_id}", response_model=ProvablyFairData, summary="Verify game fairness")
async def provably_fair(
    game_id: int,
    conn: asyncpg.Connection = Depends(get_db),
):
    """Retrieve provably fair data for a completed game, allowing independent verification."""
    row = await conn.fetchrow(
        "SELECT id, server_seed, client_seed, nonce, result_data FROM game_results WHERE id = $1",
        game_id,
    )
    if not row:
        raise NotFoundError("Game result not found.")
    if not row["server_seed"]:
        raise NotFoundError("Provably fair data not available for this game.")

    rd = row["result_data"]
    if isinstance(rd, str):
        rd = json.loads(rd)
    elif rd is None:
        rd = {}

    return ProvablyFairData(
        game_id=row["id"],
        server_seed=row["server_seed"],
        client_seed=row["client_seed"],
        nonce=row["nonce"],
        server_seed_hash=_hash_seed(row["server_seed"]),
        result=rd,
    )
