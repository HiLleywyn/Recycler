"""Tests for core/framework/amount_parser.py  -  natural-language amount parsing."""
from __future__ import annotations

import pytest

from core.framework.amount_parser import (
    FRACTION_MAP,
    ALL_KEYWORDS,
    REST_KEYWORDS,
    parse_amount,
    translate_emoji_amount,
)
from core.framework.utils import parse_amount as utils_parse_amount


# ── Named fractions ────────────────────────────────────────────────────────────

class TestNamedFractions:
    def test_half(self):
        spec = parse_amount("half")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.5)
        assert spec.resolved is None

    def test_a_half(self):
        spec = parse_amount("a half")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.5)

    def test_quarter(self):
        spec = parse_amount("quarter")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.25)

    def test_three_quarters(self):
        spec = parse_amount("three quarters")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.75)

    def test_third(self):
        spec = parse_amount("third")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(1 / 3)

    def test_two_thirds(self):
        spec = parse_amount("two thirds")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(2 / 3)

    def test_eighth(self):
        spec = parse_amount("eighth")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.125)

    def test_tenth(self):
        spec = parse_amount("tenth")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.1)

    def test_case_insensitive(self):
        spec = parse_amount("HALF")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.5)

    def test_all_named_fractions_in_map(self):
        """Every entry in FRACTION_MAP must parse to the correct fraction."""
        for word, expected in FRACTION_MAP.items():
            spec = parse_amount(word)
            assert spec.is_fraction, f"Expected fraction for '{word}'"
            assert spec.fraction_value == pytest.approx(expected, rel=1e-9), (
                f"'{word}' expected {expected}, got {spec.fraction_value}"
            )


# ── Generic fraction notation ──────────────────────────────────────────────────

class TestGenericFractions:
    def test_one_over_two(self):
        spec = parse_amount("1/2")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.5)

    def test_three_over_four(self):
        spec = parse_amount("3/4")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.75)

    def test_seven_over_eight(self):
        spec = parse_amount("7/8")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.875)

    def test_fraction_above_one_resolves_as_number(self):
        """5/2 = 2.5  -  treated as a concrete number, not a fraction-of-balance."""
        spec = parse_amount("5/2")
        assert not spec.is_fraction
        assert spec.resolved == pytest.approx(2.5)

    def test_division_by_zero_raises(self):
        with pytest.raises(ValueError, match="Division by zero"):
            parse_amount("3/0")

    def test_generic_fraction_with_spaces(self):
        spec = parse_amount("3 / 4")
        assert spec.is_fraction
        assert spec.fraction_value == pytest.approx(0.75)


# ── "all" / "everything" keywords ─────────────────────────────────────────────

class TestAllKeywords:
    @pytest.mark.parametrize("word", sorted(ALL_KEYWORDS))
    def test_all_keyword(self, word):
        spec = parse_amount(word)
        assert spec.is_all
        assert not spec.is_fraction
        assert spec.resolved is None

    def test_all_uppercase(self):
        spec = parse_amount("ALL")
        assert spec.is_all

    def test_needs_resolution_all(self):
        assert parse_amount("all").needs_resolution


# ── "rest" / "remaining" keywords ─────────────────────────────────────────────

class TestRestKeywords:
    @pytest.mark.parametrize("word", sorted(REST_KEYWORDS))
    def test_rest_keyword(self, word):
        spec = parse_amount(word)
        assert spec.is_rest
        assert not spec.is_all
        assert spec.resolved is None

    def test_needs_resolution_rest(self):
        assert parse_amount("remaining").needs_resolution


# ── USD amounts ────────────────────────────────────────────────────────────────

class TestUSDAmounts:
    def test_dollar_prefix(self):
        spec = parse_amount("$100")
        assert spec.is_usd
        assert spec.resolved == pytest.approx(100.0)

    def test_dollar_with_decimal(self):
        spec = parse_amount("$99.99")
        assert spec.is_usd
        assert spec.resolved == pytest.approx(99.99)

    def test_dollar_with_k_suffix(self):
        spec = parse_amount("$1.5k")
        assert spec.is_usd
        assert spec.resolved == pytest.approx(1500.0)

    def test_dollar_with_m_suffix(self):
        spec = parse_amount("$2m")
        assert spec.is_usd
        assert spec.resolved == pytest.approx(2_000_000.0)

    def test_dollar_with_comma(self):
        spec = parse_amount("$1,000")
        assert spec.is_usd
        assert spec.resolved == pytest.approx(1000.0)

    def test_word_dollars(self):
        spec = parse_amount("100 dollars")
        assert spec.is_usd
        assert spec.resolved == pytest.approx(100.0)

    def test_word_usd(self):
        spec = parse_amount("500 usd")
        assert spec.is_usd
        assert spec.resolved == pytest.approx(500.0)

    def test_word_bucks(self):
        spec = parse_amount("50 bucks")
        assert spec.is_usd
        assert spec.resolved == pytest.approx(50.0)

    def test_usd_not_needs_resolution(self):
        assert not parse_amount("$100").needs_resolution


# ── Suffixed numbers ───────────────────────────────────────────────────────────

class TestSuffixedNumbers:
    def test_k_suffix(self):
        spec = parse_amount("1k")
        assert spec.resolved == pytest.approx(1000.0)
        assert not spec.is_usd

    def test_m_suffix(self):
        spec = parse_amount("2.5m")
        assert spec.resolved == pytest.approx(2_500_000.0)

    def test_b_suffix(self):
        spec = parse_amount("1b")
        assert spec.resolved == pytest.approx(1_000_000_000.0)

    def test_uppercase_k(self):
        spec = parse_amount("5K")
        assert spec.resolved == pytest.approx(5000.0)

    def test_comma_in_base(self):
        spec = parse_amount("1,500k")
        assert spec.resolved == pytest.approx(1_500_000.0)


# ── Plain numbers ──────────────────────────────────────────────────────────────

class TestPlainNumbers:
    def test_integer(self):
        spec = parse_amount("42")
        assert spec.resolved == pytest.approx(42.0)
        assert not spec.is_usd

    def test_float(self):
        spec = parse_amount("3.14159")
        assert spec.resolved == pytest.approx(3.14159)

    def test_comma_separated(self):
        spec = parse_amount("10,000")
        assert spec.resolved == pytest.approx(10000.0)

    def test_zero(self):
        spec = parse_amount("0")
        assert spec.resolved == pytest.approx(0.0)

    def test_not_needs_resolution(self):
        assert not parse_amount("100").needs_resolution


# ── Unparseable input ──────────────────────────────────────────────────────────

class TestUnparseable:
    def test_garbage_returns_raw_spec(self):
        spec = parse_amount("notanumber")
        assert spec.raw == "notanumber"
        assert spec.resolved is None
        assert not spec.is_fraction
        assert not spec.is_all
        assert not spec.is_rest

    def test_empty_string_returns_spec(self):
        spec = parse_amount("")
        assert spec.resolved is None


# ── AmountSpec.needs_resolution property ─────────────────────────────────────

class TestNeedsResolution:
    def test_plain_number_resolved(self):
        assert not parse_amount("5").needs_resolution

    def test_fraction_unresolved(self):
        assert parse_amount("half").needs_resolution

    def test_all_unresolved(self):
        assert parse_amount("all").needs_resolution

    def test_rest_unresolved(self):
        assert parse_amount("rest").needs_resolution

    def test_usd_not_unresolved(self):
        assert not parse_amount("$50").needs_resolution


# ── Numeric emoji input ───────────────────────────────────────────────────────

class TestEmojiAmounts:
    def test_hundred_points(self):
        spec = parse_amount("\U0001f4af")  # 💯
        assert spec.resolved == pytest.approx(100.0)

    def test_keycap_ten(self):
        spec = parse_amount("\U0001f51f")  # 🔟
        assert spec.resolved == pytest.approx(10.0)

    def test_keycap_digits_compose(self):
        spec = parse_amount("1️⃣" "0️⃣" "0️⃣")
        assert spec.resolved == pytest.approx(100.0)

    def test_keycap_single_digit(self):
        spec = parse_amount("5️⃣")
        assert spec.resolved == pytest.approx(5.0)

    def test_emoji_with_dollar_prefix(self):
        spec = parse_amount("$\U0001f4af")  # $💯
        assert spec.is_usd
        assert spec.resolved == pytest.approx(100.0)

    def test_unknown_emoji_does_not_resolve(self):
        # Random emoji like 🎲 should not parse as a number; AmountSpec is
        # returned with raw set but no resolved value -- safe fallthrough.
        spec = parse_amount("\U0001f3b2")  # 🎲
        assert spec.resolved is None
        assert not spec.is_fraction
        assert not spec.is_all

    def test_translate_helper_passthrough_ascii(self):
        assert translate_emoji_amount("100") == "100"
        assert translate_emoji_amount("$1.5k") == "$1.5k"

    def test_translate_helper_strips_orphan_modifiers(self):
        # A keycap digit gets translated; if any orphan VS16 / keycap-combiner
        # remains it must be stripped so downstream regexes still match.
        assert translate_emoji_amount("1️⃣") == "1"

    def test_utils_parse_amount_emoji(self):
        # core.framework.utils.parse_amount is the entry point used by ,dice etc.
        value, is_usd = utils_parse_amount("\U0001f4af")
        assert value == pytest.approx(100.0)
        assert is_usd is False

    def test_utils_parse_amount_keycap_compose(self):
        value, is_usd = utils_parse_amount("1️⃣" "0️⃣")
        assert value == pytest.approx(10.0)
        assert is_usd is False

    def test_utils_parse_amount_unknown_emoji_raises(self):
        with pytest.raises(ValueError):
            utils_parse_amount("\U0001f3b2")  # 🎲
