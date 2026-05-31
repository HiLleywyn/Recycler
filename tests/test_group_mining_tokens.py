"""Tests for group mining token derivation.

Covers:
  - make_group_token: symbol from tag, name from group name
  - symbol sanitization (non-alnum stripped, truncated to 5)
  - fallback when tag is all symbols / empty result after strip
  - token_name truncation at 50 chars
  - auto-create path in group_set: new token registered when tag is first set
  - auto-create skipped when symbol already exists (collision guard)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.framework.content_filter import make_group_token


# ── make_group_token unit tests ───────────────────────────────────────────────

class TestMakeGroupToken:
    def test_symbol_is_tag_uppercased(self):
        sym, _ = make_group_token("Degen Miners", "ngmi")
        assert sym == "NGMI"

    def test_symbol_strips_non_alnum(self):
        sym, _ = make_group_token("Degen Miners", "N@G#M!")
        assert sym == "NGM"

    def test_symbol_truncated_to_4(self):
        sym, _ = make_group_token("Degen Miners", "TOOLONG")
        assert sym == "TOOL"
        assert len(sym) <= 4

    def test_symbol_already_4_preserved(self):
        sym, _ = make_group_token("Degen Miners", "ALPH")
        assert sym == "ALPH"

    def test_symbol_fallback_from_group_name_initials(self):
        # Tag that becomes empty after stripping non-alnum chars
        sym, _ = make_group_token("Night Owls Guild", "---")
        # Fallback: N O G  (first char of each word)
        assert sym == "NOG"

    def test_symbol_fallback_single_word_group(self):
        sym, _ = make_group_token("Illuminati", "!!!")
        assert sym == "I"

    def test_token_name_is_group_name_plus_token(self):
        _, name = make_group_token("Moon Chasers", "MOON")
        assert name == "Moon Chasers Token"

    def test_token_name_truncated_to_50(self):
        long_name = "A" * 48  # "AAAA...A Token" = 54 chars  -  gets cut
        _, name = make_group_token(long_name, "X")
        assert len(name) <= 50

    def test_token_name_strips_leading_trailing_whitespace(self):
        _, name = make_group_token("  Whitespace Club  ", "WC")
        assert not name.startswith(" ")

    def test_all_numeric_tag_passes(self):
        sym, _ = make_group_token("Group 9000", "9000")
        assert sym == "9000"

    def test_mixed_case_tag_normalized(self):
        sym, _ = make_group_token("Hybrid Gang", "HyBr")
        assert sym == "HYBR"

    def test_empty_tag_falls_back_to_initials(self):
        sym, _ = make_group_token("Solo Survivor", "")
        assert sym == "SS"


# ── _ensure_group_token integration tests ────────────────────────────────────

def _make_db(existing_tokens=None):
    db = MagicMock()
    # get_all_tokens_for_guild returns a dict keyed by symbol (same shape as Config.TOKENS)
    if existing_tokens is None:
        mock_return: dict = {}
    elif isinstance(existing_tokens, dict):
        mock_return = existing_tokens
    else:
        # accept list-of-dicts for convenience: [{"symbol": "X"}] -> {"X": {}}
        mock_return = {t["symbol"]: t for t in existing_tokens}
    db.get_all_tokens_for_guild = AsyncMock(return_value=mock_return)
    db.add_guild_token = AsyncMock()
    db.execute = AsyncMock()
    db.disable_token = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_ensure_group_token_creates_token():
    """First-time tag creates the group token with correct symbol and name."""
    from cogs.groups import _ensure_group_token

    db = _make_db()
    result = await _ensure_group_token(db, guild_id=123, group_name="Degen Miners", tag="NGMI")

    assert result == "NGMI"
    db.add_guild_token.assert_called_once()
    call_args = db.add_guild_token.call_args
    # positional: guild_id, symbol, name, emoji, consensus, network, price, vol
    assert call_args.args[0] == 123
    assert call_args.args[1] == "NGMI"
    assert call_args.args[2] == "Degen Miners Token"
    assert call_args.args[4] == "PoW"
    assert call_args.args[5] == "Moon Network"  # bridged pseudo-network
    assert call_args.args[6] == 0.01       # genesis price


@pytest.mark.asyncio
async def test_ensure_group_token_skips_on_collision():
    """If the derived symbol already exists, returns None and skips creation."""
    from cogs.groups import _ensure_group_token

    db = _make_db(existing_tokens=[{"symbol": "NGMI"}])
    result = await _ensure_group_token(db, guild_id=123, group_name="Degen Miners", tag="NGMI")

    assert result is None
    db.add_guild_token.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_group_token_rejects_builtin_symbol():
    """A tag that resolves to a built-in token symbol (MTA, SUN, ARC, ...) must
    be rejected so the group token never shadows the native token across the
    shared crypto_prices/pools/tx-history namespace -- the bug that left
    Moon Network 'MTA' rows un-swappable in production."""
    from cogs.groups import _ensure_group_token

    db = _make_db()  # no existing guild_tokens entries
    result = await _ensure_group_token(db, guild_id=123, group_name="Moneta Bandits", tag="MTA")

    assert result is None
    db.add_guild_token.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_group_token_seeds_price_row():
    """A crypto_prices row is inserted alongside the token registration."""
    from cogs.groups import _ensure_group_token

    # Tag must not collide with any Config.TOKENS symbol -- the collision
    # guard in _ensure_group_token short-circuits before any DB write.
    # "LUNA" is a reasonable moon-themed tag that is not a built-in.
    db = _make_db()
    await _ensure_group_token(db, guild_id=99, group_name="Moon Chasers", tag="LUNA")

    price_inserts = [
        c for c in db.execute.call_args_list
        if "crypto_prices" in str(c)
    ]
    assert len(price_inserts) == 1
    assert "LUNA" in str(price_inserts[0])
    assert "99" in str(price_inserts[0])


@pytest.mark.asyncio
async def test_ensure_group_token_sanitized_tag():
    """Tags with special chars get sanitized before registration."""
    from cogs.groups import _ensure_group_token

    db = _make_db()
    result = await _ensure_group_token(db, guild_id=1, group_name="Test Group", tag="$$$H1")

    assert result == "H1"
    db.add_guild_token.assert_called_once()
    assert db.add_guild_token.call_args.args[1] == "H1"


@pytest.mark.asyncio
async def test_ensure_group_token_pure_symbol_tag_falls_back():
    """Tag that strips to empty falls back to group name initials."""
    from cogs.groups import _ensure_group_token

    db = _make_db()
    result = await _ensure_group_token(db, guild_id=1, group_name="Alpha Beta Crew", tag="---")

    assert result == "ABC"
    db.add_guild_token.assert_called_once()
