"""Player onboarding helpers.

Two surfaces:

* Welcome DM. The first time a user touches the bot in any guild, they
  receive a single DM that explains what the bot is, what messages it
  sends, how to disable each surface, and where to send feedback.
  Dedup is per user_id (not per guild_id) -- joining a second guild
  does not re-send. Backed by the ``welcomed_users`` table.

* First-time game hints. The first time a user opens a particular
  game / module, ``mark_module_seen`` returns ``True`` once. Cogs
  call ``maybe_send_intro`` from their entry command to show a one-
  shot intro card the first time -- "Disco speaking, here is how
  this surface works". Backed by ``user_module_seen``.

Both helpers are best-effort: a DB error or a Discord ``Forbidden``
(DMs disabled) never aborts the calling command. The welcome row is
stamped after a successful DM only, so a player whose DMs were closed
on first contact gets the message the next time they re-open commands
with DMs allowed.
"""
from __future__ import annotations

import logging
from typing import Any

import discord

from core.framework.embed import card
from core.framework.ui import C_INFO, C_NAVY

log = logging.getLogger(__name__)

# Discord user id of the bot's primary maintainer / contact for help.
SUPPORT_USER_ID = 801280612111482890


# ── DB helpers ────────────────────────────────────────────────────────────

async def was_welcomed(db: Any, user_id: int) -> bool:
    """Has this user already received the welcome DM?"""
    try:
        row = await db.fetch_val(
            "SELECT 1 FROM welcomed_users WHERE user_id = $1", int(user_id),
        )
    except Exception:
        log.debug("onboarding.was_welcomed failed", exc_info=True)
        return True  # fail closed: don't spam DMs on a DB hiccup
    return bool(row)


async def mark_welcomed(db: Any, user_id: int) -> None:
    """Stamp the welcomed-users row so we never DM this player again."""
    try:
        await db.execute(
            "INSERT INTO welcomed_users (user_id) VALUES ($1) "
            "ON CONFLICT (user_id) DO NOTHING",
            int(user_id),
        )
    except Exception:
        log.debug("onboarding.mark_welcomed failed", exc_info=True)


async def mark_module_seen(db: Any, user_id: int, module: str) -> bool:
    """Record that this user has touched ``module``. Returns ``True`` on
    the very first call for a (user, module) pair, ``False`` on every
    subsequent call. Used by cogs to gate one-shot intro embeds.
    """
    try:
        row = await db.fetch_one(
            "INSERT INTO user_module_seen (user_id, module) VALUES ($1, $2) "
            "ON CONFLICT (user_id, module) DO NOTHING "
            "RETURNING first_seen_at",
            int(user_id), str(module).lower(),
        )
    except Exception:
        log.debug("onboarding.mark_module_seen failed", exc_info=True)
        return False
    return row is not None


# ── Welcome DM content ───────────────────────────────────────────────────

def build_welcome_dm(
    member: discord.abc.User,
    guild: discord.Guild | None,
    prefix: str = ",",
) -> discord.Embed:
    """Construct the introductory DM embed.

    Plain prose, no roleplay, no AI affectations. Lists what the bot
    does, what messages it sends, how to silence each, and how to
    contact the maintainer.
    """
    where = guild.name if guild is not None else "a Discord server"
    name = getattr(member, "display_name", None) or getattr(member, "name", "there")

    overview = (
        f"Welcome to Discoin, {name}. You are receiving this message because "
        f"you have just interacted with the Discoin bot for the first time in "
        f"**{where}**. This is a one-time message and will not be sent again."
    )

    what_it_does = (
        f"Discoin is an in-server economy and minigame bot. From any channel "
        f"the bot is in, the prefix is `{prefix}` and slash commands work "
        f"where enabled. Major surfaces:\n"
        f"• **Economy** -- daily / work / streaks / banking / transfers.\n"
        f"• **Markets** -- token prices, trading, staking, LP, savings.\n"
        f"• **Minigames** -- farming, fishing, delve (dungeon), mining, "
        f"crafting, gambling, expeditions, buddies, auctions.\n"
        f"• **Items + NFTs** -- a per-unit item layer with auctioning, "
        f"transfer, and inventory.\n"
        f"• **AI chat** -- mention the bot or reply to a bot message and "
        f"Disco answers using your player context."
    )

    how_to_start = (
        f"Run `{prefix}start` to open the onboarding panel, or `{prefix}help` "
        f"for the full command index. The most common first commands are "
        f"`{prefix}daily`, `{prefix}balance`, `{prefix}fish`, `{prefix}farm`, "
        f"and `{prefix}delve`."
    )

    messages_sent = (
        f"By default the bot only sends in-channel replies to commands and "
        f"AI replies when you mention it or reply to one of its messages. "
        f"It can also send DMs for the following categories, opt-in or "
        f"toggleable:\n"
        f"• Mining / staking / validator / whale alerts.\n"
        f"• Transfer receipts and item level-ups.\n"
        f"• Market event notifications and price alerts.\n"
        f"• Eat the Rich notifications when another player tries to eat you."
    )

    how_to_disable = (
        f"You control every surface:\n"
        f"• `{prefix}notify` -- view + toggle every DM notification "
        f"category (mining, staking, transfer, validator, item level-up, "
        f"whale alerts).\n"
        f"• `{prefix}optout` -- stop the AI from learning anything about "
        f"you and clear what it has stored. `{prefix}optin` reverses it.\n"
        f"• Server admins can disable individual modules with "
        f"`{prefix}admin module <name> off`."
    )

    privacy = (
        "Discoin only stores data needed to run the game (your wallet, "
        "inventory, progression, command activity). Opting out of the AI "
        "deletes every memory and trait the bot has learned about you. "
        "This bot is provided as-is and is unaffiliated with any real "
        "currency, exchange, or financial service."
    )

    contact = (
        f"Questions, bugs, or suggestions: message <@{SUPPORT_USER_ID}> "
        f"directly, or use `{prefix}report` to file a structured ticket "
        f"from inside any server the bot is in."
    )

    embed = (
        card("Welcome to Discoin", color=C_NAVY)
        .description(overview)
        .field("What the bot does", what_it_does, False)
        .field("Getting started", how_to_start, False)
        .field("Messages the bot may send you", messages_sent, False)
        .field("How to disable any of it", how_to_disable, False)
        .field("Privacy + disclaimer", privacy, False)
        .field("Help and feedback", contact, False)
        .footer(f"Sent once on first interaction in {where}.")
        .build()
    )
    return embed


async def try_send_welcome_dm(
    bot: Any, user: discord.abc.User, guild: discord.Guild | None,
    prefix: str = ",",
) -> bool:
    """Send the welcome DM and stamp the row on success.

    Returns ``True`` if the DM was delivered (and the row stamped),
    ``False`` if the DM failed (Forbidden, HTTPException, etc.) -- in
    which case the row is NOT stamped, so a future first-interaction
    can retry once the user has DMs open.
    """
    if user is None or getattr(user, "bot", False):
        return False
    db = getattr(bot, "db", None)
    if db is None:
        return False
    if await was_welcomed(db, int(user.id)):
        return False
    embed = build_welcome_dm(user, guild, prefix=prefix)
    try:
        channel = user.dm_channel or await user.create_dm()
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.debug("welcome DM blocked by user %s (forbidden)", user.id)
        return False
    except Exception:
        log.debug("welcome DM send failed for %s", user.id, exc_info=True)
        return False
    await mark_welcomed(db, int(user.id))
    return True


# ── First-time game intros ───────────────────────────────────────────────

# Per-module intro copy. Keys must match what cogs pass to
# ``maybe_send_intro``. Voice is "Disco" (the in-bot AI persona) but
# stays informational, never roleplay-heavy. Prefix is interpolated at
# render time.
_INTRO_TEMPLATES: dict[str, dict[str, str]] = {
    "farming": {
        "title": "Disco -- first farm visit",
        "body": (
            "This is your farm. The plot grid grows whatever seed packets "
            "you plant; each crop has a grow timer that ticks even when you "
            "are offline. Buy seeds and fertilizer at `{p}farm shop`, plant "
            "with the seed dropdown or `{p}farm plant`, then water + "
            "fertilize to boost yield. Harvested crops sell for HRV at "
            "`{p}farm sell`, and harvests also drop SEED tokens. Hit "
            "Refresh on the panel any time you want fresh state."
        ),
    },
    "fishing": {
        "title": "Disco -- first cast",
        "body": (
            "Fishing has a rhythm: cast, wait for the hook prompt, hit "
            "HOOK, then REEL. Bigger / rarer fish weigh more and pay more. "
            "Equip bait at `{p}fish bait <key>` for higher rarity rolls, "
            "and stake LURE at `{p}fish stake` to passively earn while "
            "you fish. Sell your catch with `{p}fish sell`. The full menu "
            "is `{p}fish help`."
        ),
    },
    "delve": {
        "title": "Disco -- first descent",
        "body": (
            "The dungeon is a turn-based crawler. Pick a class with "
            "`{p}delve class <warrior|mage|rogue|archer|druid>` and start "
            "a run with `{p}delve start`. Each room is a fight, a chest, "
            "an ore vein, or an empty corridor; advance with `{p}delve "
            "next`, dive deeper with `{p}delve descend`. Mined ore burns "
            "into RUNE at `{p}delve swap`, and RUNE cashes out to USD. "
            "`{p}delve stats` shows your full sheet."
        ),
    },
    "mining": {
        "title": "Disco -- first mine",
        "body": (
            "Mining is a passive yield surface. You buy a hashstone with "
            "`{p}shop buy hashstone`, then it produces hashrate every "
            "tick which converts into the mined network's coin. Level up "
            "the stone with `{p}shop levelup hashstone` for more "
            "hashrate. `{p}mine` shows live yield + cashout."
        ),
    },
    "buddy": {
        "title": "Disco -- first buddy",
        "body": (
            "Buddies are pet companions that grant cross-game passive "
            "bonuses. Hatch one with `{p}buddy hatch`, talk / feed / pet "
            "to grow affinity, and battle / send on expeditions for XP "
            "and loot. Each buddy has a signature lane (fishing, "
            "farming, delve, etc) that buffs that game while it is your "
            "active buddy. `{p}buddy` opens the panel."
        ),
    },
    "gambling": {
        "title": "Disco -- first wager",
        "body": (
            "Casino games (`{p}slots`, `{p}roulette`, `{p}coinflip`, "
            "`{p}blackjack`, `{p}crash`, `{p}mines`, `{p}lottery`) are "
            "luck-based and fun-money only. House edge is real -- treat "
            "it as entertainment, not income. The economy never asks "
            "for real currency."
        ),
    },
    "trade": {
        "title": "Disco -- first trade",
        "body": (
            "Tokens trade against an oracle price that drifts with "
            "supply, demand, and live market events. `{p}trade buy "
            "<sym> <amt>` and `{p}trade sell <sym> <amt>` are the "
            "primitives; `{p}prices` shows live prices and "
            "`{p}chart <sym>` shows the candle chart."
        ),
    },
    "auction": {
        "title": "Disco -- first auction",
        "body": (
            "The Auction House lists every tradeable item / NFT / buddy "
            "in the bot. `{p}ah browse` flips through pages, `{p}ah list "
            "<item> <price>` posts your own listing, `{p}ah buy <id>` "
            "purchases. Listings escrow the item until sold or cancelled."
        ),
    },
    "expedition": {
        "title": "Disco -- first expedition",
        "body": (
            "Expeditions send a buddy on a multi-hour run to a "
            "destination. You pick the destination at `{p}expedition "
            "start <buddy_id> <destination>`; loot drops on return at "
            "`{p}expedition collect`. Buddies on a run cannot be fed, "
            "petted, or sent into combat until they get back."
        ),
    },
    "crafting": {
        "title": "Disco -- first craft",
        "body": (
            "Crafting combines materials into stronger gear, recipes, "
            "or buddy treats. `{p}craft list` is the recipe browser; "
            "`{p}craft make <recipe> [qty]` runs the recipe; "
            "`{p}craft apply <recipe>` applies a treat / buff to your "
            "active buddy."
        ),
    },
}


def _intro_embed(module: str, prefix: str = ",") -> discord.Embed | None:
    """Build the one-shot intro card for a module, or ``None`` if the
    module key isn't in ``_INTRO_TEMPLATES``.
    """
    tmpl = _INTRO_TEMPLATES.get(str(module).lower())
    if not tmpl:
        return None
    body = tmpl["body"].format(p=prefix)
    embed = (
        card(tmpl["title"], color=C_INFO)
        .description(body)
        .footer("This intro shows once per player. Hit ,help <topic> any time.")
        .build()
    )
    return embed


async def maybe_send_intro(ctx: Any, module: str) -> bool:
    """If this is the player's first interaction with ``module``, send
    a one-shot intro embed in-channel and stamp the seen-row. Safe to
    call from every entry command; later calls are a no-op.

    Returns ``True`` when an intro was posted, ``False`` otherwise.
    """
    db = getattr(ctx, "db", None)
    author = getattr(ctx, "author", None)
    if db is None or author is None:
        return False
    try:
        first_time = await mark_module_seen(db, int(author.id), module)
    except Exception:
        log.debug("onboarding.maybe_send_intro mark failed", exc_info=True)
        return False
    if not first_time:
        return False
    prefix = getattr(ctx, "prefix", None) or ","
    embed = _intro_embed(module, prefix=prefix)
    if embed is None:
        return False
    try:
        await ctx.send(embed=embed)
    except Exception:
        log.debug(
            "onboarding intro send failed user=%s module=%s",
            getattr(author, "id", "?"), module, exc_info=True,
        )
        return False
    return True


__all__ = [
    "SUPPORT_USER_ID",
    "build_welcome_dm",
    "mark_module_seen",
    "mark_welcomed",
    "maybe_send_intro",
    "try_send_welcome_dm",
    "was_welcomed",
]
