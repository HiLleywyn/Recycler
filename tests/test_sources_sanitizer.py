"""Regression tests for the web-search Sources-button sanitizer.

``cogs/help.py::_sanitize_source_entry`` is the chokepoint every
``data.web_search`` result flows through before being rendered as a
Markdown link in an ephemeral Discord message. Production surfaced
invite redirectors and pig-butchering scam pages being presented as
"genuine sources" when the backend SERP echoed them back, so this file
pins the filter's behavior against concrete attack payloads.
"""
from __future__ import annotations

import pytest

from cogs.help import _sanitize_source_entry, _SOURCE_URL_DOMAIN_BLOCKLIST


# =============================================================================
# Scheme allowlist
# =============================================================================

class TestSchemeAllowlist:
    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:text/html,<script>x()</script>",
            "discord://invite/foo",
            "steam://run",
            "file:///etc/passwd",
            "ftp://old.example.com/file",
            "mailto:bad@example.com",
        ],
    )
    def test_non_http_schemes_dropped(self, url: str) -> None:
        assert _sanitize_source_entry({"title": "ok", "url": url}) is None

    def test_http_passes(self) -> None:
        out = _sanitize_source_entry({"title": "News", "url": "http://example.com/page"})
        assert out is not None and out["url"].startswith("http://")

    def test_https_passes(self) -> None:
        out = _sanitize_source_entry({"title": "News", "url": "https://en.wikipedia.org/wiki/Moneta"})
        assert out is not None and out["url"].startswith("https://")


# =============================================================================
# Netloc rules -- no bare paths, no IP literals
# =============================================================================

class TestNetlocRules:
    def test_ipv4_literal_rejected(self) -> None:
        # IP literals are a classic scam indicator; search results for
        # reputable articles land on named hosts.
        assert _sanitize_source_entry({"title": "ok", "url": "http://192.168.1.1/page"}) is None
        assert _sanitize_source_entry({"title": "ok", "url": "https://1.2.3.4/"}) is None

    def test_empty_host_rejected(self) -> None:
        assert _sanitize_source_entry({"title": "ok", "url": "http:///just/a/path"}) is None

    def test_single_word_host_rejected(self) -> None:
        # No dot = not a public FQDN. localhost, intranet hosts, etc.
        assert _sanitize_source_entry({"title": "ok", "url": "http://localhost/"}) is None

    def test_empty_url_rejected(self) -> None:
        assert _sanitize_source_entry({"title": "ok", "url": ""}) is None
        assert _sanitize_source_entry({"title": "ok", "url": None}) is None


# =============================================================================
# Blocklist
# =============================================================================

class TestDomainBlocklist:
    @pytest.mark.parametrize(
        "url",
        [
            # Direct invite redirectors
            "https://discord.gg/foobar",
            "https://DISCORD.GG/FOObar",       # case-insensitive
            "https://api.discord.gg/invite/x", # subdomain blocked too
            "https://t.me/scamchannel",
            "https://telegram.me/xyz",
            "https://wa.me/15551234567",
            # Path-scoped invite
            "https://discord.com/invite/abc123",
            "https://discordapp.com/invite/abc",
            # Shorteners
            "https://bit.ly/abc", "https://tinyurl.com/xyz",
            "https://t.co/abc",   "https://is.gd/abc",
            "https://goo.gl/abc",
        ],
    )
    def test_blocklisted_url_rejected(self, url: str) -> None:
        assert _sanitize_source_entry({"title": "ok", "url": url}) is None

    def test_wellknown_site_not_caught_by_suffix_bug(self) -> None:
        # The suffix match rule is "endswith('.' + blocked)", so
        # bitlinked.com must NOT trip the bit.ly rule.
        out = _sanitize_source_entry({"title": "Real site", "url": "https://bitlinked.com/"})
        assert out is not None

    def test_blocklist_has_known_offenders(self) -> None:
        # Spot-check the critical entries; anyone dropping them should
        # have to explicitly replace them.
        assert "discord.gg" in _SOURCE_URL_DOMAIN_BLOCKLIST
        assert "t.me" in _SOURCE_URL_DOMAIN_BLOCKLIST
        assert "bit.ly" in _SOURCE_URL_DOMAIN_BLOCKLIST


# =============================================================================
# Title sanitization
# =============================================================================

class TestTitleSanitization:
    def test_markdown_metachars_stripped(self) -> None:
        out = _sanitize_source_entry({
            "title": "Click [me](https://evil.example) for FREE CRYPTO",
            "url": "https://example.com/real",
        })
        assert out is not None
        for ch in ("[", "]", "(", ")", "`", "|"):
            assert ch not in out["title"]

    def test_angle_brackets_stripped(self) -> None:
        # Angle brackets in a title could escape the `<url>` wrapping and
        # force Discord to render an unwanted preview.
        out = _sanitize_source_entry({
            "title": "Read <https://scam.example> before trading",
            "url": "https://example.com/ok",
        })
        assert out is not None
        assert "<" not in out["title"] and ">" not in out["title"]

    def test_newlines_collapsed(self) -> None:
        out = _sanitize_source_entry({
            "title": "line1\nline2\rline3\tline4",
            "url": "https://example.com/",
        })
        assert out is not None
        assert "\n" not in out["title"]
        assert "\r" not in out["title"]
        assert "\t" not in out["title"]

    def test_zero_width_chars_stripped(self) -> None:
        # Homoglyph attack: invisible characters make "paypa​l.com"
        # read like "paypal.com" in the title but link to the real domain.
        out = _sanitize_source_entry({
            "title": "paypa​l.com login",
            "url": "https://example.com/",
        })
        assert out is not None
        assert "​" not in out["title"]

    def test_long_title_truncated(self) -> None:
        long_title = "A" * 200
        out = _sanitize_source_entry({"title": long_title, "url": "https://example.com/"})
        assert out is not None
        assert len(out["title"]) <= 100

    def test_empty_title_defaults_to_link(self) -> None:
        out = _sanitize_source_entry({"title": "", "url": "https://example.com/"})
        assert out is not None
        assert out["title"] == "Link"


# =============================================================================
# Garbage-in handling
# =============================================================================

class TestGarbageInput:
    def test_malformed_url_rejected(self) -> None:
        # urlparse tolerates most junk but we still require a netloc.
        assert _sanitize_source_entry({"title": "ok", "url": "just some words"}) is None

    def test_missing_fields_rejected(self) -> None:
        assert _sanitize_source_entry({}) is None
        assert _sanitize_source_entry({"title": "no url"}) is None
