"""Tests for api.v2.config settings loading behavior."""
from __future__ import annotations

from api.v2.config import Settings


def test_settings_ignore_unrelated_environment_variables(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/test")
    monkeypatch.setenv("DISCORD_TOKEN", "not-used-by-api-settings")
    monkeypatch.setenv("REPORT_TARGET_USER_ID", "}")

    settings = Settings()

    assert settings.DATABASE_URL == "postgresql://example/test"


def test_settings_preserve_declared_fields(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "super-secret")
    monkeypatch.setenv("API_PORT", "8123")

    settings = Settings()

    assert settings.JWT_SECRET == "super-secret"
    assert settings.API_PORT == 8123
