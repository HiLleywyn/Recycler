"""Repair malformed and hallucinated custom-emoji markup in AI replies.

Discord renders a *complete* custom emoji `<:name:id>` / `<a:name:id>` only
when the snowflake id matches an emoji the bot can actually access. Any of
the following slip past the model and end up displayed as raw `<` `:` `id` `>`
garbage in chat:

  * the model writes the opening `<:` but never closes the `>` because it
    ran out of tokens or just forgot
  * the model writes the closing `>` but the snowflake id is invented (does
    not exist on this guild)
  * the model writes a complete emoji from a guild the bot cannot see

This module provides a single function -- :func:`repair_custom_emojis` --
that walks the text once, drops any unclosed markup it finds, and then drops
any closed markup whose id does not appear in the supplied guild's emoji
roster. Callers pass the live ``discord.Guild`` so the allowlist is always
fresh; passing ``None`` skips the id-validation step but still strips the
unclosed garbage.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    import discord


# Closed custom emoji: `<:name:id>` or `<a:name:id>`. Snowflakes are 17-20
# digits but accept 15-20 to tolerate the rare short id. Names allow 1-32
# chars (Discord's own validator: ``[A-Za-z0-9_]+`` length 2-32, but a few
# legacy emojis exist with single-char names).
_CLOSED_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{1,32}):(\d{15,20})>")

# Custom emoji wrapped in inline code -- ``\`<:name:id>\``` -- which Discord
# renders as literal text inside a monospace span instead of as the actual
# emoji image. The model picks this up when it sees backticked example
# markup in the system prompt and reproduces the wrapper in its reply. We
# unwrap to plain markup so Discord renders the emoji properly. Both single
# and double backticks are unwrapped; the inner markup is preserved
# untouched so the id-validation pass below can still drop unknown ids.
_BACKTICKED_EMOJI_RE = re.compile(
    r"`{1,2}(<a?:[A-Za-z0-9_]{1,32}:\d{15,20}>)`{1,2}"
)

# Anything that looks like it WANTED to be an emoji but is missing the
# closing `>`. This is the bug behind `<a:Clown:1468240185509544153 Absolute
# not.` from production -- the model emitted the markup mid-sentence and
# Discord rendered the literal text. We match an optional leading space, the
# opening `<`, and the `a?:name:digits` payload up to either:
#   * any non-`>` whitespace (model continued typing prose)
#   * end of string
# Capturing the leading space too so we don't leave a doubled space behind
# when the broken emoji sat between two words.
_UNCLOSED_EMOJI_RE = re.compile(
    r" ?<a?:[A-Za-z0-9_]{1,32}:\d{0,20}(?=$|[\s.,!?;:\"')\]}\n])"
)


def _guild_emoji_ids(guild: "discord.Guild | None") -> set[str]:
    """Return the set of emoji snowflake ids the guild exposes, as strings."""
    if guild is None:
        return set()
    try:
        return {str(e.id) for e in guild.emojis if getattr(e, "available", True)}
    except Exception:
        return set()


def _bot_accessible_emoji_ids(guild: "discord.Guild | None") -> set[str]:
    """Union of guild emojis and any other emojis the bot can render.

    If the guild handle exposes a parent client (``.guild._state._emojis``
    or ``client.emojis``), those ids count as renderable too -- the bot can
    paste any emoji from a server it shares with the user. We only fall
    back to this when the guild lookup itself returned a result, otherwise
    we trust the caller and skip validation entirely.
    """
    ids = _guild_emoji_ids(guild)
    if not ids or guild is None:
        return ids
    client = getattr(guild, "_state", None)
    if client is not None:
        cache = getattr(client, "_emojis", None)
        if cache:
            try:
                ids.update(str(eid) for eid in cache.keys())
            except Exception:
                pass
    return ids


def repair_custom_emojis(
    text: str,
    guild: "discord.Guild | None" = None,
    *,
    extra_allowed_ids: Iterable[str] = (),
) -> str:
    """Strip unclosed and unknown custom emoji markup from ``text``.

    The two failure modes this fixes:

    1. **Unclosed markup** -- ``<a:Clown:1468240185509544153 Absolute not.``
       The opening run is dropped entirely (the surrounding text is kept).
       This runs first so a stray `<:foo:123` that happens to be followed
       by a real emoji never confuses the second pass.

    2. **Hallucinated id** -- ``<:never_existed:000000000000000000>``.
       When ``guild`` is supplied the id is checked against the guild's
       (and, where available, the bot's full) emoji roster. Unknown ids
       are removed, leaving prose intact. Pass ``extra_allowed_ids`` to
       allow specific ids the caller already validated.

    The function is idempotent: calling it twice on the same string is a
    no-op on the second pass.
    """
    if not text or "<" not in text:
        return text

    # Pass 0: unwrap any custom emoji markup that landed inside an inline
    # code span. Discord renders backticked text in monospace and skips
    # emoji rendering, so `<:peepoSip:1468...>` shows up as literal
    # `<:peepoSip:1468...>` characters in the channel. The model picks
    # up the wrapper from backticked example markup in the system prompt
    # and copies it into its reply; we strip the backticks here so the
    # actual emoji renders.
    text = _BACKTICKED_EMOJI_RE.sub(r"\1", text)

    # Pass 1: kill any unclosed `<a?:name:digits` that does NOT have a
    # closing `>` before the next whitespace / punctuation. We do this in
    # a loop because two adjacent broken emojis are possible in pathological
    # truncated outputs.
    prev = None
    while prev != text:
        prev = text
        text = _UNCLOSED_EMOJI_RE.sub("", text)

    # Pass 2: validate ids of CLOSED emojis. Skipped when we have no guild
    # and no extra allowlist -- in that case the markup is at least valid
    # syntactically and Discord can decide what to do with it.
    allowed = _bot_accessible_emoji_ids(guild) | {str(x) for x in extra_allowed_ids}
    if not allowed:
        return text

    def _drop_unknown(m: re.Match) -> str:
        return m.group(0) if m.group(3) in allowed else ""

    text = _CLOSED_EMOJI_RE.sub(_drop_unknown, text)
    return text
