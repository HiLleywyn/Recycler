"""services/ai_context.py  -  Single source of truth for AI prompt context.

Before this module existed, cogs/help.py contained four near-identical copies
of the system-prompt assembly (``ask_cmd``, ``handle_ai_reply``,
``handle_ai_mention``, ``handle_ai_ambient``) -- ~700 lines of duplicated DB
fan-out and string concatenation. Touching one and forgetting the others is
how production drifted: the mention handler quietly added a scam-log block
the other paths never saw, the ambient handler shipped without facts, and
nobody could answer "what does the model actually see for this server?"
without reading every branch.

This module replaces all of that with two pieces:

* :class:`ChatContext` -- a typed snapshot of every piece of context the AI
  may receive for one turn. Built once from the live message + DB state.
* :func:`gather_chat_context` -- one async function that fans out every DB /
  Redis / Discord lookup in parallel and returns a ``ChatContext``.
* :func:`build_chat_system_prompt` -- the single composer that turns a
  ``ChatContext`` into the final system-prompt string. The four handlers
  above all call this; behaviour differences come from the ``mode`` flag
  rather than from copy-pasted branches.

When a feature needs a new piece of context, it is added in ONE place here
and every entry point picks it up automatically. New per-channel personas,
time-of-day awareness, server vibe, market-regime hints, recent personal
wins/losses, member-role hints, etc., all live below.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import enum
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.config import Config
from core.framework.ai import sanitize_context_snippet
from core.framework.emoji_context import build_guild_emoji_context
from core.framework.ui import fmt_ts
from services.ai_easter_eggs import addendum_for_mentions, addendum_for_speaker
from services.ai_lexicon import build_lexicon
from services.ai_agents import build_tool_context, detect_tools

if TYPE_CHECKING:
    import discord
    from services.ai_agents import ToolDef

log = logging.getLogger(__name__)


# ── Mode -------------------------------------------------------------------

class ChatMode(str, enum.Enum):
    """Why the AI is being invoked. Drives prompt sections.

    * ``ASK``     - explicit ``,ask`` command. Game-aware when keywords match.
    * ``REPLY``   - user replied to a previous AI message. Same as ASK.
    * ``MENTION`` - user @mentioned the bot. Same as ASK plus scam-log block.
    * ``AMBIENT`` - bot proactively decides to chime in. Game lore is
      forbidden; SKIP-token escape hatch is added.
    """

    ASK = "ask"
    REPLY = "reply"
    MENTION = "mention"
    AMBIENT = "ambient"


# ── Channel persona inference ---------------------------------------------
# Names + topics use these substrings to nudge the model toward a tone even
# when the channel is not on either explicit allowlist. Plain ASCII keywords
# only -- regex word-boundaries handle the rest.
_PERSONA_KEYWORDS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(serious|support|help-?desk|tickets?|appeals?)\b", re.I),
     "calm-helper"),
    (re.compile(r"\b(announc|news|broadcast|updates?)\b", re.I),
     "low-key"),
    (re.compile(r"\b(off-?topic|ot|lounge|hangout|chill|cafe)\b", re.I),
     "loose-lounge"),
    (re.compile(r"\b(degens?|gambling|casino|slot|dice|coinflip)\b", re.I),
     "degen"),
    (re.compile(r"\b(market|trad(e|ing)|alpha|chart|signals?)\b", re.I),
     "market"),
    (re.compile(r"\b(memes?|shitpost|cope|copium|salt)\b", re.I),
     "shitpost"),
    (re.compile(r"\b(dev|engineering|api|backend|infra)\b", re.I),
     "tech"),
)

# Persona key -> appended hint. Kept short; the base prompt does the heavy
# lifting. Hints REINFORCE existing tone rather than replacing them.
_PERSONA_HINTS: dict[str, str] = {
    "calm-helper": (
        "CHANNEL VIBE: support / help room. Drop the dryness, dial the "
        "snark to zero, answer plainly. Real questions deserve real answers "
        "here -- save the deadpan for the lounge."
    ),
    "low-key": (
        "CHANNEL VIBE: announcements / news room. People skim here; reply "
        "in one short sentence unless asked for more."
    ),
    "loose-lounge": (
        "CHANNEL VIBE: lounge / off-topic. Loose, conversational, react to "
        "the actual vibe. Game pitches are unwelcome unless someone brings it up."
    ),
    "degen": (
        "CHANNEL VIBE: degen / gambling. Match the energy without going "
        "feral. Light ribbing for bad calls, real props for big wins, "
        "never pretend to be financial advice."
    ),
    "market": (
        "CHANNEL VIBE: markets / trading. Concrete, numerate, dry. Quote "
        "specific numbers when you have them; admit uncertainty when you don't."
    ),
    "shitpost": (
        "CHANNEL VIBE: meme / shitpost. Punchier replies, short jokes, "
        "match the bit if there is one. Don't explain the joke."
    ),
    "tech": (
        "CHANNEL VIBE: dev / tech. Be precise and concise. Code blocks for "
        "code, plain prose for everything else. Don't bro-speak the engineers."
    ),
}


def _detect_channel_persona(channel: "discord.abc.GuildChannel | None") -> str | None:
    """Return a persona key from channel name + topic, or None."""
    if channel is None:
        return None
    haystack_parts: list[str] = []
    name = getattr(channel, "name", None) or ""
    if name:
        haystack_parts.append(name)
    topic = getattr(channel, "topic", None) or ""
    if topic:
        haystack_parts.append(topic)
    if not haystack_parts:
        return None
    haystack = " ".join(haystack_parts)
    for pattern, key in _PERSONA_KEYWORDS:
        if pattern.search(haystack):
            return key
    return None


def _channel_topic_block(channel: "discord.abc.GuildChannel | None") -> str:
    """Surface the Discord channel topic so the model knows the room's purpose.

    Channel topics are the maintainer's explicit "what this room is for"
    note; ignoring them is why the model used to pitch shop strategy in
    #announcements. Capped at 240 chars and sanitised through the standard
    snippet scrubber so a hostile topic cannot smuggle instructions.
    """
    if channel is None:
        return ""
    topic = getattr(channel, "topic", None)
    if not topic:
        return ""
    safe = sanitize_context_snippet(str(topic), limit=240)
    if not safe:
        return ""
    return f"CHANNEL TOPIC (set by mods, treat as the room's stated purpose): {safe}"


def _time_of_day_block(now: _dt.datetime) -> str:
    """Tell the model the real-world date and time so it stops hallucinating.

    Without this block the model has no clock at all: it greets ``gm`` at 3am,
    calls noon "late night", and -- worst of all -- answers "what's the date?"
    from training data, which is months or years stale. We give it the full
    wall-clock here (weekday, full date, time, vibe slot) and label this line
    as the source of truth so the model quotes it instead of guessing. UTC
    because every guild we serve runs on UTC for events / market ticks.
    """
    hour = now.hour
    if 5 <= hour < 11:
        slot = "morning"
    elif 11 <= hour < 17:
        slot = "afternoon"
    elif 17 <= hour < 22:
        slot = "evening"
    elif 22 <= hour < 24:
        slot = "late-night"
    else:
        slot = "graveyard-shift"
    full_ts = now.strftime("%A, %B %d %Y, %H:%M UTC")
    iso_date = now.strftime("%Y-%m-%d")
    return (
        f"CURRENT DATE AND TIME (source of truth, quote from this when asked): "
        f"{full_ts} ({slot}; ISO {iso_date}). When the user asks the date, the "
        f"day, the time, the year, or anything like 'what day is it', answer "
        f"from THIS line -- never guess from your training data, never claim "
        f"you don't know. Match the vibe too: gm at 3am UTC is suspicious, gn "
        f"at noon is a joke, and a graveyard-shift channel is usually quieter "
        f"than peak hours."
    )


# ── Server vibe ------------------------------------------------------------

def _classify_market_regime(prices: list[dict]) -> str | None:
    """Bull / bear / crab call from the most recent price snapshot.

    The bot's price oracle stores ``price`` and ``prev_price`` (24h) on the
    ``token_prices`` rows fetched by ``get_all_prices``. We average the
    percent move across non-stable tokens to pick a regime label. Pure
    heuristic -- the model gets it as a hint, not a fact.
    """
    if not prices:
        return None
    moves: list[float] = []
    for row in prices:
        sym = (row.get("symbol") or "").upper()
        if sym in ("USDC", "DSD", "USDT", "DAI"):
            continue
        try:
            cur = float(row.get("price") or 0.0)
            prev = float(row.get("prev_price") or row.get("price_24h_ago") or 0.0)
        except (TypeError, ValueError):
            continue
        if prev <= 0 or cur <= 0:
            continue
        moves.append((cur - prev) / prev)
    if not moves:
        return None
    avg = sum(moves) / len(moves)
    if avg > 0.05:
        return f"BULL (avg +{avg * 100:.1f}% across {len(moves)} tokens)"
    if avg < -0.05:
        return f"BEAR (avg {avg * 100:.1f}% across {len(moves)} tokens)"
    return f"CRAB (avg {avg * 100:+.1f}% across {len(moves)} tokens)"


async def _fetch_active_event_summary(bot, guild_id: int) -> str:
    """One-line summary of the active market event, if any."""
    redis = None
    bus = getattr(bot, "bus", None)
    if bus is not None:
        redis = getattr(bus, "_redis", None)
    if redis is None:
        return ""
    try:
        from services.market_event_engine import (
            get_active_event,
            get_current_phase,
        )
    except Exception:
        return ""
    try:
        ae = await get_active_event(redis, guild_id)
    except Exception:
        return ""
    if ae is None:
        return ""
    phase = None
    try:
        phase = get_current_phase(ae)
    except Exception:
        pass
    label = getattr(ae, "name", None) or getattr(ae, "event_id", None) or "active event"
    phase_name = getattr(phase, "name", None) or "in progress"
    return f"{label} -- {phase_name}"


def _channel_temperature(channel_ctx_rows: list[dict]) -> str | None:
    """Quick read of how hot the channel is right now.

    ``channel_ctx_rows`` is the recent reactions/edits/banter feed. Counts
    of reactions vs deletes vs plain banter line up with how spicy the
    moment is, so we surface a single-word vibe label so the model can
    match energy without re-reading every line.
    """
    if not channel_ctx_rows:
        return None
    reactions = 0
    edits = 0
    deletes = 0
    banter = 0
    for row in channel_ctx_rows:
        et = (row.get("event_type") or "").lower()
        if et in ("reaction", "reaction_add"):
            reactions += 1
        elif et in ("edit", "message_edit"):
            edits += 1
        elif et in ("delete", "message_delete"):
            deletes += 1
        else:
            banter += 1
    total = reactions + edits + deletes + banter
    if total == 0:
        return None
    if reactions / max(total, 1) > 0.4 and reactions >= 6:
        return "HYPED (lots of reactions firing)"
    if deletes >= 3:
        return "SPICY (people deleting messages)"
    if total <= 3:
        return "QUIET (room is sleepy)"
    if banter / max(total, 1) > 0.6:
        return "CHATTY (active banter)"
    return None


# ── Member role hint -------------------------------------------------------

# Role keywords that change tone. Mapped to short labels passed to the model.
_ROLE_HINTS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(founder|owner|core|admin)\b", re.I), "staff"),
    (re.compile(r"\b(mod(erator)?|helper)\b", re.I), "mod"),
    (re.compile(r"\b(og|early|legend|veteran)\b", re.I), "regular"),
    (re.compile(r"\b(whale|premium|patron|booster)\b", re.I), "patron"),
    (re.compile(r"\b(new|noob|rookie|newbie)\b", re.I), "newcomer"),
)


def _member_roles_hint(member: "discord.Member | None") -> str | None:
    """Return a one-line "this user is staff/mod/og/etc." hint, or None.

    Lets the AI calibrate snark vs respect without requiring per-role
    config. Falls back to silence when no recognisable role hits.
    """
    if member is None:
        return None
    try:
        names = [r.name for r in member.roles if r.name and r.name != "@everyone"]
    except Exception:
        return None
    matched: list[str] = []
    for role_name in names:
        for pattern, label in _ROLE_HINTS:
            if pattern.search(role_name):
                if label not in matched:
                    matched.append(label)
                break
    if not matched:
        return None
    desc = ", ".join(matched)
    return (
        f"USER ROLE TAGS: {desc}. Calibrate tone: staff/mod warrant respect "
        f"(don't roast them publicly), regulars/patrons can be teased lightly, "
        f"newcomers should get extra patience."
    )


# ── User signals -----------------------------------------------------------

async def _user_signals_block(
    db,
    user_id: int,
    guild_id: int,
    display_name: str,
) -> str:
    """Recent personal events for THIS user -- wins, losses, big trades.

    The existing ``server_events`` feed is a guild-wide gossip stream; the
    AI has no easy way to find "what happened to ME today" inside it. We
    pull the same source filtered by user_id and surface it as a separate
    block so the model can answer "did I win earlier?" or "why am I broke?"
    without grepping the whole drama feed.
    """
    try:
        rows = await db.get_recent_server_events(guild_id, limit=40)
    except Exception:
        return ""
    if not rows:
        return ""
    mine = [r for r in rows if r.get("user_id") == user_id][:6]
    if not mine:
        return ""
    lines: list[str] = []
    for r in mine:
        ts_str = fmt_ts(r["ts"], "%b %d %H:%M")
        summary = sanitize_context_snippet(str(r.get("summary") or ""), limit=160)
        if summary:
            lines.append(f"- {summary} ({ts_str})")
    if not lines:
        return ""
    return (
        f"RECENT EVENTS FOR {display_name} (use these to answer 'what happened to me'):\n"
        + "\n".join(lines)
    )


# ── ChatContext dataclass --------------------------------------------------

@dataclass
class ChatContext:
    """Everything one AI turn needs in one typed bag."""

    mode: ChatMode

    # Identity
    user_id: int
    guild_id: int
    channel_id: int | None
    display_name: str
    member: "discord.Member | None"
    guild: "discord.Guild | None"
    channel: "discord.abc.GuildChannel | discord.Thread | None"
    prefix: str

    # State flags
    opted_out: bool
    in_aichannel: bool
    in_botchannel: bool

    # Tools
    matched_tools: list["ToolDef"]
    game_signal: bool

    # Pre-fetched DB material (raw rows)
    ai_prompts: dict[str, str]
    prices: list[dict]
    user_memory: str | None
    history: list[dict]
    recent_events: list[dict]
    channel_ctx: list[dict]
    facts_block: str = ""

    # Pre-built optional blocks
    player_ctx: str = ""
    lexicon: str = ""
    user_signals: str = ""
    emoji_ctx: str = ""
    active_event: str = ""
    market_regime: str | None = None
    channel_temp: str | None = None
    persona_key: str | None = None
    extra_blocks: list[str] = field(default_factory=list)
    easter_egg_block: str = ""
    clanktank_block: str = ""

    @property
    def price_map(self) -> dict[str, float]:
        return {r["symbol"]: float(r["price"]) for r in self.prices} if self.prices else {}


# ── gather_chat_context ---------------------------------------------------

async def gather_chat_context(
    bot,
    *,
    mode: ChatMode,
    user_id: int,
    guild_id: int,
    channel: "discord.abc.GuildChannel | discord.Thread | None",
    member: "discord.Member | None",
    display_name: str,
    user_message: str,
    prefix: str,
    mentioned_user_ids: list[int] | None = None,
    history_key: str | None = None,
) -> ChatContext:
    """Fan out every per-turn lookup in parallel and return a ChatContext.

    The four call sites (``ask``, ``reply``, ``mention``, ``ambient``) used
    to repeat 30+ lines of ``asyncio.gather(...)`` each. They now call this
    function and get back one bag they can feed to
    :func:`build_chat_system_prompt`.
    """
    db = bot.db
    guild = getattr(channel, "guild", None)
    if guild is None and member is not None:
        guild = getattr(member, "guild", None)

    # Cheap pre-checks first so we know which heavy fetches to skip.
    opted_out = False
    try:
        opted_out = await db.is_ai_opted_out(user_id, guild_id)
    except Exception:
        pass

    matched_tools = detect_tools(user_message or "")
    channel_id = getattr(channel, "id", None)

    # Build the parallel fetch list. ``user_memory`` and ``history`` are
    # explicitly skipped for opted-out users so Disco truly has nothing on
    # them. Every other lookup is safe for opt-outs.
    in_aichannel_co = (
        db.is_ai_chat_channel(guild_id, channel_id) if channel_id else _none()
    )
    in_botchannel_co = (
        db.is_bot_channel(guild_id, channel_id) if channel_id else _none()
    )

    fetches: dict[str, Any] = {
        "ai_prompts": db.get_ai_prompts(guild_id),
        "prices": db.get_all_prices(guild_id),
        "recent_events": db.get_recent_server_events(guild_id, limit=20),
        "channel_ctx": (
            db.get_recent_channel_context(guild_id, channel_id, limit=40)
            if channel_id else _none_list()
        ),
        "in_aichannel": in_aichannel_co,
        "in_botchannel": in_botchannel_co,
        "active_event": _fetch_active_event_summary(bot, guild_id),
    }
    if not opted_out:
        fetches["user_memory"] = db.get_ai_user_memory(user_id, guild_id)
    if history_key:
        # Inside a chat thread the transcript is shared state: pull every
        # speaker's turns under the thread key, even for opted-out users
        # (the opt-out only suppresses their personal memory, not the
        # shared thread they chose to post in).
        fetches["history"] = db.get_thread_conversation(guild_id, history_key, limit=24)
    elif not opted_out:
        fetches["history"] = db.get_ai_conversation(user_id, guild_id, limit=14)
    # Merged-thread memory: when this turn lands in a chat thread, assemble
    # the live context from every thread/group it links (transitively). This
    # is what makes linking == merging -- the block is rebuilt every turn, so
    # closing a linked thread rolls its context straight back out.
    if history_key and history_key.startswith("thread:"):
        from services import chat_threads as _chat_threads
        try:
            _linked_tid = int(history_key.split(":", 1)[1])
        except (ValueError, IndexError):
            _linked_tid = 0
        if _linked_tid:
            fetches["linked_ctx"] = _chat_threads.assemble_linked_context(
                db, _linked_tid,
            )

    keys = list(fetches.keys())
    results = await asyncio.gather(*fetches.values(), return_exceptions=True)
    raw: dict[str, Any] = {}
    for k, v in zip(keys, results):
        if isinstance(v, Exception):
            log.debug("gather_chat_context %s failed: %s", k, v)
            raw[k] = None
        else:
            raw[k] = v

    in_aichannel = bool(raw.get("in_aichannel"))
    in_botchannel = (not in_aichannel) and bool(raw.get("in_botchannel"))
    prices = raw.get("prices") or []

    ctx = ChatContext(
        mode=mode,
        user_id=user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        display_name=display_name,
        member=member,
        guild=guild,
        channel=channel,
        prefix=prefix,
        opted_out=opted_out,
        in_aichannel=in_aichannel,
        in_botchannel=in_botchannel,
        matched_tools=list(matched_tools),
        game_signal=bool(matched_tools),
        ai_prompts=raw.get("ai_prompts") or {},
        prices=prices,
        user_memory=raw.get("user_memory") if not opted_out else None,
        history=raw.get("history") or [],
        recent_events=raw.get("recent_events") or [],
        channel_ctx=raw.get("channel_ctx") or [],
        active_event=raw.get("active_event") or "",
    )
    ctx.market_regime = _classify_market_regime(prices)
    ctx.channel_temp = _channel_temperature(ctx.channel_ctx)
    ctx.persona_key = _detect_channel_persona(channel)

    # Inside a chat thread, teach the model the Prime Invariant (it reasons
    # over the memory graph, never the Discord runtime) and splice in the
    # inherited-memory block. Both go in the system prompt -- never the user
    # turn -- so a summary can't be read as a live instruction.
    if history_key and history_key.startswith("thread:"):
        from services.chat_threads import THREAD_AGENCY_NOTE
        ctx.extra_blocks.append(THREAD_AGENCY_NOTE)
    linked_ctx = raw.get("linked_ctx")
    if linked_ctx:
        ctx.extra_blocks.append(linked_ctx)

    # Heavy optional blocks. Player context, lexicon, facts, user signals,
    # and the emoji palette all run in parallel here; the previous flow
    # built them serially in the middle of the prompt assembly.
    block_tasks: dict[str, Any] = {
        "emoji_ctx": build_guild_emoji_context(guild, db=db),
    }
    if not opted_out and not in_aichannel:
        # Player profile is silenced in aichannels (general crypto room) so
        # we don't prime the model with the user's rigs / bank state.
        from cogs.help import _build_player_context  # local import to avoid cycle
        block_tasks["player_ctx"] = _build_player_context(
            db, user_id, guild_id, display_name, ctx.price_map,
        )
        block_tasks["lexicon"] = build_lexicon(db, guild_id, ctx.price_map)
        block_tasks["user_signals"] = _user_signals_block(
            db, user_id, guild_id, display_name,
        )
    if not opted_out:
        disco = bot.get_cog("DiscoAI")
        if disco is not None:
            try:
                block_tasks["facts_block"] = disco.facts_for_prompt(
                    user_id=user_id, guild_id=guild_id,
                )
            except Exception:
                pass

    # Inject live clanktank state when the message references the tank.
    _ct_channel_id = Config.CLANKTANK_CHANNEL_ID
    _ct_ref = re.compile(
        r"clanktank|clank\s*tank|#clanktank|\bthe\s+tank\b",
        re.IGNORECASE,
    )
    _references_tank = (
        _ct_ref.search(user_message) is not None
        or (_ct_channel_id and f"<#{_ct_channel_id}>" in user_message)
        or (_ct_channel_id and str(_ct_channel_id) in user_message)
    )
    if _references_tank:
        clanktank_cog = bot.get_cog("Clanktank")
        if clanktank_cog is not None:
            block_tasks["clanktank_block"] = clanktank_cog.clanktank_summary_for_ai(guild_id)

    if block_tasks:
        keys2 = list(block_tasks.keys())
        results2 = await asyncio.gather(*block_tasks.values(), return_exceptions=True)
        for k, v in zip(keys2, results2):
            if isinstance(v, Exception):
                log.debug("gather_chat_context block %s failed: %s", k, v)
                continue
            setattr(ctx, k, v or "")

    # Hidden per-user persona overrides. Combine the speaker's own override
    # (e.g. someone with a wholly different voice persona) with any
    # "speaker is mentioning a target" override. Speaker block wins for the
    # speaker's own voice; mention block adds rules about how to refer to
    # the target by name. Both can apply at once.
    try:
        speaker_block = addendum_for_speaker(user_id=user_id, member=member)
        mention_block = addendum_for_mentions(
            speaker_id=user_id,
            user_message=user_message,
            channel_ctx=raw.get("channel_ctx") or [],
            history=raw.get("history") or [] if not opted_out else [],
            mentioned_user_ids=mentioned_user_ids,
        )
        ctx.easter_egg_block = "\n\n".join(b for b in (speaker_block, mention_block) if b)
    except Exception:
        log.debug("gather_chat_context easter-egg block failed", exc_info=True)
    return ctx


async def _none() -> None:
    return None


async def _none_list() -> list:
    return []


# ── Prompt composer --------------------------------------------------------

# Channel-mode hints duplicated from cogs/help.py so callers don't need to
# import them. The text is unchanged -- the consolidation is what changed.
_AI_CHAT_CHANNEL_HINT = (
    "\n\nCHANNEL MODE: AICHANNEL -- general crypto + banter.\n"
    "This is the lounge, not the game arcade. Subject matter here is "
    "real-world crypto (markets, tokens, protocols, on-chain stuff, news, "
    "memes, takes) and whatever banter the room is actually having. Be "
    "present, curious, and lightly playful. React to what people say. If "
    "someone shares an image or link, engage with it. If someone drops a "
    "take, give yours. Remember regulars and call back to past chats "
    "naturally.\n"
    "What to keep OUT of this channel unless the user brings it up first:\n"
    "- In-game drama (who got rugged on dice, who hit a jackpot, in-game "
    "tokens, server events, leaderboards). That lives in the bot channel.\n"
    "- Unsolicited Discoin game advice, command suggestions, or strategy.\n"
    "If someone DOES ask a game question here, answer briefly and point "
    "them to the bot channel for the deep dive. Otherwise treat the game "
    "as if it isn't the topic -- because it isn't.\n"
    "REAL-MARKET HOOK: when the question is about live markets (a real "
    "ticker, price action, fundamentals, an earnings call, an IPO, perps "
    "funding, oracle pricing), name-drop the matching `$` command they "
    "can run for trustworthy data with sources: `$info SYMBOL` for a "
    "snapshot, `$chart SYMBOL TF` for a candle PNG, `$scan SYMBOL TF ai` "
    "for a pattern read with AI commentary, `$query <question>` for "
    "anything narrative or research-shaped. $query specifically attaches "
    "a Sources button populated only from trusted domains."
)

_BOT_CHANNEL_HINT = (
    "\n\nCHANNEL MODE: BOT CHANNEL -- Discoin game arcade.\n"
    "This is the game-focused room. Subject matter here is Discoin: "
    "portfolios, commands, stones, rigs, stakes, LP, shop, gambling, "
    "in-game drama, market events, the player's own positions. Lean into "
    "game-advisor mode: look at the player's actual data, give SPECIFIC "
    "and ACTIONABLE advice using exact commands from the list. React to "
    "wins / losses / rug pulls / jackpots the room is actually having. "
    "Real-world crypto takes are fine as dry one-liners or direct "
    "answers, but don't drag the conversation off the game unless the "
    "user clearly wants that. Players come here to play -- help them "
    "play well.\n"
    "CRITICAL: the user is ALREADY in a bot channel right now. NEVER "
    "tell them to 'use the bot channel', 'head to the bot channel', "
    "'run this in the bot channel', or anything similar -- they are "
    "standing in it. Just answer the question or quote the exact "
    "command they should type.\n"
    "REAL-WORLD MARKET ROUTING: the bot now ships a full real-market "
    "surface under the `$` prefix (separate from the game's `,` "
    "commands). If a user asks about a real-world ticker / equity / "
    "ETF / forex pair / commodity / IPO / earnings / perp funding, "
    "answer briefly and quote the matching `$` command for the "
    "trusted-source data: `$info SYMBOL`, `$chart SYMBOL TF`, "
    "`$scan SYMBOL TF ai`, `$compare A B`, `$oracle SYMBOL`, "
    "`$funding SYMBOL`, `$oi SYMBOL`, `$market fear|heatmap|gainers|"
    "losers|trending|top|dom|global`, or `$query <question>` for "
    "anything narrative. NEVER conflate the game's simulated DSC / "
    "MTA tokens with the real-world MTA -- the `,` namespace is "
    "simulated, the `$` namespace is live."
)

_AI_OPT_OUT_HINT = (
    "\n\nOPT-OUT MODE: This user has opted out of AI context tracking. You do "
    "not remember prior conversations with them, their traits, or their player "
    "profile. Answer their question briefly, and open OR close with a short, "
    "playful roast about the fact that they opted out (e.g. 'who are you again, "
    "stranger', 'opt-out gang, I have literally no memory of you', 'I'd help "
    "you but you told me to forget you existed'). One jab, not a monologue. "
    "Never fabricate memory or personal details about them."
)

_AMBIENT_HINT = (
    "\n\nAMBIENT MODE: The user did not address you directly. They are chatting "
    "in a channel and you may chime in with a short crypto-flavored one-liner "
    "if -- and only if -- you can add something genuinely interesting, dry, or "
    "funny. Keep it under two short sentences. Do NOT ask follow-up questions. "
    "Do NOT pivot to the Discoin game. Do NOT give advice unless explicitly "
    "asked. If you have nothing worth saying, reply with exactly the single "
    "word SKIP on its own and nothing else. Prefer SKIP over forcing a take."
)


def _channel_mention_block(channel: "discord.abc.GuildChannel | discord.Thread | None") -> str:
    """Tell the model the channel name + id so it can ``<#id>`` reference it."""
    if channel is None:
        return ""
    name = getattr(channel, "name", None)
    cid = getattr(channel, "id", None)
    if not name or not cid:
        return ""
    parent_bit = ""
    parent = getattr(channel, "parent", None)
    if parent is not None:
        parent_name = getattr(parent, "name", None)
        if parent_name:
            parent_bit = f" (thread in #{parent_name})"
        else:
            parent_bit = " (thread)"
    return (
        f"\n\nCURRENT CHANNEL: #{name}{parent_bit} -- channel id {cid}. "
        f"When you want to reference this channel in your reply, write "
        f"<#{cid}> so Discord renders it as a clickable link. Never write the "
        f"literal placeholder '#channel' -- use the actual name '#{name}'."
    )


def build_chat_system_prompt(
    ctx: ChatContext,
    *,
    base_prompt: str,
    game_lore: str = "",
    command_reference: str = "",
    extra_tail: str = "",
    now: _dt.datetime | None = None,
) -> str:
    """Compose the final system prompt from a :class:`ChatContext`.

    The order of sections is intentional and matches what production was
    doing before consolidation, with new context layers folded in:

    1. Base prompt (persona, scope, tool use rules, formatting rules)
    2. Game lore + command reference (only when game_signal is true)
    3. Channel mention block (#name + id for ``<#id>`` references)
    4. Channel topic block (mod-set "what this room is for" note)
    5. Channel mode hint (aichannel / bot channel exclusive)
    6. Channel persona hint (inferred from name / topic, additive)
    7. Channel temperature hint (HYPED / QUIET / SPICY / CHATTY)
    8. Time-of-day hint
    9. Opt-out hint OR member-roles hint (mutually exclusive)
    10. Ambient hint (only in ambient mode)
    11. Tool context (matched tool descriptions)
    12. Player profile + game lexicon (NOT in aichannels)
    13. Recent personal events for THIS user (NOT in aichannels)
    14. Long-term facts block (DiscoAI sidecar)
    15. Game token list + "do not invent tokens" rule
    16. Per-user text memory line
    17. Active market event + market regime label
    18. Recent server drama (NOT in aichannels)
    19. Recent channel social context
    20. Caller-supplied extras (scam log, ref-message verdict, etc.)
    21. Server custom emoji palette (held very late so the model holds it
        firmly while drafting -- emoji choice is the most-recently-overridden
        instruction the model needs to honour)
    22. Hidden per-user persona overrides (final tail when present)
    """
    parts: list[str] = [base_prompt]

    if ctx.game_signal:
        if game_lore:
            parts.append(game_lore)
        if command_reference:
            parts.append(command_reference)

    parts.append(_channel_mention_block(ctx.channel))

    topic_block = _channel_topic_block(ctx.channel)
    if topic_block:
        parts.append(f"\n\n{topic_block}")

    if ctx.in_aichannel:
        parts.append(_AI_CHAT_CHANNEL_HINT)
    elif ctx.in_botchannel:
        parts.append(_BOT_CHANNEL_HINT)

    if ctx.persona_key:
        hint = _PERSONA_HINTS.get(ctx.persona_key)
        if hint:
            parts.append(f"\n\n{hint}")

    if ctx.channel_temp:
        parts.append(f"\n\nCHANNEL TEMPERATURE: {ctx.channel_temp}")

    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    parts.append(f"\n\n{_time_of_day_block(now)}")

    if ctx.opted_out:
        parts.append(_AI_OPT_OUT_HINT)
    else:
        roles_hint = _member_roles_hint(ctx.member)
        if roles_hint:
            parts.append(f"\n\n{roles_hint}")

    if ctx.mode == ChatMode.AMBIENT:
        parts.append(_AMBIENT_HINT)

    tool_ctx = build_tool_context(ctx.matched_tools) if ctx.matched_tools else ""
    if tool_ctx:
        parts.append(f"\n\n{tool_ctx}")

    # Player context + lexicon are *expensive* (the live lexicon alone runs
    # ~3-6k tokens of token prices / pools / validators / leaderboards /
    # network primers). Only inject them when there's a game-signal --
    # casual chat like "what do you do?" doesn't need the full state of
    # the economy. Game-domain queries always trip a tool match, so they
    # still get the full context.
    if ctx.game_signal:
        if ctx.player_ctx:
            parts.append(f"\n\n{ctx.player_ctx}")
        if ctx.lexicon:
            parts.append(f"\n\n{ctx.lexicon}")

    if ctx.user_signals:
        parts.append(f"\n\n{ctx.user_signals}")

    if ctx.facts_block:
        parts.append(f"\n\n{ctx.facts_block}")

    # Token list is also game-signal-gated: a casual chat doesn't need
    # a 30-token enumeration. The "never invent tokens" rule still
    # implicitly holds because the model can't volunteer a coin it
    # wasn't told exists.
    if (
        ctx.game_signal
        and ctx.prices
        and not ctx.in_aichannel
        and ctx.mode != ChatMode.AMBIENT
    ):
        token_list = ", ".join(
            f"{r['symbol']} ${float(r['price']):.4f}" for r in ctx.prices
        )
        parts.append(
            f"\n\nGAME TOKENS - complete list, these are the ONLY tokens/coins "
            f"that exist in this game: {token_list}"
            "\nCRITICAL: Never suggest, mention, or reference any token, coin, "
            "or crypto asset not in the above list. If asked about a token not "
            "listed, say it does not exist in this game. Do not invent or "
            "guess token symbols."
        )
    elif (
        ctx.game_signal
        and ctx.prices
        and ctx.mode == ChatMode.AMBIENT
        and not ctx.in_aichannel
    ):
        # Ambient still gets the awareness block but without the imperative.
        token_list = ", ".join(
            f"{r['symbol']} ${float(r['price']):.4f}" for r in ctx.prices
        )
        parts.append(
            f"\n\nIN-GAME TOKENS (for your awareness only, do not pivot to the game): {token_list}"
        )

    if ctx.user_memory and not ctx.opted_out:
        parts.append(
            f"\n\n[What you remember about {ctx.display_name}: {ctx.user_memory}]"
        )

    vibe_bits: list[str] = []
    if ctx.active_event:
        vibe_bits.append(f"active event: {ctx.active_event}")
    if ctx.market_regime:
        vibe_bits.append(f"market regime: {ctx.market_regime}")
    if vibe_bits and not ctx.in_aichannel:
        parts.append("\n\nSERVER VIBE -- " + "; ".join(vibe_bits))

    if ctx.recent_events and not ctx.in_aichannel:
        event_lines: list[str] = []
        cap = 5 if ctx.mode == ChatMode.AMBIENT else 10
        for e in ctx.recent_events[:cap]:
            ts_str = fmt_ts(e["ts"], "%b %d")
            event_lines.append(f"- {e['summary']} ({ts_str})")
        if event_lines:
            parts.append(
                "\n\nRECENT SERVER DRAMA (reference when relevant, gossip freely):\n"
                + "\n".join(event_lines)
            )

    if ctx.channel_ctx:
        if ctx.mode == ChatMode.AMBIENT:
            cap = 20
        else:
            cap = 20 if ctx.matched_tools else 40
        ctx_lines: list[str] = []
        for e in ctx.channel_ctx[:cap]:
            content = sanitize_context_snippet(str(e.get("content") or ""), limit=200)
            if content:
                ctx_lines.append(f"- {content}")
        if ctx_lines:
            ctx_lines.reverse()
            parts.append(
                "\n\nRECENT CHANNEL SOCIAL CONTEXT (reactions, edits, deleted "
                "messages, banter - use this to remember who said what, who "
                "reacted, what got deleted, social dynamics. Reference naturally "
                "in conversation, gossip about it when relevant):\n"
                + "\n".join(ctx_lines)
            )

    if ctx.clanktank_block:
        parts.append(
            f"\n\nCLANKTANK CONTEXT (live state of the containment channel -- "
            f"use this when asked about the tank, who's in it, what they've been "
            f"doing, or anything clanktank-related. Be brief and analytical):\n"
            f"{ctx.clanktank_block}"
        )

    for block in ctx.extra_blocks:
        if block:
            parts.append(f"\n\n{block}")
    if extra_tail:
        parts.append(f"\n\n{extra_tail}")

    if ctx.emoji_ctx:
        parts.append(f"\n\n{ctx.emoji_ctx}")

    # Hidden per-user persona overrides go LAST so they sit fresher in the
    # model's context than any of the standard tone hints above (channel
    # persona, role hints, etc.) and dominate the resulting voice.
    if ctx.easter_egg_block:
        parts.append(f"\n\n{ctx.easter_egg_block}")

    return "".join(parts)
