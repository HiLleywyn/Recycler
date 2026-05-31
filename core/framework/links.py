"""core/framework/links.py  -  simple short-link manager for embeds.

Stores a mapping of short codes -> target URLs in a small JSON file and
provides helpers to extract URLs from embed text, shorten them and inject a
consolidated "Links" field into embeds.
"""
from __future__ import annotations

import json
import pathlib
import re
import secrets
from typing import Dict, List, Tuple

from core.config import Config

URL_RE = re.compile(r"https?://\S+")


def sanitize_embed(embed: object) -> object:
    """Try to run link shortening on an Embed; return the same embed."""
    try:
        LinkManager().process_embed(embed)
    except Exception:
        pass
    return embed


class LinkManager:
    def __init__(self, db_path: str | None = None) -> None:
        # store mapping next to the main DB by default
        if db_path:
            self._path = pathlib.Path(db_path)
        else:
            self._path = pathlib.Path("discoin.links.json")
        try:
            self._data = json.loads(self._path.read_text()) if self._path.exists() else {}
        except Exception:
            self._data = {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._data, indent=2))
        except Exception:
            pass

    def shorten(self, url: str) -> str:
        """Return a short URL path (dashboard base + /l/<code>). Reuse existing code if present."""
        # reuse if already present
        for code, target in self._data.items():
            if target == url:
                return f"{Config.DASHBOARD_URL.rstrip('/')}/l/{code}" if Config.DASHBOARD_URL else f"/l/{code}"
        # create new code
        code = secrets.token_urlsafe(6)
        # ensure uniqueness
        while code in self._data:
            code = secrets.token_urlsafe(6)
        self._data[code] = url
        self._save()
        return f"{Config.DASHBOARD_URL.rstrip('/')}/l/{code}" if Config.DASHBOARD_URL else f"/l/{code}"

    def resolve(self, code: str) -> str | None:
        return self._data.get(code)

    # ── Embed helpers ─────────────────────────────────────────────────────
    def extract_urls(self, text: str) -> List[str]:
        return URL_RE.findall(text or "")

    def process_embed(self, embed) -> Tuple[object, List[Tuple[str, str]]]:
        """Scan embed fields/description/footer for URLs, remove them in-place and
        add a consolidated "Links" field with shortened links.

        Returns the embed and list of (short_url, original_url).
        """
        # Remove any existing "Links" field to avoid duplicates
        for i, f in enumerate(list(getattr(embed, "fields", []))):
            if f.name == "Links":
                embed.remove_field(i)
                break

        found: List[Tuple[str, str]] = []
        url_map: Dict[str, str] = {}  # original -> short

        def _clean_text(t: str) -> str:
            if not t:
                return t
            urls = URL_RE.findall(t)
            for u in urls:
                if u not in url_map:
                    short = self.shorten(u)
                    url_map[u] = short
                    found.append((short, u))
                t = t.replace(u, "")
            # collapse multiple spaces/newlines
            t = re.sub(r"[ \t]+", " ", t)
            t = re.sub(r"\n{2,}", "\n", t)
            return t.strip()

        # title
        if getattr(embed, "title", None):
            embed.title = _clean_text(embed.title)

        # embed.url
        if getattr(embed, "url", None):
            if embed.url not in url_map:
                short = self.shorten(embed.url)
                url_map[embed.url] = short
                found.append((short, embed.url))
            embed.url = url_map[embed.url]

        # author (name + url)
        if getattr(embed, "author", None):
            if getattr(embed.author, "name", None):
                embed.set_author(name=_clean_text(embed.author.name), icon_url=embed.author.icon_url, url=embed.author.url)
            if getattr(embed.author, "url", None):
                if embed.author.url not in url_map:
                    short = self.shorten(embed.author.url)
                    url_map[embed.author.url] = short
                    found.append((short, embed.author.url))
                embed.set_author(name=embed.author.name, icon_url=embed.author.icon_url, url=url_map[embed.author.url])

        # description
        if getattr(embed, "description", None):
            embed.description = _clean_text(embed.description)

        # footer
        if embed.footer and getattr(embed.footer, "text", None):
            embed.set_footer(text=_clean_text(embed.footer.text))

        # fields
        new_fields = []
        for f in list(getattr(embed, "fields", [])):
            new_val = _clean_text(f.value)
            # only add field back if it has content
            if new_val:
                new_fields.append((f.name, new_val, f.inline))
        # clear all fields and re-add cleaned ones
        embed.clear_fields()
        for name, val, inline in new_fields:
            embed.add_field(name=name, value=val, inline=inline)

        # if found:
        #     lines = [f"{i+1}. {s}  -  {o}" for i, (s, o) in enumerate(found)]
        #     embed.add_field(name="Links", value="\n".join(lines), inline=False)

        return embed, found
