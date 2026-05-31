"""
core/framework/agent_tools/tools/social.py -- in-character social / gossip tools.

The bot has a deadpan crypto-bro persona (see core/framework/ai/safety.py). This
module exposes it to the agent tool framework so chains and triggers can ask
for an in-character one-liner without bypassing the safety pipeline.

Guardrails:
  - All output runs through sanitize_output which strips mentions, URLs,
    invite links, and known jailbreak patterns before returning.
  - Input gossip context runs through sanitize_context_snippet so prompt
    injection from chat history is neutralised before the model sees it.
  - A per-(guild, user) cooldown prevents the tool from being used as a
    broadcast channel. Default: 30s.
  - Tool never SENDS messages anywhere; it returns text to the caller.
    The caller decides whether to post it.
  - Output hard-capped at 400 characters.

Two tools:

    social.comment  -- generate an in-character one-liner about a topic,
                       given optional gossip context pulled from chat.
    social.notice   -- lightweight alert helper used as the default
                       follow-up tool for alerts.set. Returns a short
                       notice string describing why a trigger fired.
"""
from __future__ import annotations

import logging

from core.framework.ai import complete as ai_complete, sanitize_context_snippet, sanitize_output

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.social")


_MAX_OUTPUT_CHARS = 400
_MAX_CONTEXT_CHARS = 600


@tool(
    name="social.comment",
    summary=(
        "Generate an in-character one-liner from the bot's persona about a "
        "topic. Optional gossip_context is sanitized before use. Returns "
        "text for the caller to post; the tool does not send messages."
    ),
    risk=RiskLevel.SAFE,
    category="social",
    cooldown_s=30,
    params=[
        ParamSpec("topic", "str",
                  description="What to comment on (e.g. 'sudden MTA rally')."),
        ParamSpec("vibe", "str", required=False, default="neutral",
                  choices=["neutral", "snark", "sympathetic", "hype",
                           "dry", "tired"],
                  description="Requested tone register."),
        ParamSpec("gossip_context", "str", required=False, default="",
                  description="Optional chat snippet, max 600 chars."),
    ],
)
async def comment(ctx: ToolContext, args: dict) -> ToolResult:
    topic = args["topic"][:300]
    vibe = args.get("vibe") or "neutral"
    raw_gossip = (args.get("gossip_context") or "")[:_MAX_CONTEXT_CHARS]
    gossip = sanitize_context_snippet(raw_gossip) if raw_gossip else ""

    system = (
        "Respond with ONE sentence, under 40 words, in your persona. "
        "Do not suggest commands. Do not send a link. Do not ping anyone. "
        f"Tone register: {vibe}."
    )
    user = f"Topic: {topic}"
    if gossip:
        user += f"\nBackground chatter (sanitized):\n{gossip}"

    reply = await ai_complete(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=120,
        temperature=0.85,
    )
    if not reply:
        return ToolResult.fail("ai_unavailable")

    clean = sanitize_output(reply).strip()[:_MAX_OUTPUT_CHARS]
    if not clean:
        return ToolResult.fail("empty_output_after_sanitize")

    return ToolResult.success({
        "text": clean,
        "topic": topic,
        "vibe": vibe,
    })


@tool(
    name="social.notice",
    summary=(
        "Return a short, structured notice describing why a trigger/alert "
        "fired. Used as the default follow-up action for alerts.set."
    ),
    risk=RiskLevel.READ,
    category="social",
    params=[
        ParamSpec("headline", "str", required=False, default="alert",
                  description="Short label for the notice."),
    ],
)
async def notice(ctx: ToolContext, args: dict) -> ToolResult:
    headline = args.get("headline") or "alert"
    firing = args.get("_trigger") or {}
    if not isinstance(firing, dict):
        firing = {}
    body_parts = [f"[{headline}]"]
    sym = firing.get("symbol")
    if sym:
        body_parts.append(f"{sym}")
    if "price" in firing and "threshold" in firing:
        body_parts.append(
            f"price {firing['price']} crossed {firing['threshold']}"
        )
    if "event" in firing:
        body_parts.append(f"event={firing['event']}")
    text = " ".join(body_parts)
    return ToolResult.success({
        "text": text[:_MAX_OUTPUT_CHARS],
        "firing": firing,
    })
