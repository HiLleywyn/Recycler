"""
cogs/overview.py - /games and /market slash command groups.

Informational top-level slash commands that surface available gameplay systems
as rich embeds.  No gameplay happens here - these are discovery/navigation aids.

/games   - casino games, Eat the Rich, and rugpull overview
/market  - crypto prices, trading, DeFi (staking/pools), NFTs, prediction market
,start   - button-driven game-launcher hub (root -> per-game submenu)
"""
from __future__ import annotations

import copy
import json
import logging

import discord
from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only, no_bots, ensure_registered
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_GOLD, C_PINK, C_PURPLE, C_TEAL, C_AMBER, C_INFO,
    C_NEUTRAL, C_CRIMSON, FormatKit, fmt_rel, fmt_usd,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Starter pack (claimed via ,start)
# ─────────────────────────────────────────────────────────────────────────
# One-time grant to give brand-new players enough capital + consumables to
# touch every game surface without first having to claim a faucet drop.
# Claim is gated on users.starter_pack_claimed_at IS NULL (migration 0168).
STARTER_PACK_USD: float            = 1_000.0
STARTER_PACK_BAIT: dict[str, int]  = {"worm": 25, "minnow": 10}
STARTER_PACK_SEEDS: dict[str, int] = {"wheat": 10, "carrot": 5}


class Overview(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # =========================================================================
    # /games  -  gambling, Eat the Rich, rugpull overview
    # =========================================================================

    @commands.hybrid_group(name="games", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def games(self, ctx: DiscoContext) -> None:
        """Games & gambling overview - casino, Eat the Rich, and rugpull."""
        embed = (
            card(
                "🎮 Games Overview",
                description=(
                    "All gameplay uses real in-game tokens (USD, ARC, DSC, SUN...).\n"
                    "Use the prefix commands below to play - slash shows this info only."
                ),
                color=C_PINK,
            )
            .field(
                "🎰 Casino  `,play`",
                "Coinflip, slots, dice, roulette, blackjack, mines\n"
                "> `,play coinflip 100`  ·  `,play slots 50 ARC`",
                False,
            )
            .field(
                "🍽️ Eat the Rich  `,eat @target`",
                "Punch up - eat a player richer than you\n"
                "> No opt-in: everyone is fair game, poor are safe by the rules\n"
                "> `,fortify` - hire a 2-hour security detail\n"
                "> `,eatstats` - view your class-war record",
                False,
            )
            .field(
                "👑 King of Rugs  `,rugpull`",
                "Wager to claim the King of Rugs throne\n"
                "> King earns bonuses from `,work` & `,ape`\n"
                "> `,king` - current king info  ·  `,rugstats` - your stats\n"
                "> `,sabotage` - weaken the king's defense streak",
                False,
            )
            .field(
                "📊 Stats",
                "`,play stats [game]`  ·  `,eatstats [@user]`  ·  `,rugstats [@user]`",
                False,
            )
            .footer("Games are guild-specific - ask your admin to enable them")
            .build()
        )
        await ctx.send(embed=embed)

    @games.command(name="casino")
    @guild_only
    async def games_casino(self, ctx: DiscoContext) -> None:
        """Casino games overview - coinflip, slots, dice, roulette, blackjack, mines."""
        embed = (
            card(
                "🎰 Casino Games",
                description=(
                    "Place bets using any token (USD, ARC, DSC, SUN...)\n"
                    "House edge: 5%  |  "
                    f"Min: {fmt_usd(to_human(Config.MIN_BET))}"
                ),
                color=C_PINK,
            )
            .field("🪙 Coinflip  `,cf`", "50/50 heads or tails\n> Win: +95% of bet", True)
            .field("🎰 Slots  `,sl`", "3-reel spin: 🍒🍋🍊🍇💎7️⃣\n> Jackpot: 5x  |  Pair: 0.5x", True)
            .field("🎲 Dice", "Pick your multiplier target\n> Range: 1.01x - 10000x", True)
            .field("🎡 Roulette  `,rou`", "European 0-36\n> Red/black, numbers, dozens", True)
            .field("🃏 Blackjack  `,bj`", "Beat dealer to 21\n> Natural BJ: 1.5x", True)
            .field("💣 Mines", "Minesweeper grid\n> Cash out any time", True)
            .field(
                "♞ Chess  `,chess`",
                "vs AI or PvP w/ ELO leaderboard\n> Wins mint **GAMBIT**", True,
            )
            .field(
                "👑 Checkers  `,checkers`",
                "vs AI or PvP w/ ELO leaderboard\n> Wins mint **CROWN**", True,
            )
            .field(
                "Quick Start",
                f"`,play coinflip 100`  ·  `,play mines 200 5`\n"
                f"`,chess play 5 USD`  ·  `,checkers challenge @user 10`\n"
                f"`,gamba info` -- the network economy",
                False,
            )
            .build()
        )
        await ctx.send(embed=embed)

    @games.command(name="eat")
    @guild_only
    async def games_eat(self, ctx: DiscoContext) -> None:
        """Eat the Rich system overview."""
        embed = (
            card(
                "🍽️ Eat the Rich",
                description=(
                    "Class warfare for the crypto economy. No opt-in - everyone is "
                    "fair game, but you can only punch **up**: the target must be "
                    "richer than you on net worth. The wider the wealth gap, the "
                    "better your odds."
                ),
                color=C_CRIMSON,
            )
            .field("🍴 Eat  `,eat @target`", "Choose a tactic and try to eat a richer player\n> Bigger tactic = bigger stake, bigger bite", False)
            .field("🛡 Fortify  `,fortify`", "Hire a 2-hour private security detail\n> Slashes the odds of anyone eating you by 75%", False)
            .field("📊 Stats  `,eatstats [@user]`", "View a class-war record", True)
            .field("📜 History  `,eathistory`", "Recent eats in this server", True)
            .build()
        )
        await ctx.send(embed=embed)

    @games.command(name="rugpull")
    @guild_only
    async def games_rugpull(self, ctx: DiscoContext) -> None:
        """King / Queen of Rugs minigame overview."""
        embed = (
            card(
                "👑 King / Queen of Rugs",
                description=(
                    "Wager tokens for a chance to claim the throne. Winners are "
                    "crowned **King of Rugs** (male) or **Queen of Rugs** (female) "
                    "via gender detection -- pin yours with `,ruggender male|female`.\n"
                    "The monarch earns passive bonuses and a growing **crown discount** "
                    "for challengers - but anyone can rugpull them."
                ),
                color=C_GOLD,
            )
            .field(
                "Tiers",
                "**Low** - 3% of balance (min $50) - 5% chance\n"
                "**Medium** - 15% of balance (min $250) - 40% chance\n"
                "**High** - 30% of balance (min $500) - 75% chance",
                False,
            )
            .field("👑 Challenge  `,rugpull`", "Attempt to take the throne", True)
            .field("ℹ️ Monarch  `,king` / `,queen`", "Current ruler + active mechanics", True)
            .field("💰 Bounty  `,rugbounty`", "Add to the bounty pool", True)
            .field("🗡 Sabotage  `,sabotage`", "Weaken the monarch's defense", True)
            .field("🛡 Defend  `,rugdefend`", "Monarch-only paid defense buff", True)
            .field("📊 Stats  `,rugstats [@user]`", "Your win/loss history", True)
            .field("📜 History  `,rughistory`", "Recent challenges", True)
            .field("🚻 Gender  `,ruggender`", "Pin King / Queen role on next win", True)
            .footer(
                "Defense streak + active defense reduce challenger odds. "
                "Crown discount makes the throne cheaper to topple the longer it's held."
            )
            .build()
        )
        await ctx.send(embed=embed)

    # =========================================================================
    # /market  -  crypto prices, trading, DeFi, NFTs, prediction market
    # =========================================================================

    @commands.hybrid_group(name="market", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def market(self, ctx: DiscoContext) -> None:
        """Market overview - crypto, trading, DeFi, NFTs, and predictions."""
        embed = (
            card(
                "📈 Market Overview",
                description=(
                    "All market activity uses real in-game tokens across multiple networks.\n"
                    "Use the subcommands or prefix commands below to interact."
                ),
                color=C_AMBER,
            )
            .field(
                "📊 Crypto Prices  `,prices`",
                "Live token prices across all networks\n"
                "> Filter by network: `,prices --arc`  `,prices --dsc`  `,prices --sun`",
                False,
            )
            .field(
                "💱 Trading  `,buy` / `,sell` / `,swap`",
                "Spot trades and token swaps\n"
                "> `,buy 100 ARC`  ·  `,sell 50 ARC`  ·  `,swap 100 ARC DSC`",
                False,
            )
            .field(
                "🏦 DeFi  `,stake` / `,earn`",
                "Staking, liquidity pools, validators, and yield farming\n"
                "> `,stake`  ·  `,earn`  ·  `,validator`",
                False,
            )
            .field(
                "🎨 NFTs  `,nft`",
                "Mint, collect, list, and buy NFTs on ARC and DSC chains\n"
                "> `,nft collections`  ·  `,nft market`  ·  `,mint`",
                False,
            )
            .field(
                "🔮 Predictions  `,predict`",
                "Bet on real-world outcomes (parimutuel)\n"
                "> `,predict list`  ·  `,predict bet <id> yes 100`",
                False,
            )
            .footer("Prices update in real-time - run ,prices for a live snapshot")
            .build()
        )
        await ctx.send(embed=embed)

    @market.command(name="crypto")
    @guild_only
    async def market_crypto(self, ctx: DiscoContext) -> None:
        """Crypto prices and trading overview."""
        embed = (
            card(
                "📊 Crypto  -  Prices & Trading",
                description="Live token prices across all networks. Trades settle instantly.",
                color=C_AMBER,
            )
            .field("💹 Prices  `,prices`", "All tokens  |  `,prices --arc`  `,prices --dsc`  `,prices --sun`", False)
            .field("🟢 Buy  `,buy <amount> <token>`", "e.g. `,buy 100 ARC`  ·  `,buy 0.5 MTA`", True)
            .field("🔴 Sell  `,sell <amount> <token>`", "e.g. `,sell 50 ARC`", True)
            .field("🔄 Swap  `,swap <amount> <from> <to>`", "e.g. `,swap 100 ARC DSC`", False)
            .field("📁 Portfolio  `,balance`", "Your full cross-chain holdings and net worth", False)
            .footer("Price feed updates every cycle - use ,prices for latest")
            .build()
        )
        await ctx.send(embed=embed)

    @market.command(name="defi")
    @guild_only
    async def market_defi(self, ctx: DiscoContext) -> None:
        """DeFi overview - staking, pools, validators, and yield farming."""
        embed = (
            card(
                "🏦 DeFi  -  Staking, Pools & Validators",
                description=(
                    "Earn yield on idle tokens through staking, liquidity pools, "
                    "and validator delegation."
                ),
                color=C_TEAL,
            )
            .field("💎 Staking  `,stake`", "Stake tokens to validators to earn block rewards\n> `,stake farm`  ·  `,stake list`  ·  `,stake info`", False)
            .field("🌊 Earn / Pools  `,earn`", "Liquidity pools and yield farming\n> `,earn deposit`  ·  `,earn withdraw`  ·  `,earn rewards`", False)
            .field("🔐 Validators  `,validator`", "Browse and manage validators\n> `,validator list`  ·  `,validator info <name>`", False)
            .field("⛓ Chain  `,chain`", "Cross-chain bridge and network operations", False)
            .footer("Staking uses 24-hour lock periods per batch - plan accordingly")
            .build()
        )
        await ctx.send(embed=embed)

    @market.command(name="nft")
    @guild_only
    async def market_nft(self, ctx: DiscoContext) -> None:
        """NFT market overview."""
        embed = (
            card(
                "🎨 NFTs  -  Collections & Marketplace",
                description=(
                    "Non-fungible tokens on Arcadia (ARC) and Discoin (DSC) networks.\n"
                    "Each NFT has a unique on-chain hash. Rarities: common > uncommon > rare > epic > legendary."
                ),
                color=C_PURPLE,
            )
            .field("🗂 Collections  `,nft collections`", "Browse all NFT collections in this server", False)
            .field("✨ Mint  `,mint <collection>`", "Mint a new NFT from an active collection\n> Pay the mint price in the collection's token", True)
            .field("👜 Inventory  `,nft inventory`", "Your owned NFTs", True)
            .field("🏪 Marketplace  `,nft market`", "Browse NFTs listed for sale", True)
            .field("🛒 Buy  `,nft buy <id>`", "Purchase a listed NFT", True)
            .field("📋 List  `,nft list <id> <price>`", "List your NFT for sale", True)
            .field("🔍 Inspect  `,nft view <id>`", "Full detail view of any NFT", True)
            .footer("High-tier players (Protocol Dev+) can deploy custom NFT collections")
            .build()
        )
        await ctx.send(embed=embed)

    @market.command(name="predictions")
    @guild_only
    async def market_predictions(self, ctx: DiscoContext) -> None:
        """Prediction market overview."""
        embed = (
            card(
                "🔮 Prediction Markets",
                description=(
                    "Bet on real-world outcomes. Winnings are proportional to your share "
                    "of the winning pool (parimutuel). 5% house cut."
                ),
                color=C_INFO,
            )
            .field("📋 Open Markets  `,predict list`", "Browse all open prediction markets", False)
            .field("🔍 View  `,predict view <id>`", "Full details, odds, and current pool sizes", True)
            .field("🎯 Bet  `,predict bet <id> <side> <amount>`", "e.g. `,predict bet 3 yes 100`", True)
            .field("📊 My Bets  `,predict mybets`", "Track your open and settled bets", False)
            .footer("Markets are created by admins and resolved when outcomes are known")
            .build()
        )
        await ctx.send(embed=embed)


# ============================================================================
# ,start  -- button-driven game hub (root -> per-game submenu)
# ============================================================================
#
# Each entry is (label, command, emoji, style). The command is invoked exactly
# as if the user typed it, via bot.process_commands on a copy of the source
# message -- same trick the FuzzyView uses elsewhere. Every command listed
# here is a bare command (no args) so the button always lands on something
# valid: the cast UI, the shop embed, the stake panel, etc.
#
# Casino actions hand off to ``,play help <game>`` because casino games all
# need a bet amount; the help screen tells the user the exact syntax.
_GAME_ACTIONS: dict[str, list[tuple[str, str, str, discord.ButtonStyle]]] = {
    "casino": [
        ("Casino Menu",  "play",                  "\U0001F3B0", discord.ButtonStyle.primary),
        ("Coinflip",     "play help coinflip",    "\U0001FA99", discord.ButtonStyle.secondary),
        ("Dice",         "play help dice",        "\U0001F3B2", discord.ButtonStyle.secondary),
        ("Slots",        "play help slots",       "\U0001F3B0", discord.ButtonStyle.secondary),
        ("Blackjack",    "play help blackjack",   "\U0001F0CF", discord.ButtonStyle.secondary),
        ("Chess",        "chess help",            "♞",     discord.ButtonStyle.secondary),
        ("Checkers",     "checkers help",         "\U0001F451", discord.ButtonStyle.secondary),
        ("Gamba Network","gamba info",            "\U0001F3B0", discord.ButtonStyle.success),
    ],
    "fishing": [
        ("Cast Line",    "fish",       "\U0001F3A3", discord.ButtonStyle.primary),
        ("Tackle Shop",  "fish shop",  "\U0001F3EA", discord.ButtonStyle.secondary),
        ("Stake LURE",   "fish stake", "\U0001F30A", discord.ButtonStyle.secondary),
        ("Stats",        "fish stats", "\U0001F4CA", discord.ButtonStyle.secondary),
        ("Leaderboard",  "fish lb",    "\U0001F3C6", discord.ButtonStyle.secondary),
    ],
    "delve": [
        ("Dungeon",      "delve",       "\U0001F5FA", discord.ButtonStyle.primary),
        ("Surface Shop", "delve shop",  "\U00002694",  discord.ButtonStyle.secondary),
        ("Stake Ore",    "delve stake", "\U0001F510", discord.ButtonStyle.secondary),
        ("Stats",        "delve stats", "\U0001F4CA", discord.ButtonStyle.secondary),
        ("Leaderboard",  "delve lb",    "\U0001F3C6", discord.ButtonStyle.secondary),
    ],
    "farm": [
        ("Field",        "farm",        "\U0001F33E", discord.ButtonStyle.primary),
        ("Farm Shop",    "farm shop",   "\U0001F3EA", discord.ButtonStyle.secondary),
        ("Stake SEED",   "farm stake",  "\U0001F331", discord.ButtonStyle.secondary),
        ("Crops",        "farm crops",  "\U0001F33F", discord.ButtonStyle.secondary),
        ("Leaderboard",  "farm lb",     "\U0001F3C6", discord.ButtonStyle.secondary),
    ],
    "buddy": [
        ("My Buddy",     "buddy stats",   "\U0001F436", discord.ButtonStyle.primary),
        ("Buddy Shop",   "buddy shop",    "\U0001F6CD", discord.ButtonStyle.secondary),
        ("Shelter",      "buddy shelter", "\U0001F3E0", discord.ButtonStyle.secondary),
        ("Storage",      "buddy storage", "\U0001F4E6", discord.ButtonStyle.secondary),
        ("Nest",         "buddy nest",    "\U0001FAB9", discord.ButtonStyle.secondary),
        ("Battles",      "buddy battles", "\U00002694",  discord.ButtonStyle.secondary),
        ("Leaderboard",  "buddy lb",      "\U0001F3C6", discord.ButtonStyle.secondary),
    ],
    "crafting": [
        ("Forge",        "craft",            "\U0001F528", discord.ButtonStyle.primary),
        ("Recipes",      "craft list",       "\U0001F4DC", discord.ButtonStyle.secondary),
        ("Specialties",  "craft specialties","\U0001F4DA", discord.ButtonStyle.secondary),
        ("Stake INGOT",  "craft stake",      "\U0001F9F1", discord.ButtonStyle.secondary),
        ("Leaderboard",  "craft lb",         "\U0001F3C6", discord.ButtonStyle.secondary),
    ],
}

_GAME_META: dict[str, tuple[str, str, str, int]] = {
    # game_key: (display_name, emoji, blurb, color)
    "casino":   ("Casino",   "\U0001F3B0", "Coinflip, dice, slots, roulette, blackjack, mines.", C_PINK),
    "fishing":  ("Fishing",  "\U0001F3A3", "Cast for fish, sell on land for LURE, stake for REEL.", C_TEAL),
    "delve":    ("Delve",    "\U0001F5FA", "Dungeon crawler -- mob captures, ore tiers, RUNE economy.", C_AMBER),
    "farm":     ("Farm",     "\U0001F33E", "Plant seeds, weather seasons, harvest crops, brew HRV.", C_GOLD),
    "buddy":    ("Buddy",    "\U0001F436", "Hatch + raise companions, storage + nest, arena, BUD shop.", C_PURPLE),
    "crafting": ("Crafting", "\U0001F528", "Smithing / alchemy / cooking / fletching / tinkering -- mint INGOT, stake to drip FORGE.", C_AMBER),
}


def _hub_root_embed(prefix: str) -> discord.Embed:
    b = card(
        "\U0001F3AE Discoin Game Hub",
        description=(
            "Pick a game below. Each button drops you into the bare command "
            "for that surface (state view, shop, stake panel, leaderboard) "
            "without you having to remember the exact prefix syntax.\n\n"
            f"Tip: prefix commands like `{prefix}fish`, `{prefix}delve`, "
            f"`{prefix}farm`, `{prefix}buddy`, `{prefix}play` still work directly."
        ),
        color=C_GOLD,
    )
    for key, (name, emoji, blurb, _color) in _GAME_META.items():
        b.field(f"{emoji} {name}", blurb, False)
    # Real-market namespace pointer -- the `$` ecosystem is fully separate
    # from this game hub and lives at `$help`.
    b.field(
        "\U0001F4E1 Real Markets ($-prefix, separate from the game)",
        "Live cross-asset markets (crypto + stocks + ETFs + forex + perps "
        "+ oracles). Type `$help` for the tour, `$chart MTA 1d`, "
        "`$info MSFT`, `$scan ARC 4h ai`, or `$query <question>` for AI "
        "research with trusted-source citations.",
        False,
    )
    return b.footer(f"Menu times out after 5 minutes  ·  ,start to reopen").build()


def _hub_submenu_embed(game: str, prefix: str) -> discord.Embed:
    name, emoji, blurb, color = _GAME_META[game]
    b = card(f"{emoji} {name}", description=blurb, color=color)
    actions = _GAME_ACTIONS[game]
    cmd_lines = "\n".join(
        f"{e}  **{label}**  ·  `{prefix}{cmd}`"
        for label, cmd, e, _style in actions
    )
    b.field("Quick actions", cmd_lines, False)
    return b.footer("Click any button to run the listed command.").build()


class _RunCommandButton(discord.ui.Button):
    """Re-invokes a bare command on behalf of the menu owner.

    Mirrors the FuzzyView pattern in core.framework.bot: copy the source message,
    overwrite ``content`` with the target command, and route through
    ``bot.process_commands`` so middleware (cooldowns, registration checks,
    module gates) all run as if the user typed it.
    """

    def __init__(
        self,
        label: str,
        cmd: str,
        emoji: str,
        style: discord.ButtonStyle,
        ctx: DiscoContext,
        row: int,
        *,
        refresh_tab: str | None = None,
    ) -> None:
        super().__init__(label=label, emoji=emoji, style=style, row=row)
        self.cmd = cmd
        self.ctx = ctx
        self.refresh_tab = refresh_tab

    async def callback(self, interaction: discord.Interaction) -> None:
        prefix = self.ctx.prefix or Config.PREFIX
        new_msg = copy.copy(self.ctx.message)
        new_msg.content = f"{prefix}{self.cmd}"  # type: ignore[attr-defined]
        await interaction.response.defer()
        try:
            await self.ctx.bot.process_commands(new_msg)
        finally:
            if self.refresh_tab:
                try:
                    await StartInterfaceView.render_tab(
                        interaction, self.ctx, self.refresh_tab,
                    )
                except Exception:
                    pass


class _GameSelectButton(discord.ui.Button):
    def __init__(self, game: str, ctx: DiscoContext, row: int) -> None:
        name, emoji, _blurb, _color = _GAME_META[game]
        super().__init__(
            label=name, emoji=emoji,
            style=discord.ButtonStyle.primary, row=row,
        )
        self.game = game
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction) -> None:
        prefix = self.ctx.prefix or Config.PREFIX
        embed = _hub_submenu_embed(self.game, prefix)
        view = GameSubmenuView(self.ctx, self.game)
        await interaction.response.edit_message(embed=embed, view=view)


class _BackButton(discord.ui.Button):
    def __init__(self, ctx: DiscoContext) -> None:
        super().__init__(
            label="Back", emoji="\U000025C0",
            style=discord.ButtonStyle.secondary, row=4,
        )
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction) -> None:
        prefix = self.ctx.prefix or Config.PREFIX
        embed = _hub_root_embed(prefix)
        view = GameHubView(self.ctx)
        await interaction.response.edit_message(embed=embed, view=view)


class _OwnedView(discord.ui.View):
    """Base class -- only the menu opener can press buttons."""

    def __init__(self, ctx: DiscoContext) -> None:
        super().__init__(timeout=300.0)
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Not your menu -- run `,start` to open your own.", ephemeral=True,
            )
            return False
        return True


class GameHubView(_OwnedView):
    def __init__(self, ctx: DiscoContext) -> None:
        super().__init__(ctx)
        # 5 game buttons across two rows so labels stay readable on mobile.
        for i, key in enumerate(_GAME_META):
            self.add_item(_GameSelectButton(key, ctx, row=i // 3))


class GameSubmenuView(_OwnedView):
    def __init__(self, ctx: DiscoContext, game: str) -> None:
        super().__init__(ctx)
        for i, (label, cmd, emoji, style) in enumerate(_GAME_ACTIONS[game]):
            self.add_item(_RunCommandButton(label, cmd, emoji, style, ctx, row=i // 3))
        self.add_item(_BackButton(ctx))


async def _gather_player_state(ctx: DiscoContext) -> dict:
    """Pull every state-flag ,start needs into one round of queries.

    Returned dict carries flat keys so ``_starter_next_steps`` can do
    boolean checks without re-fetching anything. Failures fall back to
    sensible defaults (untouched / zero) so the dashboard always renders.
    """
    db = ctx.db
    uid, gid = ctx.author.id, ctx.guild_id

    user_row = await db.ensure_user(uid, gid, str(ctx.author))
    starter_claimed_at = (user_row or {}).get("starter_pack_claimed_at")
    wallet_h = (user_row or {}).h("wallet") if user_row else 0.0
    last_daily = (user_row or {}).get("last_daily")
    daily_streak = int((user_row or {}).get("daily_streak") or 0)

    # Net worth -- best-effort. compute_net_worth touches a lot of
    # tables, so we tolerate failures so ,start still renders for users
    # whose state is incomplete (e.g. a brand-new account that hasn't
    # been ensure_user'd through every cog yet).
    net_worth = wallet_h
    try:
        from services.net_worth import compute_net_worth
        nw = await compute_net_worth(uid, gid, db)
        net_worth = float(nw.total)
    except Exception:
        log.debug("start: compute_net_worth failed", exc_info=True)

    # Per-game progress flags. Each fetch is wrapped because the cog
    # may not have run ensure_state yet for a brand-new player.
    has_buddy = False
    hatch_count = 0
    try:
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM cc_buddies "
            "WHERE owner_user_id=$1 AND guild_id=$2 AND status='owned'",
            uid, gid,
        )
        has_buddy = bool(int((row or {}).get("n") or 0))
    except Exception:
        pass
    try:
        be = await db.fetch_one(
            "SELECT hatch_count FROM user_buddy_economy "
            "WHERE user_id=$1 AND guild_id=$2", uid, gid,
        )
        hatch_count = int((be or {}).get("hatch_count") or 0)
    except Exception:
        pass

    fish_total = 0
    try:
        fr = await db.fetch_one(
            "SELECT total_caught FROM user_fishing "
            "WHERE user_id=$1 AND guild_id=$2", uid, gid,
        )
        fish_total = int((fr or {}).get("total_caught") or 0)
    except Exception:
        pass

    farm_total = 0
    try:
        fa = await db.fetch_one(
            "SELECT total_harvested FROM user_farming "
            "WHERE user_id=$1 AND guild_id=$2", uid, gid,
        )
        farm_total = int((fa or {}).get("total_harvested") or 0)
    except Exception:
        pass

    delve_runs = 0
    try:
        d = await db.fetch_one(
            "SELECT total_runs FROM user_dungeon "
            "WHERE user_id=$1 AND guild_id=$2", uid, gid,
        )
        delve_runs = int((d or {}).get("total_runs") or 0)
    except Exception:
        pass

    has_any_stone = False
    try:
        st = await db.fetch_val(
            "SELECT 1 FROM hashstones WHERE user_id=$1 AND guild_id=$2 "
            "UNION ALL SELECT 1 FROM lockstones WHERE user_id=$1 AND guild_id=$2 "
            "LIMIT 1",
            uid, gid,
        )
        has_any_stone = bool(st)
    except Exception:
        pass

    # Crafting + buddy storage / daycare progress flags so the home tab's
    # "Next Steps" list can promote the new content discovery surfaces.
    total_crafts = 0
    try:
        cr = await db.fetch_one(
            "SELECT total_crafts FROM user_crafting "
            "WHERE user_id=$1 AND guild_id=$2", uid, gid,
        )
        total_crafts = int((cr or {}).get("total_crafts") or 0)
    except Exception:
        pass
    n_owned_buddies = 0
    n_stored_buddies = 0
    try:
        rr = await db.fetch_one(
            "SELECT "
            "  COUNT(*) FILTER (WHERE status='owned')  AS owned, "
            "  COUNT(*) FILTER (WHERE status='stored') AS stored "
            "FROM cc_buddies "
            "WHERE owner_user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        n_owned_buddies = int((rr or {}).get("owned") or 0)
        n_stored_buddies = int((rr or {}).get("stored") or 0)
    except Exception:
        pass
    daycare_active = False
    try:
        daycare_active = bool(await db.fetch_val(
            "SELECT 1 FROM cc_buddy_daycare "
            "WHERE user_id=$1 AND guild_id=$2 LIMIT 1",
            uid, gid,
        ))
    except Exception:
        pass

    return {
        "wallet_h":            float(wallet_h or 0.0),
        "net_worth":           float(net_worth or 0.0),
        "starter_claimed":     starter_claimed_at is not None,
        "last_daily":       last_daily,
        "daily_streak":        daily_streak,
        "has_buddy":           has_buddy,
        "hatch_count":         hatch_count,
        "fish_total":          fish_total,
        "farm_total":          farm_total,
        "delve_runs":          delve_runs,
        "has_any_stone":       has_any_stone,
        "total_crafts":        total_crafts,
        "n_owned_buddies":     n_owned_buddies,
        "n_stored_buddies":    n_stored_buddies,
        "daycare_active":      daycare_active,
    }


def _daily_ready(last_daily) -> bool:
    """True if ``,daily`` is off cooldown for this player."""
    if last_daily is None:
        return True
    import time as _time, datetime as _dt
    if isinstance(last_daily, _dt.datetime):
        ts = last_daily.timestamp()
    else:
        try:
            ts = float(last_daily)
        except Exception:
            return True
    return (_time.time() - ts) >= 23 * 3600  # 23h grace so streaks don't lapse


def _starter_next_steps(state: dict, prefix: str) -> list[tuple[str, str, str]]:
    """Return up to 5 (emoji, title, command) tuples ranked by relevance.

    The list adapts to the player's progress: a fresh account gets
    "claim starter pack" first; a player who's already started fishing
    sees "stake LURE" or "buy a Tidestone"; etc. Capped at 5 so the
    embed body stays readable on mobile.
    """
    steps: list[tuple[str, str, str]] = []

    if not state["starter_claimed"]:
        steps.append((
            "\U0001F381", "Claim the starter pack",
            "(use the button below)",
        ))
    if _daily_ready(state["last_daily"]):
        steps.append((
            "\U0001F4B0", "Claim your daily payout",
            f"{prefix}daily",
        ))
    if not state["has_buddy"]:
        try:
            from configs.buddies_config import HATCH_FREE_COUNT as _free_max
        except Exception:
            _free_max = 3
        free_left = max(0, int(_free_max) - state["hatch_count"])
        suffix = f"  ({free_left} free hatches left)" if free_left > 0 else ""
        steps.append((
            "\U0001F95A", "Hatch your first buddy" + suffix,
            f"{prefix}buddy hatch",
        ))
    if state["fish_total"] == 0:
        steps.append((
            "\U0001F3A3", "Cast your first line",
            f"{prefix}fish",
        ))
    if state["farm_total"] == 0:
        steps.append((
            "\U0001F33E", "Plant your first crop",
            f"{prefix}farm plant 1 wheat",
        ))
    if state["delve_runs"] == 0:
        steps.append((
            "\U0001F5FA", "Pick a class & enter the dungeon",
            f"{prefix}delve class warrior",
        ))
    if not state["has_any_stone"] and state["wallet_h"] >= 6_000.0:
        steps.append((
            "\U0001F511", "Buy a Lockstone (boosts staking + work)",
            f"{prefix}shop buy lockstone",
        ))
    if state["has_buddy"] and state["fish_total"] >= 5:
        steps.append((
            "\U0001F3DF", "Try the buddy arena (BBT + BUD)",
            f"{prefix}buddy arena fight",
        ))
    # Crafting bootstrap once the player has ingredients flowing in. Gate
    # on at least 5 fish OR 5 crops OR a delve run so a brand-new player
    # doesn't get pushed into ,craft list before they have anything to
    # craft with.
    if (
        state.get("total_crafts", 0) == 0
        and (
            state.get("fish_total", 0) >= 5
            or state.get("farm_total", 0) >= 5
            or state.get("delve_runs", 0) >= 1
        )
    ):
        steps.append((
            "\U0001F528", "Forge your first craft (mints INGOT)",
            f"{prefix}craft list",
        ))
    # Buddy storage hint: the player owns 2+ buddies but has nothing
    # stashed. Storage is the right way to keep collectible buddies
    # without surrendering them.
    if (
        state.get("n_owned_buddies", 0) >= 2
        and state.get("n_stored_buddies", 0) == 0
    ):
        steps.append((
            "\U0001F4E6", "Stash a spare buddy in storage",
            f"{prefix}buddy storage",
        ))
    # Nest hint: 2+ owned buddies and the nest slot is empty.
    if (
        state.get("n_owned_buddies", 0) >= 2
        and not state.get("daycare_active", False)
    ):
        steps.append((
            "\U0001FAB9", "Breed a new buddy in the nest",
            f"{prefix}buddy nest",
        ))

    return steps[:5]


def _start_dashboard_embed(
    ctx: DiscoContext, state: dict, steps: list[tuple[str, str, str]],
    *, summary: "Any | None" = None,
) -> discord.Embed:
    """Build the unified Home / Today embed.

    When ``summary`` (a ``services.hub.HubSummary``) is supplied, the
    panel folds in today-style live status (streak nudge, top quests,
    ready feed, stat-point reminder, in-progress activity) so ``,today``
    and ``,start`` render the same surface and share the tabbed
    interactive view. Without ``summary`` the legacy onboarding card
    renders unchanged.
    """
    prefix = ctx.prefix or Config.PREFIX
    if not state["starter_claimed"]:
        title = "\U0001F44B Welcome to Discoin"
        blurb = (
            "*First time here? Tap **Claim Starter Pack** below for some seed "
            "capital so you can poke at every game without grinding faucet drops first.*"
        )
        color = C_GOLD
    elif state["net_worth"] < 5_000.0:
        title = "\U0001F331 Getting started"
        blurb = (
            "*You've got the starter pack. The next steps below are tuned to "
            "exactly where you are right now.*"
        )
        color = C_INFO
    else:
        title = "\U0001F3AE Discoin Hub"
        blurb = (
            "*Welcome back. Quick actions, claim status, and your next "
            "objectives are below; the buttons run the listed command in place.*"
        )
        color = C_PURPLE

    b = card(title, description=blurb, color=color).author(
        ctx.author.display_name, icon_url=ctx.author.display_avatar.url,
    ).thumbnail(ctx.author.display_avatar.url)

    # Status panel -- progress-bar stat rows in the catbot panel style.
    # Wallet share of net worth, daily-claim cooldown, and the streak
    # progress to the next 30-day milestone all read at a glance.
    streak = state["daily_streak"]
    streak_ms = max(7, ((streak // 7) + 1) * 7) if streak < 30 else 30
    wallet_h = float(state["wallet_h"] or 0.0)
    net_worth = float(state["net_worth"] or 0.0)
    wallet_label = f"\U0001F4B5 Wallet  **{fmt_usd(wallet_h)}**"
    nw_label = f"\U0001F48E Net worth  **{fmt_usd(net_worth)}**"
    daily_ready = _daily_ready(state["last_daily"])
    daily_label = (
        "\U0001F4B0 Daily  **ready**" if daily_ready
        else "\U0001F4B0 Daily  *on cooldown*"
    )
    streak_label = (
        f"\U0001F525 Streak  **{streak}d**"
        if streak > 0 else "\U0001F525 Streak  *not started*"
    )
    status_lines = [
        FormatKit.stat_row(wallet_h, max(net_worth, wallet_h, 1.0), wallet_label),
        FormatKit.stat_row(net_worth, max(net_worth, 1.0), nw_label),
        FormatKit.stat_row(1 if daily_ready else 0, 1, daily_label, count_width=1),
        FormatKit.stat_row(min(streak, streak_ms), streak_ms, streak_label, count_width=2),
    ]
    b.field("Status", "\n".join(status_lines), False)

    # Per-game progress -- one stat-row per surface so the player can
    # see at a glance which games they've touched and which are next.
    # The "max" target is a soft milestone (5/10 caught/harvested, 1
    # buddy, 1 stone) so the bar fills as players cross each beat.
    progress_rows: list[str] = [
        FormatKit.stat_row(
            min(state["hatch_count"], 1), 1,
            f"\U0001F436 Buddy  *({state['hatch_count']} hatched)*",
            count_width=1,
        ),
        FormatKit.stat_row(
            min(state["fish_total"], 25), 25,
            f"\U0001F3A3 Fishing  *({state['fish_total']:,} caught)*",
            count_width=2,
        ),
        FormatKit.stat_row(
            min(state["farm_total"], 25), 25,
            f"\U0001F33E Farming  *({state['farm_total']:,} harvested)*",
            count_width=2,
        ),
        FormatKit.stat_row(
            min(state["delve_runs"], 10), 10,
            f"\U0001F5FA Delve  *({state['delve_runs']:,} runs)*",
            count_width=2,
        ),
        FormatKit.stat_row(
            min(int(state.get("total_crafts") or 0), 10), 10,
            f"\U0001F528 Crafting  *({int(state.get('total_crafts') or 0):,} crafts)*",
            count_width=2,
        ),
        FormatKit.stat_row(
            1 if state["has_any_stone"] else 0, 1,
            "\U0001F48E Stones  " + (
                "**owned**" if state["has_any_stone"] else "*not yet*"
            ),
            count_width=1,
        ),
    ]
    b.field("\U0001F9ED Your progress", "\n".join(progress_rows), False)

    if steps:
        next_lines = []
        for emoji, title, cmd in steps:
            tail = f"  ·  `{cmd}`" if cmd and not cmd.startswith("(") else f"  ·  {cmd}"
            next_lines.append(f"{emoji}  **{title}**{tail}")
        b.field(
            "\U0001F3AF Next steps",
            "\n".join(next_lines),
            False,
        )

    # Today-panel addendum -- only renders when ,today / ,start passed a
    # HubSummary in. Mirrors the field set the dedicated ``,today`` cog
    # used to draw so unifying the two commands doesn't lose any info.
    if summary is not None:
        # Top quests (max 3). Quest rows from services.quests carry only
        # ``quest_id`` + counters; name/icon live on the static template
        # in ``quests_config.QUESTS`` and must be hydrated from there.
        if summary.quests_top:
            try:
                import configs.quests_config as _qcat
                _qmap = {q["quest_id"]: q for q in _qcat.QUESTS}
            except Exception:
                _qmap = {}
            qlines: list[str] = []
            for i, q in enumerate(summary.quests_top, 1):
                target = max(1, int(q.get("target") or 0))
                progress = int(q.get("progress") or 0)
                ready = (not q.get("claimed")) and progress >= target
                state_tag = (
                    "\U00002705 ready -- `,quests claim`" if ready
                    else f"{progress}/{target}"
                )
                qid = str(q.get("quest_id") or "")
                tmpl = _qmap.get(qid) or {}
                _icon = tmpl.get("icon") or q.get("icon") or "\U0001F4DC"
                _qname = tmpl.get("name") or q.get("name") or qid or "Quest"
                _period = str(q.get("_period") or "").title()
                _period_tag = f" · _{_period}_" if _period else ""
                qlines.append(
                    f"**{i}. {_icon} {_qname}**{_period_tag} -- {state_tag}"
                )
            b.field("Top quests", "\n".join(qlines), False)

        # Stat points (delve player + buddy roster).
        stat_lines: list[str] = []
        if summary.delve_class_key:
            if summary.delve_unspent_stats > 0:
                stat_lines.append(
                    f"\U0001F4CA  **{summary.delve_unspent_stats} unspent** "
                    f"as a **{summary.delve_class_key}** -- `,delve upgrade`."
                )
        elif summary.delve_unspent_stats > 0:
            stat_lines.append(
                f"\U0001F4CA  **{summary.delve_unspent_stats} unspent** "
                f"delve points -- `,delve upgrade`."
            )
        if summary.buddies_unspent_stats > 0:
            stat_lines.append(
                f"\U0001F436  **{summary.buddies_unspent_stats} unspent** "
                f"buddy stat point"
                f"{'s' if summary.buddies_unspent_stats != 1 else ''} -- "
                f"`,buddy upgrade`."
            )
        if stat_lines:
            b.field("Stat points", "\n".join(stat_lines), False)

        # Ready right now -- claimable items only (still-running expeditions
        # are intentionally excluded; they sit in the Activity rollup below).
        if summary.ready_hints:
            b.field(
                "Ready right now",
                "\n".join(f"- {h}" for h in summary.ready_hints),
                False,
            )

        # Activity rollup -- in-progress informational lines.
        activity_bits: list[str] = []
        if summary.expeditions_running:
            activity_bits.append(
                f"\U0001F392 **{summary.expeditions_running} expedition"
                f"{'s' if summary.expeditions_running != 1 else ''}** in progress"
            )
        if summary.ah_active:
            activity_bits.append(
                f"\U0001F4B0 **{summary.ah_active} AH listing"
                f"{'s' if summary.ah_active != 1 else ''}** active"
            )
        if summary.challenges_active:
            activity_bits.append(
                f"\U0001F3C6 **{summary.challenges_active} guild challenge"
                f"{'s' if summary.challenges_active != 1 else ''}** active"
            )
        if activity_bits:
            b.field("Activity", "\n".join(activity_bits), False)

        # Top calendar tiles inline so the home view tells the player
        # what's coming up without an extra click. The full grid still
        # lives behind the Calendar button.
        cal_items = list(getattr(summary, "calendar_items", []) or [])[:5]
        if cal_items:
            cal_lines = []
            for it in cal_items:
                ts = (it.ends_at if it.active_now else it.starts_at)
                when = fmt_rel(ts, fallback="ongoing") if ts is not None else "ongoing"
                tag = "live" if it.active_now else "upcoming"
                cal_lines.append(
                    f"{it.emoji or ''} **{it.title}** ({tag}) -- "
                    f"{'ends' if it.active_now else 'starts'} {when}"
                )
            b.field(
                "Calendar",
                "\n".join(cal_lines)
                + "\n-# Tap **Calendar** below for the full grid.",
                False,
            )

    b.footer(
        f"{prefix}today to reopen  ·  switch tabs to play any game from "
        f"this panel  ·  buttons run the listed command as you'd type it"
    )
    return b.build()


async def _grant_starter_pack(ctx: DiscoContext) -> tuple[bool, str]:
    """Atomically credit the starter pack to the calling user.

    Returns ``(success, message)``. Idempotent: if the pack was already
    claimed (claimed_at IS NOT NULL on a concurrent call), no-ops and
    returns ``(False, "already claimed")``.
    """
    db = ctx.db
    uid, gid = ctx.author.id, ctx.guild_id

    try:
        async with db.atomic():
            # Conditional flip: only proceed if claimed_at is still NULL.
            # asyncpg returns the previous row on RETURNING * regardless,
            # but `rowcount` on the cursor isn't exposed -- use a SELECT
            # before to short-circuit, then RETURNING for the post-state.
            row = await db.fetch_one(
                """UPDATE users
                      SET starter_pack_claimed_at = NOW()
                    WHERE user_id = $1 AND guild_id = $2
                      AND starter_pack_claimed_at IS NULL
                    RETURNING starter_pack_claimed_at""",
                uid, gid,
            )
            if not row:
                return False, "Starter pack already claimed."

            # USD wallet credit.
            await db.update_wallet(uid, gid, to_raw(STARTER_PACK_USD))

            # Fishing bait inventory bump. Insert the user_fishing row
            # if it doesn't exist yet -- the cog ensure_state would do
            # this on first ,fish anyway, but doing it here means the
            # bait shows up before the player ever runs ,fish.
            try:
                await db.execute(
                    "INSERT INTO user_fishing (user_id, guild_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    uid, gid,
                )
                await db.execute(
                    "UPDATE user_fishing "
                    "   SET bait_inventory = "
                    "       COALESCE(bait_inventory, '{}'::jsonb) || $3::jsonb "
                    " WHERE user_id = $1 AND guild_id = $2",
                    uid, gid, json.dumps(STARTER_PACK_BAIT),
                )
            except Exception:
                log.debug("starter pack: bait inv bump failed", exc_info=True)

            # Farming seed inventory bump. Same idempotent insert.
            try:
                await db.execute(
                    "INSERT INTO user_farming (user_id, guild_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    uid, gid,
                )
                await db.execute(
                    "UPDATE user_farming "
                    "   SET seed_packets = "
                    "       COALESCE(seed_packets, '{}'::jsonb) || $3::jsonb "
                    " WHERE user_id = $1 AND guild_id = $2",
                    uid, gid, json.dumps(STARTER_PACK_SEEDS),
                )
            except Exception:
                log.debug("starter pack: seed inv bump failed", exc_info=True)

    except Exception as exc:
        log.exception("starter pack: grant failed uid=%s gid=%s", uid, gid)
        return False, f"Could not grant starter pack: {exc}"

    bait_str = ", ".join(f"{n}x {k}" for k, n in STARTER_PACK_BAIT.items())
    seed_str = ", ".join(f"{n}x {k}" for k, n in STARTER_PACK_SEEDS.items())
    msg = (
        f"+ **{fmt_usd(STARTER_PACK_USD)}** to your wallet\n"
        f"+ Fishing bait: {bait_str}\n"
        f"+ Farm seeds: {seed_str}"
    )
    return True, msg


class _StarterPackButton(discord.ui.Button):
    """Claim the starter pack and refresh the dashboard."""

    def __init__(self, ctx: DiscoContext, row: int) -> None:
        super().__init__(
            label="Claim Starter Pack",
            emoji="\U0001F381",
            style=discord.ButtonStyle.success,
            row=row,
        )
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction) -> None:
        ok, msg = await _grant_starter_pack(self.ctx)
        if ok:
            await interaction.response.send_message(
                f"\U0001F381 **Starter pack claimed!**\n{msg}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"\U000026A0 {msg}", ephemeral=True,
            )
        # Refresh the dashboard with the post-claim state.
        try:
            new_state = await _gather_player_state(self.ctx)
            prefix = self.ctx.prefix or Config.PREFIX
            steps = _starter_next_steps(new_state, prefix)
            embed = _start_dashboard_embed(self.ctx, new_state, steps)
            view = StartDashboardView(self.ctx, new_state)
            await interaction.message.edit(embed=embed, view=view)
        except Exception:
            log.debug("start: dashboard refresh failed", exc_info=True)


class StartDashboardView(_OwnedView):
    """Top-level ``,start`` view: dashboard + starter-pack + game hub jump."""

    def __init__(self, ctx: DiscoContext, state: dict) -> None:
        super().__init__(ctx)
        if not state.get("starter_claimed"):
            self.add_item(_StarterPackButton(ctx, row=0))
        if _daily_ready(state.get("last_daily")):
            self.add_item(_RunCommandButton(
                "Claim Daily", "daily", "\U0001F4B0",
                discord.ButtonStyle.success, ctx, row=0,
            ))
        if not state.get("has_buddy"):
            self.add_item(_RunCommandButton(
                "Hatch Buddy", "buddy hatch", "\U0001F95A",
                discord.ButtonStyle.primary, ctx, row=1,
            ))
        if not state.get("fish_total"):
            self.add_item(_RunCommandButton(
                "Cast Line", "fish", "\U0001F3A3",
                discord.ButtonStyle.primary, ctx, row=1,
            ))
        if not state.get("farm_total"):
            self.add_item(_RunCommandButton(
                "Plant", "farm plant 1 wheat", "\U0001F33E",
                discord.ButtonStyle.primary, ctx, row=1,
            ))
        if not state.get("delve_runs"):
            self.add_item(_RunCommandButton(
                "Delve", "delve", "\U0001F5FA",
                discord.ButtonStyle.primary, ctx, row=2,
            ))
        # Always-on jump to the multi-game hub for everything else.
        self.add_item(_RunCommandButton(
            "Game Hub", "menu", "\U0001F3AE",
            discord.ButtonStyle.secondary, ctx, row=3,
        ))
        self.add_item(_RunCommandButton(
            "My Profile", "me", "\U0001F4DC",
            discord.ButtonStyle.secondary, ctx, row=3,
        ))


# =============================================================================
# Tabbed ,start interface
# =============================================================================
# Each tab is a small bundle: (key, label, emoji, fetch, render, actions).
# StartInterfaceView keeps a Select dropdown for tab nav across the top, the
# rendered embed of the active tab, and a refresh button. Per-tab action
# buttons are attached when the tab is selected and replaced on switch.
#
# Buttons re-invoke prefix commands via process_commands (same pattern as
# _RunCommandButton + the FuzzyView in core.framework.bot) so middleware (cooldowns,
# registration, module gates) all fire as if the user typed it.

_TAB_KEYS = (
    "home", "guide", "wallet", "market", "fishing", "farming",
    "delve", "buddy", "crafting", "stones",
)
_TAB_META: dict[str, tuple[str, str, str]] = {
    "home":     ("Home",     "\U0001F3E0", "Personalised dashboard + next steps"),
    "guide":    ("Guide",    "\U0001F4D6", "How to play each minigame in 30 seconds"),
    "wallet":   ("Wallet",   "\U0001F4B5", "Balance, holdings, daily / work claim"),
    "market":   ("Market",   "\U0001F4CA", "Top crypto prices + swap"),
    "fishing":  ("Fishing",  "\U0001F3A3", "Cast lines, sell catch, stake LURE"),
    "farming":  ("Farming",  "\U0001F33E", "Plant, water, harvest, sell crops"),
    "delve":    ("Dungeon",  "\U0001F5FA", "Crawl floors, mine ore, stake RUNE"),
    "buddy":    ("Buddy",    "\U0001F436", "Buddies, storage, nest, arena, BUD shop"),
    "crafting": ("Crafting", "\U0001F528", "Recipes, specialties, INGOT stake -> FORGE"),
    "stones":   ("Stones",   "\U0001F48E", "Leveled gems + auto-levelup status"),
}


async def _tab_fetch_guide(ctx: DiscoContext) -> dict:
    """Static. The Guide tab carries no per-player state -- it's the
    same 30-second playbook for every minigame."""
    return {}


def _tab_render_guide(ctx: DiscoContext, st: dict) -> discord.Embed:
    """Centralised cheatsheet for every minigame surface: 4-5 line
    "what to do, what you earn, how to cash out" blurbs sequenced in
    rough onboarding order. Pure text -- no DB reads. The deeper docs
    live in ``,help <topic>``; the buttons below jump straight there.
    """
    prefix = ctx.prefix or Config.PREFIX
    b = card(
        "\U0001F4D6 Game Guide",
        color=C_GOLD,
        description=(
            "30-second playbook for every minigame. Tap a help button "
            "below for the full reference. New player? The `Home` tab "
            "shows next-steps tuned to your progress."
        ),
    )
    b.field(
        "\U0001F3A3 Fishing  (Lure Network)",
        f"`{prefix}fish` casts a line for fish. Sell the haul with "
        f"`{prefix}fish sell` for **LURE**. Stake LURE with "
        f"`{prefix}fish stake` to drip **REEL** passively. Burn-swap "
        f"LURE -> REEL via `{prefix}fish swap`, cash REEL -> USD via "
        f"`{prefix}fish cashout`. Buy bait + rods in `{prefix}fish shop`. "
        f"Stones: \U0001F3A3 Tidestone (REEL).",
        False,
    )
    b.field(
        "\U0001F33E Farming  (Harvest Network)",
        f"`{prefix}farm plant <slot> <crop>` -> `{prefix}farm water` -> "
        f"`{prefix}farm harvest`. Sell crops with `{prefix}farm sell` for "
        f"**HRV**, drop **SEED** every harvest. Stake SEED with "
        f"`{prefix}farm stake` to drip HRV. Cash HRV -> USD via "
        f"`{prefix}farm cashout`. Bag: `{prefix}farm bag` (alias "
        f"`{prefix}farm inv`). Stones: \U0001F33C Bloomstone (HRV).",
        False,
    )
    b.field(
        "\U0001F5FA Dungeon  (Crypt Network)",
        f"`{prefix}delve class warrior|mage|rogue` once, then "
        f"`{prefix}delve` to enter. Tap **Next** to walk, **Attack** to "
        f"fight, **Mine** for ore. Stake ore (`{prefix}delve stake "
        f"<ore> <amt>`) to drip **RUNE**. Cash RUNE -> USD via "
        f"`{prefix}delve cashout`. Wild buddies can drop -- catch them "
        f"into your shelter. Stones: \U0001F48E Cryptstone (RUNE).",
        False,
    )
    b.field(
        "\U0001F528 Crafting  (Forge Network)",
        f"`{prefix}craft list` -- recipes you can currently make. "
        f"`{prefix}craft make <key>` consumes inputs from fishing / "
        f"farming / dungeon, mints **INGOT**, deposits the crafted item "
        f"in your bag. `{prefix}craft apply <key>` ships it back into "
        f"the source game (bait into fishing, etc). Stake INGOT "
        f"(`{prefix}craft stake`) to drip **FORGE**, cash FORGE -> USD "
        f"via `{prefix}craft cashout`. Five specialties level "
        f"independently -- see `{prefix}craft specialties`.",
        False,
    )
    b.field(
        "\U0001F436 Buddies  (Buddy Network)",
        f"`{prefix}buddy hatch` to get your first buddy. Talk / pet / "
        f"feed it for **FREN** drops, level it through chat XP. Stake "
        f"FREN or BBT (`{prefix}buddy stake fren|bbt|everything`) to "
        f"drip **BUD**. Spare buddies -> `{prefix}buddy storage` "
        f"(no decay, no slot use). Breed two parents in "
        f"`{prefix}buddy nest deposit <id1> <id2>` for an egg. "
        f"Cash BUD out via `{prefix}buddy cashout`.",
        False,
    )
    b.field(
        "\U0001F3DF Arena  (cross-game battle currency)",
        f"`{prefix}buddy arena fight` -- pit your active buddy against "
        f"server-mates for a **BBT + BUD** payout (BBT is the headline, "
        f"BUD is a small drip on top). Tier ladder: Bronze -> Silver -> "
        f"Gold -> Platinum -> Diamond. Wild battles in fish/farm/delve "
        f"also mint BBT. `{prefix}lb arena` for the wins board. "
        f"`{prefix}buddy arena` alone shows the help panel.\n"
        f"`{prefix}delve arena fight` -- separate delve-side PvP "
        f"ladder (Copper/Silver/Gold/Rune) paid in ore + RUNE; "
        f"`{prefix}delve arena duel @user` for live duels.",
        False,
    )
    b.field(
        "\U0001F3DB Auction House  (cross-game marketplace)",
        f"`{prefix}ah list <kind> <ref> [qty] <price> [currency]` -- "
        f"list **buddies / eggs / fish / crops / ore / weapons / armors / "
        f"consumables / crafted items**. `{prefix}ah browse [kind] "
        f"[--max=N] [--sort=cheapest|expiring|newest]` to find a buy. "
        f"`{prefix}ah buy <id> [pay_currency]` -- direct trade in the "
        f"listed currency or cross-currency with AMM slippage. Items are "
        f"NFT-style tokens (e.g. `bud:k889ka2c`); inspect any one with "
        f"`{prefix}ah token <id>`. 5% house fee, 7-day default expiry, "
        f"DMs on settle.",
        False,
    )
    b.field(
        "\U0001F4D6 Deeper docs",
        f"`{prefix}help fishing` `{prefix}help farming` "
        f"`{prefix}help dungeon` `{prefix}help crafting` "
        f"`{prefix}help bud` `{prefix}help stones` "
        f"`{prefix}help auction` `{prefix}help currencies`",
        False,
    )
    b.footer(
        "Use the dropdown to switch tabs, or the buttons below to jump "
        "into a help page."
    )
    return b.build()


def _tab_actions_guide(
    ctx: DiscoContext, st: dict,
) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    """Quick-jump buttons into the per-game help pages. Capped at 5 by
    StartInterfaceView; the Auction option lives on the trim line so a
    new player still sees the four core games AND the marketplace.
    """
    return [
        ("Fishing",  "help fishing",  "\U0001F3A3", discord.ButtonStyle.secondary),
        ("Farming",  "help farming",  "\U0001F33E", discord.ButtonStyle.secondary),
        ("Dungeon",  "help dungeon",  "\U0001F5FA", discord.ButtonStyle.secondary),
        ("Crafting", "help crafting", "\U0001F528", discord.ButtonStyle.secondary),
        ("Auction",  "help auction",  "\U0001F3DB", discord.ButtonStyle.secondary),
    ]


async def _tab_fetch_wallet(ctx: DiscoContext) -> dict:
    db, uid, gid = ctx.db, ctx.author.id, ctx.guild_id
    user_row = await db.get_user(uid, gid) or {}
    wallet_h = user_row.h("wallet") if user_row else 0.0
    bank_h = user_row.h("bank") if user_row else 0.0
    last_daily = user_row.get("last_daily")
    last_work = user_row.get("last_work")
    streak = int(user_row.get("daily_streak") or 0)
    holdings: list[tuple[str, float, float]] = []
    try:
        rows = await db.fetch_all(
            "SELECT symbol, amount FROM wallet_holdings "
            "WHERE user_id=$1 AND guild_id=$2 AND amount > 0 "
            "ORDER BY amount DESC LIMIT 6",
            uid, gid,
        )
        for r in rows or []:
            sym = str(r.get("symbol") or "")
            amt = r.h("amount")
            try:
                p = await db.get_price(sym, gid)
                price = float((p or {}).get("price") or 0.0)
            except Exception:
                price = 0.0
            holdings.append((sym, amt, amt * price))
    except Exception:
        log.debug("start wallet tab: holdings fetch failed", exc_info=True)
    return {
        "wallet_h":   float(wallet_h or 0.0),
        "bank_h":     float(bank_h or 0.0),
        "last_daily": last_daily,
        "last_work":  last_work,
        "streak":     streak,
        "holdings":   holdings,
    }


def _tab_render_wallet(ctx: DiscoContext, st: dict) -> discord.Embed:
    daily_status = (
        "\U00002705 Ready" if _daily_ready(st["last_daily"])
        else "\U000023F3 On cooldown"
    )
    import time as _time, datetime as _dt
    work_status = "\U00002705 Ready"
    if st["last_work"] is not None:
        ts = (
            st["last_work"].timestamp()
            if isinstance(st["last_work"], _dt.datetime)
            else float(st["last_work"])
        )
        if (_time.time() - ts) < 3600:
            work_status = "\U000023F3 On cooldown"
    b = card(
        "\U0001F4B5 Wallet  -  Discoin Hub",
        color=C_GOLD,
        description=(
            f"USD balances, top crypto holdings, and per-day claim "
            f"status all in one place. Buttons run the listed command "
            f"verbatim so cooldowns + module gates apply normally."
        ),
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    b.field("\U0001F4B5 Wallet", f"**{fmt_usd(st['wallet_h'])}**", True)
    b.field("\U0001F3E6 Bank", f"**{fmt_usd(st['bank_h'])}**", True)
    b.field(
        "\U0001F4B0 Daily",
        f"{daily_status}  ·  streak **{st['streak']}d**",
        True,
    )
    b.field("\U0001F4BC Work", work_status, True)
    if st["holdings"]:
        lines = []
        for sym, amt, usd in st["holdings"]:
            usd_tag = f"  ~ {fmt_usd(usd)}" if usd > 0 else ""
            lines.append(f"`{sym:<5}` **{amt:,.4f}**{usd_tag}")
        b.field("\U0001F48E Top holdings", "\n".join(lines), False)
    return b.build()


def _tab_actions_wallet(ctx: DiscoContext, st: dict) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    actions: list[tuple[str, str, str, discord.ButtonStyle]] = []
    if _daily_ready(st["last_daily"]):
        actions.append(("Daily", "daily", "\U0001F4B0", discord.ButtonStyle.success))
    actions.append(("Work", "work", "\U0001F4BC", discord.ButtonStyle.primary))
    actions.append(("Profile", "bal", "\U0001F4DC", discord.ButtonStyle.secondary))
    actions.append(("Faucet", "airdrop", "\U0001F4A7", discord.ButtonStyle.secondary))
    return actions


async def _tab_fetch_market(ctx: DiscoContext) -> dict:
    db, gid = ctx.db, ctx.guild_id
    rows = []
    try:
        rows = await db.fetch_all(
            "SELECT symbol, price, open_price FROM crypto_prices "
            "WHERE guild_id=$1 AND price > 0 ORDER BY price DESC LIMIT 8",
            gid,
        )
    except Exception:
        log.debug("start market tab: prices fetch failed", exc_info=True)
    out: list[tuple[str, float, float]] = []
    for r in rows or []:
        sym = str(r.get("symbol") or "")
        px = float(r.get("price") or 0.0)
        op = float(r.get("open_price") or px)
        delta_pct = ((px - op) / op * 100.0) if op > 0 else 0.0
        out.append((sym, px, delta_pct))
    return {"prices": out}


def _tab_render_market(ctx: DiscoContext, st: dict) -> discord.Embed:
    b = card(
        "\U0001F4CA Market  -  Discoin Hub",
        color=C_TEAL,
        description=(
            "Live oracle prices for every tradeable token in this "
            "guild. Hit `,chart <pair>` for a TA view or `,buy` / "
            "`,sell` to trade against the oracle."
        ),
    )
    if not st["prices"]:
        b.field("Prices", "_(market not seeded yet)_", False)
        return b.build()
    lines = []
    for sym, px, dp in st["prices"]:
        sign = "+" if dp >= 0 else ""
        lines.append(f"`{sym:<5}` **{fmt_usd(px)}**  ·  {sign}{dp:.2f}%")
    b.field("\U0001F4C8 Top prices (24h delta)", "\n".join(lines), False)
    return b.build()


def _tab_actions_market(ctx: DiscoContext, st: dict) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    return [
        ("Crypto",   "crypto",   "\U0001F4B9", discord.ButtonStyle.primary),
        ("Charts",   "chart help", "\U0001F4CA", discord.ButtonStyle.secondary),
        ("Buy",      "buy help", "\U0001F7E2", discord.ButtonStyle.secondary),
        ("Sell",     "sell help", "\U0001F534", discord.ButtonStyle.secondary),
        ("Pools",    "trade pool list", "\U0001F30A", discord.ButtonStyle.secondary),
    ]


async def _tab_fetch_fishing(ctx: DiscoContext) -> dict:
    db, uid, gid = ctx.db, ctx.author.id, ctx.guild_id
    row = {}
    try:
        row = await db.fetch_one(
            "SELECT total_caught, biggest_lbs, current_combo, "
            "rod_key, bait_inventory, "
            "CASE WHEN last_beachcomb_at IS NULL THEN NULL "
            "     ELSE EXTRACT(EPOCH FROM (NOW() - last_beachcomb_at))::INTEGER "
            "END AS beachcomb_elapsed_s "
            "FROM user_fishing WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        ) or {}
    except Exception:
        log.debug("start fishing tab: user_fishing fetch failed", exc_info=True)
    bait_inv = row.get("bait_inventory") or {}
    if isinstance(bait_inv, str):
        try:
            bait_inv = json.loads(bait_inv) if bait_inv else {}
        except Exception:
            bait_inv = {}
    bait_total = sum(int(v or 0) for v in bait_inv.values())
    lure_h = reel_h = 0.0
    try:
        for sym, key in (("LURE", "lure_h"), ("REEL", "reel_h")):
            wh = await db.get_wallet_holding(uid, gid, "lur", sym)
            row_h = to_human(int((wh or {}).get("amount") or 0))
            if key == "lure_h":
                lure_h = row_h
            else:
                reel_h = row_h
    except Exception:
        pass
    tide = None
    try:
        tide = await db.get_tidestone(uid, gid)
    except Exception:
        pass
    beachcomb_ready = True
    beachcomb_wait_s = 0
    try:
        import configs.fishing_config as _fc
        elapsed = row.get("beachcomb_elapsed_s")
        if elapsed is not None:
            elapsed_i = int(elapsed)
            if elapsed_i < int(_fc.BEACHCOMB_COOLDOWN_S):
                beachcomb_ready = False
                beachcomb_wait_s = int(_fc.BEACHCOMB_COOLDOWN_S - elapsed_i)
    except Exception:
        log.debug("start fishing tab: beachcomb cooldown probe failed", exc_info=True)
    return {
        "total_caught": int(row.get("total_caught") or 0),
        "biggest_lbs":  float(row.get("biggest_lbs") or 0.0),
        "combo":        int(row.get("current_combo") or 0),
        "rod_key":      str(row.get("rod_key") or "basic"),
        "bait_total":   bait_total,
        "lure_h":       float(lure_h),
        "reel_h":       float(reel_h),
        "tidestone_lvl": int((tide or {}).get("level") or 0) if tide else 0,
        "beachcomb_ready":  beachcomb_ready,
        "beachcomb_wait_s": beachcomb_wait_s,
    }


def _tab_render_fishing(ctx: DiscoContext, st: dict) -> discord.Embed:
    b = card(
        "\U0001F3A3 Fishing  -  Discoin Hub",
        color=C_TEAL,
        description=(
            "The Lure Network. Cast lines for fish, sell on land "
            "for **LURE**, stake LURE to mint **REEL** passively, "
            "burn-swap REEL <-> USD via the Lure cashout."
        ),
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    b.field(
        "\U0001F4CA Stats",
        f"Caught **{st['total_caught']:,}**  ·  "
        f"Biggest **{st['biggest_lbs']:,.2f} lb**  ·  "
        f"Combo **{st['combo']}**",
        False,
    )
    b.field("\U0001F3A3 Rod", f"**{st['rod_key'].title()}**", True)
    b.field("\U0001FAB1 Bait packs", f"**{st['bait_total']:,}**", True)
    b.field(
        "\U0001F30A Tidestone",
        f"Lv **{st['tidestone_lvl']}**" if st["tidestone_lvl"] else "_(none)_",
        True,
    )
    b.field(
        "\U0001F4B0 Token bag",
        f"**{st['lure_h']:,.4f}** LURE  ·  "
        f"**{st['reel_h']:,.4f}** REEL",
        False,
    )
    return b.build()


def _tab_actions_fishing(ctx: DiscoContext, st: dict) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    actions: list[tuple[str, str, str, discord.ButtonStyle]] = [
        ("Cast",   "fish",       "\U0001F3A3", discord.ButtonStyle.primary),
        ("Sell",   "fish sell",  "\U0001F4B0", discord.ButtonStyle.success),
    ]
    if st.get("beachcomb_ready"):
        actions.append((
            "Beachcomb", "fish beachcomb", "\U0001F3DD",
            discord.ButtonStyle.success,
        ))
    actions.append(("Shop",   "fish shop",  "\U0001F3EA", discord.ButtonStyle.secondary))
    actions.append(("Stake",  "fish stake", "\U0001F30A", discord.ButtonStyle.secondary))
    actions.append(("Stats",  "fish stats", "\U0001F4CA", discord.ButtonStyle.secondary))
    return actions


async def _tab_fetch_farming(ctx: DiscoContext) -> dict:
    db, uid, gid = ctx.db, ctx.author.id, ctx.guild_id
    row = {}
    try:
        row = await db.fetch_one(
            "SELECT total_planted, total_harvested, plot_count, plot_tier, "
            "plots, seed_packets, daily_contract, total_contracts_completed, "
            "total_forages, "
            "CASE WHEN last_forage_at IS NULL THEN NULL "
            "     ELSE EXTRACT(EPOCH FROM (NOW() - last_forage_at))::INTEGER "
            "END AS forage_elapsed_s "
            "FROM user_farming WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        ) or {}
    except Exception:
        log.debug("start farming tab: user_farming fetch failed", exc_info=True)
    plots = row.get("plots") or []
    if isinstance(plots, str):
        try:
            plots = json.loads(plots) if plots else []
        except Exception:
            plots = []
    seeds = row.get("seed_packets") or {}
    if isinstance(seeds, str):
        try:
            seeds = json.loads(seeds) if seeds else {}
        except Exception:
            seeds = {}
    contract = row.get("daily_contract") or {}
    if isinstance(contract, str):
        try:
            contract = json.loads(contract) if contract else {}
        except Exception:
            contract = {}
    n_empty = sum(1 for p in plots if (p or {}).get("state") == "empty")
    n_growing = sum(1 for p in plots if (p or {}).get("state") == "growing")
    n_ready = sum(1 for p in plots if (p or {}).get("state") == "ready")
    n_mutated = sum(
        1 for p in plots
        if (p or {}).get("mutation") and (p or {}).get("state") in ("growing", "ready")
    )
    seed_total = sum(int(v or 0) for v in seeds.values())
    bloom = None
    try:
        bloom = await db.get_bloomstone(uid, gid)
    except Exception:
        pass
    hrv_h = 0.0
    try:
        wh = await db.get_wallet_holding(uid, gid, "har", "HRV")
        hrv_h = to_human(int((wh or {}).get("amount") or 0))
    except Exception:
        pass
    # Forage cooldown probe -- mirrors the in-cog ,farm forage gate so
    # the home tab can show the button only when ready.
    forage_ready = True
    forage_wait_s = 0
    try:
        import configs.farming_config as _fc
        elapsed = row.get("forage_elapsed_s")
        if elapsed is not None:
            elapsed_i = int(elapsed)
            if elapsed_i < int(_fc.FORAGE_COOLDOWN_S):
                forage_ready = False
                forage_wait_s = int(_fc.FORAGE_COOLDOWN_S - elapsed_i)
    except Exception:
        log.debug("start farming tab: forage cooldown probe failed", exc_info=True)
    # Daily contract status: needs work iff today's contract is set,
    # not yet completed, and the player has at least one of the crop in
    # their bag (so the "Turn In" button only appears when actionable).
    crops_inv = row.get("crop_inventory") if isinstance(row, dict) else None
    if crops_inv is None:
        try:
            inv_row = await db.fetch_one(
                "SELECT crop_inventory FROM user_farming "
                "WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            ) or {}
            crops_inv = inv_row.get("crop_inventory") or {}
        except Exception:
            crops_inv = {}
    if isinstance(crops_inv, str):
        try:
            crops_inv = json.loads(crops_inv) if crops_inv else {}
        except Exception:
            crops_inv = {}
    contract_crop_key = str(contract.get("crop_key") or "")
    contract_completed = bool(contract.get("completed"))
    contract_required = int(contract.get("qty_required") or 0)
    contract_delivered = int(contract.get("qty_delivered") or 0)
    contract_have = int((crops_inv or {}).get(contract_crop_key, 0) or 0)
    contract_actionable = (
        bool(contract_crop_key)
        and not contract_completed
        and contract_have > 0
        and contract_delivered < contract_required
    )
    return {
        "total_planted":   int(row.get("total_planted") or 0),
        "total_harvested": int(row.get("total_harvested") or 0),
        "plot_count":      int(row.get("plot_count") or 1),
        "plot_tier":       int(row.get("plot_tier") or 1),
        "n_empty":         n_empty,
        "n_growing":       n_growing,
        "n_ready":         n_ready,
        "n_mutated":       n_mutated,
        "seed_total":      seed_total,
        "bloom_lvl":       int((bloom or {}).get("level") or 0) if bloom else 0,
        "hrv_h":           float(hrv_h),
        "forage_ready":    bool(forage_ready),
        "forage_wait_s":   int(forage_wait_s),
        "total_forages":   int(row.get("total_forages") or 0),
        "contract":        dict(contract or {}),
        "contract_actionable": bool(contract_actionable),
        "contract_have":   int(contract_have),
        "total_contracts_completed": int(row.get("total_contracts_completed") or 0),
    }


def _tab_render_farming(ctx: DiscoContext, st: dict) -> discord.Embed:
    b = card(
        "\U0001F33E Farming  -  Discoin Hub",
        color=C_GOLD,
        description=(
            "The Harvest Network. Plant seeds, weather seasons, "
            "harvest crops, sell for **HRV**. Use `,farm plant all "
            "<crop>` to fill every empty plot in one shot."
        ),
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    b.field(
        "\U0001F4CA Lifetime",
        f"Planted **{st['total_planted']:,}**  ·  "
        f"Harvested **{st['total_harvested']:,}**  ·  "
        f"Forages **{st['total_forages']:,}**  ·  "
        f"Contracts **{st['total_contracts_completed']:,}**",
        False,
    )
    plots_line = (
        f"**{st['plot_count']}** total (tier {st['plot_tier']})\n"
        f"\U00002B1C Empty {st['n_empty']}  ·  "
        f"\U0001F331 Growing {st['n_growing']}  ·  "
        f"\U00002728 Ready {st['n_ready']}"
    )
    if st.get("n_mutated"):
        plots_line += f"\n\U00002728 **{st['n_mutated']} mutated** in field"
    b.field("\U0001F33F Plots", plots_line, True)
    b.field("\U0001F4E6 Seed packets", f"**{st['seed_total']:,}**", True)
    b.field(
        "\U0001F33C Bloomstone",
        f"Lv **{st['bloom_lvl']}**" if st["bloom_lvl"] else "_(none)_",
        True,
    )
    # Daily contract -- one rolling NPC order per UTC day. Rendered even
    # when the contract is unset so the today panel hints at the system.
    contract = st.get("contract") or {}
    if contract.get("crop_key"):
        try:
            import configs.farming_config as _fc
            cmeta = _fc.crop_meta(str(contract.get("crop_key"))) or {}
        except Exception:
            cmeta = {}
        required = int(contract.get("qty_required") or 0)
        delivered = int(contract.get("qty_delivered") or 0)
        if contract.get("completed"):
            line = (
                f"\U00002705 **{cmeta.get('name', contract.get('crop_key'))}** "
                f"x{required} delivered. Resets at UTC midnight."
            )
        else:
            line = (
                f"{cmeta.get('emoji', '')} **{cmeta.get('name', contract.get('crop_key'))}** "
                f"x{required}  ·  delivered **{delivered}/{required}**\n"
                f"In your bag: **{st.get('contract_have', 0)}**"
            )
        b.field("\U0001F4E6 Today's contract", line, False)
    # Forage cooldown surface so the player sees both the button cue
    # and the wait time when it's gated.
    if st["forage_ready"]:
        b.field("\U0001F33F Forage", "**Ready** -- run `,farm forage`.", True)
    else:
        wait = st["forage_wait_s"]
        b.field(
            "\U0001F33F Forage",
            f"Next in **{wait // 60}m {wait % 60}s**.",
            True,
        )
    b.field("\U0001F4B0 HRV", f"**{st['hrv_h']:,.4f}**", True)
    return b.build()


def _tab_actions_farming(ctx: DiscoContext, st: dict) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    actions: list[tuple[str, str, str, discord.ButtonStyle]] = [
        ("Field", "farm", "\U0001F33E", discord.ButtonStyle.primary),
    ]
    if st["n_ready"] > 0:
        actions.append(("Harvest", "farm harvest", "\U00002728", discord.ButtonStyle.success))
    if st.get("contract_actionable"):
        actions.append((
            "Turn In", "farm contract turnin", "\U0001F4E6",
            discord.ButtonStyle.success,
        ))
    if st["forage_ready"]:
        actions.append((
            "Forage", "farm forage", "\U0001F33F",
            discord.ButtonStyle.success,
        ))
    if st["n_empty"] > 0 and st["seed_total"] > 0:
        actions.append(("Plant All Wheat", "farm plant all wheat", "\U0001F33E", discord.ButtonStyle.primary))
    if st["n_growing"] > 0:
        actions.append(("Water", "farm water", "\U0001F4A7", discord.ButtonStyle.secondary))
    actions.append(("Contract", "farm contract", "\U0001F4DD", discord.ButtonStyle.secondary))
    actions.append(("Shop", "farm shop", "\U0001F3EA", discord.ButtonStyle.secondary))
    actions.append(("Stake", "farm stake", "\U0001F331", discord.ButtonStyle.secondary))
    return actions


async def _tab_fetch_delve(ctx: DiscoContext) -> dict:
    db, uid, gid = ctx.db, ctx.author.id, ctx.guild_id
    row = {}
    try:
        row = await db.fetch_one(
            "SELECT class_key, level, xp, current_hp, hp_max, "
            "current_floor, current_room, deepest_floor, total_runs, "
            "total_kills, total_captures, "
            "copper_staked_raw, silver_staked_raw, gold_staked_raw, "
            "run_id, current_room_type, "
            "relics_owned, equipped_relic, run_curse, "
            "total_curses_completed, total_shrines_visited, "
            "CASE WHEN last_scavenge_at IS NULL THEN NULL "
            "     ELSE EXTRACT(EPOCH FROM (NOW() - last_scavenge_at))::INTEGER "
            "END AS scavenge_elapsed_s "
            "FROM user_dungeon WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        ) or {}
    except Exception:
        log.debug("start delve tab: user_dungeon fetch failed", exc_info=True)
    rune_h = 0.0
    try:
        wh = await db.get_wallet_holding(uid, gid, "cry", "RUNE")
        rune_h = to_human(int((wh or {}).get("amount") or 0))
    except Exception:
        pass
    crypt_lvl = 0
    try:
        cs = await db.get_cryptstone(uid, gid)
        crypt_lvl = int((cs or {}).get("level") or 0) if cs else 0
    except Exception:
        pass
    in_run = bool(row.get("run_id"))
    relics_owned = row.get("relics_owned") or {}
    if isinstance(relics_owned, str):
        try:
            relics_owned = json.loads(relics_owned) if relics_owned else {}
        except Exception:
            relics_owned = {}
    n_relics = sum(int(v or 0) for v in (relics_owned or {}).values())
    scavenge_ready = True
    scavenge_wait_s = 0
    try:
        import configs.dungeon_config as _dcc
        elapsed = row.get("scavenge_elapsed_s")
        if elapsed is not None:
            elapsed_i = int(elapsed)
            if elapsed_i < int(_dcc.SCAVENGE_COOLDOWN_S):
                scavenge_ready = False
                scavenge_wait_s = int(_dcc.SCAVENGE_COOLDOWN_S - elapsed_i)
    except Exception:
        log.debug("start delve tab: scavenge cooldown probe failed", exc_info=True)
    return {
        "class_key":     str(row.get("class_key") or ""),
        "level":         int(row.get("level") or 0),
        "xp":            int(row.get("xp") or 0),
        "hp":            int(row.get("current_hp") or 0),
        "hp_max":        int(row.get("hp_max") or 0),
        "floor":         int(row.get("current_floor") or 0),
        "room":          int(row.get("current_room") or 0),
        "deepest":       int(row.get("deepest_floor") or 0),
        "total_runs":    int(row.get("total_runs") or 0),
        "total_kills":   int(row.get("total_kills") or 0),
        "total_caps":    int(row.get("total_captures") or 0),
        "copper_h":      row.h("copper_staked_raw"),
        "silver_h":      row.h("silver_staked_raw"),
        "gold_h":        row.h("gold_staked_raw"),
        "rune_h":        float(rune_h),
        "in_run":        in_run,
        "room_type":     str(row.get("current_room_type") or ""),
        "crypt_lvl":     crypt_lvl,
        "equipped_relic": str(row.get("equipped_relic") or ""),
        "n_relics":      int(n_relics),
        "run_curse":     str(row.get("run_curse") or ""),
        "total_curses_completed": int(row.get("total_curses_completed") or 0),
        "total_shrines_visited":  int(row.get("total_shrines_visited") or 0),
        "shrine_in_room":  bool(in_run and str(row.get("current_room_type") or "") == "shrine"),
        "chest_in_room":   bool(in_run and str(row.get("current_room_type") or "") == "chest"),
        "scavenge_ready":  scavenge_ready,
        "scavenge_wait_s": scavenge_wait_s,
    }


def _tab_render_delve(ctx: DiscoContext, st: dict) -> discord.Embed:
    b = card(
        "\U0001F5FA Dungeon  -  Discoin Hub",
        color=C_AMBER,
        description=(
            "The Crypt Network. Pick a class, descend floors, slay or "
            "tame mobs, mine ore for **RUNE**. Burn-swap RUNE <-> USD "
            "via the Crypt cashout."
        ),
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    if not st["class_key"]:
        b.field(
            "\U0001F3F0 Status",
            "_No class picked yet. Use `,delve class warrior|mage|rogue` "
            "to commit, then `,delve start` to enter._",
            False,
        )
        return b.build()
    b.field(
        "\U00002694 Adventurer",
        f"**{st['class_key'].title()}**  ·  Lv **{st['level']}**  ·  "
        f"HP **{st['hp']}/{st['hp_max']}**",
        False,
    )
    if st["in_run"]:
        b.field(
            "\U0001F30C Current run",
            f"Floor **{st['floor']}**  ·  Room **{st['room']}**  ·  "
            f"Type: `{st['room_type'] or '?'}`",
            False,
        )
    else:
        b.field(
            "\U0001F3F0 At surface",
            f"Deepest reached: **F{st['deepest']}**. Run `,delve start` "
            f"to dive again.",
            False,
        )
    b.field(
        "\U0001F4CA Lifetime",
        f"Runs **{st['total_runs']:,}**  ·  "
        f"Kills **{st['total_kills']:,}**  ·  "
        f"Tames **{st['total_caps']:,}**\n"
        f"Cursed runs cleared **{st['total_curses_completed']:,}**  ·  "
        f"Shrines visited **{st['total_shrines_visited']:,}**",
        False,
    )
    b.field(
        "\U0001F4B0 Ore stake",
        f"\U0001F7E4 {st['copper_h']:,.2f} COPPER\n"
        f"\U0001F4BF {st['silver_h']:,.2f} SILVER\n"
        f"\U0001F947 {st['gold_h']:,.2f} GOLD",
        True,
    )
    b.field(
        "\U0001FAA8 RUNE bag",
        f"**{st['rune_h']:,.4f} RUNE**",
        True,
    )
    b.field(
        "\U0001F48E Cryptstone",
        f"Lv **{st['crypt_lvl']}**" if st["crypt_lvl"] else "_(none)_",
        True,
    )
    # Equipped relic + active curse. Both dovetail with the new room-type
    # action buttons below so the player can see what they're carrying
    # without bouncing into ,delve relic / ,delve curse.
    try:
        import configs.dungeon_config as _dc
        relic_meta = _dc.relic_meta(st.get("equipped_relic")) or {}
        curse_meta = _dc.curse_meta(st.get("run_curse")) or {}
    except Exception:
        relic_meta, curse_meta = {}, {}
    if relic_meta:
        b.field(
            "\U0001F48E Equipped relic",
            f"{relic_meta.get('emoji', '')} **{relic_meta.get('name', '?')}** "
            f"({str(relic_meta.get('rarity', 'common')).title()})\n"
            f"_{relic_meta.get('blurb', '')}_  ·  Owned: **{st['n_relics']}**",
            False,
        )
    elif st["n_relics"] > 0:
        b.field(
            "\U0001F48E Relics",
            f"**{st['n_relics']}** owned, none equipped. `,delve relic equip <key>`",
            False,
        )
    if curse_meta:
        b.field(
            "\U0001F480 Curse armed",
            f"{curse_meta.get('emoji', '')} **{curse_meta.get('name', '?')}** "
            f"-- _{curse_meta.get('blurb', '')}_",
            False,
        )
    if st.get("shrine_in_room"):
        b.field(
            "\U0001F64F Shrine in this room",
            "Run `,delve pray` to receive a boon (or a curse).",
            False,
        )
    return b.build()


def _tab_actions_delve(ctx: DiscoContext, st: dict) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    if not st["class_key"]:
        return [
            ("Pick Warrior", "delve class warrior", "\U00002694", discord.ButtonStyle.primary),
            ("Pick Mage",    "delve class mage",    "\U0001F9D9", discord.ButtonStyle.primary),
            ("Pick Rogue",   "delve class rogue",   "\U0001F977", discord.ButtonStyle.primary),
        ]
    actions: list[tuple[str, str, str, discord.ButtonStyle]] = [
        ("Status", "delve", "\U0001F5FA", discord.ButtonStyle.primary),
    ]
    if st["in_run"]:
        if st["room_type"] in ("mob", "boss"):
            actions.append(("Attack", "delve attack", "\U00002694", discord.ButtonStyle.danger))
            actions.append(("Skill",  "delve skill",  "\U0001F4AB", discord.ButtonStyle.danger))
        elif st["room_type"] == "ore":
            actions.append(("Mine", "delve mine", "\U000026CF", discord.ButtonStyle.success))
        elif st["room_type"] == "shrine":
            actions.append(("Pray", "delve pray", "\U0001F64F", discord.ButtonStyle.success))
        elif st["room_type"] == "chest":
            actions.append(("Open", "delve open", "\U0001F4B0", discord.ButtonStyle.success))
        else:
            actions.append(("Next", "delve next", "\U000027A1", discord.ButtonStyle.primary))
    else:
        actions.append(("Start", "delve start", "\U0001F3AC", discord.ButtonStyle.success))
    # Surface-side wandering loot pickup. Only surfaces when the
    # 10-minute cooldown is satisfied so the button never errors out.
    if not st["in_run"] and st.get("scavenge_ready"):
        actions.append((
            "Scavenge", "delve scavenge", "\U0001F50E",
            discord.ButtonStyle.success,
        ))
    # Relic + Curse browsers. Always visible once a class is picked so
    # the player learns the system; equip / arm flow lives behind them.
    actions.append(("Relics", "delve relic", "\U0001F48E", discord.ButtonStyle.secondary))
    actions.append(("Curses", "delve curse", "\U0001F480", discord.ButtonStyle.secondary))
    actions.append(("Shop",   "delve shop",  "\U0001F3EA", discord.ButtonStyle.secondary))
    actions.append(("Stake",  "delve stake", "\U0001F510", discord.ButtonStyle.secondary))
    return actions


async def _tab_fetch_buddy(ctx: DiscoContext) -> dict:
    db, uid, gid = ctx.db, ctx.author.id, ctx.guild_id
    active = {}
    try:
        active = await db.fetch_one(
            "SELECT id, name, species, rarity_tier, level, xp, "
            "happiness, hunger, energy "
            "FROM cc_buddies "
            "WHERE owner_user_id=$1 AND guild_id=$2 "
            "  AND status='owned' AND is_active=TRUE LIMIT 1",
            uid, gid,
        ) or {}
    except Exception:
        log.debug("start buddy tab: cc_buddies fetch failed", exc_info=True)
    n_owned = n_stored = 0
    try:
        n_owned = int((await db.fetch_one(
            "SELECT COUNT(*) AS n FROM cc_buddies "
            "WHERE owner_user_id=$1 AND guild_id=$2 AND status='owned'",
            uid, gid,
        ) or {}).get("n") or 0)
    except Exception:
        pass
    try:
        n_stored = int((await db.fetch_one(
            "SELECT COUNT(*) AS n FROM cc_buddies "
            "WHERE owner_user_id=$1 AND guild_id=$2 AND status='stored'",
            uid, gid,
        ) or {}).get("n") or 0)
    except Exception:
        pass
    daycare_status: dict | None = None
    try:
        # Multi-slot nests post-migration 0215: pull the next-ready row
        # so the start-tab summary reads as "the closest egg" rather than
        # arbitrarily picking one of N parallel slots.
        daycare_status = await db.fetch_one(
            "SELECT egg_species, egg_rarity_tier, "
            "       GREATEST(0, EXTRACT(EPOCH FROM (egg_ready_at - NOW()))::bigint) "
            "       AS seconds_remaining "
            "FROM cc_buddy_daycare "
            "WHERE guild_id=$1 AND user_id=$2 "
            "ORDER BY egg_ready_at ASC, id ASC LIMIT 1",
            gid, uid,
        )
    except Exception:
        log.debug("start buddy tab: daycare fetch failed", exc_info=True)
    state = {}
    try:
        state = await db.fetch_one(
            "SELECT fren_staked_raw, bbt_staked_raw, "
            "bud_yield_pending_raw, hatch_count, "
            "arena_wins, arena_losses, arena_bud_earned_raw "
            "FROM user_buddy_economy WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        ) or {}
    except Exception:
        pass
    bud_h = fren_h = bbt_h = 0.0
    try:
        for sym in ("BUD", "FREN", "BBT"):
            wh = await db.get_wallet_holding(uid, gid, "bud", sym)
            h = to_human(int((wh or {}).get("amount") or 0))
            if sym == "BUD":  bud_h  = h
            if sym == "FREN": fren_h = h
            if sym == "BBT":  bbt_h  = h
    except Exception:
        pass
    return {
        "active":      active,
        "n_owned":     n_owned,
        "n_stored":    n_stored,
        "daycare":     dict(daycare_status) if daycare_status else None,
        "fren_staked": to_human(int(state.get("fren_staked_raw") or 0)),
        "bbt_staked":  to_human(int(state.get("bbt_staked_raw") or 0)),
        "pending_bud": to_human(int(state.get("bud_yield_pending_raw") or 0)),
        "hatch_count": int(state.get("hatch_count") or 0),
        "arena_w":     int(state.get("arena_wins") or 0),
        "arena_l":     int(state.get("arena_losses") or 0),
        "arena_bud":   to_human(int(state.get("arena_bud_earned_raw") or 0)),
        "bud_h":       float(bud_h),
        "fren_h":      float(fren_h),
        "bbt_h":       float(bbt_h),
    }


def _tab_render_buddy(ctx: DiscoContext, st: dict) -> discord.Embed:
    b = card(
        "\U0001F436 Buddy  -  Discoin Hub",
        color=C_PURPLE,
        description=(
            "The Buddy Network. Hatch + raise companions, talk / "
            "feed / pet for **FREN** drops, win arena fights for "
            "**BBT + BUD**, stake either token to drip BUD."
        ),
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    a = st["active"]
    if not a:
        b.field(
            "\U0001F95A No active buddy",
            f"You own **{st['n_owned']}** buddies. "
            f"Use `,buddy hatch` to hatch a new one or "
            f"`,buddy shelter` to set one active.",
            False,
        )
    else:
        b.field(
            f"\U0001F436 {a.get('name') or '?'}",
            f"Lv **{a.get('level') or 1}**  ·  "
            f"{(a.get('species') or '?').title()}  ·  "
            f"Tier {a.get('rarity_tier') or 1}",
            False,
        )
        b.field(
            "\U0001F49E Mood",
            f"Hunger **{a.get('hunger') or 0}**  ·  "
            f"Happy **{a.get('happiness') or 0}**  ·  "
            f"Energy **{a.get('energy') or 0}**",
            False,
        )
    b.field(
        "\U0001F3DF Arena",
        f"**{st['arena_w']}**W / **{st['arena_l']}**L  ·  "
        f"earned **{st['arena_bud']:,.2f} BUD**",
        True,
    )
    b.field(
        "\U0001F4CC Stake (FREN + BBT -> BUD)",
        f"FREN **{st['fren_staked']:,.2f}**  ·  "
        f"BBT **{st['bbt_staked']:,.2f}**\n"
        f"Pending: **{st['pending_bud']:,.4f} BUD**",
        False,
    )
    b.field(
        "\U0001F4B0 Wallet",
        f"BUD **{st['bud_h']:,.2f}**  ·  "
        f"FREN **{st['fren_h']:,.2f}**  ·  "
        f"BBT **{st['bbt_h']:,.2f}**",
        False,
    )
    # Storage + nest summary line so the new surfaces are discoverable
    # from the dashboard without a fresh page navigation.
    dc = st.get("daycare")
    if dc:
        secs = int(dc.get("seconds_remaining") or 0)
        if secs <= 0:
            dc_line = (
                f"\U0001FAB9 Egg ready -- `,buddy nest collect`."
            )
        else:
            hours = secs // 3600
            mins = (secs % 3600) // 60
            dc_line = (
                f"\U0001FAB9 Nest: incubating "
                f"({hours}h {mins}m left)"
            )
    else:
        dc_line = (
            "\U0001FAB9 Nest: empty -- "
            "`,buddy nest deposit <id1> <id2>`"
        )
    b.field(
        "\U0001F4E6 Storage + Nest",
        f"Stored buddies: **{int(st.get('n_stored') or 0)}** "
        f"-- `,buddy storage`\n{dc_line}",
        False,
    )
    return b.build()


def _tab_actions_buddy(ctx: DiscoContext, st: dict) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    """Up to 5 quick-action buttons for the Buddy tab.

    Priority order is action-density-aware: claim a pending payout first
    if there is one, then promote the most likely next-step. Storage +
    Nest always show so they're discoverable from the dashboard.
    """
    actions: list[tuple[str, str, str, discord.ButtonStyle]] = []
    if not st["active"]:
        actions.append(("Hatch", "buddy hatch", "\U0001F95A", discord.ButtonStyle.success))
        if st["n_owned"] > 0:
            actions.append(("Shelter", "buddy shelter", "\U0001F3E0", discord.ButtonStyle.primary))
    else:
        actions.append(("Panel", "buddy", "\U0001F436", discord.ButtonStyle.primary))
        actions.append(("Arena", "buddy arena fight", "\U0001F3DF", discord.ButtonStyle.danger))
    if st["pending_bud"] > 0.0001:
        actions.append(("Claim", "buddy claim", "\U00002728", discord.ButtonStyle.success))
    # Storage + Nest are the new discovery surfaces -- promote them
    # ahead of Stake/Convert (still reachable via prefix). 5-button cap
    # is enforced by StartInterfaceView itself, so trim happens there.
    actions.append(("Storage", "buddy storage", "\U0001F4E6", discord.ButtonStyle.secondary))
    actions.append(("Nest",    "buddy nest",    "\U0001FAB9", discord.ButtonStyle.secondary))
    actions.append(("Stake",   "buddy stake",   "\U0001F4CC", discord.ButtonStyle.secondary))
    actions.append(("Convert", "buddy convert", "\U0001F501", discord.ButtonStyle.secondary))
    return actions


async def _tab_fetch_crafting(ctx: DiscoContext) -> dict:
    """Read user_crafting (aggregate + per-specialty) plus wallet INGOT/FORGE.

    Tolerant to a missing user_crafting row (the user has never crafted) --
    the renderer falls back to a "Bootstrapping" message in that case.
    """
    db, uid, gid = ctx.db, ctx.author.id, ctx.guild_id
    row: dict = {}
    try:
        row = await db.fetch_one(
            "SELECT crafting_level, crafting_xp, total_crafts, "
            "       total_ingot_earned_raw, total_forge_earned_raw, "
            "       ingot_staked_raw, forge_yield_pending_raw, "
            "       smithing_level, smithing_xp, "
            "       alchemy_level, alchemy_xp, "
            "       cooking_level, cooking_xp, "
            "       fletching_level, fletching_xp, "
            "       tinkering_level, tinkering_xp "
            "FROM user_crafting WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        ) or {}
    except Exception:
        log.debug(
            "start crafting tab: user_crafting fetch failed", exc_info=True,
        )
    ingot_h = forge_h = fgd_h = 0.0
    try:
        for sym in ("INGOT", "FORGE", "FGD"):
            wh = await db.get_wallet_holding(uid, gid, "fge", sym)
            h = to_human(int((wh or {}).get("amount") or 0))
            if sym == "INGOT": ingot_h = h
            if sym == "FORGE": forge_h = h
            if sym == "FGD":   fgd_h   = h
    except Exception:
        pass
    return {
        "row":          dict(row),
        "agg_lvl":      int(row.get("crafting_level") or 1),
        "agg_xp":       int(row.get("crafting_xp") or 0),
        "total_crafts": int(row.get("total_crafts") or 0),
        "ingot_earned": row.h("total_ingot_earned_raw"),
        "forge_earned": row.h("total_forge_earned_raw"),
        "ingot_staked": row.h("ingot_staked_raw"),
        "pending_forge": row.h("forge_yield_pending_raw"),
        "ingot_h":      float(ingot_h),
        "forge_h":      float(forge_h),
        "fgd_h":        float(fgd_h),
    }


def _tab_render_crafting(ctx: DiscoContext, st: dict) -> discord.Embed:
    import configs.crafting_config as cc

    b = card(
        "\U0001F528 Crafting  -  Forge Network",
        color=C_AMBER,
        description=(
            "Combine fishing / farming / dungeon outputs into bait, "
            "fertilizer, dungeon consumables, and buddy treats. Mint "
            "**INGOT** per craft, stake to drip **FORGE**, cash FORGE "
            "out to USD. Five specialties level independently."
        ),
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

    if not st.get("row"):
        b.field(
            "\U0001F195 No crafting state yet",
            "Run `,craft list` to bootstrap your forge profile, then "
            "`,craft make <recipe>` once you have ingredients.",
            False,
        )
        return b.build()

    b.field(
        "\U0001F4DC Aggregate",
        f"Lv **{st['agg_lvl']}**  ·  "
        f"{st['agg_xp']:,} XP  ·  "
        f"**{st['total_crafts']:,}** crafts",
        False,
    )

    # Per-specialty levels in a compact 5-line block. Always fits inside the
    # 1024-char field cap because the longest line is ~30 chars.
    spec_lines = []
    for spec in cc.SPECIALTIES:
        meta = cc.SPECIALTY_META.get(spec) or {}
        lvl = int(st["row"].get(f"{spec}_level") or 1)
        emoji = str(meta.get("emoji") or "")
        name = str(meta.get("name") or spec.title())
        spec_lines.append(f"{emoji} **{name}**  Lv {lvl}")
    b.field("\U0001F4DA Specialties", "\n".join(spec_lines), False)

    b.field(
        "\U0001F9F1 Stake (INGOT -> FORGE)",
        f"Staked **{st['ingot_staked']:,.2f} INGOT**\n"
        f"Pending: **{st['pending_forge']:,.4f} FORGE**",
        True,
    )
    b.field(
        "\U0001F4B0 Wallet",
        f"INGOT **{st['ingot_h']:,.2f}**\n"
        f"FORGE **{st['forge_h']:,.2f}**\n"
        f"FGD **{st['fgd_h']:,.2f}**",
        True,
    )
    return b.build()


def _tab_actions_crafting(ctx: DiscoContext, st: dict) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    """Up to 5 action buttons for the Crafting tab."""
    actions: list[tuple[str, str, str, discord.ButtonStyle]] = [
        ("Forge",       "craft",            "\U0001F528", discord.ButtonStyle.primary),
        ("Recipes",     "craft list",       "\U0001F4DC", discord.ButtonStyle.secondary),
        ("Specialties", "craft specialties","\U0001F4DA", discord.ButtonStyle.secondary),
    ]
    # Only surface "Claim" when there's something to claim, otherwise the
    # prompt is wasted real estate.
    if float(st.get("pending_forge") or 0.0) > 0.0001:
        actions.append((
            "Claim", "craft claim", "\U00002728",
            discord.ButtonStyle.success,
        ))
    actions.append((
        "Stake", "craft stake", "\U0001F9F1",
        discord.ButtonStyle.secondary,
    ))
    return actions


# All nine themed stones in display order. Each entry is the
# (db_getter_name, label, emoji, currency_hint) tuple consumed by the
# stones tab fetch / render. Currency hint is purely informational --
# the actual ``lp_currency`` on the stone row is what gets rendered if
# the row exists. Stays in lockstep with the stones cheatsheet shown by
# ``,help stones``.
_STONE_DEFS: tuple[tuple[str, str, str, str], ...] = (
    ("get_hashstone",     "Hashstone",     "\U000026CF️", "MTA/SUN"),
    ("get_lockstone",     "Lockstone",     "\U0001F512",       "DSC/ARC"),
    ("get_vaultstone",    "Vaultstone",    "\U0001F3E6",       "USD"),
    ("get_liqstone",      "Liqstone",      "\U0001F30A",       "DSD/USDC"),
    ("get_tidestone",     "Tidestone",     "\U0001F3A3",       "REEL"),
    ("get_heartstone",    "Heartstone",    "\U0001F49E",       "BUD"),
    ("get_cryptstone",    "Cryptstone",    "\U0001F48E",       "RUNE"),
    ("get_bloodstone",    "Bloodstone",    "\U0001FA78",       "BBT"),
    ("get_bloomstone",    "Bloomstone",    "\U0001F33C",       "HRV"),
    # Meta-economy stones (USD-priced).
    ("get_gavelstone",    "Gavelstone",    "\U0001FA99",       "USD"),
    ("get_anvilstone",    "Anvilstone",    "\U0001F528",       "USD"),
    ("get_chimerastone",  "Chimerastone",  "\U0001F52E",       "USD"),
)


async def _tab_fetch_stones(ctx: DiscoContext) -> dict:
    """Pull every owned stone for the player. Missing rows are kept as
    ``None`` so the renderer can show "not yet" against the catalog.
    """
    db, uid, gid = ctx.db, ctx.author.id, ctx.guild_id
    out: dict[str, dict | None] = {}
    for getter, label, _emoji, _hint in _STONE_DEFS:
        try:
            fn = getattr(db, getter, None)
            row = await fn(uid, gid) if fn else None
        except Exception:
            log.debug(
                "start stones tab: %s fetch failed", getter, exc_info=True,
            )
            row = None
        out[label] = dict(row) if row else None
    autolevel = False
    try:
        ur = await db.get_user(uid, gid) or {}
        autolevel = bool(ur.get("autolevelup_enabled"))
    except Exception:
        pass
    return {"stones": out, "autolevel": autolevel}


def _tab_render_stones(ctx: DiscoContext, st: dict) -> discord.Embed:
    b = card(
        "\U0001F48E Stones  -  Discoin Hub",
        color=C_PURPLE,
        description=(
            "Leveled gems. Each is **staked** in its own currency, "
            "earns XP from one specific activity, levels 1 -> 100, and "
            "gives a permanent stat boost. Auto-levelup walks every "
            "accepted currency so a stone keeps levelling once you "
            "spend its primary token down."
        ),
    ).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

    owned = sum(1 for v in st["stones"].values() if v)
    total = len(_STONE_DEFS)
    al = "\U00002705 ON" if st.get("autolevel") else "⬜ OFF"
    b.field(
        "\U0001F4CA Summary",
        f"Owned **{owned}/{total}**  ·  Auto-levelup **{al}**",
        False,
    )

    # Pack the stone list into two compact columns. Each line is short
    # so even all 9 fit comfortably under the field cap.
    lines: list[str] = []
    for getter, label, emoji, hint in _STONE_DEFS:
        row = st["stones"].get(label)
        if row:
            lvl = int(row.get("level") or 1)
            cur = str(row.get("lp_currency") or hint)
            lines.append(
                f"{emoji} **{label}** Lv. **{lvl}** "
                f"· staked {row.h('staked_amount'):,.2f} {cur}"
            )
        else:
            lines.append(
                f"{emoji} **{label}** not yet  ·  buy with `{hint}`"
            )
    b.field("\U0001F4DA Catalog", "\n".join(lines), False)
    return b.build()


def _tab_actions_stones(
    ctx: DiscoContext, st: dict,
) -> list[tuple[str, str, str, discord.ButtonStyle]]:
    """Up to 5 quick-action buttons for the Stones tab."""
    actions: list[tuple[str, str, str, discord.ButtonStyle]] = [
        ("Inventory", "inv",   "\U0001F392", discord.ButtonStyle.primary),
        ("Shop",      "shop",  "\U0001F6CD", discord.ButtonStyle.secondary),
    ]
    if st.get("autolevel"):
        actions.append((
            "Auto-up OFF", "autolevelup off", "\U000023F8️",
            discord.ButtonStyle.secondary,
        ))
    else:
        actions.append((
            "Auto-up ON", "autolevelup on", "\U000025B6️",
            discord.ButtonStyle.success,
        ))
    actions.append((
        "Stones help", "help stones", "\U00002753",
        discord.ButtonStyle.secondary,
    ))
    return actions


_TAB_FETCHERS: dict[str, "callable"] = {
    "guide":    _tab_fetch_guide,
    "wallet":   _tab_fetch_wallet,
    "market":   _tab_fetch_market,
    "fishing":  _tab_fetch_fishing,
    "farming":  _tab_fetch_farming,
    "delve":    _tab_fetch_delve,
    "buddy":    _tab_fetch_buddy,
    "crafting": _tab_fetch_crafting,
    "stones":   _tab_fetch_stones,
}
_TAB_RENDERERS: dict[str, "callable"] = {
    "guide":    _tab_render_guide,
    "wallet":   _tab_render_wallet,
    "market":   _tab_render_market,
    "fishing":  _tab_render_fishing,
    "farming":  _tab_render_farming,
    "delve":    _tab_render_delve,
    "buddy":    _tab_render_buddy,
    "crafting": _tab_render_crafting,
    "stones":   _tab_render_stones,
}
_TAB_ACTIONS: dict[str, "callable"] = {
    "guide":    _tab_actions_guide,
    "wallet":   _tab_actions_wallet,
    "market":   _tab_actions_market,
    "fishing":  _tab_actions_fishing,
    "farming":  _tab_actions_farming,
    "delve":    _tab_actions_delve,
    "buddy":    _tab_actions_buddy,
    "crafting": _tab_actions_crafting,
    "stones":   _tab_actions_stones,
}


class _TabSelect(discord.ui.Select):
    def __init__(self, ctx: DiscoContext, current: str, row: int = 0) -> None:
        opts = [
            discord.SelectOption(
                label=label, value=key, emoji=emoji,
                description=blurb,
                default=(key == current),
            )
            for key, (label, emoji, blurb) in _TAB_META.items()
        ]
        super().__init__(
            placeholder="\U0001F500 Switch tab",
            min_values=1, max_values=1,
            options=opts, row=row,
        )
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction) -> None:
        new_tab = self.values[0]
        await interaction.response.defer()
        await StartInterfaceView.render_tab(interaction, self.ctx, new_tab)


class _RefreshTabButton(discord.ui.Button):
    def __init__(self, ctx: DiscoContext, current: str, row: int = 4) -> None:
        super().__init__(
            label="Refresh", emoji="\U0001F504",
            style=discord.ButtonStyle.secondary, row=row,
        )
        self.ctx = ctx
        self.current = current

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await StartInterfaceView.render_tab(interaction, self.ctx, self.current)


class _CloseInterfaceButton(discord.ui.Button):
    def __init__(self, ctx: DiscoContext, row: int = 4) -> None:
        super().__init__(
            label="Close", emoji="\U0000274C",
            style=discord.ButtonStyle.danger, row=row,
        )
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(
                content="\U0001F44B  See you, partner.",
                embed=None, view=None,
            )
        except Exception:
            pass


class StartInterfaceView(_OwnedView):
    """Tabbed ,start interface. Use ``render_tab(interaction, ctx, key)`` to
    switch to a tab without rebuilding the message.
    """

    def __init__(
        self, ctx: DiscoContext, current: str, state: dict,
        *, summary: "Any | None" = None,
    ) -> None:
        super().__init__(ctx)
        self.current = current
        self.state = state
        self.summary = summary
        self.add_item(_TabSelect(ctx, current, row=0))

        # Per-tab action buttons (Discord caps at 25 components / 5 rows).
        # Home tab packs starter / daily + ready-feed quick-collects so
        # the player can act on the today panel directly. Slots are
        # added in priority order and we stop at 4 row-1 buttons +
        # 4 row-2 buttons to leave row 3 free for evergreen actions.
        if current == "home":
            row1_count = 0
            row2_count = 0

            def _row1_full() -> bool:
                return row1_count >= 4

            def _row2_full() -> bool:
                return row2_count >= 4

            if not state.get("starter_claimed"):
                self.add_item(_StarterPackButton(ctx, row=1))
                row1_count += 1
            if _daily_ready(state.get("last_daily")) and not _row1_full():
                self.add_item(_RunCommandButton(
                    "Daily", "daily", "\U0001F4B0",
                    discord.ButtonStyle.success, ctx, row=1,
                ))
                row1_count += 1
            # Ready-feed quick-collects -- only if HubSummary surfaced them.
            if summary is not None:
                if int(getattr(summary, "expeditions_ready", 0) or 0) > 0 and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Collect Runs", "expedition collect", "\U0001F392",
                        discord.ButtonStyle.success, ctx, row=1,
                    ))
                    row1_count += 1
                if int(getattr(summary, "daycare_ready", 0) or 0) > 0 and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Collect Eggs", "buddy nest collect", "\U0001FAB9",
                        discord.ButtonStyle.success, ctx, row=1,
                    ))
                    row1_count += 1
                if int(getattr(summary, "plots_ripe", 0) or 0) > 0 and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Harvest", "farm", "\U0001F33E",
                        discord.ButtonStyle.success, ctx, row=1,
                    ))
                    row1_count += 1
                if int(getattr(summary, "traps_placed", 0) or 0) > 0 and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Traps", "fish trap collect", "\U0001F980",
                        discord.ButtonStyle.success, ctx, row=1,
                    ))
                    row1_count += 1
                if bool(getattr(summary, "contract_actionable", False)) and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Turn In", "farm contract turnin", "\U0001F4E6",
                        discord.ButtonStyle.success, ctx, row=1,
                    ))
                    row1_count += 1
                if bool(getattr(summary, "forage_ready", False)) and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Forage", "farm forage", "\U0001F33F",
                        discord.ButtonStyle.success, ctx, row=1,
                    ))
                    row1_count += 1
                if bool(getattr(summary, "beachcomb_ready", False)) and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Beachcomb", "fish beachcomb", "\U0001F3DD",
                        discord.ButtonStyle.success, ctx, row=1,
                    ))
                    row1_count += 1
                if bool(getattr(summary, "scavenge_ready", False)) and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Scavenge", "delve scavenge", "\U0001F50E",
                        discord.ButtonStyle.success, ctx, row=1,
                    ))
                    row1_count += 1
                if bool(getattr(summary, "delve_shrine_in_room", False)) and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Pray", "delve pray", "\U0001F64F",
                        discord.ButtonStyle.success, ctx, row=1,
                        refresh_tab="home",
                    ))
                    row1_count += 1
                if bool(getattr(summary, "delve_chest_in_room", False)) and not _row1_full():
                    self.add_item(_RunCommandButton(
                        "Open Chest", "delve open", "\U0001F4B0",
                        discord.ButtonStyle.success, ctx, row=1,
                        refresh_tab="home",
                    ))
                    row1_count += 1
                if int(getattr(summary, "delve_unspent_stats", 0) or 0) > 0 and not _row2_full():
                    self.add_item(_RunCommandButton(
                        "Spend Pts", "delve upgrade", "\U0001F4CA",
                        discord.ButtonStyle.primary, ctx, row=2,
                    ))
                    row2_count += 1
                if int(getattr(summary, "buddies_unspent_stats", 0) or 0) > 0 and not _row2_full():
                    self.add_item(_RunCommandButton(
                        "Buddy Pts", "buddy upgrade", "\U0001F436",
                        discord.ButtonStyle.primary, ctx, row=2,
                    ))
                    row2_count += 1
            # Onboarding nudges for first-time players.
            if not state.get("has_buddy") and not _row2_full():
                self.add_item(_RunCommandButton(
                    "Hatch", "buddy hatch", "\U0001F95A",
                    discord.ButtonStyle.primary, ctx, row=2,
                ))
                row2_count += 1
            if not state.get("fish_total") and not _row2_full():
                self.add_item(_RunCommandButton(
                    "Fish", "fish", "\U0001F3A3",
                    discord.ButtonStyle.primary, ctx, row=2,
                ))
                row2_count += 1
            if not state.get("farm_total") and not _row2_full():
                self.add_item(_RunCommandButton(
                    "Plant", "farm plant 1 wheat", "\U0001F33E",
                    discord.ButtonStyle.primary, ctx, row=2,
                ))
                row2_count += 1
            self.add_item(_RunCommandButton(
                "My Profile", "me", "\U0001F4DC",
                discord.ButtonStyle.secondary, ctx, row=3,
            ))
            self.add_item(_RunCommandButton(
                "Calendar", "calendar", "\U0001F5D3",
                discord.ButtonStyle.secondary, ctx, row=3,
            ))
        else:
            actions_fn = _TAB_ACTIONS.get(current)
            if actions_fn:
                for i, (label, cmd, emoji, style) in enumerate(actions_fn(ctx, state)[:5]):
                    self.add_item(_RunCommandButton(
                        label, cmd, emoji, style, ctx, row=1 + i // 3,
                    ))

        self.add_item(_RefreshTabButton(ctx, current, row=4))
        self.add_item(_CloseInterfaceButton(ctx, row=4))

    @staticmethod
    async def render_tab(
        interaction: discord.Interaction, ctx: DiscoContext, tab_key: str,
    ) -> None:
        """Pull fresh state for ``tab_key`` and edit the open message in place."""
        summary = None
        if tab_key == "home":
            state = await _gather_player_state(ctx)
            try:
                from services import hub as _hub_svc
                summary = await _hub_svc.hub_summary(
                    ctx.db, ctx.author.id, ctx.guild_id,
                )
            except Exception:
                log.debug("home tab: hub_summary fetch failed", exc_info=True)
            prefix = ctx.prefix or Config.PREFIX
            steps = _starter_next_steps(state, prefix)
            embed = _start_dashboard_embed(ctx, state, steps, summary=summary)
        else:
            fetcher = _TAB_FETCHERS.get(tab_key)
            renderer = _TAB_RENDERERS.get(tab_key)
            if not fetcher or not renderer:
                # Tab declared in _TAB_META but not implemented yet -- show a
                # placeholder rather than crashing the whole interface.
                state = {}
                embed = card(
                    f"{_TAB_META[tab_key][1]} {_TAB_META[tab_key][0]}",
                    description=(
                        f"This tab is not wired up yet. Use the prefix "
                        f"command for this surface in the meantime."
                    ),
                    color=C_NEUTRAL,
                ).build()
            else:
                state = await fetcher(ctx)
                embed = renderer(ctx, state)
        view = StartInterfaceView(ctx, tab_key, state, summary=summary)
        try:
            await interaction.message.edit(embed=embed, view=view)
        except Exception:
            log.debug("start: tab render edit failed", exc_info=True)


async def open_unified_panel(ctx: DiscoContext) -> None:
    """Open the unified ,start / ,today panel.

    Pulls onboarding state + HubSummary in parallel, builds the combined
    Home embed, mounts the tabbed interactive view. ``,today`` and
    ``,start`` both call this so the two commands open the SAME panel
    + view, and any state changes (claiming daily, sending an
    expedition, etc) refresh in place via the view's refresh button.
    """
    state = await _gather_player_state(ctx)
    summary = None
    try:
        from services import hub as _hub_svc
        summary = await _hub_svc.hub_summary(
            ctx.db, ctx.author.id, ctx.guild_id,
        )
    except Exception:
        log.debug("unified panel: hub_summary fetch failed", exc_info=True)
    prefix = ctx.prefix or Config.PREFIX
    steps = _starter_next_steps(state, prefix)
    embed = _start_dashboard_embed(ctx, state, steps, summary=summary)
    view = StartInterfaceView(ctx, "home", state, summary=summary)
    await ctx.reply(embed=embed, view=view, mention_author=False)


class GameHub(commands.Cog):
    """,start launcher -- personalised onboarding + game hub."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="start", aliases=["begin", "onboarding", "newbie"],
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def start(self, ctx: DiscoContext) -> None:
        """Unified Discoin panel -- onboarding dashboard + today status + game launcher.

        Shows your wallet, net worth, daily-claim status, per-game
        progress, top quests, ready-to-claim items (eggs, plots,
        expeditions), and unspent stat points -- all in one embed.
        Switch tabs to fishing / farming / delve / buddy / etc. and
        every button runs the underlying command in-place so you can
        play the entire game from this panel without leaving it.

        ``,today`` is an alias of this command.
        """
        await open_unified_panel(ctx)

    @commands.hybrid_command(
        name="menu", aliases=["hub", "minigames"],
    )
    @guild_only
    @no_bots
    async def menu(self, ctx: DiscoContext) -> None:
        """Multi-game launcher hub (the original ,start UI)."""
        prefix = ctx.prefix or Config.PREFIX
        embed = _hub_root_embed(prefix)
        view = GameHubView(ctx)
        await ctx.reply(embed=embed, view=view, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Overview(bot))
    await bot.add_cog(GameHub(bot))
