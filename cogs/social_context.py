"""
cogs/social_context.py - Social interaction tracking + autonomous bot personality.

Listens for reactions, message edits, message deletes, and post-catastrophe
chatter. Stores notable social context in the ``channel_context`` table so the
AI can reference it when players ask about server drama.

Also adds:
  - Autonomous reactions: the bot adds emoji reactions to messages with notable
    emotional content (loss, win, frustration, etc.) using server emojis when
    available.
  - Autonomous event reactions: reacts once per hot event window to catastrophes
    and jackpots.
  - Proactive greetings: very rarely notices and acknowledges active users the
    bot has a relationship with (gm, first-time notice, etc).
  - Dead chat revival: snarky comment after 3+ hours of silence.
  - Image awareness: when a user replies to the bot with an image, the bot
    acknowledges it and stores the context.
  - Emoji meaning context: logs what each emoji reaction means so the AI has
    richer context about how players are feeling.

Persistence:
  - Greeting and proactive-channel cooldowns are stored in Redis when available
    (discoin:social:greeted:{guild_id}:{user_id}, TTL 86400s; and
    discoin:social:proactive:{guild_id}:{channel_id}, TTL 1800s) so they survive
    bot restarts.  In-memory dicts serve as fallback when Redis is down.

Constraints:
  - Only tracks in guilds where Discoin is registered.
  - Rate-limited to 1000 entries per channel per hour.
  - Entries older than 90 days are pruned automatically.
  - Autonomous message reactions: max 1 per message, ~10-28% of qualifying msgs.
  - Proactive greetings: at most once per user per 24h, channel cooldown 30 min.
  - Reaction fetch: cached for 30s per message_id to reduce Discord API calls.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import defaultdict, OrderedDict
from datetime import date
from typing import Any

import discord
from discord.ext import commands, tasks

from core.framework.ai.safety import sanitize_context_snippet
from core.framework.bot import Discoin
from core.framework.discord_images import extract_image_urls, has_image
from core.framework.emoji_context import (
    CAT_LOSS, CAT_WIN, CAT_HYPE, CAT_FRUSTRATION, CAT_LAUGH,
    CAT_GG, CAT_SALT, CAT_SHOCK, CAT_VIBE, CAT_DEGENERATE, detect_reaction_category,
    get_emoji_description, pick_emoji,
)

log = logging.getLogger(__name__)

# ── Custom emoji usage tracking ───────────────────────────────────────────────
#
# Matches Discord custom emoji markup `<:name:id>` or animated `<a:name:id>`.
# The indexer in core/framework/emoji_index.py folds recent usage snippets into
# each emoji's nuanced meaning so the chat model picks them appropriately.
_CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]+):(\d+)>")


# ── Rate limiting ─────────────────────────────────────────────────────────────

_RATE_LIMIT = 1000
_RATE_WINDOW = 3600  # 1 hour
_rate_counters: dict[tuple[int, int], list[float]] = defaultdict(list)


def _rate_ok(guild_id: int, channel_id: int) -> bool:
    key = (guild_id, channel_id)
    now = time.time()
    _rate_counters[key] = [t for t in _rate_counters[key] if now - t < _RATE_WINDOW]
    if len(_rate_counters[key]) >= _RATE_LIMIT:
        return False
    _rate_counters[key].append(now)
    return True


# ── Hot channel tracking ──────────────────────────────────────────────────────

_hot_channels: dict[tuple[int, int], float] = {}
_HOT_WINDOW = 600
_reacted_events: set[tuple[int, int]] = set()

_last_activity: dict[tuple[int, int], float] = {}
_DEAD_CHAT_THRESHOLD = 3 * 3600
_DEAD_CHAT_COOLDOWN = 6 * 3600
_last_dead_chat_comment: dict[tuple[int, int], float] = {}


def mark_hot_channel(guild_id: int, channel_id: int, event_type: str = "catastrophe") -> None:
    """Mark a channel as 'hot' after a notable event. Called by other cogs."""
    _hot_channels[(guild_id, channel_id)] = time.time() + _HOT_WINDOW
    _reacted_events.discard((guild_id, channel_id))


def _is_hot(guild_id: int, channel_id: int) -> bool:
    key = (guild_id, channel_id)
    expires = _hot_channels.get(key, 0)
    if time.time() < expires:
        return True
    _hot_channels.pop(key, None)
    return False


# ── Reacted messages: O(1) lookup + FIFO eviction via OrderedDict ─────────────

_reacted_messages: OrderedDict[int, None] = OrderedDict()
_MAX_REACTED_CACHE = 500


def _already_reacted(msg_id: int) -> bool:
    return msg_id in _reacted_messages


def _mark_reacted(msg_id: int) -> None:
    _reacted_messages[msg_id] = None
    while len(_reacted_messages) > _MAX_REACTED_CACHE:
        _reacted_messages.popitem(last=False)


# ── Message fetch cache: avoids repeated fetch_message HTTP calls ─────────────

_message_cache: OrderedDict[int, tuple[Any, float]] = OrderedDict()  # id -> (msg, fetched_at)
_MESSAGE_CACHE_TTL = 30.0
_MESSAGE_CACHE_MAX = 200


async def _fetch_message_cached(channel: Any, message_id: int) -> Any:
    """Fetch a message with a short-lived in-memory cache to reduce API calls."""
    now = time.time()
    if message_id in _message_cache:
        msg, fetched_at = _message_cache[message_id]
        if now - fetched_at < _MESSAGE_CACHE_TTL:
            return msg
        del _message_cache[message_id]
    msg = await channel.fetch_message(message_id)
    _message_cache[message_id] = (msg, now)
    while len(_message_cache) > _MESSAGE_CACHE_MAX:
        _message_cache.popitem(last=False)
    return msg


# ── Proactive state (in-memory fallback; Redis used when available) ───────────

_greeted_today: dict[tuple[int, int], date] = {}   # (guild_id, user_id) -> date
_last_proactive: dict[tuple[int, int], float] = {}  # (guild_id, channel_id) -> ts
_last_ambient: dict[tuple[int, int], float] = {}    # (guild_id, channel_id) -> ts

_PROACTIVE_CHANNEL_COOLDOWN = 1800   # 30 min between proactive channel messages
_GREETING_CHANCE = 0.03              # 3% on first-msg-of-day from a known user
_NOTICE_CHANCE = 0.008               # 0.8% chance of a text notice (reaction only otherwise)

# Ambient crypto chatter (VisualMod-style): unsolicited one-liners when the
# channel is actively talking crypto. Tighter than proactive greetings so
# Disco can chime in a few times per hour on an active channel, but still
# rate-limited enough not to spam.
#
# Defaults below apply everywhere. When the channel is on the per-guild
# aichannel allowlist we use the boosted _AMBIENT_* values below so the
# banter / general-crypto room actually feels alive.
_AMBIENT_CHANNEL_COOLDOWN = 300      # 5 min default cooldown per channel
_AMBIENT_CHANCE = 0.05               # 5% default chance per crypto-flavored message
_AMBIENT_CHANNEL_COOLDOWN_AICHAT = 180  # 3 min in aichannels
_AMBIENT_CHANCE_AICHAT = 0.08           # 8% in aichannels (banter room)

# Redis key prefixes
_REDIS_GREET_PREFIX = "discoin:social:greeted"
_REDIS_PROACTIVE_PREFIX = "discoin:social:proactive"
_REDIS_AMBIENT_PREFIX = "discoin:social:ambient"

# Ambient content gate: at least one of these must match for Disco to consider
# chiming in. Covers $TICKER mentions, common crypto nouns, and price-move verbs.
_AMBIENT_CRYPTO_PATTERNS = re.compile(
    r"\$[A-Za-z]{2,8}\b"                                     # $MTA, $arc, $sol ...
    r"|\b(?:mta|arc|sol|xrp|doge|ada|bnb|ton|sui|arb|op|str|wif)\b"
    r"|\b(?:moneta|arcadia|solana|altcoin|shitcoin|memecoin|stablecoin)\b"
    r"|\b(?:pump|pumping|pumped|dump|dumping|dumped|rekt|rugged|rugpull|rug)\b"
    r"|\b(?:ath|atl|hodl|moon|mooning|airdrop|whale|liquidated|liquidation)\b"
    r"|\b(?:defi|cefi|nft|mint|mcap|market\s*cap|tvl|apy|apr|yield|staking)\b"
    r"|\b(?:bull\s*run|bear\s*market|degen|ngmi|wagmi|fud|fomo|cope)\b"
    r"|\b(?:ser|gm\s+ser|wen\s+lambo|buy\s+the\s+dip|btfd)\b",
    re.IGNORECASE,
)

# ── Quips and emoji pools ─────────────────────────────────────────────────────

_CATASTROPHE_QUIPS = [
    "rip in peace", "rekt", "another one bites the dust",
    "press F", "felt that in my wallet", "ngmi", "the chart remembers", "F",
]
_JACKPOT_QUIPS = [
    "gg", "nice hit", "the degen gods smile today", "somebody screenshot this",
]
_DEAD_CHAT_QUIPS = [
    "is anybody alive in here or did everyone get rugged",
    "the silence in here is louder than a liquidation alert",
    "tfw the chat is deader than the token you aped into last week",
    "gm. oh wait nobody's here. gn then",
    "even the bots stopped talking. that's concerning",
    "did everyone ragequit or just go touch grass",
]
_PROACTIVE_QUIPS_MORNING = [
    "gm", "early grind", "sunrise crew represent", "catching worms today?",
]
_PROACTIVE_QUIPS_AFTERNOON = [
    "afternoon", "still at the charts?", "mid-session check-in", "staying focused?",
]
_PROACTIVE_QUIPS_EVENING = [
    "evening session", "closing out the day?", "one more trade before gn?",
    "you showed up",
]
_PROACTIVE_QUIPS_NIGHT = [
    "still up?", "late night degen hours", "the midnight grind", "gm from the night shift",
]
_PROACTIVE_QUIPS_WIN = [
    "there they are", "living proof WAGMI is real", "screenshot that",
    "the legend returns", "riding high?",
]
_PROACTIVE_QUIPS_LOSS = [
    "we move", "rip bag, next one hits", "NGMI or WAGMI -- you decide",
    "the chart giveth and taketh away",
]


def _pick_proactive_quip(message: discord.Message, memory: str) -> str:
    """Return a greeting quip tuned to time-of-day and user's recent context."""
    hour = time.gmtime(time.time()).tm_hour  # 0-23 UTC

    content = (message.content or "").lower()
    mem_lower = (memory or "").lower()

    _LOSS_SIGNALS = {"rekt", "loss", "lost", "dump", "rugged", "liquidat", "bust", "broke"}
    _WIN_SIGNALS  = {"profit", "moon", "pump", "win", "won", "ath", "green", "gain", "rich"}

    if any(s in content or s in mem_lower for s in _LOSS_SIGNALS):
        return random.choice(_PROACTIVE_QUIPS_LOSS)
    if any(s in content or s in mem_lower for s in _WIN_SIGNALS):
        return random.choice(_PROACTIVE_QUIPS_WIN)

    if 5 <= hour < 12:
        return random.choice(_PROACTIVE_QUIPS_MORNING)
    if 12 <= hour < 17:
        return random.choice(_PROACTIVE_QUIPS_AFTERNOON)
    if 17 <= hour < 22:
        return random.choice(_PROACTIVE_QUIPS_EVENING)
    return random.choice(_PROACTIVE_QUIPS_NIGHT)

# Per-category reaction probability
_CATEGORY_CHANCE = {
    CAT_LOSS:        0.28,
    CAT_SHOCK:       0.22,
    CAT_WIN:         0.20,
    CAT_GG:          0.20,
    CAT_FRUSTRATION: 0.18,
    CAT_DEGENERATE:  0.16,
    CAT_HYPE:        0.15,
    CAT_SALT:        0.14,
    CAT_LAUGH:       0.12,
    CAT_VIBE:        0.10,
}


class SocialContext(commands.Cog):
    """Tracks social interactions for richer AI context + autonomous reactions."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._prune_task.start()

    def cog_unload(self) -> None:
        self._prune_task.cancel()

    # ── Redis helpers (graceful fallback to in-memory) ────────────────────────

    def _redis(self):
        """Return the raw redis.asyncio client from the bus, or None if unavailable."""
        return getattr(self.bot.bus, "_redis", None)

    async def _is_greeted_today(self, guild_id: int, user_id: int) -> bool:
        r = self._redis()
        if r is not None:
            try:
                return bool(await r.exists(f"{_REDIS_GREET_PREFIX}:{guild_id}:{user_id}"))
            except Exception:
                pass  # Redis down - fall through to in-memory
        return _greeted_today.get((guild_id, user_id)) == date.today()

    async def _set_greeted_today(self, guild_id: int, user_id: int) -> None:
        _greeted_today[(guild_id, user_id)] = date.today()  # always set in-memory
        r = self._redis()
        if r is not None:
            try:
                await r.setex(f"{_REDIS_GREET_PREFIX}:{guild_id}:{user_id}", 86400, "1")
            except Exception:
                pass  # Redis down - in-memory fallback already set

    async def _proactive_ok(self, guild_id: int, channel_id: int) -> bool:
        """Return True (and mark cooldown) if no proactive msg was sent recently here."""
        r = self._redis()
        if r is not None:
            try:
                key = f"{_REDIS_PROACTIVE_PREFIX}:{guild_id}:{channel_id}"
                # SET NX EX is atomic: returns True only when key didn't exist
                placed = await r.set(key, "1", ex=_PROACTIVE_CHANNEL_COOLDOWN, nx=True)
                if placed:
                    _last_proactive[(guild_id, channel_id)] = time.time()
                    return True
                return False
            except Exception:
                pass  # Redis down - fall through to in-memory
        now = time.time()
        key = (guild_id, channel_id)
        if now - _last_proactive.get(key, 0) < _PROACTIVE_CHANNEL_COOLDOWN:
            return False
        _last_proactive[key] = now
        return True

    async def _ambient_ok(self, guild_id: int, channel_id: int, *, cooldown: int | None = None) -> bool:
        """Return True (and mark cooldown) if no ambient chatter fired recently here.

        Uses its own Redis key + TTL so it doesn't share budget with the
        proactive greeting cooldown. In-memory fallback reuses
        ``_last_proactive`` because the fallback is best-effort anyway.

        ``cooldown`` overrides the default per-channel cooldown so callers can
        use a shorter window in aichannels.
        """
        cd = int(cooldown if cooldown is not None else _AMBIENT_CHANNEL_COOLDOWN)
        r = self._redis()
        if r is not None:
            try:
                key = f"{_REDIS_AMBIENT_PREFIX}:{guild_id}:{channel_id}"
                placed = await r.set(key, "1", ex=cd, nx=True)
                return bool(placed)
            except Exception:
                pass
        now = time.time()
        key = (guild_id, channel_id)
        if now - _last_ambient.get(key, 0) < cd:
            return False
        _last_ambient[key] = now
        return True

    # ── Autonomous event reactions ────────────────────────────────────────────

    async def react_to_event(
        self,
        channel: discord.TextChannel,
        message: discord.Message,
        event_type: str,
    ) -> None:
        """Add an emoji reaction and optionally a short quip to a notable event.

        Called after a catastrophe/jackpot embed is sent. Only reacts once per
        hot event window. Prefers server custom emojis.
        """
        key = (channel.guild.id, channel.id)
        if key in _reacted_events:
            return
        _reacted_events.add(key)

        try:
            ai_flags = await self.bot.db.get_ai_flags(channel.guild.id)
            if not ai_flags.get("chat"):
                return
        except Exception:
            return

        try:
            cat = CAT_LOSS if event_type in ("catastrophe", "drain", "rugpull_fail") else CAT_WIN
            emoji = pick_emoji(channel.guild, cat)
            await message.add_reaction(emoji)
        except Exception:
            pass

        if random.random() < 0.40:
            await asyncio.sleep(random.uniform(1.5, 4.0))
            try:
                quips = _CATASTROPHE_QUIPS if event_type in ("catastrophe", "drain", "rugpull_fail") else _JACKPOT_QUIPS
                await channel.send(random.choice(quips))
            except Exception:
                pass

    # ── Autonomous message reactions ──────────────────────────────────────────

    async def _maybe_react_to_message(self, message: discord.Message) -> None:
        """React with an emoji to messages with notable emotional content.

        Probability check runs first (cheapest) to minimise Discord API and DB
        calls on the ~85-90% of messages that don't qualify.
        """
        if _already_reacted(message.id):
            return

        content = message.content
        if not content or len(content) < 4:
            return

        category = detect_reaction_category(content)
        if not category:
            return

        # Probability gate runs BEFORE any I/O
        if random.random() > _CATEGORY_CHANCE.get(category, 0.10):
            return

        # Only then check AI flags (DB hit)
        try:
            ai_flags = await self.bot.db.get_ai_flags(message.guild.id)
            if not ai_flags.get("chat"):
                return
        except Exception:
            return

        _mark_reacted(message.id)
        try:
            emoji = pick_emoji(message.guild, category)
            await message.add_reaction(emoji)
        except discord.Forbidden:
            pass
        except Exception:
            log.debug("Failed to add autonomous reaction", exc_info=True)

        # Respect AI context opt-out: don't learn reaction categories or
        # trait signals for users who've asked us not to.
        try:
            if await self.bot.db.is_ai_opted_out(message.author.id, message.guild.id):
                return
        except Exception:
            pass

        # Persist the reaction category so the AI builds a picture of this user's style
        try:
            await self.bot.db.log_ai_reaction_memory(message.author.id, message.guild.id, category)
        except Exception:
            log.debug("Failed to log reaction memory", exc_info=True)
        try:
            from services.ai_traits import ingest_reaction as _ingest_rx
            await _ingest_rx(self.bot.db, message.author.id, message.guild.id, category)
        except Exception:
            log.debug("Failed to ingest reaction trait", exc_info=True)

    # ── Proactive greetings ───────────────────────────────────────────────────

    async def _maybe_greet_user(self, message: discord.Message) -> None:
        """Very rarely notice and acknowledge an active user the bot knows.

        Order of operations (cheapest first to minimise I/O on the ~97% of
        calls that fail the probability gate):
          1. Probability gate (pure random, no I/O)
          2. Redis/in-memory greeted-today check (fast KV lookup)
          3. Channel proactive cooldown (Redis SET NX or in-memory)
          4. DB query for user memory (only runs for the small fraction that pass)
          5. AI flags DB check
        """
        # 1. Probability gate first - avoid ALL I/O on ~97% of calls
        if random.random() > _GREETING_CHANCE:
            return

        guild_id = message.guild.id
        user_id = message.author.id
        channel_id = message.channel.id

        # 2. Channel must be on the AI allowlist (same rule as ambient
        # chatter). Greetings used to fire in any channel which meant the
        # bot would randomly pipe up in #announcements / #support -- the
        # allowlist is the single source of truth for "this is a room the
        # bot may speak in".
        try:
            allowed_channels = await self.bot.db.get_ai_chat_channels(guild_id)
        except Exception:
            return
        if not allowed_channels or channel_id not in allowed_channels:
            return

        # 3. Greeted-today check (Redis or in-memory)
        if await self._is_greeted_today(guild_id, user_id):
            return

        # 4. Channel cooldown (Redis SET NX or in-memory)
        if not await self._proactive_ok(guild_id, channel_id):
            return

        # Respect AI context opt-out: never greet opted-out users proactively.
        try:
            if await self.bot.db.is_ai_opted_out(user_id, guild_id):
                return
        except Exception:
            pass

        # 4. DB query - only runs for ~3% that cleared above gates
        try:
            memory = await self.bot.db.get_ai_user_memory(user_id, guild_id)
        except Exception:
            return
        if not memory:
            return  # don't greet strangers unprompted

        # 5. AI flags
        try:
            ai_flags = await self.bot.db.get_ai_flags(guild_id)
            if not ai_flags.get("chat"):
                return
        except Exception:
            return

        # Mark greeted (Redis + in-memory)
        await self._set_greeted_today(guild_id, user_id)

        # Usually just a reaction; very rarely a text greeting
        if random.random() < _NOTICE_CHANCE / _GREETING_CHANCE:
            await asyncio.sleep(random.uniform(0.8, 2.5))
            try:
                await message.channel.send(_pick_proactive_quip(message, memory))
            except Exception:
                pass
        else:
            try:
                emoji = pick_emoji(message.guild, CAT_VIBE)
                await message.add_reaction(emoji)
            except Exception:
                pass

    # ── Ambient crypto chatter (VisualMod-style) ──────────────────────────────

    async def _maybe_ambient_crypto_chatter(self, message: discord.Message) -> None:
        """Very occasionally chime in with a crypto-flavored one-liner.

        Gates in cheap-to-expensive order:
          1. Cheap probability prefilter (uses the higher aichannel rate so
             we don't reject messages before we know the channel type).
          2. Content gate (regex match against crypto vocabulary)
          3. Channel allowlist lookup -- decides which chance + cooldown to
             apply (boosted in aichannels, defaults elsewhere) and whether
             this channel is even allowed.
          4. Final probability gate against the channel-specific chance.
          5. AI flags + per-channel cooldown (Redis SET NX / in-memory).

        When all gates pass, delegate the actual AI call to
        :meth:`cogs.help.Help.handle_ai_ambient`, which reuses the full
        system-prompt build and will silently SKIP if it has nothing to say.
        """
        # 1. Cheap prefilter at the MAX of the two rates so we avoid all I/O
        #    on messages that would fail both gates anyway. Composed with a
        #    final per-channel check below so the overall probability lands
        #    exactly on _AMBIENT_CHANCE / _AMBIENT_CHANCE_AICHAT.
        _max_chance = max(_AMBIENT_CHANCE, _AMBIENT_CHANCE_AICHAT)
        if random.random() > _max_chance:
            return

        content = message.content or ""
        if len(content) < 6:
            return

        # 2. Content gate - must look crypto-flavored (cheap regex, no I/O)
        if not _AMBIENT_CRYPTO_PATTERNS.search(content):
            return

        guild_id = message.guild.id
        channel_id = message.channel.id

        # 3. Per-guild channel allowlist. Ambient game chatter is OPT-IN
        # per channel: an empty allowlist means the bot says nothing. The
        # old behaviour ("empty = allow everywhere") caused the bot to
        # comment on token prices in #general / #off-topic which is the
        # single biggest spam complaint; now silence is the safe default
        # until an admin runs .channel ai #lounge.
        try:
            allowed_channels = await self.bot.db.get_ai_chat_channels(guild_id)
        except Exception:
            return
        if not allowed_channels or channel_id not in allowed_channels:
            return

        # Inside an aichannel we boost the chatter rate so the lounge feels
        # alive. The fallback branch is dead code now (we just returned) but
        # keeping the variables makes the shape obvious if someone later
        # reintroduces a "soft" mode.
        in_aichannel = True
        chance = _AMBIENT_CHANCE_AICHAT
        cooldown = _AMBIENT_CHANNEL_COOLDOWN_AICHAT

        # 4. Rescale against the already-passed prefilter so the effective
        #    fire rate matches the channel-specific chance exactly. In an
        #    aichannel this is 1.0 (always pass); outside it's chance/max.
        _rescale = chance / _max_chance if _max_chance > 0 else 0.0
        if _rescale < 1.0 and random.random() > _rescale:
            return

        # 5. AI flags + channel cooldown
        try:
            ai_flags = await self.bot.db.get_ai_flags(guild_id)
            if not ai_flags.get("chat"):
                return
        except Exception:
            return
        if not await self._ambient_ok(guild_id, channel_id, cooldown=cooldown):
            return

        # Delegate to the Help cog's ambient pipeline
        help_cog = self.bot.get_cog("Help")
        if help_cog is None:
            return
        try:
            await help_cog.handle_ai_ambient(message)
        except Exception:
            log.warning("[ambient] handle_ai_ambient failed", exc_info=True)

    # ── Dead chat revival ─────────────────────────────────────────────────────

    async def _maybe_revive_dead_chat(self, channel: discord.TextChannel) -> None:
        key = (channel.guild.id, channel.id)
        now = time.time()
        if now - _last_dead_chat_comment.get(key, 0) < _DEAD_CHAT_COOLDOWN:
            return
        if now - _last_activity.get(key, now) < _DEAD_CHAT_THRESHOLD:
            return
        # Dead-chat revival is opt-in per channel -- the bot must not pipe
        # up in #announcements / #support / #general just because the
        # channel went quiet. Same allowlist as ambient + greet.
        try:
            allowed_channels = await self.bot.db.get_ai_chat_channels(channel.guild.id)
        except Exception:
            return
        if not allowed_channels or channel.id not in allowed_channels:
            return
        try:
            ai_flags = await self.bot.db.get_ai_flags(channel.guild.id)
            if not ai_flags.get("chat"):
                return
        except Exception:
            return
        _last_dead_chat_comment[key] = now
        try:
            await channel.send(random.choice(_DEAD_CHAT_QUIPS))
        except Exception:
            pass

    # ── Image-reply context logging ───────────────────────────────────────────
    #
    # The actual AI reply to an image-bearing reply is owned by the Help cog's
    # rich streaming pipeline (``handle_ai_reply``), which forwards the image
    # URLs through the agent tool loop so ``vision.describe_image`` can run.
    # Previously this cog also sent its own short one-liner reply, which
    # caused a visible double-reply bug (Help cog + social_context cog both
    # answering the same image). We now ONLY log the social context row so
    # future prompts still know the user sent an image, and let Help handle
    # the actual conversation.

    async def _log_custom_emoji_usage(self, message: discord.Message) -> None:
        """Record each custom server emoji used in ``message.content``.

        Snippet stored is the surrounding message text (trimmed) with the
        emoji markup stripped so the indexer's usage block reads cleanly.
        Deduped per (guild, emoji, message) so repeated uses of the same
        emoji in one message only count once.
        """
        if not message.guild or message.author.bot:
            return
        try:
            matches = list(_CUSTOM_EMOJI_RE.finditer(message.content))
        except Exception:
            return
        if not matches:
            return

        seen_ids: set[int] = set()
        snippet_raw = _CUSTOM_EMOJI_RE.sub(" ", message.content).strip()
        snippet = sanitize_context_snippet(snippet_raw, limit=180)

        for m in matches:
            try:
                eid = int(m.group(3))
            except (TypeError, ValueError):
                continue
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            # Only track emojis that actually belong to this guild -- external
            # emojis (from other servers the user is in) would pollute the
            # per-guild index.
            guild_emoji = discord.utils.get(message.guild.emojis, id=eid)
            if guild_emoji is None:
                continue
            try:
                await self.bot.db.log_emoji_usage(
                    message.guild.id, eid, message.author.id, snippet,
                )
            except Exception:
                log.debug("Failed to log emoji usage for %d", eid, exc_info=True)

    async def _log_image_reply_context(self, message: discord.Message) -> None:
        if not message.reference or not message.guild:
            return
        ref = message.reference.resolved
        if not isinstance(ref, discord.Message) or ref.author.id != self.bot.user.id:
            return
        if not has_image(message):
            return

        image_urls = extract_image_urls(message)
        img_count = len(image_urls)
        text_content = sanitize_context_snippet(message.content, limit=200) if message.content else ""

        try:
            await self.bot.db.log_channel_context(
                message.guild.id,
                message.channel.id,
                message.author.id,
                "image_reply",
                f"{message.author.display_name} shared {'an image' if img_count == 1 else f'{img_count} images'}"
                + (f" with text: {text_content}" if text_content else ""),
            )
        except Exception:
            pass

    # ── Reaction tracking ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id or payload.member is None or payload.member.bot:
            return
        if not _rate_ok(payload.guild_id, payload.channel_id):
            return

        target_uid = None
        reacted_msg_content = ""
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if channel and hasattr(channel, "fetch_message"):
                msg = await _fetch_message_cached(channel, payload.message_id)
                if msg and not msg.author.bot:
                    target_uid = msg.author.id
                    reacted_msg_content = sanitize_context_snippet(msg.content or "", limit=60)
        except Exception:
            pass

        emoji_str = str(payload.emoji)
        emoji_desc = get_emoji_description(emoji_str)
        context_line = f"{payload.member.display_name} reacted {emoji_desc}"
        if reacted_msg_content:
            context_line += f' to: "{reacted_msg_content}"'

        try:
            await self.bot.db.log_channel_context(
                payload.guild_id,
                payload.channel_id,
                payload.member.id,
                "reaction",
                context_line,
                target_uid,
                {"emoji": emoji_str, "emoji_desc": emoji_desc},
            )
        except Exception:
            log.debug("Failed to log reaction context", exc_info=True)

        # Persist the emoji category for this user to ai_reaction_memory
        from core.framework.emoji_context import detect_reaction_category as _det_cat
        user_cat = _det_cat(emoji_str) or _det_cat(emoji_desc)
        if user_cat:
            try:
                await self.bot.db.log_ai_reaction_memory(
                    payload.member.id, payload.guild_id, user_cat
                )
            except Exception:
                log.debug("Failed to log user reaction memory", exc_info=True)
            try:
                from services.ai_traits import ingest_reaction as _ingest_rx
                await _ingest_rx(self.bot.db, payload.member.id, payload.guild_id, user_cat)
            except Exception:
                log.debug("Failed to ingest reaction trait", exc_info=True)

    # ── Message delete tracking ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        # Skip command invocations that were auto-deleted by the bot
        if message.id in self.bot._autodelete_done:
            return
        if not _rate_ok(message.guild.id, message.channel.id):
            return
        snippet = sanitize_context_snippet(message.content, limit=80) if message.content else ""
        summary = f"{message.author.display_name} deleted a message"
        if snippet:
            summary += f' (was: "{snippet}...")'
        try:
            await self.bot.db.log_channel_context(
                message.guild.id, message.channel.id, message.author.id,
                "message_delete", summary,
            )
        except Exception:
            log.debug("Failed to log delete context", exc_info=True)

    # ── Message edit tracking ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot:
            return
        if not _rate_ok(after.guild.id, after.channel.id):
            return
        if before.content == after.content:
            return
        before_snip = sanitize_context_snippet(before.content, limit=60) if before.content else ""
        after_snip = sanitize_context_snippet(after.content, limit=60) if after.content else ""
        summary = f"{after.author.display_name} edited a message"
        if before_snip and after_snip:
            summary += f' (from "{before_snip}" to "{after_snip}")'
        try:
            await self.bot.db.log_channel_context(
                after.guild.id, after.channel.id, after.author.id,
                "message_edit", summary,
            )
        except Exception:
            log.debug("Failed to log edit context", exc_info=True)

    # ── Main on_message ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return

        key = (message.guild.id, message.channel.id)
        _last_activity[key] = time.time()

        # Image reply to bot: log social context only. The rich AI reply is
        # owned by the Help cog's streaming pipeline via ``handle_ai_reply``,
        # which now routes image URLs through the agent tool loop.
        if message.reference and has_image(message):
            asyncio.create_task(self._log_image_reply_context(message))

        # Dead chat revival
        if isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            asyncio.create_task(self._maybe_revive_dead_chat(message.channel))

        if not message.content:
            return

        # Custom emoji usage tracking: record each custom server emoji used
        # in a message so the per-guild emoji indexer can learn from real
        # usage patterns. Dedup per-message-per-emoji so a spammed reaction
        # doesn't dominate the sample.
        if _CUSTOM_EMOJI_RE.search(message.content):
            asyncio.create_task(self._log_custom_emoji_usage(message))

        # Autonomous emoji reaction (probability gate inside method)
        if isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            asyncio.create_task(self._maybe_react_to_message(message))

        # Proactive greeting (probability gate inside method)
        if isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            asyncio.create_task(self._maybe_greet_user(message))

        # Ambient crypto chatter (probability + content + allowlist gates inside)
        if isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            asyncio.create_task(self._maybe_ambient_crypto_chatter(message))

        # Respect AI context opt-out: skip ALL channel-context capture for
        # opted-out users so their words don't end up in anyone else's prompt.
        try:
            if await self.bot.db.is_ai_opted_out(message.author.id, message.guild.id):
                return
        except Exception:
            pass

        # Hot channel: full tracking
        if _is_hot(message.guild.id, message.channel.id):
            if _rate_ok(message.guild.id, message.channel.id):
                snippet = sanitize_context_snippet(message.content, limit=120)
                if snippet and len(snippet) >= 5:
                    try:
                        await self.bot.db.log_channel_context(
                            message.guild.id, message.channel.id, message.author.id,
                            "reply", f"{message.author.display_name}: {snippet}",
                        )
                    except Exception:
                        log.debug("Failed to log chatter context", exc_info=True)
            return

        # Ambient chatter: ~60% sample
        if len(message.content) > 15 and random.random() < 0.60:
            if _rate_ok(message.guild.id, message.channel.id):
                snippet = sanitize_context_snippet(message.content, limit=120)
                if snippet and len(snippet) >= 10:
                    try:
                        await self.bot.db.log_channel_context(
                            message.guild.id, message.channel.id, message.author.id,
                            "chat", f"{message.author.display_name}: {snippet}",
                        )
                    except Exception:
                        log.debug("Failed to log ambient context", exc_info=True)

    # ── Periodic tasks ────────────────────────────────────────────────────────

    @tasks.loop(hours=6)
    async def _prune_task(self) -> None:
        """Prune channel_context DB entries older than 90 days, and purge stale
        in-memory dicts that act as Redis fallback."""
        try:
            deleted = await self.bot.db.prune_old_channel_context(days=90)
            if deleted > 0:
                log.info("Pruned %d old channel_context entries", deleted)
        except Exception:
            log.debug("Channel context prune failed", exc_info=True)

        try:
            emoji_deleted = await self.bot.db.prune_old_emoji_usage(days=30)
            if emoji_deleted > 0:
                log.info("Pruned %d old guild_emoji_usage entries", emoji_deleted)
        except Exception:
            log.debug("Emoji usage prune failed", exc_info=True)

        # Purge stale in-memory greeting/proactive entries (Redis fallback cleanup)
        today = date.today()
        stale_greet = [k for k, v in _greeted_today.items() if v < today]
        for k in stale_greet:
            _greeted_today.pop(k, None)

        cutoff_ts = time.time() - _PROACTIVE_CHANNEL_COOLDOWN
        stale_pro = [k for k, v in _last_proactive.items() if v < cutoff_ts]
        for k in stale_pro:
            _last_proactive.pop(k, None)

        ambient_cutoff_ts = time.time() - _AMBIENT_CHANNEL_COOLDOWN
        stale_amb = [k for k, v in _last_ambient.items() if v < ambient_cutoff_ts]
        for k in stale_amb:
            _last_ambient.pop(k, None)

        if stale_greet or stale_pro or stale_amb:
            log.debug(
                "Pruned %d greeted / %d proactive / %d ambient in-memory entries",
                len(stale_greet), len(stale_pro), len(stale_amb),
            )

    @_prune_task.before_loop
    async def _before_prune(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Discoin) -> None:
    await bot.add_cog(SocialContext(bot))
