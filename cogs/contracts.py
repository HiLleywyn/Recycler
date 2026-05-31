"""
cogs/contracts.py  -  On-chain Smart Contract Framework

Architecture:
  - Contracts are JSON-defined programs deployed by players on a network
  - Each contract has: address, owner, persistent state, token balance, event log
  - Functions are sequences of ops (receive, send, swap, buy, sell, require, etc.)
  - Contract calls go through the mempool and execute during validator block processing
  - ContractEngine handles op-by-op execution with atomic rollback on revert

Analogy to Arcadia:
  - contract_deploy mempool action  = CREATE transaction
  - contract_call mempool action    = CALL transaction
  - ContractEngine.execute()        = EVM execution context
  - contract.state                  = contract storage
  - virtual_uid                     = contract account address (holds balances)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import discord
from discord.ext import commands, tasks

from core.framework.embed import card
from core.framework.network import normalize_full as normalize_network_full

log = logging.getLogger(__name__)

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_allowed, no_bots
from core.framework.fuzzy import suggest_subcommand
from core.framework.ui import C_AMBER, C_INFO, fmt_token, fmt_usd, fmt_gas, fmt_ts, mention
from core.framework.scale import to_raw, to_human

# ── Built-in contract templates ────────────────────────────────────────────────

_TEMPLATE_LIMIT_ORDER: dict = {
    "functions": {
        "place": {
            "description": "Lock tokens and set a price target. Anyone can call execute() once met.",
            "params": {
                "token_in":     "string",
                "token_out":    "string",
                "amount":       "number",
                "target_price": "number",
                "pool_id":      "string",
            },
            "steps": [
                {"op": "receive",    "symbol": "$token_in",     "amount": "$amount"},
                {"op": "set_state",  "key": "token_in",          "value": "$token_in"},
                {"op": "set_state",  "key": "token_out",         "value": "$token_out"},
                {"op": "set_state",  "key": "amount",            "value": "$amount"},
                {"op": "set_state",  "key": "target_price",      "value": "$target_price"},
                {"op": "set_state",  "key": "pool_id",           "value": "$pool_id"},
                {"op": "set_state",  "key": "owner",             "value": "$caller"},
                {"op": "set_state",  "key": "filled",            "value": 0},
                {"op": "emit",       "event": "OrderPlaced",
                 "data": {"token_in": "$token_in", "amount": "$amount", "target": "$target_price"}},
            ],
        },
        "execute": {
            "description": "Execute the swap if the price condition is met. Callable by anyone (or the keeper).",
            "keeper": True,
            "params": {},
            "steps": [
                {"op": "require",       "lhs": "{{filled}}", "op_cmp": "eq", "rhs": 0},
                {"op": "get_state",     "key": "token_out",     "as": "token_out"},
                {"op": "get_state",     "key": "target_price",  "as": "target_price"},
                {"op": "require_price", "symbol": "{{token_out}}", "op_cmp": "lte", "value": "{{target_price}}"},
                {"op": "get_state",     "key": "token_in",      "as": "token_in"},
                {"op": "get_state",     "key": "amount",        "as": "amount"},
                {"op": "get_state",     "key": "pool_id",       "as": "pool_id"},
                {"op": "swap",          "pool_id": "{{pool_id}}",
                 "token_in": "{{token_in}}", "token_out": "{{token_out}}", "amount": "{{amount}}"},
                {"op": "get_state",     "key": "owner",         "as": "owner"},
                {"op": "send",          "to": "{{owner}}",      "symbol": "{{token_out}}", "amount": "$output"},
                {"op": "set_state",     "key": "filled",        "value": 1},
                {"op": "emit",          "event": "OrderExecuted", "data": {"received": "$output"}},
            ],
        },
        "cancel": {
            "description": "Cancel the order and refund tokens. Owner only.",
            "params": {},
            "steps": [
                {"op": "require",    "lhs": "{{filled}}", "op_cmp": "eq", "rhs": 0},
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "get_state",  "key": "token_in",  "as": "token_in"},
                {"op": "get_state",  "key": "amount",    "as": "amount"},
                {"op": "get_state",  "key": "owner",     "as": "owner"},
                {"op": "send",       "to": "{{owner}}",  "symbol": "{{token_in}}", "amount": "{{amount}}"},
                {"op": "set_state",  "key": "filled",    "value": 1},
                {"op": "emit",       "event": "OrderCancelled"},
            ],
        },
    },
}

_TEMPLATE_ESCROW: dict = {
    "functions": {
        "deposit": {
            "description": "Deposit tokens into escrow. Owner only.",
            "params": {"symbol": "string", "amount": "number", "recipient": "string"},
            "steps": [
                {"op": "require_caller",  "user_id": "{{owner}}"},
                {"op": "receive",         "symbol": "$symbol",    "amount": "$amount"},
                {"op": "set_state",       "key": "symbol",        "value": "$symbol"},
                {"op": "set_state",       "key": "amount",        "value": "$amount"},
                {"op": "set_state",       "key": "recipient",     "value": "$recipient"},
                {"op": "set_state",       "key": "released",      "value": 0},
                {"op": "emit",            "event": "Deposited",
                 "data": {"symbol": "$symbol", "amount": "$amount", "recipient": "$recipient"}},
            ],
        },
        "release": {
            "description": "Release escrowed tokens to recipient. Owner only.",
            "params": {},
            "steps": [
                {"op": "require",        "lhs": "{{released}}", "op_cmp": "eq", "rhs": 0},
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "get_state",      "key": "symbol",    "as": "symbol"},
                {"op": "get_state",      "key": "amount",    "as": "amount"},
                {"op": "get_state",      "key": "recipient", "as": "recipient"},
                {"op": "send",           "to": "{{recipient}}", "symbol": "{{symbol}}", "amount": "{{amount}}"},
                {"op": "set_state",      "key": "released",  "value": 1},
                {"op": "emit",           "event": "Released"},
            ],
        },
        "refund": {
            "description": "Refund tokens back to owner. Owner only.",
            "params": {},
            "steps": [
                {"op": "require",        "lhs": "{{released}}", "op_cmp": "eq", "rhs": 0},
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "get_state",      "key": "symbol",  "as": "symbol"},
                {"op": "get_state",      "key": "amount",  "as": "amount"},
                {"op": "get_state",      "key": "owner",   "as": "owner"},
                {"op": "send",           "to": "{{owner}}", "symbol": "{{symbol}}", "amount": "{{amount}}"},
                {"op": "set_state",      "key": "released", "value": 1},
                {"op": "emit",           "event": "Refunded"},
            ],
        },
    },
}

_TEMPLATE_VESTING: dict = {
    "functions": {
        "fund": {
            "description": "Fund the vesting contract with the total allocation. Owner only.",
            "params": {"symbol": "string", "amount": "number", "recipient": "string",
                       "duration_days": "number", "cliff_days": "number"},
            "steps": [
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "receive",        "symbol": "$symbol",        "amount": "$amount"},
                {"op": "set_state",      "key": "symbol",            "value": "$symbol"},
                {"op": "set_state",      "key": "total",             "value": "$amount"},
                {"op": "set_state",      "key": "claimed",           "value": 0},
                {"op": "set_state",      "key": "recipient",         "value": "$recipient"},
                {"op": "set_state",      "key": "start_ts",          "value": "$now"},
                {"op": "set_state",      "key": "duration_secs",     "value": {"mul": ["$duration_days", 86400]}},
                {"op": "set_state",      "key": "cliff_secs",        "value": {"mul": ["$cliff_days", 86400]}},
                {"op": "emit",           "event": "Funded",
                 "data": {"symbol": "$symbol", "amount": "$amount", "recipient": "$recipient"}},
            ],
        },
        "claim": {
            "description": "Claim vested tokens. Recipient only.",
            "params": {},
            "steps": [
                {"op": "require_caller",  "user_id": "{{recipient}}"},
                {"op": "require_time",    "op_cmp": "gte", "ts": {"add": ["{{start_ts}}", "{{cliff_secs}}"]}},
                {"op": "get_state",       "key": "total",         "as": "total"},
                {"op": "get_state",       "key": "claimed",       "as": "claimed"},
                {"op": "get_state",       "key": "start_ts",      "as": "start_ts"},
                {"op": "get_state",       "key": "duration_secs", "as": "duration_secs"},
                {"op": "get_state",       "key": "symbol",        "as": "symbol"},
                {"op": "get_state",       "key": "recipient",     "as": "recipient"},
                {"op": "vested_claim",    "total": "{{total}}", "claimed": "{{claimed}}",
                 "start_ts": "{{start_ts}}", "duration_secs": "{{duration_secs}}"},
                {"op": "send",            "to": "{{recipient}}", "symbol": "{{symbol}}", "amount": "$output"},
                {"op": "set_state",       "key": "claimed",      "value": {"add": ["{{claimed}}", "$output"]}},
                {"op": "emit",            "event": "Claimed", "data": {"amount": "$output"}},
            ],
        },
    },
}

_TEMPLATE_MULTISIG: dict = {
    "functions": {
        "setup": {
            "description": "Initialize signers list and approval threshold.",
            "params": {"threshold": "number"},
            "steps": [
                {"op": "require_caller", "user_id": "{{owner}}"},
                {"op": "set_state",      "key": "threshold",  "value": "$threshold"},
                {"op": "set_state",      "key": "approvals",  "value": 0},
                {"op": "set_state",      "key": "executed",   "value": 0},
                {"op": "emit",           "event": "Initialized", "data": {"threshold": "$threshold"}},
            ],
        },
        "deposit": {
            "description": "Deposit tokens to be held until execution.",
            "params": {"symbol": "string", "amount": "number"},
            "steps": [
                {"op": "receive",   "symbol": "$symbol",  "amount": "$amount"},
                {"op": "set_state", "key": "symbol",      "value": "$symbol"},
                {"op": "set_state", "key": "amount",      "value": "$amount"},
                {"op": "emit",      "event": "Deposited", "data": {"symbol": "$symbol", "amount": "$amount"}},
            ],
        },
        "approve": {
            "description": "Add your approval. Increments approval count.",
            "params": {},
            "steps": [
                {"op": "require",   "lhs": "{{executed}}", "op_cmp": "eq", "rhs": 0},
                {"op": "set_state", "key": "approvals",    "value": {"add": ["{{approvals}}", 1]}},
                {"op": "emit",      "event": "Approved",   "data": {"by": "$caller", "total": "{{approvals}}"}},
            ],
        },
        "execute": {
            "description": "Execute if approval threshold met. Sends tokens to owner.",
            "params": {"to": "string"},
            "steps": [
                {"op": "require",        "lhs": "{{executed}}",  "op_cmp": "eq", "rhs": 0},
                {"op": "require",        "lhs": "{{approvals}}", "op_cmp": "gte", "rhs": "{{threshold}}"},
                {"op": "get_state",      "key": "symbol",  "as": "symbol"},
                {"op": "get_state",      "key": "amount",  "as": "amount"},
                {"op": "send",           "to": "$to",      "symbol": "{{symbol}}", "amount": "{{amount}}"},
                {"op": "set_state",      "key": "executed", "value": 1},
                {"op": "emit",           "event": "Executed", "data": {"to": "$to"}},
            ],
        },
    },
}

TEMPLATES: dict[str, dict] = {
    "limit_order": _TEMPLATE_LIMIT_ORDER,
    "escrow":      _TEMPLATE_ESCROW,
    "vesting":     _TEMPLATE_VESTING,
    "multisig":    _TEMPLATE_MULTISIG,
}

VALID_OPS = {
    "receive", "send", "swap", "buy", "sell",
    "require", "require_caller", "require_price", "require_time",
    "set_state", "get_state", "emit", "vested_claim",
}


# ── Contract execution engine ──────────────────────────────────────────────────

class ContractRevert(Exception):
    """Raised when a contract step fails and execution should be reverted."""


class ContractEngine:
    """Interprets and executes contract function op lists."""

    @staticmethod
    def validate_definition(definition: dict) -> None:
        """Validate a contract definition structure. Raises ValueError on error."""
        if not isinstance(definition, dict):
            raise ValueError("Definition must be a JSON object")
        fns = definition.get("functions")
        if not isinstance(fns, dict) or not fns:
            raise ValueError("Definition must have at least one function")
        for fname, fdef in fns.items():
            steps = fdef.get("steps")
            if not isinstance(steps, list):
                raise ValueError(f"Function '{fname}' must have a steps list")
            for i, step in enumerate(steps):
                op = step.get("op")
                if op not in VALID_OPS:
                    raise ValueError(f"Function '{fname}' step {i}: unknown op '{op}'")

    @staticmethod
    async def execute(
        db,
        guild: discord.Guild,
        contract: dict,
        function_name: str,
        args: dict,
        caller_id: int,
        block_id: int | None,
    ) -> tuple[bool, str]:
        """
        Execute a contract function. Returns (success, reason).
        Atomically reverts all balance changes on failure.
        """
        definition = contract["definition"]
        fns = definition.get("functions", {})
        if function_name not in fns:
            return False, f"Function '{function_name}' not found in contract"

        fn = fns[function_name]
        params = fn.get("params", {})
        steps  = fn.get("steps", [])

        # Validate required params are present
        for pname in params:
            if pname not in args:
                return False, f"Missing param '{pname}'"

        # Execution context
        address    = contract["address"]
        vuid       = contract["virtual_uid"]
        state      = dict(contract["state"])  # mutable copy
        local_vars: dict[str, Any] = {}       # local variables from get_state
        output     = 0.0                       # result of last swap/buy/sell
        rollback:  list[tuple] = []            # [(type, *args)] for undo on revert

        def _resolve(val: Any) -> Any:
            """Resolve variable references in a value."""
            if not isinstance(val, str):
                return val
            if val.startswith("$"):
                vname = val[1:]
                if vname == "caller":
                    return caller_id
                if vname == "now":
                    return time.time()
                if vname == "output":
                    return output
                return args.get(vname, local_vars.get(vname, val))
            if val.startswith("{{") and val.endswith("}}"):
                key = val[2:-2].strip()
                if key in local_vars:
                    return local_vars[key]
                if key in state:
                    return state[key]
                return None
            return val

        def _resolve_numeric(val: Any) -> float:
            v = _resolve(val)
            if isinstance(v, dict):
                # Support simple arithmetic: {"add": [a, b]}, {"mul": [a, b]}
                if "add" in v:
                    a, b = v["add"]
                    return float(_resolve_numeric(a)) + float(_resolve_numeric(b))
                if "mul" in v:
                    a, b = v["mul"]
                    return float(_resolve_numeric(a)) * float(_resolve_numeric(b))
            return float(v) if v is not None else 0.0

        try:
            for step in steps:
                op = step["op"]
                nonlocal_output = [output]

                if op == "receive":
                    # Pull tokens from caller into contract balance
                    sym = str(_resolve(step["symbol"])).upper()
                    amt = _resolve_numeric(step["amount"])
                    if amt <= 0:
                        raise ContractRevert(f"receive: amount must be positive")
                    amt_raw = to_raw(amt)
                    if sym == "USD":
                        user = await db.get_user(caller_id, guild.id)
                        if not user or int(user["wallet"] or 0) < amt_raw:
                            raise ContractRevert(f"Insufficient USD balance (need {fmt_usd(amt)})")
                        await db.update_wallet(caller_id, guild.id, -amt_raw)
                        await db.update_wallet(vuid, guild.id, amt_raw)
                        rollback.append(("wallet", caller_id, amt_raw, vuid, -amt_raw))
                    else:
                        h = await db.get_holding(caller_id, guild.id, sym)
                        bal_raw = int(h["amount"] or 0) if h else 0
                        if bal_raw < amt_raw:
                            raise ContractRevert(f"Insufficient {sym} (have {fmt_token(to_human(bal_raw), sym)}, need {fmt_token(amt, sym)})")
                        await db.update_holding(caller_id, guild.id, sym, -amt_raw)
                        await db.update_holding(vuid, guild.id, sym, amt_raw)
                        rollback.append(("holding", caller_id, guild.id, sym, amt_raw, vuid, guild.id, sym, -amt_raw))

                elif op == "send":
                    # Transfer from contract to target user
                    to_user_id_val = _resolve(step["to"])
                    sym    = str(_resolve(step["symbol"])).upper()
                    amt    = _resolve_numeric(step["amount"])
                    to_id  = int(to_user_id_val) if to_user_id_val is not None else None
                    if not to_id or amt <= 0:
                        raise ContractRevert("send: invalid to/amount")
                    amt_raw = to_raw(amt)
                    await db.ensure_user(to_id, guild.id)
                    if sym == "USD":
                        await db.update_wallet(vuid, guild.id, -amt_raw)
                        await db.update_wallet(to_id, guild.id, amt_raw)
                        rollback.append(("wallet", vuid, amt_raw, to_id, -amt_raw))
                    else:
                        await db.update_holding(vuid, guild.id, sym, -amt_raw)
                        await db.update_holding(to_id, guild.id, sym, amt_raw)
                        rollback.append(("holding", vuid, guild.id, sym, amt_raw, to_id, guild.id, sym, -amt_raw))

                elif op == "swap":
                    pool_id   = str(_resolve(step["pool_id"]))
                    token_in  = str(_resolve(step["token_in"])).upper()
                    token_out = str(_resolve(step["token_out"])).upper()
                    amt       = _resolve_numeric(step["amount"])
                    amt_raw   = to_raw(amt)
                    pool = await db.get_pool(pool_id, guild.id)
                    if not pool:
                        raise ContractRevert(f"Pool {pool_id} not found")
                    ca = pool["token_a"]
                    # pool reserves are raw NUMERIC(36,0) scaled ints
                    if token_in == ca:
                        res_in_raw, res_out_raw = int(pool["reserve_a"] or 0), int(pool["reserve_b"] or 0)
                    else:
                        res_in_raw, res_out_raw = int(pool["reserve_b"] or 0), int(pool["reserve_a"] or 0)
                    if res_in_raw <= 0 or res_out_raw <= 0:
                        raise ContractRevert("Pool has no liquidity")
                    FEE = 0.003
                    # Constant-product math stays scale-invariant in ratios but
                    # we keep all DB writes as raw ints.
                    amt_fee_raw = amt_raw - int(amt_raw * FEE)
                    amt_out_raw = (res_out_raw * amt_fee_raw) // (res_in_raw + amt_fee_raw)
                    if amt_out_raw <= 0:
                        raise ContractRevert("Swap output too small")
                    await db.update_holding(vuid, guild.id, token_in, -amt_raw)
                    await db.update_holding(vuid, guild.id, token_out, amt_out_raw)
                    if token_in == ca:
                        await db.update_pool_reserves(pool_id, guild.id, res_in_raw + amt_raw, res_out_raw - amt_out_raw, pool["total_lp"])
                    else:
                        await db.update_pool_reserves(pool_id, guild.id, res_out_raw - amt_out_raw, res_in_raw + amt_raw, pool["total_lp"])
                    rollback.append(("swap_undo", vuid, guild.id, token_in, amt_raw, token_out, amt_out_raw, pool_id, pool))
                    output = to_human(amt_out_raw)

                elif op == "buy":
                    sym        = str(_resolve(step["symbol"])).upper()
                    amount_usd = _resolve_numeric(step["amount_usd"])
                    price_row  = await db.get_price(sym, guild.id)
                    if not price_row or price_row["price"] <= 0:
                        raise ContractRevert(f"No price data for {sym}")
                    cost = amount_usd
                    qty  = cost / float(price_row["price"])
                    cost_raw = to_raw(cost)
                    qty_raw  = to_raw(qty)
                    await db.update_wallet(vuid, guild.id, -cost_raw)
                    await db.update_holding(vuid, guild.id, sym, qty_raw)
                    rollback.append(("buy_undo", vuid, guild.id, sym, qty_raw, cost_raw))
                    output = qty

                elif op == "sell":
                    sym   = str(_resolve(step["symbol"])).upper()
                    amt   = _resolve_numeric(step["amount"])
                    price_row = await db.get_price(sym, guild.id)
                    if not price_row or price_row["price"] <= 0:
                        raise ContractRevert(f"No price data for {sym}")
                    revenue = float(price_row["price"]) * amt
                    amt_raw     = to_raw(amt)
                    revenue_raw = to_raw(revenue)
                    await db.update_holding(vuid, guild.id, sym, -amt_raw)
                    await db.update_wallet(vuid, guild.id, revenue_raw)
                    rollback.append(("sell_undo", vuid, guild.id, sym, amt_raw, revenue_raw))
                    output = revenue

                elif op == "require":
                    lhs = _resolve_numeric(step["lhs"]) if isinstance(_resolve(step["lhs"]), (int, float)) else _resolve(step["lhs"])
                    rhs = _resolve_numeric(step["rhs"]) if isinstance(_resolve(step["rhs"]), (int, float)) else _resolve(step["rhs"])
                    op_cmp = step.get("op_cmp", "eq")
                    try:
                        lhs_f, rhs_f = float(lhs), float(rhs)
                    except (TypeError, ValueError):
                        lhs_f, rhs_f = None, None
                    if op_cmp == "eq":
                        ok = lhs == rhs or (lhs_f is not None and abs(lhs_f - rhs_f) < 1e-9)
                    elif op_cmp == "lt"  and lhs_f is not None: ok = lhs_f <  rhs_f
                    elif op_cmp == "lte" and lhs_f is not None: ok = lhs_f <= rhs_f
                    elif op_cmp == "gt"  and lhs_f is not None: ok = lhs_f >  rhs_f
                    elif op_cmp == "gte" and lhs_f is not None: ok = lhs_f >= rhs_f
                    else: ok = False
                    if not ok:
                        raise ContractRevert(f"require({op_cmp}) failed: {lhs} vs {rhs}")

                elif op == "require_caller":
                    expected = int(_resolve(step.get("user_id", "")))
                    if caller_id != expected:
                        raise ContractRevert("require_caller: unauthorized caller")

                elif op == "require_price":
                    sym    = str(_resolve(step["symbol"])).upper()
                    op_cmp = step.get("op_cmp", "lte")
                    target = _resolve_numeric(step["value"])
                    pr     = await db.get_price(sym, guild.id)
                    if not pr:
                        raise ContractRevert(f"No price for {sym}")
                    current = float(pr["price"])
                    if op_cmp == "lte": ok = current <= target
                    elif op_cmp == "lt": ok = current < target
                    elif op_cmp == "gte": ok = current >= target
                    elif op_cmp == "gt": ok = current > target
                    elif op_cmp == "eq": ok = abs(current - target) < 1e-9
                    else: ok = False
                    if not ok:
                        raise ContractRevert(
                            f"require_price: {sym} {fmt_usd(current)} does not satisfy {op_cmp} {fmt_usd(target)}"
                        )

                elif op == "require_time":
                    op_cmp = step.get("op_cmp", "gte")
                    ts     = _resolve_numeric(step["ts"])
                    now    = time.time()
                    if op_cmp == "gte": ok = now >= ts
                    elif op_cmp == "lte": ok = now <= ts
                    else: ok = False
                    if not ok:
                        raise ContractRevert(f"require_time: condition not met (now={now:.0f}, target={ts:.0f})")

                elif op == "set_state":
                    key = str(step["key"])
                    val = step["value"]
                    if isinstance(val, dict) and ("add" in val or "mul" in val):
                        val = _resolve_numeric(val)
                    else:
                        val = _resolve(val)
                    state[key] = val

                elif op == "get_state":
                    key     = str(step["key"])
                    varname = str(step["as"])
                    local_vars[varname] = state.get(key)

                elif op == "emit":
                    event    = str(step["event"])
                    raw_data = step.get("data", {})
                    data     = {k: _resolve(v) for k, v in raw_data.items()} if isinstance(raw_data, dict) else {}
                    await db.log_contract_event(guild.id, address, event, data, block_id)

                elif op == "vested_claim":
                    total        = _resolve_numeric(step["total"])
                    claimed      = _resolve_numeric(step["claimed"])
                    start_ts     = _resolve_numeric(step["start_ts"])
                    duration_sec = _resolve_numeric(step["duration_secs"])
                    now          = time.time()
                    if duration_sec <= 0:
                        claimable = total - claimed
                    else:
                        elapsed  = max(0.0, now - start_ts)
                        vested   = min(total, total * (elapsed / duration_sec))
                        claimable = max(0.0, vested - claimed)
                    if claimable <= 0:
                        raise ContractRevert("Nothing vested yet")
                    output = claimable

            # All steps passed  -  persist new state
            await db.update_contract_state(address, state)
            await db.increment_contract_calls(address)
            return True, f"Contract {function_name}() executed"

        except ContractRevert as e:
            # Roll back all balance changes in reverse order
            for rb in reversed(rollback):
                try:
                    rtype = rb[0]
                    if rtype == "holding":
                        _, uid1, gid1, sym1, d1, uid2, gid2, sym2, d2 = rb
                        await db.update_holding(uid1, gid1, sym1, d1)
                        await db.update_holding(uid2, gid2, sym2, d2)
                    elif rtype == "wallet":
                        _, uid1, d1, uid2, d2 = rb
                        await db.update_wallet(uid1, guild.id, d1)
                        await db.update_wallet(uid2, guild.id, d2)
                    elif rtype == "swap_undo":
                        _, vuid_, gid_, ti, ta, to_, ao, pid, orig_pool = rb
                        await db.update_holding(vuid_, gid_, ti, ta)
                        await db.update_holding(vuid_, gid_, to_, -ao)
                        await db.update_pool_reserves(pid, gid_,
                            orig_pool["reserve_a"], orig_pool["reserve_b"], orig_pool["total_lp"])
                    elif rtype == "buy_undo":
                        _, vuid_, gid_, sym_, qty_, cost_ = rb
                        await db.update_holding(vuid_, gid_, sym_, -qty_)
                        await db.update_wallet(vuid_, gid_, cost_)
                    elif rtype == "sell_undo":
                        _, vuid_, gid_, sym_, amt_, rev_ = rb
                        await db.update_holding(vuid_, gid_, sym_, amt_)
                        await db.update_wallet(vuid_, gid_, -rev_)
                except Exception:
                    log.exception("[contracts] Rollback error")
            return False, str(e)

        except Exception as e:
            return False, f"Contract execution error: {e}"


# ── Cog ────────────────────────────────────────────────────────────────────────

def _keeper_check_price(step: dict, prices: dict[str, float]) -> bool:
    """Cheaply evaluate a require_price op against a prices dict {SYMBOL: price}.
    Returns True if the condition is satisfied (keeper should trigger)."""
    sym = str(step.get("symbol", "")).upper()
    op_cmp = step.get("op_cmp", "lte")
    try:
        target = float(step.get("value", 0))
    except (TypeError, ValueError):
        return False
    current = prices.get(sym)
    if current is None:
        return False
    if op_cmp == "lte": return current <= target
    if op_cmp == "lt":  return current <  target
    if op_cmp == "gte": return current >= target
    if op_cmp == "gt":  return current >  target
    if op_cmp == "eq":  return abs(current - target) < 1e-9
    return False


def _keeper_check_time(step: dict) -> bool:
    """Cheaply evaluate a require_time op. Returns True if condition is satisfied."""
    op_cmp = step.get("op_cmp", "gte")
    try:
        ts = float(step.get("ts", 0))
    except (TypeError, ValueError):
        return False
    now = time.time()
    if op_cmp == "gte": return now >= ts
    if op_cmp == "lte": return now <= ts
    return False


class Contracts(commands.Cog):
    """On-chain smart contract framework."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._keeper_loop.start()

    def cog_unload(self) -> None:
        self._keeper_loop.cancel()

    @tasks.loop(seconds=60)
    async def _keeper_loop(self) -> None:
        """Scan all active contracts for keeper-eligible functions and auto-execute them
        when their leading price/time condition is met."""
        for guild in self.bot.guilds:
            try:
                await self._keeper_run_guild(guild)
            except Exception as exc:
                log.error("Keeper loop error for guild %s: %s", guild.id, exc)

    @_keeper_loop.before_loop
    async def _before_keeper(self) -> None:
        await self.bot.wait_until_ready()

    async def _keeper_run_guild(self, guild: discord.Guild) -> None:
        contracts = await self.bot.db.get_all_active_contracts(guild.id)
        if not contracts:
            return

        # Build a price snapshot once per guild tick for efficiency
        price_rows = await self.bot.db.get_all_prices(guild.id)
        prices: dict[str, float] = {r["symbol"]: float(r["price"]) for r in price_rows}

        settings = await self.bot.db.get_guild_settings(guild.id)
        contracts_channel_id = settings.get("contracts_channel")

        for contract in contracts:
            definition = contract.get("definition") or {}
            if isinstance(definition, str):
                try:
                    definition = json.loads(definition)
                except Exception:
                    continue
            functions = definition.get("functions", {})

            for fn_name, fn_def in functions.items():
                if not fn_def.get("keeper"):
                    continue
                steps = fn_def.get("steps", [])
                if not steps:
                    continue

                # Check if there's already a pending keeper call in the mempool
                pending = await self.bot.db.get_pending_mempool(
                    guild.id, contract["network"], limit=50
                )
                already_queued = any(
                    a.get("action_type") == "contract_call"
                    and json.loads(a.get("payload", "{}")).get("address") == contract["address"]
                    and json.loads(a.get("payload", "{}")).get("function") == fn_name
                    for a in pending
                )
                if already_queued:
                    continue

                # Load contract state to resolve state references in conditions
                state = contract.get("state") or {}
                if isinstance(state, str):
                    try:
                        state = json.loads(state)
                    except Exception:
                        state = {}

                # Walk the first ops to find and evaluate the leading condition
                condition_met = True
                for step in steps:
                    op = step.get("op", "")
                    if op == "require_price":
                        # Resolve state-templated symbol/value
                        sym = step.get("symbol", "")
                        if sym.startswith("{{") and sym.endswith("}}"):
                            sym = state.get(sym[2:-2].strip(), sym)
                        value = step.get("value")
                        if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
                            value = state.get(value[2:-2].strip(), value)
                        resolved_step = dict(step, symbol=sym, value=value)
                        if not _keeper_check_price(resolved_step, prices):
                            condition_met = False
                        break
                    elif op == "require_time":
                        if not _keeper_check_time(step):
                            condition_met = False
                        break
                    elif op in ("get_state", "set_state", "require"):
                        # Non-price/time ops: skip to next op looking for a condition
                        continue
                    else:
                        # Hit an action op before finding a price/time condition  -  nothing to auto-trigger
                        condition_met = False
                        break

                if not condition_met:
                    continue

                # Condition met  -  submit a keeper call to the mempool
                keeper_uid = Config.COMMUNITY_RESERVE_USER_ID
                payload = {
                    "address":  contract["address"],
                    "function": fn_name,
                    "args":     {},
                }
                try:
                    action_id = await self.bot.db.add_to_mempool(
                        guild_id=guild.id,
                        user_id=keeper_uid,
                        network=contract["network"],
                        action_type="contract_call",
                        payload=payload,
                        gas_price="medium",
                        gas_fee=0.0,
                    )
                    log.info(
                        "Keeper queued %s.%s() → mempool #%s (guild %s)",
                        contract["address"], fn_name, action_id, guild.id,
                    )
                except Exception as exc:
                    log.error("Keeper failed to queue %s.%s: %s", contract["address"], fn_name, exc)
                    continue

                # Post notification to contracts channel if configured
                if contracts_channel_id:
                    ch = guild.get_channel(contracts_channel_id)
                    if ch:
                        embed = card(
                            "🤖 Keeper Triggered",
                            description=(
                                f"**{contract['name']}**.`{fn_name}()` condition met.\n"
                                f"`{contract['address']}`"
                            ),
                            color=C_INFO,
                        ).footer(f"Queued to {contract['network']} mempool • auto-executed by keeper").build()
                        try:
                            await ch.send(embed=embed)
                        except Exception:
                            pass

    async def cog_check(self, ctx) -> bool:
        if ctx.guild and not await module_allowed(ctx, "validators"):
            raise commands.CheckFailure("The **validators** module must be enabled to use contracts.")
        return True

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _queue_contract_action(
        self,
        ctx: DiscoContext,
        action_type: str,
        network: str,
        payload: dict,
        gas_price: str = "medium",
    ) -> int | None:
        """Deduct gas and submit a contract action to the mempool."""
        from cogs.validators import gas_fee_for_network
        gas_coin, gas_fee = await gas_fee_for_network(ctx.db, ctx.guild_id, action_type, gas_price, network)
        gas_cfg   = Config.TOKENS.get(gas_coin, {})
        gas_emoji = gas_cfg.get("emoji", "●")

        h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, gas_coin)
        gas_bal = to_human(h["amount"]) if h else 0.0
        if gas_bal < gas_fee:
            await ctx.reply_error(
                f"Need **{fmt_gas(gas_fee, gas_coin, gas_emoji)}** for gas. "
                f"You have **{fmt_gas(gas_bal, gas_coin, gas_emoji)}**."
            )
            return None

        await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(-gas_fee))
        action_id = await ctx.db.add_to_mempool(
            guild_id=ctx.guild_id,
            user_id=ctx.author.id,
            network=network,
            action_type=action_type,
            payload=payload,
            gas_price=gas_price,
            gas_fee=to_raw(gas_fee),
        )
        return action_id

    def _parse_flags(self, flags: str) -> tuple[str, dict]:
        """Parse gas tier and arg key=val from flags string.
        Returns (gas_price, {arg_key: arg_value})."""
        gas_price = "medium"
        args: dict = {}
        parts = flags.split()
        i = 0
        while i < len(parts):
            p = parts[i]
            if p in ("gas", "gas-price") and i + 1 < len(parts):
                gas_price = parts[i + 1].lower()
                i += 2
                continue
            if p == "high":
                gas_price = "high"; i += 1; continue
            if p == "low":
                gas_price = "low"; i += 1; continue
            if p == "arg" and i + 1 < len(parts):
                kv = parts[i + 1]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    # Try numeric coercion
                    try:
                        args[k] = float(v) if "." in v else int(v)
                    except ValueError:
                        args[k] = v
                i += 2; continue
            i += 1
        return gas_price, args

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="contract", aliases=["ct"], invoke_without_command=True, with_app_command=False)
    @guild_only
    async def contract(self, ctx: DiscoContext) -> None:
        """On-chain smart contracts: deploy, call, fund, withdraw, info, list, events, pause, resume.

        Quick examples:
          .contract deploy MyEscrow arc escrow
          .contract deploy LimitBot "Arcadia Network" limit_order desc "Auto sells ARC at $5000"
          .contract list
          .contract info 0x<address>
          .contract call 0x<address> place arg token=ARC arg amount=1.0 arg price=3500
          .contract call 0x<address> execute
          .contract call 0x<address> cancel
          .contract fund 0x<address> ARC 0.5
          .contract events 0x<address>

        Contract types: limit_order | escrow | vesting | multisig | custom
        OpSet (steps in custom contracts): TRANSFER, SWAP, LOCK, UNLOCK, EMIT, REQUIRE, SET_STATE, GET_STATE
        """
        if await suggest_subcommand(ctx, self.contract):
            return
        await ctx.send_help(ctx.command)

    @contract.command(name="deploy")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_deploy(
        self, ctx: DiscoContext, name: str, network: str, ctype: str = "custom", *, flags: str = ""
    ) -> None:
        """Deploy a smart contract.

        Usage: .contract deploy <name> <network> [type] [flags]
        Types: limit_order | escrow | vesting | multisig | custom
        Flags:
          desc "description"   Set a human-readable description for this contract
          def {json}           Provide a custom JSON definition (for type=custom)

        Examples:
          .contract deploy MyEscrow arc escrow desc "Holds funds until both parties confirm"
          .contract deploy LimitBot arc limit_order desc "Auto sell ARC above $5000"

        Gas is required to submit the deploy transaction.
        """
        # Resolve network shorthand via the canonical normalizer.
        full_network = normalize_network_full(network) or network

        # Resolve template or custom definition
        if ctype in TEMPLATES:
            definition = TEMPLATES[ctype]
        else:
            # Try to parse def JSON from flags
            import re as _re
            m = _re.search(r'def\s+(\{.+\})', flags)
            if m:
                try:
                    definition = json.loads(m.group(1))
                except json.JSONDecodeError as e:
                    await ctx.reply_error(f"Invalid JSON definition: {e}")
                    return
            else:
                definition = {"functions": {}}

        try:
            ContractEngine.validate_definition(definition)
        except ValueError as e:
            await ctx.reply_error(f"Invalid contract definition: {e}")
            return

        gas_price, _ = self._parse_flags(flags)

        # Parse desc flag
        import re as _re2
        desc_match = _re2.search(r'desc\s+"([^"]*)"', flags) or _re2.search(r"desc\s+'([^']*)'", flags) or _re2.search(r'desc\s+([^\s]+(?:\s+[^\s]+)*?)(?:\s+(?:def|gas|arg)\b|$)', flags)
        description = desc_match.group(1).strip() if desc_match else ""

        payload = {
            "name":        name,
            "network":     full_network,
            "type":        ctype,
            "definition":  definition,
            "description": description,
        }
        action_id = await self._queue_contract_action(
            ctx, "contract_deploy", full_network, payload, gas_price
        )
        if action_id is None:
            return

        tier_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}[gas_price]
        _b = card(
            "📜 Contract Deploy Queued",
            description=(
                f"**{name}** (`{ctype}`) on **{full_network}**\n"
                f"Mempool `#{action_id}`  -  will be deployed in the next validator block."
            ),
            color=C_AMBER,
        )
        if description:
            _b.field("Description", description[:80], True)
        functions = list(definition.get("functions", {}).keys())
        _b.field("Gas Price", f"{tier_emoji} {gas_price.title()}", True)
        _b.field("Functions", ", ".join(f"`{f}`" for f in functions) or "none", True)
        _b.footer("Use .contract list to find your contract address after confirmation.")
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="call")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_call(
        self, ctx: DiscoContext, address: str, function: str, *, flags: str = ""
    ) -> None:
        """Call a function on a deployed contract.

        Usage: .contract call <address> <function> [arg key=val ...] [gas high|low]

        Example:
          .contract call 0xabc123 place arg token_in=ARC arg token_out=USDC \\
                                        arg amount=0.5 arg target_price=1800 \\
                                        arg pool_id=ETH_USDC
        """
        contract = await ctx.db.get_contract(ctx.guild_id, address)
        if not contract:
            await ctx.reply_error(f"Contract `{address}` not found on this server.")
            return
        if contract["is_paused"]:
            await ctx.reply_error(f"Contract `{address}` is currently paused.")
            return

        fns = contract["definition"].get("functions", {})
        if function not in fns:
            available = ", ".join(f"`{f}`" for f in fns)
            await ctx.reply_error(f"Function `{function}` not found. Available: {available}")
            return

        gas_price, args = self._parse_flags(flags)

        payload = {
            "address":  address,
            "function": function,
            "args":     args,
        }
        action_id = await self._queue_contract_action(
            ctx, "contract_call", contract["network"], payload, gas_price
        )
        if action_id is None:
            return

        tier_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}[gas_price]
        fn_def = fns[function]
        _b = card(
            "⚡ Contract Call Queued",
            description=(
                f"**{contract['name']}**.`{function}()`\n"
                f"`{address}`\n\n"
                f"{fn_def.get('description', '')}"
            ),
            color=C_AMBER,
        )
        if args:
            args_text = "\n".join(f"`{k}` = `{v}`" for k, v in args.items())
            if len(args_text) > 1024:
                args_text = args_text[:1010] + "\n*(…)*"
            _b.field("Arguments", args_text, True)
        _b.field("Gas Price",  f"{tier_emoji} {gas_price.title()}", True)
        _b.field("Mempool ID", f"`#{action_id}`",                   True)
        _b.footer(f"Executes in next {contract['network']} validator block.")
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="info")
    @guild_only
    async def contract_info(self, ctx: DiscoContext, address: str) -> None:
        """Show detailed info about a deployed contract.
        Usage: .contract info <address>
        """
        contract = await ctx.db.get_contract(ctx.guild_id, address)
        if not contract:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return

        vuid = contract["virtual_uid"]
        fns  = contract["definition"].get("functions", {})

        # Gather contract balances
        balances: list[str] = []
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        for sym in list(all_tokens.keys()) + ["USD"]:
            if sym == "USD":
                user_row = await ctx.db.get_user(vuid, ctx.guild_id)
                bal = user_row.h("wallet") if user_row else 0.0
            else:
                h = await ctx.db.get_holding(vuid, ctx.guild_id, sym)
                bal = h.h("amount") if h else 0.0
            if bal > 0:
                cfg = Config.TOKENS.get(sym, {})
                balances.append(f"{cfg.get('emoji','●')}`{sym}` {fmt_token(bal, sym)}")

        owner = ctx.guild.get_member(contract["owner_id"])
        owner_str = owner.display_name if owner else mention(contract["owner_id"], ctx.guild, self.bot)

        desc_text = contract.get("description", "")
        _b = card(
            f"📜 {contract['name']}",
            description=(f"`{address}`\n\n*{desc_text}*" if desc_text else f"`{address}`"),
            color=C_INFO,
        )
        _b.field("Type",       contract["type"],           True)
        _b.field("Network",    contract["network"],        True)
        _b.field("Owner",      owner_str,                  True)
        _b.field("Calls",      str(contract["call_count"]), True)
        _b.field("Status",     "⏸ Paused" if contract["is_paused"] else "✅ Active", True)

        dep_ts = fmt_ts(contract["deployed_at"], "%Y-%m-%d %H:%M UTC")
        _b.field("Deployed",   dep_ts,                     True)

        fn_list = "\n".join(
            f"`{fname}`  -  {fdef.get('description','')[:60]}"
            for fname, fdef in fns.items()
        )
        _b.field("Functions",  fn_list or "none",          False)

        if balances:
            _b.field("Balances", "\n".join(balances),     False)

        state = contract["state"]
        if state:
            state_lines = [f"`{k}` = `{v}`" for k, v in list(state.items())[:10]]
            if len(state) > 10:
                state_lines.append(f"…and {len(state)-10} more")
            _b.field("State", "\n".join(state_lines),     False)

        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="list", aliases=["ls"])
    @guild_only
    async def contract_list(self, ctx: DiscoContext, network: str = "") -> None:
        """List all deployed contracts on this server.
        Usage: .contract list [network]
        """
        net_filter = (normalize_network_full(network) or network) if network else None
        contracts = await ctx.db.get_contracts(ctx.guild_id, net_filter)

        if not contracts:
            msg = f"No contracts on **{net_filter}**." if net_filter else "No contracts deployed yet."
            await ctx.reply_error(msg)
            return

        _b = card(
            "📜 Smart Contracts",
            description=f"{'Network: ' + net_filter if net_filter else 'All networks'}  -  {len(contracts)} contract(s)",
            color=C_INFO,
        )
        for c in contracts[:15]:
            owner = ctx.guild.get_member(c["owner_id"])
            owner_str = owner.display_name if owner else mention(c["owner_id"], ctx.guild, self.bot)
            status = "⏸" if c["is_paused"] else "✅"
            fns = list(c["definition"].get("functions", {}).keys())
            _b.field(
                f"{status} {c['name']} [{c['type']}]",
                (
                    f"`{c['address']}`\n"
                    f"Network: {c['network']} | Owner: {owner_str}\n"
                    f"Calls: {c['call_count']} | Functions: {', '.join(f'`{f}`' for f in fns)}"
                ),
                False,
            )
        if len(contracts) > 15:
            _b.footer(f"Showing 15 of {len(contracts)} contracts.")
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="events", aliases=["log"])
    @guild_only
    async def contract_events(self, ctx: DiscoContext, address: str, limit: int = 10) -> None:
        """Show recent events emitted by a contract.
        Usage: .contract events <address> [limit]
        """
        contract = await ctx.db.get_contract(ctx.guild_id, address)
        if not contract:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return

        events = await ctx.db.get_contract_events(ctx.guild_id, address, min(limit, 25))
        if not events:
            await ctx.reply_error("No events emitted yet.")
            return

        _b = card(
            f"📋 Events  -  {contract['name']}",
            description=f"`{address}`",
            color=C_INFO,
        )
        for ev in events:
            ts_str = fmt_ts(ev["ts"])
            data_str = ", ".join(f"{k}={v}" for k, v in ev["data"].items()) if ev["data"] else ""
            _b.field(
                f"**{ev['event']}**  -  {ts_str}",
                f"`{data_str}`" if data_str else "*no data*",
                True,
            )
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="fund")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_fund(
        self, ctx: DiscoContext, address: str, symbol: str, amount: float
    ) -> None:
        """Send tokens directly to a contract's balance (owner or anyone).
        Usage: .contract fund <address> <symbol> <amount>
        This is an instant transfer  -  no gas required beyond normal tx.
        """
        contract = await ctx.db.get_contract(ctx.guild_id, address)
        if not contract:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return
        if contract["is_paused"]:
            await ctx.reply_error("Contract is paused.")
            return

        symbol = symbol.upper()
        if amount <= 0:
            await ctx.reply_error("Amount must be positive.")
            return

        vuid = contract["virtual_uid"]
        if symbol == "USD":
            if ctx.user_row.h("wallet") < amount:
                await ctx.reply_error(f"Insufficient USD. You have **{fmt_usd(ctx.user_row.h('wallet'))}**.")
                return
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -to_raw(amount))
            await ctx.db.ensure_user(vuid, ctx.guild_id)
            await ctx.db.update_wallet(vuid, ctx.guild_id, to_raw(amount))
        else:
            h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
            bal = h.h("amount") if h else 0.0
            if bal < amount:
                await ctx.reply_error(f"Insufficient {symbol}. You have **{fmt_token(bal, symbol)}**.")
                return
            await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, -to_raw(amount))
            await ctx.db.update_holding(vuid, ctx.guild_id, symbol, to_raw(amount))

        cfg = Config.TOKENS.get(symbol, {})
        await ctx.reply_success(
            f"Sent **{fmt_token(amount, symbol, cfg.get('emoji', ''))}** to contract **{contract['name']}**.",
            title="💸 Contract Funded",
        )
        await ctx.bot.bus.publish(
            "contract_event",
            guild=ctx.guild,
            contract=contract,
            action="fund",
            caller_id=ctx.author.id,
            block_id=None,
            events=[],
            extra={"symbol": symbol, "amount": amount},
        )

    @contract.command(name="withdraw")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_withdraw(
        self, ctx: DiscoContext, address: str, symbol: str, amount: float
    ) -> None:
        """Withdraw tokens from a contract's balance. Owner only.
        Usage: .contract withdraw <address> <symbol> <amount>
        """
        contract = await ctx.db.get_contract(ctx.guild_id, address)
        if not contract:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return
        if contract["owner_id"] != ctx.author.id:
            await ctx.reply_error("Only the contract owner can withdraw.")
            return

        symbol = symbol.upper()
        vuid   = contract["virtual_uid"]

        if symbol == "USD":
            user_row = await ctx.db.get_user(vuid, ctx.guild_id)
            bal = user_row.h("wallet") if user_row else 0.0
            if bal < amount:
                await ctx.reply_error(f"Contract only has **{fmt_usd(bal)}** USD.")
                return
            await ctx.db.update_wallet(vuid, ctx.guild_id, -to_raw(amount))
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(amount))
        else:
            h = await ctx.db.get_holding(vuid, ctx.guild_id, symbol)
            bal = h.h("amount") if h else 0.0
            if bal < amount:
                await ctx.reply_error(f"Contract only has **{fmt_token(bal, symbol)}**.")
                return
            await ctx.db.update_holding(vuid, ctx.guild_id, symbol, -to_raw(amount))
            await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, to_raw(amount))

        cfg = Config.TOKENS.get(symbol, {})
        await ctx.reply_success(
            f"Withdrew **{fmt_token(amount, symbol, cfg.get('emoji', ''))}** from **{contract['name']}**.",
            title="💰 Withdrawn",
        )
        await ctx.bot.bus.publish(
            "contract_event",
            guild=ctx.guild,
            contract=contract,
            action="withdraw",
            caller_id=ctx.author.id,
            block_id=None,
            events=[],
            extra={"symbol": symbol, "amount": amount},
        )

    @contract.command(name="txs", aliases=["transactions", "history"])
    @guild_only
    async def contract_txs(self, ctx: DiscoContext, address: str, limit: int = 15) -> None:
        """Show transaction history for a contract's balance address.
        Usage: .contract txs <address> [limit]
        """
        contract = await ctx.db.get_contract(ctx.guild_id, address)
        if not contract:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return

        vuid = contract["virtual_uid"]
        txs  = await ctx.db.get_user_tx_history(vuid, ctx.guild_id, min(limit, 25))

        if not txs:
            await ctx.reply_error("No transactions recorded for this contract yet.")
            return

        _b = card(
            f"📋 Transactions  -  {contract['name']}",
            description=f"`{address}` · showing {len(txs)} tx(s)",
            color=C_INFO,
        )
        for tx in txs:
            ts_str = fmt_ts(tx["ts"])
            sym_in   = tx.get("symbol_in", "")
            amt_in   = tx.get("amount_in", 0) or 0
            sym_out  = tx.get("symbol_out", "")
            amt_out  = tx.get("amount_out", 0) or 0
            tx_type  = tx.get("tx_type", "?")
            block    = f" · blk #{tx['block_num']}" if tx.get("block_num") else ""

            if sym_in and sym_out and sym_in != sym_out:
                detail = f"{fmt_token(amt_in, sym_in)} → {fmt_token(amt_out, sym_out)}"
            elif sym_in:
                detail = fmt_token(amt_in, sym_in)
            elif sym_out:
                detail = f"→ {sym_out}"
            else:
                detail = ""

            _b.field(
                f"`{tx_type}`  -  {ts_str}{block}",
                detail or "*no detail*",
                True,
            )

        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @contract.command(name="pause")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_pause(self, ctx: DiscoContext, address: str) -> None:
        """Pause a contract, preventing all calls. Owner only."""
        contract = await ctx.db.get_contract(ctx.guild_id, address)
        if not contract:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return
        if contract["owner_id"] != ctx.author.id:
            await ctx.reply_error("Only the contract owner can pause it.")
            return
        await ctx.db.pause_contract(address, True)
        await ctx.reply_success(f"Contract **{contract['name']}** is now paused.", title="⏸ Paused")
        await ctx.bot.bus.publish(
            "contract_event",
            guild=ctx.guild,
            contract=contract,
            action="pause",
            caller_id=ctx.author.id,
            block_id=None,
            events=[],
            extra={},
        )

    @contract.command(name="resume")
    @guild_only
    @no_bots
    @ensure_registered
    async def contract_resume(self, ctx: DiscoContext, address: str) -> None:
        """Resume a paused contract. Owner only."""
        contract = await ctx.db.get_contract(ctx.guild_id, address)
        if not contract:
            await ctx.reply_error(f"Contract `{address}` not found.")
            return
        if contract["owner_id"] != ctx.author.id:
            await ctx.reply_error("Only the contract owner can resume it.")
            return
        await ctx.db.pause_contract(address, False)
        await ctx.reply_success(f"Contract **{contract['name']}** is now active.", title="▶️ Resumed")
        await ctx.bot.bus.publish(
            "contract_event",
            guild=ctx.guild,
            contract=contract,
            action="resume",
            caller_id=ctx.author.id,
            block_id=None,
            events=[],
            extra={},
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Contracts(bot))
