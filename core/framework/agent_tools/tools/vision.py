"""
core/framework/agent_tools/tools/vision.py -- vision tools for the agent loop.

Purpose: the chat orchestrator (OpenRouter Gemma 4b or whatever TOOLS_BACKEND
is set to) is not necessarily vision-capable, and hosted Ollama Turbo refuses
multimodal requests that use remote image URLs. To bridge that, the agent
bridge strips ``image_url`` blocks from the convo before dispatch and replaces
them with ``[ATTACHMENT: <url>]`` text markers. The chat model then invokes
this tool against the URL to get a real description via the vision-capable
Ollama endpoint (which accepts base64 data URIs).

Flow:
  1. User attaches an image to a Discord message.
  2. help.py builds a multimodal user turn containing the image_url.
  3. ai_bridge._strip_image_blocks rewrites it to text + [ATTACHMENT: <url>].
  4. The chat model sees the marker and calls ``vision.describe_image(url=...)``.
  5. This tool fetches the bytes, base64-encodes them into a data URI,
     and calls ``complete_ollama_vision`` which posts to the Ollama
     OpenAI-compat vision endpoint.
  6. The returned description flows back into the agent loop as a
     ``role=tool`` turn so the chat model can compose its final reply.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from urllib.parse import urlparse

import aiohttp

from core.config import Config
from core.framework.ai.client import complete as _ai_complete
from core.framework.ai.client import complete_ollama_vision

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.vision")


# ── limits ───────────────────────────────────────────────────────────────────

# Max image size we'll pull off the network and hand to the vision model.
# 8 MiB is comfortably under Discord's 25 MiB attachment limit and more
# than enough pixels for any vision model to work with.
_IMAGE_MAX_BYTES = 8 * 1024 * 1024

# Allowed image content types. Anything else (svg, tiff, etc.) is rejected
# so the vision model never gets asked to interpret something it wasn't
# trained on. We accept the common Discord-friendly set.
_IMAGE_ALLOWED_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
}

# Hosts we'll pull image bytes from. Discord CDN is the primary source;
# the others catch forum/avatar edge cases and a couple of commonly linked
# image hosts. Never a wildcard -- image downloads are a server-side
# network egress tool, so we gate them just like data.web_fetch does.
_IMAGE_HOST_ALLOWLIST = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net",
    "i.imgur.com",
    "imgur.com",
    "pbs.twimg.com",
    "user-images.githubusercontent.com",
    "raw.githubusercontent.com",
}

_IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)

_DEFAULT_VISION_PROMPT = (
    "Describe this image in detail. What is shown? Who or what is in it, "
    "what are they doing, and what does the setting look like? If there's "
    "any text in the image, read it verbatim. Keep the description factual "
    "-- no speculation about intent."
)


# ── vision.describe_image ────────────────────────────────────────────────────

@tool(
    name="vision.describe_image",
    summary=(
        "Describe an image attachment. Takes an HTTPS URL (as seen in a "
        "[ATTACHMENT: <url>] marker in the chat), downloads the bytes, "
        "base64-encodes them, and routes the image through the Ollama "
        "vision backend. Returns a natural-language description of what "
        "the image shows. Call this whenever you need to actually SEE an "
        "attachment to answer the player -- the base chat model cannot "
        "read image URLs on its own."
    ),
    risk=RiskLevel.READ,
    category="vision",
    cooldown_s=3,
    params=[
        ParamSpec(
            "url", "str",
            description=(
                "HTTPS URL of the image to describe. Usually pulled from a "
                "[ATTACHMENT: <url>] marker in the user's message."
            ),
        ),
        ParamSpec(
            "prompt", "str", required=False, default=_DEFAULT_VISION_PROMPT,
            description=(
                "Optional focus prompt. Override this when you need the "
                "vision model to answer a specific question about the "
                "image instead of giving a generic description."
            ),
        ),
    ],
)
async def describe_image(ctx: ToolContext, args: dict) -> ToolResult:
    url = str(args.get("url") or "").strip()
    if not url:
        return ToolResult.fail("empty_url")
    if not url.startswith("https://"):
        return ToolResult.fail("url_must_be_https")

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in _IMAGE_HOST_ALLOWLIST:
        return ToolResult.fail(
            f"host_not_allowed: {host} not in image allowlist"
        )

    prompt = str(args.get("prompt") or _DEFAULT_VISION_PROMPT)

    # Download
    try:
        async with aiohttp.ClientSession(timeout=_IMAGE_TIMEOUT) as sess:
            async with sess.get(
                url, headers={"User-Agent": "Discoin-Agent/1.0"},
            ) as r:
                if r.status != 200:
                    return ToolResult.fail(f"http_{r.status}")
                raw_mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if raw_mime not in _IMAGE_ALLOWED_MIMES:
                    return ToolResult.fail(
                        f"unsupported_content_type: {raw_mime or 'unknown'}"
                    )
                body = await r.content.read(_IMAGE_MAX_BYTES + 1)
                if not body:
                    return ToolResult.fail("empty_body")
                if len(body) > _IMAGE_MAX_BYTES:
                    return ToolResult.fail(
                        f"image_too_large: >{_IMAGE_MAX_BYTES} bytes"
                    )
    except asyncio.TimeoutError:
        return ToolResult.fail("timeout")
    except Exception as exc:
        log.info("[vision.describe_image] download %s", exc)
        return ToolResult.fail(f"download_error: {type(exc).__name__}")

    # Normalise image/jpg -> image/jpeg so the OpenAI data URI spec is happy.
    mime = "image/jpeg" if raw_mime == "image/jpg" else raw_mime

    # Magic-byte check: trust the actual bytes over the HTTP Content-Type
    # header. Discord CDN occasionally mislabels images, and some upstream
    # proxies rewrite headers; a vision provider that receives a
    # "data:image/png" URI whose payload is actually a JPEG (or vice
    # versa) returns "unsupported image" and wastes the call. If the
    # magic doesn't match any supported format, bail out now with a
    # clear failure instead of paying the API round-trip.
    _MAGIC = {
        "image/png":  (b"\x89PNG\r\n\x1a\n",),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/gif":  (b"GIF87a", b"GIF89a"),
        "image/webp": None,  # special-cased below (RIFF...WEBP)
    }
    def _sniff(sample: bytes) -> str | None:
        if not sample:
            return None
        if sample.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if sample[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if sample[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
            return "image/webp"
        return None

    sniffed = _sniff(body[:16])
    if sniffed is None:
        log.info(
            "[vision.describe_image] unrecognised magic bytes "
            "(header said %s, first 16B: %s) -- refusing to forward",
            raw_mime, body[:16].hex(),
        )
        return ToolResult.fail(f"invalid_image_bytes: header={raw_mime}")
    if sniffed != mime:
        # Trust the bytes. Happens when the CDN lies about the type
        # (e.g. serves a JPEG as image/png).
        log.info(
            "[vision.describe_image] MIME mismatch: header=%s magic=%s -- using magic",
            mime, sniffed,
        )
        mime = sniffed

    b64 = base64.b64encode(body).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"

    # Env-wins policy: the operator's env vars are the canonical source of
    # truth. Guild ``,ai model set vision`` only takes effect when the
    # corresponding env var is empty/unset, so a noisy Discord pick (or a
    # text-only model accidentally selected for vision) cannot override
    # what the operator deliberately configured in Railway/Docker.
    _guild_vision_model: str | None = None
    _guild_vision_provider: str | None = None
    try:
        from core.framework.ai.models import (
            get_guild_default as _ai_get_guild_default,
            is_vision_capable_slug as _is_vision_capable,
        )
        _guild_pick = await _ai_get_guild_default(ctx.db, ctx.guild_id, "vision")
        if _guild_pick and _guild_pick.model:
            _guild_vision_model = _guild_pick.model
            _guild_vision_provider = _guild_pick.provider
    except Exception:
        _is_vision_capable = lambda _m: False  # noqa: E731 -- import-failure shim

    # Drop the guild pick entirely if it doesn't look multimodal. Trying a
    # text-only slug for vision wastes a request and almost always returns
    # a hallucinated "I can't see an image". Logging the skip helps the
    # operator notice when an admin has fat-fingered ``,ai model set vision``
    # to a non-multimodal model.
    if _guild_vision_model and not _is_vision_capable(_guild_vision_model):
        log.info(
            "[vision.describe_image] ignoring guild vision pick %r "
            "(provider=%s) -- slug is not on the known-multimodal list; "
            "falling through to env-default backend and fallback chain",
            _guild_vision_model, _guild_vision_provider,
        )
        _guild_vision_model = None
        _guild_vision_provider = None

    # Resolve effective backend: VISION_BACKEND env var ALWAYS wins.
    # Only when the env var is unset do we fall back to the guild pick's
    # provider, and only when both are missing do we default to ollama.
    if Config.VISION_BACKEND:
        _backend = Config.VISION_BACKEND
    elif _guild_vision_provider in ("openrouter", "ollama"):
        _backend = _guild_vision_provider
    else:
        _backend = "ollama"

    description: str | None = None

    if _backend == "ollama":
        # Env-wins for the Ollama vision model too: Config.VISION_MODEL takes
        # priority, the guild pick only fills in when the env var is unset.
        # complete_ollama_vision applies its own final fallback (Config.TOOLS_MODEL,
        # then "gemma3:27b") if model is None.
        _ollama_vision_model = Config.VISION_MODEL or (
            _guild_vision_model if _guild_vision_provider == "ollama" else None
        )
        # Ollama path: post base64 data URI to the vision endpoint.
        # complete_ollama_vision returns None on HTTP non-200 (logged at
        # that level with model + image size), so no exception fires
        # here for HTTP 500s -- description stays None and the
        # OpenRouter fallback below takes over automatically.
        try:
            description = await complete_ollama_vision(
                prompt=prompt,
                image_data_uri=data_uri,
                model=_ollama_vision_model,
            )
            if not description:
                log.info(
                    "[vision.describe_image] ollama returned empty (model=%s, img=%skB); "
                    "trying OpenRouter fallback",
                    _ollama_vision_model or "<default>", len(data_uri) // 1024,
                )
        except Exception as exc:
            log.warning(
                "[vision.describe_image] ollama crashed (model=%s), falling back to OpenRouter: %s",
                _ollama_vision_model or "<default>", exc,
            )

    # OpenRouter path (primary when backend != ollama, or as Ollama fallback).
    # Iterates through configured vision models until one returns a description.
    # Each attempt tries the original Discord HTTPS URL FIRST (most providers
    # handle remote URLs better than data URIs and Discord CDN tokens stay
    # valid ~24h) and only falls back to the base64 data URI if the URL form
    # fails.
    #
    # Model resolution order (env-wins):
    #   1. Legacy Config.OPENROUTER_VISION_MODEL env, prepended if set.
    #   2. Config.OPENROUTER_VISION_MODELS env (comma-separated). Defaults
    #      cover three independent upstreams so a single-provider outage
    #      never takes vision down for the whole bot.
    #   3. Guild-picked model from ``,ai model set vision <model>``, IF it's
    #      a multimodal slug AND no env-set models above are available.
    #      The guild pick can only fill in where the operator left a hole.
    if not description:
        candidate_models: list[str] = []
        if Config.OPENROUTER_VISION_MODEL:
            candidate_models.append(Config.OPENROUTER_VISION_MODEL)
        for raw in (Config.OPENROUTER_VISION_MODELS or "").split(","):
            slug = raw.strip()
            if slug and slug not in candidate_models:
                candidate_models.append(slug)
        # Guild pick is only consulted when env supplied nothing. The guild
        # pick already passed the multimodal capability filter above, so we
        # don't re-check here.
        if not candidate_models and _guild_vision_provider == "openrouter" and _guild_vision_model:
            candidate_models.append(_guild_vision_model)
        if not candidate_models:
            candidate_models = ["google/gemini-2.5-flash"]

        # Two URL forms per model: original Discord URL first (lighter,
        # faster, no base64 inflation) then data URI fallback. detail:auto
        # tells providers to pick a sensible token budget instead of the
        # high-detail default, which some providers reject for very small
        # images.
        url_forms: list[tuple[str, str]] = [
            ("https_url", url),
            ("data_uri", data_uri),
        ]

        last_error: str | None = None
        for model_slug in candidate_models:
            for form_name, image_url in url_forms:
                try:
                    fallback_msgs = [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}},
                            ],
                        }
                    ]
                    log.info(
                        "[vision.describe_image] OpenRouter try (model=%s, form=%s, mime=%s, %skB)",
                        model_slug, form_name, mime, len(body) // 1024,
                    )
                    description = await _ai_complete(
                        fallback_msgs, max_tokens=400, temperature=0.2,
                        model=model_slug,
                    )
                    if description:
                        break  # success: stop trying more URL forms
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log.warning(
                        "[vision.describe_image] OpenRouter exception (model=%s, form=%s): %s",
                        model_slug, form_name, exc,
                    )
            if description:
                break  # success: stop trying more models

        if not description:
            log.warning(
                "[vision.describe_image] all OpenRouter vision models failed "
                "(tried %d model(s) x 2 URL forms; last_error=%s)",
                len(candidate_models), last_error or "none",
            )

    if not description:
        return ToolResult.fail("vision_returned_empty")

    return ToolResult.success({
        "url": url,
        "host": host,
        "mime": mime,
        "bytes": len(body),
        "description": description[:2000],
    })
