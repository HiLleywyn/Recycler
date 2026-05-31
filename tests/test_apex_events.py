"""V3 Pillar 6 unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from configs.apex_events_config import EVENTS, total_weight


def test_event_catalogue_is_nonempty() -> None:
    assert len(EVENTS) > 0


def test_every_event_has_required_keys() -> None:
    required = {"name", "flavour", "duration_secs", "rarity", "modifiers", "weight"}
    for eid, ev in EVENTS.items():
        missing = required - set(ev.keys())
        assert not missing, f"{eid}: missing {missing}"
        assert int(ev["duration_secs"]) > 0
        assert int(ev["weight"]) > 0
        assert isinstance(ev["modifiers"], dict) and ev["modifiers"]


def test_total_weight_is_positive() -> None:
    assert total_weight() > 0


def test_rarity_is_a_known_level() -> None:
    allowed = {"info", "warning", "volatile", "catastrophe"}
    for eid, ev in EVENTS.items():
        assert ev["rarity"] in allowed, eid


def test_modifier_values_are_floats() -> None:
    for eid, ev in EVENTS.items():
        for k, v in ev["modifiers"].items():
            assert isinstance(v, (int, float)), f"{eid}.{k}"


def test_event_poster_renders() -> None:
    from services.event_poster import render_event_poster
    ends = datetime.now(timezone.utc) + timedelta(hours=1)
    png = render_event_poster(
        "solar_flare", ends_at=ends,
    )
    assert png.startswith(b"\x89PNG")
    assert len(png) > 5000


def test_event_poster_handles_unknown_id() -> None:
    from services.event_poster import render_event_poster
    png = render_event_poster(
        "phantom_event",
        name="Phantom", flavour="Mystery surge",
        modifiers={"x.y": 1.5},
        ends_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert png.startswith(b"\x89PNG")
