from __future__ import annotations

import logging
import re as _re
import time

import discord
from discord.ext import commands

from core.config import Config
from core.framework.ui import send_paginated
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.ui import (
    C_AMBER, C_ERROR, C_SUCCESS, C_PURPLE, C_INFO, C_NEUTRAL, C_GOLD,
    fmt_bonus, fmt_token, fmt_ts, fmt_usd, ConfirmView,
)
from core.framework.scale import to_human as _h, to_raw as _tr
from cogs.helpers import _MemberOrID
from cogs.shop import _item_stat
from core.framework.embed import card
from core.framework.fuzzy import suggest_subcommand
from core.framework.contracts import make_contract_address, make_token_hash
from core.framework.content_filter import (
    sanitize_text, sanitize_display, has_scam_patterns, has_discord_entities,
    validate_group_name, validate_group_description, validate_image_url,
    make_group_token, is_safe_url,
)
from core.framework.ui import InputModal

log = logging.getLogger(__name__)

# Commands always permitted inside a Group Hall thread (read-only / info)
_HALL_ALWAYS_ALLOWED: frozenset[str] = frozenset({
    "group", "help", "status", "diagnose", "balance", "wallet", "portfolio",
    "inventory", "chart", "market", "rates", "tokeninfo", "crypto", "gas",
    "token", "txinfo", "contract", "block", "mempool", "leaderboard",
    "economy", "eatstats", "eathistory", "rugstats", "rughistory",
    "gambstats", "report", "reports", "vals", "vnetworks", "vstats",
})

# Commands blocked inside Hall threads by default; populated from registered commands in cog_load
_HALL_BLOCKED_COMMANDS: frozenset[str] = frozenset()

# Groups that are never put behind the Hall gate
_HALL_PRIVILEGED_GROUPS: frozenset[str] = frozenset({"group", "admin", "drs", "dev", "help"})

# Maps items_config hall_unlock category names to cog class names so every
# command (including standalone prefix aliases) in that cog is unlocked.
_HALL_UNLOCK_COGS: dict[str, frozenset[str]] = {
    "Earn": frozenset({"Earn", "Faucet"}),
    "Trading": frozenset({"Trade", "Crypto"}),
    "DeFi": frozenset({"Stake", "Validators", "ChainGroup"}),
    "Play": frozenset({"Play"}),
}

_RIGS = Config.MINING_RIGS


class GroupInviteView(discord.ui.View):
    """Persistent Accept / Decline buttons sent in invite DMs.

    custom_id format: ``group_invite:<guild_id>:<group_id>:<accept|decline>``
    These survive bot restarts and work in DMs (no guild context needed).
    """

    def __init__(self, guild_id: int, group_id: str, bot: "Discoin") -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.group_id = group_id
        self.bot = bot

        self.accept_btn = discord.ui.Button(
            label="Join Group",
            style=discord.ButtonStyle.success,
            custom_id=f"group_invite:{guild_id}:{group_id}:accept",
            emoji="✅",
        )
        self.decline_btn = discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.secondary,
            custom_id=f"group_invite:{guild_id}:{group_id}:decline",
            emoji="❌",
        )
        self.accept_btn.callback = self._accept
        self.decline_btn.callback = self._decline
        self.add_item(self.accept_btn)
        self.add_item(self.decline_btn)

    async def _accept(self, interaction: discord.Interaction) -> None:
        try:
            await self._accept_inner(interaction)
        except Exception:
            log.exception("[GroupInviteView] _accept crashed for user %s guild %s group %s",
                          interaction.user.id, self.guild_id, self.group_id)
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(
                        content="Something went wrong joining the group. Try `.group accept` in the server instead.",
                        embed=None, view=None,
                    )
            except Exception:
                pass

    async def _accept_inner(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        db = self.bot.db

        invite = await db.get_group_invite(self.guild_id, self.group_id, user_id)
        if not invite:
            # Check if user is already in this group (invite was already used)
            existing = await db.get_user_mining_group(user_id, self.guild_id)
            if existing and existing["group_id"] == self.group_id:
                grp = await db.get_mining_group(self.guild_id, group_id=self.group_id)
                g_name = grp["name"] if grp else self.group_id
                await interaction.response.edit_message(
                    content=f"You're already in **{g_name}**!", embed=None, view=None,
                )
                return
            # Check if group is public (no invite needed)
            grp = await db.get_mining_group(self.guild_id, group_id=self.group_id)
            if grp and grp.get("is_public"):
                if existing:
                    g = await db.get_mining_group(self.guild_id, group_id=existing["group_id"])
                    g_name = g["name"] if g else existing["group_id"]
                    await interaction.response.edit_message(
                        content=(
                            f"You're already in **{g_name}**. "
                            f"Leave it first with `.group leave`, then try again."
                        ),
                        embed=None, view=None,
                    )
                    return
                await db.join_mining_group(user_id, self.guild_id, self.group_id)
                if grp.get("hall_thread_id") and interaction.guild:
                    try:
                        thread = interaction.guild.get_channel_or_thread(grp["hall_thread_id"])
                        if thread and isinstance(thread, discord.Thread) and not thread.archived:
                            await thread.add_user(interaction.user)
                    except Exception:
                        pass
                embed = (
                    card("⛏️ Joined Mining Group", color=C_SUCCESS)
                    .description(f"You joined **{grp['name']}**!")
                    .build()
                )
                await interaction.response.edit_message(embed=embed, view=None, content=None)
                return
            await interaction.response.edit_message(
                content=(
                    "This invite is no longer valid. "
                    "Ask the group founder to send a new one."
                ),
                embed=None, view=None,
            )
            return

        existing = await db.get_user_mining_group(user_id, self.guild_id)
        if existing:
            g = await db.get_mining_group(self.guild_id, group_id=existing["group_id"])
            g_name = g["name"] if g else existing["group_id"]
            await interaction.response.send_message(
                f"You're already in **{g_name}**. Leave it first before accepting.", ephemeral=True,
            )
            return

        grp = await db.get_mining_group(self.guild_id, group_id=self.group_id)
        if not grp:
            await db.delete_group_invite(self.guild_id, self.group_id, user_id)
            await interaction.response.edit_message(
                content="That group no longer exists.", embed=None, view=None,
            )
            return

        await db.delete_group_invite(self.guild_id, self.group_id, user_id)
        await db.join_mining_group(user_id, self.guild_id, self.group_id)

        # Add to Hall thread if one exists and user is in the right guild
        if grp.get("hall_thread_id") and interaction.guild:
            try:
                thread = interaction.guild.get_channel_or_thread(grp["hall_thread_id"])
                if thread and isinstance(thread, discord.Thread) and not thread.archived:
                    await thread.add_user(interaction.user)
            except Exception:
                pass

        hall_note = " Head to the group's Hall thread to get started." if grp.get("hall_thread_id") else ""
        embed = (
            card("⛏️ Joined Mining Group", color=C_SUCCESS)
            .description(f"You joined **{grp['name']}**!{hall_note}")
            .build()
        )
        await interaction.response.edit_message(embed=embed, view=None, content=None)

    async def _decline(self, interaction: discord.Interaction) -> None:
        try:
            await self._decline_inner(interaction)
        except Exception:
            log.exception("[GroupInviteView] _decline crashed for user %s guild %s group %s",
                          interaction.user.id, self.guild_id, self.group_id)
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(
                        content="Something went wrong. Try `.group decline` in the server instead.",
                        embed=None, view=None,
                    )
            except Exception:
                pass

    async def _decline_inner(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        db = self.bot.db

        invite = await db.get_group_invite(self.guild_id, self.group_id, user_id)
        grp = await db.get_mining_group(self.guild_id, group_id=self.group_id)
        grp_name = grp["name"] if grp else self.group_id

        if invite:
            await db.delete_group_invite(self.guild_id, self.group_id, user_id)

        await interaction.response.edit_message(
            content=f"Declined invite to **{grp_name}**.", embed=None, view=None,
        )


class GroupPoolProposalView(discord.ui.View):
    """Persistent Accept / Decline buttons sent to target group founder for LP pool proposals.

    custom_id format: ``group_pool_proposal:<guild_id>:<proposal_id>:<accept|decline>``
    """

    def __init__(self, guild_id: int, proposal_id: int, bot: "Discoin") -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.proposal_id = proposal_id
        self.bot = bot

        accept_btn = discord.ui.Button(
            label="Accept Pair",
            style=discord.ButtonStyle.success,
            custom_id=f"group_pool_proposal:{guild_id}:{proposal_id}:accept",
            emoji="✅",
        )
        decline_btn = discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.secondary,
            custom_id=f"group_pool_proposal:{guild_id}:{proposal_id}:decline",
            emoji="❌",
        )
        accept_btn.callback = self._accept
        decline_btn.callback = self._decline
        self.add_item(accept_btn)
        self.add_item(decline_btn)

    async def _accept(self, interaction: discord.Interaction) -> None:
        try:
            await self._accept_inner(interaction)
        except Exception:
            log.exception(
                "[GroupPoolProposalView] accept crashed proposal=%d guild=%d",
                self.proposal_id, self.guild_id,
            )
            if not interaction.response.is_done():
                await interaction.response.edit_message(
                    content="Something went wrong. Try `.group pool accept` in the server.",
                    embed=None, view=None,
                )

    async def _accept_inner(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        db = self.bot.db

        proposal = await db.get_group_pool_proposal(self.proposal_id, self.guild_id)
        if not proposal:
            await interaction.response.edit_message(
                content="This proposal has already been accepted, declined, or expired.",
                embed=None, view=None,
            )
            return

        # Verify the accepting user is the founder of the target group
        target_grp = await db.get_mining_group(self.guild_id, group_id=proposal["target_group"])
        if not target_grp or target_grp["founder_id"] != user_id:
            await interaction.response.send_message(
                "Only the founder of the target group can accept this proposal.", ephemeral=True,
            )
            return

        pool_id, ca, cb = db.make_pool_id(proposal["token_a"], proposal["token_b"])
        existing = await db.get_pool(pool_id, self.guild_id)
        if existing:
            await db.delete_group_pool_proposal(self.proposal_id, self.guild_id)
            await interaction.response.edit_message(
                content=f"Pool **{ca}/{cb}** already exists. Proposal closed.",
                embed=None, view=None,
            )
            return

        await db.create_group_pool(pool_id, self.guild_id, ca, cb)
        await db.delete_group_pool_proposal(self.proposal_id, self.guild_id)

        proposer_grp = await db.get_mining_group(self.guild_id, group_id=proposal["proposer_group"])

        # Auto-seed from each group's vault
        seed_note = await Groups._seed_group_pool_from_vault(
            db, self.guild_id, pool_id,
            proposer_grp, target_grp,
            proposal["token_a"], proposal["token_b"],
        ) if proposer_grp else "Seeding skipped: proposer group not found."

        await interaction.response.edit_message(
            content=(
                f"Partnership accepted! Pool **{ca}/{cb}** is live.\n"
                f"{seed_note}\n"
                f"Both groups can add more LP with `{Config.PREFIX}trade pool add {ca} {cb} <amount_a> <amount_b>`."
            ),
            embed=None, view=None,
        )

        # Notify proposing founder
        if proposer_grp:
            proposer_member = interaction.guild and interaction.guild.get_member(proposer_grp["founder_id"])
            if proposer_member:
                try:
                    await proposer_member.send(
                        f"**{target_grp['name']}** accepted your pool proposal!\n"
                        f"Pool **{ca}/{cb}** is now live.\n"
                        f"{seed_note}"
                    )
                except discord.Forbidden:
                    pass

    async def _decline(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        proposal = await db.get_group_pool_proposal(self.proposal_id, self.guild_id)
        if not proposal:
            await interaction.response.edit_message(
                content="This proposal has already been resolved.", embed=None, view=None,
            )
            return

        target_grp = await db.get_mining_group(self.guild_id, group_id=proposal["target_group"])
        if not target_grp or target_grp["founder_id"] != interaction.user.id:
            await interaction.response.send_message(
                "Only the founder of the target group can decline this proposal.", ephemeral=True,
            )
            return

        await db.delete_group_pool_proposal(self.proposal_id, self.guild_id)
        proposer_grp = await db.get_mining_group(self.guild_id, group_id=proposal["proposer_group"])
        await interaction.response.edit_message(
            content=f"Declined pool proposal from **{proposer_grp['name'] if proposer_grp else proposal['proposer_group']}**.",
            embed=None, view=None,
        )


_GROUP_HELP_CATEGORIES: list[tuple[str, str, str, str]] = [
    # (value, label, emoji, description)  -- description <= 100 chars
    ("overview",  "Overview",         "📖", "What groups are and how to get started"),
    ("manage",    "Management",       "⚙️", "Create, rename, configure, and disband your group"),
    ("members",   "Members",          "👥", "Invite, kick, weights, and membership settings"),
    ("mine",      "Mining & Token",   "⛏️", "Mining chains, group token network, and vault"),
    ("lp",        "LP Treasury",      "🏛️", "Founder-only: deposit/withdraw vault tokens into LP"),
    ("pool",      "LP Pools",         "💧", "Cross-group liquidity pools and fee harvesting"),
    ("hall",      "Group Hall",       "🏛️", "Private Hall channel: open, close, and info"),
    ("reserve",   "Reserves",         "💰", "Reserve balance, cut rate, and spending"),
    ("upgrade",   "Upgrades",         "🔧", "Group upgrades: list available and buy"),
]


class GroupHelpView(discord.ui.View):
    """Dropdown-based help for .group commands, organised by category."""

    def __init__(self, ctx: DiscoContext, *, timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self._ctx = ctx
        self._prefix = ""

        select = discord.ui.Select(
            placeholder="Choose a category...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=val, emoji=emoji, description=desc)
                for val, label, emoji, desc in _GROUP_HELP_CATEGORIES
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._ctx.author.id:
            await interaction.response.send_message(
                "This help menu isn't for you.", ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        p = self._prefix or self._ctx.prefix or Config.PREFIX
        value = interaction.data["values"][0]
        embed = _build_group_help_embed(value, p)
        await interaction.response.edit_message(embed=embed, view=self)

    async def send(self) -> None:
        """Send the overview embed with the dropdown."""
        self._prefix = self._ctx.prefix or Config.PREFIX
        embed = _build_group_help_embed("overview", self._prefix)
        await self._ctx.reply(embed=embed, view=self, mention_author=False)


def _build_group_help_embed(category: str, p: str) -> discord.Embed:
    """Return a `discord.Embed` for the requested help category."""
    from core.framework.embed import card as _card

    def _e(title: str, color: int) -> "_card":
        return _card(title, color=color)

    if category == "overview":
        return (
            _e("👥 Mining Groups -- the short version", C_PURPLE)
            .description(
                "**Why join a group?** Solo mining is slow. A group "
                "pools every member's hashrate so blocks land faster, "
                "splits the reward, and unlocks a bunch of group-only "
                "machinery: a custom mintable token, a USD treasury "
                "(`reserve_usd`), a private Hall thread, group-wide "
                "minigame bonuses, and partnership LP pools with "
                "other groups.\n\n"
                "**Pick a path:** join an existing group with "
                f"`{p}group join`, or run `{p}group create <name>` "
                "to start your own and become its founder."
            )
            .field(
                "The journey, in order",
                f"  **1.** `{p}group create <name>`  -- you're the founder.\n"
                f"  **2.** `{p}group set tag=XXXX`  -- pick a 1-4 char "
                "token symbol; the group can now mint a custom token.\n"
                f"  **3.** `{p}group token network <mta|sun>`  -- bind the "
                "token to a PoW network so mining produces it. **One-time, "
                "irreversible.** Pick carefully.\n"
                f"  **4.** `{p}group reserve set <0-100>`  -- the % of every "
                "mined block that flows into the group's USD reserve.\n"
                f"  **5.** `{p}group invite @member` / `{p}group hall open` "
                "-- bring people in, give them a private thread to play in.\n"
                f"  **6.** Mine -> earn -> spend reserve on `{p}group "
                "upgrade buy` for permanent group-wide perks, or push "
                f"vault tokens / reserve USD into LP via `{p}group lp`.",
                False,
            )
            .field(
                "Cheat sheet",
                f"`{p}group info`  -- snapshot of your group\n"
                f"`{p}group list`  -- browse every group on the server\n"
                f"`{p}group lb`  -- leaderboards by hashrate / treasury / "
                "mkt cap / token spot\n"
                f"`{p}mg`  -- shortcut alias for `{p}group`",
                False,
            )
            .footer(
                "Pick a category above for the deep dive on each surface."
            )
            .build()
        )

    if category == "manage":
        return (
            _e("⚙️ Group Management", C_PURPLE)
            .description(
                "Founder-only commands for shaping your group: "
                "name, tag, settings, member-reward mode, and the "
                "two end-of-life paths (disband + founder transfer)."
            )
            .field(
                "Day-1 setup",
                f"`{p}group create <name>` -- start the group; you become "
                "the founder automatically. Append `private` to make it "
                "invite-only from the start.\n"
                f"`{p}group set tag=XXXX` -- 1-4 character token symbol. "
                f"You **must** set a tag before `{p}group token network` "
                "can bind your token to a PoW chain.\n"
                f"`{p}group set description=...` -- shows up on info.\n"
                f"`{p}group set image=<url>` -- banner image URL.",
                False,
            )
            .field(
                "Tweaks any time",
                f"`{p}group rename <new name>` -- 30-day cooldown.\n"
                f"`{p}group privacy` -- flip between public (anyone "
                f"can `{p}group join`) and invite-only.\n"
                f"`{p}group weightmode <equal|hashrate>` -- how mining "
                "rewards split among members. See **Members** for the "
                "full mechanics.\n"
                f"`{p}group reserve set <0-100>` -- the % of every "
                "mined block that flows into the reserve. See **Reserve**.",
                False,
            )
            .field(
                "Read-only",
                f"`{p}group info [name]` -- snapshot of your group "
                "(or another by name): treasury, vault, member roster, "
                "reserve rate, mining chain, active LP positions.\n"
                f"`{p}group list` -- browse every group on the server.\n"
                f"`{p}group lb` -- leaderboards.",
                False,
            )
            .field(
                "End of the road",
                f"`{p}group disband` -- nukes the group permanently. "
                "Founder-only, irreversible, and the vault / reserve / "
                "LP positions get unwound first (members get their "
                "share of the reserve back; vault token is burned).\n"
                f"`{p}group transfer @member` -- propose a founder "
                f"handover. Target accepts with `{p}group transfer accept` "
                f"or refuses with `{p}group transfer decline`. You can "
                f"cancel with `{p}group transfer cancel`, or check "
                f"the open proposal with `{p}group transfer status`.",
                False,
            )
            .build()
        )

    if category == "members":
        return (
            _e("👥 Members & Rewards", C_PURPLE)
            .description(
                "How players join + leave the group, and how mining "
                "rewards split between members on every block."
            )
            .field(
                "Join / leave flow",
                f"`{p}group join <name or tag>` -- request a public group; "
                "you're in immediately. Invite-only groups need a "
                "pending invite first.\n"
                f"`{p}group invite @member` -- founder pings someone "
                "with an invite.\n"
                f"`{p}group accept <group name>` / `decline <group name>` "
                "-- the invitee responds.\n"
                f"`{p}group leave` -- step out any time. Your share of "
                "vault / reserve does **not** come with you.",
                False,
            )
            .field(
                "Reward split modes  (founder picks)",
                f"`{p}group weightmode equal` -- every member gets an "
                "equal slice of the mined reward, regardless of how "
                "much hashrate they contributed. Simplest, fairest "
                "for casual groups.\n"
                f"`{p}group weightmode hashrate` -- each member's share "
                "scales with their **hashrate × weight**. Bigger rigs "
                "earn proportionally more. Use this when you want "
                "miners to be rewarded for actual contribution.",
                False,
            )
            .field(
                "Per-member weights  (hashrate mode only)",
                f"`{p}group setweight @member <weight>` -- multiplier on "
                "that member's hashrate when computing the split. "
                "Default is `1.0`. Founder might set their own to `2.0` "
                "to claim a bigger founder cut, or a lazy member to "
                "`0.5` as a soft warning. Equal mode ignores weights.",
                False,
            )
            .field(
                "Founder controls",
                f"`{p}group kick @member` -- remove someone from the "
                "group. Their accumulated share of vault / reserve "
                "stays with the group.\n"
                f"`{p}group transfer @member` -- hand the founder seat "
                "to another member (see **Manage**).",
                False,
            )
            .build()
        )

    if category == "mine":
        return (
            _e("⛏️ Mining & Group Token", C_PURPLE)
            .description(
                "**The flow.** Each member's mining rigs contribute "
                "hashrate to the GROUP's pool. When the group's "
                "combined hashrate hits a block on whatever PoW chain "
                "you're targeting, the block reward is split three "
                "ways:\n\n"
                "  **1.** Reserve cut -> `reserve_usd` (you set the %)\n"
                "  **2.** Vault mint -> custom group token (if bound)\n"
                "  **3.** Member split -> the rest goes to members "
                "by weight mode\n\n"
                "Then those vault tokens can be staked into LP for "
                "swap fees, the reserve buys upgrades, and members "
                "spend their cut however they want."
            )
            .field(
                "Pick a chain  (12h cooldown between switches)",
                f"`{p}group mine mta` -- mine Moneta Chain. Slow, "
                "stable, top-tier reward.\n"
                f"`{p}group mine sun` -- mine Sun Network. Faster blocks, "
                "smaller per-block reward.\n\n"
                f"Switching reassigns **every member's rigs** to the "
                f"new chain. Use `{p}group info` for the current chain + "
                f"member rig counts; `{p}group token info` for live token "
                "stats once a token is bound.",
                False,
            )
            .field(
                "Mint a custom token  (one-time setup)",
                f"  **1.** `{p}group set tag=XXXX` -- pick a 1-4 char "
                "symbol (e.g. `WOLF`, `OG`, `KING`).\n"
                f"  **2.** `{p}group token network <mta|sun>` -- bind "
                "the token to that PoW chain. **Founder-only, "
                "ONE-TIME, IRREVERSIBLE.** Once bound, mining the "
                "chosen chain mints the token into your group's vault "
                "alongside the chain's native rewards.\n\n"
                "Until you bind, mining still pays the chain's native "
                "reward (MTA / SUN) -- you just don't get the bonus "
                "vault-token mint.",
                False,
            )
            .field(
                "Inspect / spend the token",
                f"`{p}group token info` -- live snapshot: symbol, "
                "circulating supply, oracle price, vault balance, "
                "and every LP pool the token sits in (with USD "
                "values).\n\n"
                "Once your vault has a balance, push it into LP via "
                f"**LP Treasury** (`{p}group lp deposit <pct>`) to "
                "earn swap fees on every player trade through your "
                "token's pool. The pool starts at the genesis ratio "
                "and price-discovers as players buy/sell.",
                False,
            )
            .footer(
                "Tag first, network bind second. The bind is a "
                "permanent commitment -- choose your chain carefully."
            )
            .build()
        )

    if category == "lp":
        return (
            _e("\U0001F3DB LP Treasury (Founder)", C_PURPLE)
            .description(
                "**TL;DR.** Liquidity pools (LP) are two-token vending "
                "machines. Players swap between the tokens at a price "
                "set by the ratio of the reserves, paying a small fee "
                "on every trade. Whoever supplied the reserves owns "
                "the fees, in proportion to their share of the pool. "
                "Your group can plug its vault and its USD reserve "
                "into the pool to harvest those fees.\n\n"
                "**Two ways to add liquidity** -- pick by what you're "
                "spending:"
            )
            .field(
                "\U0001F4CA  Status",
                f"`{p}group lp status`  -- live snapshot: vault balance, "
                "pool reserves on both sides, your group's LP share, "
                "lifetime deposited / withdrawn, last action timestamp.",
                False,
            )
            .field(
                "A.  Vault GROUP_TOKEN -> LP  (single-sided, slippage-priced)",
                "**What it does:** takes a slice of the GROUP_TOKEN "
                "sitting in your group's vault and dumps it into the "
                "GROUP_TOKEN/<wrapped-coin> pool's token side -- mMTA "
                "for Moneta Chain groups, mSUN for Sun Network. The "
                "wrapped-coin side does not move, so the ratio shifts "
                "and **the price of GROUP_TOKEN goes down**. The bigger "
                "the deposit relative to the pool, the bigger the drop -- "
                "that slippage IS the cost of the deposit.\n\n"
                "**Worked example.** Vault holds 100,000 MYTKN. Pool "
                "currently has 1,000,000 MYTKN reserve. "
                f"`{p}group lp deposit 10` pushes 10,000 MYTKN (10% "
                "of vault) into the pool. Pool reserve becomes "
                "1,010,000 MYTKN against the same mMTA; price drops "
                "~1%. Your group's LP share grows by the proportional "
                "amount.\n\n"
                "**Caps.** Max **25%** of vault per action, **24-hour** "
                "cooldown between actions. Keeps an impulsive deposit "
                "from cratering the price.\n\n"
                f"`{p}group lp deposit <pct>` (alias `add`)\n"
                f"`{p}group lp withdraw <pct>` (aliases `remove`, `pull`)  "
                "-- pulls pct% of the pool reserve back to vault. Same caps.",
                False,
            )
            .field(
                "B.  Reserve USD -> LP  (double-sided, no price impact)",
                "**What it does:** pulls USD from the group's "
                "`reserve_usd`, splits it 50/50 at oracle prices, "
                "adds liquidity to both sides at the current ratio. "
                "Both reserves grow proportionally so **price does "
                "not move** -- the safe way to add capital without "
                "slippage.\n\n"
                "**Works on any pool** your group has (or wants) a "
                "position in -- your own GROUP_TOKEN/USD pool, a "
                "partnership pool shared with another group, or "
                "any other pool. Cost basis is tracked per-group so "
                "you only ever harvest your own slice.\n\n"
                "**Example.** Reserve holds $5,000. "
                f"`{p}group lp topup MYTKN USD 1000` pulls $1,000, "
                "splits $500 / $500, mints LP at the current ratio, "
                "bumps your cost basis by $1,000.\n\n"
                "**No 25% cap, no 24h cooldown** -- just bring the USD.\n\n"
                f"`{p}group lp topup <TOKEN_A> <TOKEN_B> <USD>` "
                f"(aliases `addlp`, `fund`).  "
                f"Same as `{p}group pool deposit`.",
                False,
            )
            .field(
                "\U0001F4B0  Harvest -- claim the fees",
                "Every time someone swaps through the pool, a small "
                "fee gets baked into the reserves. Your group's slice "
                "of those fees is the **gain over your cost basis** "
                "(the USD value of your LP position now, minus the USD "
                "you put in). Harvest pays that delta back to "
                "`reserve_usd` without unwinding the LP itself, so the "
                "position keeps earning.\n\n"
                f"`{p}group lp harvest <TOKEN_A> <TOKEN_B>` (alias `claim`).  "
                f"Same as legacy `{p}group pool harvest`.\n"
                "**24-hour cooldown** per pool. If the position is "
                "below cost basis (e.g. a token-side dump), harvest "
                "refuses to pay -- you're not supposed to take "
                "profit at a loss.",
                False,
            )
            .field(
                "\U0001F91D  Cross-group partnerships",
                f"`{p}group pool propose <other group>`  -- propose a "
                "shared pool with another group. On accept, BOTH groups "
                "auto-seed liquidity from their vaults (up to 5% each, "
                f"capped at {fmt_usd(Config.GROUP_POOL_SEED_MAX_USD)} "
                "per side) and split the initial LP 50/50.\n"
                f"`{p}group pool accept <id>`  ·  `{p}group pool decline <id>`  ·  "
                f"`{p}group pool list`  ·  `{p}group pool cancel`  -- "
                "manage the partnership inbox.\n\n"
                "**After acceptance, EITHER founder can keep adding "
                "to the pool independently.**  Each group's "
                "contribution is tracked separately in "
                "`group_lp_positions` so cost basis, fees earned, "
                "and harvest entitlements stay per-group:\n"
                f"  · Founder A runs `{p}group lp topup A_TOKEN B_TOKEN 500` "
                "→ pulls $500 from Group A's reserve, mints LP for "
                "Group A's slice.\n"
                f"  · Founder B runs `{p}group lp topup A_TOKEN B_TOKEN 500` "
                "→ pulls $500 from Group B's reserve, mints LP for "
                "Group B's slice.\n"
                "  · Both groups now own a bigger share of the pool "
                "and earn proportional fees. Each calls "
                f"`{p}group lp harvest A_TOKEN B_TOKEN` to claim only "
                "their own gains.",
                False,
            )
            .field(
                "\U0001F501  Auto-compound?",
                "Group LP doesn't auto-compound. Harvested fees land in "
                "`reserve_usd` and you decide what to do next:\n"
                f"  · `{p}group lp topup` -- recycle the fees back into "
                "the same pool (compound it manually).\n"
                f"  · `{p}group upgrade buy` -- spend on Hall upgrades.\n"
                f"  · `{p}group reserve` -- view the breakdown.\n\n"
                "The auto-compound toggle is on **Gamba** stakes only "
                f"(`{p}gamba autocompound on`).",
                False,
            )
            .footer(
                "Founder-only on every command. Path A moves price; "
                "Path B doesn't. Harvest converts fees into reserve USD."
            )
            .build()
        )

    if category == "pool":
        return (
            _e("💧 LP Pools", C_PURPLE)
            .description(
                "Cross-group token pools let two groups share liquidity. "
                "On acceptance the pool is auto-seeded from each group's vault "
                f"(up to 5% of vault value, capped at {fmt_usd(Config.GROUP_POOL_SEED_MAX_USD)} per side). "
                "Both groups hold 50% of the initial LP and earn swap fees."
            )
            .field(f"`{p}group pool propose <group name>`",
                "Send a pool partnership proposal to another group (founder only).", False)
            .field(f"`{p}group pool accept <proposal id>`",
                "Accept an incoming proposal and create the pool.", False)
            .field(f"`{p}group pool decline <proposal id>`",
                "Decline an incoming proposal.", False)
            .field(f"`{p}group pool list`",
                "List incoming and outgoing proposals for your group.", False)
            .field(f"`{p}group pool cancel`",
                "Cancel all your outgoing proposals.", False)
            .field(f"`{p}group pool harvest <TOKEN_A> <TOKEN_B>`",
                "Claim accumulated LP fee earnings to `reserve_usd`. "
                "24-hour cooldown per pool. Proceeds are valued at current prices.", False)
            .footer(f"Users can also add LP: {p}trade pool add TOKEN_A TOKEN_B amount_a amount_b")
            .build()
        )

    if category == "hall":
        return (
            _e("🏛️ Group Hall", C_PURPLE)
            .description(
                "A private Discord thread that only your members can "
                "access. Non-members see a locked gate. The Hall is "
                "where Atmosphere upgrades take effect, where you "
                "can run game commands without bot-channel "
                "restrictions, and where prefixless mode lives."
            )
            .field(
                "Open / close / info",
                f"`{p}group hall open` -- founder-only. Creates the "
                "private thread under the configured `grouphall_channel`. "
                "Members are invited automatically.\n"
                f"`{p}group hall close` -- archive the Hall. Founder "
                f"only. Re-open it any time with `{p}group hall open`.\n"
                f"`{p}group hall info` -- thread link + member count + "
                "any active upgrades.",
                False,
            )
            .field(
                "Why the Hall is special",
                "**1. Bot-channel bypass.** If the server admin "
                "restricts game commands to a few `bot_channels`, "
                "the Hall thread bypasses that gate entirely. "
                "Members can run `,fish cast`, `,chess move`, "
                "`,gamba stake`, etc inside the Hall without an "
                "admin wedging the thread into bot_channels.\n\n"
                "**2. Atmosphere bonuses.** Atmosphere-line upgrades "
                "give passive % bonuses that ONLY apply when the "
                "command runs inside the Hall (e.g. +5% on gambling "
                "wins, +5% on daily / work). See **Upgrades**.",
                False,
            )
            .field(
                "Prefixless mode  (founder toggle)",
                f"`{p}group hall prefix on` -- inside the Hall, members "
                "can type bare commands without any prefix:\n"
                "  `fish cast`  instead of  `,fish cast`\n"
                "  `gamba stake all`  instead of  `,gamba stake all`\n\n"
                "The guild prefix and the comma prefix still work "
                "alongside this -- prefixless is additive, not "
                f"replacement. `{p}group hall prefix off` flips it back.",
                False,
            )
            .build()
        )

    if category == "reserve":
        return (
            _e("💰 Reserve  (the group's USD treasury)", C_PURPLE)
            .description(
                "`reserve_usd` is the group's shared USD treasury. "
                "Founders spend it on **Hall upgrades**, **LP topups** "
                f"(`{p}group lp topup`), and **partnership pool seeding**. "
                "Members can't withdraw -- the reserve only flows out "
                "via founder spending."
            )
            .field(
                "Three income sources",
                "**1. Reserve Rate** -- the % of every mined block "
                "that's converted to USD at oracle and added to the "
                f"reserve. Founder sets this with `{p}group reserve set "
                "<0-100>`. Higher rate = bigger reserve, smaller "
                "per-member payout. 5-15% is a common range.\n\n"
                "**2. LP yield** -- swap fees from any group LP "
                f"position. Claim with `{p}group lp harvest`. Fees "
                "deposit straight to `reserve_usd`.\n\n"
                "**3. Tribute upgrades** -- system-funded grants on "
                "member fishing / farming / delve / crafting cashouts. "
                "Bought from the **Tribute** upgrade line (see "
                "**Upgrades**). Each member cashout adds a small % "
                "to the reserve.",
                False,
            )
            .field(
                "Worked example",
                "Reserve rate **10%**. Group mines a $1,000 block.\n"
                "  · $100 -> `reserve_usd`\n"
                "  · Optional: vault token minted alongside (no $)\n"
                "  · $900 -> split among members by weight mode\n\n"
                "Add a Trade Tribute upgrade (3% on member crafting "
                "cashouts). A member sells $5,000 of crafted items "
                "for USD -> $150 lands in `reserve_usd` automatically.",
                False,
            )
            .field(
                "Read / write",
                f"`{p}group reserve` -- total value, balance "
                "breakdown, reserve rate, every active income source, "
                "spend history.\n"
                f"`{p}group reserve set <0-100>` -- founder-only, "
                "no cooldown.",
                False,
            )
            .footer(
                f"Spend with {p}group upgrade buy / {p}group lp topup / "
                "partnership pool seeding."
            )
            .build()
        )

    if category == "upgrade":
        return (
            _e("🔧 Upgrades  (permanent group-wide perks)", C_PURPLE)
            .description(
                "Founder-only purchases paid from `reserve_usd`. "
                "Each upgrade is permanent; many require a lower-tier "
                f"upgrade in the same line first. `{p}group upgrade list` "
                "shows costs + prerequisites + effects."
            )
            .field(
                "🔥  Atmosphere -- Hall ambiance + Hall-only bonuses",
                "Each tier adds a passive % bonus that **only applies "
                "to commands run inside the Hall thread**. "
                "Tier 1 (Hearth, ~$35k): +5% gambling wins. "
                "Tier 2 (Trophy Wall, ~$90k): +5% daily reward. "
                "Tier 3 (Gilded Arch, ~$280k): +5% work earnings. "
                "Stack with Industry for compounding bonuses; the "
                "Hall is the most efficient place for an active "
                "group to play.",
                False,
            )
            .field(
                "📋  Access -- unlock more command surfaces in the Hall",
                "Some commands are gated outside the Hall by default. "
                "Access upgrades open them up for members:\n"
                "  · Command Board (~$75k): unlocks Earn (work, daily, "
                "faucet) inside the Hall.\n"
                "  · Trading Desk: unlocks Trade.\n"
                "  · ... more tiers per Access line.\n"
                "Useful when the bot-channel restriction would "
                "otherwise force members to leave the Hall to run "
                "an earn / trade flow.",
                False,
            )
            .field(
                "📈  Expansion -- more member slots",
                "Default member cap is the base group cap. Each "
                "Expansion tier adds slots so you can scale up to "
                "larger groups. Buy these before you start hitting "
                "join refusals.",
                False,
            )
            .field(
                "🏭  Industry -- group-wide minigame bonuses",
                "Apply EVERYWHERE the upgrade's minigame is played, "
                "not just inside the Hall. Examples: Fishing yield "
                "+%, Farming HRV +%, Delve loot +%, Crafting XP +%. "
                "These are the highest-impact upgrades for an "
                "active group because they reward existing play "
                "without changing behavior.",
                False,
            )
            .field(
                "💼  Tribute -- system-funded reserve income",
                "On every member cashout in the matching minigame, "
                "the SYSTEM contributes a small % to `reserve_usd` "
                "(the member's payout is unchanged). Examples: 3% on "
                "fishing cashouts, 3% on craft sales, etc. "
                "Stacks across lines -- a fully-Tributed group can "
                "fund LP topups + further upgrades just from "
                "members playing.",
                False,
            )
            .field(
                "Commands",
                f"`{p}group upgrade list` -- browse every upgrade by "
                "line, with cost / requires / effect.\n"
                f"`{p}group upgrade buy <id>` -- purchase. Founder-only. "
                "Reserve must hold the cost; prerequisites must be met.",
                False,
            )
            .build()
        )

    # Fallback
    return _build_group_help_embed("overview", p)


class CreateGroupView(discord.ui.View):
    """Shows a 'Create a Group' button that opens a modal for the group name."""

    def __init__(self, ctx: DiscoContext, *, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self._ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._ctx.author.id:
            await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Create a Group", style=discord.ButtonStyle.primary)
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = InputModal(
            title="Create Mining Group",
            label="Group Name",
            placeholder="e.g. Miners United",
            max_length=32,
        )
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value is None:
            return

        name = modal.value.strip()
        ok, err = validate_group_name(name)
        if not ok:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return

        existing = await self._ctx.db.get_user_mining_group(self._ctx.author.id, self._ctx.guild_id)
        if existing:
            await interaction.followup.send("❌ You're already in a group. Leave it first.", ephemeral=True)
            return

        collision = await self._ctx.db.get_mining_group(self._ctx.guild_id, name=name)
        if collision:
            await interaction.followup.send(f"❌ A group named **{name}** already exists.", ephemeral=True)
            return

        grp = await self._ctx.db.create_mining_group(self._ctx.guild_id, name, self._ctx.author.id)

        embed = (
            card("⛏️ Mining Group Created", color=C_SUCCESS)
            .author(self._ctx.author.display_name, icon_url=self._ctx.author.display_avatar.url)
            .field("Name",    f"**{name}**",          True)
            .field("ID",      f"`{grp['group_id']}`", True)
            .field("Founder", self._ctx.author.mention, True)
            .footer("Use .group set to add description/tag/image")
            .build()
        )
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.message.edit(view=self)  # type: ignore[union-attr]
        await self._ctx.reply(embed=embed, mention_author=False)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        self.stop()

_WEIGHT_MODES = {"hashrate", "equal", "custom"}


def _format_upgrades(purchased_ids: list[str]) -> str:
    """Return a formatted list of purchased Hall upgrades, or 'None'."""
    hall = Config.GROUP_HALL_UPGRADES
    names = [hall[uid]["name"] for uid in purchased_ids if uid in hall]
    return ", ".join(names) if names else "None"


def _format_upgrade_bonuses(purchased_ids: list[str]) -> str:
    """Return a compact summary of active Hall upgrade effects."""
    hall = Config.GROUP_HALL_UPGRADES
    gambling_bonus = 0.0
    daily_bonus = 0.0
    work_bonus = 0.0
    fishing_bonus = 0.0
    farming_bonus = 0.0
    dungeon_bonus = 0.0
    crafting_bonus = 0.0
    tribute_fishing = 0.0
    tribute_farming = 0.0
    tribute_dungeon = 0.0
    tribute_crafting = 0.0
    tribute_multiplier = 0.0
    extra_slots = 0
    unlocks: set[str] = set()
    token_trading = False

    for uid in purchased_ids:
        cfg = hall.get(uid)
        if not cfg:
            continue
        effect = cfg.get("effect", {})
        gambling_bonus     += effect.get("hall_gambling_bonus", 0.0)
        daily_bonus        += effect.get("hall_daily_bonus", 0.0)
        work_bonus         += effect.get("hall_work_bonus", 0.0)
        fishing_bonus      += effect.get("member_fishing_bonus", 0.0)
        farming_bonus      += effect.get("member_farming_bonus", 0.0)
        dungeon_bonus      += effect.get("member_dungeon_bonus", 0.0)
        crafting_bonus     += effect.get("member_crafting_bonus", 0.0)
        tribute_fishing    += effect.get("tribute_fishing_pct", 0.0)
        tribute_farming    += effect.get("tribute_farming_pct", 0.0)
        tribute_dungeon    += effect.get("tribute_dungeon_pct", 0.0)
        tribute_crafting   += effect.get("tribute_crafting_pct", 0.0)
        tribute_multiplier += effect.get("tribute_multiplier", 0.0)
        extra_slots        += int(effect.get("group_max_members", 0))
        if "hall_unlock" in effect:
            unlocks.add(effect["hall_unlock"])
        if effect.get("group_token_trading"):
            token_trading = True

    # Tribute multiplier scales every base tribute %, mirroring the
    # services/group_reserve.py math so the embed shows the real amount
    # the reserve will receive.
    trib_mult = 1.0 + tribute_multiplier
    eff_t_fish = tribute_fishing  * trib_mult
    eff_t_farm = tribute_farming  * trib_mult
    eff_t_delv = tribute_dungeon  * trib_mult
    eff_t_craft = tribute_crafting * trib_mult

    parts: list[str] = []
    if gambling_bonus > 0:
        parts.append(f"🎲 Hall gambling: +{gambling_bonus*100:.0f}%")
    if daily_bonus > 0:
        parts.append(f"📅 Hall daily: +{daily_bonus*100:.0f}%")
    if work_bonus > 0:
        parts.append(f"💼 Hall work: +{work_bonus*100:.0f}%")
    if fishing_bonus > 0:
        parts.append(f"🎣 Fishing payouts: +{fishing_bonus*100:.0f}%")
    if farming_bonus > 0:
        parts.append(f"🌱 Farming payouts: +{farming_bonus*100:.0f}%")
    if dungeon_bonus > 0:
        parts.append(f"⛓️ Delve payouts: +{dungeon_bonus*100:.0f}%")
    if crafting_bonus > 0:
        parts.append(f"⚒️ Crafting payouts: +{crafting_bonus*100:.0f}%")
    if eff_t_fish > 0:
        parts.append(f"🪙 Fishing tribute -> reserve: {eff_t_fish*100:.2f}%")
    if eff_t_farm > 0:
        parts.append(f"🪙 Farming tribute -> reserve: {eff_t_farm*100:.2f}%")
    if eff_t_delv > 0:
        parts.append(f"🪙 Delve tribute -> reserve: {eff_t_delv*100:.2f}%")
    if eff_t_craft > 0:
        parts.append(f"🪙 Crafting tribute -> reserve: {eff_t_craft*100:.2f}%")
    if extra_slots > 0:
        parts.append(f"🏗️ Extra slots: +{extra_slots}")
    if unlocks:
        parts.append(f"📋 Unlocked in Hall: {', '.join(sorted(unlocks))}")
    if token_trading:
        parts.append("💹 Group token: trading enabled")
    if not parts:
        return "None"
    return "\n".join(parts)


async def _spendable_reserve_total(ctx: DiscoContext, grp: dict) -> float:
    """Sum the spendable USD value of every reserve bucket the group owns.

    USD bucket + MTA bucket (at live MTA oracle) + group token vault (at
    live token oracle). This is the single source of truth for "how much
    can this group spend on a Hall upgrade right now". Every embed and
    affordability check MUST go through this helper -- previously the
    ,group hall info / ,group upgrade list / ,group upgrade buy paths
    each rolled their own and drifted apart.
    """
    reserve_usd = grp.h("reserve_usd")
    reserve_btc = grp.h("reserve_btc")
    vault_bal   = float(grp.get("vault_token_bal") or 0.0)
    tok_sym     = grp.get("token_symbol") or ""

    btc_price_row = await ctx.db.get_price("MTA", ctx.guild_id)
    btc_price = float(btc_price_row["price"]) if btc_price_row else 0.0
    tok_price = 0.0
    if tok_sym:
        tok_price_row = await ctx.db.get_price(tok_sym, ctx.guild_id)
        tok_price = float(tok_price_row["price"]) if tok_price_row else 0.0

    return reserve_usd + reserve_btc * btc_price + vault_bal * tok_price


async def _resolve_token_price(db, symbol: str, guild_id: int) -> float:
    """Resolve a token's USD price, treating USD itself as a constant 1.0.

    USD is the fiat base currency, not a token -- it is never a row in
    crypto_prices, so ``db.get_price('USD', ...)`` always returns None.
    Group LP pools legitimately pair a group token against USD (e.g.
    ``,group lp topup usd cook``), so every price lookup that may see USD
    on one side MUST go through this helper instead of get_price directly.
    Returns 0.0 when a real token has no price configured.
    """
    if (symbol or "").upper() == "USD":
        return 1.0
    row = await db.get_price(symbol, guild_id)
    return float(row["price"]) if row and row.get("price") else 0.0


async def _ensure_group_token(db, guild_id: int, group_name: str, tag: str, creator_id: int = 0) -> str | None:
    """Auto-register a guild token for a mining group when a tag is first set.

    Returns the symbol that was created, or None if skipped (collision or empty symbol).
    Group tokens are created with trading enabled by default.

    All group tokens live on the bridged ``"Moon Network"`` pseudo-network so
    that cross-group partnership pools (e.g. ``COOK/FEM``) can swap without
    tripping the cross-network swap blocker. The founder's mining chain is
    tracked separately in ``mining_groups.token_network`` for vault-pool
    pairing and block rewards.
    """
    sym, tok_name = make_group_token(group_name, tag)
    if not sym:
        return None
    # Hard reject any symbol that collides with a built-in token. Built-ins
    # share the global ``crypto_prices``/``pools``/tx-history namespace, so a
    # group token named ``MTA`` would silently shadow native MTA and create
    # un-swappable wallet rows on Moon Network. The dict returned by
    # ``get_all_tokens_for_guild`` skips colliding guild_tokens entries on
    # purpose, so checking ``Config.TOKENS`` directly is the only way to catch
    # this collision before the DB row is written.
    if sym in Config.TOKENS:
        return None
    existing = await db.get_all_tokens_for_guild(guild_id)
    # get_all_tokens_for_guild returns a dict keyed by symbol
    taken = set(existing)
    if sym in taken:
        return None

    # Group tokens get the same 100M default cap as the network coins
    # (DFUN, MOON, REEL, GBC) so PoW group rewards can't mint past the
    # configured tokenomics.  Group operators can later edit this through
    # the contract surface if a different ceiling is needed.
    await db.add_guild_token(
        guild_id, sym, tok_name, "⛏️", "PoW", "Moon Network", 0.01, 0.05,
        max_supply=_tr(100_000_000),
    )

    # Mark as group token, assign on-chain identity, and unlock trading.
    # Group tokens are auto-enabled on creation so founders do not need an
    # admin step to activate their own token. Admins can still disable a
    # specific token via `.admin grouptoken disable <symbol>` if needed.
    contract_address = make_contract_address(guild_id, creator_id, sym)
    token_hash = make_token_hash(guild_id, sym)
    await db.execute(
        "UPDATE guild_tokens SET token_type='group', contract_address=$1, token_hash=$2, "
        "trading_enabled=TRUE "
        "WHERE guild_id=$3 AND symbol=$4",
        contract_address, token_hash, guild_id, sym,
    )

    await db.execute(
        "INSERT INTO crypto_prices (symbol, guild_id, price, open_price, day_high, day_low) "
        "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING",
        sym, guild_id, 0.01, 0.01, 0.01, 0.01,
    )

    # Seed genesis liquidity pools so the new token is tradeable immediately.
    # Silent-on-failure: if a pair can't be seeded (price lookup missing, etc.)
    # the group is still created -- pool seeding is a nice-to-have, not a hard
    # requirement for group creation to succeed. The logic lives in
    # database/pools.py so startup backfill and creation-time seeding share
    # a single source of truth.
    try:
        await db.seed_group_genesis_pools(guild_id, sym)
    except Exception:
        log.exception("group token %s: genesis pool seeding failed", sym)

    return sym


class Groups(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_check(self._hall_gate_check)
        global _HALL_BLOCKED_COMMANDS
        _HALL_BLOCKED_COMMANDS = frozenset(
            name
            for cmd in self.bot.commands
            for name in (cmd.name, *cmd.aliases)
            if name not in _HALL_ALWAYS_ALLOWED
            and cmd.name not in _HALL_PRIVILEGED_GROUPS
        )

    async def cog_unload(self) -> None:
        self.bot.remove_check(self._hall_gate_check)

    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "groups")

    # ── Redis cache helpers ───────────────────────────────────────────────────

    def _r(self):
        return getattr(self.bot.bus, "_redis", None)

    async def _cache_get(self, key: str) -> str | None:
        r = self._r()
        if r is None:
            return None
        try:
            val = await r.get(key)
            return val.decode() if isinstance(val, bytes) else val
        except Exception:
            return None

    async def _cache_set(self, key: str, value: str, ttl: int = 300) -> None:
        r = self._r()
        if r is None:
            return
        try:
            await r.setex(key, ttl, value)
        except Exception:
            pass

    async def _cache_del(self, *keys: str) -> None:
        r = self._r()
        if r is None:
            return
        try:
            await r.delete(*keys)
        except Exception:
            pass

    # ── Hall gate check ───────────────────────────────────────────────────────

    async def _hall_gate_check(self, ctx: DiscoContext) -> bool:
        """Global check: block commands in Group Hall threads unless unlocked by Hall upgrades.

        Sets ``ctx.hall_bonus`` dict on the context so earning cogs can apply
        the appropriate percentage bonus (gambling, work, daily).
        """
        ctx.hall_bonus = {}  # type: ignore[attr-defined]
        if not isinstance(ctx.channel, discord.Thread) or not ctx.guild:
            return True
        if ctx.command is None:
            return True
        root = ctx.command.root_parent or ctx.command
        root_name = root.name
        if root_name in _HALL_ALWAYS_ALLOWED or root_name not in _HALL_BLOCKED_COMMANDS:
            return True

        # Cache layer: is this thread a Group Hall?
        t_key = f"discoin:hall:thread:{ctx.channel.id}"
        cached = await self._cache_get(t_key)
        if cached == "none":
            return True
        if cached is not None:
            group_id, guild_id_str = cached.split(":", 1)
            hall_guild_id = int(guild_id_str)
        else:
            hall_row = await self.bot.db.fetch_one(
                "SELECT group_id, guild_id FROM mining_groups WHERE hall_thread_id=$1",
                ctx.channel.id,
            )
            if not hall_row:
                await self._cache_set(t_key, "none")
                return True
            group_id = hall_row["group_id"]
            hall_guild_id = hall_row["guild_id"]
            await self._cache_set(t_key, f"{group_id}:{hall_guild_id}")

        # Verify user is a group member
        member_row = await self.bot.db.fetch_one(
            "SELECT 1 FROM mining_group_members WHERE user_id=$1 AND guild_id=$2 AND group_id=$3",
            ctx.author.id, hall_guild_id, group_id,
        )
        if not member_row:
            raise commands.CheckFailure(
                "You must be a member of this group to use commands in its Hall."
            )

        # Load purchased upgrades
        upgrades = await self.bot.db.fetch_all(
            "SELECT upgrade_id FROM group_upgrades WHERE guild_id=$1 AND group_id=$2",
            hall_guild_id, group_id,
        )
        purchased = {u["upgrade_id"] for u in upgrades}
        hall_cfg = Config.GROUP_HALL_UPGRADES

        # Compute earned bonuses for this context
        gambling_bonus = 0.0
        work_bonus = 0.0
        daily_bonus = 0.0
        unlocked_categories: set[str] = set()
        for uid in purchased:
            eff = hall_cfg.get(uid, {}).get("effect", {})
            gambling_bonus += eff.get("hall_gambling_bonus", 0.0)
            work_bonus     += eff.get("hall_work_bonus", 0.0)
            daily_bonus    += eff.get("hall_daily_bonus", 0.0)
            if "hall_unlock" in eff:
                unlocked_categories.add(eff["hall_unlock"])

        ctx.hall_bonus = {  # type: ignore[attr-defined]
            "gambling": gambling_bonus,
            "work": work_bonus,
            "daily": daily_bonus,
        }

        unlocked_cogs: set[str] = set()
        for cat in unlocked_categories:
            unlocked_cogs |= _HALL_UNLOCK_COGS.get(cat, frozenset())
        if ctx.command.cog_name in unlocked_cogs:
            return True

        p = ctx.prefix or ","
        await ctx.reply(
            embed=card(
                "Command Locked",
                description=(
                    f"`{p}{root_name}` is not yet unlocked in this Hall.\n\n"
                    f"The group founder can purchase **Hall upgrades** to unlock command "
                    f"categories:\n"
                    f"- **Command Board** (`command_board`) - unlocks Earn commands\n"
                    f"- **Trading Desk** (`trading_desk`) - unlocks Trade commands\n"
                    f"- **DeFi Terminal** (`defi_terminal`) - unlocks DeFi commands\n"
                    f"- **Hall Hearth** (`hearth`) - unlocks gambling commands\n\n"
                    f"Purchase with `{p}group upgrade buy <id>`"
                ),
                color=C_ERROR,
            ).build(),
            mention_author=False,
        )
        raise commands.CheckFailure(f"`{root_name}` locked in Hall")

    # ── Hall thread helpers ───────────────────────────────────────────────────

    async def _get_hall_thread(self, guild: discord.Guild, thread_id: int) -> discord.Thread | None:
        th = guild.get_channel_or_thread(thread_id)
        if th is None:
            try:
                th = await self.bot.fetch_channel(thread_id)
            except Exception:
                return None
        return th if isinstance(th, discord.Thread) else None

    async def _hall_add_member(self, guild: discord.Guild, grp: dict, member: discord.Member) -> None:
        """Add a Discord member to the group's Hall thread, if one exists."""
        thread_id = grp.get("hall_thread_id")
        if not thread_id:
            return
        thread = await self._get_hall_thread(guild, thread_id)
        if thread:
            try:
                await thread.add_user(member)
            except Exception:
                pass

    async def _hall_remove_member(self, guild: discord.Guild, grp: dict, member: discord.abc.Snowflake) -> None:
        """Remove a Discord user from the group's Hall thread, if one exists."""
        thread_id = grp.get("hall_thread_id")
        if not thread_id:
            return
        thread = await self._get_hall_thread(guild, thread_id)
        if thread:
            try:
                await thread.remove_user(member)
            except Exception:
                pass

    # ── $group ────────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="group", aliases=["mg"], invoke_without_command=True, with_app_command=False)
    @guild_only
    async def group(self, ctx: DiscoContext) -> None:
        """Mining group commands. Use .group for an interactive help menu."""
        if await suggest_subcommand(ctx, self.group):
            return
        await GroupHelpView(ctx).send()

    # ── $group create ─────────────────────────────────────────────────────────

    @group.command(name="create")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_create(self, ctx: DiscoContext, *, name: str) -> None:
        """Create a new mining group. Append 'private' to make it invite-only.
        Usage: .group create <name> [private]"""
        private = False
        if "private" in name.lower():
            private = True
            name = name.replace("private", "").replace("Private", "").strip()

        ok, err = validate_group_name(name)
        if not ok:
            await ctx.reply_error(err)
            return

        existing_membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if existing_membership:
            g = await ctx.db.get_mining_group(ctx.guild_id, group_id=existing_membership["group_id"])
            g_name = g["name"] if g else existing_membership["group_id"]
            await ctx.reply_error(f"You're already in **{g_name}**. Leave it first with `.group leave`.")
            return

        collision = await ctx.db.get_mining_group(ctx.guild_id, name=name)
        if collision:
            await ctx.reply_error(f"A group named **{name}** already exists.")
            return

        grp = await ctx.db.create_mining_group(ctx.guild_id, name, ctx.author.id)
        # Set privacy if --private was passed
        if private:
            await ctx.db.update_mining_group_fields(ctx.guild_id, grp["group_id"], is_public=False)

        # Auto-derive a tag from the first 3 alphanumeric chars of the name
        auto_tag = _re.sub(r"[^A-Z0-9]", "", name.upper())[:3]
        if not auto_tag:
            auto_tag = "".join(w[0].upper() for w in name.split() if w)[:3]
        token_sym: str | None = None
        if auto_tag:
            await ctx.db.update_mining_group(ctx.guild_id, grp["group_id"], tag=auto_tag)
            token_sym = await _ensure_group_token(
                ctx.db, ctx.guild_id, name, auto_tag, ctx.author.id
            )

        privacy_str = "🔒 Invite-Only" if private else "Public"
        footer = "Use .group set to add description/tag/image"
        if private:
            footer += "  •  Use .group invite @user to add members"
        else:
            footer += f"  •  .group join {name} to recruit"
        token_note = (
            f"Token **{token_sym}** created and tradeable immediately.\n"
            f"Pools are auto-seeded on Moon Network: `MMTA/{token_sym}`, "
            f"`MSUN/{token_sym}`, and `MOON/{token_sym}`. Wrap native MTA or SUN "
            f"with `{ctx.prefix}moon wrap mta|sun <amount>` and swap it straight "
            f"into the token."
            if token_sym else ""
        )
        b = (
            card("⛏️ Mining Group Created", color=C_SUCCESS)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("Name",    f"**{name}**",                   True)
            .field("ID",      f"`{grp['group_id']}`",          True)
            .field("Privacy", privacy_str,                     True)
            .field("Founder", ctx.author.mention,              True)
            .field("Tag",     f"`{auto_tag}`" if auto_tag else "(none)", True)
        )
        if token_note:
            b.field("Group Token", token_note, False)
        b.footer(footer)
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── $group set ────────────────────────────────────────────────────────────

    @group.command(name="set")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_set(self, ctx: DiscoContext, *, raw: str = "") -> None:
        """Set group description/tag/image. Keys: description, tag, image (founder only)."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            is_mod = ctx.author.guild_permissions.manage_guild
            if not is_mod:
                await ctx.reply_error("Only the group founder can configure settings.")
                return

        if not raw.strip():
            await ctx.reply(
                "**Usage:** `.group set description=\"...\" tag=NGMI image=https://...`\n"
                "Keys: `description` `tag` (<=4 chars) `image`",
                mention_author=False,
            )
            return

        kv: dict[str, str] = {}
        for m in _re.finditer(r'(\w+)=(?:"([^"]*)"|([\S]+))', raw.replace("\n", " ")):
            key = m.group(1).lower()
            val = m.group(2) if m.group(2) is not None else m.group(3)
            kv[key] = val

        fields: dict[str, str] = {}
        if "description" in kv:
            desc = kv["description"][:200]
            ok, err = validate_group_description(desc)
            if not ok:
                await ctx.reply_error(err)
                return
            fields["description"] = sanitize_text(desc, allow_urls=False)
        if "tag" in kv:
            tag = sanitize_text(kv["tag"][:4]).upper()
            if has_scam_patterns(tag):
                await ctx.reply_error("Tag contains blocked content.")
                return
            if has_discord_entities(kv["tag"]):
                await ctx.reply_error("Tag cannot contain mentions, channels, or IDs.")
                return
            fields["tag"] = tag
        if "image" in kv:
            ok, err = validate_image_url(kv["image"][:512])
            if not ok:
                await ctx.reply_error(err)
                return
            fields["image_url"] = kv["image"][:512]

        if not fields:
            _edit_keys = ("description", "tag", "image")
            valid = ", ".join(f"`{k}`" for k in _edit_keys)
            await ctx.reply_error(f"No valid keys found. Valid: {valid}")
            return

        await ctx.db.update_mining_group(ctx.guild_id, grp["group_id"], **fields)

        # Auto-create a guild token when a tag is set for the first time
        if "tag" in fields:
            await _ensure_group_token(ctx.db, ctx.guild_id, grp["name"], fields["tag"], ctx.author.id)

        updated = ", ".join(f"`{k}`" for k in fields)
        await ctx.reply_success(f"Updated: {updated}", title="✅ Group Updated")

    # ── $group token network ──────────────────────────────────────────────────

    # Accepted short names -> canonical network name + coin symbol
    _POW_NETWORK_ALIASES: dict[str, tuple[str, str]] = {
        "sun":  ("Sun Network",     "SUN"),
        "mta":  ("Moneta Chain", "MTA"),
        "moneta": ("Moneta Chain", "MTA"),
    }

    @group.command(name="token")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_token(self, ctx: DiscoContext, *, raw: str = "") -> None:
        """Bind the group's token to a PoW mining chain for vault pairing.

        Group tokens themselves live on the bridged "Moon Network" so that
        cross-group swaps (e.g. COOK/FEM) work regardless of each founder's
        mining chain. This command picks the chain the vault pool pairs with
        and the coin the group mines for block rewards.

        Usage: .group token network <sun|mta>
        Usage: .group token info   -- show current token + vault balance"""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp:
            await ctx.reply_error("Group not found.")
            return

        parts = raw.strip().lower().split()
        # Preserve original case for arguments that need it (e.g. custom emoji
        # names which can be uppercase). Index aligns with `parts`.
        raw_parts = raw.strip().split()
        sub = parts[0] if parts else "info"

        # ── info ──────────────────────────────────────────────────────────────
        if sub == "info" or not raw.strip():
            sym     = grp.get("token_symbol") or ""
            net     = grp.get("token_network") or "Not set"
            bal     = float(grp.get("vault_token_bal") or 0.0)
            b = (
                card("⛏️ Group Token", color=C_AMBER)
                .field("Symbol",      sym or "(no tag set yet)", True)
                .field("Trades on",   "Moon Network (bridged)", True)
                .field("Mining Chain", net,                      True)
                .field("Vault",       f"{bal:,.4f} {sym}" if sym else "0", True)
            )
            if sym and net != "Not set":
                # Vault-locked pool: TOKEN / Moon-wrapped PoW coin (MMTA / MSUN).
                net_sym = Config.NETWORK_COINS.get(net, "")
                if net_sym:
                    from constants.moons import wrapped_coin as _wrapped_coin
                    wrapped_sym = _wrapped_coin(net_sym)
                    vault_pool_id, _, _ = ctx.db.make_pool_id(sym, wrapped_sym)
                    vault_pool = await ctx.db.get_pool(vault_pool_id, ctx.guild_id)
                    if vault_pool:
                        vra = _h(int(vault_pool["reserve_a"]))
                        vrb = _h(int(vault_pool["reserve_b"]))
                        vpr_a = await ctx.db.get_price(vault_pool["token_a"], ctx.guild_id)
                        vpr_b = await ctx.db.get_price(vault_pool["token_b"], ctx.guild_id)
                        vpa = float(vpr_a["price"]) if vpr_a else 0.0
                        vpb = float(vpr_b["price"]) if vpr_b else 0.0
                        vtvl = vra * vpa + vrb * vpb
                        b.field(
                            f"Vault Pool: {vault_pool['token_a']}/{vault_pool['token_b']}",
                            f"`{vra:,.4f} {vault_pool['token_a']}` ({fmt_usd(vra * vpa)}) "
                            f"| `{vrb:,.4f} {vault_pool['token_b']}` ({fmt_usd(vrb * vpb)})\n"
                            f"TVL: **{fmt_usd(vtvl)}** - vault managed",
                            False,
                        )

            if sym:
                # Group-to-group partnership pools
                gtp_pools = await ctx.db.fetch_all(
                    """SELECT * FROM pools
                       WHERE guild_id=$1 AND is_group_pool=TRUE AND vault_locked=FALSE
                         AND (token_a=$2 OR token_b=$2)""",
                    ctx.guild_id, sym.upper(),
                )
                for gp in gtp_pools:
                    gra = _h(int(gp["reserve_a"]))
                    grb = _h(int(gp["reserve_b"]))
                    pr_ga = await ctx.db.get_price(gp["token_a"], ctx.guild_id)
                    pr_gb = await ctx.db.get_price(gp["token_b"], ctx.guild_id)
                    pga = float(pr_ga["price"]) if pr_ga else 0.0
                    pgb = float(pr_gb["price"]) if pr_gb else 0.0
                    gtvl = gra * pga + grb * pgb
                    gp_lp = _h(int(gp["total_lp"]))
                    # Show the group's own LP position if any
                    my_lp_pos = await ctx.db.get_group_lp_position(
                        ctx.guild_id, grp["group_id"], gp["pool_id"],
                    )
                    my_lp_h = _h(int(my_lp_pos["lp_shares"])) if my_lp_pos else 0.0
                    my_pct = (my_lp_h / gp_lp * 100.0) if gp_lp > 0 else 0.0
                    b.field(
                        f"Group Pool: {gp['token_a']}/{gp['token_b']}",
                        f"`{gra:,.4f} {gp['token_a']}` ({fmt_usd(gra * pga)}) "
                        f"| `{grb:,.4f} {gp['token_b']}` ({fmt_usd(grb * pgb)})\n"
                        f"TVL: **{fmt_usd(gtvl)}** - your LP: `{my_lp_h:,.4f}` ({my_pct:.1f}%)",
                        False,
                    )

            await ctx.reply(embed=b.build(), mention_author=False)
            return

        # ── emoji ─────────────────────────────────────────────────────────────
        if sub == "emoji":
            is_founder = grp["founder_id"] == ctx.author.id
            is_mod     = ctx.author.guild_permissions.manage_guild
            if not is_founder and not is_mod:
                await ctx.reply_error(
                    "Only the group founder or a server moderator can change "
                    "the token emoji."
                )
                return

            tok_sym = grp.get("token_symbol") or ""
            if not tok_sym:
                await ctx.reply_error(
                    "Your group has no token yet. Set a tag first with `.group set tag=XXXX`."
                )
                return

            # Pull from raw_parts to preserve case (custom emoji names)
            new_emoji = raw_parts[1] if len(raw_parts) > 1 else ""
            if not new_emoji:
                await ctx.reply_error("Usage: `.group token emoji <emoji>`")
                return

            # Strip whitespace; clamp at 64 chars so a full custom emoji
            # (<a:longname:18-digit-id>) still fits without being truncated
            # into garbage. Unicode emoji are tiny so the cap only matters
            # for runaway input.
            new_emoji = new_emoji.strip()[:64]
            if not new_emoji:
                await ctx.reply_error("Emoji cannot be empty.")
                return
            if has_discord_entities(new_emoji):
                # Allow custom Discord emoji (<:name:id> / <a:name:id>) but block
                # mentions, channel links and user IDs
                if not _re.fullmatch(r"<a?:\w+:\d+>", new_emoji):
                    await ctx.reply_error("Emoji cannot contain mentions or channel links.")
                    return

            await ctx.db.execute(
                "UPDATE guild_tokens SET emoji=$1 WHERE guild_id=$2 AND symbol=$3",
                new_emoji, ctx.guild_id, tok_sym,
            )
            await ctx.reply_success(
                f"Token **{tok_sym}** emoji is now {new_emoji}",
                title="✅ Token Emoji Updated",
            )
            return

        # ── network ───────────────────────────────────────────────────────────
        if sub != "network":
            await ctx.reply_error(
                f"Unknown sub-command `{sub}`. "
                f"Usage: `.group token network <sun|mta>`, "
                f"`.group token emoji <emoji>`, or `.group token info`"
            )
            return

        # Founder or a server moderator can bind the network
        is_founder = grp["founder_id"] == ctx.author.id
        is_mod     = ctx.author.guild_permissions.manage_guild
        if not is_founder and not is_mod:
            await ctx.reply_error(
                "Only the group founder or a server moderator can set the "
                "token network."
            )
            return

        net_arg = parts[1] if len(parts) > 1 else ""
        if not net_arg:
            nets = ", ".join(f"`{k}`" for k in self._POW_NETWORK_ALIASES)
            await ctx.reply_error(f"Specify a PoW network: {nets}")
            return

        entry = self._POW_NETWORK_ALIASES.get(net_arg)
        if not entry:
            nets = ", ".join(f"`{k}`" for k in self._POW_NETWORK_ALIASES)
            await ctx.reply_error(f"Unknown network `{net_arg}`. Valid: {nets}")
            return
        net_name, net_sym = entry

        tag = grp.get("tag") or ""
        if not tag:
            await ctx.reply_error(
                "Set a group tag first with `.group set tag=XXXX`  -  "
                "the tag becomes your token symbol."
            )
            return

        sym, _ = make_group_token(grp["name"], tag)
        if not sym:
            await ctx.reply_error("Tag could not be turned into a valid token symbol.")
            return

        # Already bound?
        if grp.get("token_network"):
            await ctx.reply_error(
                f"Mining chain already bound to **{grp['token_network']}**. "
                "Contact an admin to change it."
            )
            return

        # The vault pool pairs the group token against the Moon-Network
        # WRAPPED version of the mining coin (MMTA / MSUN), not the raw coin
        # itself, so every group-token trade stays on Moon Network. Users
        # wrap native MTA / SUN into MMTA / MSUN via .moon wrap.
        from constants.moons import wrapped_coin as _wrapped_coin
        wrapped_sym = _wrapped_coin(net_sym)

        # Fetch current prices for LP seeding. The wrapped price is used
        # for pool ratio math; the raw net price is still retained for
        # the block-reward matching ratio.
        tok_price_row = await ctx.db.get_price(sym, ctx.guild_id)
        wrapped_price_row = await ctx.db.get_price(wrapped_sym, ctx.guild_id)
        tok_price = float(tok_price_row["price"]) if tok_price_row else 0.01
        wrapped_price = float(wrapped_price_row["price"]) if wrapped_price_row else 0.10

        # Unlock the guild token for trading. The token's ``network`` stays on
        # the bridged ``"Moon Network"`` pseudo-network (set at creation) so
        # cross-group swaps work; ``net_name`` here is the mining chain that
        # the vault pool pairs against, tracked on ``mining_groups``.
        await ctx.db.execute(
            "UPDATE guild_tokens SET vault_locked=FALSE, trading_enabled=TRUE WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, sym,
        )

        # Bind the network on the group
        await ctx.db.set_group_token_network(ctx.guild_id, grp["group_id"], sym, net_name)

        # Create the vault LP (locked, $10k per side -- see
        # Config.GROUP_VAULT_POOL_SEED_USD). The pool is TOKEN/MMTA or
        # TOKEN/MSUN so swapping routes through the wrapped coin.
        pool = await ctx.db.create_vault_pool(
            ctx.guild_id, sym, wrapped_sym, tok_price, wrapped_price,
        )
        # Keep local names meaningful for the downstream embed text.
        net_price = wrapped_price

        ratio = tok_price / max(net_price, 1e-12)
        b = (
            card("✅ Group Token Mining Chain Set", color=C_SUCCESS)
            .field("Token",        f"**{sym}** (trading enabled ✅)",   True)
            .field("Trades on",    "Moon Network (bridged)",            True)
            .field("Mining Chain", f"**{net_name}** ({net_sym})",       True)
            .field("Vault Pool",   f"`{sym}/{wrapped_sym}` created",    True)
            .field("Mint Rate",
                   f"5 {sym} per block mined\n"
                   f"LP adds: 5 {sym} + {ratio * 5:.4f} {wrapped_sym} per block\n"
                   f"Wrap native {net_sym} into {wrapped_sym} with "
                   f"`{ctx.prefix}moon wrap {net_sym.lower()} <amount>`", False)
            .field("Status",
                   "Token is **unlocked** - players can trade immediately.\n"
                   "Cross-group swaps (e.g. with other group tokens) work "
                   "because all group tokens share the bridged Moon Network.\n"
                   "Vault balance grows with every group block.", False)
            .build()
        )
        await ctx.reply(embed=b, mention_author=False)

    # ── $group mine ──────────────────────────────────────────────────────────

    _MINE_SWITCH_COOLDOWN = 43_200  # 12 hours in seconds

    @group.command(name="mine")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_mine(self, ctx: DiscoContext, chain: str = "") -> None:
        """Switch all group members' rigs to mine a PoW chain.
        Usage: .group mine <sun|mta>"""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp:
            await ctx.reply_error("Group not found.")
            return
        if grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can switch the group's mining chain.")
            return

        chain_arg = chain.strip().lower()
        if not chain_arg:
            nets = ", ".join(f"`{k}`" for k in self._POW_NETWORK_ALIASES if k in ("sun", "mta"))
            await ctx.reply_error(f"Specify a chain: {nets}")
            return

        entry = self._POW_NETWORK_ALIASES.get(chain_arg)
        if not entry:
            nets = ", ".join(f"`{k}`" for k in self._POW_NETWORK_ALIASES if k in ("sun", "mta"))
            await ctx.reply_error(f"Unknown chain `{chain_arg}`. Valid: {nets}")
            return
        chain_name, chain_symbol = entry

        # 12-hour cooldown using epoch float from DB
        switched_at = await ctx.db.get_group_mine_switched_at(ctx.guild_id, membership["group_id"])
        if switched_at is not None:
            elapsed = time.time() - float(switched_at)
            remaining = self._MINE_SWITCH_COOLDOWN - elapsed
            if remaining > 0:
                await ctx.reply_cooldown(remaining)
                return

        # Move all members' rigs to the target chain
        summary = await ctx.db.group_bulk_move_rigs_to_chain(
            ctx.guild_id, membership["group_id"], chain_symbol
        )
        if not summary:
            await ctx.reply_error("No group members have mining rigs assigned.")
            return

        # Record the switch timestamp
        await ctx.db.record_group_mine_switch(ctx.guild_id, membership["group_id"])

        group_name = grp.get("name", "")
        b = (
            card(f"Group Mine Switched to {chain_symbol}", color=C_AMBER)
            .description(
                f"All **{group_name}** members' rigs have been reassigned to **{chain_name}** mining."
            )
        )

        # Per-member rig counts (up to 10, then "+N more")
        items = sorted(summary.items(), key=lambda kv: kv[1], reverse=True)
        shown = items[:10]
        extra = len(items) - 10
        for uid, rig_count in shown:
            member = ctx.guild.get_member(uid)
            member_name = member.display_name if member else f"<{uid}>"
            b.field(member_name, f"{rig_count:,} rig{'s' if rig_count != 1 else ''}", True)
        if extra > 0:
            b.field("And more", f"+{extra} more members", False)

        # Token network mismatch warning
        token_network = grp.get("token_network") or ""
        if token_network:
            net_coin = Config.NETWORK_COINS.get(token_network, "")
            if net_coin and net_coin.upper() != chain_symbol.upper():
                # Find which chain alias matches the token network
                matching_chain = next(
                    (alias for alias, (_, sym) in self._POW_NETWORK_ALIASES.items()
                     if sym.upper() == net_coin.upper() and alias in ("sun", "mta")),
                    net_coin.lower(),
                )
                b.field(
                    "Token Network Mismatch",
                    f"Your group token is on **{token_network}** - you won't earn group tokens "
                    f"while mining **{chain_symbol}**. Switch to **{matching_chain}** to earn them.",
                    False,
                )

        b.footer("Can be switched again in 12 hours")
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── $group rename ─────────────────────────────────────────────────────────

    _RENAME_COST    = 1_000.0   # USD charged per rename
    _RENAME_COOLDOWN = 86_400   # 24 hours in seconds

    @group.command(name="rename")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_rename(self, ctx: DiscoContext, *, new_name: str) -> None:
        """Rename your mining group. Costs $1,000 and has a 24-hour cooldown. (Founder only)
        Usage: .group rename <new name>"""
        import time as _time

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can rename the group.")
            return

        new_name = new_name.strip()
        ok, err = validate_group_name(new_name)
        if not ok:
            await ctx.reply_error(err)
            return
        if new_name == grp["name"]:
            await ctx.reply_error("That's already your group's name.")
            return

        # 24-hour cooldown check
        renamed_at = grp.get("renamed_at")
        if renamed_at:
            elapsed = _time.time() - renamed_at
            remaining = self._RENAME_COOLDOWN - elapsed
            if remaining > 0:
                h, m = divmod(int(remaining), 3600)
                m //= 60
                await ctx.reply_error(
                    f"You can rename again in **{h}h {m}m**. "
                    f"Groups can only be renamed once per 24 hours."
                )
                return

        # Name collision check
        collision = await ctx.db.get_mining_group(ctx.guild_id, name=new_name)
        if collision:
            await ctx.reply_error(f"A group named **{new_name}** already exists.")
            return

        # Balance check
        row = ctx.user_row
        if row.h("wallet") < self._RENAME_COST:
            await ctx.reply_error(
                f"Renaming costs **${self._RENAME_COST:,.0f}**. "
                f"You only have **${row.h('wallet'):,.2f}** in your wallet."
            )
            return

        old_name = grp["name"]
        await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -_tr(self._RENAME_COST))
        await ctx.db.update_mining_group_fields(
            ctx.guild_id, grp["group_id"],
            name=new_name,
            renamed_at=_time.time(),
        )
        await ctx.reply_success(
            f"**{old_name}** → **{new_name}**\n"
            f"**${self._RENAME_COST:,.0f}** deducted from your wallet. "
            f"Next rename available in 24 hours.",
            title="✅ Group Renamed",
        )

    # ── $group privacy ────────────────────────────────────────────────────────

    @group.command(name="privacy")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_privacy(self, ctx: DiscoContext, setting: str) -> None:
        """Set group privacy. Usage: .group privacy <public|private> (founder only)"""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can change privacy settings.")
            return

        _PRIVACY_OPTS = ("public", "private")
        setting = setting.lower()
        if setting not in _PRIVACY_OPTS:
            await ctx.reply_error(f"Choose {' or '.join(f'`{o}`' for o in _PRIVACY_OPTS)}.")
            return

        is_public = setting == "public"
        await ctx.db.update_mining_group_fields(ctx.guild_id, grp["group_id"], is_public=is_public)
        label = "Public" if is_public else "🔒 Invite-Only"
        await ctx.reply_success(f"**{grp['name']}** is now **{label}**.", title="✅ Privacy Updated")

    # ── $group weightmode ─────────────────────────────────────────────────────

    @group.command(name="weightmode")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_weightmode(self, ctx: DiscoContext, mode: str) -> None:
        """Set reward mode: hashrate | equal | custom (founder only)."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the founder can change the weight mode.")
            return

        mode = mode.lower()
        if mode not in _WEIGHT_MODES:
            modes = " ".join(f"`{m}`" for m in sorted(_WEIGHT_MODES))
            await ctx.reply_error(f"Invalid mode. Choose: {modes}")
            return

        await ctx.db.update_mining_group(ctx.guild_id, grp["group_id"], weight_mode=mode)
        descriptions = {
            "hashrate": "Rewards are split based on each miner's rig power  -  stronger rigs earn a bigger share.",
            "equal":    "Rewards are split equally  -  every member gets the same cut regardless of rigs.",
            "custom":   "Rewards are split by the weights you assign. Use `.group setweight @user 2` to give someone a 2× share.",
        }
        await ctx.reply_success(
            f"Mode set to **{mode}**\n\n{descriptions[mode]}",
            title="✅ Weight Mode Updated",
        )

    # ── $group setweight ──────────────────────────────────────────────────────

    @group.command(name="setweight")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_setweight(self, ctx: DiscoContext, member: discord.Member, weight: float) -> None:
        """Set custom mining weight for a member (founder only, requires custom mode)."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the founder can set member weights.")
            return
        if weight <= 0:
            await ctx.reply_error("Weight must be a positive number.")
            return
        target_membership = await ctx.db.get_user_mining_group(member.id, ctx.guild_id)
        if not target_membership or target_membership["group_id"] != grp["group_id"]:
            await ctx.reply_error(f"{member.display_name} is not in your group.")
            return

        await ctx.db.set_group_member_weight(ctx.guild_id, grp["group_id"], member.id, weight)
        current_mode = grp.get("weight_mode", "hashrate")
        extra = (
            f"\n\n⚠️ Your group is on **{current_mode}** mode  -  these weights won't apply yet.\n"
            f"Run `.group weightmode custom` to activate them."
            if current_mode != "custom" else ""
        )
        await ctx.reply_success(
            f"{member.mention} will receive a **{weight}×** share of group rewards.{extra}",
            title="✅ Weight Set",
        )

    # ── $group join ───────────────────────────────────────────────────────────

    @group.command(name="join")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_join(self, ctx: DiscoContext, *, name: str) -> None:
        """Join a mining group. Usage: .group join <name>"""
        existing = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if existing:
            g = await ctx.db.get_mining_group(ctx.guild_id, group_id=existing["group_id"])
            g_name = g["name"] if g else existing["group_id"]
            await ctx.reply_error(f"You're already in **{g_name}**. Use `.group leave` first.")
            return

        grp = await ctx.db.get_mining_group(ctx.guild_id, name=name)
        if not grp:
            await ctx.reply_error(f"No group named **{name}** found. Use `.group list` to see all groups.")
            return

        # Check privacy
        if not grp.get("is_public", 1):
            # Check for a pending invite
            invite = await ctx.db.get_group_invite(ctx.guild_id, grp["group_id"], ctx.author.id)
            if not invite:
                await ctx.reply_error(
                    f"**{grp['name']}** is invite-only. Request an invite from the founder."
                )
                return
            # Consume the invite on join
            await ctx.db.delete_group_invite(ctx.guild_id, grp["group_id"], ctx.author.id)

        await ctx.db.join_mining_group(ctx.author.id, ctx.guild_id, grp["group_id"])
        await self._hall_add_member(ctx.guild, grp, ctx.author)
        tag_str = f" `[{sanitize_text(grp['tag'])}]`" if grp.get("tag") else ""
        hall_note = " Head to the group's Hall thread to get started." if grp.get("hall_thread_id") else ""
        await ctx.reply_success(
            f"You joined **{sanitize_display(grp['name'])}**{tag_str}.{hall_note}",
            title="⛏️ Joined Mining Group",
        )

    # ── $group invite ─────────────────────────────────────────────────────────

    @group.command(name="invite")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_invite(self, ctx: DiscoContext, member: discord.Member) -> None:
        """Invite a user to your group (founder only). Usage: .group invite @user"""
        if member.id == ctx.author.id:
            await ctx.reply_error("You can't invite yourself.")
            return
        if member.bot:
            await ctx.reply_error("Bots can't join mining groups.")
            return

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can send invites.")
            return

        # Check if user is already in a group
        target_membership = await ctx.db.get_user_mining_group(member.id, ctx.guild_id)
        if target_membership:
            await ctx.reply_error(f"{member.display_name} is already in a mining group.")
            return

        # Check for duplicate invite
        existing_invite = await ctx.db.get_group_invite(ctx.guild_id, grp["group_id"], member.id)
        if existing_invite:
            await ctx.reply_error(f"An invite for {member.display_name} is already pending.")
            return

        await ctx.db.create_group_invite(ctx.guild_id, grp["group_id"], member.id, ctx.author.id)

        # Send DM to the invited user with Accept / Decline buttons
        p = ctx.prefix or Config.PREFIX
        invite_embed = card(
            "⛏️ Mining Group Invite",
            description=(
                f"**{ctx.author.display_name}** has invited you to join "
                f"**{grp['name']}** on **{ctx.guild.name}**.\n\n"
                f"Use the buttons below, or run `{p}group accept {grp['group_id']}` in the server."
            ),
            color=C_AMBER,
        ).build()
        invite_view = GroupInviteView(ctx.guild_id, grp["group_id"], self.bot)
        try:
            await member.send(embed=invite_embed, view=invite_view)
            dm_note = f"An invite DM was sent to {member.mention}."
        except discord.Forbidden:
            dm_note = f"{member.mention} has DMs disabled  -  tell them to run `{p}group accept {grp['group_id']}`."

        await ctx.reply_success(
            f"Invited {member.mention} to **{grp['name']}**.\n{dm_note}",
            title="Invite Sent",
        )

    # ── $group accept ─────────────────────────────────────────────────────────

    @group.command(name="accept")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_accept(self, ctx: DiscoContext, *, group_id: str = "") -> None:
        """Accept a pending group invite. Usage: .group accept [group name or id]"""
        existing = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if existing:
            g = await ctx.db.get_mining_group(ctx.guild_id, group_id=existing["group_id"])
            g_name = g["name"] if g else existing["group_id"]
            await ctx.reply_error(f"You're already in **{g_name}**. Leave it first.")
            return

        # Fetch all pending invites for this user
        pending = await ctx.db.get_pending_invites_for_user(ctx.author.id, ctx.guild_id)

        if not pending:
            await ctx.reply_error("You have no pending group invites.")
            return

        # If no argument given and only one invite, auto-accept it
        query = group_id.strip().strip('"').strip("'")
        if not query:
            if len(pending) == 1:
                invite = pending[0]
            else:
                names = ", ".join(f"**{p['group_name']}**" for p in pending[:10])
                await ctx.reply_error(
                    f"You have {len(pending)} pending invites ({names}). "
                    f"Specify which one: `.group accept <name>`"
                )
                return
        else:
            # Try matching by group_id first, then by name (case-insensitive)
            invite = None
            for p in pending:
                if p["group_id"] == query:
                    invite = p
                    break
            if not invite:
                q_lower = query.lower()
                for p in pending:
                    if p["group_name"].lower() == q_lower:
                        invite = p
                        break
            if not invite:
                names = ", ".join(f"**{p['group_name']}**" for p in pending[:10])
                await ctx.reply_error(
                    f"No pending invite matching **{query}**. "
                    f"Your pending invites: {names}"
                )
                return

        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=invite["group_id"])
        if not grp:
            await ctx.reply_error("That group no longer exists.")
            await ctx.db.delete_group_invite(ctx.guild_id, invite["group_id"], ctx.author.id)
            return

        await ctx.db.delete_group_invite(ctx.guild_id, invite["group_id"], ctx.author.id)
        await ctx.db.join_mining_group(ctx.author.id, ctx.guild_id, invite["group_id"])
        await self._hall_add_member(ctx.guild, grp, ctx.author)
        hall_note = " Head to the group's Hall thread to get started." if grp.get("hall_thread_id") else ""
        await ctx.reply_success(f"You joined **{grp['name']}**.{hall_note}", title="⛏️ Joined Mining Group")

    # ── $group decline ────────────────────────────────────────────────────────

    @group.command(name="decline")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_decline(self, ctx: DiscoContext, *, group_id: str = "") -> None:
        """Decline a pending group invite. Usage: .group decline [group name or id]"""
        pending = await ctx.db.get_pending_invites_for_user(ctx.author.id, ctx.guild_id)
        if not pending:
            await ctx.reply_error("You have no pending group invites.")
            return

        query = group_id.strip().strip('"').strip("'")
        if not query:
            if len(pending) == 1:
                invite = pending[0]
            else:
                names = ", ".join(f"**{p['group_name']}**" for p in pending[:10])
                await ctx.reply_error(
                    f"You have {len(pending)} pending invites ({names}). "
                    f"Specify which one: `.group decline <name>`"
                )
                return
        else:
            invite = None
            for p in pending:
                if p["group_id"] == query:
                    invite = p
                    break
            if not invite:
                q_lower = query.lower()
                for p in pending:
                    if p["group_name"].lower() == q_lower:
                        invite = p
                        break
            if not invite:
                names = ", ".join(f"**{p['group_name']}**" for p in pending[:10])
                await ctx.reply_error(
                    f"No pending invite matching **{query}**. "
                    f"Your pending invites: {names}"
                )
                return

        grp_name = invite.get("group_name") or invite["group_id"]
        await ctx.db.delete_group_invite(ctx.guild_id, invite["group_id"], ctx.author.id)
        await ctx.reply_success(f"Declined invite to **{grp_name}**.", title="Invite Declined")

    # ── $group leave ──────────────────────────────────────────────────────────

    @group.command(name="leave")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_leave(self, ctx: DiscoContext) -> None:
        """Leave your current mining group."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return

        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if grp and grp["founder_id"] == ctx.author.id:
            await ctx.reply_error(
                "You're the founder. Use `.group disband` to dissolve the group."
            )
            return

        await ctx.db.leave_mining_group(ctx.author.id, ctx.guild_id)
        if grp:
            await self._hall_remove_member(ctx.guild, grp, ctx.author)
        grp_name = grp["name"] if grp else membership["group_id"]
        await ctx.reply_success(f"You left **{grp_name}**.", title="Left Group")

    # ── $group lp ─────────────────────────────────────────────────────────────
    # Founder-only treasury <-> LP plumbing for the group's TOKEN/<pair>
    # pool, where <pair> is the Moon-Network wrapped version of the
    # founder's mining coin (mMTA for Moneta Chain, mSUN for Sun
    # Network). Backed by services/group_lp.py which enforces the
    # safeguards (founder check, 25% pct cap, 24h cooldown,
    # audit-logged via tx_log).

    @group.group(name="lp", invoke_without_command=True)
    @guild_only
    async def group_lp(self, ctx: DiscoContext) -> None:
        """Group LP: status / deposit / withdraw / topup / harvest."""
        if await suggest_subcommand(ctx, self.group_lp):
            return
        prefix = ctx.prefix or "."
        body = (
            f"`{prefix}group lp status`  -  treasury + pool snapshot\n"
            f"`{prefix}group lp deposit <pct>`  -  push pct% of vault into LP\n"
            f"`{prefix}group lp withdraw <pct>`  -  pull pct% of pool reserve out\n"
            f"`{prefix}group lp topup <A> <B> <USD>`  -  reserve_usd -> LP, no slippage\n"
            f"`{prefix}group lp harvest <A> <B>`  -  claim swap-fee earnings to reserve\n"
            f"\n"
            f"Safeguards on deposit/withdraw: founder-only, max "
            f"**25% per action**, **24h** cooldown. Single-sided ops -- "
            f"only the group-token side moves, so price will shift."
        )
        await ctx.reply(
            embed=card(
                "\U0001F3DB Group LP Treasury",
                description=body,
                color=C_INFO,
            ).build(),
            mention_author=False,
        )

    async def _resolve_founder_group(
        self, ctx: DiscoContext,
    ) -> dict | None:
        """Find the group the caller founded, in this guild. Returns
        None + replies an error when the caller isn't a founder.
        """
        row = await ctx.db.fetch_one(
            "SELECT * FROM mining_groups "
            " WHERE guild_id = $1 AND founder_id = $2",
            ctx.guild_id, ctx.author.id,
        )
        if not row:
            await ctx.reply_error(
                "You aren't the founder of any group in this server. "
                "Only founders can run `,group lp` ops."
            )
            return None
        return dict(row)

    @group_lp.command(name="status")
    @guild_only
    async def group_lp_status(self, ctx: DiscoContext) -> None:
        """Read-only status panel for the founder's group LP."""
        from services import group_lp as _glp
        grp = await self._resolve_founder_group(ctx)
        if not grp:
            return
        s = await _glp.status(
            ctx.db, guild_id=ctx.guild_id,
            group_id=str(grp["group_id"]),
        )
        sym = s.get("symbol") or "?"
        pair = s.get("pair_symbol") or "?"
        unlocked = "✅ unlocked" if s.get("unlocked") else "\U0001F512 locked"
        last = s.get("last_at")
        last_part = f"  ·  last action: {fmt_ts(last)}" if last else ""
        builder = card(
            f"\U0001F3DB Group LP  ·  {grp.get('name') or grp['group_id']}",
            color=C_INFO,
        )
        builder = builder.field(
            "Treasury",
            (
                f"`{_h(s['vault_token_bal_raw']):,.4f} {sym}` in vault\n"
                f"Lifetime deposited: "
                f"`{_h(s['lifetime_total_raw']):,.4f} {sym}`"
            ),
            False,
        )
        builder = builder.field(
            f"Pool {sym}/{pair}",
            (
                f"`{_h(s['pool_token_raw']):,.4f} {sym}`  ·  "
                f"`{_h(s['pool_pair_raw']):,.6f} {pair}`"
            ),
            False,
        )
        builder = builder.field(
            "Status",
            f"{unlocked}{last_part}",
            False,
        )
        builder = builder.field(
            "Safeguards",
            (
                f"Max **{s['max_pct']:.0f}%** per action  ·  "
                f"**{s['cooldown_h']}h** cooldown  ·  founder-only"
            ),
            False,
        )
        prefix = ctx.prefix or "."
        builder = builder.footer(
            f"`{prefix}group lp deposit <pct>` to add to LP  ·  "
            f"`{prefix}group lp withdraw <pct>` to pull"
        )
        await ctx.reply(embed=builder.build(), mention_author=False)

    # ,group lp enable / disable used to gate deposits behind a
    # master unlock flag (treasury_lp_unlocked). The flag is gone --
    # the 25% / 24h caps already prevent runaway deposits, and the
    # extra "founder must opt in once" step was just friction every
    # founder hit on day-1. Old commands removed; the column stays
    # on mining_groups for backwards compat with the status RPC and
    # is reported as informational only.

    @group_lp.command(name="deposit", aliases=["add"])
    @guild_only
    async def group_lp_deposit(
        self, ctx: DiscoContext, pct: float = 0.0,
    ) -> None:
        """Move a % of the vault group-token into the group's LP pool.

        Example: ``,group lp deposit 10`` moves 10% of vault_token_bal
        into the {GROUP_TOKEN}/{wrapped-coin} pool's group-token reserve
        (mMTA for Moneta Chain groups, mSUN for Sun Network). Caps at
        25% per action and 24h cooldown.
        """
        from services import group_lp as _glp
        if pct <= 0:
            await ctx.reply_error(
                "Pass a percent: `,group lp deposit 10` for 10%."
            )
            return
        grp = await self._resolve_founder_group(ctx)
        if not grp:
            return
        # Resolve the pair up-front so the confirm dialog can name it. If
        # the group hasn't bound a mining chain yet, surface that error
        # immediately instead of after the confirm round-trip.
        try:
            pair = _glp.resolve_pair(grp)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        sym = grp.get("token_symbol") or "?"
        # Confirm dialog -- the founder gets one chance to abort.
        confirm_embed = card(
            f"\U0001F3DB Deposit {pct:.1f}% to LP?",
            description=(
                f"Push **{pct:.1f}%** of the **{sym}** "
                f"vault into the {sym}/{pair} pool. "
                f"Single-sided -- price will move.\n\n"
                f"Cooldown: 24h after this action."
            ),
            color=C_AMBER,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(
                description="Deposit cancelled.", color=C_ERROR,
            ).build())
            return

        try:
            res = await _glp.deposit(
                ctx.db, guild_id=ctx.guild_id,
                group_id=str(grp["group_id"]),
                user_id=ctx.author.id, pct=float(pct),
            )
        except ValueError as e:
            await msg.edit(embed=card(
                description=str(e), color=C_ERROR,
            ).build())
            return
        sym = res["symbol"]
        pair = res["pair_symbol"]
        receipt = card(
            f"✅ Deposited {res['token_added_h']:,.4f} {sym}",
            description=(
                f"Pool {sym}/{pair} pre-price: "
                f"`{res['price_before']:,.6f} {pair}/{sym}`  "
                f"·  post: `{res['price_after']:,.6f} {pair}/{sym}`\n"
                f"Vault remaining: "
                f"`{_h(res['vault_remaining_raw']):,.4f} {sym}`\n"
                f"Pool {sym} reserve now: "
                f"`{_h(res['new_reserve_a_raw']):,.4f} {sym}`"
            ),
            color=C_SUCCESS,
        ).build()
        await msg.edit(embed=receipt)

    @group_lp.command(name="withdraw", aliases=["remove", "pull"])
    @guild_only
    async def group_lp_withdraw(
        self, ctx: DiscoContext, pct: float = 0.0,
    ) -> None:
        """Pull a % of the pool's group-token reserve back into the vault.

        Example: ``,group lp withdraw 10`` pulls 10% of the
        {GROUP_TOKEN}/{wrapped-coin} pool's group-token reserve into the
        vault (mMTA for Moneta Chain groups, mSUN for Sun Network).
        """
        from services import group_lp as _glp
        if pct <= 0:
            await ctx.reply_error(
                "Pass a percent: `,group lp withdraw 10` for 10%."
            )
            return
        grp = await self._resolve_founder_group(ctx)
        if not grp:
            return
        try:
            pair = _glp.resolve_pair(grp)
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        sym = grp.get("token_symbol") or "?"
        confirm_embed = card(
            f"\U0001F3DB Withdraw {pct:.1f}% from LP?",
            description=(
                f"Pull **{pct:.1f}%** of the {sym} "
                f"reserve from the {sym}/{pair} pool "
                f"back into the vault. Single-sided -- price will move.\n\n"
                f"Cooldown: 24h after this action."
            ),
            color=C_AMBER,
        ).build()
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(
                description="Withdraw cancelled.", color=C_ERROR,
            ).build())
            return

        try:
            res = await _glp.withdraw(
                ctx.db, guild_id=ctx.guild_id,
                group_id=str(grp["group_id"]),
                user_id=ctx.author.id, pct=float(pct),
            )
        except ValueError as e:
            await msg.edit(embed=card(
                description=str(e), color=C_ERROR,
            ).build())
            return
        sym = res["symbol"]
        pair = res["pair_symbol"]
        receipt = card(
            f"✅ Withdrew {res['token_pulled_h']:,.4f} {sym}",
            description=(
                f"Pool {sym}/{pair} pre-price: "
                f"`{res['price_before']:,.6f} {pair}/{sym}`  "
                f"·  post: `{res['price_after']:,.6f} {pair}/{sym}`\n"
                f"Pool {sym} reserve now: "
                f"`{_h(res['new_reserve_a_raw']):,.4f} {sym}`"
            ),
            color=C_SUCCESS,
        ).build()
        await msg.edit(embed=receipt)

    # ── $group lp topup / harvest ────────────────────────────────────────────
    # Thin wrappers so every LP-related action lives under ``,group lp`` --
    # the founder doesn't have to remember which actions live under
    # ``,group pool`` and which live under ``,group lp``. Both reuse the
    # existing ``,group pool ...`` implementations to keep one source of
    # truth for the cost-basis math + reserve_usd accounting.

    @group_lp.command(name="topup", aliases=["addlp", "fund"])
    @guild_only
    @no_bots
    @ensure_registered
    async def group_lp_topup(
        self, ctx: DiscoContext, *, args: str = "",
    ) -> None:
        """Top up a group LP pool from your group's reserve_usd.

        Usage: ``,group lp topup <TOKEN_A> <TOKEN_B> <USD>``

        Pulls ``USD`` from the group's reserve, splits 50/50 across both
        sides of the ``TOKEN_A/TOKEN_B`` pool at oracle prices, mints LP
        at the current ratio, and bumps cost basis. Same effect as
        ``,group pool deposit`` (which still works) -- this alias just
        keeps every LP verb under one group.
        """
        await self._group_pool_deposit(ctx, args)

    @group_lp.command(name="harvest", aliases=["claim"])
    @guild_only
    @no_bots
    @ensure_registered
    async def group_lp_harvest(
        self, ctx: DiscoContext, *, args: str = "",
    ) -> None:
        """Claim accumulated swap-fee earnings from a group LP pool.

        Usage: ``,group lp harvest <TOKEN_A> <TOKEN_B>``

        Computes the fee delta over the position's cost basis and pays
        it to ``reserve_usd``. 24-hour cooldown per pool. Mirror of
        ``,group pool harvest`` (which still works) -- this alias just
        keeps the LP surface consolidated.
        """
        await self._group_pool_harvest(ctx, args)

    # ── $group hall ───────────────────────────────────────────────────────────

    @group.group(name="hall", invoke_without_command=True)
    @guild_only
    async def group_hall(self, ctx: DiscoContext) -> None:
        """Group Hall commands: open, close, info."""
        if await suggest_subcommand(ctx, self.group_hall):
            return
        await ctx.send_help(ctx.command)

    @group_hall.command(name="open")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_hall_open(self, ctx: DiscoContext) -> None:
        """Open the Group Hall - creates a private thread for all group members (founder only)."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can open the Hall.")
            return

        if grp.get("hall_thread_id"):
            # Check if thread still exists
            existing = await self._get_hall_thread(ctx.guild, grp["hall_thread_id"])
            if existing and not existing.archived:
                await ctx.reply_error(
                    f"Your Hall is already open: {existing.mention}\n"
                    f"Use `.group hall close` to archive it first."
                )
                return

        # Resolve parent channel from guild settings
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        parent_ch_id = settings.get("grouphall_channel")
        if not parent_ch_id:
            await ctx.reply_error(
                "No Group Hall parent channel is set.\n"
                "An admin must run `.admin setchannel grouphall #channel` first."
            )
            return
        parent_ch = ctx.guild.get_channel(parent_ch_id)
        if not isinstance(parent_ch, discord.TextChannel):
            await ctx.reply_error("The Group Hall parent channel is invalid or missing.")
            return

        safe_name = sanitize_display(grp["name"])
        thread_name = f"{safe_name}'s Hall"[:100]
        thread = await parent_ch.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            reason=f"Group Hall opened by {ctx.author} ({ctx.author.id})",
        )
        await thread.add_user(ctx.author)

        # Add all current members
        members = await ctx.db.get_group_members(ctx.guild_id, grp["group_id"])
        for m in members:
            if m["user_id"] == ctx.author.id:
                continue
            discord_member = ctx.guild.get_member(m["user_id"])
            if discord_member:
                try:
                    await thread.add_user(discord_member)
                except Exception:
                    pass

        # Persist thread IDs in DB
        await ctx.db.execute(
            "UPDATE mining_groups SET hall_thread_id=$1, hall_channel_id=$2, hall_opened_at=NOW() "
            "WHERE guild_id=$3 AND group_id=$4",
            thread.id, parent_ch.id, ctx.guild_id, grp["group_id"],
        )
        # Invalidate cache for old thread ID (if any)
        old_tid = grp.get("hall_thread_id")
        if old_tid:
            await self._cache_del(f"discoin:hall:thread:{old_tid}")

        upgrades = await ctx.db.get_group_upgrades(ctx.guild_id, grp["group_id"])
        purchased_ids = [u["upgrade_id"] for u in upgrades]
        hall_cfg = Config.GROUP_HALL_UPGRADES
        unlocked: list[str] = []
        for uid in purchased_ids:
            eff = hall_cfg.get(uid, {}).get("effect", {})
            if "hall_unlock" in eff:
                unlocked.append(eff["hall_unlock"])

        welcome_lines = [
            f"Welcome to **{safe_name}'s Hall**, {ctx.author.mention}!",
            "",
            "This is your group's private space. Commands are gated by default.",
        ]
        if unlocked:
            welcome_lines.append(f"Unlocked categories here: **{', '.join(unlocked)}**")
        else:
            welcome_lines.append(
                "Purchase Hall upgrades (`.group upgrade buy <id>`) to unlock commands and earn bonuses here."
            )
        welcome_lines += [
            "",
            "- `.group hall info` - view Hall status and upgrades",
            "- `.group upgrade list` - browse available Hall upgrades",
            "- `.group upgrade buy <id>` - purchase an upgrade from the reserve",
        ]
        welcome_embed = (
            card(f"🏛️ {safe_name}'s Hall", description="\n".join(welcome_lines), color=C_PURPLE)
            .footer(f"Founder: {ctx.author.display_name}  |  Members: {len(members)}")
            .build()
        )
        await thread.send(embed=welcome_embed)
        await ctx.reply_success(
            f"Hall opened: {thread.mention}\nAll {len(members)} member(s) have been added.",
            title="🏛️ Hall Opened",
        )

    @group_hall.command(name="close")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_hall_close(self, ctx: DiscoContext) -> None:
        """Archive the group's Hall thread (founder only)."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can close the Hall.")
            return
        if not grp.get("hall_thread_id"):
            await ctx.reply_error("Your group doesn't have an open Hall. Use `.group hall open` first.")
            return

        thread = await self._get_hall_thread(ctx.guild, grp["hall_thread_id"])
        if thread and not thread.archived:
            try:
                await thread.edit(archived=True, locked=False, reason=f"Hall closed by {ctx.author}")
            except Exception:
                pass

        await self._cache_del(f"discoin:hall:thread:{grp['hall_thread_id']}")
        await ctx.db.execute(
            "UPDATE mining_groups SET hall_thread_id=NULL, hall_channel_id=NULL "
            "WHERE guild_id=$1 AND group_id=$2",
            ctx.guild_id, grp["group_id"],
        )
        await ctx.reply_success("Hall archived. Use `.group hall open` to reopen it.", title="Hall Closed")

    @group_hall.command(name="prefix", aliases=["prefixless", "bare", "noprefix"])
    @guild_only
    @no_bots
    @ensure_registered
    async def group_hall_prefix(
        self, ctx: DiscoContext, mode: str = "",
    ) -> None:
        """Toggle whether the Hall thread requires the bot prefix (founder only).

        ``,group hall prefix on``      require the bot prefix (default)
        ``,group hall prefix off``     accept bare commands in the Hall
        ``,group hall prefix toggle``  flip whichever is active
        ``,group hall prefix``         show the current state

        When OFF, members can type ``work`` / ``daily`` / ``fish`` / etc.
        directly in the Hall thread, the same way the admin-set bot
        channels accept bare commands. The prefixed form (``,work``) keeps
        working in both modes. Default is ON.
        """
        membership = await ctx.db.get_user_mining_group(
            ctx.author.id, ctx.guild_id,
        )
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(
            ctx.guild_id, group_id=membership["group_id"],
        )
        if not grp:
            await ctx.reply_error("Group not found.")
            return
        # User-facing: prefix ON = prefix REQUIRED. Storage column is
        # hall_prefixless (TRUE => no prefix needed), so prefix_on is
        # the inverse and we flip on read/write.
        prefix_on = not bool(grp.get("hall_prefixless"))
        m = (mode or "").strip().lower()
        if not m:
            state = "ON" if prefix_on else "OFF"
            scope = " in your Hall thread" if grp.get("hall_thread_id") else ""
            body = (
                f"Bot prefix{scope} is currently **{state}**.\n"
                f"`,group hall prefix on` / `off` / `toggle` (founder only)."
            )
            await ctx.reply_success(
                body, title=f"🏛️ Hall Prefix - {state}",
            )
            return
        if grp["founder_id"] != ctx.author.id:
            await ctx.reply_error(
                "Only the group founder can change the Hall prefix policy."
            )
            return
        if m in ("on", "enable", "enabled", "true", "1", "yes", "required"):
            new_prefix_on = True
        elif m in ("off", "disable", "disabled", "false", "0", "no", "bare"):
            new_prefix_on = False
        elif m in ("toggle", "flip"):
            new_prefix_on = not prefix_on
        else:
            await ctx.reply_error(
                "Usage: `,group hall prefix <on|off|toggle>`."
            )
            return
        if new_prefix_on == prefix_on:
            state = "ON" if prefix_on else "OFF"
            await ctx.reply_success(
                f"Hall prefix was already **{state}** -- no change.",
                title="🏛️ Hall Prefix",
            )
            return
        # Storage column is hall_prefixless => invert the user-facing flag
        # before writing so ON/OFF in the command matches "prefix required".
        await ctx.db.execute(
            "UPDATE mining_groups SET hall_prefixless = $1 "
            " WHERE guild_id = $2 AND group_id = $3",
            bool(not new_prefix_on), ctx.guild_id, grp["group_id"],
        )
        state = "ON" if new_prefix_on else "OFF"
        body = (
            "Members must use the bot prefix "
            "(e.g. `,work`, `,fish`, `,daily`) inside the Hall thread."
            if new_prefix_on else
            "Members can now type bare commands "
            "(e.g. `work`, `fish`, `daily`) inside the Hall thread. "
            "The prefixed form still works."
        )
        await ctx.reply_success(body, title=f"🏛️ Hall Prefix - {state}")

    @group_hall.command(name="info")
    @guild_only
    async def group_hall_info(self, ctx: DiscoContext) -> None:
        """Show Hall status, active bonuses, and available upgrades."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp:
            await ctx.reply_error("Group not found.")
            return

        upgrades = await ctx.db.get_group_upgrades(ctx.guild_id, grp["group_id"])
        purchased_ids = [u["upgrade_id"] for u in upgrades]
        hall_cfg = Config.GROUP_HALL_UPGRADES

        thread_mention = "None"
        if grp.get("hall_thread_id"):
            thread = await self._get_hall_thread(ctx.guild, grp["hall_thread_id"])
            if thread and not thread.archived:
                thread_mention = thread.mention
            else:
                thread_mention = "Archived"

        bonus_str = _format_upgrade_bonuses(purchased_ids) or "None"
        upgrade_str = _format_upgrades(purchased_ids) or "None"

        # Spendable reserve = USD bucket + MTA bucket + token vault, all
        # converted at live oracle prices. Routed through the shared
        # helper so this stays in sync with .group upgrade list / buy.
        reserve_total_h = await _spendable_reserve_total(ctx, grp)

        # Next affordable upgrade
        next_upgrades = []
        for uid, cfg in hall_cfg.items():
            if uid in purchased_ids:
                continue
            requires = cfg.get("requires", [])
            if any(r not in purchased_ids for r in requires):
                continue
            cost = _h(int(cfg.get("cost_usd", 0) or 0))
            if cost <= reserve_total_h:
                next_upgrades.append(f"`{uid}` - {cfg['name']} ({fmt_usd(cost)})")
        next_str = "\n".join(next_upgrades[:3]) if next_upgrades else "None affordable yet"

        _b = (
            card(f"🏛️ {sanitize_display(grp['name'])} - Hall Status", color=C_PURPLE)
            .field("Hall Thread",     thread_mention,  True)
            .field("Reserve",         fmt_usd(reserve_total_h), True)
            .field("Purchased Upgrades", upgrade_str,  False)
            .field("Active Bonuses",  bonus_str,       False)
            .field("Buyable Now",     next_str,        False)
            .footer("Open Hall: .group hall open  |  Buy upgrades: .group upgrade buy <id>")
        )
        await ctx.reply(embed=_b.build(), mention_author=False)

    # ── $group info ───────────────────────────────────────────────────────────

    @group.command(name="info")
    @guild_only
    async def group_info(self, ctx: DiscoContext, *, name: str = "") -> None:
        """Show group details. Usage: .group info [name] (defaults to your group)"""
        if name:
            grp = await ctx.db.get_mining_group(ctx.guild_id, name=name)
        else:
            membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
            grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"]) if membership else None

        if not grp:
            if name:
                await ctx.reply_error(f"No group named **{name}** found.")
            else:
                embed = card("", description="❌  You're not in a group. Specify a name or join one first.", color=C_ERROR).build()
                view = CreateGroupView(ctx)
                await ctx.reply(embed=embed, view=view, mention_author=False)
            return

        members = await ctx.db.get_group_members(ctx.guild_id, grp["group_id"])
        weights = {w["user_id"]: w["weight"] for w in await ctx.db.get_group_weights(ctx.guild_id, grp["group_id"])}
        mode    = grp.get("weight_mode", "hashrate")

        combined_hr   = 0.0
        member_lines  = []
        viewer_bonus  = 0.0
        for m in members:
            user_hr = await ctx.db.get_user_total_hashrate(m["user_id"], ctx.guild_id)
            combined_hr += user_hr
            member_obj  = ctx.guild.get_member(m["user_id"])
            disp_name   = member_obj.display_name if member_obj else f"User {m['user_id']}"
            tag_founder = " 👑" if m["user_id"] == grp["founder_id"] else ""
            w_str = ""
            if mode == "custom":
                w = weights.get(m["user_id"], 1.0)
                w_str = f"  [{w}x]"
            hr_str = f"{user_hr:,} MH/s{w_str}"
            # Show bonus indicator on the viewing user's own line
            if m["user_id"] == ctx.author.id:
                hashstone = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
                viewer_bonus = _item_stat(hashstone, "mining_bonus")
                hr_str = fmt_bonus(hr_str, viewer_bonus)
            member_lines.append(f"• **{disp_name}**{tag_founder}  {hr_str}")

        founder_obj = ctx.guild.get_member(grp["founder_id"])
        founder_str = founder_obj.display_name if founder_obj else f"User {grp['founder_id']}"
        tag_str     = f" `[{sanitize_text(grp['tag'])}]`" if grp.get("tag") else ""

        # Privacy
        is_public    = grp.get("is_public", 1)
        privacy_str  = "Public" if is_public else "🔒 Invite-Only"
        # Reserve
        reserve_usd  = grp.h("reserve_usd")
        reserve_btc  = grp.h("reserve_btc")
        reserve_pct  = grp.get("reserve_pct", 5.0)
        # Upgrades
        upgrades     = await ctx.db.get_group_upgrades(ctx.guild_id, grp["group_id"])
        upgrade_ids  = [u["upgrade_id"] for u in upgrades]

        safe_name = sanitize_display(grp["name"])
        safe_desc = sanitize_display(grp.get("description") or "") or None

        # Vault token info
        tok_sym    = grp.get("token_symbol") or ""
        tok_net    = grp.get("token_network") or ""
        vault_bal  = float(grp.get("vault_token_bal") or 0.0)
        tok_enabled = False
        contract_addr = ""
        if tok_sym:
            tok_row = await ctx.db.fetch_one(
                "SELECT trading_enabled, contract_address FROM guild_tokens "
                "WHERE guild_id=$1 AND symbol=$2",
                ctx.guild_id, tok_sym,
            )
            if tok_row:
                tok_enabled = bool(tok_row.get("trading_enabled"))
                contract_addr = tok_row.get("contract_address") or ""

        # Compute total reserve value in USD across USD bucket + MTA + group token vault
        btc_price_row = await ctx.db.get_price("MTA", ctx.guild_id)
        btc_price = float(btc_price_row["price"]) if btc_price_row else 0.0
        tok_price = 0.0
        if tok_sym:
            tok_price_row = await ctx.db.get_price(tok_sym, ctx.guild_id)
            tok_price = float(tok_price_row["price"]) if tok_price_row else 0.0
        total_reserve_value = reserve_usd + reserve_btc * btc_price + vault_bal * tok_price

        _b = (
            card(f"⛏️ {safe_name}{tag_str}", description=safe_desc, color=C_AMBER)
            .field("ID",              f"`{grp['group_id']}`",         True)
            .field("Founder",         founder_str,                     True)
            .field("Privacy",         privacy_str,                     True)
            .field("Members",         str(len(members)),               True)
            .field("Total Hashrate",  f"**{combined_hr:,.0f} MH/s**", True)
            .field("Reward Mode",     f"`{mode}`",                    True)
            .field("Reserve Value",   f"**{fmt_usd(total_reserve_value)}**",   True)
            .field("Reserve Rate",    f"**{reserve_pct:.1f}%** per mined block", True)
            .field("Reserve Detail",  f"`.group reserve` for breakdown",       True)
        )
        if tok_sym:
            status_icon = "✅" if tok_enabled else "🔒"
            _b.field(
                "Group Token",
                f"**{tok_sym}** on {tok_net or 'unbound'}\n"
                f"Vault: `{vault_bal:,.4f} {tok_sym}`  {status_icon} {'tradeable' if tok_enabled else 'locked'}",
                False,
            )
            if contract_addr:
                _b.field("Contract", f"`{contract_addr}`", False)
        # Hall thread status
        hall_str = "None"
        if grp.get("hall_thread_id"):
            hall_th = await self._get_hall_thread(ctx.guild, grp["hall_thread_id"])
            if hall_th and not hall_th.archived:
                hall_str = hall_th.mention
            else:
                hall_str = "Archived"
        _b.field("Hall Thread",    hall_str,                             True)
        _b.field("Hall Upgrades",  _format_upgrades(upgrade_ids),       True)
        _b.field("Hall Bonuses",   _format_upgrade_bonuses(upgrade_ids), False)
        _b.field("Member List",    "\n".join(member_lines[:15]) or " - ", False)

        if grp.get("image_url") and is_safe_url(grp["image_url"]):
            _b.thumbnail(grp["image_url"])
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── $group list ───────────────────────────────────────────────────────────

    @group.command(name="list", aliases=["ls"])
    @guild_only
    async def group_list(self, ctx: DiscoContext) -> None:
        """List all mining groups on this server (paginated)."""
        groups = await ctx.db.get_all_mining_groups(ctx.guild_id)
        if not groups:
            embed = card("", description="❌  No mining groups yet. Use `.group create <name>` to start one.", color=C_ERROR).build()
            view = CreateGroupView(ctx)
            await ctx.reply(embed=embed, view=view, mention_author=False)
            return

        pages: list[discord.Embed] = []
        per_page = 6
        for i in range(0, len(groups), per_page):
            chunk = groups[i:i + per_page]
            _b = card("⛏️ Mining Groups", color=C_AMBER)
            for grp in chunk:
                members    = await ctx.db.get_group_members(ctx.guild_id, grp["group_id"])
                combined_hr = 0.0
                for m in members:
                    combined_hr += await ctx.db.get_user_total_hashrate(m["user_id"], ctx.guild_id)
                founder_obj = ctx.guild.get_member(grp["founder_id"])
                founder_str = founder_obj.display_name if founder_obj else f"User {grp['founder_id']}"
                safe_name = sanitize_display(grp["name"])
                tag_str  = f" `[{sanitize_text(grp['tag'])}]`" if grp.get("tag") else ""
                lock_str = " 🔒" if not grp.get("is_public", 1) else ""
                mode     = grp.get("weight_mode", "hashrate")
                desc     = sanitize_display(grp.get("description", ""))
                desc_line = f"\n*{desc[:80]}*" if desc else ""
                _b.field(
                    f"{safe_name}{tag_str}{lock_str}",
                    (
                        f"Founder: **{founder_str}**  •  {len(members)} members  •  "
                        f"{combined_hr:,.0f} MH/s  •  `{mode}`{desc_line}"
                    ),
                    False,
                )
            pages.append(_b.build())
        await send_paginated(ctx, pages)

    # ── $group lb / leaderboard ───────────────────────────────────────────────
    # Ranks groups by what THEY OWN: their treasury reserves (USD + MTA + SUN
    # converted at oracle), their token vault balance, AND their pro-rata
    # share of every pool they've contributed LP to (group_lp_positions).
    # Three views: combined value (default), USD side only, own-token side
    # only -- so a group sitting on real USD reads differently from one
    # whose net worth is mostly its own token.

    @group.group(name="lb", aliases=["leaderboard", "leaderboards", "rankings", "top"],
                 invoke_without_command=True)
    @guild_only
    async def group_lb(self, ctx: DiscoContext) -> None:
        """Group leaderboards. Defaults to combined value (USD + token).

        Subcommands:
            ``,group lb`` (or ``value``) -- combined USD value of treasury + LP
            ``,group lb usd``            -- USD side only (reserves + LP USD leg)
            ``,group lb token``          -- group token side only (vault + LP token leg)
        """
        await self._render_group_value_lb(ctx, mode="combined")

    @group_lb.command(name="value", aliases=["worth", "total", "combined", "all"])
    @guild_only
    async def group_lb_value(self, ctx: DiscoContext) -> None:
        """Top groups by combined USD value of treasury + LP positions."""
        await self._render_group_value_lb(ctx, mode="combined")

    @group_lb.command(name="usd", aliases=[
        "dollars", "money", "cash",
        # Back-compat: the old ,group lb reserves / treasury / vault
        # measured USD-side reserves only, so route them here.
        "reserves", "reserve", "treasury", "vault", "$",
    ])
    @guild_only
    async def group_lb_usd(self, ctx: DiscoContext) -> None:
        """Top groups by USD-denominated holdings only (reserves + LP USD leg)."""
        await self._render_group_value_lb(ctx, mode="usd")

    @group_lb.command(name="token", aliases=["tok", "tokens", "supply", "holdings"])
    @guild_only
    async def group_lb_token(self, ctx: DiscoContext) -> None:
        """Top groups by their own token holdings (vault + LP token leg)."""
        await self._render_group_value_lb(ctx, mode="token")

    async def _render_group_value_lb(
        self, ctx: DiscoContext, *, mode: str,
    ) -> None:
        """Walk every group + group_lp_positions row once, compute per-group
        treasury / LP / token value, render the requested leaderboard view.

        ``mode``:
          - ``combined`` -> sort by total USD value (treasury + LP USD)
          - ``usd``      -> sort by USD-denominated holdings only
          - ``token``    -> sort by own-token holdings (vault + LP token leg)
        """
        groups = await ctx.db.get_all_mining_groups(ctx.guild_id)
        if not groups:
            await ctx.reply_error("No mining groups in this server yet.")
            return

        # ── price cache ──────────────────────────────────────────────
        price_cache: dict[str, float] = {}

        async def _price(sym: str) -> float:
            sym = (sym or "").upper()
            if not sym:
                return 0.0
            if sym in price_cache:
                return price_cache[sym]
            row = await ctx.db.get_price(sym, ctx.guild_id)
            p = float(row["price"]) if row and float(row.get("price") or 0) > 0 else 0.0
            if not p:
                cfg = Config.TOKENS.get(sym, {})
                p = float(cfg.get("start_price") or 0.0)
            price_cache[sym] = p
            return p

        # ── pull every group LP position joined to its pool, ONCE ────
        # The reserve / lp_shares columns are NUMERIC(36,0) raw scaled
        # by 10**18; cast to TEXT so asyncpg gives us strings we can
        # safely turn into Python ints without overflow.
        lp_rows = await ctx.db.fetch_all(
            """
            SELECT glp.group_id,
                   p.pool_id,
                   p.token_a,
                   p.token_b,
                   p.reserve_a::TEXT  AS ra,
                   p.reserve_b::TEXT  AS rb,
                   p.total_lp::TEXT   AS total_lp,
                   glp.lp_shares::TEXT AS shares
            FROM   group_lp_positions glp
            JOIN   pools p
                   ON p.pool_id = glp.pool_id
                   AND p.guild_id = glp.guild_id
            WHERE  glp.guild_id = $1
            """,
            ctx.guild_id,
        )

        _S = 10 ** 18

        def _maybe_human(raw_str: str | None) -> int:
            try:
                return int(float(raw_str or 0))
            except Exception:
                return 0

        # Bucket LP positions by group_id with pre-computed shares.
        lp_by_group: dict[str, list[dict]] = {}
        for r in lp_rows:
            shares_raw = _maybe_human(r.get("shares"))
            total_raw = _maybe_human(r.get("total_lp"))
            if shares_raw <= 0 or total_raw <= 0:
                continue
            lp_by_group.setdefault(r["group_id"], []).append({
                "token_a": (r["token_a"] or "").upper(),
                "token_b": (r["token_b"] or "").upper(),
                "ra_raw":  _maybe_human(r.get("ra")),
                "rb_raw":  _maybe_human(r.get("rb")),
                "total_raw": total_raw,
                "shares_raw": shares_raw,
            })

        btc_p = await _price("MTA")
        sun_p = await _price("SUN")

        # ── per-group rollup ─────────────────────────────────────────
        ranked: list[dict] = []
        for grp in groups:
            sym = (grp.get("token_symbol") or "").upper()
            tok_p = await _price(sym) if sym else 0.0

            # Treasury reserves. NUMERIC(36,0) columns are stored raw
            # (×10**18) when large; smaller hand-written values stay
            # human-scaled. Heuristic: anything > 10**12 is raw.
            def _h(raw: int) -> float:
                return raw / _S if raw > 10 ** 12 else float(raw)

            usd_reserve   = _h(int(grp.get("reserve_usd") or 0))
            btc_reserve   = _h(int(grp.get("reserve_btc") or 0))
            sun_reserve   = _h(int(grp.get("reserve_sun") or 0))
            vault_token_h = _h(int(grp.get("vault_token_bal") or 0))

            treasury_usd = (
                usd_reserve
                + btc_reserve * btc_p
                + sun_reserve * sun_p
                + vault_token_h * tok_p
            )

            # LP positions: for each pool the group has shares in,
            # the group owns ``shares / total_lp`` of the pool's
            # reserves. We accumulate three numbers per group in a
            # single pass:
            #   lp_usd_value  -- USD value of BOTH legs (combined view)
            #   lp_usd_leg    -- USD value of the non-own-token leg
            #                    only (USD-side view); equals lp_usd_value
            #                    minus the own-token leg's USD value
            #   lp_own_token  -- amount of the own-token leg (token view)
            lp_usd_value = 0.0
            lp_usd_leg = 0.0
            lp_own_token = 0.0
            for pos in lp_by_group.get(grp["group_id"], []):
                share_num, share_den = pos["shares_raw"], pos["total_raw"]
                ra_h = pos["ra_raw"] / _S
                rb_h = pos["rb_raw"] / _S
                pa = await _price(pos["token_a"])
                pb = await _price(pos["token_b"])
                share_a = ra_h * share_num / share_den
                share_b = rb_h * share_num / share_den
                lp_usd_value += (share_a * pa) + (share_b * pb)
                if sym and pos["token_a"] == sym:
                    lp_own_token += share_a
                    lp_usd_leg  += share_b * pb
                elif sym and pos["token_b"] == sym:
                    lp_own_token += share_b
                    lp_usd_leg  += share_a * pa
                else:
                    # Pool has none of the group's own token (e.g. an
                    # exotic pair). Treat both legs as USD-side.
                    lp_usd_leg += (share_a * pa) + (share_b * pb)

            usd_only = (
                usd_reserve
                + btc_reserve * btc_p
                + sun_reserve * sun_p
                + lp_usd_leg
            )
            token_total = vault_token_h + lp_own_token

            ranked.append({
                "group": grp,
                "symbol": sym,
                "tok_price": tok_p,
                "treasury_usd_total": treasury_usd,
                "lp_usd_value": lp_usd_value,
                "combined_usd": treasury_usd + lp_usd_value,
                "usd_only": usd_only,
                "token_amount": token_total,
                "vault_token_h": vault_token_h,
                "usd_reserve": usd_reserve,
                "btc_reserve": btc_reserve,
                "sun_reserve": sun_reserve,
            })

        if mode == "usd":
            ranked.sort(key=lambda r: r["usd_only"], reverse=True)
            title = "💵 Group USD Holdings"
            footer_alt = "value (combined) / token"
        elif mode == "token":
            ranked.sort(key=lambda r: r["token_amount"] * r["tok_price"], reverse=True)
            title = "🪙 Group Token Holdings"
            footer_alt = "value (combined) / usd"
        else:
            ranked.sort(key=lambda r: r["combined_usd"], reverse=True)
            title = "💰 Group Total Value (treasury + LP)"
            footer_alt = "usd / token"

        # Drop empty rows so the lb isn't padded with zeroes
        if mode == "token":
            ranked = [r for r in ranked if r["token_amount"] > 0]
        elif mode == "usd":
            ranked = [r for r in ranked if r["usd_only"] > 0]
        else:
            ranked = [r for r in ranked if r["combined_usd"] > 0]

        if not ranked:
            await ctx.reply_error(
                f"No groups have any {mode if mode != 'combined' else ''} value to rank yet."
            )
            return

        per_page = 10
        pages: list[discord.Embed] = []
        total_pages = max(1, (len(ranked) + per_page - 1) // per_page)
        prefix = ctx.prefix or ","
        for i in range(0, len(ranked), per_page):
            chunk = ranked[i:i + per_page]
            page = i // per_page + 1
            b = card(f"{title} -- Top {len(ranked)} (Page {page}/{total_pages})", color=C_GOLD)
            lines: list[str] = []
            for rank, row in enumerate(chunk, start=i + 1):
                grp = row["group"]
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"`#{rank:>2}`")
                tag_str = f" `[{grp['tag']}]`" if grp.get("tag") else ""
                sym = row["symbol"] or "-"
                if mode == "token":
                    head = (
                        f"**{row['token_amount']:,.4f} {sym}**  "
                        f"(~{fmt_usd(row['token_amount'] * row['tok_price'])})"
                    )
                    sub_parts: list[str] = []
                    if row["vault_token_h"] > 0:
                        sub_parts.append(f"vault {row['vault_token_h']:,.2f}")
                    lp_only_token = row["token_amount"] - row["vault_token_h"]
                    if lp_only_token > 0:
                        sub_parts.append(f"LP {lp_only_token:,.2f}")
                    sub = "  ·  ".join(sub_parts) or "-"
                elif mode == "usd":
                    head = f"**{fmt_usd(row['usd_only'])}**"
                    sub_parts = []
                    if row["usd_reserve"] > 0:
                        sub_parts.append(f"USD {fmt_usd(row['usd_reserve'])}")
                    if row["btc_reserve"] > 0:
                        sub_parts.append(f"{row['btc_reserve']:,.4f} MTA")
                    if row["sun_reserve"] > 0:
                        sub_parts.append(f"{row['sun_reserve']:,.2f} SUN")
                    sub = "  ·  ".join(sub_parts) or "-"
                else:
                    head = f"**{fmt_usd(row['combined_usd'])}**"
                    sub_parts = [
                        f"treasury {fmt_usd(row['treasury_usd_total'])}",
                        f"LP {fmt_usd(row['lp_usd_value'])}",
                    ]
                    sub = "  ·  ".join(sub_parts)
                lines.append(
                    f"{medal} **{grp['name']}**{tag_str}  ·  `{sym}`\n"
                    f"        {head}\n"
                    f"        {sub}"
                )
            b.description("\n".join(lines))
            b.footer(f"{prefix}group lb {footer_alt}")
            pages.append(b.build())
        await send_paginated(ctx, pages)

    # ── $group reserve ────────────────────────────────────────────────────────

    @group.group(name="reserve", invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def group_reserve(self, ctx: DiscoContext) -> None:
        """Show your group's reserve balance and settings.
        Subcommands: .group reserve set <pct>"""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp:
            await ctx.reply_error("Could not find your group.")
            return

        reserve_usd = grp.h("reserve_usd")
        reserve_btc = grp.h("reserve_btc")
        vault_bal   = float(grp.get("vault_token_bal") or 0.0)
        tok_sym     = grp.get("token_symbol") or ""
        reserve_pct = grp.get("reserve_pct", 5.0)
        upgrades    = await ctx.db.get_group_upgrades(ctx.guild_id, grp["group_id"])
        upgrade_ids = [u["upgrade_id"] for u in upgrades]

        # Compute total reserve value in USD
        btc_price_row = await ctx.db.get_price("MTA", ctx.guild_id)
        btc_price = float(btc_price_row["price"]) if btc_price_row else 0.0
        tok_price = 0.0
        if tok_sym:
            tok_row = await ctx.db.get_price(tok_sym, ctx.guild_id)
            tok_price = float(tok_row["price"]) if tok_row else 0.0
        total_value = reserve_usd + reserve_btc * btc_price + vault_bal * tok_price

        # Aggregate active tribute % so the embed lists every active reserve
        # inflow alongside the mining cut. Mirrors the math in
        # services/group_reserve.py so members see the actual rate they earn.
        hall_cfg = Config.GROUP_HALL_UPGRADES
        t_fish = t_farm = t_delv = t_craft = t_mult = 0.0
        for uid in upgrade_ids:
            cfg = hall_cfg.get(uid) or {}
            eff = cfg.get("effect", {})
            t_fish  += eff.get("tribute_fishing_pct",  0.0)
            t_farm  += eff.get("tribute_farming_pct",  0.0)
            t_delv  += eff.get("tribute_dungeon_pct",  0.0)
            t_craft += eff.get("tribute_crafting_pct", 0.0)
            t_mult  += eff.get("tribute_multiplier",   0.0)
        mult = 1.0 + t_mult
        active_inflows: list[str] = []
        if reserve_pct > 0:
            active_inflows.append(f"{reserve_pct:.1f}% of every mined block")
        if t_fish > 0:
            active_inflows.append(f"{t_fish*mult*100:.2f}% fishing cashouts")
        if t_farm > 0:
            active_inflows.append(f"{t_farm*mult*100:.2f}% farming cashouts")
        if t_delv > 0:
            active_inflows.append(f"{t_delv*mult*100:.2f}% delve cashouts")
        if t_craft > 0:
            active_inflows.append(f"{t_craft*mult*100:.2f}% crafting cashouts")
        active_inflows.append("LP yield from group LP positions")
        inflow_str = "\n".join(f"- {s}" for s in active_inflows)

        balance_lines = [f"`{fmt_usd(reserve_usd)}` USD"]
        balance_lines.append(
            f"`{fmt_token(reserve_btc, 'MTA', '🟡')}` MTA "
            f"({fmt_usd(reserve_btc * btc_price)})"
        )
        if tok_sym:
            balance_lines.append(
                f"`{vault_bal:,.4f} {tok_sym}` "
                f"({fmt_usd(vault_bal * tok_price)})"
            )
        balance_str = "\n".join(balance_lines)

        p = ctx.prefix or Config.PREFIX
        embed = (
            card(f"💰 {grp['name']}  -  Reserve", color=C_PURPLE)
            .description(
                "Your group's shared treasury. Funds **Hall upgrades** "
                "and **LP pool seeding**. The Reserve Rate below is the "
                "share of every mined block that goes here."
            )
            .field("Total Value",   f"**{fmt_usd(total_value)}**",              True)
            .field("Reserve Rate",  f"**{reserve_pct:.1f}%** per mined block",  True)
            .field("Hall Upgrades", _format_upgrades(upgrade_ids),              True)
            .field("Balance",       balance_str,                                False)
            .field("How It Grows",  inflow_str[:1024],                          False)
            .footer(
                f"Change rate: {p}group reserve set <0-100>  •  "
                f"Spend: {p}group upgrade buy <id>"
            )
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── $group reserve set ───────────────────────────────────────────────────

    @group_reserve.command(name="set")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_reserve_set(self, ctx: DiscoContext, pct: float) -> None:
        """Set the % of each mined block that flows into the reserve (founder only).
        Usage: .group reserve set <0-100>"""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can change the reserve rate.")
            return

        if pct < 0 or pct > 100:
            await ctx.reply_error("Reserve rate must be between 0 and 100.")
            return

        await ctx.db.update_mining_group_fields(ctx.guild_id, grp["group_id"], reserve_pct=pct)
        await ctx.reply_success(
            f"Reserve rate set to **{pct:.1f}%** of each mined block.",
            title="✅ Reserve Updated",
        )

    # ── $group upgrade ────────────────────────────────────────────────────────

    @group.group(name="upgrade", invoke_without_command=True)
    @guild_only
    async def group_upgrade(self, ctx: DiscoContext) -> None:
        """Group upgrade commands. Use .group upgrade list or .group upgrade buy <id>."""
        if await suggest_subcommand(ctx, self.group_upgrade):
            return
        await ctx.send_help(ctx.command)

    @group_upgrade.command(name="list", aliases=["ls"])
    @guild_only
    async def group_upgrade_list(self, ctx: DiscoContext) -> None:
        """Show all available Group Hall upgrades with costs and effects."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        grp = None
        purchased_ids: list[str] = []
        if membership:
            grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
            if grp:
                upgrades      = await ctx.db.get_group_upgrades(ctx.guild_id, grp["group_id"])
                purchased_ids = [u["upgrade_id"] for u in upgrades]

        # Spendable value for upgrades = USD bucket + MTA bucket + group
        # token vault, all converted to USD at the live oracle. The buy
        # drains in priority: USD -> MTA -> token vault. Routed through
        # the shared helper so .group hall info / .group upgrade buy
        # always agree on the total.
        reserve_total_h = await _spendable_reserve_total(ctx, grp) if grp else 0.0
        hall_cfg = Config.GROUP_HALL_UPGRADES

        # Group upgrades by line
        lines: dict[str, list[str]] = {}
        for uid, cfg in hall_cfg.items():
            line = cfg.get("line", "other")
            lines.setdefault(line, []).append(uid)

        line_labels = {
            "atmosphere": "Atmosphere",
            "access":     "Access",
            "expansion":  "Expansion",
            "industry":   "Industry",
            "tribute":    "Tribute",
        }
        embeds = []
        for line_key, uids in lines.items():
            label = line_labels.get(line_key, line_key.title())
            _b = card(f"🏛️ Hall Upgrades - {label}", color=C_PURPLE)
            for uid in uids:
                cfg = hall_cfg[uid]
                emoji = cfg.get("emoji", "🔧")
                tier = cfg.get("tier", 1)
                requires = cfg.get("requires", [])
                cost_usd = _h(int(cfg.get("cost_usd", 0) or 0))
                locked = any(r not in purchased_ids for r in requires)
                if uid in purchased_ids:
                    status = "✅ Purchased"
                elif locked:
                    req_names = ", ".join(hall_cfg.get(r, {}).get("name", r) for r in requires)
                    status = f"🔒 Requires: {req_names}"
                else:
                    affordable = "✓" if cost_usd <= reserve_total_h else ""
                    status = f"**{fmt_usd(cost_usd)}** {affordable}"
                # Format effects
                eff_parts = []
                eff = cfg.get("effect", {})
                if eff.get("hall_gambling_bonus"):
                    eff_parts.append(f"+{eff['hall_gambling_bonus']*100:.0f}% gambling (in Hall)")
                if eff.get("hall_daily_bonus"):
                    eff_parts.append(f"+{eff['hall_daily_bonus']*100:.0f}% daily (in Hall)")
                if eff.get("hall_work_bonus"):
                    eff_parts.append(f"+{eff['hall_work_bonus']*100:.0f}% work (in Hall)")
                if eff.get("member_fishing_bonus"):
                    eff_parts.append(f"+{eff['member_fishing_bonus']*100:.0f}% fishing (group-wide)")
                if eff.get("member_farming_bonus"):
                    eff_parts.append(f"+{eff['member_farming_bonus']*100:.0f}% farming (group-wide)")
                if eff.get("member_dungeon_bonus"):
                    eff_parts.append(f"+{eff['member_dungeon_bonus']*100:.0f}% delves (group-wide)")
                if eff.get("member_crafting_bonus"):
                    eff_parts.append(f"+{eff['member_crafting_bonus']*100:.0f}% crafting (group-wide)")
                if eff.get("tribute_fishing_pct"):
                    eff_parts.append(f"+{eff['tribute_fishing_pct']*100:.2f}% fishing -> reserve")
                if eff.get("tribute_farming_pct"):
                    eff_parts.append(f"+{eff['tribute_farming_pct']*100:.2f}% farming -> reserve")
                if eff.get("tribute_dungeon_pct"):
                    eff_parts.append(f"+{eff['tribute_dungeon_pct']*100:.2f}% delve -> reserve")
                if eff.get("tribute_crafting_pct"):
                    eff_parts.append(f"+{eff['tribute_crafting_pct']*100:.2f}% craft -> reserve")
                if eff.get("tribute_multiplier"):
                    eff_parts.append(f"+{eff['tribute_multiplier']*100:.0f}% on every tribute")
                if eff.get("hall_unlock"):
                    eff_parts.append(f"Unlocks {eff['hall_unlock']} commands in Hall")
                if eff.get("group_token_trading"):
                    eff_parts.append("Enables group token trading")
                if eff.get("group_max_members"):
                    eff_parts.append(f"+{int(eff['group_max_members'])} member slots")
                eff_str = " · ".join(eff_parts) if eff_parts else ""
                field_val = f"{cfg['description']}\n{eff_str}\nCost: {status}"
                _b.field(f"{emoji} {cfg['name']} (T{tier}) - `{uid}`", field_val[:1024], False)
            embeds.append(_b.build())

        # Reserve summary footer - ALWAYS shown (even with no upgrades
        # purchased) so founders can see how much they have to spend.
        # Breaks down USD + MTA + token vault so the total matches the
        # buy flow's drain priority.
        if grp:
            _ru = grp.h("reserve_usd")
            _rb = grp.h("reserve_btc")
            _vb = float(grp.get("vault_token_bal") or 0.0)
            _ts = grp.get("token_symbol") or ""
            _btc_row = await ctx.db.get_price("MTA", ctx.guild_id)
            _btc_p   = float(_btc_row["price"]) if _btc_row else 0.0
            _tok_p   = 0.0
            if _ts:
                _tok_row = await ctx.db.get_price(_ts, ctx.guild_id)
                _tok_p   = float(_tok_row["price"]) if _tok_row else 0.0

            _reserve_lines = [
                f"USD bucket: **{fmt_usd(_ru)}**",
                f"MTA bucket: **{fmt_token(_rb, 'MTA', '🟡')}** ({fmt_usd(_rb * _btc_p)})",
            ]
            if _ts:
                _reserve_lines.append(
                    f"Token vault: **{_vb:,.4f} {_ts}** ({fmt_usd(_vb * _tok_p)})"
                )
            _reserve_lines.append(f"\n**Total spendable: {fmt_usd(reserve_total_h)}**")

            bonus_summary = _format_upgrade_bonuses(purchased_ids) if purchased_ids else "None"
            _b3 = (
                card("💰 Spendable Reserve", color=C_AMBER)
                .description("\n".join(_reserve_lines))
                .field("Active Bonuses", bonus_summary, False)
                .footer("Drain order on buy: USD -> MTA -> Token Vault  •  .group upgrade buy <id>")
            )
            embeds.append(_b3.build())

        if not embeds:
            embeds = [card("🏛️ Hall Upgrades", color=C_PURPLE).description("No upgrades defined.").build()]

        await ctx.reply(embeds=embeds, mention_author=False)

    @group_upgrade.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_upgrade_buy(self, ctx: DiscoContext, upgrade_id: str) -> None:
        """Purchase a Hall upgrade for your group using the reserve (founder only).
        Usage: .group upgrade buy <upgrade_id>"""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can purchase Hall upgrades.")
            return

        hall_cfg = Config.GROUP_HALL_UPGRADES
        uid = upgrade_id.lower().strip()
        cfg = hall_cfg.get(uid)
        if not cfg:
            valid = ", ".join(f"`{k}`" for k in hall_cfg)
            await ctx.reply_error(
                f"Unknown Hall upgrade `{upgrade_id}`.\nAvailable: {valid}\n"
                f"See `.group upgrade list` for details."
            )
            return

        existing = await ctx.db.get_group_upgrades(ctx.guild_id, grp["group_id"])
        existing_ids = {u["upgrade_id"] for u in existing}
        if uid in existing_ids:
            await ctx.reply_error(f"**{cfg['name']}** is already purchased.")
            return

        # Check prerequisites
        requires = cfg.get("requires", [])
        for req in requires:
            if req not in existing_ids:
                req_name = hall_cfg.get(req, {}).get("name", req)
                await ctx.reply_error(f"**{cfg['name']}** requires **{req_name}** first.")
                return

        from core.framework.scale import to_raw as _fsr
        cost_usd_raw = cfg.get("cost_usd", 0)
        cost_usd = _h(int(cost_usd_raw or 0))
        reserve_usd = grp.h("reserve_usd")
        reserve_btc = grp.h("reserve_btc")
        vault_bal   = float(grp.get("vault_token_bal") or 0.0)
        tok_sym     = grp.get("token_symbol") or ""

        # Fetch oracle prices to compute total spendable liquidity. The
        # group's spendable funds are USD + MTA + token vault, all
        # converted at live oracle rates. The buy drains in priority:
        # USD -> MTA -> token vault.
        btc_price_row = await ctx.db.get_price("MTA", ctx.guild_id)
        btc_price_human = float(btc_price_row["price"]) if btc_price_row else 0.0
        tok_price_human = 0.0
        if tok_sym:
            tok_price_row = await ctx.db.get_price(tok_sym, ctx.guild_id)
            tok_price_human = float(tok_price_row["price"]) if tok_price_row else 0.0
        total_liquid = (
            reserve_usd
            + reserve_btc * btc_price_human
            + vault_bal * tok_price_human
        )

        if total_liquid < cost_usd:
            shortage = cost_usd - total_liquid
            tok_line = (
                f" + `{vault_bal:,.4f} {tok_sym}` (~{fmt_usd(vault_bal * tok_price_human)})"
                if tok_sym and vault_bal > 0
                else ""
            )
            await ctx.reply_error(
                f"Insufficient reserve. Need **{fmt_usd(cost_usd)}**, "
                f"have **{fmt_usd(reserve_usd)}** USD + **{fmt_token(reserve_btc, 'MTA', '🟡')}**{tok_line} "
                f"(~{fmt_usd(total_liquid)} total, short **{fmt_usd(shortage)}**).\n"
                f"Reserve grows from PoW mining, LP yield, and any cashout tributes "
                f"unlocked via the Tribute upgrade line."
            )
            return

        # Drain USD bucket first, then MTA, then the group token vault.
        # Each step covers as much remaining cost as it can; the next
        # step picks up only the shortfall.
        remaining = cost_usd
        spent_usd_h = 0.0
        spent_btc_h = 0.0
        spent_tok_h = 0.0

        if remaining > 0 and reserve_usd > 0:
            pay_usd = min(remaining, reserve_usd)
            success = await ctx.db.spend_group_reserve_usd(
                ctx.guild_id, grp["group_id"], _fsr(pay_usd),
            )
            if not success:
                await ctx.reply_error("Reserve balance insufficient (concurrent update). Try again.")
                return
            remaining   -= pay_usd
            spent_usd_h  = pay_usd

        if remaining > 0 and reserve_btc > 0 and btc_price_human > 0:
            btc_value = reserve_btc * btc_price_human
            pay_value = min(remaining, btc_value)
            btc_needed = pay_value / btc_price_human
            await ctx.db.add_group_reserve_btc(
                ctx.guild_id, grp["group_id"], -btc_needed,
            )
            remaining   -= pay_value
            spent_btc_h  = btc_needed

        if remaining > 0 and tok_sym and vault_bal > 0 and tok_price_human > 0:
            tok_needed = remaining / tok_price_human
            # Cap at the actual vault balance to avoid asking the DB for
            # a deduction it would refuse on the WHERE clause.
            tok_needed = min(tok_needed, vault_bal)
            tok_success = await ctx.db.deduct_group_vault_tokens(
                ctx.guild_id, grp["group_id"], tok_needed,
            )
            if not tok_success:
                await ctx.reply_error(
                    "Vault deduction failed (concurrent update). Try again."
                )
                return
            remaining   -= tok_needed * tok_price_human
            spent_tok_h  = tok_needed

        if remaining > 1e-9:
            await ctx.reply_error(
                "Reserve balance insufficient (concurrent update). Try again."
            )
            return

        await ctx.db.add_group_upgrade(ctx.guild_id, grp["group_id"], uid)
        new_balance = total_liquid - cost_usd

        # If Trading Desk: enable group token trading
        effect = cfg.get("effect", {})
        token_note = ""
        if effect.get("group_token_trading") and grp.get("token_symbol"):
            sym = grp["token_symbol"]
            try:
                await ctx.db.execute(
                    "UPDATE guild_tokens SET trading_enabled=TRUE WHERE guild_id=$1 AND symbol=$2",
                    ctx.guild_id, sym,
                )
                token_note = f"\n\nGroup token **{sym}** is now open for trading!"
            except Exception:
                token_note = f"\n\n(Group token trading could not be enabled automatically - run `.admin grouptoken enable {sym}` manually.)"

        # Build effect summary
        eff_parts = []
        if effect.get("hall_gambling_bonus"):
            eff_parts.append(f"+{effect['hall_gambling_bonus']*100:.0f}% gambling winnings inside the Hall")
        if effect.get("hall_daily_bonus"):
            eff_parts.append(f"+{effect['hall_daily_bonus']*100:.0f}% daily reward inside the Hall")
        if effect.get("hall_work_bonus"):
            eff_parts.append(f"+{effect['hall_work_bonus']*100:.0f}% work earnings inside the Hall")
        if effect.get("member_fishing_bonus"):
            eff_parts.append(f"+{effect['member_fishing_bonus']*100:.0f}% fishing payouts (every member, anywhere)")
        if effect.get("member_farming_bonus"):
            eff_parts.append(f"+{effect['member_farming_bonus']*100:.0f}% farming payouts (every member, anywhere)")
        if effect.get("member_dungeon_bonus"):
            eff_parts.append(f"+{effect['member_dungeon_bonus']*100:.0f}% delve payouts (every member, anywhere)")
        if effect.get("member_crafting_bonus"):
            eff_parts.append(f"+{effect['member_crafting_bonus']*100:.0f}% crafting payouts (every member, anywhere)")
        if effect.get("tribute_fishing_pct"):
            eff_parts.append(f"+{effect['tribute_fishing_pct']*100:.2f}% of fishing cashouts granted to reserve")
        if effect.get("tribute_farming_pct"):
            eff_parts.append(f"+{effect['tribute_farming_pct']*100:.2f}% of farming cashouts granted to reserve")
        if effect.get("tribute_dungeon_pct"):
            eff_parts.append(f"+{effect['tribute_dungeon_pct']*100:.2f}% of delve cashouts granted to reserve")
        if effect.get("tribute_crafting_pct"):
            eff_parts.append(f"+{effect['tribute_crafting_pct']*100:.2f}% of craft cashouts granted to reserve")
        if effect.get("tribute_multiplier"):
            eff_parts.append(f"+{effect['tribute_multiplier']*100:.0f}% on every cashout tribute")
        if effect.get("hall_unlock"):
            eff_parts.append(f"{effect['hall_unlock']} commands unlocked in the Hall")
        if effect.get("group_max_members"):
            eff_parts.append(f"+{int(effect['group_max_members'])} member slots")
        bonus_str = "\n".join(f"- {p}" for p in eff_parts) if eff_parts else cfg.get("description", "")

        spent_parts: list[str] = []
        if spent_usd_h > 0:
            spent_parts.append(fmt_usd(spent_usd_h))
        if spent_btc_h > 0:
            spent_parts.append(fmt_token(spent_btc_h, "MTA", "🟡"))
        if spent_tok_h > 0 and tok_sym:
            spent_parts.append(f"{spent_tok_h:,.4f} {tok_sym}")
        spent_str = " + ".join(spent_parts) if spent_parts else fmt_usd(cost_usd)

        await ctx.reply_success(
            f"Purchased **{cfg.get('emoji', '')} {cfg['name']}**!\n\n"
            f"{bonus_str}{token_note}\n\n"
            f"Spent: **{spent_str}**\n"
            f"Reserve remaining: **{fmt_usd(new_balance)}**",
            title="✅ Hall Upgrade Purchased",
        )

    # ── $group disband ────────────────────────────────────────────────────────

    @group.command(name="disband")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_disband(self, ctx: DiscoContext) -> None:
        """Disband your mining group (founder only)."""
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return

        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can disband the group.")
            return

        confirmed = await ctx.confirm(
            f"Disband **{grp['name']}**? All members will be removed."
        )
        if not confirmed:
            await ctx.reply_error("Disband cancelled.")
            return

        # Archive the Hall thread if one exists
        hall_tid = grp.get("hall_thread_id")
        if hall_tid:
            hall_thread = await self._get_hall_thread(ctx.guild, hall_tid)
            if hall_thread:
                try:
                    await hall_thread.edit(archived=True, locked=True, reason="Group disbanded")
                except Exception:
                    pass
            await self._cache_del(f"discoin:hall:thread:{hall_tid}")

        await ctx.db.disband_mining_group(ctx.guild_id, grp["group_id"])
        await ctx.reply_success(f"**{grp['name']}** has been disbanded.", title="Group Disbanded")

    # ── $group transfer ──────────────────────────────────────────────────────

    @group.command(name="transfer", aliases=["handover", "giveowner"])
    @guild_only
    @no_bots
    @ensure_registered
    async def group_transfer(
        self, ctx: DiscoContext,
        action_or_target: str = "",
    ) -> None:
        """Two-sided ownership handover.

        Founder runs ``,group transfer @user`` to open a proposal; the
        target then runs ``,group transfer accept`` to take ownership or
        ``,group transfer decline`` to refuse. The founder can run
        ``,group transfer cancel`` while the proposal is still open.
        ``,group transfer status`` shows the current pending proposal.

        The target must already be a member of the group (invite them
        first with ``,group invite``). The old founder stays in the
        group after the transfer; ``,group leave`` for a clean exit.
        """
        action = (action_or_target or "").strip().lower()
        if action in ("accept", "decline", "cancel", "status"):
            await self._group_transfer_subcommand(ctx, action)
            return

        target = None
        if action_or_target:
            try:
                target = await commands.MemberConverter().convert(
                    ctx, action_or_target,
                )
            except commands.BadArgument:
                target = None
        if target is None:
            await ctx.reply_error_hint(
                "Mention the new founder, or run "
                "`,group transfer accept` / `decline` / `cancel` / `status`.",
                hint="group transfer @user",
                command_name="group transfer",
            )
            return
        await self._group_transfer_propose(ctx, target)

    async def _group_transfer_propose(
        self, ctx: DiscoContext, target: discord.Member,
    ) -> None:
        if target.bot:
            await ctx.reply_error("You can't transfer ownership to a bot.")
            return
        if target.id == ctx.author.id:
            await ctx.reply_error("You're already the founder of this group.")
            return

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return

        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the current founder can transfer the group.")
            return

        target_membership = await ctx.db.get_user_mining_group(target.id, ctx.guild_id)
        if not target_membership or target_membership["group_id"] != grp["group_id"]:
            await ctx.reply_error(
                f"{target.mention} is not a member of **{grp['name']}**. "
                f"Invite them with `,group invite @user` first."
            )
            return

        existing = await ctx.db.get_group_transfer_proposal(
            ctx.guild_id, grp["group_id"],
        )
        if existing:
            await ctx.reply_error(
                f"There is already a pending proposal to transfer "
                f"**{grp['name']}** to <@{int(existing['to_user_id'])}>. "
                f"Run `,group transfer cancel` first."
            )
            return

        confirmed = await ctx.confirm(
            f"Open a transfer proposal for **{grp['name']}** to {target.mention}? "
            f"They will need to run `,group transfer accept` for the "
            f"handover to take effect. You can `,group transfer cancel` "
            f"any time before they accept."
        )
        if not confirmed:
            await ctx.reply_error("Transfer proposal cancelled.")
            return

        row = await ctx.db.create_group_transfer_proposal(
            ctx.guild_id, grp["group_id"], ctx.author.id, target.id,
        )
        if not row:
            await ctx.reply_error(
                "Could not open the proposal -- one already exists. "
                "Try `,group transfer status`."
            )
            return

        await ctx.reply_success(
            f"Proposal opened. {target.mention} can now run "
            f"`,group transfer accept` to become the founder of "
            f"**{grp['name']}**, or `,group transfer decline` to refuse.",
            title="Transfer Proposal Sent",
        )

    async def _group_transfer_subcommand(
        self, ctx: DiscoContext, action: str,
    ) -> None:
        if action == "accept":
            await self._group_transfer_accept(ctx)
        elif action == "decline":
            await self._group_transfer_decline(ctx)
        elif action == "cancel":
            await self._group_transfer_cancel(ctx)
        elif action == "status":
            await self._group_transfer_status(ctx)

    async def _group_transfer_accept(self, ctx: DiscoContext) -> None:
        proposals = await ctx.db.get_group_transfer_proposals_for_user(
            ctx.guild_id, ctx.author.id,
        )
        if not proposals:
            await ctx.reply_error(
                "No pending transfer proposals are addressed to you."
            )
            return
        if len(proposals) > 1:
            # Edge case: a user is the target of multiple proposals across
            # different groups. Force them to accept inside the group whose
            # membership row points at it (most recent join wins).
            membership = await ctx.db.get_user_mining_group(
                ctx.author.id, ctx.guild_id,
            )
            if not membership:
                await ctx.reply_error(
                    "You have multiple pending proposals; join the target "
                    "group first so the system knows which to accept."
                )
                return
            proposals = [
                p for p in proposals if p["group_id"] == membership["group_id"]
            ]
            if not proposals:
                await ctx.reply_error(
                    "You have multiple pending proposals but none for the "
                    "group you are currently in. Join the right group first."
                )
                return

        prop = proposals[0]
        grp = await ctx.db.get_mining_group(
            ctx.guild_id, group_id=str(prop["group_id"]),
        )
        if not grp:
            await ctx.db.delete_group_transfer_proposal(
                ctx.guild_id, str(prop["group_id"]),
            )
            await ctx.reply_error("That group no longer exists.")
            return
        if grp["founder_id"] != int(prop["from_user_id"]):
            # Founder changed since the proposal was opened -- it's stale.
            await ctx.db.delete_group_transfer_proposal(
                ctx.guild_id, str(prop["group_id"]),
            )
            await ctx.reply_error(
                "That proposal is stale -- the founder has changed since "
                "it was opened."
            )
            return

        try:
            await ctx.db.transfer_mining_group(
                ctx.guild_id, str(prop["group_id"]), ctx.author.id,
            )
        except Exception:
            log.exception(
                "group transfer accept failed gid=%s grp=%s to=%s",
                ctx.guild_id, prop["group_id"], ctx.author.id,
            )
            await ctx.reply_error(
                "Transfer failed -- please try again. If it keeps failing, "
                "contact an admin."
            )
            return

        await ctx.reply_success(
            f"You are now the founder of **{grp['name']}**. The previous "
            f"founder stays as a member.",
            title="Ownership Accepted",
        )

    async def _group_transfer_decline(self, ctx: DiscoContext) -> None:
        proposals = await ctx.db.get_group_transfer_proposals_for_user(
            ctx.guild_id, ctx.author.id,
        )
        if not proposals:
            await ctx.reply_error(
                "No pending transfer proposals are addressed to you."
            )
            return
        for p in proposals:
            await ctx.db.delete_group_transfer_proposal(
                ctx.guild_id, str(p["group_id"]),
            )
        await ctx.reply_success(
            f"Declined {len(proposals)} pending proposal(s).",
            title="Transfer Declined",
        )

    async def _group_transfer_cancel(self, ctx: DiscoContext) -> None:
        membership = await ctx.db.get_user_mining_group(
            ctx.author.id, ctx.guild_id,
        )
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(
            ctx.guild_id, group_id=membership["group_id"],
        )
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the founder can cancel a transfer.")
            return
        existing = await ctx.db.get_group_transfer_proposal(
            ctx.guild_id, grp["group_id"],
        )
        if not existing:
            await ctx.reply_error("There is no open transfer proposal to cancel.")
            return
        await ctx.db.delete_group_transfer_proposal(
            ctx.guild_id, grp["group_id"],
        )
        await ctx.reply_success(
            f"Cancelled the open transfer proposal for **{grp['name']}**.",
            title="Transfer Cancelled",
        )

    async def _group_transfer_status(self, ctx: DiscoContext) -> None:
        membership = await ctx.db.get_user_mining_group(
            ctx.author.id, ctx.guild_id,
        )
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        grp = await ctx.db.get_mining_group(
            ctx.guild_id, group_id=membership["group_id"],
        )
        if not grp:
            await ctx.reply_error("Group not found.")
            return
        existing = await ctx.db.get_group_transfer_proposal(
            ctx.guild_id, grp["group_id"],
        )
        if not existing:
            await ctx.reply_success(
                f"No open transfer proposal for **{grp['name']}**.",
                title="Transfer Status",
            )
            return
        await ctx.reply_success(
            f"**{grp['name']}**: <@{int(existing['from_user_id'])}> "
            f"-> <@{int(existing['to_user_id'])}> (opened "
            f"{fmt_ts(existing['created_at'])}).",
            title="Pending Transfer",
        )

    # ── $group pool ───────────────────────────────────────────────────────────

    @group.command(name="pool")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_pool(self, ctx: DiscoContext, subcommand: str = "", *, args: str = "") -> None:
        """Cross-group LP pool commands. Usage: .group pool <propose|accept|decline|list|cancel>"""
        sub = subcommand.lower()
        if sub == "propose":
            await self._group_pool_propose(ctx, args.strip())
        elif sub == "accept":
            await self._group_pool_accept(ctx, args.strip())
        elif sub == "decline":
            await self._group_pool_decline(ctx, args.strip())
        elif sub == "list":
            await self._group_pool_list(ctx)
        elif sub == "cancel":
            await self._group_pool_cancel(ctx)
        elif sub == "harvest":
            await self._group_pool_harvest(ctx, args.strip())
        elif sub in ("deposit", "topup", "addlp"):
            await self._group_pool_deposit(ctx, args.strip())
        else:
            p = ctx.prefix or Config.PREFIX
            # Pool surface is now ONLY for cross-group partnership
            # plumbing -- deposit + harvest moved under ,group lp so
            # every liquidity action lives in one place. The legacy
            # ,group pool deposit / ,group pool harvest still work
            # (mapped above) for back-compat but aren't shown here.
            await ctx.reply_error(
                f"Unknown subcommand. ``,group pool`` only handles "
                f"cross-group **partnerships**:\n"
                f"`{p}group pool propose <group name or tag>` - propose a partnership\n"
                f"`{p}group pool accept <proposal id>` - accept an incoming partnership\n"
                f"`{p}group pool decline <proposal id>` - decline an incoming partnership\n"
                f"`{p}group pool list` - see pending proposals\n"
                f"`{p}group pool cancel` - cancel your outgoing proposal\n\n"
                f"**Adding / harvesting LP** is now one group: "
                f"`{p}group lp` (status / deposit / withdraw / topup / harvest). "
                f"`{p}group help` -> **LP Treasury** for the full surface."
            )

    @staticmethod
    async def _seed_group_pool_from_vault(
        db,
        guild_id: int,
        pool_id: str,
        grp_a: dict,
        grp_b: dict,
        token_a: str,
        token_b: str,
    ) -> str:
        """Attempt to seed the freshly-created group pool from each group's vault.

        Each group contributes up to GROUP_POOL_SEED_PCT of its vault balance
        (in USD), capped at GROUP_POOL_SEED_MAX_USD and gated by
        GROUP_POOL_SEED_MIN_USD.  Both sides contribute equal USD value so the
        LP split is always 50/50.

        Returns a human-readable string describing what was seeded (or why it
        was skipped), suitable for inclusion in a confirmation message.
        """
        sym_a = (token_a or "").upper()
        sym_b = (token_b or "").upper()
        grp_a_sym = (grp_a.get("token_symbol") or "").upper()
        grp_b_sym = (grp_b.get("token_symbol") or "").upper()
        vault_a = float(grp_a.get("vault_token_bal") or 0.0)
        vault_b = float(grp_b.get("vault_token_bal") or 0.0)

        if not sym_a or not sym_b:
            return "Seeding skipped: one or both groups have no token symbol."
        if grp_a_sym != sym_a or grp_b_sym != sym_b:
            return (
                "Seeding skipped: one or both group token symbols changed after proposal; "
                "re-propose the partnership for the updated symbols."
            )

        # Fetch token prices (fall back to a negligible value so we never divide-by-zero)
        pr_a = await db.get_price(sym_a, guild_id)
        pr_b = await db.get_price(sym_b, guild_id)
        price_a = float(pr_a["price"]) if pr_a else 0.0
        price_b = float(pr_b["price"]) if pr_b else 0.0

        if price_a <= 0 or price_b <= 0:
            return (
                "Seeding skipped: set a price for both tokens with "
                "`,admin setprice` before the pool can be auto-seeded."
            )

        # USD value each group can contribute
        usd_a_avail = vault_a * price_a * Config.GROUP_POOL_SEED_PCT
        usd_b_avail = vault_b * price_b * Config.GROUP_POOL_SEED_PCT
        # Both sides must be equal in USD; take the lower of the two, then cap
        seed_usd = min(usd_a_avail, usd_b_avail, Config.GROUP_POOL_SEED_MAX_USD)

        if seed_usd < Config.GROUP_POOL_SEED_MIN_USD:
            return (
                f"Seeding skipped: vault contribution too small "
                f"(need at least {fmt_usd(Config.GROUP_POOL_SEED_MIN_USD)} per side). "
                f"Add LP manually once each group has enough vault tokens."
            )

        # Convert seed USD to token amounts
        amount_a = seed_usd / price_a
        amount_b = seed_usd / price_b

        # Deduct from each group's vault (both deductions must succeed)
        ok_a = await db.deduct_group_vault_tokens(guild_id, grp_a["group_id"], amount_a)
        if not ok_a:
            return (
                f"Seeding skipped: **{grp_a['name']}** vault insufficient "
                f"(need {amount_a:,.4f} {sym_a})."
            )
        ok_b = await db.deduct_group_vault_tokens(guild_id, grp_b["group_id"], amount_b)
        if not ok_b:
            # Roll back group A's deduction
            await db.mint_vault_tokens(guild_id, grp_a["group_id"], amount_a)
            return (
                f"Seeding skipped: **{grp_b['name']}** vault insufficient "
                f"(need {amount_b:,.4f} {sym_b})."
            )

        # Seed the pool and record group LP positions
        try:
            _pid, ca, cb = db.make_pool_id(sym_a, sym_b)
            if ca == sym_a.upper():
                seed_ca, seed_cb = amount_a, amount_b
            else:
                seed_ca, seed_cb = amount_b, amount_a
            seeded = await db.seed_group_pool(
                guild_id, pool_id,
                seed_ca, seed_cb,
                grp_a["group_id"], grp_b["group_id"],
                cost_basis_usd_per_side=seed_usd,
            )
            if not seeded:
                raise ValueError(
                    f"Group LP seed failed: missing pool guild={guild_id} pool={pool_id} "
                    f"groups={grp_a['group_id']},{grp_b['group_id']}. "
                    "Verify the pool exists, then retry acceptance or add LP manually."
                )
        except Exception:
            # Roll back both vault deductions on failure
            await db.mint_vault_tokens(guild_id, grp_a["group_id"], amount_a)
            await db.mint_vault_tokens(guild_id, grp_b["group_id"], amount_b)
            raise

        return (
            f"Auto-seeded with **{amount_a:,.4f} {sym_a}** + **{amount_b:,.4f} {sym_b}** "
            f"({fmt_usd(seed_usd)} per side). Each group holds 50% of the initial LP. "
            f"Use `.group pool harvest` to claim fee earnings back to reserve."
        )

    @staticmethod
    async def _seed_group_pool_from_reserve(
        db,
        guild_id: int,
        pool_id: str,
        grp_a: dict,
        grp_b: dict,
        token_a: str,
        token_b: str,
    ) -> str:
        """Fallback seed using each group's reserve_usd when vault tokens are too thin.

        Buys ``GROUP_POOL_SEED_MIN_USD`` (capped at ``GROUP_POOL_SEED_MAX_USD``)
        worth of each side from the group's reserve_usd at current oracle prices
        and seeds the pool 50/50. Treats reserve_usd as the source of truth so a
        founder who funds the reserve can guarantee a partnership goes live even
        when neither vault has the matching tokens minted yet.
        """
        sym_a = (token_a or "").upper()
        sym_b = (token_b or "").upper()
        if not sym_a or not sym_b:
            return "Reserve fallback skipped: missing token symbols."

        price_a = await _resolve_token_price(db, sym_a, guild_id)
        price_b = await _resolve_token_price(db, sym_b, guild_id)
        if price_a <= 0 or price_b <= 0:
            return (
                "Reserve fallback skipped: set a price for both tokens with "
                "`,admin setprice` so the reserve can buy in."
            )

        reserve_a_usd = float(grp_a.get("reserve_usd") or 0.0)
        reserve_b_usd = float(grp_b.get("reserve_usd") or 0.0)
        seed_usd = min(
            reserve_a_usd, reserve_b_usd,
            float(Config.GROUP_POOL_SEED_MAX_USD),
        )
        if seed_usd < float(Config.GROUP_POOL_SEED_MIN_USD):
            return (
                f"Reserve fallback skipped: each group needs at least "
                f"{fmt_usd(Config.GROUP_POOL_SEED_MIN_USD)} in reserve_usd "
                f"(have {fmt_usd(reserve_a_usd)} / {fmt_usd(reserve_b_usd)})."
            )

        amount_a = seed_usd / price_a
        amount_b = seed_usd / price_b

        # Debit both reserves first (atomic-ish: rollback group A if group B fails).
        await db.add_group_reserve_usd(guild_id, grp_a["group_id"], -seed_usd)
        try:
            await db.add_group_reserve_usd(guild_id, grp_b["group_id"], -seed_usd)
        except Exception:
            await db.add_group_reserve_usd(guild_id, grp_a["group_id"], seed_usd)
            raise

        try:
            _pid, ca, cb = db.make_pool_id(sym_a, sym_b)
            if ca == sym_a:
                seed_ca, seed_cb = amount_a, amount_b
            else:
                seed_ca, seed_cb = amount_b, amount_a
            seeded = await db.seed_group_pool(
                guild_id, pool_id,
                seed_ca, seed_cb,
                grp_a["group_id"], grp_b["group_id"],
                cost_basis_usd_per_side=seed_usd,
            )
            if not seeded:
                raise ValueError(
                    f"Reserve fallback seed failed: pool not found guild={guild_id} pool={pool_id}"
                )
        except Exception:
            # Roll back both reserve debits on failure
            await db.add_group_reserve_usd(guild_id, grp_a["group_id"], seed_usd)
            await db.add_group_reserve_usd(guild_id, grp_b["group_id"], seed_usd)
            raise

        return (
            f"Reserve-funded seed: **{amount_a:,.4f} {sym_a}** + "
            f"**{amount_b:,.4f} {sym_b}** ({fmt_usd(seed_usd)} per side) "
            f"bought from each group's reserve_usd at oracle prices."
        )

    async def _group_pool_propose(self, ctx: DiscoContext, target_name: str) -> None:
        if not target_name:
            await ctx.reply_error("Usage: `.group pool propose <group name or tag>`")
            return

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        my_grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not my_grp or my_grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can propose a pool partnership.")
            return
        if not my_grp.get("token_symbol"):
            await ctx.reply_error(
                "Your group doesn't have a token yet. Set a group tag first with `.group set tag=XXXX`."
            )
            return

        # Find target group by name or tag
        all_grps = await ctx.db.fetch_all(
            "SELECT * FROM mining_groups WHERE guild_id=$1", ctx.guild_id,
        )
        target_grp = None
        name_lower = target_name.lower()
        for g in all_grps:
            if g["group_id"] == membership["group_id"]:
                continue
            if (g["name"] or "").lower() == name_lower or (g.get("tag") or "").lower() == name_lower:
                target_grp = g
                break
        if not target_grp:
            await ctx.reply_error(f"No group found named **{target_name}**.")
            return
        if not target_grp.get("token_symbol"):
            await ctx.reply_error(f"**{target_grp['name']}** doesn't have a group token yet.")
            return

        token_a = my_grp["token_symbol"]
        token_b = target_grp["token_symbol"]
        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)

        existing = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if existing:
            await ctx.reply_error(f"A pool for **{ca}/{cb}** already exists.")
            return

        proposal_id = await ctx.db.create_group_pool_proposal(
            ctx.guild_id, my_grp["group_id"], target_grp["group_id"],
            ctx.author.id, token_a, token_b,
        )

        # DM the target founder
        target_founder = ctx.guild.get_member(target_grp["founder_id"])
        p = ctx.prefix or Config.PREFIX
        proposal_embed = (
            card("Pool Partnership Proposal", color=C_INFO)
            .description(
                f"**{my_grp['name']}** wants to open a shared liquidity pool with your group.\n\n"
                f"**Pair:** `{ca}` / `{cb}`\n\n"
                f"On acceptance the pool is **automatically seeded** from each group's vault "
                f"(up to 5% of vault balance, capped at {fmt_usd(Config.GROUP_POOL_SEED_MAX_USD)} per side). "
                f"Both groups receive 50% of the initial LP and earn swap fees immediately.\n\n"
                f"Both groups can add more LP with `{p}trade pool add {ca} {cb} <amount_a> <amount_b>`.\n"
                f"You can also accept via `{p}group pool accept {proposal_id}`."
            )
            .footer(f"Proposal #{proposal_id}")
            .build()
        )
        view = GroupPoolProposalView(ctx.guild_id, proposal_id, self.bot)
        self.bot.add_view(view)
        dm_sent = False
        if target_founder:
            try:
                await target_founder.send(embed=proposal_embed, view=view)
                dm_sent = True
            except discord.Forbidden:
                pass

        hint = f" A DM was sent to **{target_founder.display_name}**." if dm_sent else \
               f" Their founder has DMs closed - they can accept via `{p}group pool accept {proposal_id}`."
        await ctx.reply_success(
            f"Proposal sent to **{target_grp['name']}** for pair **{ca}/{cb}**.{hint}\n"
            f"Proposal ID: `{proposal_id}`",
            title="Pool Proposal Sent",
        )

    async def _group_pool_accept(self, ctx: DiscoContext, proposal_id_str: str) -> None:
        if not proposal_id_str.isdigit():
            await ctx.reply_error("Usage: `.group pool accept <proposal id>`")
            return
        proposal_id = int(proposal_id_str)

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        my_grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not my_grp or my_grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can accept pool proposals.")
            return

        proposal = await ctx.db.get_group_pool_proposal(proposal_id, ctx.guild_id)
        if not proposal:
            await ctx.reply_error(f"No pending proposal with ID `{proposal_id}`.")
            return
        if proposal["target_group"] != my_grp["group_id"]:
            await ctx.reply_error("That proposal is not addressed to your group.")
            return

        pool_id, ca, cb = ctx.db.make_pool_id(proposal["token_a"], proposal["token_b"])
        if await ctx.db.get_pool(pool_id, ctx.guild_id):
            await ctx.db.delete_group_pool_proposal(proposal_id, ctx.guild_id)
            await ctx.reply_error(f"Pool **{ca}/{cb}** already exists. Proposal cleaned up.")
            return

        await ctx.db.create_group_pool(pool_id, ctx.guild_id, ca, cb)
        await ctx.db.delete_group_pool_proposal(proposal_id, ctx.guild_id)

        proposer_grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=proposal["proposer_group"])

        # Auto-seed from each group's vault, with a fallback to reserve_usd when
        # vaults are too thin. If both attempts fail the empty pool would let the
        # first user with `addlp` mint 100% of LP shares (geometric-mean rule),
        # locking the pool's price forever -- so we delete it and report the
        # failure instead of leaving zombie pools behind.
        seed_note = (
            await Groups._seed_group_pool_from_vault(
                ctx.db, ctx.guild_id, pool_id,
                proposer_grp, my_grp,
                proposal["token_a"], proposal["token_b"],
            )
            if proposer_grp
            else "Seeding skipped: proposer group not found."
        )

        # Verify the pool actually has liquidity. If not, fall back to seeding
        # from each group's reserve_usd (buying tokens at oracle prices), then
        # delete the pool if that also fails.
        pool_after = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if pool_after and int(pool_after.get("total_lp") or 0) <= 0 and proposer_grp:
            fallback_note = await Groups._seed_group_pool_from_reserve(
                ctx.db, ctx.guild_id, pool_id,
                proposer_grp, my_grp,
                proposal["token_a"], proposal["token_b"],
            )
            seed_note = f"{seed_note}\n{fallback_note}"
            pool_after = await ctx.db.get_pool(pool_id, ctx.guild_id)

        if not pool_after or int(pool_after.get("total_lp") or 0) <= 0:
            await ctx.db.delete_pool(pool_id, ctx.guild_id)
            p = ctx.prefix or Config.PREFIX
            await ctx.reply_error(
                f"Pool **{ca}/{cb}** could not be seeded -- no liquidity was added "
                f"from either group's vault or reserve. The empty pool was removed "
                f"so it cannot be locked at 100% by the first manual depositor. "
                f"Top up either group's vault or reserve_usd, then re-propose with "
                f"`{p}group pool propose <group>`.\n"
                f"{seed_note}"
            )
            return

        p = ctx.prefix or Config.PREFIX
        await ctx.reply_success(
            f"Pool **{ca}/{cb}** is now live!\n"
            f"{seed_note}\n"
            f"Both groups can add more LP with `{p}addlp {ca} {cb} <amount_a> <amount_b>`.",
            title="Partnership Accepted",
        )

        if proposer_grp:
            proposer_member = ctx.guild.get_member(proposer_grp["founder_id"])
            if proposer_member:
                try:
                    await proposer_member.send(
                        f"**{my_grp['name']}** accepted your pool proposal!\n"
                        f"Pool **{ca}/{cb}** is live.\n"
                        f"{seed_note}"
                    )
                except discord.Forbidden:
                    pass

    async def _group_pool_decline(self, ctx: DiscoContext, proposal_id_str: str) -> None:
        if not proposal_id_str.isdigit():
            await ctx.reply_error("Usage: `.group pool decline <proposal id>`")
            return
        proposal_id = int(proposal_id_str)

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        my_grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not my_grp or my_grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can decline pool proposals.")
            return

        proposal = await ctx.db.get_group_pool_proposal(proposal_id, ctx.guild_id)
        if not proposal:
            await ctx.reply_error(f"No pending proposal with ID `{proposal_id}`.")
            return
        if proposal["target_group"] != my_grp["group_id"]:
            await ctx.reply_error("That proposal is not addressed to your group.")
            return

        await ctx.db.delete_group_pool_proposal(proposal_id, ctx.guild_id)
        proposer_grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=proposal["proposer_group"])
        await ctx.reply_success(
            f"Declined proposal #{proposal_id} from **{proposer_grp['name'] if proposer_grp else proposal['proposer_group']}**.",
            title="Proposal Declined",
        )

    async def _group_pool_list(self, ctx: DiscoContext) -> None:
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        my_grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not my_grp or my_grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can view proposals.")
            return

        incoming = await ctx.db.get_incoming_pool_proposals(ctx.guild_id, my_grp["group_id"])
        outgoing = await ctx.db.get_outgoing_pool_proposals(ctx.guild_id, my_grp["group_id"])

        if not incoming and not outgoing:
            await ctx.reply_error("No pending pool proposals for your group.")
            return

        b = card("Pool Proposals", color=C_INFO)
        if incoming:
            lines = []
            for p in incoming:
                pool_id, ca, cb = ctx.db.make_pool_id(p["token_a"], p["token_b"])
                lines.append(f"`#{p['id']}` from **{p['proposer_name']}** - pair `{ca}/{cb}`")
            b.field("Incoming (awaiting your response)", "\n".join(lines))
        if outgoing:
            lines = []
            for p in outgoing:
                pool_id, ca, cb = ctx.db.make_pool_id(p["token_a"], p["token_b"])
                lines.append(f"`#{p['id']}` to **{p['target_name']}** - pair `{ca}/{cb}`")
            b.field("Outgoing (awaiting their response)", "\n".join(lines))

        p = ctx.prefix or Config.PREFIX
        b.footer(f"Accept: {p}group pool accept <id>  |  Decline: {p}group pool decline <id>")
        await ctx.reply(embed=b.build(), mention_author=False)

    async def _group_pool_cancel(self, ctx: DiscoContext) -> None:
        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        my_grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not my_grp or my_grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can cancel proposals.")
            return

        outgoing = await ctx.db.get_outgoing_pool_proposals(ctx.guild_id, my_grp["group_id"])
        if not outgoing:
            await ctx.reply_error("You have no outgoing proposals to cancel.")
            return

        for p in outgoing:
            await ctx.db.delete_group_pool_proposal(p["id"], ctx.guild_id)
        await ctx.reply_success(
            f"Cancelled {len(outgoing)} outgoing proposal(s).",
            title="Proposals Cancelled",
        )

    async def _group_pool_harvest(self, ctx: DiscoContext, args: str) -> None:
        """Claim only the group's accumulated LP fee earnings to reserve_usd.

        Usage: .group pool harvest <TOKEN_A> <TOKEN_B>

        Removes the fraction of LP whose value sits ABOVE the position's
        cost basis (the original USD seed value, plus any later
        ``,group pool deposit`` top-ups). The principal stays in the
        pool and keeps earning swap fees + per-tick passive yield, so
        a successful harvest no longer zeroes the position. Cooldown:
        once per 24 hours per pool.

        (Fixed: prior versions burned the *entire* LP position, which
        sent ``lp_shares`` to 0 and silently disqualified the group
        from every future yield tick. cost basis bookkeeping in
        migration 0224 makes the fees-only path possible.)
        """
        parts = args.split()
        if len(parts) < 2:
            p = ctx.prefix or Config.PREFIX
            await ctx.reply_error(
                f"Usage: `{p}group pool harvest <TOKEN_A> <TOKEN_B>`"
            )
            return

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        my_grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not my_grp or my_grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can harvest LP earnings.")
            return

        pool_id, ca, cb = ctx.db.make_pool_id(parts[0], parts[1])
        lp_pos = await ctx.db.get_group_lp_position(ctx.guild_id, my_grp["group_id"], pool_id)
        if not lp_pos or int(lp_pos.get("lp_shares") or 0) <= 0:
            await ctx.reply_error(
                f"Your group has no LP position in **{ca}/{cb}**.\n"
                f"-# Use `{ctx.prefix}group pool deposit {ca} {cb} <USD>` "
                f"to seed (or restore) one from your group reserve."
            )
            return

        # Cooldown check
        cooldown_secs = Config.GROUP_POOL_HARVEST_COOLDOWN
        last_harvest_raw = lp_pos.get("last_harvest_at")
        if last_harvest_raw is not None:
            last_ts = (
                last_harvest_raw.timestamp()
                if hasattr(last_harvest_raw, "timestamp")
                else float(last_harvest_raw)
            )
            elapsed = time.time() - last_ts
            if elapsed < cooldown_secs:
                remaining = int(cooldown_secs - elapsed)
                await ctx.reply_cooldown(remaining)
                return

        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not pool:
            await ctx.reply_error(f"Pool **{ca}/{cb}** not found.")
            return

        # Convert proceeds to USD using current prices
        price_a = await _resolve_token_price(ctx.db, ca, ctx.guild_id)
        price_b = await _resolve_token_price(ctx.db, cb, ctx.guild_id)
        if price_a <= 0 or price_b <= 0:
            await ctx.reply_error(
                "Cannot harvest: token prices are not set. "
                "Use `,admin setprice` to configure prices first."
            )
            return

        # Pre-flight preview: compute current value & fees-only delta
        # so the confirmation shows what the player is about to claim.
        lp_shares_raw = int(lp_pos["lp_shares"])
        total_lp_raw = int(pool.get("total_lp") or 0)
        cost_basis_raw = int(lp_pos.get("cost_basis_usd_raw") or 0)
        share_a_h = _h(int(pool["reserve_a"])) * (lp_shares_raw / max(1, total_lp_raw))
        share_b_h = _h(int(pool["reserve_b"])) * (lp_shares_raw / max(1, total_lp_raw))
        current_value_usd = share_a_h * price_a + share_b_h * price_b
        cost_basis_usd = _h(cost_basis_raw)
        gain_usd = current_value_usd - cost_basis_usd
        if gain_usd <= 0:
            await ctx.reply_error(
                f"No LP fees accrued yet. Position value "
                f"`{fmt_usd(current_value_usd)}` is at-or-below cost basis "
                f"`{fmt_usd(cost_basis_usd)}`. Wait for trade volume on "
                f"**{ca}/{cb}** or buy/sell into the pool to seed fees."
            )
            return

        frac = gain_usd / current_value_usd if current_value_usd > 0 else 0.0
        preview_out_a = share_a_h * frac
        preview_out_b = share_b_h * frac

        embed = (
            card(f"Harvest LP fees - {ca}/{cb}", color=C_AMBER)
            .field(
                "Position",
                f"`{_h(lp_shares_raw):,.6f}` LP\n"
                f"≈ {fmt_usd(current_value_usd)} (cost basis "
                f"{fmt_usd(cost_basis_usd)})",
                False,
            )
            .field("Fees Accrued",     f"**{fmt_usd(gain_usd)}**",                              True)
            .field(f"Receive {ca}",    f"`{preview_out_a:,.6f} {ca}`  ({fmt_usd(preview_out_a * price_a)})",  True)
            .field(f"Receive {cb}",    f"`{preview_out_b:,.6f} {cb}`  ({fmt_usd(preview_out_b * price_b)})",  True)
            .footer(
                "Only fees above cost basis are claimed -- principal "
                "stays in the pool and keeps earning."
            )
            .build()
        )
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card("", description="Harvest cancelled.", color=C_NEUTRAL).build())
            return

        # Execute: remove fees-only fraction of LP, add USD to reserve.
        try:
            out_a, out_b, harvested_lp_raw, remaining_lp_raw = (
                await ctx.db.harvest_group_lp_fees_only(
                    ctx.guild_id, pool_id, my_grp["group_id"],
                    price_a, price_b,
                )
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if harvested_lp_raw <= 0:
            await ctx.reply_error(
                "No fees accrued between the preview and the confirmation. "
                "Try again in a moment."
            )
            return

        actual_usd = out_a * price_a + out_b * price_b
        await ctx.db.add_group_reserve_usd(ctx.guild_id, my_grp["group_id"], actual_usd)
        await ctx.db.set_group_lp_harvest_time(ctx.guild_id, my_grp["group_id"], pool_id)

        await ctx.reply_success(
            f"Harvested **{_h(harvested_lp_raw):,.6f} LP** in fees from "
            f"**{ca}/{cb}**.\n"
            f"Received: `{out_a:,.6f} {ca}` + `{out_b:,.6f} {cb}`\n"
            f"Added **{fmt_usd(actual_usd)}** to group reserve.\n"
            f"-# Principal preserved: `{_h(remaining_lp_raw):,.6f} LP` "
            f"still earning.",
            title="LP Fees Harvested",
        )

    async def _group_pool_deposit(self, ctx: DiscoContext, args: str) -> None:
        """Top up (or restore) a group LP position from the group's reserve_usd.

        Usage: .group pool deposit <TOKEN_A> <TOKEN_B> <USD>

        Pulls ``USD`` from ``reserve_usd``, splits it half-and-half
        across both sides of the ``TOKEN_A/TOKEN_B`` pool at the current
        oracle prices, mints LP at the pool's current ratio, and bumps
        the position's cost basis by the same USD amount so future
        ``,group pool harvest`` calls treat the new contribution as
        principal. Founder-only.

        This is also the recovery path for groups whose LP got nuked
        by the legacy "burn the whole position" harvest -- founders
        can re-seed straight from reserve_usd without re-running the
        partnership flow.
        """
        parts = args.split()
        if len(parts) < 3:
            p = ctx.prefix or Config.PREFIX
            await ctx.reply_error(
                f"Usage: `{p}group pool deposit <TOKEN_A> <TOKEN_B> <USD>`"
            )
            return
        try:
            usd_amount = float(parts[2].lstrip("$").replace(",", ""))
        except ValueError:
            await ctx.reply_error("USD amount must be numeric.")
            return
        if usd_amount <= 0:
            await ctx.reply_error("USD amount must be positive.")
            return

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return
        my_grp = await ctx.db.get_mining_group(
            ctx.guild_id, group_id=membership["group_id"],
        )
        if not my_grp or my_grp["founder_id"] != ctx.author.id:
            await ctx.reply_error(
                "Only the group founder can deposit into LP."
            )
            return
        reserve_usd = float(my_grp.get("reserve_usd") or 0.0)
        if reserve_usd < usd_amount:
            await ctx.reply_error(
                f"Group reserve only holds {fmt_usd(reserve_usd)} -- "
                f"can't pull {fmt_usd(usd_amount)}."
            )
            return

        pool_id, ca, cb = ctx.db.make_pool_id(parts[0], parts[1])
        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not pool:
            await ctx.reply_error(f"Pool **{ca}/{cb}** not found.")
            return
        price_a = await _resolve_token_price(ctx.db, ca, ctx.guild_id)
        price_b = await _resolve_token_price(ctx.db, cb, ctx.guild_id)
        if price_a <= 0 or price_b <= 0:
            await ctx.reply_error(
                "Both tokens need a price configured "
                "(use `,admin setprice`)."
            )
            return

        # Confirmation preview
        half = usd_amount / 2.0
        preview_a = half / price_a
        preview_b = half / price_b
        embed = (
            card(f"Deposit to LP - {ca}/{cb}", color=C_AMBER)
            .field("From Reserve",     f"**{fmt_usd(usd_amount)}**", False)
            .field(f"Add {ca}",        f"`{preview_a:,.6f} {ca}`", True)
            .field(f"Add {cb}",        f"`{preview_b:,.6f} {cb}`", True)
            .footer(
                "LP is minted at the current pool ratio. Cost basis "
                "bumps by the same USD amount, so a future harvest "
                "won't dip below this baseline."
            )
            .build()
        )
        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await msg.edit(view=None)
        if not confirmed:
            await msg.edit(embed=card(
                "", description="Deposit cancelled.", color=C_NEUTRAL,
            ).build())
            return

        # Pull from reserve up front so a failure mid-deposit returns
        # the funds rather than silently leaving the group short.
        await ctx.db.add_group_reserve_usd(
            ctx.guild_id, my_grp["group_id"], -usd_amount,
        )
        try:
            added_a, added_b, added_lp_raw = (
                await ctx.db.deposit_group_lp_from_reserve(
                    ctx.guild_id, pool_id, my_grp["group_id"],
                    usd_amount, price_a, price_b,
                )
            )
        except ValueError as exc:
            await ctx.db.add_group_reserve_usd(
                ctx.guild_id, my_grp["group_id"], usd_amount,
            )
            await ctx.reply_error(str(exc))
            return
        except Exception:
            await ctx.db.add_group_reserve_usd(
                ctx.guild_id, my_grp["group_id"], usd_amount,
            )
            log.exception(
                "group pool deposit failed gid=%s pool=%s grp=%s",
                ctx.guild_id, pool_id, my_grp["group_id"],
            )
            await ctx.reply_error(
                "Deposit failed. Reserve refunded."
            )
            return
        await ctx.reply_success(
            f"Added **`{added_a:,.6f} {ca}`** + **`{added_b:,.6f} {cb}`** "
            f"to **{ca}/{cb}**.\n"
            f"Minted `{_h(added_lp_raw):,.6f}` LP for **{my_grp['name']}**, "
            f"cost basis +{fmt_usd(usd_amount)}.\n"
            f"-# Pulled from reserve_usd. The position now earns swap "
            f"fees + per-tick yield until you harvest.",
            title="LP Deposited",
        )

    # ── $group kick ───────────────────────────────────────────────────────────

    @group.command(name="kick")
    @guild_only
    @no_bots
    @ensure_registered
    async def group_kick(self, ctx: DiscoContext, member: _MemberOrID) -> None:
        """Kick a member from your group (founder only). Works even if the
        user has left or been banned from the server.
        Usage: .group kick @member | .group kick <user_id>"""
        if member.id == ctx.author.id:
            await ctx.reply_error("You can't kick yourself.")
            return

        membership = await ctx.db.get_user_mining_group(ctx.author.id, ctx.guild_id)
        if not membership:
            await ctx.reply_error("You're not in a mining group.")
            return

        grp = await ctx.db.get_mining_group(ctx.guild_id, group_id=membership["group_id"])
        if not grp or grp["founder_id"] != ctx.author.id:
            await ctx.reply_error("Only the group founder can kick members.")
            return

        target_membership = await ctx.db.get_user_mining_group(member.id, ctx.guild_id)
        if not target_membership or target_membership["group_id"] != grp["group_id"]:
            await ctx.reply_error(f"{member.display_name} is not in your group.")
            return

        await ctx.db.kick_from_group(member.id, ctx.guild_id)
        await self._hall_remove_member(ctx.guild, grp, member)
        await ctx.reply_success(
            f"{member.mention} removed from **{grp['name']}**.",
            title="Member Kicked",
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Groups(bot))

    # Register persistent invite views so buttons survive bot restarts
    if getattr(bot, "db", None):
        try:
            invites = await bot.db.get_all_pending_invites()
            seen: set[tuple[int, str]] = set()
            for inv in invites:
                key = (inv["guild_id"], inv["group_id"])
                if key not in seen:
                    seen.add(key)
                    bot.add_view(GroupInviteView(inv["guild_id"], inv["group_id"], bot))
        except Exception:
            pass  # DB not ready yet; buttons will work on next invite

        try:
            proposals = await bot.db.get_all_pending_pool_proposals()
            for prop in proposals:
                bot.add_view(GroupPoolProposalView(prop["guild_id"], prop["id"], bot))
        except Exception:
            pass
