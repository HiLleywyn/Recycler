"""api/v2/main.py -- the embedded REST API.

The framework starts this automatically when ``API_PORT`` is set: it calls
``create_app(bot)`` and serves the returned FastAPI app with uvicorn. The
``/health`` endpoint is public (used by the Docker/Railway healthcheck);
everything under ``/api/v2`` requires the ``X-API-Key`` header to match the
``CLANK_API_KEY`` env var. If that var is unset the data endpoints are
disabled (health still works).
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from api.v2.exceptions import (
    AppError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)


def create_app(bot: Any = None) -> FastAPI:
    # The framework calls ``create_app()`` with no arguments and then sets
    # ``app.state.bot = self`` immediately after (see
    # ``FrameworkBot._ensure_api_server_started``). ``bot`` is therefore
    # optional; handlers read the live bot via ``app.state.bot``.
    app = FastAPI(title="Recycler API", version="2.0.0", docs_url="/api/docs")
    app.state.bot = bot

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    async def require_key(x_api_key: str | None = Header(default=None)) -> None:
        configured = os.getenv("CLANK_API_KEY", "").strip()
        if not configured:
            raise ForbiddenError("API is disabled (CLANK_API_KEY is not set).")
        if not x_api_key or x_api_key != configured:
            raise UnauthorizedError("Invalid or missing X-API-Key.")

    def _db():  # type: ignore[no-untyped-def]
        db = getattr(app.state.bot, "db", None)
        if db is None:
            raise AppError("Database is not ready.")
        return db

    # -- public ----------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        b = app.state.bot
        return {
            "status": "ok",
            "bot": getattr(getattr(b, "user", None), "name", None),
            "guilds": len(getattr(b, "guilds", []) or []),
            "ready": bool(getattr(b, "is_ready", lambda: False)()),
        }

    # -- backups ---------------------------------------------------------------

    @app.get("/api/v2/backups", dependencies=[Depends(require_key)])
    async def list_backups(owner_id: int) -> dict[str, Any]:
        rows = await _db().backups.list_for_owner(int(owner_id))
        return {"backups": [dict(r) for r in rows]}

    @app.get("/api/v2/backups/{backup_id}", dependencies=[Depends(require_key)])
    async def get_backup(backup_id: str) -> dict[str, Any]:
        row = await _db().backups.get(backup_id.lower())
        if row is None:
            raise NotFoundError(f"No backup {backup_id!r}.")
        return dict(row)

    # -- templates -------------------------------------------------------------

    @app.get("/api/v2/templates", dependencies=[Depends(require_key)])
    async def browse_templates(q: str = "", limit: int = 25) -> dict[str, Any]:
        rows = await _db().templates.browse(query=q, limit=min(int(limit), 100))
        return {"templates": [dict(r) for r in rows]}

    @app.get("/api/v2/templates/{template_id}", dependencies=[Depends(require_key)])
    async def get_template(template_id: str) -> dict[str, Any]:
        row = await _db().templates.get(template_id.lower())
        if row is None:
            raise NotFoundError(f"No template {template_id!r}.")
        return dict(row)

    # -- per-guild settings ----------------------------------------------------

    @app.get("/api/v2/guilds/settings/schema", dependencies=[Depends(require_key)])
    async def guild_settings_schema() -> dict[str, Any]:
        from clanklib.guild_schema import schema_json
        return schema_json()

    @app.get("/api/v2/guilds/{guild_id}/settings", dependencies=[Depends(require_key)])
    async def get_guild_settings(guild_id: int) -> dict[str, Any]:
        from clanklib.guild_schema import public_view
        row = await _db().get_guild_settings(int(guild_id))
        return public_view(row)

    @app.patch("/api/v2/guilds/{guild_id}/settings", dependencies=[Depends(require_key)])
    async def patch_guild_settings(guild_id: int, body: dict[str, Any]) -> dict[str, Any]:
        from clanklib.guild_schema import public_view, validate_guild_settings
        coerced, errors = validate_guild_settings(body or {})
        if errors:
            raise ValidationError("; ".join(errors))
        db = _db()
        for key, value in coerced.items():
            await db.update_guild_setting(int(guild_id), key, value)
        row = await db.get_guild_settings(int(guild_id))
        return {"ok": True, "settings": public_view(row)}

    return app

