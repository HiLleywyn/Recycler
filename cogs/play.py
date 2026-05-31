from __future__ import annotations

import asyncio
import io
import math
import secrets
import time

# Cryptographically secure RNG for all gambling outcomes.
# Python's default `random` uses Mersenne Twister (predictable if seed is known).
_srng = secrets.SystemRandom()

import discord
from discord import app_commands
from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_allowed, no_bots
from core.framework.cooldowns import user_cooldown
from core.framework.tx import set_tx
from core.framework import whale as _whale
from core.framework.ui import C_NEUTRAL, C_PINK, C_PURPLE, C_SUCCESS, C_GOLD, C_ERROR, FormatKit, fmt_ts, fmt_usd
from core.framework.embed import card
from services.board_render import (
    render_blackjack_png, render_roulette_png,
)
from core.framework.fuzzy import suggest_subcommand
from core.framework.utils import parse_amount
from core.framework.scale import to_human, to_raw
from core.framework.shutdown import (
    complete_game_session,
    register_active_view,
    start_game_session,
    unregister_active_view,
)

# ── Slots constants ───────────────────────────────────────────────────────────
_SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣"]

# ── Roulette constants ────────────────────────────────────────────────────────
_RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
_BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}
_COLUMNS = {
    1: set(range(1, 37, 3)),
    2: set(range(2, 37, 3)),
    3: set(range(3, 37, 3)),
}

# ── Mines constants ───────────────────────────────────────────────────────────
from constants.games import (
    MINES_TOTAL_TILES as _MINES_TOTAL,
    MINES_DEFAULT_BOMBS as _MINES_DEFAULT,
    MINES_MIN_BOMBS as _MINES_MIN_BOMBS,
    MINES_MAX_BOMBS as _MINES_MAX_BOMBS,
    MINES_HOUSE_EDGE as _MINES_HOUSE_EDGE,
    MINES_TIMEOUT_SECS as _MINES_TIMEOUT,
    CF_STREAK_MIN as _CF_STREAK_MIN,
    CF_STREAK_MAX as _CF_STREAK_MAX,
    CF_DON_MAX_ROUNDS as _CF_DON_MAX,
    CF_DON_TIMEOUT as _CF_DON_TIMEOUT,
    CF_TRIO_COUNT as _CF_TRIO_COUNT,
    CF_RAINBOW_COUNT as _CF_RAINBOW_COUNT,
    CF_RAINBOW_PICK_MIN as _CF_RAINBOW_MIN,
    CF_RAINBOW_PICK_MAX as _CF_RAINBOW_MAX,
    DICE_ROLL_SIZE as _DICE_ROLL_SIZE,
    DICE_OVER_MIN as _DICE_OVER_MIN,
    DICE_OVER_MAX as _DICE_OVER_MAX,
    DICE_UNDER_MIN as _DICE_UNDER_MIN,
    DICE_UNDER_MAX as _DICE_UNDER_MAX,
    DICE_RANGE_MIN_SIZE as _DICE_RANGE_MIN,
    DICE_RANGE_MAX_SIZE as _DICE_RANGE_MAX,
    DICE_EXACT_MIN as _DICE_EXACT_MIN,
    DICE_EXACT_MAX as _DICE_EXACT_MAX,
    DICE_LADDER_MIN as _DICE_LADDER_MIN,
    DICE_LADDER_MAX as _DICE_LADDER_MAX,
    GAME_ANIM_FRAME_DELAY as _ANIM_DELAY,
    GAME_ANIM_STEP_DELAY as _ANIM_STEP,
)


# ── Blackjack helpers ─────────────────────────────────────────────────────────

def _card_name(c: int) -> str:
    return {1: "A", 11: "J", 12: "Q", 13: "K"}.get(c, str(c))


def _hand_value(cards: list[int]) -> int:
    total = 0
    aces = 0
    for c in cards:
        val = min(c, 10)
        if c == 1:
            aces += 1
            val = 11
        total += val
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


# ── Roulette helpers ──────────────────────────────────────────────────────────

def _spin_color(n: int) -> str:
    if n == 0:
        return "🟢 Green"
    return "🔴 Red" if n in _RED_NUMBERS else "⚫ Black"


_ROULETTE_BET_TYPES = ("number", "red", "black", "odd", "even", "dozen", "column")


def _parse_roulette_bet(bet_type: str, detail: str) -> tuple[set, float]:
    if bet_type == "number":
        try:
            n = int(detail)
        except (ValueError, TypeError):
            raise ValueError("Specify a number 0-36. Example: `play roulette 100 number 17`")
        if not 0 <= n <= 36:
            raise ValueError("Number must be between 0 and 36.")
        return {n}, 35.0
    if bet_type in ("red", "black"):
        return (_RED_NUMBERS if bet_type == "red" else _BLACK_NUMBERS), 1.0
    if bet_type in ("odd", "even"):
        nums = {n for n in range(1, 37) if (n % 2 == 0) == (bet_type == "even")}
        return nums, 1.0
    if bet_type == "dozen":
        try:
            d = int(detail)
        except (ValueError, TypeError):
            raise ValueError("Specify dozen 1, 2, or 3. Example: `play roulette 100 dozen 2`")
        if d not in (1, 2, 3):
            raise ValueError("Dozen must be 1, 2, or 3.")
        start = (d - 1) * 12 + 1
        return set(range(start, start + 12)), 2.0
    if bet_type == "column":
        try:
            c = int(detail)
        except (ValueError, TypeError):
            raise ValueError("Specify column 1, 2, or 3. Example: `play roulette 100 column 1`")
        if c not in (1, 2, 3):
            raise ValueError("Column must be 1, 2, or 3.")
        return _COLUMNS[c], 2.0
    valid = " ".join(f"`{t}`" for t in _ROULETTE_BET_TYPES)
    raise ValueError(
        f"Unknown bet type `{bet_type}`. Valid: {valid}"
    )


# ── Mines helpers ─────────────────────────────────────────────────────────────

def _all_mine_tiles() -> list[tuple[int, int]]:
    """Return all 24 tile coordinates for the grid.

    Layout:
      Rows 0-3: x in 0-4 (5 tiles each = 20 tiles)
      Row 4:    x in 0-3 (4 tiles) + x=4 is the Cash Out button
    """
    tiles: list[tuple[int, int]] = [(x, y) for y in range(4) for x in range(5)]
    tiles += [(x, 4) for x in range(4)]
    return tiles  # len == 24


# ── Blackjack View ────────────────────────────────────────────────────────────

class BlackjackView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=60.0)
        self._author_id = author_id
        self.action: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("Not your table.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.danger)
    async def hit_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.action = "hit"
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.action = "stand"
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()


# ── Mines Game State ──────────────────────────────────────────────────────────

class MinesGame:
    """All mutable state for a single Mines session."""

    TOTAL = _MINES_TOTAL

    def __init__(self, user_id: int, bet: float, token: str, bomb_count: int) -> None:
        self.user_id    = user_id
        self.bet        = bet
        self.token      = token
        self.bomb_count = bomb_count
        self.bombs: set[tuple[int, int]] = set(
            map(tuple, _srng.sample(_all_mine_tiles(), bomb_count))  # type: ignore[arg-type]
        )
        self.revealed: set[tuple[int, int]] = set()
        self.safe_picks   = 0
        self.multiplier   = 1.0
        self.done         = False
        # Written before done_event fires:
        self.delta: float  = 0.0
        self.result: str   = ""
        self.payout: float = 0.0
        # Set by command coroutine after the initial send:
        self.message: discord.Message | None = None
        # Serializes concurrent button-click Tasks:
        self._click_lock = asyncio.Lock()

    def safe_tiles_total(self) -> int:
        return self.TOTAL - self.bomb_count

    def current_cashout(self) -> float:
        """Gross payout if cashed out now."""
        return self.bet * self.multiplier

    def advance_multiplier(self) -> None:
        """
        Update multiplier after a safe pick (safe_picks already incremented to k).

        Provably-fair formula:
          tiles_before = TOTAL - (k - 1)
          safe_before  = safe_tiles_total() - (k - 1)
          p            = safe_before / tiles_before
          multiplier  *= (1 / p) * (1 - HOUSE_EDGE)

        EV after k picks = bet * (1 - HOUSE_EDGE)^k  <  bet  for all k >= 1.
        """
        k = self.safe_picks
        tiles_before = self.TOTAL - (k - 1)
        safe_before  = self.safe_tiles_total() - (k - 1)
        if tiles_before <= 0 or safe_before <= 0:
            return
        p = safe_before / tiles_before
        self.multiplier *= (1.0 / p) * (1.0 - _MINES_HOUSE_EDGE)


# ── Mines Discord UI components ──────────────────────────────────────────────

class MinesTileButton(discord.ui.Button):
    def __init__(self, x: int, y: int) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="\u200b",   # zero-width space  -  visually blank
            row=y,
        )
        self.tx = x
        self.ty = y

    async def callback(self, interaction: discord.Interaction) -> None:
        view: MinesView = self.view  # type: ignore[assignment]
        await view.handle_tile(interaction, self.tx, self.ty)


class MinesCashoutButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label="💰 Cash Out",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: MinesView = self.view  # type: ignore[assignment]
        await view.handle_cashout(interaction)


class MinesView(discord.ui.View):
    """
    Interactive Mines game board.

    Signal contract
    ───────────────
    done_event is set (exactly once) when the game reaches a terminal state.
    game.result / game.delta / game.payout are written before done_event.set().
    The command coroutine (holding the per-user lock) wakes on done_event and
    handles all balance changes + transaction logging.  View callbacks NEVER
    touch the balance  -  they only update embeds/buttons and signal the command.
    """

    def __init__(
        self,
        game: MinesGame,
        cog: "Play",
        done_event: asyncio.Event,
    ) -> None:
        super().__init__(timeout=_MINES_TIMEOUT)
        self.game       = game
        self.cog        = cog
        self.done_event = done_event
        self._tiles: dict[tuple[int, int], MinesTileButton] = {}
        self._cashout_btn: MinesCashoutButton | None = None
        self._build_buttons()

    def _build_buttons(self) -> None:
        # Rows 0-3: 5 tile buttons each
        for y in range(4):
            for x in range(5):
                btn = MinesTileButton(x, y)
                self._tiles[(x, y)] = btn
                self.add_item(btn)
        # Row 4: 4 tile buttons (x=0-3)
        for x in range(4):
            btn = MinesTileButton(x, 4)
            self._tiles[(x, 4)] = btn
            self.add_item(btn)
        # Row 4, slot x=4: Cash Out
        cashout = MinesCashoutButton()
        self._cashout_btn = cashout
        self.add_item(cashout)

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game.user_id:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return False
        return True

    async def handle_tile(
        self, interaction: discord.Interaction, x: int, y: int
    ) -> None:
        async with self.game._click_lock:
            coord = (x, y)
            if self.game.done or coord in self.game.revealed:
                await interaction.response.defer()
                return

            if coord in self.game.bombs:
                # ── Bomb hit ──────────────────────────────────────────────────
                self.game.done   = True
                self.game.delta  = -self.game.bet
                self.game.payout = 0.0
                self.game.result = "bomb"
                self._disable_all()
                for (bx, by), btn in self._tiles.items():
                    if (bx, by) in self.game.bombs:
                        btn.style = discord.ButtonStyle.danger
                        btn.label = "💣"
                self._tiles[coord].label = "💥"
                await interaction.response.edit_message(
                    embed=self._build_embed_bomb(), view=self,
                )
                self.stop()
                self.done_event.set()

            else:
                # ── Safe tile ─────────────────────────────────────────────────
                self.game.revealed.add(coord)
                self.game.safe_picks += 1
                self.game.advance_multiplier()
                btn = self._tiles[coord]
                btn.style    = discord.ButtonStyle.primary
                btn.label    = "✅"
                btn.disabled = True

                if self.game.safe_picks >= self.game.safe_tiles_total():
                    # ── Auto-win: all safe tiles cleared ──────────────────────
                    payout = self.game.current_cashout()
                    self.game.payout = payout
                    self.game.delta  = payout - self.game.bet
                    self.game.result = "autowin"
                    self._disable_all()
                    await interaction.response.edit_message(
                        embed=self._build_embed_autowin(), view=self,
                    )
                    self.stop()
                    self.done_event.set()
                else:
                    await interaction.response.edit_message(
                        embed=self._build_embed_playing(), view=self,
                    )

    async def handle_cashout(self, interaction: discord.Interaction) -> None:
        async with self.game._click_lock:
            if self.game.done:
                await interaction.response.defer()
                return
            if self.game.safe_picks == 0:
                await interaction.response.send_message(
                    "Reveal at least one tile before cashing out!", ephemeral=True
                )
                return
            payout = self.game.current_cashout()
            self.game.done   = True
            self.game.payout = payout
            self.game.delta  = payout - self.game.bet
            self.game.result = "cashout"
            self._disable_all()
            await interaction.response.edit_message(
                embed=self._build_embed_cashout(), view=self,
            )
            self.stop()
            self.done_event.set()

    async def on_timeout(self) -> None:
        if self.game.done:
            return
        self.game.done = True
        if self.game.safe_picks == 0:
            self.game.payout = 0.0
            self.game.delta  = -self.game.bet
            self.game.result = "timeout_forfeit"
        else:
            payout = self.game.current_cashout()
            self.game.payout = payout
            self.game.delta  = payout - self.game.bet
            self.game.result = "timeout_cashout"
        self._disable_all()
        if self.game.message:
            try:
                await self.game.message.edit(
                    embed=self._build_embed_timeout(), view=self,
                )
            except discord.HTTPException:
                pass
        self.done_event.set()

    async def handle_shutdown(self) -> None:
        # Called when the bot is draining active games before a redeploy.
        # Refund the bet if no tiles were picked; otherwise cash out at the
        # current multiplier. Never forfeit on shutdown.
        if self.game.done:
            return
        self.game.done = True
        if self.game.safe_picks == 0:
            self.game.payout = self.game.bet
            self.game.delta  = 0.0
            self.game.result = "shutdown_refund"
        else:
            payout = self.game.current_cashout()
            self.game.payout = payout
            self.game.delta  = payout - self.game.bet
            self.game.result = "shutdown_cashout"
        self._disable_all()
        if self.game.message:
            try:
                await self.game.message.edit(
                    embed=self._build_embed_timeout(), view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()
        self.done_event.set()

    # ── Embed builders ─────────────────────────────────────────────────────────

    def _badge(self) -> str:
        return self.cog._token_badge(self.game.token)

    def _fmt(self, amount: float) -> str:
        return self.cog._fmt_amount(amount, self.game.token)

    def _build_embed_initial(self) -> discord.Embed:
        b = self.game.bomb_count
        safe = self.game.safe_tiles_total()
        return (
            card(
                f"💣 Mines  {self._badge()}",
                description=(
                    f"**{b}** bomb{'s' if b != 1 else ''} hidden in **{_MINES_TOTAL}** tiles  •  **{safe}** safe squares\n"
                    "Click tiles to reveal safe squares  -  hit a bomb and lose everything!\n"
                    "Each safe tile raises your multiplier. Hit **💰 Cash Out** to secure winnings."
                ),
                color=C_PINK,
            )
            .field("💣 Bombs",      str(b),                   True)
            .field("💰 Bet",        self._fmt(self.game.bet), True)
            .field("✖️ Multiplier", "1.00×",                  True)
            .build()
        )

    def _build_embed_playing(self) -> discord.Embed:
        g = self.game
        cashout = g.current_cashout()
        profit = cashout - g.bet
        return (
            card(
                f"💣 Mines  -  In Progress  {self._badge()}",
                description=(
                    f"**{g.safe_picks}** safe tile{'s' if g.safe_picks != 1 else ''} revealed  -  "
                    f"multiplier is growing! Cash out to lock in your winnings."
                ),
                color=C_PINK,
            )
            .field(
                "💣 Game",
                f"💣 Bombs: {g.bomb_count}\n"
                f"✖️ Multiplier: **{g.multiplier:.2f}×**\n"
                f"🗂️ Safe Tiles: {g.safe_picks} / {g.safe_tiles_total()}",
                True,
            )
            .field(
                "💰 Payout",
                f"💰 Bet: {self._fmt(g.bet)}\n"
                f"💸 Cash Out: **{self._fmt(cashout)}**  (+{self._fmt(profit)})",
                True,
            )
            .build()
        )

    def _build_embed_bomb(self) -> discord.Embed:
        g = self.game
        return (
            card(
                f"💥 BOOM! You Hit a Mine  {self._badge()}",
                description=(
                    f"After **{g.safe_picks}** safe pick{'s' if g.safe_picks != 1 else ''}, "
                    f"you found a bomb! Better luck next time.\n"
                    f"Lost **{self._fmt(g.bet)}**."
                ),
                color=C_ERROR,
            )
            .field(
                "💣 Game",
                f"💣 Bombs: {g.bomb_count}\n"
                f"✅ Safe Picks: {g.safe_picks}",
                True,
            )
            .field("💸 Lost", self._fmt(g.bet), True)
            .build()
        )

    def _build_embed_cashout(self) -> discord.Embed:
        g = self.game
        profit = g.delta
        return (
            card(
                f"💰 Cashed Out!  {self._badge()}",
                description=(
                    f"Smart move  -  cashed out at **{g.multiplier:.2f}×** after "
                    f"**{g.safe_picks}** safe pick{'s' if g.safe_picks != 1 else ''}."
                ),
                color=C_SUCCESS,
            )
            .field(
                "✖️ Payout",
                f"✖️ Multiplier: **{g.multiplier:.2f}×**\n"
                f"💸 Payout: {self._fmt(g.payout)}",
                True,
            )
            .field("📈 Profit", f"+{self._fmt(profit)}", True)
            .build()
        )

    def _build_embed_autowin(self) -> discord.Embed:
        g = self.game
        return (
            card(
                f"🏆 PERFECT SWEEP  -  Jackpot!  {self._badge()}",
                description=(
                    f"Unbelievable  -  you cleared **all {g.safe_tiles_total()} safe tiles**!\n"
                    f"Final multiplier: **{g.multiplier:.2f}×**"
                ),
                color=C_GOLD,
            )
            .field(
                "✖️ Payout",
                f"✖️ Multiplier: **{g.multiplier:.2f}×**\n"
                f"💸 Payout: {self._fmt(g.payout)}",
                True,
            )
            .field("📈 Profit", f"+{self._fmt(g.delta)}", True)
            .build()
        )

    def _build_embed_timeout(self) -> discord.Embed:
        g = self.game
        if g.result == "timeout_forfeit":
            return (
                card(
                    f"⏰ Timed Out  {self._badge()}",
                    description=(
                        "Game expired  -  no tiles were revealed, so the bet is forfeited.\n"
                        f"Lost **{self._fmt(g.bet)}**."
                    ),
                    color=C_ERROR,
                )
                .field("💸 Lost", self._fmt(g.bet), True)
                    .build()
            )
        return (
            card(
                f"⏰ Auto Cash Out  {self._badge()}",
                description=(
                    f"Game timed out  -  auto-cashed out at **{g.multiplier:.2f}×** "
                    f"after **{g.safe_picks}** safe pick{'s' if g.safe_picks != 1 else ''}."
                ),
                color=C_SUCCESS,
            )
            .field(
                "✖️ Payout",
                f"✖️ Multiplier: **{g.multiplier:.2f}×**\n"
                f"💸 Payout: {self._fmt(g.payout)}",
                True,
            )
            .field("📈 Profit", f"+{self._fmt(g.delta)}", True)
            .build()
        )


# ── Double-or-Nothing State ───────────────────────────────────────────────────

class _DoNState:
    """Mutable state for a double-or-nothing coinflip session."""

    def __init__(
        self, user_id: int, bet: float, token: str, side: str, initial_payout: float,
    ) -> None:
        self.user_id = user_id
        self.bet = bet
        self.token = token
        self.side = side
        self.payout = initial_payout
        self.pre_bust_payout = initial_payout
        self.round = 0
        self.flips: list[str] = []
        self.done = False
        self.result = ""
        self.message: discord.Message | None = None
        self._click_lock = asyncio.Lock()


# ── Double-or-Nothing View ───────────────────────────────────────────────────

class _DoNView(discord.ui.View):
    """Interactive double-or-nothing coinflip board.

    Signal contract matches MinesView: done_event is set exactly once when
    the game reaches a terminal state.  The command coroutine holds the
    per-user lock and handles all balance changes after done_event fires.
    """

    def __init__(
        self, state: _DoNState, cog: "Play", done_event: asyncio.Event,
    ) -> None:
        super().__init__(timeout=_CF_DON_TIMEOUT)
        self.state = state
        self.cog = cog
        self.done_event = done_event

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return False
        return True

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    # ── Buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="Double", style=discord.ButtonStyle.danger)
    async def double_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        async with self.state._click_lock:
            if self.state.done:
                await interaction.response.defer()
                return
            self.state.pre_bust_payout = self.state.payout
            result = _srng.choice(("heads", "tails"))
            self.state.flips.append(result)
            won = result == self.state.side
            if not won:
                self.state.done = True
                self.state.payout = 0.0
                self.state.result = "bust"
                self._disable_all()
                await interaction.response.edit_message(
                    embed=self._build_bust_embed(), view=self,
                )
                self.stop()
                self.done_event.set()
                return
            self.state.payout *= 2.0
            self.state.round += 1
            if self.state.round >= _CF_DON_MAX:
                self.state.done = True
                self.state.result = "max"
                self._disable_all()
                await interaction.response.edit_message(
                    embed=self._build_max_embed(), view=self,
                )
                self.stop()
                self.done_event.set()
                return
            await interaction.response.edit_message(
                embed=self._build_playing_embed(), view=self,
            )

    @discord.ui.button(label="Cash Out", style=discord.ButtonStyle.success)
    async def cashout_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        async with self.state._click_lock:
            if self.state.done:
                await interaction.response.defer()
                return
            self.state.done = True
            self.state.result = "cashout"
            self._disable_all()
            await interaction.response.edit_message(
                embed=self._build_cashout_embed(), view=self,
            )
            self.stop()
            self.done_event.set()

    async def on_timeout(self) -> None:
        if self.state.done:
            return
        self.state.done = True
        self.state.result = "timeout"
        self._disable_all()
        if self.state.message:
            try:
                await self.state.message.edit(
                    embed=self._build_timeout_embed(), view=self,
                )
            except discord.HTTPException:
                pass
        self.done_event.set()

    async def handle_shutdown(self) -> None:
        # Cash out at current state.payout (same behaviour as on_timeout:
        # DoN keeps accumulated winnings, never forfeits).
        if self.state.done:
            return
        self.state.done = True
        self.state.result = "timeout"
        self._disable_all()
        if self.state.message:
            try:
                await self.state.message.edit(
                    embed=self._build_timeout_embed(), view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()
        self.done_event.set()

    # ── Embed builders ────────────────────────────────────────────────────────

    def _badge(self) -> str:
        return self.cog._token_badge(self.state.token)

    def _fmt(self, amount: float) -> str:
        return self.cog._fmt_amount(amount, self.state.token)

    def _flip_history(self) -> str:
        icons = []
        for f in self.state.flips:
            if f == self.state.side:
                icons.append(f"🟢 {f[0].upper()}")
            else:
                icons.append(f"🔴 {f[0].upper()}")
        return " -> ".join(icons) if icons else "-"

    def _build_playing_embed(self) -> discord.Embed:
        s = self.state
        profit = s.payout - s.bet
        doubles = s.round
        suffix = "s" if doubles != 1 else ""
        header = "Opening flip won!" if doubles == 0 else f"**{doubles}** double{suffix} won!"
        return (
            card(
                f"🪙 Double or Nothing  {self._badge()}",
                description=(
                    f"{header}  Risk it all for **{self._fmt(s.payout * 2)}**, or cash out now."
                ),
                color=C_GOLD,
            )
            .field("🪙 Flips", self._flip_history(), False)
            .field(
                "💰 Current Payout",
                f"💰 Bet: {self._fmt(s.bet)}\n"
                f"💸 Payout: **{self._fmt(s.payout)}**\n"
                f"📈 Profit: +{self._fmt(profit)}",
                True,
            )
            .field(
                "🎲 Next Double",
                f"Win: **{self._fmt(s.payout * 2)}**\n"
                f"Lose: **{self._fmt(0)}** (bust)",
                True,
            )
            .build()
        )

    def _build_bust_embed(self) -> discord.Embed:
        s = self.state
        return (
            card(
                f"💀 Busted!  {self._badge()}",
                description=(
                    f"The coin landed **{s.flips[-1].upper()}** - Loss!\n"
                    f"Should have cashed out at **{self._fmt(s.pre_bust_payout)}**."
                ),
                color=C_ERROR,
            )
            .field("🪙 Flips", self._flip_history(), False)
            .field("💸 Lost", self._fmt(s.bet), True)
            .build()
        )

    def _build_cashout_embed(self) -> discord.Embed:
        s = self.state
        profit = s.payout - s.bet
        return (
            card(
                f"💰 Cashed Out!  {self._badge()}",
                description=(
                    f"Secured **{self._fmt(s.payout)}** after "
                    f"**{s.round}** successful double{'s' if s.round != 1 else ''}!"
                ),
                color=C_SUCCESS,
            )
            .field("🪙 Flips", self._flip_history(), False)
            .field(
                "💰 Payout",
                f"💰 Bet: {self._fmt(s.bet)}\n"
                f"💸 Payout: **{self._fmt(s.payout)}**\n"
                f"📈 Profit: +{self._fmt(profit)}",
                True,
            )
            .build()
        )

    def _build_max_embed(self) -> discord.Embed:
        s = self.state
        profit = s.payout - s.bet
        return (
            card(
                f"🏆 Max Doubles!  {self._badge()}",
                description=(
                    f"Hit the **{_CF_DON_MAX} double maximum**!\n"
                    f"Final payout: **{self._fmt(s.payout)}**"
                ),
                color=C_GOLD,
            )
            .field("🪙 Flips", self._flip_history(), False)
            .field(
                "💰 Payout",
                f"💰 Bet: {self._fmt(s.bet)}\n"
                f"💸 Payout: **{self._fmt(s.payout)}**\n"
                f"📈 Profit: +{self._fmt(profit)}",
                True,
            )
            .build()
        )

    def _build_timeout_embed(self) -> discord.Embed:
        s = self.state
        profit = s.payout - s.bet
        return (
            card(
                f"⏰ Auto Cash Out  {self._badge()}",
                description=(
                    f"Timed out - auto-cashed out after "
                    f"**{s.round}** double{'s' if s.round != 1 else ''}."
                ),
                color=C_SUCCESS,
            )
            .field("🪙 Flips", self._flip_history(), False)
            .field(
                "💰 Payout",
                f"💰 Bet: {self._fmt(s.bet)}\n"
                f"💸 Payout: **{self._fmt(s.payout)}**\n"
                f"📈 Profit: +{self._fmt(profit)}",
                True,
            )
            .build()
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Play cog  -  unified /play group combining gamble + games
# ══════════════════════════════════════════════════════════════════════════════

class Play(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        # Cache balance from _resolve_amount("all") so _validate_bet uses the
        # same read rather than issuing a second DB query that could race with
        # external wallet changes (API, transfers, etc.).
        self._balance_prefetch: dict[tuple[int, int, str], float] = {}
        # Raw (DB-scale) balance cached alongside the human float. Used to cap
        # upfront bet deductions for "all" bets and prevent to_raw(to_human(raw))
        # from rounding up past the actual balance, causing spurious "insufficient
        # balance" errors.
        self._balance_prefetch_raw: dict[tuple[int, int, str], int] = {}

    # ── Module check ──────────────────────────────────────────────────────────

    async def cog_check(self, ctx) -> bool:
        if ctx.guild:
            gamble_ok = await module_allowed(ctx, "gambling")
            games_ok = await module_allowed(ctx, "games")
            if not gamble_ok and not games_ok:
                raise commands.CheckFailure("The **gamble** and **games** modules are both disabled on this server.")
        return True

    async def _check_submodule(self, ctx, sub: str) -> bool:
        """Returns True if the sub-module is enabled; sends error and returns False otherwise."""
        if ctx.guild and not await module_allowed(ctx, f"gambling_{sub}"):
            await ctx.reply_error(f"**{sub.capitalize()}** is disabled on this server.")
            return False
        return True

    # ── Balance helpers ───────────────────────────────────────────────────────

    # Tokens stored in wallet_holdings on the gam-network short. Mirrors
    # _PARTNER_NETWORK_BY_SYM in services/buddy_economy.py and the migration
    # 0235 scope. The central holding helpers below dispatch on this set so
    # GBC/game-token bets land in (and pay out of) the DeFi wallet, the same
    # place ,wallet list reads from.
    _GAMBA_WALLET_TOKENS: frozenset[str] = frozenset({
        "GBC", "GAMBIT", "CROWN", "VEIN", "PIP",
        "EDGE", "ACE", "NOIR", "CHERRY",
    })

    async def _holding_get_raw(
        self, ctx: DiscoContext, uid: int, token: str,
    ) -> int:
        """Read a non-USD token balance, dispatching by storage table."""
        if token in self._GAMBA_WALLET_TOKENS:
            row = await ctx.db.get_wallet_holding(
                uid, ctx.guild_id, Config.GAMBA_NETWORK_SHORT, token,
            )
        else:
            row = await ctx.db.get_holding(uid, ctx.guild_id, token)
        return int(row["amount"]) if row else 0

    async def _holding_update_raw(
        self, ctx: DiscoContext, uid: int, token: str, delta_raw: int,
    ) -> int:
        """Adjust a non-USD token balance, dispatching by storage table."""
        if token in self._GAMBA_WALLET_TOKENS:
            return await ctx.db.update_wallet_holding(
                uid, ctx.guild_id,
                Config.GAMBA_NETWORK_SHORT, token, int(delta_raw),
            )
        return await ctx.db.update_holding(
            uid, ctx.guild_id, token, int(delta_raw),
        )

    async def _get_balance(self, ctx: DiscoContext, token: str) -> float:
        """Return the user's current balance (human units) for the given token.

        Database columns are raw-scaled NUMERIC(36,0) (see migration
        0075_scaled_integers.sql), so always descale at this boundary and
        keep the rest of the cog in human units.
        """
        if token == "USD":
            row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            return row.h("wallet") if row else 0.0
        return to_human(await self._holding_get_raw(ctx, ctx.author.id, token))

    async def _resolve_amount(
        self, ctx: DiscoContext, amount_str: str, token: str
    ) -> float | None:
        """Resolve 'all' to balance or parse as float. Returns None and sends error on failure."""
        if amount_str.lower() == "all":
            # Fetch raw balance directly so we can cache it for safe deduction.
            # to_raw(to_human(raw)) can round up past raw due to float64 limits,
            # causing spurious "insufficient balance" DB errors on "all" bets.
            if token == "USD":
                _row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
                raw = int(_row["wallet"]) if _row else 0
            else:
                raw = await self._holding_get_raw(ctx, ctx.author.id, token)
            bal = to_human(raw)
            if bal <= 0:
                await ctx.reply_error(f"You have no **{token}** to bet.")
                return None
            # Cache human balance so _validate_bet reuses this read.
            self._balance_prefetch[(ctx.author.id, ctx.guild_id, token)] = bal
            self._balance_prefetch_raw[(ctx.author.id, ctx.guild_id, token)] = raw
            return bal
        try:
            return parse_amount(amount_str)[0]
        except ValueError:
            await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
            return None

    async def _get_gamble_items(self, ctx: DiscoContext) -> dict | None:
        """Fetch hashstone ONCE per game. Pre-fetching avoids redundant DB round-trips per roll."""
        uid, gid = ctx.author.id, ctx.guild_id
        return await ctx.db.get_hashstone(uid, gid)

    def _apply_hall_bonus(self, ctx: DiscoContext, delta: float) -> float:
        """Boost a winning delta by the Hall gambling bonus; records pct on ctx."""
        if delta > 0:
            pct = getattr(ctx, "hall_bonus", {}).get("gambling", 0.0)
            if pct > 0:
                delta = round(delta * (1.0 + pct), 2)
                ctx._hall_bonus_pct = pct  # type: ignore[attr-defined]
        return delta

    def _game_footer(self, ctx: DiscoContext, bal_label: str, new_bal: float, token: str) -> str:
        base = f"{bal_label}: {self._fmt_amount(new_bal, token)}"
        pct = getattr(ctx, "_hall_bonus_pct", 0.0)
        if pct:
            return f"{base} | Hall +{round(pct * 100):.0f}%"
        return base

    async def _apply_delta(self, ctx: DiscoContext, token: str, delta: float) -> tuple[float, float]:
        """Apply win/loss delta to wallet (USD) or holding, return (new_balance, final_delta)."""
        if delta > 0:
            delta = self._apply_hall_bonus(ctx, delta)
            # Guild gambling multiplier (admin-configurable)
            try:
                _gsettings = await ctx.db.get_guild_settings(ctx.guild_id)
                _gmult = float(_gsettings.get("gambling_multiplier") or 1.0)
                if _gmult != 1.0:
                    delta = round(delta * _gmult, 2)
            except Exception:
                pass
        # DB ledger columns are raw-scaled NUMERIC(36,0); convert the human
        # delta once at the boundary and return the new balance in human units.
        delta_raw = to_raw(delta)
        # Drop any stale "all" prefetch cap -- we now re-read live below.
        self._balance_prefetch_raw.pop((ctx.author.id, ctx.guild_id, token), None)
        if delta_raw < 0:
            # Re-read the live balance at apply time and clamp the deduction
            # so the DB's (balance + delta) >= 0 guard cannot fail. This is
            # the single chokepoint that makes every gambling flow resilient
            # to: (a) float->raw rounding overshoot on "all" bets, (b) stale
            # prefetch caps, and (c) external wallet changes (shop/trade/
            # transfer) that race with the in-game animation window.
            if token == "USD":
                _row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
                _avail_raw = int(_row["wallet"]) if _row else 0
            else:
                _avail_raw = await self._holding_get_raw(ctx, ctx.author.id, token)
            if -delta_raw > _avail_raw:
                delta_raw = -_avail_raw
                delta = -to_human(_avail_raw)
        if token == "USD":
            new_raw = await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, delta_raw)
        else:
            new_raw = await self._holding_update_raw(ctx, ctx.author.id, token, delta_raw)
        return to_human(int(new_raw)), delta

    def _win_factor_from(self, hashstone: dict | None) -> float:
        """Compute win payout factor from pre-fetched hashstone (no DB calls)."""
        return 0.95

    async def _win_factor(self, ctx: DiscoContext) -> float:
        """Win payout factor (convenience wrapper that fetches items itself)."""
        hashstone = await self._get_gamble_items(ctx)
        return self._win_factor_from(hashstone)

    async def _validate_bet(self, ctx: DiscoContext, token: str, amount: float) -> str | None:
        """Returns an error string if the bet is invalid, else None.

        Config.MIN_BET is stored raw-scaled (x10^18), so descale
        before comparing to the human `amount`.
        """
        if math.isnan(amount) or math.isinf(amount):
            return "Invalid bet amount."
        if amount <= 0:
            return "Amount must be positive."
        min_bet_h = to_human(Config.MIN_BET)
        if amount < min_bet_h:
            return f"Minimum bet is **{min_bet_h:,.2f}** {token}."
        # Use the balance pre-fetched by _resolve_amount("all") if available
        # (avoids a second DB read that could race with external wallet changes).
        _pf_key = (ctx.author.id, ctx.guild_id, token)
        balance = self._balance_prefetch.pop(_pf_key, None)
        if balance is None:
            balance = await self._get_balance(ctx, token)
        tol = 0.005 if token == "USD" else 1e-9
        if amount > balance + tol:
            bal_str = fmt_usd(balance) if token == "USD" else f"{balance:,.4f} {token}"
            return f"Bet **{self._fmt_amount(amount, token)}** exceeds your balance (**{bal_str}**)."
        return None

    def _token_badge(self, token: str) -> str:
        return f"  `{Config.currency_label(token)}`"

    def _fmt_amount(self, amount: float, token: str) -> str:
        if token == "USD":
            return fmt_usd(amount)
        return f"{amount:,.4f} {token}"

    async def _finish_game(
        self, ctx: DiscoContext, game_name: str,
        token: str, amount: float, delta: float, new_bal: float,
    ) -> str:
        """Log the gamble/game tx and publish result event. Returns tx_hash."""
        # Record amount_out as the net payout (bet + profit/loss) so that win/loss
        # stats are accurate: amount_out - amount_in = delta (the actual P&L).
        # Previously recorded new_bal (post-game wallet balance), which inflated
        # win_rate to near 100% since balance is almost always larger than bet size.
        payout = max(0.0, amount + delta)
        # log_tx stores raw-scaled amounts (NUMERIC(36,0)); convert at the boundary.
        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, f"GAMBLE_{game_name.upper()}",
            symbol_in=token, amount_in=to_raw(amount),
            symbol_out=token, amount_out=to_raw(payout),
            network="usd" if token == "USD" else "",
        )
        await ctx.bot.bus.publish(
            "gamble_result",
            guild=ctx.guild,
            user=ctx.author,
            game=game_name,
            token=token,
            bet=amount,
            delta=delta,
            won=(delta > 0),
            tx_hash=tx_hash,
        )
        _usd = await _whale.usd_value_of(ctx.bot, token, amount, ctx.guild_id)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "gamble", _usd, symbol=token, amount=amount)
        # Log gambling outcomes as server events for AI context
        # Any nonzero result is tracked; $10k+ gets a "big" event type as well
        if delta != 0:
            _delta_usd = await _whale.usd_value_of(ctx.bot, token, abs(delta), ctx.guild_id) if token != "USD" else abs(delta)
            _won = delta > 0
            _verb = "won" if _won else "lost"
            _amt_label = fmt_usd(_delta_usd) if token == "USD" else f"{abs(delta):,.4f} {token}"
            _meta = {"game": game_name, "token": token, "bet": round(amount, 6), "delta_usd": round(_delta_usd, 2)}
            _base_type = "gamble_win" if _won else "gamble_loss"
            _summary = f"{ctx.author.display_name} {_verb} {_amt_label} on {game_name}"
            try:
                await ctx.db.log_server_event(
                    ctx.guild_id, ctx.channel.id, ctx.author.id,
                    _base_type, _summary, _delta_usd, _meta,
                )
                if _delta_usd >= 10_000:
                    _big_type = "gamble_big_win" if _won else "gamble_big_loss"
                    await ctx.db.log_server_event(
                        ctx.guild_id, ctx.channel.id, ctx.author.id,
                        _big_type, _summary, _delta_usd, _meta,
                    )
            except Exception:
                pass
        # Gamba Network hooks: themed-token mint on wins, Lucky Chip /
        # Side Bet Slip / House Marker auto-consume. Wrapped in try/except
        # so a gamba bug never blocks a settled game.
        try:
            await self._apply_gamba_hooks(
                ctx, game_name, token, amount, delta,
            )
        except Exception:
            log = __import__("logging").getLogger(__name__)
            log.exception("gamba hooks failed for %s", game_name)
        return tx_hash

    async def _apply_gamba_hooks(
        self, ctx: DiscoContext, game_name: str,
        token: str, amount: float, delta: float,
    ) -> None:
        """Mint themed Gamba tokens on win + auto-apply consumables.

        Imports kept local so the gamba surface stays optional -- a
        deployment without the gamba migration applied still settles
        gambles cleanly. Whatever happens is surfaced via a follow-up
        embed so the player can SEE the mint / refund / Lucky Chip
        bonus -- the existing per-game result embed is sealed by the
        time _finish_game runs, so a follow-up is the cleanest channel.
        """
        from services import gamba as _gs
        from configs.items_config import SHOP_ITEMS as _SI
        sym = _gs.game_token_for(game_name)
        if sym is None:
            return
        uid = ctx.author.id
        gid = ctx.guild_id
        # Collected user-facing lines + summary state for the follow-up.
        notes: list[str] = []
        minted_raw = 0
        bonus_raw = 0
        refund_raw = 0
        doubled = False
        if delta > 0:
            # Side Bet Slip doubles the token mint.
            doubled = await _gs.consume_if_present(
                ctx.db, gid, uid, "side_bet_slip",
            )
            # Convert delta -> USD so the mint scales by USD profit.
            if token == "USD":
                profit_usd = float(delta)
            else:
                try:
                    profit_usd = await _whale.usd_value_of(
                        ctx.bot, token, float(delta), gid,
                    )
                except Exception:
                    profit_usd = 0.0
            if profit_usd > 0:
                _, minted_raw = await _gs.award_game_token(
                    ctx.db, gid, uid, game_name, profit_usd,
                    side_bet_double=doubled,
                )
            # Lucky Chip: +5% on the USD payout, paid straight to wallet.
            if await _gs.consume_if_present(ctx.db, gid, uid, "lucky_chip"):
                bonus_pct = float(
                    _SI.get("lucky_chip", {}).get("stats", {}).get(
                        "gamba_win_bonus", 0.0,
                    ) or 0.05,
                )
                if token == "USD":
                    bonus_h = float(delta) * bonus_pct
                    if bonus_h > 0:
                        bonus_raw = to_raw(bonus_h)
                        await ctx.db.update_wallet(uid, gid, bonus_raw)
        elif delta < 0:
            # House Marker: refund 25% of the bet to the wallet.
            if await _gs.consume_if_present(ctx.db, gid, uid, "house_marker"):
                refund_pct = float(
                    _SI.get("house_marker", {}).get("stats", {}).get(
                        "gamba_loss_refund", 0.0,
                    ) or 0.25,
                )
                refund_h = float(amount) * refund_pct
                if refund_h > 0:
                    refund_raw = to_raw(refund_h)
                    if token == "USD":
                        await ctx.db.update_wallet(uid, gid, refund_raw)
                    else:
                        try:
                            await self._holding_update_raw(
                                ctx, uid, token, refund_raw,
                            )
                        except ValueError:
                            refund_raw = 0

        # Collect user-facing lines to inline in the per-game result
        # embed. Stashed on ``ctx`` so each game's _set_tx wrapper picks
        # them up and renders a "Gamba Network" field on the same embed
        # as the win/loss reveal -- no separate follow-up message.
        if minted_raw > 0:
            spec = Config.TOKENS.get(sym, {})
            emoji = spec.get("emoji", "")
            prefix = "\U0001F3AB 2x  " if doubled else ""
            notes.append(
                f"{prefix}{emoji} Earned **{to_human(minted_raw):,.4f} {sym}**"
            )
        if bonus_raw > 0:
            notes.append(
                f"\U0001F340 Lucky Chip: **+{fmt_usd(to_human(bonus_raw))}**"
            )
        if refund_raw > 0:
            unit = (
                fmt_usd(to_human(refund_raw)) if token == "USD"
                else f"{to_human(refund_raw):,.4f} {token}"
            )
            notes.append(
                f"\U0001F3F4 House Marker: refunded **{unit}**"
            )
        # Stash on ctx; each game's _set_tx wrapper reads this when
        # finalising the result embed. Cleared at the next round.
        if notes:
            ctx._gamba_notes = notes  # type: ignore[attr-defined]
            ctx._gamba_token = sym  # type: ignore[attr-defined]

    def _set_tx(
        self, embed: discord.Embed, ctx: DiscoContext, tx_hash: str,
        *, footer_extra: str = "",
    ) -> None:
        """Wrap core.framework.tx.set_tx + inline any gamba mint/refund notes.

        Every game in this cog used to call ``set_tx(embed, ...)`` then
        let _apply_gamba_hooks send a separate follow-up embed for the
        themed-token mint. The follow-up was a UX miss -- players
        wanted the token-earned line ON the win embed itself. So now
        every game calls this wrapper instead of set_tx directly: it
        applies the standard tx footer, then -- if gamba notes were
        stashed by _apply_gamba_hooks during _finish_game -- adds a
        single "🎰 Gamba Network" field to the embed before send.
        """
        set_tx(
            embed, ctx.guild_id, tx_hash, footer_extra=footer_extra,
        )
        notes = getattr(ctx, "_gamba_notes", None)
        if notes:
            sym = getattr(ctx, "_gamba_token", "") or ""
            value = "\n".join(notes)
            if sym:
                value += (
                    f"\n-# Stake `{ctx.prefix}gamba stake {sym} all`  ·  "
                    f"shop `{ctx.prefix}gamba shop`"
                )
            embed.add_field(
                name="\U0001F3B0 Gamba Network",
                value=value, inline=False,
            )
            # Clear so a single ctx never double-renders.
            try:
                del ctx._gamba_notes
                del ctx._gamba_token
            except AttributeError:
                pass

    # ── Animation helpers ─────────────────────────────────────────────────────

    async def _animate(
        self, ctx: DiscoContext, title: str, frames: list[str],
        color: int = C_NEUTRAL, delay: float = _ANIM_DELAY,
    ) -> discord.Message:
        """Send a teaser message cycling through `frames`, return it for a final edit.

        Each frame is rendered as the embed description. Failing edits are swallowed
        so a hiccupping Discord never blocks the final result reveal.
        """
        msg = await ctx.send(
            embed=card(title, description=frames[0], color=color).build(),
        )
        for frame in frames[1:]:
            await asyncio.sleep(delay)
            try:
                await msg.edit(
                    embed=card(title, description=frame, color=color).build(),
                )
            except discord.HTTPException:
                break
        return msg

    @staticmethod
    def _flip_anim_frames(count: int = 1) -> list[str]:
        """Cute spinning-coin teaser frames for `count` coins."""
        coins = " ".join("🪙" for _ in range(count))
        swirl = " ".join("🌀" for _ in range(count))
        spark = " ".join("✨" for _ in range(count))
        return [
            f"## {coins}\n*flip!*",
            f"## {swirl}\n*tumbling through the air...*",
            f"## {spark}\n*landing...*",
        ]

    @staticmethod
    def _roll_anim_frames() -> list[str]:
        """Cute tumbling-dice teaser frames for a single d100 roll."""
        return [
            "## 🎲\n*rattle rattle*",
            "## 🎲 🎲\n*tumbling...*",
            "## 🎯\n*almost there...*",
        ]

    # ── Argument parsers ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_cf_args(
        raw_args: list[str], valid_tokens: set[str],
    ) -> tuple[str, str, int, str, str]:
        """Parse coinflip free-form args into (mode, side, count, token, pattern)."""
        mode = "classic"
        side = "heads"
        count = -1
        token = "USD"
        pattern = ""
        for a in raw_args:
            if not a:
                continue
            low = a.lower()
            up = a.upper()
            if low in ("streak",):
                mode = "streak"
            elif low in ("don", "double"):
                mode = "don"
            elif low in ("classic",):
                mode = "classic"
            elif low in ("trio", "triple"):
                mode = "trio"
            elif low in ("rainbow", "rb"):
                mode = "rainbow"
            elif len(low) == _CF_TRIO_COUNT and set(low) <= {"h", "t"}:
                pattern = low
                if mode == "classic":
                    mode = "trio"
            elif low in ("heads", "tails", "h", "t", "head", "tail"):
                side = "tails" if low.startswith("t") else "heads"
            elif up in valid_tokens:
                token = up
            else:
                try:
                    count = int(a)
                except ValueError:
                    pass
        if count >= _CF_STREAK_MIN and mode == "classic":
            mode = "streak"
        return mode, side, count, token, pattern

    @staticmethod
    def _parse_dice_args(
        raw_args: list[str], valid_tokens: set[str],
    ) -> tuple[str, float, int | None, int | None, str]:
        """Parse dice free-form args into (mode, multiplier, target, target2, token)."""
        mode = "classic"
        multiplier = 2.0
        target: int | None = None
        target2: int | None = None
        token = "USD"
        args = [a for a in raw_args if a]
        i = 0
        while i < len(args):
            low = args[i].lower()
            up = args[i].upper()
            if low in ("over", "under"):
                mode = low
                if i + 1 < len(args):
                    try:
                        target = int(args[i + 1])
                        i += 1
                    except ValueError:
                        pass
            elif low == "range":
                mode = "range"
                if i + 1 < len(args):
                    try:
                        target = int(args[i + 1])
                        i += 1
                    except ValueError:
                        pass
                if i + 1 < len(args):
                    try:
                        target2 = int(args[i + 1])
                        i += 1
                    except ValueError:
                        pass
            elif low in ("exact", "pick"):
                mode = "exact"
                if i + 1 < len(args):
                    try:
                        target = int(args[i + 1])
                        i += 1
                    except ValueError:
                        pass
            elif low in ("odd", "even"):
                mode = low
            elif low in ("ladder", "climb"):
                mode = "ladder"
                if i + 1 < len(args):
                    try:
                        target = int(args[i + 1])
                        i += 1
                    except ValueError:
                        pass
            elif up in valid_tokens:
                token = up
            else:
                try:
                    multiplier = round(float(args[i]), 2)
                except ValueError:
                    pass
            i += 1
        return mode, multiplier, target, target2, token

    # ── Coinflip modes ───────────────────────────────────────────────────────

    async def _coinflip_classic(
        self, ctx: DiscoContext, amount: float, token: str, side: str,
    ) -> None:
        """Coinflip: classic 50/50 mode."""
        if side not in ("heads", "tails"):
            await ctx.reply_error("Choose `heads` or `tails`.")
            return
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            anim_title = f"🪙 Coin Flip  {self._token_badge(token)}"
            msg = await self._animate(ctx, anim_title, self._flip_anim_frames(1), C_NEUTRAL)
            result = _srng.choice(("heads", "tails"))
            won = result == side
            factor = await self._win_factor(ctx)
            delta = amount * factor if won else -amount
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "coinflip", token, amount, delta, new_bal)
        if won:
            title = "🎉 You Won!"
            color = C_SUCCESS
            result_icon = "🟢"
        else:
            title = "💸 You Lost"
            color = C_ERROR
            result_icon = "🔴"
        embed = (
            card(
                f"{title}  {self._token_badge(token)}",
                description=(
                    f"The coin landed on **{result.upper()}** {result_icon}\n"
                    f"You picked: **{side.upper()}**"
                ),
                color=color,
            )
            .field(
                "💰 Bet / Change",
                f"💰 {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if won else '－'}{self._fmt_amount(abs(delta), token)}",
                True,
            )
            .field("🏦 Balance", self._fmt_amount(new_bal, token), True)
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    async def _coinflip_streak(
        self, ctx: DiscoContext, amount: float, token: str, side: str, count: int,
    ) -> None:
        """Coinflip: streak mode - N consecutive flips must all match."""
        if side not in ("heads", "tails"):
            await ctx.reply_error("Choose `heads` or `tails`.")
            return
        if count < _CF_STREAK_MIN or count > _CF_STREAK_MAX:
            await ctx.reply_error(
                f"Streak count must be between **{_CF_STREAK_MIN}** and **{_CF_STREAK_MAX}**."
            )
            return
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            factor = await self._win_factor(ctx)
            flips: list[str] = []
            won = True
            for _ in range(count):
                f = _srng.choice(("heads", "tails"))
                flips.append(f)
                if f != side:
                    won = False
                    break
            multiplier = 2 ** count
            win_profit = amount * (multiplier - 1) * factor
            delta = win_profit if won else -amount
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "coinflip", token, amount, delta, new_bal)
        # Progressive reveal: show each flip appearing one at a time.
        anim_title = f"🔥 {count}-Streak on **{side.upper()}**  {self._token_badge(token)}"
        placeholders = ["⬜" for _ in range(count)]
        msg = await self._animate(
            ctx, anim_title,
            [f"## {' '.join(placeholders)}\n*flipping {count} coins...*"],
            C_GOLD,
        )
        revealed: list[str] = []
        for i, f in enumerate(flips):
            revealed.append("🟢 " + f[0].upper() if f == side else "🔴 " + f[0].upper())
            shown = revealed + placeholders[len(revealed):]
            desc = f"## {' → '.join(shown)}\n*flip {i+1} of {count}*"
            await asyncio.sleep(_ANIM_STEP)
            try:
                await msg.edit(embed=card(anim_title, description=desc, color=C_GOLD).build())
            except discord.HTTPException:
                break
        flip_icons = []
        for f in flips:
            if f == side:
                flip_icons.append(f"🟢 {f[0].upper()}")
            else:
                flip_icons.append(f"🔴 {f[0].upper()}")
        flip_str = " -> ".join(flip_icons)
        p_win = 0.5 ** count
        if won:
            title = f"🔥 {count}-Streak!"
            desc = (
                f"**{count}/{count}** flips matched **{side.upper()}**!\n"
                f"{flip_str}"
            )
            color = C_GOLD
        else:
            title = "💸 Streak Broken"
            desc = (
                f"Broke on flip **{len(flips)}** of {count}\n"
                f"{flip_str}"
            )
            color = C_ERROR
        embed = (
            card(
                f"{title}  {self._token_badge(token)}",
                description=desc,
                color=color,
            )
            .field(
                "🎯 Streak",
                f"🎯 Target: {count} in a row\n"
                f"📊 Win Chance: {p_win * 100:.2f}%\n"
                f"✖️ Multiplier: **{multiplier}x**",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    async def _coinflip_don(
        self, ctx: DiscoContext, amount: float, token: str, side: str,
    ) -> None:
        """Coinflip: double or nothing - keep flipping or cash out."""
        if side not in ("heads", "tails"):
            await ctx.reply_error("Choose `heads` or `tails`.")
            return
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            factor = await self._win_factor(ctx)
            # Deduct bet upfront (interactive game)
            amount_raw = to_raw(amount)
            _raw_cap = self._balance_prefetch_raw.pop((ctx.author.id, ctx.guild_id, token), None)
            if _raw_cap is not None:
                amount_raw = min(amount_raw, _raw_cap)
            if token == "USD":
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -amount_raw)
            else:
                await self._holding_update_raw(ctx, ctx.author.id, token, -amount_raw)
            # Opening flip
            result = _srng.choice(("heads", "tails"))
            opening_won = result == side
            if not opening_won:
                new_bal = await self._get_balance(ctx, token)
                delta = -amount
                tx_hash = await self._finish_game(ctx, "coinflip", token, amount, delta, new_bal)
                embed = (
                    card(
                        f"🪙 Double or Nothing  {self._token_badge(token)}",
                        description=(
                            f"The coin landed on **{result.upper()}** 🔴\n"
                            f"You picked **{side.upper()}** - no doubles today."
                        ),
                        color=C_ERROR,
                    )
                    .field("💸 Lost", self._fmt_amount(amount, token), True)
                    .field("🏦 Balance", self._fmt_amount(new_bal, token), True)
                    .build()
                )
                bal_label = "Wallet" if token == "USD" else f"{token} Balance"
                self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
                await ctx.send(embed=embed)
                return
            # Won - enter interactive DoN phase
            initial_payout = amount * (1.0 + factor)
            state = _DoNState(ctx.author.id, amount, token, side, initial_payout)
            state.flips.append(result)
            done_event = asyncio.Event()
            view = _DoNView(state, self, done_event)
            # Persist the in-flight bet so a SIGKILL'd process can be recovered
            # on the next boot (see core/framework/shutdown.recover_orphaned_sessions).
            session_id = await start_game_session(
                ctx.db,
                guild_id=ctx.guild_id,
                user_id=ctx.author.id,
                game_type="coinflip_don",
                bet_amount_raw=int(amount_raw),
                token=token,
            )
            register_active_view(view)
            msg = await ctx.reply(
                embed=view._build_playing_embed(), view=view, mention_author=False,
            )
            state.message = msg
            try:
                await done_event.wait()
            finally:
                unregister_active_view(view)
                await complete_game_session(ctx.db, session_id)
            # Game over - resolve balance
            if state.payout > 0:
                payout_raw = to_raw(state.payout)
                if token == "USD":
                    new_bal_raw = await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, payout_raw)
                else:
                    new_bal_raw = await self._holding_update_raw(ctx, ctx.author.id, token, payout_raw)
                new_bal = to_human(int(new_bal_raw))
            else:
                new_bal = await self._get_balance(ctx, token)
            delta = state.payout - amount
            tx_hash = await self._finish_game(ctx, "coinflip", token, amount, delta, new_bal)
            result_map = {
                "cashout": view._build_cashout_embed,
                "bust": view._build_bust_embed,
                "max": view._build_max_embed,
                "timeout": view._build_timeout_embed,
            }
            build_fn = result_map.get(state.result, view._build_timeout_embed)
            final_embed = build_fn()
            bal_label = "Wallet" if token == "USD" else f"{token} Balance"
            set_tx(final_embed, ctx.guild_id, tx_hash, footer_extra=f"{bal_label}: {self._fmt_amount(new_bal, token)}")
            try:
                await msg.edit(embed=final_embed, view=None)
            except discord.HTTPException:
                pass

    async def _coinflip_trio(
        self, ctx: DiscoContext, amount: float, token: str, pattern: str,
    ) -> None:
        """Coinflip: three-coin pattern predictor - guess the exact H/T triplet."""
        if len(pattern) != _CF_TRIO_COUNT or set(pattern) - {"h", "t"}:
            await ctx.reply_error(
                f"Trio needs a {_CF_TRIO_COUNT}-char pattern of `h`/`t`, e.g. "
                f"`hht`, `tth`. Got: `{pattern or '(none)'}`"
            )
            return
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            anim_title = f"🎰 Trio Flip  {self._token_badge(token)}"
            msg = await self._animate(
                ctx, anim_title, self._flip_anim_frames(_CF_TRIO_COUNT), C_PURPLE,
            )
            factor = await self._win_factor(ctx)
            flips = [_srng.choice(("h", "t")) for _ in range(_CF_TRIO_COUNT)]
            won = "".join(flips) == pattern
            multiplier = 2 ** _CF_TRIO_COUNT
            p_win = 0.5 ** _CF_TRIO_COUNT
            win_profit = amount * (multiplier - 1) * factor
            delta = win_profit if won else -amount
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "coinflip", token, amount, delta, new_bal)
        pattern_icons = " ".join(c.upper() for c in pattern)
        flip_icons = []
        for f, p in zip(flips, pattern):
            mark = "🟢" if f == p else "🔴"
            flip_icons.append(f"{mark} {f.upper()}")
        flip_str = " ┃ ".join(flip_icons)
        if won:
            title = "🎉 Perfect Trio!"
            color = C_GOLD
            desc = f"All {_CF_TRIO_COUNT} coins matched **{pattern.upper()}**!\n{flip_str}"
        else:
            title = "💸 Trio Miss"
            color = C_ERROR
            desc = f"Pattern **{pattern.upper()}** missed.\n{flip_str}"
        embed = (
            card(f"{title}  {self._token_badge(token)}", description=desc, color=color)
            .field(
                "🎯 Trio",
                f"🎯 Target: `{pattern_icons}`\n"
                f"📊 Win Chance: {p_win * 100:.2f}%\n"
                f"✖️ Multiplier: **{multiplier}x**",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    async def _coinflip_rainbow(
        self, ctx: DiscoContext, amount: float, token: str, side: str, pick: int,
    ) -> None:
        """Coinflip: rainbow - flip 5 coins, win if exactly `pick` match `side`."""
        if side not in ("heads", "tails"):
            await ctx.reply_error("Choose `heads` or `tails`.")
            return
        if pick < _CF_RAINBOW_MIN or pick > _CF_RAINBOW_MAX:
            await ctx.reply_error(
                f"Rainbow pick must be between **{_CF_RAINBOW_MIN}** and **{_CF_RAINBOW_MAX}**."
            )
            return
        # Binomial: C(n,k) / 2^n. House edge applied via win_factor on profit.
        n = _CF_RAINBOW_COUNT
        ways = math.comb(n, pick)
        p_win = ways / (2 ** n)
        multiplier = (2 ** n) / ways
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            anim_title = f"🌈 Rainbow Flip  {self._token_badge(token)}"
            msg = await self._animate(
                ctx, anim_title, self._flip_anim_frames(n), C_PINK,
            )
            factor = await self._win_factor(ctx)
            flips = [_srng.choice(("heads", "tails")) for _ in range(n)]
            matches = sum(1 for f in flips if f == side)
            won = matches == pick
            win_profit = amount * (multiplier - 1) * factor
            delta = win_profit if won else -amount
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "coinflip", token, amount, delta, new_bal)
        flip_icons = []
        for f in flips:
            mark = "🟢" if f == side else "⚪"
            flip_icons.append(f"{mark} {f[0].upper()}")
        flip_str = " ┃ ".join(flip_icons)
        if won:
            title = f"🌈 Rainbow Hit!  {matches}/{n}"
            color = C_GOLD
            desc = (
                f"Exactly **{pick}** of {n} landed **{side.upper()}** 🎯\n{flip_str}"
            )
        else:
            title = f"💸 Rainbow Miss  {matches}/{n}"
            color = C_ERROR
            desc = (
                f"Got **{matches}** {side.upper()}, needed exactly **{pick}**\n{flip_str}"
            )
        mult_str = f"{multiplier:.2f}x"
        embed = (
            card(f"{title}  {self._token_badge(token)}", description=desc, color=color)
            .field(
                "🌈 Rainbow",
                f"🎯 Target: exactly **{pick}** {side}\n"
                f"📊 Win Chance: {p_win * 100:.2f}%\n"
                f"✖️ Multiplier: **{mult_str}**",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    # ── Dice modes ───────────────────────────────────────────────────────────

    async def _dice_classic(
        self, ctx: DiscoContext, amount: float, token: str, multiplier: float,
    ) -> None:
        """Dice: classic multiplier mode."""
        if math.isnan(multiplier) or math.isinf(multiplier) or multiplier <= 0:
            await ctx.reply_error("Invalid multiplier.")
            return
        multiplier = round(multiplier, 2)
        if multiplier <= 1.0:
            await ctx.reply_error("Multiplier must be strictly greater than 1.0.")
            return
        if multiplier < 1.01 or multiplier > 10000:
            await ctx.reply_error("Multiplier must be between **1.01** and **10000**.")
            return
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            anim_title = f"🎲 Dice Roll  {self._token_badge(token)}"
            msg = await self._animate(ctx, anim_title, self._roll_anim_frames(), C_NEUTRAL)
            p_win = 1.0 / multiplier
            factor = await self._win_factor(ctx)
            win_profit = amount * (multiplier - 1) * factor
            roll = _srng.randint(1, 10000)
            won = roll <= round(10000 / multiplier)
            if won:
                delta = win_profit
                color = C_SUCCESS
            else:
                delta = -amount
                color = C_ERROR
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "dice", token, amount, delta, new_bal)
        mult_str = f"{multiplier:.2f}".rstrip("0").rstrip(".") + "x"
        win_threshold = round(10000 / multiplier)
        if won:
            title = "🎲 You Rolled High!"
            desc = f"Roll **{roll}** <= **{win_threshold}**  -  you win at **{mult_str}**!"
        else:
            title = "🎲 Unlucky Roll"
            desc = f"Roll **{roll}** > **{win_threshold}**  -  needed <= {win_threshold} to win."
        embed = (
            card(
                f"{title}  {self._token_badge(token)}",
                description=desc,
                color=color,
            )
            .field(
                "🎲 Game",
                f"🎯 Multiplier: **{mult_str}**\n"
                f"📊 Win Chance: {p_win * 100:.2f}%\n"
                f"🎲 Roll: **{roll}** / 10 000  (need <= {win_threshold})",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    async def _dice_over_under(
        self, ctx: DiscoContext, amount: float, token: str,
        mode: str, target: int | None,
    ) -> None:
        """Dice: over/under mode on a 1-100 roll."""
        if target is None:
            await ctx.reply_error(f"Usage: `play dice <amount> {mode} <target>`")
            return
        if mode == "over":
            if target < _DICE_OVER_MIN or target > _DICE_OVER_MAX:
                await ctx.reply_error(
                    f"Over target must be between **{_DICE_OVER_MIN}** and **{_DICE_OVER_MAX}**."
                )
                return
            winning_count = _DICE_ROLL_SIZE - target
            target_str = f"> {target}"
        else:
            if target < _DICE_UNDER_MIN or target > _DICE_UNDER_MAX:
                await ctx.reply_error(
                    f"Under target must be between **{_DICE_UNDER_MIN}** and **{_DICE_UNDER_MAX}**."
                )
                return
            winning_count = target - 1
            target_str = f"< {target}"
        p_win = winning_count / _DICE_ROLL_SIZE
        multiplier = _DICE_ROLL_SIZE / winning_count
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            anim_title = f"🎲 {mode.title()} {target}  {self._token_badge(token)}"
            msg = await self._animate(ctx, anim_title, self._roll_anim_frames(), C_NEUTRAL)
            factor = await self._win_factor(ctx)
            win_profit = amount * (multiplier - 1) * factor
            roll = _srng.randint(1, _DICE_ROLL_SIZE)
            if mode == "over":
                won = roll > target
            else:
                won = roll < target
            if won:
                delta = win_profit
                color = C_SUCCESS
            else:
                delta = -amount
                color = C_ERROR
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "dice", token, amount, delta, new_bal)
        mult_str = f"{multiplier:.2f}x"
        bar_len = 20
        bar_pos = max(1, round(roll / _DICE_ROLL_SIZE * bar_len))
        bar = "█" * bar_pos + "░" * (bar_len - bar_pos)
        if won:
            title = f"🎲 {mode.title()} {target} - You Win!"
            desc = f"Roll **{roll}** {target_str} ✅\n`[{bar}]` **{roll}** / {_DICE_ROLL_SIZE}"
        else:
            title = f"🎲 {mode.title()} {target} - No Luck"
            desc = f"Roll **{roll}** vs {target_str} ❌\n`[{bar}]` **{roll}** / {_DICE_ROLL_SIZE}"
        embed = (
            card(
                f"{title}  {self._token_badge(token)}",
                description=desc,
                color=color,
            )
            .field(
                "🎲 Game",
                f"🎯 Target: {mode.title()} {target}\n"
                f"📊 Win Chance: {p_win * 100:.1f}%\n"
                f"✖️ Multiplier: **{mult_str}**",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    async def _dice_range(
        self, ctx: DiscoContext, amount: float, token: str,
        low: int | None, high: int | None,
    ) -> None:
        """Dice: range mode - roll must land within [low, high]."""
        if low is None or high is None:
            await ctx.reply_error("Usage: `play dice <amount> range <low> <high>`")
            return
        if low > high:
            low, high = high, low
        low = max(1, low)
        high = min(_DICE_ROLL_SIZE, high)
        range_size = high - low + 1
        if range_size < _DICE_RANGE_MIN or range_size > _DICE_RANGE_MAX:
            await ctx.reply_error(
                f"Range size must be **{_DICE_RANGE_MIN}**-**{_DICE_RANGE_MAX}** numbers. "
                f"Your range [{low}, {high}] covers {range_size}."
            )
            return
        p_win = range_size / _DICE_ROLL_SIZE
        multiplier = _DICE_ROLL_SIZE / range_size
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            anim_title = f"🎲 Range [{low}-{high}]  {self._token_badge(token)}"
            msg = await self._animate(ctx, anim_title, self._roll_anim_frames(), C_NEUTRAL)
            factor = await self._win_factor(ctx)
            win_profit = amount * (multiplier - 1) * factor
            roll = _srng.randint(1, _DICE_ROLL_SIZE)
            won = low <= roll <= high
            if won:
                delta = win_profit
                color = C_SUCCESS
            else:
                delta = -amount
                color = C_ERROR
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "dice", token, amount, delta, new_bal)
        mult_str = f"{multiplier:.2f}x"
        bar_len = 20
        lo_i = max(0, round((low - 1) / _DICE_ROLL_SIZE * bar_len))
        hi_i = min(bar_len, round(high / _DICE_ROLL_SIZE * bar_len))
        bar = list("░" * bar_len)
        for idx in range(lo_i, hi_i):
            bar[idx] = "▒"
        roll_i = max(0, min(bar_len - 1, round((roll - 0.5) / _DICE_ROLL_SIZE * bar_len)))
        bar[roll_i] = "█"
        bar_str = "".join(bar)
        if won:
            title = f"🎲 Range [{low}-{high}] - Hit!"
            desc = (
                f"Roll **{roll}** lands in [{low}, {high}] ✅\n"
                f"`[{bar_str}]` **{roll}** / {_DICE_ROLL_SIZE}"
            )
        else:
            title = f"🎲 Range [{low}-{high}] - Miss"
            desc = (
                f"Roll **{roll}** outside [{low}, {high}] ❌\n"
                f"`[{bar_str}]` **{roll}** / {_DICE_ROLL_SIZE}"
            )
        embed = (
            card(
                f"{title}  {self._token_badge(token)}",
                description=desc,
                color=color,
            )
            .field(
                "🎲 Game",
                f"🎯 Range: [{low}, {high}] ({range_size} numbers)\n"
                f"📊 Win Chance: {p_win * 100:.1f}%\n"
                f"✖️ Multiplier: **{mult_str}**",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    async def _dice_exact(
        self, ctx: DiscoContext, amount: float, token: str, target: int | None,
    ) -> None:
        """Dice: exact - pick one number 1-100, win if the roll matches exactly."""
        if target is None:
            await ctx.reply_error(
                f"Usage: `play dice <amount> exact <{_DICE_EXACT_MIN}-{_DICE_EXACT_MAX}>`"
            )
            return
        if target < _DICE_EXACT_MIN or target > _DICE_EXACT_MAX:
            await ctx.reply_error(
                f"Exact target must be between **{_DICE_EXACT_MIN}** and **{_DICE_EXACT_MAX}**."
            )
            return
        p_win = 1.0 / _DICE_ROLL_SIZE
        multiplier = float(_DICE_ROLL_SIZE)
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            anim_title = f"🎯 Exact {target}  {self._token_badge(token)}"
            msg = await self._animate(ctx, anim_title, self._roll_anim_frames(), C_PURPLE)
            factor = await self._win_factor(ctx)
            win_profit = amount * (multiplier - 1) * factor
            roll = _srng.randint(1, _DICE_ROLL_SIZE)
            won = roll == target
            delta = win_profit if won else -amount
            color = C_GOLD if won else C_ERROR
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "dice", token, amount, delta, new_bal)
        if won:
            title = f"💎 Jackpot!  Exactly {target}!"
            desc = f"Roll **{roll}** = target **{target}** 🎯💫"
        else:
            title = f"🎲 Missed Target {target}"
            desc = f"Roll **{roll}**, needed exactly **{target}**."
        mult_str = f"{multiplier:.0f}x"
        embed = (
            card(f"{title}  {self._token_badge(token)}", description=desc, color=color)
            .field(
                "🎯 Exact",
                f"🎯 Target: **{target}**\n"
                f"📊 Win Chance: {p_win * 100:.2f}%\n"
                f"✖️ Multiplier: **{mult_str}**",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    async def _dice_parity(
        self, ctx: DiscoContext, amount: float, token: str, mode: str,
    ) -> None:
        """Dice: parity - bet on odd or even. Roll 1-100, half of outcomes win."""
        if mode not in ("odd", "even"):
            await ctx.reply_error("Parity mode must be `odd` or `even`.")
            return
        p_win = 0.5
        multiplier = 2.0
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            anim_title = f"🎲 {mode.title()} or Even  {self._token_badge(token)}"
            msg = await self._animate(ctx, anim_title, self._roll_anim_frames(), C_NEUTRAL)
            factor = await self._win_factor(ctx)
            win_profit = amount * (multiplier - 1) * factor
            roll = _srng.randint(1, _DICE_ROLL_SIZE)
            roll_parity = "even" if roll % 2 == 0 else "odd"
            won = roll_parity == mode
            delta = win_profit if won else -amount
            color = C_SUCCESS if won else C_ERROR
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "dice", token, amount, delta, new_bal)
        icon = "🟢" if won else "🔴"
        if won:
            title = f"🎲 {mode.title()} Wins!"
            desc = f"Roll **{roll}** is **{roll_parity.upper()}** {icon}"
        else:
            title = f"💸 {mode.title()} Miss"
            desc = f"Roll **{roll}** is **{roll_parity.upper()}** {icon}"
        embed = (
            card(f"{title}  {self._token_badge(token)}", description=desc, color=color)
            .field(
                "🎲 Parity",
                f"🎯 Pick: **{mode.upper()}**\n"
                f"📊 Win Chance: {p_win * 100:.0f}%\n"
                f"✖️ Multiplier: **{multiplier:.2f}x**",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    async def _dice_ladder(
        self, ctx: DiscoContext, amount: float, token: str, count: int,
    ) -> None:
        """Dice: ladder - roll `count` times; each roll must be strictly > previous."""
        if count < _DICE_LADDER_MIN or count > _DICE_LADDER_MAX:
            await ctx.reply_error(
                f"Ladder count must be between **{_DICE_LADDER_MIN}** and **{_DICE_LADDER_MAX}**."
            )
            return
        # P(strict ascending n rolls of d100) = C(100, n) / 100^n.
        # Multiplier = 1/P. House edge applied via win_factor on profit.
        ways = math.comb(_DICE_ROLL_SIZE, count)
        p_win = ways / (_DICE_ROLL_SIZE ** count)
        multiplier = 1.0 / p_win
        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return
            factor = await self._win_factor(ctx)
            rolls: list[int] = []
            won = True
            for _ in range(count):
                r = _srng.randint(1, _DICE_ROLL_SIZE)
                rolls.append(r)
                if len(rolls) > 1 and r <= rolls[-2]:
                    won = False
                    break
            win_profit = amount * (multiplier - 1) * factor
            delta = win_profit if won else -amount
            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "dice", token, amount, delta, new_bal)
        # Progressive rung-by-rung reveal.
        anim_title = f"🪜 Climbing Ladder x{count}  {self._token_badge(token)}"
        placeholders = ["⬜" for _ in range(count)]
        msg = await self._animate(
            ctx, anim_title,
            [f"## {' '.join(placeholders)}\n*climbing {count} rungs...*"],
            C_GOLD,
        )
        revealed: list[str] = []
        prev = 0
        for i, r in enumerate(rolls):
            if i == 0 or r > prev:
                revealed.append(f"🟢 **{r}**")
            else:
                revealed.append(f"🔴 **{r}**")
            shown = revealed + placeholders[len(revealed):]
            desc = (
                f"## {' → '.join(shown)}\n"
                f"*rung {i+1} of {count}  -  each roll must beat the last*"
            )
            await asyncio.sleep(_ANIM_STEP)
            try:
                await msg.edit(embed=card(anim_title, description=desc, color=C_GOLD).build())
            except discord.HTTPException:
                break
            prev = r
        rolls_str = " → ".join(f"**{r}**" for r in rolls)
        if won:
            title = f"🏆 Ladder Cleared!  x{count}"
            color = C_GOLD
            desc = f"All {count} rolls strictly ascending: {rolls_str} ✅"
        else:
            title = f"💸 Ladder Broken at rung {len(rolls)}/{count}"
            color = C_ERROR
            desc = f"Ascending chain broken: {rolls_str}"
        mult_str = f"{multiplier:.2f}x" if multiplier < 1000 else f"{multiplier:,.0f}x"
        embed = (
            card(f"{title}  {self._token_badge(token)}", description=desc, color=color)
            .field(
                "🪜 Ladder",
                f"🎯 Rungs: **{count}**\n"
                f"📊 Win Chance: {p_win * 100:.3f}%\n"
                f"✖️ Multiplier: **{mult_str}**",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    # ── /play group ───────────────────────────────────────────────────────────

    @commands.hybrid_group(name="play", aliases=["gamble"], description="Gambling & games", invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    async def play(self, ctx: DiscoContext) -> None:
        """Gambling & games. Subcommands: coinflip (classic/streak/don/trio/rainbow), dice (classic/over/under/range/exact/odd/even/ladder), slots, roulette, blackjack, mines"""
        if await suggest_subcommand(ctx, self.play):
            return
        try:
            from services.onboarding import maybe_send_intro
            await maybe_send_intro(ctx, "gambling")
        except Exception:
            pass
        embed = (
            card(
                "🎰 Casino  -  Pick Your Game",
                description=(
                    "Place your bets  -  any token accepted (USD, ARC, DSC, SUN...)\n"
                    "─────────────────────────────────────────"
                ),
                color=C_PINK,
            )
            .field(
                "🪙 Coinflip  `cf` `flip`",
                "**Classic** - 50/50 heads or tails\n"
                "**Streak** - N flips in a row (2-10), exponential payouts\n"
                "**Double or Nothing** - keep doubling or cash out\n"
                "**Trio** 🎰 - 3 coins, predict exact H/T pattern (8x)\n"
                "**Rainbow** 🌈 - 5 coins, predict exact count matching your side\n"
                f"> `{ctx.prefix}cf 100` | `{ctx.prefix}cf 100 streak 5` | `{ctx.prefix}cf 100 don`\n"
                f"> `{ctx.prefix}cf 100 trio hht` | `{ctx.prefix}cf 100 rainbow 3`\n"
                f"> `{ctx.prefix}play help coinflip` for full details",
                False,
            )
            .field(
                "🎲 Dice",
                "**Classic** - set your multiplier (1.01x-10000x)\n"
                "**Over/Under** - roll 1-100, bet over or under a target\n"
                "**Range** - roll 1-100, bet it lands in your range\n"
                "**Exact** 🎯 - pick one number for a ~100x jackpot\n"
                "**Odd/Even** - bet the roll's parity (~2x)\n"
                "**Ladder** 🪜 - 2-5 rolls, each strictly greater than the last\n"
                f"> `{ctx.prefix}dice 100 3` | `{ctx.prefix}dice 100 over 65` | `{ctx.prefix}dice 100 range 30 60`\n"
                f"> `{ctx.prefix}dice 100 exact 77` | `{ctx.prefix}dice 100 odd` | `{ctx.prefix}dice 100 ladder 3`\n"
                f"> `{ctx.prefix}play help dice` for full details",
                False,
            )
            .field(
                "🎰 Slots  `sl`",
                "Spin 3 reels: 🍒🍋🍊🍇💎7️⃣\n> Jackpot (3x): **5x**  |  Pair: **0.5x**",
                True,
            )
            .field(
                "🎡 Roulette  `rou`",
                "European roulette (0-36)\n> red/black, odd/even, number, dozen, column",
                True,
            )
            .field(
                "🃏 Blackjack  `bj`",
                "Beat the dealer to 21\n> Natural BJ: **1.5x**  |  Win: **0.95x**",
                True,
            )
            .field(
                "💣 Mines",
                "Minesweeper-style grid\n> Cash out anytime for your multiplier",
                True,
            )
            .field(
                "♞ Chess  `chess` / `chs`",
                f"vs AI or PvP w/ ELO leaderboard\n"
                f"> `{ctx.prefix}chess play [bet]`  ·  "
                f"`{ctx.prefix}chess challenge @user [bet]`\n"
                f"> Wins mint **GAMBIT** on the Gamba Network",
                False,
            )
            .field(
                "\U0001F451 Checkers  `checkers` / `ck`",
                f"vs AI or PvP w/ ELO leaderboard\n"
                f"> `{ctx.prefix}checkers play [bet]`  ·  "
                f"`{ctx.prefix}checkers challenge @user [bet]`\n"
                f"> Wins mint **CROWN** on the Gamba Network",
                False,
            )
            .field(
                "\U0001F3B0 Gamba Network  `gamba`",
                f"Earn-only economy backing every gamba game.\n"
                f"> Stake your themed token (PIP, ACE, VEIN, etc) to drip GBC.\n"
                f"> `{ctx.prefix}gamba info`  ·  `{ctx.prefix}gamba stakes`  ·  `{ctx.prefix}gamba shop`",
                False,
            )
            .field(
                "\U0001f4ca Stats  `gambstats`",
                f"`{ctx.prefix}play stats [game] [daily/weekly/monthly/yearly]`\n"
                f"`{ctx.prefix}play stats group` | `{ctx.prefix}play stats lb`",
                False,
            )
            .footer("House edge: 5%  |  Min bet: {0:,.0f}  |  Use play help <game> for details".format(
                Config.MIN_BET
            ))
            .build()
        )
        await ctx.send(embed=embed)

    # ── per-game help ────────────────────────────────────────────────────────

    @play.command(name="help")
    @guild_only
    @no_bots
    async def play_help(
        self, ctx: DiscoContext, game: str = "",
    ) -> None:
        """Detailed help for each game mode. Usage: play help <game>"""
        game = game.lower()
        alias_map = {"cf": "coinflip", "flip": "coinflip", "bj": "blackjack",
                      "rou": "roulette", "sl": "slots"}
        game = alias_map.get(game, game)
        if game == "coinflip":
            await self._help_coinflip(ctx)
        elif game == "dice":
            await self._help_dice(ctx)
        elif game == "roulette":
            await self._help_roulette(ctx)
        elif game == "blackjack":
            await self._help_blackjack(ctx)
        elif game == "slots":
            await self._help_slots(ctx)
        elif game == "mines":
            await self._help_mines(ctx)
        else:
            # No game specified - show general overview
            await ctx.invoke(self.play)

    async def _help_coinflip(self, ctx: DiscoContext) -> None:
        p = ctx.prefix
        embed = (
            card(
                "🪙 Coinflip  -  Game Guide",
                description="Five ways to flip. All modes accept any token.",
                color=C_PINK,
            )
            .field(
                "🪙 Classic Mode",
                "Standard 50/50 coin flip. Pick heads or tails.\n"
                "**Win:** +95% of bet  |  **Lose:** -100% of bet\n"
                f"> `{p}cf <amount> [heads|tails] [token]`\n"
                f"> `{p}cf 100` | `{p}cf 500 tails` | `{p}cf 100 tails ARC`",
                False,
            )
            .field(
                "🔥 Streak Mode",
                "Predict N consecutive flips (2-10) all landing on your side.\n"
                "Probability halves with each flip  -  payouts grow exponentially.\n"
                f"> `{p}cf <amount> streak <count> [side] [token]`\n"
                f"> `{p}cf 100 streak 3` | `{p}cf 50 streak 5 tails ARC`\n"
                f"> Shorthand: `{p}cf 100 5` (bare number = streak)\n"
                "\n"
                "**Payout table:**\n"
                "> Streak 2: 25.00% chance, **4x** payout\n"
                "> Streak 3: 12.50% chance, **8x** payout\n"
                "> Streak 5: 3.13% chance, **32x** payout\n"
                "> Streak 7: 0.78% chance, **128x** payout\n"
                "> Streak 10: 0.10% chance, **1024x** payout",
                False,
            )
            .field(
                "🪙 Double or Nothing  (don)",
                "Win the opening flip, then choose: **Double** your payout "
                "or **Cash Out** to keep it. Each double is a new 50/50 flip.\n"
                "Bust = lose your entire bet. Max 10 doubles.\n"
                f"> `{p}cf <amount> don [side] [token]`\n"
                f"> `{p}cf 100 don` | `{p}cf 200 don tails ARC`\n"
                "\n"
                "**How it works:**\n"
                "> 1. Bet deducted, opening flip happens automatically\n"
                "> 2. If you lose the opener  -  game over (same as classic)\n"
                "> 3. If you win  -  payout starts at 1.95x your bet\n"
                "> 4. Press **Double** to risk it all for 2x, or **Cash Out**\n"
                "> 5. Timeout (30s) auto-cashes out your current payout",
                False,
            )
            .field(
                "🎰 Trio Mode  (triple-flip pattern)",
                "Flip 3 coins at once. Predict the **exact** 3-char H/T pattern "
                "(e.g. `hht`, `tth`, `hhh`). One of 8 outcomes -> **8x payout**.\n"
                f"> `{p}cf <amount> trio <pattern> [token]`\n"
                f"> `{p}cf 100 trio hht` | `{p}cf 50 trio ttt ARC`\n"
                f"> Shorthand: any 3-char h/t string triggers trio mode\n"
                "\n"
                "**Odds:** 12.50% win chance per pattern, **8x** gross (7.6x profit)",
                False,
            )
            .field(
                "🌈 Rainbow Mode  (5-coin binomial)",
                "Flip 5 coins. Predict **exactly how many** land on your side "
                "(0-5). Payouts scale by how rare your pick is.\n"
                f"> `{p}cf <amount> rainbow <count> [side] [token]`\n"
                f"> `{p}cf 100 rainbow 3` | `{p}cf 50 rainbow 5 tails`\n"
                "\n"
                "**Payout table:**\n"
                "> 0 or 5: 3.13% chance, **32x** payout\n"
                "> 1 or 4: 15.63% chance, **6.4x** payout\n"
                "> 2 or 3: 31.25% chance, **3.2x** payout",
                False,
            )
            .field(
                "💡 Tips",
                "- Args are **order-independent**: `cf 100 ARC tails streak 3` works\n"
                "- Use `h` or `t` as shorthand for heads/tails\n"
                "- Use `all` as amount to bet your full balance",
                False,
            )
            .footer(f"House edge: 5%  |  Min: {fmt_usd(to_human(Config.MIN_BET))}")
            .build()
        )
        await ctx.send(embed=embed)

    async def _help_dice(self, ctx: DiscoContext) -> None:
        p = ctx.prefix
        embed = (
            card(
                "🎲 Dice  -  Game Guide",
                description="Six ways to roll. All modes accept any token.",
                color=C_PINK,
            )
            .field(
                "🎲 Classic Mode  (multiplier)",
                "Set a target multiplier (1.01x-10000x). Higher multiplier = lower "
                "win chance = bigger payout. Roll 1-10000 internally.\n"
                f"> `{p}dice <amount> [multiplier] [token]`\n"
                f"> `{p}dice 100` (default 2x) | `{p}dice 100 5` | `{p}dice 50 10 ARC`\n"
                "\n"
                "**Examples:**\n"
                "> 2x multiplier: 50.00% chance, profit = +0.95x bet\n"
                "> 5x multiplier: 20.00% chance, profit = +3.80x bet\n"
                "> 100x multiplier: 1.00% chance, profit = +94.05x bet",
                False,
            )
            .field(
                "📈 Over/Under Mode",
                "Roll 1-100. Bet that the roll is **over** or **under** a target number.\n"
                "More intuitive  -  you pick the threshold, odds are calculated for you.\n"
                f"> `{p}dice <amount> over <target> [token]`\n"
                f"> `{p}dice <amount> under <target> [token]`\n"
                f"> `{p}dice 100 over 65` | `{p}dice 200 under 30 ARC`\n"
                "\n"
                "**Over:** win if roll > target (target 2-98)\n"
                "**Under:** win if roll < target (target 3-99)\n"
                "\n"
                "**Examples:**\n"
                "> Over 50: 50% chance, 2.00x multiplier\n"
                "> Over 80: 20% chance, 5.00x multiplier\n"
                "> Under 20: 19% chance, 5.26x multiplier\n"
                "> Over 95: 5% chance, 20.00x multiplier",
                False,
            )
            .field(
                "🎯 Range Mode",
                "Roll 1-100. Win if the roll lands **inside** your chosen range [low, high].\n"
                "Narrower range = higher multiplier. Visual bar shows your range and the roll.\n"
                f"> `{p}dice <amount> range <low> <high> [token]`\n"
                f"> `{p}dice 100 range 30 60` | `{p}dice 50 range 45 55 ARC`\n"
                "\n"
                "**Examples:**\n"
                "> Range [1, 50]: 50% chance, 2.00x multiplier\n"
                "> Range [40, 60]: 21% chance, 4.76x multiplier\n"
                "> Range [49, 51]: 3% chance, 33.33x multiplier\n"
                "> Range [50, 50]: 1% chance, 100.00x multiplier",
                False,
            )
            .field(
                "💎 Exact Mode  (pick one number)",
                "Pick a single number 1-100. Win only if the roll matches exactly.\n"
                f"> `{p}dice <amount> exact <target> [token]`\n"
                f"> `{p}dice 100 exact 77` | `{p}dice 50 exact 42 ARC`\n"
                "\n"
                "**Odds:** 1.00% chance, **100x** gross (95x profit)",
                False,
            )
            .field(
                "🟥🟦 Odd / Even Mode",
                "Bet the parity of the roll. Half of outcomes win.\n"
                f"> `{p}dice <amount> odd [token]` | `{p}dice <amount> even [token]`\n"
                f"> `{p}dice 100 odd` | `{p}dice 200 even ARC`\n"
                "\n"
                "**Odds:** 50% chance, **2x** gross (1.95x profit)",
                False,
            )
            .field(
                "🪜 Ladder Mode  (strict ascent)",
                "Roll N dice in sequence (2-5). Each roll must be **strictly greater** "
                "than the last or the ladder breaks. Rolls revealed one rung at a time.\n"
                f"> `{p}dice <amount> ladder <count> [token]`\n"
                f"> `{p}dice 100 ladder 3` | `{p}dice 50 ladder 5 ARC`\n"
                "\n"
                "**Payout table (approx.):**\n"
                "> Ladder 2: 49.5% chance, **2.02x**\n"
                "> Ladder 3: 16.17% chance, **6.18x**\n"
                "> Ladder 4: 3.92% chance, **25.5x**\n"
                "> Ladder 5: 0.75% chance, **132.9x**",
                False,
            )
            .field(
                "💡 Tips",
                "- Args are **order-independent**: `dice 100 ARC over 65` works\n"
                "- If low > high in range mode, they auto-swap\n"
                "- Use `all` as amount to bet your full balance",
                False,
            )
            .footer(f"House edge: 5%  |  Min: {fmt_usd(to_human(Config.MIN_BET))}")
            .build()
        )
        await ctx.send(embed=embed)

    async def _help_roulette(self, ctx: DiscoContext) -> None:
        p = ctx.prefix
        embed = (
            card(
                "🎡 Roulette  -  Game Guide",
                description="European roulette  -  37 pockets (0-36).",
                color=C_PINK,
            )
            .field(
                "Bet Types",
                f"**Red/Black** (1x payout): `{p}rou 100 red` | `{p}rou 100 black`\n"
                f"**Odd/Even** (1x payout): `{p}rou 100 odd` | `{p}rou 100 even`\n"
                f"**Dozen** (2x payout): `{p}rou 100 dozen 1` (1-12, 13-24, 25-36)\n"
                f"**Column** (2x payout): `{p}rou 100 column 1` (cols 1, 2, 3)\n"
                f"**Number** (35x payout): `{p}rou 100 number 17`",
                False,
            )
            .field(
                "Usage",
                f"> `{p}play roulette <amount> [token] <bet_type> [detail]`\n"
                f"> Token is optional: `{p}rou 100 red` | `{p}rou 100 ARC red`",
                False,
            )
            .footer(f"Min: {fmt_usd(to_human(Config.MIN_BET))}")
            .build()
        )
        await ctx.send(embed=embed)

    async def _help_blackjack(self, ctx: DiscoContext) -> None:
        p = ctx.prefix
        embed = (
            card(
                "🃏 Blackjack  -  Game Guide",
                description="Beat the dealer without going over 21.",
                color=C_PINK,
            )
            .field(
                "Rules",
                "- You and the dealer each get 2 cards\n"
                "- Face cards (J/Q/K) = 10, Aces = 11 (or 1 if bust)\n"
                "- **Hit** to draw another card, **Stand** to hold\n"
                "- Dealer hits on 16 or less, stands on 17+\n"
                "- Bust (over 21) = instant loss",
                False,
            )
            .field(
                "Payouts",
                "**Natural Blackjack (21 on deal):** 1.5x payout\n"
                "**Beat the dealer:** 0.95x payout (5% house edge)\n"
                "**Push (tie):** bet refunded\n"
                "**Lose/Bust:** -100% of bet",
                False,
            )
            .field(
                "Usage",
                f"> `{p}bj <amount> [token]`\n"
                f"> `{p}bj 100` | `{p}bj 500 ARC`\n"
                "> 60-second timeout = auto-stand",
                False,
            )
            .footer(f"Min: {fmt_usd(to_human(Config.MIN_BET))}")
            .build()
        )
        await ctx.send(embed=embed)

    async def _help_slots(self, ctx: DiscoContext) -> None:
        p = ctx.prefix
        embed = (
            card(
                "🎰 Slots  -  Game Guide",
                description="Spin 3 reels and match symbols.",
                color=C_PINK,
            )
            .field(
                "Symbols",
                "🍒 🍋 🍊 🍇 💎 7️⃣  -  6 possible symbols per reel",
                False,
            )
            .field(
                "Payouts",
                "**Three of a kind (Jackpot):** +4x bet (5x total return)\n"
                "**Two of a kind (Pair):** +0.5x bet (1.5x total return)\n"
                "**No match:** -100% of bet",
                False,
            )
            .field(
                "Usage",
                f"> `{p}sl <amount> [token]`\n"
                f"> `{p}sl 100` | `{p}sl 500 ARC`",
                False,
            )
            .footer(f"Min: {fmt_usd(to_human(Config.MIN_BET))}")
            .build()
        )
        await ctx.send(embed=embed)

    async def _help_mines(self, ctx: DiscoContext) -> None:
        p = ctx.prefix
        embed = (
            card(
                "💣 Mines  -  Game Guide",
                description="Minesweeper-style grid  -  reveal tiles, avoid bombs.",
                color=C_PINK,
            )
            .field(
                "How to Play",
                "- 24-tile grid with 1-20 hidden bombs (default: 3)\n"
                "- Click tiles to reveal safe squares\n"
                "- Each safe tile increases your multiplier\n"
                "- Hit a bomb = lose your entire bet\n"
                "- Press **Cash Out** anytime to lock in your winnings\n"
                "- Clear all safe tiles = auto-win at max multiplier",
                False,
            )
            .field(
                "Multiplier Formula",
                "Each safe pick: multiplier *= (1/p) * 0.95\n"
                "where p = safe tiles left / total tiles left\n"
                "More bombs = faster multiplier growth = higher risk",
                False,
            )
            .field(
                "Usage",
                f"> `{p}mines <amount> [bombs=3] [token]`\n"
                f"> `{p}mines 100` | `{p}mines 200 5` | `{p}mines 500 10 ARC`\n"
                "> 120-second timeout = auto-cashout (or forfeit if no picks)",
                False,
            )
            .footer(f"House edge: 5% per pick  |  Min: {fmt_usd(to_human(Config.MIN_BET))}")
            .build()
        )
        await ctx.send(embed=embed)

    # ── coinflip ──────────────────────────────────────────────────────────────

    @play.command(name="coinflip", aliases=["cf", "flip"])
    @app_commands.describe(amount="Amount to bet")
    @guild_only
    @no_bots
    @ensure_registered
    async def coinflip(
        self,
        ctx: DiscoContext,
        amount: str,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
    ) -> None:
        """Flip a coin.

        Modes:
          classic (default)  -  pick heads or tails, 50/50
          streak <count>     -  N consecutive flips must all match (2-10)
          don                -  double or nothing: keep flipping or cash out
          trio <pattern>     -  3 coins, predict exact H/T pattern (e.g. hht)
          rainbow <count>    -  5 coins, predict exactly how many match your side

        Usage:
          play coinflip <amount> [side] [token]
          play coinflip <amount> streak <count> [side] [token]
          play coinflip <amount> don [side] [token]
          play coinflip <amount> trio <pattern> [token]
          play coinflip <amount> rainbow <count> [side] [token]
        """
        if not await self._check_submodule(ctx, "coinflip"):
            return
        valid_set = set((await ctx.db.get_all_tokens_for_guild(ctx.guild_id)).keys()) | {"USD"}
        mode, side, count, token, pattern = self._parse_cf_args([p1, p2, p3, p4], valid_set)
        if token not in valid_set:
            await ctx.reply_error(f"Unknown token **{token}**. Valid: {', '.join(sorted(valid_set))}")
            return
        amt = await self._resolve_amount(ctx, amount, token)
        if amt is None:
            return
        if mode == "streak":
            await self._coinflip_streak(ctx, amt, token, side, count)
        elif mode == "don":
            await self._coinflip_don(ctx, amt, token, side)
        elif mode == "trio":
            await self._coinflip_trio(ctx, amt, token, pattern)
        elif mode == "rainbow":
            pick = count if count >= 0 else _CF_RAINBOW_COUNT // 2
            await self._coinflip_rainbow(ctx, amt, token, side, pick)
        else:
            await self._coinflip_classic(ctx, amt, token, side)

    # ── slots ─────────────────────────────────────────────────────────────────

    @play.command(name="slots", aliases=["sl"])
    @app_commands.describe(
        amount="Amount to bet",
        token="Token to bet (default: USD)",
    )
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def slots(
        self,
        ctx: DiscoContext,
        amount: str,
        token: str = "USD",
    ) -> None:
        """Spin the slot machine. Usage: $play slots <amount|all> [token]"""
        if not await self._check_submodule(ctx, "slots"):
            return
        token = token.upper()
        valid_set = set((await ctx.db.get_all_tokens_for_guild(ctx.guild_id)).keys()) | {"USD"}
        if token not in valid_set:
            await ctx.reply_error(f"Unknown token **{token}**. Valid: {', '.join(sorted(valid_set))}")
            return
        amount = await self._resolve_amount(ctx, amount, token)
        if amount is None:
            return

        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return

            reels = [_srng.choice(_SLOT_SYMBOLS) for _ in range(3)]

            if reels[0] == reels[1] == reels[2]:
                gross = amount * 4.0  # net profit; payout = amount + gross = 5× bet
                title = "💎 JACKPOT!"
                outcome_label = "Three of a kind  -  **5× payout**"
                delta = gross
                color = C_GOLD
            elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
                gross = amount * 0.5
                title = "✨ Two of a Kind"
                outcome_label = "Pair  -  **0.5× payout**"
                delta = gross
                color = C_SUCCESS
            else:
                title = "💸 No Match"
                outcome_label = "No match  -  **bet lost**"
                delta = -amount
                color = C_ERROR

            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "slots", token, amount, delta, new_bal)

        embed = (
            card(
                f"🎰 Slots  {self._token_badge(token)}",
                description=f"## {' ┃ '.join(reels)}\n{title}  -  {outcome_label}",
                color=color,
            )
            .field(
                "💰 Bet / Change",
                f"💰 {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if delta >= 0 else '－'}{self._fmt_amount(abs(delta), token)}",
                True,
            )
            .field("🏦 Balance", self._fmt_amount(new_bal, token), True)
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        await ctx.send(embed=embed)

    # ── dice ──────────────────────────────────────────────────────────────────

    @play.command(name="dice")
    @app_commands.describe(amount="Amount to bet")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def dice(
        self,
        ctx: DiscoContext,
        amount: str,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
    ) -> None:
        """Roll dice.

        Modes:
          classic (default)  -  set a multiplier target (1.01x - 10000x)
          over <target>      -  roll 1-100, win if roll > target
          under <target>     -  roll 1-100, win if roll < target
          range <low> <high> -  roll 1-100, win if roll lands in range
          exact <target>     -  pick one number 1-100 for a ~100x jackpot
          odd | even         -  parity bet on the roll (~2x)
          ladder <count>     -  N rolls (2-5) each strictly greater than the last

        Usage:
          play dice <amount> [multiplier] [token]
          play dice <amount> over <target> [token]
          play dice <amount> under <target> [token]
          play dice <amount> range <low> <high> [token]
          play dice <amount> exact <target> [token]
          play dice <amount> odd [token]
          play dice <amount> even [token]
          play dice <amount> ladder <count> [token]
        """
        if not await self._check_submodule(ctx, "dice"):
            return
        valid_set = set((await ctx.db.get_all_tokens_for_guild(ctx.guild_id)).keys()) | {"USD"}
        mode, multiplier, target, target2, token = self._parse_dice_args(
            [p1, p2, p3, p4], valid_set,
        )
        if token not in valid_set:
            await ctx.reply_error(f"Unknown token **{token}**. Valid: {', '.join(sorted(valid_set))}")
            return
        amt = await self._resolve_amount(ctx, amount, token)
        if amt is None:
            return
        if mode in ("over", "under"):
            await self._dice_over_under(ctx, amt, token, mode, target)
        elif mode == "range":
            await self._dice_range(ctx, amt, token, target, target2)
        elif mode == "exact":
            await self._dice_exact(ctx, amt, token, target)
        elif mode in ("odd", "even"):
            await self._dice_parity(ctx, amt, token, mode)
        elif mode == "ladder":
            ladder_count = target if target is not None else _DICE_LADDER_MIN
            await self._dice_ladder(ctx, amt, token, ladder_count)
        else:
            await self._dice_classic(ctx, amt, token, multiplier)

    # ── roulette ──────────────────────────────────────────────────────────────

    @play.command(name="roulette", aliases=["rou"])
    @app_commands.describe(
        amount="Amount to bet",
        token="Token to bet (default: USD)",
        bet_type="red/black/odd/even/number/dozen/column",
        detail="For number: 0-36. For dozen/column: 1/2/3.",
    )
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def roulette(
        self,
        ctx: DiscoContext,
        amount: str,
        token: str = "USD",
        bet_type: str = "red",
        detail: str = "",
    ) -> None:
        """Spin the roulette wheel. Usage: $play roulette <amount|all> [token] <bet_type> [detail]"""
        if not await self._check_submodule(ctx, "roulette"):
            return
        token_upper = token.upper()
        valid_set = set((await ctx.db.get_all_tokens_for_guild(ctx.guild_id)).keys()) | {"USD"}

        if token_upper not in valid_set:
            detail = bet_type
            bet_type = token
            token_upper = "USD"
        token = token_upper

        amount = await self._resolve_amount(ctx, amount, token)
        if amount is None:
            return

        try:
            cover, payout_mult = _parse_roulette_bet(bet_type.lower(), detail)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have a game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return

            spin = _srng.randint(0, 36)
            won = spin in cover

            delta = amount * payout_mult if won else -amount
            color = C_SUCCESS if won else C_ERROR

            new_bal, delta = await self._apply_delta(ctx, token, delta)
            tx_hash = await self._finish_game(ctx, "roulette", token, amount, delta, new_bal)

        spin_color_str = _spin_color(spin)
        bet_display = f"{bet_type} {detail}".strip()
        if won:
            payout_note = f" ({payout_mult:.0f}×)" if payout_mult > 1 else ""
            title = f"🎡 Winner! {spin_color_str}{payout_note}"
            desc = f"The ball landed on **{spin}**  -  your bet **{bet_display}** hits!"
        else:
            title = f"🎡 No Luck  -  {spin_color_str}"
            desc = f"The ball landed on **{spin}**  -  your bet **{bet_display}** loses."
        _ROULETTE_FILENAME = "roulette_wheel.png"
        embed = (
            card(
                f"{title}  {self._token_badge(token)}",
                description=desc,
                color=color,
            )
            .field(
                "🎡 Spin",
                f"🎡 **{spin}**  {spin_color_str}\n"
                f"🎯 Bet: {bet_display.capitalize()}\n"
                f"📊 Payout: {payout_mult:.0f}× on win",
                True,
            )
            .field(
                "💰 Result",
                f"💰 Bet: {self._fmt_amount(amount, token)}\n"
                f"📈 {'＋' if won else '－'}{self._fmt_amount(abs(delta), token)}\n"
                f"🏦 {self._fmt_amount(new_bal, token)}",
                True,
            )
            .image(f"attachment://{_ROULETTE_FILENAME}")
            .build()
        )
        bal_label = "Wallet" if token == "USD" else f"{token} Balance"
        self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
        roulette_png = render_roulette_png(
            spin=spin, bet_label=bet_display.strip() or "—",
            won=won, payout_mult=payout_mult,
        )
        await ctx.send(
            embed=embed,
            file=discord.File(
                io.BytesIO(roulette_png), filename=_ROULETTE_FILENAME,
            ),
        )

    # ── blackjack ─────────────────────────────────────────────────────────────

    @play.command(name="blackjack", aliases=["bj"])
    @app_commands.describe(
        amount="Amount to bet",
        token="Token to bet (default: USD)",
    )
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def blackjack(
        self,
        ctx: DiscoContext,
        amount: str,
        token: str = "USD",
    ) -> None:
        """Play blackjack against the dealer. Usage: $play blackjack <amount|all> [token]"""
        if not await self._check_submodule(ctx, "blackjack"):
            return
        token = token.upper()
        valid_set = set((await ctx.db.get_all_tokens_for_guild(ctx.guild_id)).keys()) | {"USD"}
        if token not in valid_set:
            await ctx.reply_error(f"Unknown token **{token}**.")
            return
        amount = await self._resolve_amount(ctx, amount, token)
        if amount is None:
            return

        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have an active game in progress. Finish it first.")
            return
        async with lock:
            err = await self._validate_bet(ctx, token, amount)
            if err:
                await ctx.reply_error(err)
                return

            factor = await self._win_factor(ctx)

            # Deduct bet upfront  -  funds are reserved before any UI wait begins.
            # (no save check on upfront deduction  -  saves apply at resolution)
            amount_raw = to_raw(amount)
            _raw_cap = self._balance_prefetch_raw.pop((ctx.author.id, ctx.guild_id, token), None)
            if _raw_cap is not None:
                amount_raw = min(amount_raw, _raw_cap)
            if token == "USD":
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -amount_raw)
            else:
                await self._holding_update_raw(ctx, ctx.author.id, token, -amount_raw)

            player: list[int] = [_srng.randint(1, 13), _srng.randint(1, 13)]
            dealer: list[int] = [_srng.randint(1, 13), _srng.randint(1, 13)]

            _BJ_FILENAME = "blackjack_table.png"

            def _fmt_hand(cards: list[int]) -> str:
                return "  ".join(f"`{_card_name(c)}`" for c in cards)

            def build_embed(reveal: bool = False, result_str: str = "", color: int = C_PINK) -> discord.Embed:
                pv = _hand_value(player)
                p_str = _fmt_hand(player) + f"  →  **{pv}**"
                if reveal:
                    dv = _hand_value(dealer)
                    d_str = _fmt_hand(dealer) + f"  →  **{dv}**"
                else:
                    d_str = f"`{_card_name(dealer[0])}`  `?`  →  **?**"
                if result_str:
                    title = f"🃏 Blackjack  {self._token_badge(token)}"
                    desc = result_str
                else:
                    title = f"🃏 Blackjack  {self._token_badge(token)}"
                    desc = "Hit or Stand?"
                _b = (
                    card(title, description=desc, color=color)
                    .field("🙋 Your Hand", p_str, True)
                    .field("🤖 Dealer",    d_str, True)
                    .image(f"attachment://{_BJ_FILENAME}")
                )
                _b.footer(f"Bet: {self._fmt_amount(amount, token)}")
                if not reveal:
                    _b.field("", f"Expires {fmt_ts(int(time.time() + 60))}", False)
                return _b.build()

            def build_file(reveal: bool = False, result: str | None = None) -> discord.File:
                """Render the blackjack table to a PNG attachment.

                ``result`` is one of ``win`` / ``lose`` / ``bust`` /
                ``push`` / ``blackjack`` and is only honoured on reveal.
                """
                pv = _hand_value(player)
                dv = _hand_value(dealer) if reveal else None
                png = render_blackjack_png(
                    player_cards=list(player),
                    dealer_cards=list(dealer),
                    player_value=pv,
                    dealer_value=dv,
                    reveal=reveal,
                    result=result if reveal else None,
                )
                return discord.File(io.BytesIO(png), filename=_BJ_FILENAME)

            pval = _hand_value(player)

            # Natural blackjack on deal
            if pval == 21:
                dval = _hand_value(dealer)
                if dval == 21:
                    delta, result_str, color = 0.0, "🤝 Push  -  both have Blackjack, bet refunded.", C_NEUTRAL
                    bj_result = "push"
                else:
                    delta = self._apply_hall_bonus(ctx, amount * 1.5)
                    result_str = f"🎉 Blackjack! Won **{self._fmt_amount(delta, token)}** (1.5x payout)"
                    color = C_GOLD
                    bj_result = "blackjack"
                net_return = amount + delta
                if net_return > 0:
                    net_return_raw = to_raw(net_return)
                    if token == "USD":
                        new_bal_raw = await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, net_return_raw)
                    else:
                        new_bal_raw = await self._holding_update_raw(ctx, ctx.author.id, token, net_return_raw)
                    new_bal = to_human(int(new_bal_raw))
                else:
                    new_bal = await self._get_balance(ctx, token)
                tx_hash = await self._finish_game(ctx, "blackjack", token, amount, delta, new_bal)
                embed = build_embed(reveal=True, result_str=result_str, color=color)
                bal_label = "Wallet" if token == "USD" else f"{token} Balance"
                self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
                await ctx.send(
                    embed=embed,
                    file=build_file(reveal=True, result=bj_result),
                )
                return

            msg = await ctx.reply(
                embed=build_embed(),
                file=build_file(),
                view=BlackjackView(ctx.author.id),
                mention_author=False,
            )

            delta = 0.0
            result_str = ""
            color = C_PINK
            bj_result: str | None = None

            while True:
                view = BlackjackView(ctx.author.id)
                try:
                    await msg.edit(
                        embed=build_embed(), view=view,
                        attachments=[build_file()],
                    )
                except discord.HTTPException:
                    break
                timed_out = await view.wait()
                action = view.action if not timed_out else "stand"

                if action == "hit":
                    player.append(_srng.randint(1, 13))
                    pval = _hand_value(player)
                    if pval > 21:
                        delta = -amount
                        result_str = f"💥 Bust! Hand is **{pval}**  -  lost **{self._fmt_amount(amount, token)}**"
                        color = C_ERROR
                        bj_result = "bust"
                        break
                    if pval == 21:
                        action = "stand"

                if action == "stand":
                    while _hand_value(dealer) <= 16:
                        dealer.append(_srng.randint(1, 13))
                    pval = _hand_value(player)
                    dval = _hand_value(dealer)
                    if dval > 21 or pval > dval:
                        delta = amount * factor
                        result_str = f"🎉 You Win! **{pval}** beats dealer's **{dval if dval <= 21 else 'bust'}**  -  won **{self._fmt_amount(delta, token)}**"
                        color = C_SUCCESS
                        bj_result = "win"
                    elif pval == dval:
                        delta = 0.0
                        result_str = f"🤝 Push  -  tied at **{pval}**, bet refunded."
                        color = C_NEUTRAL
                        bj_result = "push"
                    else:
                        delta = -amount
                        result_str = f"💸 Dealer Wins  -  **{dval}** beats your **{pval}**  -  lost **{self._fmt_amount(amount, token)}**"
                        color = C_ERROR
                        bj_result = "lose"
                    break

            delta = self._apply_hall_bonus(ctx, delta)
            net_return = amount + delta
            if net_return > 0:
                net_return_raw = to_raw(net_return)
                if token == "USD":
                    new_bal_raw = await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, net_return_raw)
                else:
                    new_bal_raw = await self._holding_update_raw(ctx, ctx.author.id, token, net_return_raw)
                new_bal = to_human(int(new_bal_raw))
            else:
                new_bal = await self._get_balance(ctx, token)
            tx_hash = await self._finish_game(ctx, "blackjack", token, amount, delta, new_bal)
            embed = build_embed(reveal=True, result_str=result_str, color=color)
            bal_label = "Wallet" if token == "USD" else f"{token} Balance"
            self._set_tx(embed, ctx, tx_hash, footer_extra=self._game_footer(ctx, bal_label, new_bal, token))
            try:
                await msg.edit(
                    embed=embed, view=None,
                    attachments=[build_file(reveal=True, result=bj_result)],
                )
            except discord.HTTPException:
                pass

    # ── mines ─────────────────────────────────────────────────────────────────

    @play.command(name="mines")
    @app_commands.describe(
        amount="Amount to bet",
        bombs="Number of mines, 1 - 20 (default: 3)",
        token="Token to bet (default: USD)",
    )
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def mines(
        self,
        ctx: DiscoContext,
        amount: str,
        bombs: int = _MINES_DEFAULT,
        token: str = "USD",
    ) -> None:
        """Minesweeper. Usage: $play mines <amount|all> [bombs=3] [token]"""
        token = token.upper()
        valid_set = set((await ctx.db.get_all_tokens_for_guild(ctx.guild_id)).keys()) | {"USD"}
        if token not in valid_set:
            await ctx.reply_error(f"Unknown token **{token}**.")
            return

        amount_f = await self._resolve_amount(ctx, amount, token)
        if amount_f is None:
            return

        if not (_MINES_MIN_BOMBS <= bombs <= _MINES_MAX_BOMBS):
            await ctx.reply_error(
                f"Bombs must be between **{_MINES_MIN_BOMBS}** and **{_MINES_MAX_BOMBS}**."
            )
            return
        if bombs >= _MINES_TOTAL:
            await ctx.reply_error("Too many bombs  -  at least one safe tile must exist.")
            return

        lock_key = (ctx.author.id, ctx.guild_id, ctx.command.qualified_name)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You already have an active game in progress. Finish it first.")
            return

        async with lock:
            err = await self._validate_bet(ctx, token, amount_f)
            if err:
                await ctx.reply_error(err)
                return

            # Deduct bet upfront  -  funds reserved before any UI wait.
            amount_f_raw = to_raw(amount_f)
            _raw_cap = self._balance_prefetch_raw.pop((ctx.author.id, ctx.guild_id, token), None)
            if _raw_cap is not None:
                amount_f_raw = min(amount_f_raw, _raw_cap)
            if token == "USD":
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -amount_f_raw)
            else:
                await self._holding_update_raw(ctx, ctx.author.id, token, -amount_f_raw)
            # Persist the in-flight bet so a SIGKILL'd process can be recovered
            # on the next boot (see core/framework/shutdown.recover_orphaned_sessions).
            session_id = await start_game_session(
                ctx.db,
                guild_id=ctx.guild_id,
                user_id=ctx.author.id,
                game_type="mines",
                bet_amount_raw=int(amount_f_raw),
                token=token,
            )
            game       = MinesGame(ctx.author.id, amount_f, token, bombs)
            done_event = asyncio.Event()
            view       = MinesView(game=game, cog=self, done_event=done_event)
            register_active_view(view)

            msg = await ctx.reply(
                embed=view._build_embed_initial(),
                view=view, mention_author=False,
            )
            game.message = msg

            try:
                # Yield to event loop  -  button callbacks run as separate Tasks.
                await done_event.wait()
            finally:
                unregister_active_view(view)
                await complete_game_session(ctx.db, session_id)

            # ── Game over: resolve balance ─────────────────────────────────────
            if game.payout > 0:
                game.delta = self._apply_hall_bonus(ctx, game.delta)
                game.payout = amount_f + game.delta
                payout_raw = to_raw(game.payout)
                if token == "USD":
                    new_bal_raw = await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, payout_raw)
                else:
                    new_bal_raw = await self._holding_update_raw(ctx, ctx.author.id, token, payout_raw)
                new_bal = to_human(int(new_bal_raw))
            elif game.delta < 0:
                new_bal = await self._get_balance(ctx, token)
            else:
                new_bal = await self._get_balance(ctx, token)

            tx_hash = await self._finish_game(
                ctx, "mines", token, amount_f, game.delta, new_bal
            )

            # Build final embed for tx-footer edit.
            result_embed_map = {
                "bomb":             view._build_embed_bomb,
                "cashout":          view._build_embed_cashout,
                "autowin":          view._build_embed_autowin,
                "timeout_cashout":  view._build_embed_timeout,
                "timeout_forfeit":  view._build_embed_timeout,
                "shutdown_cashout": view._build_embed_timeout,
                "shutdown_refund":  view._build_embed_timeout,
            }
            build_fn = result_embed_map.get(game.result, view._build_embed_timeout)
            final_embed = build_fn()

            bal_label = "Wallet" if token == "USD" else f"{token} Balance"
            self._set_tx(
                final_embed, ctx, tx_hash,
                footer_extra=self._game_footer(ctx, bal_label, new_bal, token),
            )
            try:
                await msg.edit(embed=final_embed, view=None)
            except discord.HTTPException:
                pass

    # ── Hidden prefix-only aliases for backward compatibility ─────────────────

    @commands.command(name="cf", hidden=True, aliases=["flip"])
    @guild_only
    @no_bots
    @ensure_registered
    async def _cf_alias(self, ctx: DiscoContext, amount: str, p1: str = "", p2: str = "", p3: str = "", p4: str = "") -> None:
        """Shortcode for .play coinflip. Usage: .cf <amount> [side] [streak N|don|trio <pat>|rainbow N] [token]"""
        await ctx.invoke(self.coinflip, amount=amount, p1=p1, p2=p2, p3=p3, p4=p4)

    @commands.command(name="sl", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def _sl_alias(self, ctx: DiscoContext, amount: str, token: str = "USD") -> None:
        """Shortcode for .play slots. Usage: .sl <amount> [token]"""
        await ctx.invoke(self.slots, amount=amount, token=token)

    @commands.command(name="dice", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def _dice_alias(self, ctx: DiscoContext, amount: str, p1: str = "", p2: str = "", p3: str = "", p4: str = "") -> None:
        """Shortcode for .play dice. Usage: .dice <amount> [multiplier|over N|under N|range L H|exact N|odd|even|ladder N] [token]"""
        await ctx.invoke(self.dice, amount=amount, p1=p1, p2=p2, p3=p3, p4=p4)

    @commands.command(name="rou", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def _rou_alias(self, ctx: DiscoContext, amount: str, bet_type: str = "red", token: str = "USD") -> None:
        """Shortcode for .play roulette. Usage: .rou <amount> [bet_type] [token]"""
        await ctx.invoke(self.roulette, amount=amount, token=token, bet_type=bet_type)

    @commands.command(name="bj", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def _bj_alias(self, ctx: DiscoContext, amount: str, token: str = "USD") -> None:
        """Shortcode for .play blackjack. Usage: .bj <amount> [token]"""
        await ctx.invoke(self.blackjack, amount=amount, token=token)

    @commands.command(name="mines", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(1)
    async def _mines_alias(
        self,
        ctx: DiscoContext,
        amount: str,
        bombs: int = _MINES_DEFAULT,
        token: str = "USD",
    ) -> None:
        """Shortcode for .play mines. Usage: .mines <amount> [bombs] [token]"""
        await ctx.invoke(self.mines, amount=amount, bombs=bombs, token=token)

    # ── Gambling stats ─────────────────────────────────────────────────────────

    # ── Time-period helpers for stats filtering ────────────────────────────
    _PERIOD_MAP: dict[str, int] = {
        "daily": 1, "day": 1, "today": 1, "1d": 1,
        "weekly": 7, "week": 7, "7d": 7,
        "monthly": 30, "month": 30, "30d": 30,
        "yearly": 365, "year": 365, "365d": 365,
    }
    _VALID_GAMES: set[str] = {"coinflip", "dice", "slots", "roulette", "blackjack", "mines"}

    @staticmethod
    def _parse_stats_args(raw: str) -> tuple[str | None, str | None, str | None]:
        """Parse the free-text args string for play stats.

        Returns (mode, game_type, period).
        mode is "group" or "lb" if specified, else None (normal user stats).
        """
        game_type = None
        period = None
        mode = None
        alias_map = {"cf": "coinflip", "flip": "coinflip", "sl": "slots", "bj": "blackjack", "rou": "roulette"}
        for a in raw.split():
            low = a.lower().strip()
            if not low:
                continue
            if low in Play._PERIOD_MAP:
                period = low
            elif low in ("group", "grp", "team"):
                mode = "group"
            elif low in ("lb", "leaderboard", "top", "rank"):
                mode = "lb"
            elif low in Play._VALID_GAMES or low in alias_map:
                game_type = alias_map.get(low, low)
            # else: ignore (member mentions, etc.)
        return mode, game_type, period

    @play.command(name="stats", aliases=["gambstats", "history"])
    @guild_only
    @no_bots
    @ensure_registered
    async def play_stats(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Show gambling statistics per game.

        Usage:
          play stats [member] [game] [daily|weekly|monthly|yearly]
          play stats group [game] [period]   -- group aggregate stats
          play stats lb [game] [period]      -- gambling leaderboard

        Examples:
          play stats             -- overall stats
          play stats daily       -- today's stats
          play stats dice        -- all-time dice stats
          play stats dice weekly -- dice stats for the last 7 days
          play stats group       -- your group's combined stats
          play stats lb monthly  -- top gamblers this month
        """
        from datetime import datetime, timedelta, timezone
        import re

        # Try to resolve a member mention or ID from the args
        target = ctx.author
        mention_match = re.search(r"<@!?(\d+)>", args)
        if mention_match:
            uid = int(mention_match.group(1))
            m = ctx.guild.get_member(uid)
            if m:
                target = m
            args = args[:mention_match.start()] + args[mention_match.end():]
        else:
            # Check for a bare user ID or name at the start
            parts = args.split()
            if parts and parts[0].isdigit() and len(parts[0]) > 10:
                m = ctx.guild.get_member(int(parts[0]))
                if m:
                    target = m
                    args = " ".join(parts[1:])

        mode, game_filter, period = self._parse_stats_args(args)

        since: datetime | None = None
        if period:
            days = self._PERIOD_MAP[period]
            since = datetime.now(timezone.utc) - timedelta(days=days)

        if mode == "group":
            await self._play_stats_group(ctx, game_filter, period, since)
            return
        if mode == "lb":
            await self._play_stats_lb(ctx, game_filter, period, since)
            return

        # ── Normal user stats ──
        await self._play_stats_user(ctx, target, game_filter, period, since)

    async def _play_stats_user(
        self, ctx: DiscoContext, target: discord.Member,
        game_filter: str | None, period: str | None, since,
    ) -> None:
        """Display individual user gambling stats."""
        stats = await ctx.db.get_gambling_stats(
            target.id, ctx.guild_id, since=since, game_type=game_filter,
        )

        title_parts = ["\U0001f3b0 Gambling Stats"]
        if game_filter:
            title_parts.append(f"({game_filter.title()})")
        if period:
            title_parts.append(f"[{period.title()}]")
        title_parts.append(f" -  {target.display_name}")

        _b = card(
            " ".join(title_parts),
            color=C_PINK,
        ).author(target.display_name, icon_url=target.display_avatar.url)

        if not stats:
            _b.description("No gambling history found for this filter.")
            await ctx.reply(embed=_b.build(), mention_author=False)
            return

        total_wagered = to_human(sum(s.get("total_wagered") or 0 for s in stats))
        total_pnl     = to_human(sum(s.get("net_pnl") or 0 for s in stats))
        total_games   = sum(s.get("total_games") or 0 for s in stats)
        total_wins    = sum(s.get("wins") or 0 for s in stats)
        total_losses  = total_games - total_wins
        overall_wr    = total_wins / total_games if total_games > 0 else 0.0

        pnl_sign = "+" if total_pnl >= 0 else ""
        _b.field(
            "\U0001f4ca Overall",
            f"**{total_games:,}** games  ·  **{total_wins}W / {total_losses}L**  ·  WR **{overall_wr:.1%}**\n"
            f"Wagered **${total_wagered:,.2f}**  ·  Net **{pnl_sign}${total_pnl:,.2f}**",
            False,
        )

        # Streaks -- hide entirely when there are none (prevents the "0 nones"
        # placeholder text when a user has no recent gambling history).
        streaks = await ctx.db.get_gambling_streaks(
            target.id, ctx.guild_id, since=since,
            game_type=game_filter.upper() if game_filter else None,
        )
        cur = streaks["current_streak"]
        cur_type = streaks["current_type"]
        best_w = streaks["best_win_streak"]
        best_l = streaks["best_loss_streak"]
        if cur > 0 or best_w > 0 or best_l > 0:
            streak_icon = (
                "\U0001f525" if cur_type == "win" and cur >= 3
                else ("\U0001f9ca" if cur_type == "loss" and cur >= 3
                      else "\u27a1\ufe0f")
            )
            cur_line = (
                f"Current: {streak_icon} **{cur}** {cur_type}{'s' if cur != 1 else ''}"
                if cur > 0 and cur_type in ("win", "loss")
                else "Current: none"
            )
            _b.field(
                "Streaks",
                f"{cur_line}\n"
                f"Best win streak: **{best_w}**  ·  Best loss streak: **{best_l}**",
                False,
            )

        _GAME_EMOJI = {
            "COINFLIP": "\U0001ffa9", "SLOTS": "\U0001f3b0", "DICE": "\U0001f3b2",
            "ROULETTE": "\U0001f3a1", "BLACKJACK": "\U0001f0cf", "MINES": "\U0001f4a3",
        }
        for s in stats:
            game    = s.get("game", "?")
            emoji   = _GAME_EMOJI.get(game.upper(), "\U0001f3ae")
            games   = s.get("total_games") or 0
            wins    = s.get("wins") or 0
            losses  = games - wins
            wr      = s.get("win_rate", 0)
            pnl     = to_human(s.get("net_pnl") or 0)
            wagered = to_human(s.get("total_wagered") or 0)
            pnl_str = f"{'+' if pnl >= 0 else ''}${pnl:,.2f}"
            _b.field(
                f"{emoji} {game.title()}",
                f"**{games}** games  ·  **{wins}W / {losses}L**  ·  WR **{wr:.1%}**\n"
                f"Wagered **${wagered:,.2f}**  ·  Net **{pnl_str}**",
                True,
            )

        # Recent games
        recent = await ctx.db.get_gambling_history(
            target.id, ctx.guild_id, limit=5,
            game_type=game_filter.upper() if game_filter else None,
            since=since,
        )
        if recent:
            import time as _time
            now = _time.time()
            _RECENT_EMOJI = {
                "COINFLIP": "\U0001ffa9", "SLOTS": "\U0001f3b0", "DICE": "\U0001f3b2",
                "ROULETTE": "\U0001f3a1", "BLACKJACK": "\U0001f0cf", "MINES": "\U0001f4a3",
            }
            recent_lines = []
            for g in recent:
                gtype = g.get("game_type", "?")
                emj = _RECENT_EMOJI.get(gtype.upper(), "\U0001f3ae")
                profit = g.h("profit")
                bet = g.h("bet_amount")
                mult = float(g.get("multiplier", 0))
                _pa = g["played_at"]
                _pa_ts = _pa.timestamp() if hasattr(_pa, "timestamp") else float(_pa or 0)
                age = FormatKit.time_ago(now - _pa_ts)
                sign = "+" if profit >= 0 else ""
                result = f"**{sign}${profit:,.2f}**" if profit != 0 else "Push"
                recent_lines.append(f"{emj} {gtype.title()} -- ${bet:,.2f} bet -- {result} ({mult:.2f}x) -- {age}")
            _b.field("\U0001f552 Recent Games", "\n".join(recent_lines), False)

        await ctx.reply(embed=_b.build(), mention_author=False)

    # ── Group stats ──────────────────────────────────────────────────────────

    async def _play_stats_group(
        self, ctx: DiscoContext,
        game_filter: str | None, period: str | None, since,
    ) -> None:
        """Display combined gambling stats for the caller's group."""
        grp = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not grp:
            await ctx.reply_error("You are not in a group. Join one with `group join`.")
            return

        members = await ctx.db.get_group_members(ctx.guild_id, grp["group_id"])
        member_ids = [m["user_id"] for m in members]
        if not member_ids:
            await ctx.reply_error("Your group has no members.")
            return

        stats = await ctx.db.get_group_gambling_stats(
            ctx.guild_id, member_ids, since=since, game_type=game_filter,
        )

        title_parts = ["\U0001f3b0 Group Gambling Stats"]
        if game_filter:
            title_parts.append(f"({game_filter.title()})")
        if period:
            title_parts.append(f"[{period.title()}]")
        title_parts.append(f" -  {grp['name']}")

        _b = card(" ".join(title_parts), color=C_PINK)
        _b.description(f"**{len(member_ids)}** members")

        if not stats:
            _b.description("No gambling history found for this group/filter.")
            await ctx.reply(embed=_b.build(), mention_author=False)
            return

        total_wagered = to_human(sum(s.get("total_wagered") or 0 for s in stats))
        total_pnl     = to_human(sum(s.get("net_pnl") or 0 for s in stats))
        total_games   = sum(s.get("total_games") or 0 for s in stats)
        total_wins    = sum(s.get("wins") or 0 for s in stats)
        overall_wr    = total_wins / total_games if total_games > 0 else 0.0

        pnl_sign = "+" if total_pnl >= 0 else ""
        _b.field("\U0001f4ca Overall",
                 f"Games: **{total_games:,}** | W/L: **{total_wins}/{total_games-total_wins}** | "
                 f"Win Rate: **{overall_wr:.1%}**\n"
                 f"Wagered: **${total_wagered:,.2f}** | Net P&L: **{pnl_sign}${total_pnl:,.2f}**",
                 False)

        _GAME_EMOJI = {
            "COINFLIP": "\U0001ffa9", "SLOTS": "\U0001f3b0", "DICE": "\U0001f3b2",
            "ROULETTE": "\U0001f3a1", "BLACKJACK": "\U0001f0cf", "MINES": "\U0001f4a3",
        }
        for s in stats:
            game   = s.get("game", "?")
            emoji  = _GAME_EMOJI.get(game.upper(), "\U0001f3ae")
            games  = s.get("total_games") or 0
            wins   = s.get("wins") or 0
            players = s.get("unique_players") or 0
            wr     = s.get("win_rate", 0)
            pnl    = to_human(s.get("net_pnl") or 0)
            wagered= to_human(s.get("total_wagered") or 0)
            bwin   = to_human(s.get("biggest_win") or 0)
            bloss  = to_human(s.get("biggest_loss") or 0)
            pnl_str = f"{'+'if pnl>=0 else ''}${pnl:,.2f}"
            lines = [
                f"Players: **{players}** | Games: **{games}** | WR: **{wr:.1%}**",
                f"Wagered: **${wagered:,.2f}** | Net: **{pnl_str}**",
            ]
            if bwin > 0:
                lines.append(f"Best Win: **+${bwin:,.2f}** | Worst Loss: **${bloss:,.2f}**")
            _b.field(f"{emoji} {game.title()}", "\n".join(lines), True)

        # Per-member leaderboard within group
        lb_rows = await ctx.db.get_gambling_leaderboard(
            ctx.guild_id, limit=10, since=since,
            game_type=game_filter.upper() if game_filter else None,
            user_ids=member_ids,
        )
        if lb_rows:
            lb_lines = []
            medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
            for i, row in enumerate(lb_rows):
                pnl = row.h("net_pnl")
                games = int(row.get("total_games", 0))
                sign = "+" if pnl >= 0 else ""
                m = ctx.guild.get_member(row["user_id"])
                name = m.display_name if m else f"User {row['user_id']}"
                prefix = medals[i] if i < 3 else f"`{i+1}.`"
                lb_lines.append(f"{prefix} **{name}**  -  {sign}${pnl:,.2f} ({games:,} games)")
            _b.field("\U0001f3c6 Member Rankings", "\n".join(lb_lines), False)

        await ctx.reply(embed=_b.build(), mention_author=False)

    # ── Gambling leaderboard ─────────────────────────────────────────────────

    async def _play_stats_lb(
        self, ctx: DiscoContext,
        game_filter: str | None, period: str | None, since,
    ) -> None:
        """Display a gambling leaderboard with optional filters."""
        from core.framework.ui import send_paginated

        rows = await ctx.db.get_gambling_leaderboard(
            ctx.guild_id, limit=200, since=since,
            game_type=game_filter.upper() if game_filter else None,
        )
        if not rows:
            await ctx.reply_error("No gambling activity found for this filter.")
            return

        # Fetch member names
        missing = [r["user_id"] for r in rows if ctx.guild.get_member(r["user_id"]) is None]
        if missing:
            try:
                await ctx.guild.query_members(user_ids=missing[:100], cache=True)
            except Exception:
                pass

        title_parts = ["\U0001f3b0 Gambling Leaderboard"]
        if game_filter:
            title_parts.append(f"({game_filter.title()})")
        if period:
            title_parts.append(f"[{period.title()}]")
        title_str = " ".join(title_parts)

        caller_id = ctx.author.id
        all_ids = [r["user_id"] for r in rows]
        caller_rank = None
        for i, uid in enumerate(all_ids):
            if uid == caller_id:
                caller_rank = i
                break

        medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
        rank_bars = ["\u2588" * 20, "\u2588" * 18 + "\u2591" * 2, "\u2588" * 16 + "\u2591" * 4]
        rank_titles = ["Champion", "Runner-up", "Third Place"]
        display_rows = rows[:50]
        per_page = 10
        pages = []

        for chunk_start in range(0, len(display_rows), per_page):
            chunk = display_rows[chunk_start:chunk_start + per_page]
            lines = []
            for i, row in enumerate(chunk):
                rank = chunk_start + i
                pnl = row.h("net_pnl")
                games = int(row.get("total_games", 0))
                wins = int(row.get("wins", 0))
                wagered = row.h("total_wagered")
                wr = wins / games if games > 0 else 0.0
                sign = "+" if pnl >= 0 else ""
                m = ctx.guild.get_member(row["user_id"])
                name = m.display_name if m else f"User {row['user_id']}"
                you = " \u25c4 **you**" if row["user_id"] == caller_id else ""
                val_str = f"{sign}${pnl:,.2f} ({games:,} games, {wr:.0%} WR, ${wagered:,.0f} wagered)"

                if rank < 3 and chunk_start == 0:
                    line = f"{medals[rank]} **{name}**  -  {val_str}{you}\n\u2003`{rank_bars[rank]}` *{rank_titles[rank]}*"
                elif rank < 3:
                    line = f"{medals[rank]} **{name}**  -  {val_str}{you}"
                else:
                    line = f"`{rank + 1}.` **{name}**  -  {val_str}{you}"
                lines.append(line)

            footer_parts = ["Ranked by net gambling P&L"]
            if caller_rank is not None:
                footer_parts.append(f"\U0001f4cd Your rank: #{caller_rank + 1}")
            else:
                footer_parts.append("\U0001f4cd You are not ranked yet")

            embed = card(title_str, color=C_PINK).description("\n".join(lines)).footer("\n".join(footer_parts)).build()
            pages.append(embed)

        await send_paginated(ctx, pages)

    @commands.command(name="gambstats", aliases=["gambstat", "gstats"], hidden=True)
    async def _alias_gambstats(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Shortcode for .play stats."""
        await ctx.invoke(self.play_stats, args=args)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Play(bot))
