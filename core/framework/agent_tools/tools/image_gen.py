"""
core/framework/agent_tools/tools/image_gen.py -- AI image generation tool.

Registered only when IMAGE_GEN_ENABLED=true.

Uses OpenRouter's /api/v1/chat/completions endpoint with IMAGE_GEN_MODEL
(default: black-forest-labs/flux-schnell). Any OpenRouter image model works:
  black-forest-labs/flux-schnell      fast, cheap, good quality
  black-forest-labs/flux-1.1-pro      higher quality, costs more
  stabilityai/stable-diffusion-3-5-large

OpenRouter image models are called via the standard chat completions API
(not /v1/images/generations which does not exist on OpenRouter). The model
returns the generated image as a URL inside the message content. We extract
the https:// URL from the content and return it.

The tool returns the generated image URL in ToolResult.data["url"].
The ai_bridge picks this up and yields an "image_generated" event so the
Discord layer can send the image alongside the AI text reply.
"""
from __future__ import annotations

import logging
import re

import aiohttp

from core.config import Config
from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.image_gen")

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=90, connect=8, sock_read=82)
_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Matches the first https URL in the model's response content.
_URL_RE = re.compile(r"https://\S+")


# Registered UNCONDITIONALLY so the chat model always sees image.generate in
# its tool catalog and can advertise it to players. When IMAGE_GEN_ENABLED is
# false (or OPENROUTER_API_KEY is missing), the tool still executes but
# returns a clean failure immediately -- far better than the old behavior
# where the tool disappeared from the catalog entirely and the chat model
# would spin trying to answer image requests with no tool to call, eating
# the whole orchestrator iteration budget before giving up.
@tool(
    name="image.generate",
    summary=(
        "Generate an image from a text description using AI. "
        "Returns a URL to the generated image. Use this when the player "
        "explicitly asks you to generate, create, draw, or make an image "
        "of something. Keep prompts descriptive but concise (under 200 words). "
        "Do not generate explicit, violent, hateful, or NSFW content."
    ),
    risk=RiskLevel.READ,
    category="image",
    cooldown_s=15,
    params=[
        ParamSpec(
            "prompt", "str",
            description=(
                "Detailed description of the image to generate. "
                "Include style, subject, composition, and mood. "
                "More detail = better results."
            ),
        ),
        ParamSpec(
            "size", "str", required=False, default="1024x1024",
            choices=["256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"],
            description="Image dimensions. Default: 1024x1024.",
        ),
    ],
)
async def generate(ctx: ToolContext, args: dict) -> ToolResult:
    # Enabled-check lives inside the tool body so the registration is
    # always visible; the chat model can explain *why* it can't make an
    # image instead of pretending no such tool exists.
    if not Config.IMAGE_GEN_ENABLED:
        return ToolResult.fail(
            "image_gen_disabled: operator has IMAGE_GEN_ENABLED=false",
        )

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return ToolResult.fail("empty_prompt")
    if len(prompt) > 2000:
        prompt = prompt[:2000]

    size = str(args.get("size") or "1024x1024")

    key = Config.OPENROUTER_API_KEY
    if not key:
        return ToolResult.fail("openrouter_key_not_configured")

    # Resolve guild image model (set via ,ai model set image).
    _guild_image_model: str | None = None
    try:
        from core.framework.ai.models import get_guild_default as _ai_get_guild_default
        _guild_pick = await _ai_get_guild_default(ctx.db, ctx.guild_id, "image")
        if _guild_pick and _guild_pick.model:
            _guild_image_model = _guild_pick.model
    except Exception:
        pass
    model = _guild_image_model or Config.IMAGE_GEN_MODEL or "black-forest-labs/flux-schnell"

    # OpenRouter image models use the standard chat completions API.
    # The size hint is passed as part of the prompt context.
    payload: dict = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": f"{prompt}\n\nSize: {size}",
            }
        ],
    }

    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as sess:
            async with sess.post(
                _OPENROUTER_CHAT_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "https://econbot",
                    "X-Title": "Discoin",
                },
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("[image.generate] HTTP %s: %.300s", r.status, body)
                    return ToolResult.fail(f"image_gen_http_{r.status}")
                data = await r.json()
    except Exception as exc:
        log.warning("[image.generate] request failed: %s", exc)
        return ToolResult.fail(f"image_gen_error: {type(exc).__name__}")

    # Extract the image URL from the chat response content.
    # FLUX models on OpenRouter return the URL as plain text or inside
    # a markdown image block (![...](url)) in the message content.
    choices = data.get("choices") or []
    if not choices:
        log.warning("[image.generate] empty choices in response: %s", data)
        return ToolResult.fail("image_gen_empty_response")

    content = (choices[0].get("message") or {}).get("content") or ""
    content = content.strip()

    # Try to pull an https URL out of the content.
    match = _URL_RE.search(content)
    img_url = match.group(0).rstrip(")>\"'") if match else ""

    # Some models return a bare URL as the entire content.
    if not img_url and content.startswith("https://"):
        img_url = content.split()[0]

    if not img_url:
        log.warning("[image.generate] could not extract URL from content: %.200s", content)
        return ToolResult.fail("image_gen_no_url")

    log.info("[image.generate] model=%s size=%s uid=%s", model, size, ctx.user_id)
    return ToolResult.success({
        "url": img_url,
        "model": model,
        "size": size,
        "prompt": prompt,
    })
