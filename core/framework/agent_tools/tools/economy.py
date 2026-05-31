"""
core/framework/agent_tools/tools/economy.py -- economy state mutation tools.

Every mutation tool here is classified MUTATE or DANGER, which means the
executor requires an explicit approval flag before the handler runs. The AI
alone cannot call these.

    economy.transfer      move wallet funds between two players (MUTATE).
    economy.mint          admin-only: create new supply of a token (DANGER).
    economy.burn          admin-only: destroy supply of a token (DANGER).

These all share a single implementation path so the logic lives in one place
instead of duplicating wallet arithmetic into multiple tools.
"""
from __future__ import annotations

import logging

from core.config import Config
from core.framework.scale import to_raw

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.economy")


# ── economy.transfer ─────────────────────────────────────────────────────────

@tool(
    name="economy.transfer",
    summary=(
        "Send wallet USD from the caller to another player. Runs through "
        "services.transfer.execute_transfer so balance checks and audit "
        "hooks fire. Requires approval."
    ),
    risk=RiskLevel.MUTATE,
    category="economy",
    params=[
        ParamSpec("amount", "float", min=0.0,
                  description="USD amount to transfer."),
        ParamSpec("to_user_id", "uid", description="Recipient player id."),
    ],
)
async def transfer(ctx: ToolContext, args: dict) -> ToolResult:
    amt = float(args["amount"])
    to_uid = int(args["to_user_id"])
    if to_uid == int(ctx.user_id):
        return ToolResult.fail("cannot_self_transfer")
    if amt <= 0:
        return ToolResult.fail("amount must be positive")

    try:
        from services.transfer import execute_transfer
    except Exception as exc:
        log.warning("[economy.transfer] transfer service unavailable: %s", exc)
        return ToolResult.fail(f"service_unavailable: {exc}")

    try:
        result = await execute_transfer(
            ctx.db,
            guild_id=int(ctx.guild_id),
            sender_id=int(ctx.user_id),
            recipient_id=to_uid,
            amount=amt,
        )
    except Exception as exc:
        log.warning("[economy.transfer] service raised: %s", exc)
        return ToolResult.fail(f"transfer_failed: {exc}")

    if not result.success:
        return ToolResult.fail(f"transfer_rejected: {result.error}")

    if ctx.bus is not None:
        try:
            await ctx.bus.publish(
                "transfer_sent",
                guild_id=int(ctx.guild_id),
                from_user_id=int(ctx.user_id),
                to_user_id=to_uid,
                amount=amt,
                tx_hash=result.tx_hash,
                actor=ctx.actor,
            )
        except Exception:
            pass

    return ToolResult.success({
        "amount_sent": amt,
        "to_user_id": to_uid,
        "new_wallet_balance": result.new_balance,
        "tx_hash": result.tx_hash,
    })


# ── economy.mint (DANGER) ────────────────────────────────────────────────────

@tool(
    name="economy.mint",
    summary=(
        "Admin-only: mint new supply of a token directly to a target wallet. "
        "Irreversible; always requires explicit approval."
    ),
    risk=RiskLevel.DANGER,
    category="economy",
    params=[
        ParamSpec("symbol", "symbol", description="Token symbol."),
        ParamSpec("amount", "float", min=0.0, description="Human-unit amount to mint."),
        ParamSpec("to_user_id", "uid", description="Recipient player id."),
        ParamSpec("reason", "str", required=False, default="agent_mint",
                  description="Audit reason string."),
    ],
)
async def mint(ctx: ToolContext, args: dict) -> ToolResult:
    return await _admin_supply_op(ctx, args, direction=+1)


# ── economy.burn (DANGER) ────────────────────────────────────────────────────

@tool(
    name="economy.burn",
    summary=(
        "Admin-only: burn supply of a token from a target wallet. "
        "Irreversible; always requires explicit approval."
    ),
    risk=RiskLevel.DANGER,
    category="economy",
    params=[
        ParamSpec("symbol", "symbol", description="Token symbol."),
        ParamSpec("amount", "float", min=0.0, description="Human-unit amount to burn."),
        ParamSpec("from_user_id", "uid", description="Source player id."),
        ParamSpec("reason", "str", required=False, default="agent_burn",
                  description="Audit reason string."),
    ],
)
async def burn(ctx: ToolContext, args: dict) -> ToolResult:
    # Normalize to the shared path so we only write adjust logic once.
    return await _admin_supply_op(
        ctx,
        {
            "symbol": args["symbol"],
            "amount": args["amount"],
            "to_user_id": args["from_user_id"],
            "reason": args.get("reason") or "agent_burn",
        },
        direction=-1,
    )


async def _admin_supply_op(
    ctx: ToolContext, args: dict, direction: int
) -> ToolResult:
    sym = args["symbol"]
    amt = float(args["amount"])
    uid = int(args["to_user_id"])
    reason = str(args.get("reason") or "agent_supply")

    tok = Config.TOKENS.get(sym)
    if tok is None:
        return ToolResult.fail(f"unknown_token: {sym}")
    if amt <= 0:
        return ToolResult.fail("amount must be positive")

    # Verify caller is a guild admin.  We intentionally do not trust the
    # approved flag alone for DANGER economy ops: approval + admin perm.
    if not await _is_guild_admin(ctx):
        return ToolResult.fail("forbidden: requires guild admin")

    delta_raw = to_raw(amt) * direction

    async with ctx.db.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT amount FROM crypto_holdings "
                "WHERE guild_id=$1 AND user_id=$2 AND symbol=$3 FOR UPDATE",
                int(ctx.guild_id), uid, sym,
            )
            current = int(row["amount"]) if row else 0
            new_amt = current + delta_raw
            if new_amt < 0:
                return ToolResult.fail("insufficient_balance_on_target")
            if row is None:
                await conn.execute(
                    "INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount) "
                    "VALUES ($1,$2,$3,$4)",
                    uid, int(ctx.guild_id), sym, new_amt,
                )
            else:
                await conn.execute(
                    "UPDATE crypto_holdings SET amount=$1 "
                    "WHERE guild_id=$2 AND user_id=$3 AND symbol=$4",
                    new_amt, int(ctx.guild_id), uid, sym,
                )
            await conn.execute(
                "UPDATE crypto_prices "
                "SET circulating_supply = GREATEST(0, circulating_supply + $1) "
                "WHERE guild_id=$2 AND symbol=$3",
                delta_raw, int(ctx.guild_id), sym,
            )

    return ToolResult.success({
        "direction": "mint" if direction > 0 else "burn",
        "symbol": sym,
        "amount": amt,
        "target_user_id": uid,
        "reason": reason,
    })


async def _is_guild_admin(ctx: ToolContext) -> bool:
    """Return True if the caller has guild-admin privileges.

    Checks:
      1. the admin_users dashboard allowlist (most explicit)
      2. the bot_manager_id on guild_settings
    No Discord object access -- the context is decoupled from discord.py.
    """
    row = await ctx.db.fetch_one(
        "SELECT 1 FROM admin_users WHERE guild_id=$1 AND user_id=$2",
        int(ctx.guild_id), int(ctx.user_id),
    )
    if row is not None:
        return True
    settings = await ctx.db.fetch_one(
        "SELECT bot_manager_id FROM guild_settings WHERE guild_id=$1",
        int(ctx.guild_id),
    )
    if settings is None:
        return False
    mgr = settings.get("bot_manager_id")
    return mgr is not None and int(mgr) == int(ctx.user_id)
