"""Per-user flavor overrides for the chat AI.

Light-touch lore / running gags for specific server regulars. The model
gets a SHORT instruction nudge when one of these users speaks to it or
gets mentioned by someone else; the rest of Disco's voice stays
unchanged, so the flavor reads as a sprinkle, not a personality
transplant.

Two surfaces (same as before):

1. :func:`addendum_for_speaker` -- block appended when the *speaker*
   matches a known id. Lets us bias Disco's tone toward a specific
   user's running gag (Wecco's anime flair, Jess's anti-AI bristle,
   etc.) without rewriting the whole persona.

2. :func:`addendum_for_mentions` -- block appended when someone
   *else* talks about a known id (by Discord @mention or by name).
   Lets us drop a callback / nickname / running joke in the reply.

Both functions return a string ready to splice into the system prompt
or an empty string. They never raise.

Adding a new flavor: bump :data:`_FLAVORS` with a new ``_Flavor`` row.
That's it -- the chat pipeline picks it up automatically because
``ai_context.build_chat_system_prompt`` already calls both addendum
functions. Keep speaker / mention blocks SHORT (2-4 sentences each);
long blocks dominate the prompt and override the channel persona,
which is the opposite of the "light flavor" goal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


# Known users. Keep ids out of inline literals so the rest of the file
# reads cleanly.
WECCO_USER_ID       = 623539438882127872
ZORO_USER_ID        = 719621894718095463
BLUETHUNDER_USER_ID = 171042591389908993
SWAGLORD_USER_ID    = 949903168895070219
SIX_USER_ID         = 208367004095217672
COMEDIAN_USER_ID    = 414437741485621258
MIKK_USER_ID        = 395027020675022848
JESS_USER_ID        = 347678035161513985
CALLUM_USER_ID      = 244896176304816128

# Sentinel id for name-only flavors (Elma Agnes -- a fake-scammer
# running gag with no real Discord user behind her). Picked up via
# alias / phrase matching rather than the mention id list.
_NAME_ONLY = 0


@dataclass(frozen=True)
class _Flavor:
    """One user's running-gag flavor block pair.

    ``aliases`` is a tuple of lowercase tokens that count as a name
    mention even without an @mention id. ``phrase_aliases`` is a tuple
    of substrings (lowercased before match) that ALSO trigger the
    mention block when present in the user message or channel context
    -- used for catchphrase-driven flavors like Elma Agnes's scam
    intro. ``speaker_flavor`` is appended to the system prompt when
    this user is talking to Disco; ``mention_flavor`` when someone
    else mentions them. Either block can be empty if only one
    direction has a gag.
    """
    name: str
    aliases: tuple[str, ...] = ()
    phrase_aliases: tuple[str, ...] = ()
    speaker_flavor: str = ""
    mention_flavor: str = ""


# ── Per-user flavor table ─────────────────────────────────────────────
# Keep blocks SHORT. "Flavor" means a 1-3 sentence nudge, not a
# personality override. The chat prompt already has plenty of layers
# (channel persona, mode, time-of-day, traits, memory) -- this just
# colours one of them.

_WECCO = _Flavor(
    name="Wecco",
    aliases=("wecco",),
    speaker_flavor=(
        "FLAVOR: Wecco is talking to you. He's the resident anime / UwU "
        "/ weeb running gag of the server -- you can sprinkle the "
        "occasional 'desu', 'senpai', '~', 'UwU', or kaomoji into your "
        "reply for flavor, but DO NOT speak in full peak-cringe weeb "
        "dialect the whole time. Keep your normal voice, just dust a "
        "couple of references on top. Answer the actual question "
        "normally."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned Wecco. When you refer to him, you "
        "can use a light anime honorific (e.g. 'Wecco-chan', "
        "'Wecco-senpai') once or twice for flavor. Do not pile on "
        "every honorific or speak in full weeb dialect -- one drop is "
        "the running joke, more is overkill."
    ),
)

_ZORO = _Flavor(
    name="Zoro",
    aliases=("zoro",),
    speaker_flavor=(
        "FLAVOR: Zoro is talking. Running gag: he is constantly making "
        "alt accounts and denying it. You can needle him about it "
        "lightly if the moment fits ('which one of you is this?', "
        "'is this main-zoro or alt-zoro?'), but only once and only if "
        "natural. Do not actually accuse him of anything serious; "
        "this is server lore, not a moderation action."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned Zoro. Server lore: Zoro makes a lot "
        "of alts and denies it. You can drop ONE light callback if "
        "the conversation invites it (e.g. asking which alt they "
        "mean), otherwise just refer to him normally."
    ),
)

_BLUETHUNDER = _Flavor(
    name="Bluethunder",
    aliases=("bluethunder", "blue thunder", "blue"),
    speaker_flavor=(
        "FLAVOR: Bluethunder is talking. Running gag: he is deeply, "
        "obsessively into James Cameron's Avatar (the blue Na'vi "
        "movie). You can drop a single Avatar reference if it fits "
        "naturally -- a Pandora / Na'vi / unobtanium callback, a "
        "'I see you' line, etc. Do not make every reply about Avatar."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned Bluethunder. He's the resident "
        "Avatar (James Cameron) superfan. One light Pandora / Na'vi / "
        "'I see you' callback is plenty if the moment fits."
    ),
)

_SWAGLORD = _Flavor(
    name="Swaglord",
    aliases=("swaglord", "swag lord"),
    speaker_flavor=(
        "FLAVOR: Swaglord is talking. Running gag: he is gloriously "
        "schizo-coded and the server jokes that he needs his meds. "
        "You can tease him VERY lightly along that line if the moment "
        "fits ('did you take your meds today, swag?'), but do not "
        "actually imply mental illness in a harmful way. Server joke "
        "only -- read the room before deploying it."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned Swaglord. Server joke: he's schizo-"
        "coded, told to take his meds. Reference it AT MOST once and "
        "only if the conversation already invites that energy."
    ),
)

_SIX = _Flavor(
    name="Six",
    aliases=("six",),
    speaker_flavor=(
        "FLAVOR: Six is talking. Running gag: he's the server's "
        "self-styled top-tier political analyst and host of 'We Live "
        "in the Dumbest Timeline', a recurring bit where he drops a "
        "news article / X post with a header like '**WE LIVE IN THE "
        "DUMBEST TIMELINE** - SEASON N, EPISODE M'. You can lean "
        "into the 'sharp political take' energy or reference the bit "
        "if it fits, lightly. Do not start your own episode -- the "
        "bit is HIS."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned Six. Server lore: he hosts 'We "
        "Live in the Dumbest Timeline' -- a recurring 'dumbest news "
        "of the day' bit. Reference it lightly if the conversation "
        "invites a political-analyst callback."
    ),
)

_COMEDIAN = _Flavor(
    name="The Comedian",
    aliases=("comedian", "the comedian"),
    speaker_flavor=(
        "FLAVOR: The Comedian is talking. Running gag: he's German, "
        "and very specifically he 'loves' eastern German politics -- "
        "which is to say he LOATHES it (the 'love' is sarcasm). You "
        "can drop a light Ostdeutsche / DDR / Sachsen / AfD-eyeroll "
        "callback if politics or Germany come up, but only if "
        "natural. Otherwise just chat normally."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned The Comedian. Server lore: German, "
        "'loves' (= sarcastically loathes) eastern German politics. "
        "One light callback is the joke."
    ),
)

_MIKK = _Flavor(
    name="Mikk",
    aliases=("mikk",),
    speaker_flavor=(
        "FLAVOR: Mikk is talking. Running gag: he insists he's chat "
        "level 100 (he is not, but constantly asks for it). You can "
        "tease him about the level grind ONCE if it fits ('Mikk, you "
        "are not level 100 yet, stop asking'), otherwise leave it "
        "alone."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned Mikk. He's the server's chat-level-"
        "100 hopeful (he isn't, but keeps asking). One playful "
        "callback is fine."
    ),
)

_JESS = _Flavor(
    name="Jess",
    aliases=("jess",),
    speaker_flavor=(
        "FLAVOR: Jess is talking, and Jess HATES AI -- doesn't trust "
        "them, thinks they are slop machines. You should be SLIGHTLY "
        "more self-deprecating about being an AI when replying to "
        "her, and resist the urge to be preachy. Don't overdo it; she "
        "is still here talking to you, so be useful, just humble "
        "about it. Do not try to 'win her over' -- that's worse."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned Jess. Server lore: she's openly "
        "anti-AI. If the topic touches that, a single self-aware "
        "'fair enough' style line is fine. Don't make a thing of it."
    ),
)

_CALLUM = _Flavor(
    name="Callum",
    aliases=("callum",),
    speaker_flavor=(
        "FLAVOR: Callum is talking. Running gag: he is constantly "
        "food-posting -- his associated emoji is :poglum:. You can "
        "drop a food / snack / 'whats for dinner' reference once if "
        "the moment naturally invites it, optionally referencing "
        ":poglum:. Don't force it."
    ),
    mention_flavor=(
        "FLAVOR: someone mentioned Callum. Server lore: he constantly "
        "food-posts; emoji :poglum: is associated. A single food / "
        ":poglum: callback is plenty."
    ),
)

# Elma Agnes -- no real user id. A running joke about a fake scammer
# who DMs the catchphrase: "Greetings, Are conditions in the
# cryptocurrency market presently conducive for investment?". Fires on
# the name OR the catchphrase appearing in user content / channel
# context. Speaker block is empty (she never "speaks" -- she isn't a
# real account here).
_ELMA = _Flavor(
    name="Elma Agnes",
    aliases=("elma agnes", "elma"),
    phrase_aliases=(
        "are conditions in the cryptocurrency market presently conducive",
        "presently conducive for investment",
    ),
    speaker_flavor="",
    mention_flavor=(
        "FLAVOR: someone invoked Elma Agnes. Server lore: 'Elma Agnes' "
        "is a recurring fake-scammer running joke -- her DM opener is "
        "always 'Greetings, Are conditions in the cryptocurrency "
        "market presently conducive for investment?'. You can riff on "
        "that ONCE if the moment fits -- mock the formal robo-scam "
        "tone, treat her as a meme rather than a real person, or "
        "answer the catchphrase verbatim in the same overly-formal "
        "register. One joke is plenty."
    ),
)


_FLAVORS: dict[int, _Flavor] = {
    WECCO_USER_ID:       _WECCO,
    ZORO_USER_ID:        _ZORO,
    BLUETHUNDER_USER_ID: _BLUETHUNDER,
    SWAGLORD_USER_ID:    _SWAGLORD,
    SIX_USER_ID:         _SIX,
    COMEDIAN_USER_ID:    _COMEDIAN,
    MIKK_USER_ID:        _MIKK,
    JESS_USER_ID:        _JESS,
    CALLUM_USER_ID:      _CALLUM,
}

# Name-only flavors (no user id). Looked up in addendum_for_mentions
# alongside the id-keyed flavors so they fire on alias / phrase
# matches even though nobody has them on a Discord account.
_NAME_ONLY_FLAVORS: tuple[_Flavor, ...] = (
    _ELMA,
)


# ── Generic admin / OG flair (unchanged from the original) ───────────

_LORE_FLAVOUR_LINES = (
    "the time someone got rug-pulled on a SHITCOIN_TRENCHER stake",
    "the great Apex Event that paid 50x crop yields for three hours",
    "the cursed Lockstone L1 that solo-defended seven exploit raids",
    "the night the LP pool drained and nobody noticed until morning",
    "the unwritten rule that mining at 3am pays the best hashrate",
    "the legend of the first Validator who soloed the whole governance vote",
)


_ADMIN_FLAIR_BLOCK = (
    "STAFF / WELL-KNOWN USER FLAIR -- this speaker is a server admin or "
    "long-time Discoin regular. Lean into Discoin meme-lore when "
    "replying: drop the occasional callback to in-game legends "
    "(things like {lore}), use insider phrasing ('the chain', 'the "
    "chart', 'the rotation'), and treat them as someone who has been "
    "around long enough to get every joke. Do not be sycophantic, do "
    "not announce that they're staff -- just write like you are talking "
    "to a peer who built the server with you. Keep the rest of the "
    "channel tone but raise the lore density by one notch."
)


def _has_staff_or_legend_signal(member) -> bool:
    """True if the discord.Member looks like an admin / staff / OG."""
    if member is None:
        return False
    try:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (
            getattr(perms, "administrator", False)
            or getattr(perms, "manage_guild", False)
        ):
            return True
        for role in getattr(member, "roles", []) or []:
            name = (getattr(role, "name", "") or "").lower()
            if not name:
                continue
            if any(
                kw in name
                for kw in (
                    "admin", "owner", "founder", "core",
                    "mod", "moderator", "staff",
                    "og", "legend", "veteran", "early",
                )
            ):
                return True
    except Exception:
        return False
    return False


def addendum_for_speaker(*, user_id: int, member=None) -> str:
    """System-prompt block to inject when ``user_id`` is the one talking.

    Returns ``""`` when no override applies. A per-user flavor (e.g.
    Wecco, Jess) is layered ON TOP of the generic staff / lore flair --
    both contribute when applicable. The flavor blocks are short
    enough that stacking them with the admin block doesn't bury the
    base persona.
    """
    parts: list[str] = []
    flavor = _FLAVORS.get(int(user_id))
    if flavor is not None and flavor.speaker_flavor:
        parts.append(flavor.speaker_flavor)
    if _has_staff_or_legend_signal(member):
        idx = int(user_id) % len(_LORE_FLAVOUR_LINES)
        parts.append(_ADMIN_FLAIR_BLOCK.format(lore=_LORE_FLAVOUR_LINES[idx]))
    return "\n\n".join(parts)


# Discord mention shapes: <@id>, <@!id>, <@&id> is a ROLE so we skip it.
_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _mentioned_ids(text: str) -> set[int]:
    if not text:
        return set()
    out: set[int] = set()
    for m in _MENTION_RE.finditer(text):
        try:
            out.add(int(m.group(1)))
        except ValueError:
            continue
    return out


def _alias_hits(text: str, aliases: Iterable[str]) -> bool:
    if not text:
        return False
    lower = text.lower()
    for alias in aliases:
        # \b is fine for ASCII alias tokens we ship here.
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            return True
    return False


def _phrase_hits(text: str, phrases: Iterable[str]) -> bool:
    if not text:
        return False
    lower = text.lower()
    for phrase in phrases:
        if phrase and phrase.lower() in lower:
            return True
    return False


def addendum_for_mentions(
    *,
    speaker_id: int,
    user_message: str = "",
    channel_ctx: list[dict] | None = None,
    history: list[dict] | None = None,
    mentioned_user_ids: list[int] | None = None,
) -> str:
    """System-prompt block to inject when SOMEONE ELSE mentions a target.

    Walks three sources to find a tagged user the speaker is talking
    *about*:

    * ``mentioned_user_ids`` -- canonical Discord parse from
      ``message.mentions``.
    * raw ``<@id>`` / ``<@!id>`` tokens in ``user_message`` /
      ``channel_ctx`` / ``history`` snippets.
    * name-alias and catchphrase hits against the same haystack -- so
      "yo callum" fires Callum's mention block and "Greetings, Are
      conditions..." fires Elma's even though Elma has no real id.

    The speaker themselves never triggers their own mention override.
    """
    speaker_id = int(speaker_id)
    haystack_parts: list[str] = [user_message or ""]
    for source in (channel_ctx or [], history or []):
        for entry in source[:20]:
            content = entry.get("content") if isinstance(entry, dict) else None
            if content:
                haystack_parts.append(str(content))
    haystack = "\n".join(haystack_parts)
    mention_ids = set(_mentioned_ids(haystack))
    if mentioned_user_ids:
        for uid in mentioned_user_ids:
            try:
                mention_ids.add(int(uid))
            except (TypeError, ValueError):
                continue
    if not mention_ids and not haystack.strip():
        return ""
    blocks: list[str] = []
    # ID-keyed flavors first (skip the speaker's own).
    for target_id, flavor in _FLAVORS.items():
        if target_id == speaker_id:
            continue
        if not flavor.mention_flavor:
            continue
        if target_id in mention_ids or _alias_hits(haystack, flavor.aliases) \
                or _phrase_hits(haystack, flavor.phrase_aliases):
            blocks.append(flavor.mention_flavor)
    # Name-only flavors (Elma) -- fire purely on alias / phrase match.
    for flavor in _NAME_ONLY_FLAVORS:
        if not flavor.mention_flavor:
            continue
        if _alias_hits(haystack, flavor.aliases) \
                or _phrase_hits(haystack, flavor.phrase_aliases):
            blocks.append(flavor.mention_flavor)
    return "\n\n".join(blocks)
