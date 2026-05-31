"""
cogs/governance.py  -  Discoin Governance voting system.

Voting power mirrors on-chain governance models (Compound/VTR/Cardano style):
  - 1 DSC = 1 vote, across ALL positions (CeFi + DeFi wallet + staked + delegated)
  - Quorum: total voted weight >= quorum_pct of DSC circulating supply at proposal time
  - Pass threshold: YES > threshold% of (YES + NO) weight -- abstain excluded from ratio

Commands (prefix-only, no slash):
    ,gov                     -  list active proposals
    ,gov info <id>           -  proposal detail + live tally
    ,gov vote <id> yes|no|abstain  -  cast or change your vote
    ,gov propose <hours> <title> | <description>  -  create proposal (GM/admin)
    ,gov tally <id>          -  finalize an ended proposal (GM/admin)
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only, no_bots, ensure_registered
from core.framework.cooldowns import user_cooldown
from core.framework.ui import (
    C_SUCCESS, C_ERROR, C_INFO, C_GOLD, C_NAVY,
    C_NEUTRAL, C_TEAL, FormatKit, fmt_token, fmt_ts,
)

log = logging.getLogger(__name__)

# Governance token for voting weight
_GOV_TOKEN = "DSC"

# ── Voting-power query ─────────────────────────────────────────────────────────
# Sums DSC across every position a user can hold, identical to how ADA/ARC
# governance counts tokens held in wallets, staking, and delegation.
_VOTING_POWER_SQL = """
SELECT
  COALESCE((
      SELECT SUM(amount) FROM crypto_holdings
      WHERE user_id = $1 AND guild_id = $2 AND symbol = 'DSC'
  ), 0)
  + COALESCE((
      SELECT SUM(amount) FROM wallet_holdings
      WHERE user_id = $1 AND guild_id = $2 AND symbol = 'DSC' AND network = 'dsc'
  ), 0)
  + COALESCE((
      SELECT SUM(amount) FROM stakes
      WHERE user_id = $1 AND guild_id = $2 AND symbol = 'DSC'
  ), 0)
  + COALESCE((
      SELECT SUM(stake_amount) FROM pos_validators
      WHERE user_id = $1 AND guild_id = $2 AND stake_token = 'DSC'
  ), 0)
  + COALESCE((
      SELECT SUM(amount) FROM pos_delegations
      WHERE delegator_id = $1 AND guild_id = $2 AND token = 'DSC'
  ), 0)
  AS voting_power
"""


async def _get_voting_power(db, uid: int, gid: int) -> float:
    val = await db.fetch_val(_VOTING_POWER_SQL, uid, gid)
    return float(val or 0.0)


async def _is_gm_or_admin(ctx: DiscoContext) -> bool:
    """Return True if the invoker is a guild admin or a registered game helper."""
    if ctx.author.guild_permissions.manage_guild:
        return True
    row = await ctx.db.fetch_one(
        "SELECT 1 FROM game_helpers WHERE guild_id = $1 AND user_id = $2",
        ctx.guild_id, ctx.author.id,
    )
    return row is not None


async def _tally_proposal(db, prop: dict) -> dict:
    """Aggregate vote rows for a proposal and return a tally summary dict."""
    rows = await db.fetch_all(
        "SELECT vote, SUM(voting_power) AS weight, COUNT(*) AS cnt "
        "FROM governance_votes WHERE proposal_id = $1 GROUP BY vote",
        prop["id"],
    )
    tally = {"yes": 0.0, "no": 0.0, "abstain": 0.0, "voters": 0}
    for r in rows:
        tally[r["vote"]] = float(r["weight"])
        tally["voters"] += int(r["cnt"])

    yes, no = tally["yes"], tally["no"]
    yn_total = yes + no
    tally["yes_pct"] = (yes / yn_total * 100) if yn_total > 0 else 0.0
    tally["no_pct"] = (no / yn_total * 100) if yn_total > 0 else 0.0

    total_voted = yes + no + tally["abstain"]
    supply = float(prop["supply_snapshot"])
    tally["quorum_pct_reached"] = (total_voted / supply * 100) if supply > 0 else 0.0
    tally["total_voted"] = total_voted
    tally["quorum_target"] = float(prop["quorum_pct"])
    tally["pass_threshold"] = float(prop["pass_threshold"])
    tally["quorum_met"] = tally["quorum_pct_reached"] >= tally["quorum_target"]
    tally["passed"] = tally["quorum_met"] and tally["yes_pct"] >= tally["pass_threshold"]
    return tally


def _proposal_status_color(status: str) -> int:
    return {
        "active": C_INFO,
        "passed": C_SUCCESS,
        "failed": C_ERROR,
        "cancelled": C_NEUTRAL,
    }.get(status, C_NAVY)


def _status_label(status: str) -> str:
    return {
        "active": "Active",
        "passed": "Passed",
        "failed": "Failed",
        "cancelled": "Cancelled",
    }.get(status, status.title())


async def _notify_new_proposal(bot: Discoin, guild: discord.Guild, prop_id: int, title: str, description: str, hours: int) -> None:
    """DM all DSC holders in the guild about a new governance proposal."""
    try:
        rows = await bot.db.fetch_all(
            """
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM crypto_holdings
                WHERE guild_id=$1 AND symbol='DSC' AND amount > 0
                UNION
                SELECT user_id FROM wallet_holdings
                WHERE guild_id=$1 AND symbol='DSC' AND network='dsc' AND amount > 0
                UNION
                SELECT user_id FROM stakes
                WHERE guild_id=$1 AND symbol='DSC' AND amount > 0
                UNION
                SELECT user_id FROM pos_validators
                WHERE guild_id=$1 AND stake_token='DSC' AND stake_amount > 0
                UNION
                SELECT delegator_id AS user_id FROM pos_delegations
                WHERE guild_id=$1 AND token='DSC' AND amount > 0
            ) t
            """,
            guild.id,
        )
        for row in rows:
            try:
                user = bot.get_user(row["user_id"]) or await bot.fetch_user(row["user_id"])
                if not user or user.bot:
                    continue
                embed = (
                    card(
                        "🗳 New Governance Proposal",
                        description=(
                            f"**#{prop_id}: {title}**\n\n"
                            f"{description}\n\n"
                            f"Voting open for **{hours}h** in **{guild.name}**."
                        ),
                        color=C_GOLD,
                    )
                    .footer(f"Vote with: ,gov vote {prop_id} yes/no/abstain")
                    .build()
                )
                await user.send(embed=embed)
            except Exception:
                pass
    except Exception as exc:
        log.warning("gov proposal DM notify failed for guild %d: %s", guild.id, exc)


async def _notify_proposal_outcome(bot: Discoin, guild: discord.Guild, prop: dict, tally: dict) -> None:
    """DM all voters on a finalized proposal with the outcome."""
    try:
        rows = await bot.db.fetch_all(
            "SELECT user_id, vote FROM governance_votes WHERE proposal_id = $1",
            prop["id"],
        )
        status = prop["status"]
        color = C_SUCCESS if status == "passed" else C_ERROR
        outcome_label = "PASSED" if status == "passed" else "FAILED"
        for row in rows:
            try:
                user = bot.get_user(row["user_id"]) or await bot.fetch_user(row["user_id"])
                if not user or user.bot:
                    continue
                icon = {"yes": "✅", "no": "❌", "abstain": "⬜"}.get(row["vote"], "")
                embed = (
                    card(
                        f"🗳 Proposal #{prop['id']}: {outcome_label}",
                        description=(
                            f"**{prop['title']}**\n\n"
                            f"Result: **{outcome_label}**\n"
                            f"Your vote: {icon} **{row['vote'].title()}**\n\n"
                            f"✅ Yes: {fmt_token(tally['yes'], _GOV_TOKEN)} ({tally['yes_pct']:.1f}%)\n"
                            f"❌ No: {fmt_token(tally['no'], _GOV_TOKEN)} ({tally['no_pct']:.1f}%)\n"
                            f"Total voters: **{tally['voters']}**"
                        ),
                        color=color,
                    )
                    .footer(f"Server: {guild.name}")
                    .build()
                )
                await user.send(embed=embed)
            except Exception:
                pass
    except Exception as exc:
        log.warning("gov outcome DM notify failed for guild %d: %s", guild.id, exc)


def _build_info_embed(prop: dict, tally: dict, author_vote: str | None = None) -> discord.Embed:
    """Build the detailed proposal embed with live vote breakdown."""
    status = prop["status"]
    color = _proposal_status_color(status)
    ends_label = "Ended" if status != "active" else "Ends"

    yes_bar = FormatKit.bar(tally["yes"], tally["yes"] + tally["no"], width=12)
    quorum_bar = FormatKit.bar(
        tally["quorum_pct_reached"], tally["quorum_target"], width=12, show_pct=False
    )
    quorum_check = "Reached" if tally["quorum_met"] else f"Need {tally['quorum_target']:.1f}%"

    your_vote_line = ""
    if author_vote:
        icon = {"yes": "✅", "no": "❌", "abstain": "⬜"}.get(author_vote, "")
        your_vote_line = f"\n\nYour vote: {icon} **{author_vote.title()}**"

    b = (
        card(
            f"Proposal #{prop['id']}: {prop['title']}",
            color=color,
        )
        .description(
            f"{prop['description']}"
            f"{your_vote_line}"
        )
        .field(
            "Status",
            f"**{_status_label(status)}**  |  "
            f"{ends_label}: {fmt_ts(prop['ends_at'])}",
            inline=False,
        )
        .field(
            "Vote Breakdown",
            (
                f"✅ **Yes** -- {fmt_token(tally['yes'], _GOV_TOKEN)}  ({tally['yes_pct']:.1f}%)\n"
                f"❌ **No** -- {fmt_token(tally['no'], _GOV_TOKEN)}  ({tally['no_pct']:.1f}%)\n"
                f"⬜ **Abstain** -- {fmt_token(tally['abstain'], _GOV_TOKEN)}\n"
                f"\n{yes_bar}"
            ),
            inline=False,
        )
        .field(
            "Quorum",
            (
                f"{quorum_bar}  {tally['quorum_pct_reached']:.2f}% / {tally['quorum_target']:.1f}%  ({quorum_check})\n"
                f"Total voted: {fmt_token(tally['total_voted'], _GOV_TOKEN)}  "
                f"| Supply snapshot: {fmt_token(float(prop['supply_snapshot']), _GOV_TOKEN)}\n"
                f"Unique voters: **{tally['voters']}**"
            ),
            inline=False,
        )
    )

    if status == "active":
        b.footer(
            f"Pass threshold: {tally['pass_threshold']:.0f}% of YES+NO weight  "
            f"|  Vote with ,gov vote {prop['id']} yes/no/abstain"
        )
    else:
        outcome = "Proposal passed." if status == "passed" else (
            "Proposal failed." if status == "failed" else "Proposal cancelled."
        )
        b.footer(outcome)

    return b.build()


class Governance(commands.Cog):
    """Discoin on-chain style governance -- vote with your DSC holdings."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ── ,gov (list) ───────────────────────────────────────────────────────────

    @commands.group(name="gov", aliases=["governance", "vote"], invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def gov(self, ctx: DiscoContext) -> None:
        """List active governance proposals. Use ,gov help for subcommands."""
        rows = await ctx.db.fetch_all(
            "SELECT id, title, ends_at, status FROM governance_proposals "
            "WHERE guild_id = $1 AND status = 'active' ORDER BY ends_at ASC",
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply(
                embed=card(
                    "Governance",
                    description=(
                        "No active proposals right now.\n\n"
                        "Game helpers and admins can create one with:\n"
                        f"`{ctx.prefix or ','}gov propose <hours> Title | Description`"
                    ),
                    color=C_NAVY,
                ).build(),
                mention_author=False,
            )
            return

        lines = []
        for r in rows:
            lines.append(
                f"**#{r['id']}** {r['title']}\n"
                f"  Ends: {fmt_ts(r['ends_at'])}"
            )

        await ctx.reply(
            embed=card(
                f"Active Proposals ({len(rows)})",
                description="\n\n".join(lines),
                color=C_INFO,
            )
            .footer("Use ,gov info <id> to see details and cast your vote.")
            .build(),
            mention_author=False,
        )

    # ── ,gov info ─────────────────────────────────────────────────────────────

    @gov.command(name="info", aliases=["view", "show"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gov_info(self, ctx: DiscoContext, proposal_id: int) -> None:
        """Show full details and live tally for a proposal."""
        prop = await ctx.db.fetch_one(
            "SELECT * FROM governance_proposals WHERE id = $1 AND guild_id = $2",
            proposal_id, ctx.guild_id,
        )
        if not prop:
            await ctx.reply_error(f"Proposal #{proposal_id} not found.")
            return

        # Auto-expire: if active but past ends_at, resolve it now
        if prop["status"] == "active":
            still_active = await ctx.db.fetch_val(
                "SELECT EXTRACT(EPOCH FROM (ends_at - NOW())) > 0 "
                "FROM governance_proposals WHERE id = $1",
                prop["id"],
            )
            if not still_active:
                tally = await _tally_proposal(ctx.db, prop)
                new_status = "passed" if tally["passed"] else "failed"
                await ctx.db.execute(
                    "UPDATE governance_proposals SET status = $1 WHERE id = $2",
                    new_status, prop["id"],
                )
                tally_for_dm = await _tally_proposal(ctx.db, prop)
                prop = dict(prop)
                prop["status"] = new_status
                log.info(
                    "Auto-resolved proposal #%d as %s for guild %d",
                    prop["id"], new_status, ctx.guild_id,
                )
                asyncio.create_task(
                    _notify_proposal_outcome(self.bot, ctx.guild, prop, tally_for_dm)
                )

        tally = await _tally_proposal(ctx.db, prop)

        # Fetch the invoker's vote if any
        vote_row = await ctx.db.fetch_one(
            "SELECT vote FROM governance_votes WHERE proposal_id = $1 AND user_id = $2",
            prop["id"], ctx.author.id,
        )
        author_vote = vote_row["vote"] if vote_row else None

        embed = _build_info_embed(prop, tally, author_vote)
        await ctx.reply(embed=embed, mention_author=False)

    # ── ,gov vote ─────────────────────────────────────────────────────────────

    @gov.command(name="vote", aliases=["cast"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def gov_vote(self, ctx: DiscoContext, proposal_id: int, choice: str) -> None:
        """Cast your vote on a proposal. Weight = your total DSC holdings.

        Usage: ,gov vote <id> yes|no|abstain
        """
        choice = choice.lower().strip()
        if choice not in ("yes", "no", "abstain", "y", "n", "a"):
            await ctx.reply_error(
                "Invalid vote. Use: `,gov vote <id> yes`, `no`, or `abstain`"
            )
            return
        # Normalise short forms
        choice = {"y": "yes", "n": "no", "a": "abstain"}.get(choice, choice)

        prop = await ctx.db.fetch_one(
            "SELECT * FROM governance_proposals WHERE id = $1 AND guild_id = $2",
            proposal_id, ctx.guild_id,
        )
        if not prop:
            await ctx.reply_error(f"Proposal #{proposal_id} not found.")
            return

        if prop["status"] != "active":
            await ctx.reply_error(
                f"Proposal #{proposal_id} is already **{_status_label(prop['status'])}** "
                "and no longer accepting votes."
            )
            return

        # Check if voting period is still open (DB-side clock)
        still_active = await ctx.db.fetch_val(
            "SELECT EXTRACT(EPOCH FROM (ends_at - NOW())) > 0 "
            "FROM governance_proposals WHERE id = $1",
            prop["id"],
        )
        if not still_active:
            await ctx.reply_error(
                f"Proposal #{proposal_id} voting period has ended. "
                f"Run `,gov tally {proposal_id}` to finalize it."
            )
            return

        # Voting power = all DSC positions (CeFi + DeFi + staked + delegated)
        power = await _get_voting_power(ctx.db, ctx.author.id, ctx.guild_id)
        if power < 0.000001:
            await ctx.reply_error(
                f"You need at least some **{_GOV_TOKEN}** to participate in governance.\n"
                f"Acquire DSC via trading or staking to gain voting power."
            )
            return

        # Upsert: allow changing vote
        existing = await ctx.db.fetch_one(
            "SELECT vote FROM governance_votes WHERE proposal_id = $1 AND user_id = $2",
            prop["id"], ctx.author.id,
        )
        await ctx.db.execute(
            """
            INSERT INTO governance_votes (proposal_id, user_id, vote, voting_power)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (proposal_id, user_id)
            DO UPDATE SET vote = EXCLUDED.vote,
                          voting_power = EXCLUDED.voting_power,
                          voted_at = NOW()
            """,
            prop["id"], ctx.author.id, choice, power,
        )

        icon = {"yes": "✅", "no": "❌", "abstain": "⬜"}[choice]
        changed_note = (
            f" (changed from **{existing['vote'].title()}**)" if existing else ""
        )

        await ctx.reply(
            embed=card(
                f"{icon} Vote Recorded",
                description=(
                    f"Your vote on **Proposal #{prop['id']}: {prop['title']}**\n\n"
                    f"Choice: **{choice.title()}**{changed_note}\n"
                    f"Voting power: **{fmt_token(power, _GOV_TOKEN)}**\n\n"
                    f"Run `,gov info {prop['id']}` to see the live tally."
                ),
                color=C_TEAL,
            ).build(),
            mention_author=False,
        )
        log.info(
            "gov_vote: uid=%d gid=%d proposal=%d choice=%s power=%.4f",
            ctx.author.id, ctx.guild_id, prop["id"], choice, power,
        )

    # ── ,gov propose ─────────────────────────────────────────────────────────

    @gov.command(name="propose", aliases=["create", "new"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gov_propose(self, ctx: DiscoContext, hours: int, *, text: str) -> None:
        """Create a governance proposal. GM/admin only.

        Usage: ,gov propose <hours> Title | Description of the proposal
        The pipe character separates title from description.
        """
        if not await _is_gm_or_admin(ctx):
            await ctx.reply_error("Only **GMs** and server admins can create proposals.")
            return

        if hours < 1 or hours > 336:  # 1 hour to 2 weeks
            await ctx.reply_error("Voting duration must be between 1 and 336 hours (2 weeks).")
            return

        if "|" not in text:
            await ctx.reply_error(
                "Separate title and description with `|`.\n"
                "Example: `,gov propose 48 Lower burn rate | Reduce DSC burn from 2% to 1%`"
            )
            return

        parts = text.split("|", 1)
        title = parts[0].strip()
        description = parts[1].strip()

        if not title or not description:
            await ctx.reply_error("Both title and description are required.")
            return

        if len(title) > 100:
            await ctx.reply_error("Title must be 100 characters or fewer.")
            return

        if len(description) > 900:
            await ctx.reply_error("Description must be 900 characters or fewer.")
            return

        # Snapshot the current DSC circulating supply for quorum calculation
        supply_row = await ctx.db.fetch_one(
            "SELECT circulating_supply FROM crypto_prices "
            "WHERE symbol = 'DSC' AND guild_id = $1",
            ctx.guild_id,
        )
        circulating = supply_row["circulating_supply"] if supply_row else None
        if not circulating or float(circulating) <= 0:
            await ctx.reply_error(
                "Cannot create a proposal right now - the DSC circulating supply snapshot "
                "is unavailable. Ask an admin to update token supply data and try again."
            )
            return
        supply_snapshot = float(circulating)

        prop_id = await ctx.db.fetch_val(
            """
            INSERT INTO governance_proposals
                (guild_id, title, description, created_by, ends_at, supply_snapshot)
            VALUES
                ($1, $2, $3, $4, NOW() + ($5 || ' hours')::INTERVAL, $6)
            RETURNING id
            """,
            ctx.guild_id, title, description, ctx.author.id, str(hours), supply_snapshot,
        )

        await ctx.reply(
            embed=card(
                "Proposal Created",
                description=(
                    f"**#{prop_id}: {title}**\n\n"
                    f"{description}\n\n"
                    f"Voting open for **{hours}h**  |  "
                    f"Supply snapshot: {fmt_token(supply_snapshot, _GOV_TOKEN)}\n"
                    f"Quorum required: **5%** of snapshot  |  Pass threshold: **51%** of YES+NO"
                ),
                color=C_GOLD,
            )
            .footer(f"Players vote with: ,gov vote {prop_id} yes/no/abstain")
            .build(),
            mention_author=False,
        )
        log.info(
            "gov_propose: uid=%d gid=%d proposal=%d hours=%d title=%r",
            ctx.author.id, ctx.guild_id, prop_id, hours, title,
        )
        # DM all DSC holders about the new proposal (fire and forget)
        asyncio.create_task(
            _notify_new_proposal(self.bot, ctx.guild, prop_id, title, description, hours)
        )

    # ── ,gov tally ────────────────────────────────────────────────────────────

    @gov.command(name="tally", aliases=["close", "finalize"])
    @guild_only
    @no_bots
    @ensure_registered
    async def gov_tally(self, ctx: DiscoContext, proposal_id: int) -> None:
        """Finalize an ended proposal and record its outcome. GM/admin only."""
        if not await _is_gm_or_admin(ctx):
            await ctx.reply_error("You need **DRS Terminal** or admin status to finalize proposals.")
            return

        prop = await ctx.db.fetch_one(
            "SELECT * FROM governance_proposals WHERE id = $1 AND guild_id = $2",
            proposal_id, ctx.guild_id,
        )
        if not prop:
            await ctx.reply_error(f"Proposal #{proposal_id} not found.")
            return

        if prop["status"] != "active":
            await ctx.reply(
                embed=card(
                    f"Proposal #{proposal_id}",
                    description=f"Already finalized as **{_status_label(prop['status'])}**.",
                    color=_proposal_status_color(prop["status"]),
                ).build(),
                mention_author=False,
            )
            return

        # Require voting period to have ended (DB-side clock)
        still_active = await ctx.db.fetch_val(
            "SELECT EXTRACT(EPOCH FROM (ends_at - NOW())) > 0 "
            "FROM governance_proposals WHERE id = $1",
            prop["id"],
        )
        if still_active:
            secs_left = await ctx.db.fetch_val(
                "SELECT EXTRACT(EPOCH FROM (ends_at - NOW()))::BIGINT "
                "FROM governance_proposals WHERE id = $1",
                prop["id"],
            )
            h, rem = divmod(int(secs_left), 3600)
            m = rem // 60
            await ctx.reply_error(
                f"Voting is still open for **{h}h {m}m**. "
                "Wait until it expires to tally."
            )
            return

        tally = await _tally_proposal(ctx.db, prop)
        new_status = "passed" if tally["passed"] else "failed"

        await ctx.db.execute(
            "UPDATE governance_proposals SET status = $1 WHERE id = $2",
            new_status, prop["id"],
        )

        prop = dict(prop)
        prop["status"] = new_status
        embed = _build_info_embed(prop, tally)
        await ctx.reply(embed=embed, mention_author=False)
        log.info(
            "gov_tally: uid=%d gid=%d proposal=%d outcome=%s",
            ctx.author.id, ctx.guild_id, prop["id"], new_status,
        )
        # DM all voters with the outcome (fire and forget)
        asyncio.create_task(
            _notify_proposal_outcome(self.bot, ctx.guild, prop, tally)
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Governance(bot))
