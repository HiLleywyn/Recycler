"""Smoke tests for the cosmetics art overhaul.

Verifies that every themed cosmetic combo renders without raising, and
that the resulting PNGs are non-trivial (the procedural art adds bytes
over a plain colour-swatch render). The actual visual fidelity is
verified by eyeballing the /tmp output during development -- here we
just guard against regressions.
"""
from __future__ import annotations

from services.profile_render import (
    render_profile_card, render_shop, shop_paginate,
)
from services.cosmetics import shop_listings


_COMBOS = [
    ("cats",     "cat",          "tabby",         "cat_meadow",     "cat_lord"),
    ("moons",    "moon",         "crescent",      "lunar_glow",     "moonchaser"),
    ("turtles",  "turtle",       "shell",         "reef_bloom",     "sea_turtle"),
    ("stars",    "star_shop",    "comet",         "stellar",        "star_walker"),
    ("ocean",    "ocean_wave",   "coral",         "deep_ocean",     "tidemaster"),
    ("pirates",  "pirate_skull", "anchor_chain",  "jolly_roger",    "captain"),
    ("gambling", "dice",         "cards",         "casino_floor",   "high_roller"),
    ("politics", "gavel",        "eagle",         "capitol",        "senator"),
]


def test_profile_renders_every_theme() -> None:
    for theme, sigil, frame, banner, title in _COMBOS:
        png = render_profile_card(
            user_name="testplayer",
            avatar_bytes=None,
            equipped={"sigil": sigil, "frame": frame, "banner": banner, "title": title},
            net_worth_usd=1_000_000.0,
            job_title="Tester", job_level=1,
        )
        assert png.startswith(b"\x89PNG"), f"{theme}: not a PNG"
        assert len(png) > 5000, f"{theme}: render suspiciously small ({len(png)} bytes)"


def test_legendary_sigils_render() -> None:
    for sig in ("phoenix", "dragon", "infinity"):
        png = render_profile_card(
            user_name="legendary",
            equipped={"sigil": sig, "frame": "diamond", "banner": "starfield", "title": "myth"},
            net_worth_usd=999_999_999.0,
        )
        assert png.startswith(b"\x89PNG"), f"sigil {sig}: not a PNG"
        assert len(png) > 5000


def test_unthemed_sigils_fall_through_to_glyph() -> None:
    # ``crown`` and ``infinity`` exist; pick one that ISN'T in sigil_art's
    # dispatch (currently every catalog sigil IS dispatched, so the
    # fallback path is tested by feeding a synthetic id).
    png = render_profile_card(
        user_name="legacy",
        equipped={"sigil": "no_such_sigil_id_exists", "frame": "simple",
                  "banner": "obsidian", "title": "novice"},
        net_worth_usd=0.0,
    )
    assert png.startswith(b"\x89PNG")


def test_shop_renders_real_art_per_theme() -> None:
    for theme, *_ in _COMBOS:
        listings = shop_listings(theme=theme)
        slice_, page, total = shop_paginate(listings, page=1, per_page=12)
        png = render_shop(
            slice_, theme=theme, owned=set(),
            wallet_usd=50_000.0, page=page, per_page=12, total_pages=total,
        )
        assert png.startswith(b"\x89PNG"), f"shop {theme}: not a PNG"
        assert len(png) > 5000


def test_title_epithet_present_on_themed_titles() -> None:
    """Every themed shop / achievement title must carry an ``epithet``."""
    from configs.cosmetics_config import TITLES
    for tid, entry in TITLES.items():
        assert entry.get("epithet"), f"title {tid!r} is missing epithet"
        # Effect_key only required for the gameplay-relevant tier; novice
        # / early_adopter are flavour-only and that's fine.
        if entry.get("theme") or entry.get("rarity") == "legendary":
            assert entry.get("effect_key"), (
                f"themed/legendary title {tid!r} is missing effect_key"
            )
