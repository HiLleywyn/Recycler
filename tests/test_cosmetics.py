"""V3 Pillar 4 unit tests."""
from __future__ import annotations

from configs.cosmetics_config import BANNERS, FRAMES, SIGILS, SLOTS, TITLES, all_items


def test_every_slot_has_a_system_default() -> None:
    for slot, cat in SLOTS.items():
        defaults = [cid for cid, entry in cat.items()
                    if (entry.get("unlock") or "").lower() == "system"]
        assert defaults, f"{slot} has no system default"


def test_all_items_round_trip() -> None:
    items = all_items()
    for path, entry in items.items():
        slot, cid = path.split("/", 1)
        assert slot in SLOTS
        assert cid in SLOTS[slot]
        assert entry["slot"] == slot
        assert entry["id"] == cid


def test_banners_have_color_and_accent() -> None:
    for cid, entry in BANNERS.items():
        assert "color" in entry
        assert "accent" in entry


def test_frames_have_color_and_ring_width() -> None:
    for cid, entry in FRAMES.items():
        assert "color" in entry
        assert "ring_width" in entry


def test_sigils_have_glyph_and_color() -> None:
    for cid, entry in SIGILS.items():
        assert "glyph" in entry
        assert "color" in entry


def test_titles_have_labels() -> None:
    for cid, entry in TITLES.items():
        assert entry.get("label")


def test_profile_card_renders() -> None:
    from services.profile_render import render_profile_card
    png = render_profile_card(
        user_name="Test User",
        avatar_bytes=None,
        equipped={"title": "novice", "banner": "midnight",
                  "frame": "simple", "sigil": "star"},
        net_worth_usd=123_456.78,
    )
    assert png.startswith(b"\x89PNG")
    assert len(png) > 8000


def test_profile_card_with_mastery() -> None:
    from services.profile_render import render_profile_card
    ms = {"tracks": {"fisher": {"level": 42}, "farmer": {"level": 20}},
          "unlocked_count": 5}
    png = render_profile_card(
        user_name="Apex Player",
        equipped={"title": "season_champ", "banner": "aurora",
                  "frame": "gold", "sigil": "crown"},
        net_worth_usd=10_000_000.0,
        mastery_summary=ms,
        season_rank=1,
        clan_war_scoreline="MyClan 7 - 3 RivalClan",
        badges=("LP Pioneer", "Whale Tamer", "First Catch"),
    )
    assert png.startswith(b"\x89PNG")


def test_gallery_renders() -> None:
    from services.profile_render import render_gallery
    inv = {
        "title": ["novice", "fisher_apex"],
        "banner": ["midnight", "aurora"],
        "frame": ["simple", "gold"],
        "sigil": ["star", "crown"],
    }
    png = render_gallery(inv, user_name="Test")
    assert png.startswith(b"\x89PNG")


# ── Shop ───────────────────────────────────────────────────────────────
def test_shop_price_parser() -> None:
    from services.cosmetics import shop_price_usd
    assert shop_price_usd("shop:1234") == 1234.0
    assert shop_price_usd("shop:99.5") == 99.5
    assert shop_price_usd("achievement:foo") is None
    assert shop_price_usd("system") is None
    assert shop_price_usd("") is None
    assert shop_price_usd("shop:not_a_number") is None


def test_shop_listings_nonempty() -> None:
    from services.cosmetics import shop_listings
    all_items = shop_listings()
    assert len(all_items) > 0
    for entry in all_items:
        assert entry["price_usd"] > 0
        assert entry["slot"] in ("title", "banner", "frame", "sigil")


def test_shop_listings_filtered_by_theme() -> None:
    from configs.cosmetics_config import THEMES
    from services.cosmetics import shop_listings
    for theme in THEMES:
        items = shop_listings(theme=theme)
        assert items, f"Theme {theme} has no shop items"
        for entry in items:
            assert entry["theme"] == theme


def test_themes_have_required_keys() -> None:
    from configs.cosmetics_config import THEMES
    for theme_id, meta in THEMES.items():
        assert meta.get("label")
        assert "color" in meta


def test_eight_user_requested_themes_exist() -> None:
    from configs.cosmetics_config import THEMES
    expected = {"cats", "moons", "turtles", "stars", "ocean",
                "pirates", "gambling", "politics"}
    assert expected.issubset(set(THEMES.keys()))


def test_shop_renderer_smoke() -> None:
    from services.cosmetics import shop_listings
    from services.profile_render import render_shop
    png = render_shop(shop_listings(), owned=set(), wallet_usd=1000.0)
    assert png.startswith(b"\x89PNG")


def test_shop_renderer_themed() -> None:
    from services.cosmetics import shop_listings
    from services.profile_render import render_shop
    png = render_shop(
        shop_listings(theme="cats"),
        theme="cats",
        owned={"sigil/cat"},
        wallet_usd=500.0,
    )
    assert png.startswith(b"\x89PNG")


def test_shop_renderer_empty() -> None:
    from services.profile_render import render_shop
    png = render_shop([], owned=set(), wallet_usd=0.0)
    assert png.startswith(b"\x89PNG")


# ── Level card ─────────────────────────────────────────────────────────
def test_level_card_renders_themed() -> None:
    from services.level_render import render_level_card
    png = render_level_card(
        user_name="Apex Player",
        level=42, rank_name="Apex Trader", total_xp=12345,
        level_floor_xp=10000, level_next_xp=15000,
        messages=523, streak_days=14, position=3,
        equipped={
            "title": "high_roller", "banner": "vegas_strip",
            "frame": "cards", "sigil": "dice",
        },
    )
    assert png.startswith(b"\x89PNG")
    assert len(png) > 8000


def test_level_card_renders_default() -> None:
    from services.level_render import render_level_card
    png = render_level_card(
        user_name="New Player",
        level=1, rank_name=None, total_xp=50,
        level_floor_xp=0, level_next_xp=100,
        messages=3, streak_days=0, position=None,
        equipped={},
    )
    assert png.startswith(b"\x89PNG")


def test_level_card_zero_xp_does_not_divide_by_zero() -> None:
    # Edge case: a fresh user at level 0 with no XP must not crash the
    # progress bar's denominator.
    from services.level_render import render_level_card
    png = render_level_card(
        user_name="Bot",
        level=0, rank_name=None, total_xp=0,
        level_floor_xp=0, level_next_xp=0,
        messages=0, streak_days=0, position=None,
        equipped={},
    )
    assert png.startswith(b"\x89PNG")
