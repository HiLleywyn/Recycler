"""V3 Pillar 5 unit tests."""
from __future__ import annotations

from datetime import datetime, timezone


from services.inbox_render import render_inbox_index, render_inbox_message


def test_inbox_index_empty_renders() -> None:
    png = render_inbox_index([], display_name="Test")
    assert png.startswith(b"\x89PNG")


def test_inbox_index_with_messages_renders() -> None:
    now = datetime.now(timezone.utc)
    msgs = [
        {"id": 1, "category": "raid", "title": "You were raided",
         "body": "Lost 100 USD", "severity": "error",
         "posted_at": now, "read_at": None},
        {"id": 2, "category": "achievement", "title": "First catch!",
         "body": "Caught your first fish", "severity": "success",
         "posted_at": now, "read_at": now},
        {"id": 3, "category": "market_event", "title": "Solar flare",
         "body": "Mining +50%", "severity": "warning",
         "posted_at": now, "read_at": None},
    ]
    png = render_inbox_index(msgs, display_name="Test", unread_count=2)
    assert png.startswith(b"\x89PNG")
    assert len(png) > 5000


def test_inbox_message_renders() -> None:
    msg = {
        "id": 1, "category": "season",
        "title": "Season 12 has ended",
        "body": "Final standings posted. Check ,season last for podiums.\n\nNew season starts at the next weekly tick.",
        "severity": "info",
        "posted_at": datetime.now(timezone.utc),
        "read_at": None,
    }
    png = render_inbox_message(msg, display_name="Test")
    assert png.startswith(b"\x89PNG")


def test_inbox_message_long_body_wraps() -> None:
    msg = {
        "id": 1, "category": "info",
        "title": "Long body test",
        "body": "lorem ipsum dolor sit amet " * 30,
        "severity": "info",
        "posted_at": datetime.now(timezone.utc),
        "read_at": None,
    }
    png = render_inbox_message(msg, display_name="Test")
    assert png.startswith(b"\x89PNG")
