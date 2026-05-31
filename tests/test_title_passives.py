"""Tests for ``services.cosmetics.title_passives``.

The title-passive lookup MUST return the same ``{effect_key: value}``
shape that ``services.mastery.passives`` returns so a consumer can
merge the two dicts and call ``services.mastery.apply`` on the union
without learning about titles specifically.
"""
from __future__ import annotations

import pytest

from configs.cosmetics_config import TITLES
from services import cosmetics as _cos
from services.mastery import apply as mastery_apply


class _FakeDB:
    """Minimal asyncpg-stand-in for unit tests.

    ``equipped`` reads ``user_cosmetics_equipped`` -- we just return a
    pre-seeded dict-of-rows shape.
    """
    def __init__(self, equipped_rows: list[dict]) -> None:
        self._rows = equipped_rows

    async def fetch_all(self, _query: str, *_args) -> list[dict]:
        return list(self._rows)


def _seeded_db(title_id: str | None) -> _FakeDB:
    rows: list[dict] = []
    if title_id:
        rows.append({"slot": "title", "item_id": title_id})
    return _FakeDB(rows)


@pytest.mark.asyncio
async def test_no_title_returns_empty() -> None:
    out = await _cos.title_passives(_seeded_db(None), user_id=1)
    assert out == {}


@pytest.mark.asyncio
async def test_cat_lord_grants_buddy_dmg() -> None:
    out = await _cos.title_passives(_seeded_db("cat_lord"), user_id=1)
    entry = TITLES["cat_lord"]
    assert out == {entry["effect_key"]: entry["effect_value"]}


@pytest.mark.asyncio
async def test_high_roller_grants_gamba_payout() -> None:
    out = await _cos.title_passives(_seeded_db("high_roller"), user_id=2)
    assert out["combat.gamba_payout"] > 0.0


@pytest.mark.asyncio
async def test_title_passives_merge_with_mastery_apply() -> None:
    """A consumer can merge title + mastery dicts and read the union via
    mastery.apply -- the whole point of matching the dict shape."""
    title_dict = await _cos.title_passives(_seeded_db("cat_lord"), user_id=3)
    mastery_dict = {"combat.buddy_dmg": 0.10}  # eg. Pack Leader node
    merged: dict[str, float] = {}
    for k in set(title_dict) | set(mastery_dict):
        merged[k] = title_dict.get(k, 0.0) + mastery_dict.get(k, 0.0)
    bonus = mastery_apply(merged, "combat.buddy_dmg")
    # cat_lord = 0.05, Pack Leader = 0.10 -> stack to 0.15
    assert abs(bonus - 0.15) < 1e-9


@pytest.mark.asyncio
async def test_unknown_title_returns_empty() -> None:
    out = await _cos.title_passives(_seeded_db("no_such_title"), user_id=4)
    assert out == {}


@pytest.mark.asyncio
async def test_novice_title_has_no_effect() -> None:
    # Novice is intentionally flavour-only -- no effect_key, so passives
    # must return empty even though the title IS equipped.
    out = await _cos.title_passives(_seeded_db("novice"), user_id=5)
    assert out == {}


def test_cosmetics_for_achievement_finds_titles() -> None:
    # cat_lord / kitten / captain are wired to real firing badges.
    paths = _cos.cosmetics_for_achievement("buddy_champion")
    assert "title/cat_lord" in paths
    paths = _cos.cosmetics_for_achievement("new_best_friend")
    assert "title/kitten" in paths
    paths = _cos.cosmetics_for_achievement("robin_hood")
    assert "title/captain" in paths
    # An unknown badge returns nothing.
    assert _cos.cosmetics_for_achievement("no_such_badge") == []
