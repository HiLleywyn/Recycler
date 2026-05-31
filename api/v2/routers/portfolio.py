"""Portfolio endpoints  -  holdings, stakes, LP, savings, loans, net worth, PnL."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_current_user, get_db, get_orm_db
from api.v2.exceptions import NotFoundError
from api.v2.utils import to_iso
from core.framework.scale import to_human
from api.v2.schemas.common import PaginatedResponse
from api.v2.schemas.user import (
    LoanItem,
    LPPositionItem,
    NetWorthBreakdown,
    PnLData,
    PortfolioOverview,
    SavingsItem,
    StakeItem,
    TransactionItem,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


# ---------------------------------------------------------------------------
# GET /portfolio  -  full portfolio overview
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=PortfolioOverview,
    summary="Get portfolio overview",
)
async def portfolio_overview(
    db=Depends(get_db),
    orm_db=Depends(get_orm_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> PortfolioOverview:
    """Return a high-level summary of the authenticated user's portfolio.

    Includes wallet/bank balances, net worth, and counts of holdings,
    stakes, and LP positions.
    """
    from services.net_worth import compute_net_worth

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    nw = await compute_net_worth(user_id, guild_id, orm_db)
    if nw.wallet == 0 and nw.bank == 0 and nw.total == 0:
        # Check if user actually exists
        user_row = await db.fetchrow(
            "SELECT 1 FROM users WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )
        if not user_row:
            raise NotFoundError("User not found in this guild.")

    return PortfolioOverview(
        wallet=nw.wallet,
        bank=nw.bank,
        net_worth=nw.total,
        net_worth_change_24h=0.0,
        holdings_count=len(nw.holdings) + len(nw.wallet_holdings),
        stakes_count=len(nw.stakes),
        lp_count=len(nw.lp_positions),
    )


# ---------------------------------------------------------------------------
# GET /portfolio/bank  -  wallet and bank balances
# ---------------------------------------------------------------------------

@router.get(
    "/bank",
    summary="Get wallet and bank balances",
)
async def bank_balance(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return the authenticated user's wallet and bank balances."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    row = await db.fetchrow(
        "SELECT wallet, bank FROM users WHERE user_id = $1 AND guild_id = $2",
        user_id,
        guild_id,
    )
    if not row:
        return {"wallet": 0.0, "bank": 0.0}

    return {
        "wallet": to_human(int(row["wallet"] or 0)),
        "bank": to_human(int(row["bank"] or 0)),
    }


# ---------------------------------------------------------------------------
# GET /portfolio/holdings  -  CeFi + DeFi holdings
# ---------------------------------------------------------------------------

@router.get(
    "/holdings",
    summary="Get all holdings",
)
async def list_holdings(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[dict]:
    """Return all CeFi and DeFi token holdings aggregated by symbol."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # CeFi holdings
    cefi_rows = await db.fetch(
        """
        SELECT ch.symbol, ch.amount, COALESCE(cp.price, 0) AS price
        FROM crypto_holdings ch
        LEFT JOIN crypto_prices cp
            ON cp.guild_id = $2 AND cp.symbol = ch.symbol
        WHERE ch.user_id = $1 AND ch.guild_id = $2 AND ch.amount > 0
        ORDER BY ch.symbol
        """,
        user_id,
        guild_id,
    )

    # DeFi wallet holdings
    defi_rows = await db.fetch(
        """
        SELECT wh.symbol, wh.amount, COALESCE(cp.price, 0) AS price
        FROM wallet_holdings wh
        LEFT JOIN crypto_prices cp
            ON cp.guild_id = $2 AND cp.symbol = wh.symbol
        WHERE wh.user_id = $1 AND wh.guild_id = $2 AND wh.amount > 0
        ORDER BY wh.symbol
        """,
        user_id,
        guild_id,
    )

    # Aggregate by symbol
    combined: dict[str, dict] = {}

    for r in cefi_rows:
        sym = r["symbol"]
        price = float(r["price"])
        if sym not in combined:
            combined[sym] = {"symbol": sym, "cefi_amount": 0.0, "defi_amount": 0.0, "price": price}
        combined[sym]["cefi_amount"] += to_human(int(r["amount"] or 0))
        if combined[sym]["price"] == 0 and price > 0:
            combined[sym]["price"] = price

    for r in defi_rows:
        sym = r["symbol"]
        price = float(r["price"])
        if sym not in combined:
            combined[sym] = {"symbol": sym, "cefi_amount": 0.0, "defi_amount": 0.0, "price": price}
        combined[sym]["defi_amount"] += to_human(int(r["amount"] or 0))
        if combined[sym]["price"] == 0 and price > 0:
            combined[sym]["price"] = price

    result = []
    for data in combined.values():
        total = data["cefi_amount"] + data["defi_amount"]
        result.append({
            "symbol": data["symbol"],
            "cefi_amount": round(data["cefi_amount"], 8),
            "defi_amount": round(data["defi_amount"], 8),
            "total_amount": round(total, 8),
            "price": data["price"],
            "value": round(total * data["price"], 2),
        })

    return sorted(result, key=lambda x: x["value"], reverse=True)


# ---------------------------------------------------------------------------
# GET /portfolio/stakes  -  active stakes
# ---------------------------------------------------------------------------

@router.get(
    "/stakes",
    response_model=list[StakeItem],
    summary="Get active stakes",
)
async def list_stakes(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[StakeItem]:
    """Return all active stake positions for the authenticated user."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await db.fetch(
        """
        SELECT s.validator_id, v.name AS validator_name, s.symbol, s.amount,
               COALESCE(cp.price, 0) AS price, v.reward_rate, s.staked_at
        FROM stakes s
        LEFT JOIN validators v
            ON v.validator_id = s.validator_id AND v.guild_id = s.guild_id
        LEFT JOIN crypto_prices cp
            ON cp.guild_id = $2 AND cp.symbol = s.symbol
        WHERE s.user_id = $1 AND s.guild_id = $2 AND s.amount > 0
        ORDER BY s.validator_id, s.symbol
        """,
        user_id,
        guild_id,
    )

    return [
        StakeItem(
            validator_id=r["validator_id"],
            validator_name=r["validator_name"] or "",
            symbol=r["symbol"],
            amount=to_human(int(r["amount"] or 0)),
            value_usd=round(to_human(int(r["amount"] or 0)) * float(r["price"]), 2),
            apy=float(r["reward_rate"]) * 100 if r["reward_rate"] else 0.0,
            staked_at=to_iso(r["staked_at"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /portfolio/lp-positions  -  LP positions
# ---------------------------------------------------------------------------

@router.get(
    "/lp-positions",
    response_model=list[LPPositionItem],
    summary="Get LP positions",
)
async def list_lp_positions(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[LPPositionItem]:
    """Return all liquidity pool positions for the authenticated user."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await db.fetch(
        """
        SELECT lp.pool_id, lp.lp_shares, lp.added_at,
               p.token_a, p.token_b, p.reserve_a, p.reserve_b, p.total_lp
        FROM lp_positions lp
        JOIN pools p ON p.pool_id = lp.pool_id AND p.guild_id = lp.guild_id
        WHERE lp.user_id = $1 AND lp.guild_id = $2 AND lp.lp_shares > 0
        ORDER BY lp.pool_id
        """,
        user_id,
        guild_id,
    )

    results: list[LPPositionItem] = []
    for r in rows:
        total_lp = to_human(int(r["total_lp"] or 0))
        lp_shares = to_human(int(r["lp_shares"] or 0))
        share_pct = (lp_shares / total_lp * 100) if total_lp > 0 else 0.0

        # Calculate USD value
        usd_value = 0.0
        if total_lp > 0:
            share = lp_shares / total_lp
            val_a = share * to_human(int(r["reserve_a"] or 0))
            val_b = share * to_human(int(r["reserve_b"] or 0))
            ta, tb = r["token_a"], r["token_b"]
            if tb == "USD":
                usd_value = val_b * 2
            elif ta == "USD":
                usd_value = val_a * 2
            else:
                pa_row = await db.fetchrow(
                    "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
                    guild_id,
                    ta,
                )
                pb_row = await db.fetchrow(
                    "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
                    guild_id,
                    tb,
                )
                usd_value = (
                    val_a * (float(pa_row["price"]) if pa_row else 0)
                    + val_b * (float(pb_row["price"]) if pb_row else 0)
                )

        results.append(
            LPPositionItem(
                pool_id=r["pool_id"],
                token_a=r["token_a"],
                token_b=r["token_b"],
                lp_shares=lp_shares,
                value_usd=round(usd_value, 2),
                share_pct=round(share_pct, 4),
                added_at=to_iso(r["added_at"]),
            )
        )

    return results


# ---------------------------------------------------------------------------
# GET /portfolio/savings  -  savings positions
# ---------------------------------------------------------------------------

@router.get(
    "/savings",
    response_model=list[SavingsItem],
    summary="Get savings positions",
)
async def list_savings(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[SavingsItem]:
    """Return all savings deposit positions for the authenticated user."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await db.fetch(
        """
        SELECT symbol, amount, last_interest, created_at
        FROM savings_deposits
        WHERE user_id = $1 AND guild_id = $2 AND amount > 0
        ORDER BY symbol
        """,
        user_id,
        guild_id,
    )

    return [
        SavingsItem(
            asset=r["symbol"],
            amount=to_human(int(r["amount"] or 0)),
            interest_earned=0.0,  # would need cumulative interest tracking
            apy=5.0 if r["symbol"] == "USD" else 8.0,  # default APY rates
            deposited_at=to_iso(r["created_at"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /portfolio/loans  -  active loans
# ---------------------------------------------------------------------------

@router.get(
    "/loans",
    response_model=list[LoanItem],
    summary="Get active loans",
)
async def list_loans(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[LoanItem]:
    """Return all active loan positions for the authenticated user.

    Includes both USD-backed loans and SUN-collateral loans.
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    results: list[LoanItem] = []

    # USD loans
    usd_loan = await db.fetchrow(
        """
        SELECT principal, outstanding, collateral, created_at
        FROM loans
        WHERE user_id = $1 AND guild_id = $2 AND outstanding > 0
        """,
        user_id,
        guild_id,
    )
    if usd_loan:
        results.append(
            LoanItem(
                loan_id=f"usd-{user_id}-{guild_id}",
                principal=to_human(int(usd_loan["principal"] or 0)),
                outstanding=to_human(int(usd_loan["outstanding"] or 0)),
                collateral=to_human(int(usd_loan["collateral"] or 0)),
                interest_rate=5.0,  # default annual rate
                created_at=to_iso(usd_loan["created_at"]),
                loan_type="usd",
            )
        )

    return results


# ---------------------------------------------------------------------------
# GET /portfolio/net-worth  -  net worth breakdown
# ---------------------------------------------------------------------------

@router.get(
    "/net-worth",
    response_model=NetWorthBreakdown,
    summary="Get net worth breakdown",
)
async def net_worth_breakdown(
    orm_db=Depends(get_orm_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> NetWorthBreakdown:
    """Return a detailed breakdown of the user's net worth by category.

    Uses the canonical compute_net_worth from services/net_worth.py.
    """
    from services.net_worth import compute_net_worth

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    nw = await compute_net_worth(user_id, guild_id, orm_db)

    return NetWorthBreakdown(
        cefi=round(nw.cefi_crypto, 2),
        defi=round(nw.defi_wallet, 2),
        staking=round(nw.stake_value, 2),
        pos=round(nw.pos_stake_value, 2),
        lp=round(nw.lp_value, 2),
        mining=round(nw.rig_value, 2),
        delegations=round(nw.delegation_value, 2),
        savings=round(nw.savings_value, 2),
        items=round(nw.items_value, 2),
        lunar_mint=round(nw.moon_stake_value, 2),
        moon_pool=round(nw.moon_pool_stake_value, 2),
        total=nw.total,
    )


# ---------------------------------------------------------------------------
# GET /portfolio/pnl  -  realized + unrealized PnL
# ---------------------------------------------------------------------------

@router.get(
    "/pnl",
    response_model=PnLData,
    summary="Get profit and loss data",
)
async def portfolio_pnl(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> PnLData:
    """Return realized and unrealized PnL for the authenticated user.

    Realized PnL is computed from completed sell transactions.
    Unrealized PnL is estimated from current holdings vs. average cost.
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Realized PnL from sells: revenue from sells minus cost from buys
    # Buy cost for each symbol
    buy_rows = await db.fetch(
        """
        SELECT symbol_out AS symbol, SUM(amount_in) AS total_cost, SUM(amount_out) AS total_bought
        FROM transactions
        WHERE guild_id = $1 AND user_id = $2 AND tx_type = 'BUY'
        GROUP BY symbol_out
        """,
        guild_id,
        user_id,
    )
    buy_cost_map: dict[str, float] = {}
    buy_qty_map: dict[str, float] = {}
    for br in buy_rows:
        sym = br["symbol"]
        buy_cost_map[sym] = float(br["total_cost"])
        buy_qty_map[sym] = float(br["total_bought"])

    # Sell revenue for each symbol
    sell_rows = await db.fetch(
        """
        SELECT symbol_in AS symbol, SUM(amount_out) AS total_revenue, SUM(amount_in) AS total_sold
        FROM transactions
        WHERE guild_id = $1 AND user_id = $2 AND tx_type = 'SELL'
        GROUP BY symbol_in
        """,
        guild_id,
        user_id,
    )

    realized_pnl = 0.0
    for sr in sell_rows:
        sym = sr["symbol"]
        revenue = float(sr["total_revenue"])
        sold_qty = float(sr["total_sold"])
        bought_qty = buy_qty_map.get(sym, 0.0)
        total_cost = buy_cost_map.get(sym, 0.0)
        # Average cost basis for sold portion
        if bought_qty > 0:
            avg_cost = total_cost / bought_qty
            cost_of_sold = avg_cost * sold_qty
        else:
            cost_of_sold = 0.0
        realized_pnl += revenue - cost_of_sold

    # Unrealized PnL from current holdings
    holdings = await db.fetch(
        """
        SELECT ch.symbol, ch.amount, COALESCE(cp.price, 0) AS price
        FROM crypto_holdings ch
        LEFT JOIN crypto_prices cp ON cp.guild_id = $2 AND cp.symbol = ch.symbol
        WHERE ch.user_id = $1 AND ch.guild_id = $2 AND ch.amount > 0
        """,
        user_id,
        guild_id,
    )

    unrealized_pnl = 0.0
    for h in holdings:
        sym = h["symbol"]
        amount = to_human(int(h["amount"] or 0))
        current_price = float(h["price"])
        current_value = amount * current_price
        bought_qty = buy_qty_map.get(sym, 0.0)
        total_cost = buy_cost_map.get(sym, 0.0)
        if bought_qty > 0:
            avg_cost = total_cost / bought_qty
            cost_basis = avg_cost * amount
        else:
            cost_basis = 0.0
        unrealized_pnl += current_value - cost_basis

    return PnLData(
        realized_pnl=round(realized_pnl, 2),
        unrealized_pnl=round(unrealized_pnl, 2),
        total_pnl=round(realized_pnl + unrealized_pnl, 2),
        pnl_history=[],  # would require net worth snapshots
    )


# ---------------------------------------------------------------------------
# GET /portfolio/tx-history  -  paginated transaction history
# ---------------------------------------------------------------------------

@router.get(
    "/tx-history",
    response_model=PaginatedResponse,
    summary="Get transaction history",
)
async def transaction_history(
    tx_type: str | None = Query(None, description="Filter by tx type."),
    symbol: str | None = Query(None, description="Filter by token symbol."),
    include_usd: bool = Query(True, description="Include USD transactions (games, deposits, etc.)."),
    limit: int = Query(50, ge=1, le=200, description="Page size."),
    offset: int = Query(0, ge=0, description="Offset for pagination."),
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> PaginatedResponse:
    """Return paginated transaction history for the authenticated user.

    Includes all transaction types (BUY, SELL, SWAP, TRANSFER, STAKE, etc.)
    as well as USD transactions (game results, deposits, withdrawals) when
    include_usd is True.
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Build the crypto transactions query
    tx_conditions = ["guild_id = $1", "user_id = $2"]
    params: list[Any] = [guild_id, user_id]
    idx = 3

    if tx_type:
        tx_conditions.append(f"tx_type = ${idx}")
        params.append(tx_type.upper())
        idx += 1

    if symbol:
        sym = symbol.upper()
        tx_conditions.append(f"(symbol_in = ${idx} OR symbol_out = ${idx})")
        params.append(sym)
        idx += 1

    tx_where = " AND ".join(tx_conditions)

    # Determine whether to include game results as USD transactions.
    # We skip game results when filtering by a non-USD symbol or a non-game tx_type.
    include_games = (
        include_usd
        and (symbol is None or symbol.upper() == "USD")
        and (tx_type is None or tx_type.upper() in ("GAME_WIN", "GAME_LOSS", "GAME"))
    )

    if include_games:
        # Use a UNION ALL to combine crypto transactions and game results
        count_sql = f"""
            SELECT (
                (SELECT COUNT(*) FROM transactions WHERE {tx_where})
                +
                (SELECT COUNT(*) FROM game_results WHERE guild_id = $1 AND user_id = $2)
            ) AS cnt
        """
        count_row = await db.fetchrow(count_sql, *params[:idx - 1])
        total = int(count_row["cnt"]) if count_row else 0

        params.extend([limit, offset])
        rows = await db.fetch(
            f"""
            SELECT * FROM (
                SELECT tx_hash, tx_type, symbol_in, amount_in, symbol_out, amount_out,
                       gas_fee, ts
                FROM transactions
                WHERE {tx_where}

                UNION ALL

                SELECT
                    'game-' || id::TEXT AS tx_hash,
                    CASE WHEN profit >= 0 THEN 'GAME_WIN' ELSE 'GAME_LOSS' END AS tx_type,
                    'USD' AS symbol_in,
                    bet_amount AS amount_in,
                    'USD' AS symbol_out,
                    payout AS amount_out,
                    0.0::NUMERIC AS gas_fee,
                    played_at AS ts
                FROM game_results
                WHERE guild_id = $1 AND user_id = $2
            ) combined
            ORDER BY ts DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
    else:
        count_row = await db.fetchrow(
            f"SELECT COUNT(*) AS cnt FROM transactions WHERE {tx_where}",
            *params[:idx - 1],
        )
        total = int(count_row["cnt"]) if count_row else 0

        params.extend([limit, offset])
        rows = await db.fetch(
            f"""
            SELECT tx_hash, tx_type, symbol_in, amount_in, symbol_out, amount_out,
                   gas_fee, ts
            FROM transactions
            WHERE {tx_where}
            ORDER BY ts DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )

    items = [
        TransactionItem(
            tx_hash=r["tx_hash"],
            tx_type=r["tx_type"],
            symbol_in=r["symbol_in"],
            amount_in=float(r["amount_in"]) if r["amount_in"] is not None else None,
            symbol_out=r["symbol_out"],
            amount_out=float(r["amount_out"]) if r["amount_out"] is not None else None,
            fee=0.0,
            gas_fee=float(r["gas_fee"]) if r["gas_fee"] is not None else 0.0,
            ts=to_iso(r["ts"]),
        ).model_dump()
        for r in rows
    ]

    return PaginatedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )
