"""
core/framework/staking.py  -  Shared staking panel + receipts for every minigame.

Goal: identical look-and-feel for ``,buddy stake``, ``,delve stake``,
``,craft stake``, ``,fish stake``, ``,farm stake`` so a player learns
the panel once and uses it everywhere. Each game wires its own service
callbacks via :class:`StakeAdapter` and the framework owns rendering
and button-driven interactions (Stake / Unstake / Claim / Refresh).

Single-token (FARM, CRAFT, FISH) and multi-token (BUDDY: FREN+BBT,
DELVE: COPPER+SILVER+GOLD) panels share the same surface. Multi-token
modals add a ``token`` text input ahead of the amount so the buttons
still cover both panels with one layout.

Reply embeds for ``,x stake <amt>`` / ``,x unstake <amt>`` / ``,x claim``
/ ``,x cashout <amt>`` are standardised via the helper functions at the
bottom of this module so every game shows crypto + USD on every line and
every cashout shows the same oracle / slippage / LP-fee breakdown.

Usage from a cog (single-token):

    from core.framework.staking import StakeAdapter, StakePanelView, StakeToken

    adapter = StakeAdapter(
        title="\U0001F528 Crafting Stake (INGOT -> FORGE)",
        color=C_AMBER,
        stake_tokens=[StakeToken("INGOT", emoji="\U0001F9F1")],
        yield_symbol="FORGE", yield_emoji="\U0001F528",
        get_state=_state, do_stake=_stake,
        do_unstake=_unstake, do_claim=_claim,
    )
    await StakePanelView.send(ctx, adapter)

The state dict from ``get_state`` is expected to contain (raw ints):

    staked_by_sym       {SYMBOL: int}   per-token staked balance
    wallet_by_sym       {SYMBOL: int}   per-token wallet balance
    pending_raw         int             yield owed but not yet paid (claimable)
    daily_rate_raw      int             projected daily yield at current stake

Optional fields on state are forwarded into the embed:

    stake_oracle_by_sym {SYMBOL: float} live USD price of each stake token
    yield_oracle        float           live USD price of the yield token
    apy_pct             float           effective APY for the wallet line
    note                str             plain text shown below the title

For backward compatibility the panel also accepts the legacy single-token
shape (``staked_raw`` / ``wallet_raw``) and normalises it internally.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_ERROR, C_GOLD, C_INFO, C_SUCCESS, fmt_token, fmt_usd,
)

log = logging.getLogger(__name__)

# Unified panel branding so every game shows the same "header" line. Each
# game still gets its own title (e.g. "INGOT Stake (Forge)") but the panel
# footer + the Stake/Unstake/Claim button labels are constant across games.
_PANEL_FOOTER = (
    "Stake panel  -  buttons trigger amount prompts. "
    "Or use the text command directly: stake / unstake / claim."
)
_PANEL_TIMEOUT_S = 180


@dataclass
class StakeToken:
    """One stake-side token in a (possibly multi-token) staking adapter."""
    symbol: str
    emoji: str = ""


@dataclass
class StakeAdapter:
    """Glue between the framework panel and a game's service layer."""

    title: str
    color: int
    stake_tokens: list[StakeToken]      # 1 or more (multi-token panels)
    yield_symbol: str
    yield_emoji: str = ""

    get_state: Callable[[DiscoContext], Awaitable[dict]] = None  # type: ignore
    # do_stake / do_unstake receive the picked stake-side symbol so the
    # same callbacks work for single- and multi-token panels.
    do_stake: Callable[[DiscoContext, int, str], Awaitable[int]] = None  # type: ignore
    do_unstake: Callable[[DiscoContext, int, str], Awaitable[int]] = None  # type: ignore
    do_claim: Callable[[DiscoContext], Awaitable[int]] = None  # type: ignore

    note: str = ""

    @property
    def is_multi_token(self) -> bool:
        return len(self.stake_tokens) > 1

    @property
    def primary_stake(self) -> StakeToken:
        return self.stake_tokens[0]


# ============================================================================
# Embed helpers
# ============================================================================

def _usd_tag(amount: float, oracle: float) -> str:
    """Trailing ``  ~ **$X.XX**`` when both inputs are positive, else ``""``."""
    if amount <= 0 or oracle <= 0:
        return ""
    return f"  ~ **{fmt_usd(amount * oracle)}**"


def _normalise_state(adapter: StakeAdapter, state: dict) -> dict:
    """Translate legacy single-token state shape into the dict-of-dicts shape."""
    if "staked_by_sym" in state or "wallet_by_sym" in state:
        return state
    sym = adapter.primary_stake.symbol
    out = dict(state)
    out["staked_by_sym"] = {sym: int(state.get("staked_raw") or 0)}
    out["wallet_by_sym"] = {sym: int(state.get("wallet_raw") or 0)}
    if "stake_oracle" in state:
        out["stake_oracle_by_sym"] = {sym: float(state.get("stake_oracle") or 0.0)}
    return out


def build_stake_embed(adapter: StakeAdapter, state: dict) -> discord.Embed:
    """Render a panel embed for ``adapter`` with ``state`` from get_state."""
    state = _normalise_state(adapter, state)
    staked_by_sym: dict[str, int] = state.get("staked_by_sym") or {}
    wallet_by_sym: dict[str, int] = state.get("wallet_by_sym") or {}
    oracle_by_sym: dict[str, float] = state.get("stake_oracle_by_sym") or {}
    pending = int(state.get("pending_raw") or 0)
    daily = int(state.get("daily_rate_raw") or 0)
    yield_oracle = float(state.get("yield_oracle") or 0.0)
    note = str(adapter.note or state.get("note") or "")

    desc_default = (
        f"Stake **{' / '.join(t.symbol for t in adapter.stake_tokens)}** "
        f"to drip **{adapter.yield_symbol}**."
    )
    builder = (
        card(adapter.title, color=adapter.color)
        .description(note or desc_default)
    )

    # Per-token Staked + Wallet rows so multi-token panels show the
    # same crypto + USD breakdown for every stake-side token.
    for tok in adapter.stake_tokens:
        sym = tok.symbol
        staked_h = to_human(int(staked_by_sym.get(sym) or 0))
        wallet_h = to_human(int(wallet_by_sym.get(sym) or 0))
        ora = float(oracle_by_sym.get(sym) or 0.0)
        builder = builder.field(
            f"{sym} Staked",
            f"**{fmt_token(staked_h, sym, tok.emoji)}**{_usd_tag(staked_h, ora)}",
            True,
        ).field(
            f"{sym} Wallet",
            f"**{fmt_token(wallet_h, sym, tok.emoji)}**{_usd_tag(wallet_h, ora)}",
            True,
        )

    pending_h = to_human(pending)
    daily_h = to_human(daily)
    builder = builder.field(
        "Pending yield",
        (
            f"**{fmt_token(pending_h, adapter.yield_symbol, adapter.yield_emoji)}**"
            f"{_usd_tag(pending_h, yield_oracle)}"
        ),
        True,
    ).field(
        "Daily rate",
        (
            f"**{fmt_token(daily_h, adapter.yield_symbol, adapter.yield_emoji)}** / day"
            f"{_usd_tag(daily_h, yield_oracle)}"
        ),
        True,
    )

    apy = state.get("apy_pct")
    if apy is not None:
        builder = builder.field("Effective APY", f"`{float(apy):.2f}%`", True)

    return builder.footer(_PANEL_FOOTER).build()


# ============================================================================
# Reply receipts -- shared across every cog so stake / unstake / claim /
# cashout messages look identical regardless of which game emitted them.
# ============================================================================

def stake_receipt(
    *,
    action: str,                # "Staked" or "Unstaked"
    stake_symbol: str,
    stake_emoji: str = "",
    delta_h: float,
    total_h: float,
    stake_oracle: float = 0.0,
    yield_symbol: str = "",
    yield_emoji: str = "",
    yield_paid_h: float = 0.0,
    yield_oracle: float = 0.0,
    note: str = "",
) -> discord.Embed:
    """Standard stake / unstake receipt -- crypto + USD on every line.

    ``action`` switches the title (lock vs unlock emoji) and the labels
    ("Staked"/"Total staked" vs "Unstaked"/"Remaining staked"). When
    ``yield_paid_h > 0`` (auto-claim on unstake, or stake that crystallised
    pending yield) the receipt also shows the yield paid + USD.
    """
    title_emoji = "\U0001F512" if action == "Staked" else "\U0001F513"
    title = f"{title_emoji} {stake_symbol} {action}"

    delta_label = action  # "Staked" or "Unstaked"
    total_label = "Total staked" if action == "Staked" else "Remaining staked"
    desc = (
        f"{delta_label}: **{fmt_token(delta_h, stake_symbol, stake_emoji)}**"
        f"{_usd_tag(delta_h, stake_oracle)}\n"
        f"{total_label}: **{fmt_token(total_h, stake_symbol, stake_emoji)}**"
        f"{_usd_tag(total_h, stake_oracle)}"
    )
    if yield_paid_h > 0 and yield_symbol:
        desc += (
            f"\n✨ Yield paid: **"
            f"{fmt_token(yield_paid_h, yield_symbol, yield_emoji)}**"
            f"{_usd_tag(yield_paid_h, yield_oracle)}"
        )
    if note:
        desc += f"\n-# {note}"
    return card(title, color=C_SUCCESS).description(desc).build()


def claim_receipt(
    *,
    yield_symbol: str,
    yield_emoji: str = "",
    yield_paid_h: float,
    yield_oracle: float = 0.0,
    total_staked_h: float = 0.0,
    stake_symbol: str = "",
    stake_emoji: str = "",
    stake_oracle: float = 0.0,
    note: str = "",
) -> discord.Embed:
    """Standard ``,x claim`` receipt -- yield paid + remaining stake."""
    title = f"\U0001F4B0 {yield_symbol} Claimed"
    desc = (
        f"Claimed: **{fmt_token(yield_paid_h, yield_symbol, yield_emoji)}**"
        f"{_usd_tag(yield_paid_h, yield_oracle)}"
    )
    if total_staked_h > 0 and stake_symbol:
        desc += (
            f"\nStake: **{fmt_token(total_staked_h, stake_symbol, stake_emoji)}**"
            f"{_usd_tag(total_staked_h, stake_oracle)}  (still earning)"
        )
    if note:
        desc += f"\n-# {note}"
    return card(title, color=C_SUCCESS).description(desc).build()


def cashout_receipt(
    *,
    burned_symbol: str,
    burned_emoji: str = "",
    burned_h: float,
    usd_credited_h: float,
    oracle_before: float = 0.0,
    oracle_after: float = 0.0,
    impact_pct: float = 0.0,
    revenue_usd: float = 0.0,
    lp_reward_usd: float = 0.0,
) -> discord.Embed:
    """Standard ``,x cashout`` receipt -- token burned + USD credit + slippage.

    ``impact_pct`` is the decimal impact (e.g. ``0.025`` for 2.5%);
    rendered as ``2.50%``. ``revenue_usd`` is the gross pre-impact USD
    value -- shown alongside the net credited USD so users can see the
    slippage take. ``lp_reward_usd`` surfaces any LP-holder kickback.
    """
    title = f"\U0001F4B5 {burned_symbol} Cashed Out"
    gross = revenue_usd if revenue_usd > 0 else (
        burned_h * oracle_before if oracle_before > 0 else 0.0
    )
    burn_line = f"Burned: **{fmt_token(burned_h, burned_symbol, burned_emoji)}**"
    if gross > 0 and fmt_usd(gross) != fmt_usd(usd_credited_h):
        burn_line += f"  (gross **{fmt_usd(gross)}**)"
    desc = (
        f"{burn_line}\n"
        f"Credited: **{fmt_usd(usd_credited_h)}** to your wallet."
    )
    if oracle_before > 0 and oracle_after > 0:
        desc += (
            f"\n-# {burned_symbol} oracle: ${oracle_before:,.6f} -> "
            f"${oracle_after:,.6f}  "
            f"(slippage **{impact_pct * 100:.2f}%**)"
        )
    else:
        desc += f"\n-# Slippage: **{impact_pct * 100:.2f}%**"
    if lp_reward_usd > 0:
        desc += f"\n-# Paid **{fmt_usd(lp_reward_usd)}** to LP holders."
    return card(title, color=C_GOLD).description(desc).build()


# ============================================================================
# Interactive panel
# ============================================================================

class _AmountModal(discord.ui.Modal):
    """Amount-entry modal triggered by Stake / Unstake buttons.

    Multi-token panels add a ``token`` field at the top so the same
    button layout drives both single- and multi-token games.
    """

    def __init__(
        self, *, view: "StakePanelView", action: str, max_label: str,
    ) -> None:
        super().__init__(title=f"{action.title()} -- {max_label}")
        self._view = view
        self._action = action
        self._multi = view.adapter.is_multi_token
        if self._multi:
            options = "/".join(t.symbol for t in view.adapter.stake_tokens)
            self.token: discord.ui.TextInput = discord.ui.TextInput(
                label=f"Token ({options})",
                placeholder=options,
                required=True, max_length=12,
            )
            self.add_item(self.token)
        self.amount: discord.ui.TextInput = discord.ui.TextInput(
            label="Amount (number or 'all')",
            placeholder="e.g. 100, 0.5, all",
            required=True, max_length=24,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        amt_text = (self.amount.value or "").strip().lower()
        if not amt_text:
            await interaction.response.send_message(
                embed=card(
                    "Empty amount", color=C_ERROR,
                ).description(
                    "Pass a number or `all`."
                ).build(),
                ephemeral=True,
            )
            return
        sym_text = ""
        if self._multi:
            sym_text = (self.token.value or "").strip().upper()
        await self._view._on_amount_submit(
            interaction, self._action, amt_text, sym_text,
        )


class StakePanelView(discord.ui.View):
    """Interactive stake panel. Stake / Unstake / Claim / Refresh buttons."""

    def __init__(
        self, ctx: DiscoContext, adapter: StakeAdapter,
    ) -> None:
        super().__init__(timeout=_PANEL_TIMEOUT_S)
        self.ctx = ctx
        self.adapter = adapter
        self.owner_id = int(ctx.author.id)
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()

    @classmethod
    async def send(
        cls, ctx: DiscoContext, adapter: StakeAdapter,
    ) -> "StakePanelView":
        """Render the panel and attach the view in one call."""
        state = await adapter.get_state(ctx)
        view = cls(ctx, adapter)
        embed = build_stake_embed(adapter, state)
        msg = await ctx.reply(
            embed=embed, view=view, mention_author=False,
        )
        view.message = msg
        return view

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Not your panel.", ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _redraw(self) -> None:
        """Re-render the panel embed with the latest state.

        Named ``_redraw`` rather than ``_refresh`` so we don't shadow
        ``discord.ui.View._refresh(components)`` -- that's a sync method
        the gateway calls on every message-update event for a tracked
        view, and overriding it with an async no-arg method crashes the
        whole client loop the next time Discord edits the panel message.
        """
        if self.message is None:
            return
        state = await self.adapter.get_state(self.ctx)
        try:
            await self.message.edit(
                embed=build_stake_embed(self.adapter, state),
                view=self,
            )
        except discord.HTTPException:
            log.debug("stake panel refresh failed", exc_info=True)

    def _resolve_token(self, sym_text: str) -> StakeToken | None:
        if not sym_text:
            return self.adapter.primary_stake if not self.adapter.is_multi_token else None
        sym_up = sym_text.upper().strip()
        for t in self.adapter.stake_tokens:
            if t.symbol == sym_up:
                return t
        return None

    @discord.ui.button(
        label="Stake", emoji="\U0001F4E5",
        style=discord.ButtonStyle.success, row=0,
    )
    async def btn_stake(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        state = _normalise_state(
            self.adapter, await self.adapter.get_state(self.ctx),
        )
        wallet_by = state.get("wallet_by_sym") or {}
        if self.adapter.is_multi_token:
            max_label = " / ".join(
                f"{to_human(int(wallet_by.get(t.symbol) or 0)):,.4f} {t.symbol}"
                for t in self.adapter.stake_tokens
            )
        else:
            sym = self.adapter.primary_stake.symbol
            wallet_h = to_human(int(wallet_by.get(sym) or 0))
            max_label = f"max {wallet_h:,.4f} {sym}"
        modal = _AmountModal(view=self, action="stake", max_label=max_label)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Unstake", emoji="\U0001F4E4",
        style=discord.ButtonStyle.danger, row=0,
    )
    async def btn_unstake(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        state = _normalise_state(
            self.adapter, await self.adapter.get_state(self.ctx),
        )
        staked_by = state.get("staked_by_sym") or {}
        if self.adapter.is_multi_token:
            max_label = " / ".join(
                f"{to_human(int(staked_by.get(t.symbol) or 0)):,.4f} {t.symbol}"
                for t in self.adapter.stake_tokens
            )
        else:
            sym = self.adapter.primary_stake.symbol
            staked_h = to_human(int(staked_by.get(sym) or 0))
            max_label = f"max {staked_h:,.4f} {sym}"
        modal = _AmountModal(view=self, action="unstake", max_label=max_label)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Claim", emoji="\U0001F4B0",
        style=discord.ButtonStyle.primary, row=0,
    )
    async def btn_claim(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        if self._lock.locked():
            await interaction.response.defer()
            return
        async with self._lock:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            try:
                paid_raw = await self.adapter.do_claim(self.ctx)
            except ValueError as e:
                await interaction.followup.send(
                    embed=card(description=str(e), color=C_ERROR).build(),
                    ephemeral=True,
                )
                return
            except Exception:
                log.exception("stake panel: claim failed")
                await interaction.followup.send(
                    embed=card(
                        description="Claim failed. Try again.",
                        color=C_ERROR,
                    ).build(),
                    ephemeral=True,
                )
                return
            paid_h = to_human(int(paid_raw or 0))
            await interaction.followup.send(
                embed=claim_receipt(
                    yield_symbol=self.adapter.yield_symbol,
                    yield_emoji=self.adapter.yield_emoji,
                    yield_paid_h=paid_h,
                ),
                ephemeral=True,
            )
            await self._redraw()

    @discord.ui.button(
        label="Refresh", emoji="\U0001F501",
        style=discord.ButtonStyle.secondary, row=0,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        await self._redraw()

    async def _on_amount_submit(
        self,
        interaction: discord.Interaction,
        action: str,
        amt_text: str,
        sym_text: str,
    ) -> None:
        if self._lock.locked():
            await interaction.response.send_message(
                "Another action is in progress -- try again in a moment.",
                ephemeral=True,
            )
            return
        async with self._lock:
            tok = self._resolve_token(sym_text)
            if tok is None:
                opts = " / ".join(t.symbol for t in self.adapter.stake_tokens)
                await interaction.response.send_message(
                    embed=card(
                        "Pick a token", color=C_ERROR,
                    ).description(
                        f"Token must be one of: {opts}."
                    ).build(),
                    ephemeral=True,
                )
                return
            state = _normalise_state(
                self.adapter, await self.adapter.get_state(self.ctx),
            )
            staked_by = state.get("staked_by_sym") or {}
            wallet_by = state.get("wallet_by_sym") or {}
            cap_raw = (
                int(wallet_by.get(tok.symbol) or 0)
                if action == "stake"
                else int(staked_by.get(tok.symbol) or 0)
            )
            if amt_text in ("all", "max", "everything"):
                amt_raw = int(cap_raw)
            else:
                try:
                    amt_human = float(amt_text.replace(",", ""))
                except ValueError:
                    await interaction.response.send_message(
                        embed=card(
                            "Invalid amount", color=C_ERROR,
                        ).description(
                            "Pass a number like `100`, `0.5`, or `all`."
                        ).build(),
                        ephemeral=True,
                    )
                    return
                amt_raw = int(to_raw(amt_human))
            if amt_raw <= 0:
                await interaction.response.send_message(
                    embed=card(
                        "Empty action", color=C_INFO,
                    ).description(
                        f"Nothing to {action}."
                    ).build(),
                    ephemeral=True,
                )
                return
            if amt_raw > cap_raw:
                await interaction.response.send_message(
                    embed=card(
                        "Over cap", color=C_ERROR,
                    ).description(
                        f"You can {action} at most "
                        f"{to_human(cap_raw):,.4f} {tok.symbol}."
                    ).build(),
                    ephemeral=True,
                )
                return
            stake_oracle = float(
                (state.get("stake_oracle_by_sym") or {}).get(tok.symbol) or 0.0
            )
            yield_oracle = float(state.get("yield_oracle") or 0.0)
            try:
                if action == "stake":
                    new_total_raw = await self.adapter.do_stake(
                        self.ctx, int(amt_raw), tok.symbol,
                    )
                    embed = stake_receipt(
                        action="Staked",
                        stake_symbol=tok.symbol, stake_emoji=tok.emoji,
                        delta_h=to_human(int(amt_raw)),
                        total_h=to_human(int(new_total_raw or 0)),
                        stake_oracle=stake_oracle,
                    )
                else:
                    remaining_raw = await self.adapter.do_unstake(
                        self.ctx, int(amt_raw), tok.symbol,
                    )
                    embed = stake_receipt(
                        action="Unstaked",
                        stake_symbol=tok.symbol, stake_emoji=tok.emoji,
                        delta_h=to_human(int(amt_raw)),
                        total_h=to_human(int(remaining_raw or 0)),
                        stake_oracle=stake_oracle,
                        yield_symbol=self.adapter.yield_symbol,
                        yield_emoji=self.adapter.yield_emoji,
                        yield_oracle=yield_oracle,
                    )
            except ValueError as e:
                await interaction.response.send_message(
                    embed=card(description=str(e), color=C_ERROR).build(),
                    ephemeral=True,
                )
                return
            except Exception:
                log.exception("stake panel: %s failed", action)
                await interaction.response.send_message(
                    embed=card(
                        description=f"{action.title()} failed. Try again.",
                        color=C_ERROR,
                    ).build(),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                embed=embed, ephemeral=True,
            )
            await self._redraw()


__all__ = [
    "StakeToken",
    "StakeAdapter",
    "StakePanelView",
    "build_stake_embed",
    "stake_receipt",
    "claim_receipt",
    "cashout_receipt",
]
