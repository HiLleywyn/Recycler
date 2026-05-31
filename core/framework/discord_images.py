"""core/framework/discord_images.py  -  shared media-detection helpers for Discord messages.

Centralises extension lists, Discord media host allowlist, and URL/attachment
extraction so cogs don't drift out of sync with each other.
"""
from __future__ import annotations

IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
})

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".webm", ".avi", ".mkv",
})

AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus",
})

# Only these hosts are sent to external APIs (OpenRouter vision calls).
# Attachment URLs are always Discord CDN. Embed proxy URLs go through
# media.discordapp.net. We never forward raw third-party embed URLs.
_ALLOWED_HOSTS: tuple[str, ...] = (
    "https://cdn.discordapp.com/",
    "https://media.discordapp.net/",
    "https://images-ext-1.discordapp.net/",
    "https://images-ext-2.discordapp.net/",
)


def is_discord_media_url(url: str | None) -> bool:
    """Return True only if *url* is served from a trusted Discord media host."""
    if not url:
        return False
    return any(url.startswith(prefix) for prefix in _ALLOWED_HOSTS)


def extract_image_urls(message, *, max_images: int = 4) -> list[str]:
    """Return up to *max_images* image URLs from a Discord Message.

    Attachment URLs (always Discord CDN) are included for any image MIME type
    or recognised extension.  Embed image/thumbnail URLs are only included
    when they resolve to a trusted Discord media host  -  embed ``proxy_url``
    is preferred over the raw ``url`` because Discord rewrites external links
    through its own CDN, avoiding leaking third-party hosts to OpenRouter.

    GIFs are included  -  most vision models process them as static (first frame).
    """
    urls: list[str] = []

    for att in message.attachments:
        if len(urls) >= max_images:
            break
        if (att.content_type and att.content_type.startswith("image/")) or \
                any(att.filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
            urls.append(att.url)

    for emb in message.embeds:
        if len(urls) >= max_images:
            break
        # Prefer the Discord-proxied version; fall back to raw only if it's
        # already a trusted Discord host.
        for src in (
            getattr(getattr(emb, "image", None), "proxy_url", None),
            getattr(getattr(emb, "image", None), "url", None),
            getattr(getattr(emb, "thumbnail", None), "proxy_url", None),
            getattr(getattr(emb, "thumbnail", None), "url", None),
        ):
            if src and is_discord_media_url(src) and src not in urls:
                urls.append(src)
                break

    return urls


def extract_media_notes(message) -> str:
    """Return a plain-text description of non-image media attachments.

    Injected into the user turn so the AI knows what was attached even though
    it can't process the file directly.
    """
    notes: list[str] = []
    for att in message.attachments:
        fname = att.filename.lower()
        ct = att.content_type or ""
        if ct.startswith("video/") or any(fname.endswith(e) for e in VIDEO_EXTENSIONS):
            dur = f", {int(att.duration)}s" if getattr(att, "duration", None) else ""
            notes.append(f"[User attached a video: {att.filename}{dur}  -  you cannot watch it]")
        elif ct.startswith("audio/") or any(fname.endswith(e) for e in AUDIO_EXTENSIONS):
            dur = f", {int(att.duration)}s" if getattr(att, "duration", None) else ""
            notes.append(f"[User attached audio: {att.filename}{dur}  -  you cannot hear it]")
    return "\n".join(notes)


def has_image(message) -> bool:
    """Return True if the message contains at least one image attachment or embed."""
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            return True
        if any(att.filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
            return True
    for emb in message.embeds:
        if emb.image or emb.thumbnail:
            return True
    return False


def has_video(message) -> bool:
    """Return True if the message has a video attachment."""
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("video/"):
            return True
        if any(att.filename.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
            return True
    return False


def has_audio(message) -> bool:
    """Return True if the message has an audio attachment."""
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("audio/"):
            return True
        if any(att.filename.lower().endswith(ext) for ext in AUDIO_EXTENSIONS):
            return True
    return False
