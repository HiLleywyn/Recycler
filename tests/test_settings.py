"""Tests for clanklib.settings -- the live-config resolution layer.

The point of this module is that config changes pushed from the Auren UI
(into ``bot.settings``) take effect without a redeploy, with env and defaults
as fallbacks. These tests pin that precedence with a stub bot.
"""
from __future__ import annotations

import pytest

from clanklib.settings import prefix, setting, setting_int


class _StubSettings:
    """Mimics the framework's Settings.get (the control link target)."""

    def __init__(self, values: dict) -> None:
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


class _StubBot:
    def __init__(self, values: dict | None = None) -> None:
        self.settings = _StubSettings(values or {})


def test_bot_settings_take_precedence_over_env(monkeypatch):
    monkeypatch.setenv("PREFIX", "!")
    bot = _StubBot({"PREFIX": "?"})
    # Live bot.settings wins over the environment.
    assert setting(bot, "PREFIX") == "?"
    assert prefix(bot) == "?"


def test_env_used_when_settings_absent(monkeypatch):
    monkeypatch.setenv("PREFIX", "!")
    bot = _StubBot({})  # nothing pushed yet
    assert setting(bot, "PREFIX") == "!"


def test_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("PREFIX", raising=False)
    bot = _StubBot({})
    assert setting(bot, "PREFIX", ".") == "."
    assert prefix(bot) == "."


def test_blank_values_fall_through(monkeypatch):
    monkeypatch.delenv("CLANK_API_KEY", raising=False)
    # An empty string from settings is treated as unset (falls to env/default).
    bot = _StubBot({"CLANK_API_KEY": ""})
    assert setting(bot, "CLANK_API_KEY", "fallback") == "fallback"


def test_setting_int_coerces_and_defaults():
    assert setting_int(_StubBot({"BACKUP_MAX_PER_USER": "25"}), "BACKUP_MAX_PER_USER", 50) == 25
    assert setting_int(_StubBot({"BACKUP_MAX_PER_USER": "nope"}), "BACKUP_MAX_PER_USER", 50) == 50
    assert setting_int(_StubBot({}), "BACKUP_MAX_PER_USER", 50) == 50


def test_live_update_is_reflected():
    """A control-link push (settings.update equivalent) is seen immediately."""
    bot = _StubBot({"BACKUP_MAX_PER_USER": 10})
    assert setting_int(bot, "BACKUP_MAX_PER_USER", 50) == 10
    bot.settings._v["BACKUP_MAX_PER_USER"] = 99  # simulate a live push
    assert setting_int(bot, "BACKUP_MAX_PER_USER", 50) == 99


def test_missing_settings_attr_is_safe(monkeypatch):
    monkeypatch.setenv("PREFIX", "%")

    class _Bare:
        pass

    # A bot without a .settings attribute still resolves via env/default.
    assert setting(_Bare(), "PREFIX", ".") == "%"
