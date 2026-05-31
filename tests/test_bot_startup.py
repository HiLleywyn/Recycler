"""Startup-order tests for the bot bootstrap path.

These tests focus on deployment stability: the HTTP server should begin
listening before slow Discord startup work (cog loading / slash sync) so
Railway health checks do not see a long connection-refused window.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.framework.bot as bot_mod


class _FakeRedisBus:
    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        self.is_connected = False

    async def connect(self) -> None:
        self.is_connected = False

    async def close(self) -> None:
        return None


class _FakeDatabase:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, *_args, **_kwargs):
        return None

    async def seed_pools(self, *_args, **_kwargs):
        return None


class _FakeUvicornConfig:
    def __init__(self, app, **kwargs) -> None:
        self.app = app
        self.kwargs = kwargs


class _FakeUvicornServer:
    def __init__(self, config) -> None:
        self.config = config
        self.should_exit = False

    async def serve(self) -> None:
        return None


@pytest.mark.asyncio
async def test_setup_hook_starts_api_server_before_loading_extensions(monkeypatch):
    monkeypatch.setattr(bot_mod, "RedisBus", _FakeRedisBus)
    monkeypatch.setattr(bot_mod, "Database", _FakeDatabase)
    monkeypatch.setattr(bot_mod._live_engine, "init", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot_mod._live_engine, "stop", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot_mod, "setup_internal_commands_cog", AsyncMock())
    monkeypatch.setattr(bot_mod, "COGS", ["cogs.alpha"])

    import uvicorn
    fake_api_main = types.ModuleType("api.v2.main")
    fake_api_main.create_app = lambda: SimpleNamespace(state=SimpleNamespace())
    if "fastapi_swagger_ui_theme" not in sys.modules:
        monkeypatch.setitem(
            sys.modules,
            "fastapi_swagger_ui_theme",
            SimpleNamespace(setup_swagger_ui_theme=lambda *args, **kwargs: None),
        )
    monkeypatch.setitem(sys.modules, "api.v2.main", fake_api_main)

    monkeypatch.setattr(uvicorn, "Config", _FakeUvicornConfig)
    monkeypatch.setattr(uvicorn, "Server", _FakeUvicornServer)

    bot = bot_mod.Discoin()
    bot.tree.sync = AsyncMock(return_value=[])
    bot.tree.copy_global_to = MagicMock()

    loaded: list[str] = []

    async def _fake_load_extension(name: str) -> None:
        assert bot._api_server_task is not None
        loaded.append(name)

    bot.load_extension = _fake_load_extension  # type: ignore[method-assign]

    await bot.setup_hook()

    assert loaded == ["cogs.alpha"]
    assert bot._api_server_task is not None
    assert bot.startup_phase == "commands_synced"

    await bot.close()