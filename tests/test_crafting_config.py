"""Tests for crafting_config.py -- recipe catalog + helpers.

Pure-Python, no DB / Discord mocks needed. Validates that every recipe
in the catalog is internally consistent (rarity is one of the canonical
tiers, apply target prefix is one of the four supported routes,
inputs reference declared kinds, level gates are sane, etc.) and that
the level / XP helpers behave at the expected boundaries.
"""
from __future__ import annotations

import pytest

import configs.crafting_config as cc


# ── Catalog invariants ──────────────────────────────────────────────────────


_VALID_INPUT_KINDS = {"fish", "crop", "ore", "recipe", "token"}
# Mirrors the dispatch in services.crafting.apply_crafted_item:
#   bait     -> user_fishing.bait_inventory
#   fert     -> user_farming.fertilizer_inventory
#   consum   -> user_dungeon.consumables
#   weapon   -> user_dungeon.weapons_owned (legendary smithing recipes)
#   armor    -> user_dungeon.armor_owned (legendary smithing recipes)
#   buddy    -> direct effect on active buddy
#   cosmetic -> users.cosmetics JSONB (craft-only role-grant items)
_VALID_APPLY_KINDS = {
    "bait", "fert", "consum", "weapon", "armor", "buddy", "cosmetic",
    # battle/<key>: buddy battle consumables routed into
    # user_buddy_economy.battle_inventory (Buddy Battles expansion).
    # Catalogue lives in buddies_config.BATTLE_CONSUMABLES.
    "battle",
}


class TestRecipeCatalog:
    def test_catalog_is_non_empty(self):
        assert len(cc.CRAFT_ITEMS) > 0

    def test_keys_are_lowercase_and_unique(self):
        keys = list(cc.CRAFT_ITEMS.keys())
        assert keys == [k.lower() for k in keys]
        assert len(keys) == len(set(keys))

    @pytest.mark.parametrize("key, meta", list(cc.CRAFT_ITEMS.items()))
    def test_recipe_shape(self, key: str, meta: dict):
        assert "name" in meta and meta["name"]
        assert "rarity" in meta and meta["rarity"] in cc.RARITIES
        assert "inputs" in meta and isinstance(meta["inputs"], dict)
        assert "fgd_cost" in meta and float(meta["fgd_cost"]) >= 0
        assert "min_level" in meta and 1 <= int(meta["min_level"]) <= cc.MAX_LEVEL
        assert "apply" in meta and meta["apply"]
        assert "max_stack" in meta and int(meta["max_stack"]) > 0

    @pytest.mark.parametrize("key, meta", list(cc.CRAFT_ITEMS.items()))
    def test_inputs_reference_known_kinds(self, key: str, meta: dict):
        for raw in meta["inputs"]:
            kind, sub = cc.parse_input_key(raw)
            assert kind in _VALID_INPUT_KINDS, f"{key}: unknown input kind `{kind}`"
            assert sub, f"{key}: empty sub for input `{raw}`"

    @pytest.mark.parametrize("key, meta", list(cc.CRAFT_ITEMS.items()))
    def test_apply_target_is_known_route(self, key: str, meta: dict):
        kind, target = cc.parse_apply_target(meta["apply"])
        assert kind in _VALID_APPLY_KINDS, f"{key}: unknown apply kind `{kind}`"
        assert target, f"{key}: empty apply target"


# ── Helpers ─────────────────────────────────────────────────────────────────


class TestParseHelpers:
    def test_parse_input_key_basic(self):
        assert cc.parse_input_key("fish/bass") == ("fish", "bass")
        assert cc.parse_input_key("ore/COPPER") == ("ore", "COPPER")

    def test_parse_input_key_strips_whitespace(self):
        assert cc.parse_input_key("  fish/bass  ") == ("fish", "bass")

    def test_parse_input_key_lowercases_kind_only(self):
        # Sub keeps its case so symbols like COPPER survive intact.
        assert cc.parse_input_key("FISH/Bass") == ("fish", "Bass")

    def test_parse_input_key_malformed(self):
        assert cc.parse_input_key("") == ("", "")
        assert cc.parse_input_key("nothingelse") == ("", "")
        assert cc.parse_input_key(None) == ("", "")  # type: ignore[arg-type]

    def test_parse_apply_target_uses_same_machinery(self):
        assert cc.parse_apply_target("bait/worm") == ("bait", "worm")
        assert cc.parse_apply_target("buddy/feed") == ("buddy", "feed")


class TestLevelCurve:
    def test_xp_for_level_one_is_zero(self):
        assert cc.xp_for_level(1) == 0

    def test_xp_for_level_increases_monotonically(self):
        last = -1
        for lvl in range(1, cc.MAX_LEVEL + 1):
            xp = cc.xp_for_level(lvl)
            assert xp >= last
            last = xp

    def test_xp_for_level_caps_at_max(self):
        capped = cc.xp_for_level(cc.MAX_LEVEL + 50)
        assert capped == cc.xp_for_level(cc.MAX_LEVEL)

    def test_level_from_xp_zero_returns_one(self):
        assert cc.level_from_xp(0) == 1
        assert cc.level_from_xp(-100) == 1

    def test_level_from_xp_round_trip(self):
        # For each level, the xp threshold should resolve back to that level.
        for lvl in range(1, cc.MAX_LEVEL + 1):
            assert cc.level_from_xp(cc.xp_for_level(lvl)) == lvl

    def test_level_from_xp_caps_at_max(self):
        # Way past MAX_LEVEL still resolves to MAX_LEVEL.
        astronomic = cc.xp_for_level(cc.MAX_LEVEL) * 10
        assert cc.level_from_xp(astronomic) == cc.MAX_LEVEL


class TestRecipesAtLevel:
    def test_level_one_returns_only_min_level_one_recipes(self):
        recipes = cc.recipes_at_level(1)
        for _, m in recipes:
            assert int(m["min_level"]) == 1

    def test_higher_level_includes_lower_recipes(self):
        low = {k for k, _ in cc.recipes_at_level(1)}
        high = {k for k, _ in cc.recipes_at_level(cc.MAX_LEVEL)}
        assert low.issubset(high)

    def test_sorted_by_min_level_then_alphabetical(self):
        recipes = cc.recipes_at_level(cc.MAX_LEVEL)
        keys_levels = [(int(m["min_level"]), k) for k, m in recipes]
        assert keys_levels == sorted(keys_levels)


class TestRarityPayout:
    def test_payout_table_covers_all_rarities(self):
        for r in cc.RARITIES:
            assert r in cc.RARITY_INGOT_PAYOUT
            lo, hi = cc.RARITY_INGOT_PAYOUT[r]
            assert 0 <= lo <= hi

    def test_xp_table_covers_all_rarities(self):
        for r in cc.RARITIES:
            assert r in cc.RARITY_XP
            assert cc.RARITY_XP[r] >= 0

    def test_rarity_payout_increases_with_tier(self):
        prev = -1.0
        for r in cc.RARITIES:
            lo, _ = cc.RARITY_INGOT_PAYOUT[r]
            assert lo > prev
            prev = lo
