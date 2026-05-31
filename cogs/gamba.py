"""cogs/gamba.py  -  Gamba Network player surface.

Houses the unified ``,gamba`` command group:

    ,gamba stake [SYM] [<amt|all>] [gbc|bud]   open the stake panel or stake directly
    ,gamba unstake SYM <amt|all>               unlock a game token; auto-claims yield
    ,gamba claim [SYM]                         pay out pending yield across one / all stakes
    ,gamba yield SYM <gbc|bud>                 flip the yield target on a stake
    ,gamba cashout <amt|all>                   burn GBC -> credit USD at oracle minus impact
    ,gamba autocompound SYM [on|off]           roll yield back into the same stake
    ,gamba stakes                              overview of every active stake position
    ,gamba info                                network coin + game-token reference card
    ,gamba shop                                browse the three GBC-priced consumables
    ,gamba buy <item> [qty]                    purchase a consumable with GBC
    ,gamba inventory                           show owned consumables

Each stake position picks one yield target -- GBC (default) or BUD --
via the optional final arg on ``,gamba stake`` or by ``,gamba yield``
on an existing position.

Every embed uses the framework helpers (``card`` / ``stake_receipt`` /
``claim_receipt`` / ``StakePanelView``) so the look-and-feel matches
``,fish stake``, ``,farm stake``, ``,craft stake``, etc.
"""
from __future__ import annotations

import logging
from typing import Optional

from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.scale import to_human, to_raw
from core.framework.staking import (
    StakeAdapter, StakePanelView, StakeToken,
    claim_receipt, stake_receipt,
)
from core.framework.ui import (
    C_AMBER, C_GOLD, C_INFO, C_NEUTRAL, C_SUCCESS, fmt_token, fmt_usd,
)
from configs.items_config import SHOP_ITEMS as _SHOP_ITEMS
from services import gamba as gamba_svc

log = logging.getLogger(__name__)


_TOKEN_EMOJI: dict[str, str] = {
    "GBC":    "\U0001F3B0",
    "BUD":    "\U0001F436",   # dog face -- mirrors Config.TOKENS["BUD"]["emoji"]
    "GAMBIT": "♞",
    "CROWN":  "\U0001F451",
    "VEIN":   "\U0001F48E",
    "PIP":    "\U0001F3B2",
    "EDGE":   "\U0001FA99",
    "ACE":    "\U0001F0A1",
    "NOIR":   "⚫",
    "CHERRY": "\U0001F352",
}


def _parse_yield_target(text: str | None) -> str | None:
    """Resolve a 'gbc' / 'bud' (case-insensitive) flag into the canonical symbol.

    Returns None when ``text`` is empty / not a recognised target so the
    caller can decide whether to default or error. The string ``"none"``
    is treated as not-a-target as well.
    """
    s = (text or "").strip().upper()
    if s in ("GBC", "BUD"):
        return s
    return None


def _is_amount_text(text: str | None) -> bool:
    """True when ``text`` looks like a numeric stake amount (or ``all``)."""
    s = (text or "").strip().lower()
    if not s:
        return False
    if s in ("all", "max", "everything"):
        return True
    s = s.replace(",", "").replace("_", "")
    try:
        float(s)
    except ValueError:
        return False
    return True


async def _oracle_price(ctx: DiscoContext, symbol: str) -> float:
    """Read the live USD oracle for ``symbol`` from crypto_prices."""
    try:
        row = await ctx.db.get_price(symbol, ctx.guild_id)
    except Exception:
        return 0.0
    if not row:
        return 0.0
    try:
        return float(row["price"])
    except Exception:
        return 0.0


async def _holding_raw(ctx: DiscoContext, symbol: str) -> int:
    """Read the user's current raw balance of a Gamba Network token.

    Gamba Network tokens (GBC + the eight game-themed tokens) live in
    ``wallet_holdings`` on the ``gam`` network short, same as every other
    earn-only network coin. Always read through this helper so the
    storage layout stays in one place.
    """
    h = await ctx.db.get_wallet_holding(
        ctx.author.id, ctx.guild_id, gamba_svc.GAMBA_NETWORK_SHORT, symbol,
    )
    return int(h["amount"]) if h else 0


def _parse_amount_or_all(text: str) -> tuple[bool, float]:
    """Parse ``"all"`` / ``"max"`` / ``"everything"`` or a numeric string.

    Returns ``(is_all, human_amount)`` where ``human_amount`` is meaningful
    only when ``is_all`` is False. Raises ``ValueError`` on bad input so
    the command can surface a clean error.
    """
    s = (text or "").strip().lower()
    if not s:
        raise ValueError("Pass a number or `all`.")
    if s in ("all", "max", "everything"):
        return True, 0.0
    s = s.replace(",", "").replace("_", "")
    try:
        amt = float(s)
    except ValueError as exc:
        raise ValueError(f"Invalid amount: `{text}`.") from exc
    if amt <= 0:
        raise ValueError("Amount must be positive.")
    return False, amt


# ============================================================================
# Cog
# ============================================================================

class GambaCog(commands.Cog):
    """Gamba Network: GBC + 8 game-themed earn-only tokens + shop."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ── ,gamba root ──────────────────────────────────────────────────────

    @commands.group(
        name="gamba", aliases=["gam", "gambanet"], invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba(self, ctx: DiscoContext) -> None:
        """Gamba Network hub. ``,gamba info`` for the reference card."""
        await self._info(ctx)

    # ── stake (panel or direct) ──────────────────────────────────────────

    @gamba.command(name="stake")
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_stake(
        self, ctx: DiscoContext,
        symbol: Optional[str] = None,
        amount: Optional[str] = None,
        target: Optional[str] = None,
    ) -> None:
        """Stake a game token to passively drip GBC (default) or BUD.

        With no symbol: opens the unified panel listing every game-
        token stake position. With ``all`` / ``everything``: stakes the
        full wallet balance of every game token at once. With a
        symbol + amount: stakes that one position. Optional final
        ``gbc`` / ``bud`` flag opens (or rotates) the position to drip
        the chosen target -- existing pending is paid out at the OLD
        target's rate before the flip.
        """
        if not symbol:
            await self._open_stake_panel(ctx, focus_symbol=None)
            return
        sym_low = symbol.lower()
        if sym_low in ("all", "everything", "max"):
            # Optional ``,gamba stake all <gbc|bud>`` -- bulk-stake every
            # game token AND set every freshly-touched position to the
            # given target. ``amount`` slot carries the target here.
            await self._stake_everything(ctx, target=_parse_yield_target(amount))
            return
        if sym_low in ("autocompound", "ac", "compound"):
            await self._set_autocompound_all(
                ctx, (amount or "toggle").strip().lower(),
            )
            return
        sym = symbol.upper()
        if sym not in gamba_svc.GAME_TOKEN_SET:
            await ctx.reply_error(
                f"`{sym}` is not a Gamba Network game token. Stakeable: "
                f"{', '.join(sorted(gamba_svc.GAME_TOKEN_SET))}."
            )
            return
        if not amount:
            await self._open_stake_panel(ctx, focus_symbol=sym)
            return
        # ``amount`` may actually be the target flag if the user typed
        # ``,gamba stake pip bud`` (no quantity). Treat that as "open the
        # focused panel after flipping the target on the existing
        # position", not a stake.
        if (
            target is None
            and _parse_yield_target(amount) is not None
            and not _is_amount_text(amount)
        ):
            new_target = _parse_yield_target(amount)
            try:
                final, _ = await gamba_svc.set_yield_target(
                    ctx.db, ctx.guild_id, ctx.author.id, sym, new_target,
                )
            except ValueError as exc:
                await ctx.reply_error(str(exc))
                return
            await ctx.reply_success(
                f"Yield target on **{sym}** set to **{final}**.",
                title="\U0001F501 Yield target",
            )
            return
        try:
            is_all, amt_h = _parse_amount_or_all(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if is_all:
            req_raw = await _holding_raw(ctx, sym)
            if req_raw <= 0:
                await ctx.reply_error(f"You have no **{sym}** to stake.")
                return
        else:
            req_raw = int(to_raw(amt_h))
        explicit_target = _parse_yield_target(target)
        try:
            res = await gamba_svc.stake(
                ctx.db, ctx.guild_id, ctx.author.id, sym, req_raw,
                yield_target=explicit_target,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        ora = await _oracle_price(ctx, sym)
        rate = gamba_svc.STAKE_RATE_BY_TARGET.get(
            res.yield_target, gamba_svc.STAKE_GBC_PER_DAY,
        )
        await ctx.reply(
            embed=stake_receipt(
                action="Staked",
                stake_symbol=sym, stake_emoji=_TOKEN_EMOJI.get(sym, ""),
                delta_h=to_human(int(res.delta_raw)),
                total_h=to_human(int(res.staked_raw)),
                stake_oracle=ora,
                note=(
                    f"{sym} locked -- earns "
                    f"{rate:g} {res.yield_target} per {sym} per day."
                ),
            ),
            mention_author=False,
        )

    # ── unstake ──────────────────────────────────────────────────────────

    @gamba.command(name="unstake", aliases=["withdraw"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_unstake(
        self, ctx: DiscoContext,
        symbol: Optional[str] = None,
        amount: Optional[str] = None,
    ) -> None:
        """Unstake a game token back to your wallet.

        ``,gamba unstake SYM all``     -- unwind one position (auto-claims GBC)
        ``,gamba unstake all``         -- unwind every game-token position
        """
        if not symbol:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}gamba unstake SYM <amt|all>`  ·  "
                f"`{ctx.prefix}gamba unstake all`"
            )
            return
        if symbol.lower() in ("all", "everything", "max"):
            await self._unstake_everything(ctx)
            return
        if not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}gamba unstake {symbol.upper()} <amt|all>`"
            )
            return
        sym = (symbol or "").upper()
        if sym not in gamba_svc.GAME_TOKEN_SET:
            await ctx.reply_error(f"`{sym}` is not a Gamba Network game token.")
            return
        try:
            is_all, amt_h = _parse_amount_or_all(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if is_all:
            snap = await gamba_svc.get_stake(
                ctx.db, ctx.guild_id, ctx.author.id, sym,
            )
            req_raw = int(snap.staked_raw)
            if req_raw <= 0:
                await ctx.reply_error(f"You have no {sym} staked.")
                return
        else:
            req_raw = int(to_raw(amt_h))
        try:
            res = await gamba_svc.unstake(
                ctx.db, ctx.guild_id, ctx.author.id, sym, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        sym_oracle = await _oracle_price(ctx, sym)
        yield_oracle = await _oracle_price(ctx, res.yield_target)
        note = f"{sym} unlocked -- no oracle impact (not a trade)."
        if res.yield_paid_raw > 0:
            note += (
                f" {res.yield_target} minted from stake yield "
                f"-- no oracle impact."
            )
        await ctx.reply(
            embed=stake_receipt(
                action="Unstaked",
                stake_symbol=sym, stake_emoji=_TOKEN_EMOJI.get(sym, ""),
                delta_h=to_human(abs(int(res.delta_raw))),
                total_h=to_human(int(res.staked_raw)),
                stake_oracle=sym_oracle,
                yield_symbol=res.yield_target,
                yield_emoji=_TOKEN_EMOJI.get(res.yield_target, ""),
                yield_paid_h=to_human(int(res.yield_paid_raw)),
                yield_oracle=yield_oracle,
                note=note,
            ),
            mention_author=False,
        )

    # ── claim ────────────────────────────────────────────────────────────

    @gamba.command(name="claim", aliases=["harvest"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_claim(
        self, ctx: DiscoContext, symbol: Optional[str] = None,
    ) -> None:
        """Pay out accrued yield. No symbol = claim across every position.

        Each position's yield target is honoured -- GBC stakes pay GBC,
        BUD stakes pay BUD. The receipt shows the most-recent symbol's
        target; bulk claims also list the per-target totals when both
        currencies were paid.
        """
        sym = symbol.upper() if symbol else None
        if sym is not None and sym not in gamba_svc.GAME_TOKEN_SET:
            await ctx.reply_error(f"`{sym}` is not a Gamba Network game token.")
            return
        # Snapshot per-target pending BEFORE the claim so the receipt can
        # show how the total split between GBC and BUD even when the
        # service returns a single ``yield_target`` field on its result.
        pre_totals = await gamba_svc.total_accrued_yield(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        try:
            res = await gamba_svc.claim(
                ctx.db, ctx.guild_id, ctx.author.id, sym,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        yield_oracle = await _oracle_price(ctx, res.yield_target)
        notes = [f"{res.yield_target} minted from stake yield -- no oracle impact."]
        # Cross-target hint: if the bulk claim crossed both targets, list
        # the per-target totals so the player sees the breakdown.
        if not sym and pre_totals.get(gamba_svc.YIELD_TARGET_GBC, 0) > 0 \
                  and pre_totals.get(gamba_svc.YIELD_TARGET_BUD, 0) > 0:
            gbc_h = to_human(int(pre_totals.get(gamba_svc.YIELD_TARGET_GBC, 0)))
            bud_h = to_human(int(pre_totals.get(gamba_svc.YIELD_TARGET_BUD, 0)))
            notes.append(
                f"Split: **{gbc_h:,.4f} GBC** + **{bud_h:,.4f} BUD** "
                f"across positions."
            )
        await ctx.reply(
            embed=claim_receipt(
                yield_symbol=res.yield_target,
                yield_emoji=_TOKEN_EMOJI.get(res.yield_target, ""),
                yield_paid_h=to_human(int(res.yield_paid_raw)),
                yield_oracle=yield_oracle,
                stake_symbol=res.symbol,
                stake_emoji=_TOKEN_EMOJI.get(res.symbol, ""),
                total_staked_h=to_human(int(res.staked_raw)) if res.symbol else 0.0,
                stake_oracle=(
                    await _oracle_price(ctx, res.symbol) if res.symbol else 0.0
                ),
                note="\n".join(notes),
            ),
            mention_author=False,
        )

    # ── yield-target toggle ──────────────────────────────────────────────

    @gamba.command(name="yield", aliases=["target", "payout"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_yield(
        self, ctx: DiscoContext,
        symbol: Optional[str] = None,
        target: Optional[str] = None,
    ) -> None:
        """Set what a game-token stake drips: GBC (gamba) or BUD (buddy).

        ``,gamba yield PIP bud``  -- flip PIP's stake to drip BUD instead
        ``,gamba yield PIP gbc``  -- back to the default GBC drip
        ``,gamba yield all bud``  -- flip every active position to BUD
        ``,gamba yield all gbc``  -- back to GBC across the board

        Crystallises the existing pending payout AT THE OLD RATE and
        pays it to the OLD target's wallet before flipping, so you
        never lose accrued yield by switching mid-cycle.
        """
        if not symbol or not target:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}gamba yield <SYM|all> <gbc|bud>`"
            )
            return
        new_target = _parse_yield_target(target)
        if new_target is None:
            await ctx.reply_error("Target must be `gbc` or `bud`.")
            return
        sym_low = symbol.lower()
        if sym_low in ("all", "everything", "max"):
            await self._set_yield_target_all(ctx, new_target)
            return
        sym = symbol.upper()
        if sym not in gamba_svc.GAME_TOKEN_SET:
            await ctx.reply_error(
                f"`{sym}` is not a Gamba Network game token. Stakeable: "
                f"{', '.join(sorted(gamba_svc.GAME_TOKEN_SET))}."
            )
            return
        try:
            final, paid_raw = await gamba_svc.set_yield_target(
                ctx.db, ctx.guild_id, ctx.author.id, sym, new_target,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        rate = gamba_svc.STAKE_RATE_BY_TARGET.get(
            final, gamba_svc.STAKE_GBC_PER_DAY,
        )
        builder = card(
            f"\U0001F501 Yield Target -- {sym} -> {final}",
            color=C_SUCCESS,
        ).description(
            f"**{sym}** stake now drips **{rate:g} {final} per {sym} per day**."
        )
        if paid_raw > 0:
            paid_h = to_human(int(paid_raw))
            old_target = (
                gamba_svc.YIELD_TARGET_BUD
                if final == gamba_svc.YIELD_TARGET_GBC
                else gamba_svc.YIELD_TARGET_GBC
            )
            builder = builder.field(
                "Pending paid out",
                f"`{paid_h:,.4f} {old_target}` "
                f"(crystallised at the old rate before flipping).",
                False,
            )
        await ctx.reply(embed=builder.build(), mention_author=False)

    # ── cashout (GBC -> USD burn) ────────────────────────────────────────

    @gamba.command(name="cashout", aliases=["burn", "sellgbc"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_cashout(
        self, ctx: DiscoContext, amount: Optional[str] = None,
    ) -> None:
        """Burn GBC for USD wallet credit at oracle minus impact slippage.

        ``,gamba cashout <amt>``  -- burn a specific amount of GBC
        ``,gamba cashout all``    -- dump every GBC you hold

        Game-token stakes (PIP / ACE / VEIN / EDGE / NOIR / CHERRY /
        GAMBIT / CROWN) are untouched -- you can keep restaking them
        and cash out new GBC as it accrues.
        """
        if not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}gamba cashout <amt|all>`."
            )
            return
        try:
            is_all, amt_h = _parse_amount_or_all(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if is_all:
            req_raw = await gamba_svc.get_gbc_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if req_raw <= 0:
                await ctx.reply_error("You have no GBC to cash out.")
                return
        else:
            req_raw = int(to_raw(amt_h))
        try:
            res = await gamba_svc.cashout_gbc(
                ctx.db, ctx.guild_id, ctx.author.id, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        # V3 Pillar 2: gambler mastery XP scales with USD cashed out.
        try:
            from services import mastery as _mastery
            _xp = _mastery.xp_for_action(to_human(int(res.usd_credited_raw)))
            await _mastery.add_mastery(
                ctx.db, ctx.author.id, ctx.guild_id, "gambler", _xp,
            )
        except Exception:
            pass
        from core.framework.staking import cashout_receipt
        await ctx.reply(
            embed=cashout_receipt(
                burned_symbol=gamba_svc.GBC_SYMBOL,
                burned_emoji=_TOKEN_EMOJI[gamba_svc.GBC_SYMBOL],
                burned_h=to_human(int(res.gbc_burned_raw)),
                usd_credited_h=to_human(int(res.usd_credited_raw)),
                oracle_before=float(res.gbc_oracle_before),
                oracle_after=float(res.gbc_oracle_after),
                impact_pct=float(res.price_impact_pct),
                revenue_usd=float(res.revenue_usd or 0.0),
                lp_reward_usd=float(res.lp_reward_usd or 0.0),
            ),
            mention_author=False,
        )

    # ── autocompound ─────────────────────────────────────────────────────

    @gamba.command(name="autocompound", aliases=["ac", "compound"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_autocompound(
        self, ctx: DiscoContext,
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> None:
        """Toggle auto-compound. ``on`` / ``off`` / ``toggle``.

        ``,gamba autocompound on``           -- flip every game token at once
        ``,gamba autocompound SYM on``       -- single position
        """
        first = (symbol or "").strip().lower()
        # First arg is on/off/toggle  ->  apply to ALL game tokens.
        if first in ("on", "off", "toggle", "flip", ""):
            target = first or "toggle"
            await self._set_autocompound_all(ctx, target)
            return
        sym = (symbol or "").upper()
        if sym not in gamba_svc.GAME_TOKEN_SET:
            await ctx.reply_error(f"`{sym}` is not a Gamba Network game token.")
            return
        m = (mode or "toggle").strip().lower()
        snap = await gamba_svc.get_stake(
            ctx.db, ctx.guild_id, ctx.author.id, sym,
        )
        cur = bool(snap.auto_compound)
        if m == "on":
            new = True
        elif m == "off":
            new = False
        elif m in ("toggle", "flip", ""):
            new = not cur
        else:
            await ctx.reply_error("Use `on`, `off`, or `toggle`.")
            return
        try:
            new = await gamba_svc.set_autocompound(
                ctx.db, ctx.guild_id, ctx.author.id, sym, new,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        state = "ON" if new else "OFF"
        emoji = "\U0001F501" if new else "\U0001F4A4"
        target = snap.yield_target
        await ctx.reply(
            embed=card(
                f"{emoji} Auto-Compound {state}", color=C_SUCCESS if new else C_NEUTRAL,
            ).description(
                f"Auto-compound on **{sym}** is **{state}**. "
                + (
                    f"Yield rolls back into the {sym} stake on every claim "
                    f"(your {target}-target rate keeps applying)."
                    if new else
                    f"{target} yield credits to your wallet on every claim."
                )
            ).build(),
            mention_author=False,
        )

    # ── stakes overview ──────────────────────────────────────────────────

    @gamba.command(name="stakes", aliases=["staked", "positions"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_stakes(self, ctx: DiscoContext) -> None:
        """Snapshot every active gamba stake with live pending yield."""
        stakes = await gamba_svc.list_stakes(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if not stakes:
            await ctx.reply(
                embed=card(
                    "\U0001F3B0 No Gamba Stakes",
                    color=C_NEUTRAL,
                ).description(
                    "You have no game tokens staked. Win a gamba to mint a "
                    "themed token (e.g. `,play mines`, `,chess play`), then "
                    "use `,gamba stake SYM all` to drip GBC, or "
                    "`,gamba stake SYM all bud` to drip BUD instead."
                ).build(),
                mention_author=False,
            )
            return

        # Cache yield-token oracles so each row's pending USD valuation
        # uses the right one without re-fetching per position.
        yield_oracle_cache: dict[str, float] = {
            gamba_svc.YIELD_TARGET_GBC: await _oracle_price(
                ctx, gamba_svc.GBC_SYMBOL,
            ),
            gamba_svc.YIELD_TARGET_BUD: await _oracle_price(ctx, "BUD"),
        }
        total_value_usd = 0.0
        pending_by_target: dict[str, int] = {
            gamba_svc.YIELD_TARGET_GBC: 0,
            gamba_svc.YIELD_TARGET_BUD: 0,
        }
        total_claimed_raw = 0
        lines: list[str] = []
        for snap in stakes:
            sym_ora = await _oracle_price(ctx, snap.symbol)
            staked_h = to_human(snap.staked_raw)
            pending_raw, target = await gamba_svc.accrued_yield(
                ctx.db, ctx.guild_id, ctx.author.id, snap.symbol,
            )
            pending_h = to_human(pending_raw)
            claimed_h = to_human(snap.total_claimed_raw)
            ac_badge = "\U0001F501 AUTO" if snap.auto_compound else ""
            tgt_emoji = _TOKEN_EMOJI.get(target, "")
            target_badge = f"{tgt_emoji} {target}".strip()
            yield_oracle = yield_oracle_cache.get(target, 0.0)
            staked_usd = staked_h * sym_ora if sym_ora > 0 else 0.0
            pending_usd = pending_h * yield_oracle if yield_oracle > 0 else 0.0
            total_value_usd += staked_usd
            pending_by_target[target] = (
                pending_by_target.get(target, 0) + int(pending_raw)
            )
            total_claimed_raw += int(snap.total_claimed_raw)
            badges = "  ".join(b for b in (target_badge, ac_badge) if b)
            line = (
                f"{_TOKEN_EMOJI.get(snap.symbol, '')} **{snap.symbol}**  {badges}\n"
                f"  staked: `{fmt_token(staked_h, snap.symbol)}`"
                f"{f'  ~ {fmt_usd(staked_usd)}' if staked_usd > 0 else ''}\n"
                f"  pending: `{fmt_token(pending_h, target)}`"
                f"{f'  ~ {fmt_usd(pending_usd)}' if pending_usd > 0 else ''}"
                f"  · claimed: `{claimed_h:,.4f}`"
            )
            lines.append(line)

        # Headline APY: if the player has any BUD-target positions, show
        # both rates side-by-side so the panel doesn't lie about a BUD
        # stake's reward direction.
        gbc_apy = gamba_svc.effective_apy_pct(gamba_svc.YIELD_TARGET_GBC)
        bud_apy = gamba_svc.effective_apy_pct(gamba_svc.YIELD_TARGET_BUD)
        has_bud = pending_by_target.get(gamba_svc.YIELD_TARGET_BUD, 0) > 0
        title_apy = (
            f"GBC {gbc_apy:,.1f}%  ·  BUD {bud_apy:,.1f}%"
            if has_bud else f"base APY {gbc_apy:,.1f}%"
        )
        pending_field = (
            f"`{fmt_token(to_human(pending_by_target[gamba_svc.YIELD_TARGET_GBC]), gamba_svc.GBC_SYMBOL)}`"
            + (
                f"\n`{fmt_token(to_human(pending_by_target[gamba_svc.YIELD_TARGET_BUD]), 'BUD')}`"
                if has_bud else ""
            )
        )
        embed = (
            card(
                f"\U0001F3B0 Your Gamba Stakes  ·  {title_apy}",
                color=C_GOLD,
            )
            .description("\n\n".join(lines))
            .field(
                "Total Staked",
                f"`{fmt_usd(total_value_usd) if total_value_usd > 0 else '$0.00'}`",
                True,
            )
            .field(
                "Pending Yield",
                pending_field,
                True,
            )
            .field(
                "Lifetime Claimed",
                f"`{to_human(total_claimed_raw):,.4f}` (target's token)",
                True,
            )
            .footer(
                f"{ctx.prefix}gamba claim [SYM]  ·  "
                f"{ctx.prefix}gamba yield SYM <gbc|bud>  ·  "
                f"{ctx.prefix}gamba unstake SYM all  ·  "
                f"{ctx.prefix}gamba autocompound SYM"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── info card ────────────────────────────────────────────────────────

    @gamba.command(name="info", aliases=["help", "tokens"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_info(self, ctx: DiscoContext) -> None:
        await self._info(ctx)

    async def _info(self, ctx: DiscoContext) -> None:
        """Network reference card -- coin + per-game token mapping."""
        gbc_ora = await _oracle_price(ctx, gamba_svc.GBC_SYMBOL)
        rows: list[str] = []
        rows.append(
            f"**{_TOKEN_EMOJI['GBC']} GBC** (Gamba Coin) -- network coin. "
            f"Live: **{fmt_usd(gbc_ora) if gbc_ora > 0 else '--'}**.\n"
            f"Earn by staking any game token. Spend at the Gamba Shop "
            f"or burn for USD via `{ctx.prefix}gamba cashout`."
        )
        rows.append("")
        rows.append("**Game tokens** (mint on win, stake to drip GBC or BUD):")
        for game, sym in gamba_svc.GAME_TOKEN.items():
            ora = await _oracle_price(ctx, sym)
            ora_str = f"{fmt_usd(ora)}" if ora > 0 else "--"
            rows.append(
                f"  {_TOKEN_EMOJI.get(sym, '')} **{sym}** "
                f"({game.title()}) · {ora_str}"
            )

        gbc_apy = gamba_svc.effective_apy_pct(gamba_svc.YIELD_TARGET_GBC)
        bud_apy = gamba_svc.effective_apy_pct(gamba_svc.YIELD_TARGET_BUD)
        embed = (
            card("\U0001F3B0 Gamba Network", color=C_PURPLE_INFO)
            .description("\n".join(rows))
            .field(
                "Stake yield",
                f"`{gamba_svc.STAKE_GBC_PER_DAY:g} GBC` per token per day "
                f"(`~{gbc_apy:.0f}%` APY at parity)\n"
                f"`{gamba_svc.STAKE_BUD_PER_DAY:g} BUD` per token per day "
                f"(`~{bud_apy:.0f}%`) -- set with "
                f"`{ctx.prefix}gamba yield SYM bud`",
                False,
            )
            .field(
                "Win mint rate",
                f"`{gamba_svc.TOKEN_MINT_PER_USD_WIN:g}` token per `$1` of profit",
                True,
            )
            .field(
                "Bet currencies",
                ", ".join(sorted(Config.GAMBA_BET_TOKENS)),
                True,
            )
            .footer(
                f"{ctx.prefix}gamba stake  ·  "
                f"{ctx.prefix}gamba yield  ·  "
                f"{ctx.prefix}gamba cashout  ·  "
                f"{ctx.prefix}chess play"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── shop ─────────────────────────────────────────────────────────────

    @gamba.command(name="shop", aliases=["store"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_shop(self, ctx: DiscoContext) -> None:
        """Browse the three GBC-priced consumables."""
        gbc_ora = await _oracle_price(ctx, gamba_svc.GBC_SYMBOL)
        gbc_h = to_human(await _holding_raw(ctx, gamba_svc.GBC_SYMBOL))
        builder = card("\U0001F3B0 Gamba Shop", color=C_GOLD).description(
            "Three single-use consumables, paid in **GBC**. Auto-applied "
            "when you trigger the matching event -- no need to activate.\n"
            f"Your balance: **{fmt_token(gbc_h, gamba_svc.GBC_SYMBOL)}**"
            f"{f'  ~ {fmt_usd(gbc_h * gbc_ora)}' if gbc_ora > 0 else ''}"
        )
        for key in gamba_svc.SHOP_ITEMS:
            item = _SHOP_ITEMS.get(key)
            if not item:
                continue
            cost_h = to_human(int(item.get("cost_stable") or 0))
            owned = await gamba_svc.get_consumable_count(
                ctx.db, ctx.guild_id, ctx.author.id, key,
            )
            line = (
                f"{item.get('description', '')}\n"
                f"Cost: **{fmt_token(cost_h, gamba_svc.GBC_SYMBOL)}**  "
                f"({fmt_usd(cost_h * gbc_ora) if gbc_ora > 0 else '--'})  "
                f"· You own: **{owned}**"
            )
            builder = builder.field(
                f"{item.get('emoji', '')} {item.get('name', key)}",
                line,
                False,
            )
        builder = builder.footer(
            f"{ctx.prefix}gamba buy <item> [qty]  ·  "
            f"{ctx.prefix}gamba inventory"
        )
        await ctx.reply(embed=builder.build(), mention_author=False)

    @gamba.command(name="buy", aliases=["purchase"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_buy(
        self, ctx: DiscoContext, item: str, qty: int = 1,
    ) -> None:
        """Buy a Gamba Shop consumable. Pays in GBC."""
        key = (item or "").strip().lower().replace("-", "_").replace(" ", "_")
        if key not in gamba_svc.SHOP_ITEM_SET:
            await ctx.reply_error(
                f"Unknown item `{item}`. Try: "
                f"{', '.join(gamba_svc.SHOP_ITEMS)}."
            )
            return
        if qty <= 0 or qty > 50:
            await ctx.reply_error("Quantity must be between 1 and 50.")
            return
        spec = _SHOP_ITEMS.get(key) or {}
        cost_per_raw = int(spec.get("cost_stable") or 0)
        total_cost_raw = cost_per_raw * int(qty)
        if total_cost_raw <= 0:
            await ctx.reply_error("Item is not currently for sale.")
            return
        bal_raw = await _holding_raw(ctx, gamba_svc.GBC_SYMBOL)
        if bal_raw < total_cost_raw:
            await ctx.reply_error(
                f"Need **{fmt_token(to_human(total_cost_raw), gamba_svc.GBC_SYMBOL)}** -- "
                f"you have **{fmt_token(to_human(bal_raw), gamba_svc.GBC_SYMBOL)}**."
            )
            return
        try:
            await ctx.db.update_wallet_holding(
                ctx.author.id, ctx.guild_id,
                gamba_svc.GAMBA_NETWORK_SHORT, gamba_svc.GBC_SYMBOL,
                -int(total_cost_raw),
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        new_total = await gamba_svc.add_consumable(
            ctx.db, ctx.guild_id, ctx.author.id, key, int(qty),
        )
        await ctx.reply(
            embed=card(
                f"{spec.get('emoji', '')} Bought {qty}x {spec.get('name', key)}",
                color=C_SUCCESS,
            ).description(
                f"Paid **{fmt_token(to_human(total_cost_raw), gamba_svc.GBC_SYMBOL)}**.\n"
                f"You now own **{new_total}** {spec.get('name', key)}."
            ).footer(
                "Auto-applied on the matching event -- no need to activate."
            ).build(),
            mention_author=False,
        )

    @gamba.command(name="inventory", aliases=["inv", "items"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gamba_inventory(self, ctx: DiscoContext) -> None:
        """Show owned Gamba Shop consumables."""
        inv = await gamba_svc.list_consumables(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if not inv:
            await ctx.reply(
                embed=card(
                    "\U0001F3B0 Gamba Inventory", color=C_NEUTRAL,
                ).description(
                    f"Empty. Buy with `{ctx.prefix}gamba buy <item>`."
                ).build(),
                mention_author=False,
            )
            return
        lines: list[str] = []
        for key in gamba_svc.SHOP_ITEMS:
            qty = inv.get(key, 0)
            spec = _SHOP_ITEMS.get(key) or {}
            lines.append(
                f"{spec.get('emoji', '')} **{spec.get('name', key)}** -- "
                f"`{qty}`"
            )
        await ctx.reply(
            embed=card(
                "\U0001F3B0 Gamba Inventory", color=C_INFO,
            ).description("\n".join(lines)).footer(
                "Auto-applied on the matching event."
            ).build(),
            mention_author=False,
        )

    # ── stake everything / unstake everything ────────────────────────────

    async def _stake_everything(
        self, ctx: DiscoContext, target: str | None = None,
    ) -> None:
        """Stake the full wallet balance of every game token in one shot.

        Iterates the eight game tokens, skips any with zero balance,
        and emits a single combined receipt. ``target`` opens / rotates
        every freshly-touched position to the chosen yield direction;
        when None the existing per-row target is preserved.
        """
        lines: list[str] = []
        total_usd = 0.0
        # Daily-drip preview is built per-target so a mixed bulk-stake
        # shows GBC + BUD totals separately rather than collapsing.
        daily_by_target: dict[str, float] = {
            gamba_svc.YIELD_TARGET_GBC: 0.0,
            gamba_svc.YIELD_TARGET_BUD: 0.0,
        }
        skipped = 0
        for sym in sorted(gamba_svc.GAME_TOKEN_SET):
            req_raw = await _holding_raw(ctx, sym)
            if req_raw <= 0:
                continue
            try:
                res = await gamba_svc.stake(
                    ctx.db, ctx.guild_id, ctx.author.id, sym, req_raw,
                    yield_target=target,
                )
            except Exception as exc:
                skipped += 1
                log.debug("gamba stake everything: %s skipped (%s)", sym, exc)
                continue
            ora = await _oracle_price(ctx, sym)
            staked_h = to_human(int(res.delta_raw))
            total_h = to_human(int(res.staked_raw))
            usd = staked_h * ora if ora > 0 else 0.0
            total_usd += usd
            usd_str = f"  ~ **{fmt_usd(usd)}**" if usd > 0 else ""
            emoji = _TOKEN_EMOJI.get(sym, "")
            tgt_emoji = _TOKEN_EMOJI.get(res.yield_target, "")
            rate = gamba_svc.STAKE_RATE_BY_TARGET.get(res.yield_target, 0.0)
            daily_by_target[res.yield_target] = (
                daily_by_target.get(res.yield_target, 0.0)
                + total_h * rate
            )
            lines.append(
                f"{emoji} **{sym}** -- staked `{fmt_token(staked_h, sym)}`"
                f"{usd_str}  (position: `{total_h:,.4f}`)  · "
                f"{tgt_emoji} {res.yield_target}"
            )
        if not lines:
            await ctx.reply_error(
                "You don't hold any Gamba game tokens to stake. "
                f"Win some by playing on the casino surface "
                f"(`{ctx.prefix}play mines`, `{ctx.prefix}chess play`, ...)."
            )
            return
        gbc_oracle = await _oracle_price(ctx, gamba_svc.GBC_SYMBOL)
        bud_oracle = await _oracle_price(ctx, "BUD")
        oracle_for: dict[str, float] = {
            gamba_svc.YIELD_TARGET_GBC: gbc_oracle,
            gamba_svc.YIELD_TARGET_BUD: bud_oracle,
        }
        drip_lines: list[str] = []
        daily_usd_total = 0.0
        for tgt, daily_h in daily_by_target.items():
            if daily_h <= 0:
                continue
            tgt_usd = daily_h * oracle_for.get(tgt, 0.0)
            daily_usd_total += tgt_usd if tgt_usd > 0 else 0.0
            drip_lines.append(
                f"`{fmt_token(daily_h, tgt)}`"
                + (f"  ~ {fmt_usd(tgt_usd)}" if tgt_usd > 0 else "")
            )
        embed = (
            card("\U0001F512 Staked Everything", color=C_SUCCESS)
            .description("\n".join(lines))
            .field(
                "Total Position Value",
                f"`{fmt_usd(total_usd)}`",
                True,
            )
            .field(
                "Daily Drip",
                "\n".join(drip_lines) or "`-`",
                True,
            )
            .field(
                "APY (base)",
                f"`{gamba_svc.effective_apy_pct():,.0f}%`  at parity",
                True,
            )
            .footer(
                (f"{skipped} skipped · " if skipped else "")
                + f"{ctx.prefix}gamba stakes -- live overview  ·  "
                + f"{ctx.prefix}gamba claim -- sweep yield"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _unstake_everything(self, ctx: DiscoContext) -> None:
        """Unstake every game-token position and auto-claim accrued yield."""
        snaps = await gamba_svc.list_stakes(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if not snaps:
            await ctx.reply_error("You have no Gamba stakes to unwind.")
            return
        lines: list[str] = []
        total_usd = 0.0
        paid_by_target: dict[str, int] = {
            gamba_svc.YIELD_TARGET_GBC: 0,
            gamba_svc.YIELD_TARGET_BUD: 0,
        }
        skipped = 0
        for snap in snaps:
            sym = snap.symbol
            req_raw = int(snap.staked_raw)
            if req_raw <= 0:
                continue
            try:
                res = await gamba_svc.unstake(
                    ctx.db, ctx.guild_id, ctx.author.id, sym, req_raw,
                )
            except Exception as exc:
                skipped += 1
                log.debug("gamba unstake everything: %s skipped (%s)", sym, exc)
                continue
            ora = await _oracle_price(ctx, sym)
            unlocked_h = to_human(abs(int(res.delta_raw)))
            usd = unlocked_h * ora if ora > 0 else 0.0
            total_usd += usd
            paid_by_target[res.yield_target] = (
                paid_by_target.get(res.yield_target, 0) + int(res.yield_paid_raw)
            )
            usd_str = f"  ~ **{fmt_usd(usd)}**" if usd > 0 else ""
            paid_h = to_human(int(res.yield_paid_raw))
            paid_str = (
                f"  · yield `{fmt_token(paid_h, res.yield_target)}`"
                if paid_h > 0 else ""
            )
            emoji = _TOKEN_EMOJI.get(sym, "")
            lines.append(
                f"{emoji} **{sym}** -- unlocked `{fmt_token(unlocked_h, sym)}`"
                f"{usd_str}{paid_str}"
            )
        if not lines:
            await ctx.reply_error("Nothing to unwind.")
            return
        gbc_oracle = await _oracle_price(ctx, gamba_svc.GBC_SYMBOL)
        bud_oracle = await _oracle_price(ctx, "BUD")
        oracle_for = {
            gamba_svc.YIELD_TARGET_GBC: gbc_oracle,
            gamba_svc.YIELD_TARGET_BUD: bud_oracle,
        }
        yield_lines: list[str] = []
        for tgt, raw in paid_by_target.items():
            if raw <= 0:
                continue
            h = to_human(raw)
            usd = h * oracle_for.get(tgt, 0.0)
            yield_lines.append(
                f"`{fmt_token(h, tgt)}`"
                + (f"  ~ {fmt_usd(usd)}" if usd > 0 else "")
            )
        embed = (
            card("\U0001F513 Unstaked Everything", color=C_AMBER)
            .description("\n".join(lines))
            .field(
                "Total Unlocked Value",
                f"`{fmt_usd(total_usd)}`",
                True,
            )
            .field(
                "Yield Claimed",
                "\n".join(yield_lines) or "`-`",
                True,
            )
            .footer(
                (f"{skipped} skipped · " if skipped else "")
                + f"{ctx.prefix}gamba stake all  -- relock  ·  "
                + f"{ctx.prefix}gamba shop  -- spend GBC"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _set_yield_target_all(
        self, ctx: DiscoContext, new_target: str,
    ) -> None:
        """Flip the yield target on every active game-token stake.

        Each position's existing pending is crystallised at the OLD
        target's rate and paid out before the flip -- skipping zero-
        stake rows the same way the autocompound bulk flip does.
        """
        snaps = await gamba_svc.list_stakes(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if not snaps:
            await ctx.reply_error(
                "You have no active Gamba stakes to flip. "
                f"Open one with `{ctx.prefix}gamba stake SYM all`."
            )
            return
        lines: list[str] = []
        paid_by_old_target: dict[str, int] = {
            gamba_svc.YIELD_TARGET_GBC: 0,
            gamba_svc.YIELD_TARGET_BUD: 0,
        }
        flipped = 0
        skipped = 0
        for snap in snaps:
            old_target = snap.yield_target
            try:
                final, paid_raw = await gamba_svc.set_yield_target(
                    ctx.db, ctx.guild_id, ctx.author.id, snap.symbol, new_target,
                )
            except Exception as exc:
                skipped += 1
                log.debug("gamba yield all: %s skipped (%s)", snap.symbol, exc)
                continue
            if old_target != final:
                flipped += 1
                if paid_raw > 0:
                    paid_by_old_target[old_target] = (
                        paid_by_old_target.get(old_target, 0) + int(paid_raw)
                    )
                emoji = _TOKEN_EMOJI.get(snap.symbol, "")
                lines.append(
                    f"{emoji} **{snap.symbol}** -- {old_target} -> {final}"
                    + (
                        f"  · paid `{to_human(paid_raw):,.4f} {old_target}`"
                        if paid_raw > 0 else ""
                    )
                )
            else:
                emoji = _TOKEN_EMOJI.get(snap.symbol, "")
                lines.append(
                    f"{emoji} **{snap.symbol}** -- already on {final}"
                )
        rate = gamba_svc.STAKE_RATE_BY_TARGET.get(new_target, 0.0)
        embed = (
            card(
                f"\U0001F501 Yield Target -- ALL -> {new_target}",
                color=C_SUCCESS if flipped else C_NEUTRAL,
            )
            .description("\n".join(lines))
            .field(
                "Flipped",
                f"`{flipped}` position(s)",
                True,
            )
            .field(
                "New rate",
                f"`{rate:g} {new_target}` per token per day",
                True,
            )
            .footer(
                (f"{skipped} skipped · " if skipped else "")
                + f"{ctx.prefix}gamba stakes -- live overview"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _set_autocompound_all(
        self, ctx: DiscoContext, mode: str,
    ) -> None:
        """Flip auto-compound on every game-token stake at once.

        ``mode`` is ``on`` / ``off`` / ``toggle`` (toggle is the
        default; flips each position individually so a half-on /
        half-off setup ends up uniform). Renders one combined receipt
        listing every position's new state alongside its emoji so the
        player sees the global state at a glance.
        """
        m = (mode or "toggle").strip().lower()
        if m in ("flip", ""):
            m = "toggle"
        if m not in ("on", "off", "toggle"):
            await ctx.reply_error("Use `on`, `off`, or `toggle`.")
            return
        lines: list[str] = []
        on_count = 0
        off_count = 0
        for sym in sorted(gamba_svc.GAME_TOKEN_SET):
            snap = await gamba_svc.get_stake(
                ctx.db, ctx.guild_id, ctx.author.id, sym,
            )
            if m == "on":
                target = True
            elif m == "off":
                target = False
            else:
                target = not bool(snap.auto_compound)
            try:
                new_state = await gamba_svc.set_autocompound(
                    ctx.db, ctx.guild_id, ctx.author.id, sym, target,
                )
            except Exception:
                continue
            tag = "\U0001F501 ON" if new_state else "\U0001F4A4 OFF"
            on_count += 1 if new_state else 0
            off_count += 0 if new_state else 1
            emoji = _TOKEN_EMOJI.get(sym, "")
            staked_h = to_human(int(snap.staked_raw))
            stake_str = (
                f"`{staked_h:,.4f}` staked" if staked_h > 0
                else "`0` staked"
            )
            lines.append(f"{emoji} **{sym}**  ·  {stake_str}  ·  {tag}")
        title_state = "ON" if m == "on" else ("OFF" if m == "off" else "Toggled")
        embed = (
            card(
                f"\U0001F501 Auto-Compound  ·  {title_state}",
                color=C_SUCCESS if on_count else C_NEUTRAL,
            )
            .description("\n".join(lines) or "_No game-token stakes yet._")
            .field("Now ON", f"`{on_count}` token(s)", True)
            .field("Now OFF", f"`{off_count}` token(s)", True)
            .footer(
                "ON = yield (GBC or BUD) rolls back into the same stake "
                "as more game token. OFF = the target token credits to "
                "your wallet on every claim."
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── stake panel (button-driven) ──────────────────────────────────────

    async def _open_stake_panel(
        self, ctx: DiscoContext, focus_symbol: Optional[str] = None,
    ) -> None:
        """Open the unified stake panel.

        ``focus_symbol`` is the single game token the buttons act on. When
        None, the multi-token panel is used. The panel headline shows the
        position's yield target (GBC or BUD) -- in the multi-token case
        the headline defaults to GBC with a note pointing players at
        ``,gamba stakes`` for the per-row target breakdown.
        """
        if focus_symbol:
            tokens = [
                StakeToken(focus_symbol, _TOKEN_EMOJI.get(focus_symbol, ""))
            ]
            # Read the focused row's yield target so the panel headline
            # tells the truth for that position.
            focus_snap = await gamba_svc.get_stake(
                ctx.db, ctx.guild_id, ctx.author.id, focus_symbol,
            )
            panel_target = focus_snap.yield_target
        else:
            tokens = [
                StakeToken(s, _TOKEN_EMOJI.get(s, ""))
                for s in sorted(gamba_svc.GAME_TOKEN_SET)
            ]
            panel_target = gamba_svc.YIELD_TARGET_GBC

        rate = gamba_svc.STAKE_RATE_BY_TARGET.get(
            panel_target, gamba_svc.STAKE_GBC_PER_DAY,
        )

        async def _state(c: DiscoContext) -> dict:
            staked_by: dict[str, int] = {}
            wallet_by: dict[str, int] = {}
            oracle_by: dict[str, float] = {}
            total_pending = 0
            total_daily = 0
            for tok in tokens:
                snap = await gamba_svc.get_stake(
                    c.db, c.guild_id, c.author.id, tok.symbol,
                )
                staked_by[tok.symbol] = int(snap.staked_raw)
                wallet_by[tok.symbol] = await _holding_raw(c, tok.symbol)
                oracle_by[tok.symbol] = await _oracle_price(c, tok.symbol)
                # Only roll positions whose target matches the panel's
                # display target into the headline pending / daily totals
                # -- mixing GBC and BUD in one number would lie about the
                # yield denomination.
                if snap.yield_target != panel_target:
                    continue
                pending_raw, _ = await gamba_svc.accrued_yield(
                    c.db, c.guild_id, c.author.id, tok.symbol,
                )
                total_pending += int(pending_raw)
                total_daily += int(
                    (snap.staked_raw * to_raw(rate)) // to_raw(1.0)
                )
            yield_ora = await _oracle_price(c, panel_target)
            return {
                "staked_by_sym": staked_by,
                "wallet_by_sym": wallet_by,
                "stake_oracle_by_sym": oracle_by,
                "yield_oracle": yield_ora,
                "pending_raw": int(total_pending),
                "daily_rate_raw": int(total_daily),
                "apy_pct": gamba_svc.effective_apy_pct(panel_target),
            }

        async def _do_stake(c: DiscoContext, raw: int, sym: str) -> int:
            res = await gamba_svc.stake(
                c.db, c.guild_id, c.author.id, sym, int(raw),
            )
            return int(res.staked_raw)

        async def _do_unstake(c: DiscoContext, raw: int, sym: str) -> int:
            res = await gamba_svc.unstake(
                c.db, c.guild_id, c.author.id, sym, int(raw),
            )
            return int(res.staked_raw)

        async def _do_claim(c: DiscoContext) -> int:
            try:
                res = await gamba_svc.claim(
                    c.db, c.guild_id, c.author.id, focus_symbol,
                )
            except ValueError:
                return 0
            return int(res.yield_paid_raw)

        title = (
            f"\U0001F3B0 Gamba Stake ({focus_symbol} -> {panel_target})"
            if focus_symbol
            else f"\U0001F3B0 Gamba Stake (any game token -> {panel_target})"
        )
        note = (
            f"Stake any game token to drip {panel_target}. Yield: "
            f"{rate:g} {panel_target} per token per day."
        )
        if not focus_symbol:
            note += (
                f" Use `{ctx.prefix}gamba yield SYM bud` to flip individual "
                f"positions; `{ctx.prefix}gamba stakes` shows the per-row "
                f"target breakdown."
            )
        adapter = StakeAdapter(
            title=title, color=C_GOLD,
            stake_tokens=tokens,
            yield_symbol=panel_target,
            yield_emoji=_TOKEN_EMOJI.get(panel_target, ""),
            get_state=_state, do_stake=_do_stake,
            do_unstake=_do_unstake, do_claim=_do_claim,
            note=note,
        )
        await StakePanelView.send(ctx, adapter)


# C_PURPLE_INFO is just an alias for the network color so the info card
# stays consistent if we re-skin it later. Pinned here to avoid touching
# core/framework/ui.py for a one-line constant.
C_PURPLE_INFO = C_GOLD


async def setup(bot: Discoin) -> None:
    await bot.add_cog(GambaCog(bot))
