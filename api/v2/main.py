from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from api.v2 import idempotency
from api.v2.config import get_settings
from api.v2.exceptions import AppError, app_error_handler
from api.v2.middleware.logging import RequestLoggingMiddleware
from api.v2.middleware.rate_limit import RateLimitMiddleware
from api.v2.middleware.events import EventPublishingMiddleware
from api.v2.middleware.security import SecurityMiddleware
from fastapi_swagger_ui_theme import setup_swagger_ui_theme

from api.v2.auth.router import router as auth_router
from api.v2.routers.users import router as users_router
from api.v2.routers.portfolio import router as portfolio_router
from api.v2.routers.market import router as market_router
from api.v2.routers.trading import router as trading_router
from api.v2.routers.pools import router as pools_router
from api.v2.routers.staking import router as staking_router
from api.v2.routers.mining import router as mining_router
from api.v2.routers.blockchain import router as blockchain_router
from api.v2.routers.savings import router as savings_router
from api.v2.routers.lending import router as lending_router
from api.v2.routers.contracts import router as contracts_router
from api.v2.routers.shop import router as shop_router
from api.v2.routers.games import router as games_router
from api.v2.routers.notifications import router as notifications_router
from api.v2.routers.admin import router as admin_router
from api.v2.routers.ws import router as ws_router
from api.v2.routers.groups import router as groups_router
from api.v2.routers.stats import router as stats_router
from api.v2.routers.security import router as security_router
from api.v2.routers.constants import router as constants_router
from api.v2.routers.nfts import router as nfts_router
from api.v2.routers.predictions import router as predictions_router
from api.v2.routers.events import router as events_router
from api.v2.routers.vaults import router as vaults_router
from api.v2.routers.paypal import router as paypal_router
from api.v2.routers.udf import router as udf_router
from api.v2.routers.v3 import router as v3_router

log = logging.getLogger("discoin.api")

# OpenAPI tags for all 17 domains
OPENAPI_TAGS = [
    {"name": "auth", "description": "Authentication, OAuth2, JWT, and 2FA"},
    {"name": "users", "description": "User profiles and settings"},
    {"name": "portfolio", "description": "User portfolio and balances"},
    {"name": "market", "description": "Market data, prices, and charts"},
    {"name": "trading", "description": "Buy, sell, and swap tokens"},
    {"name": "pools", "description": "Liquidity pools"},
    {"name": "staking", "description": "Token staking and rewards"},
    {"name": "mining", "description": "Mining operations"},
    {"name": "blockchain", "description": "On-chain data and transactions"},
    {"name": "savings", "description": "Savings accounts and interest"},
    {"name": "lending", "description": "Lending and borrowing"},
    {"name": "contracts", "description": "Smart contracts and futures"},
    {"name": "shop", "description": "In-game shop and items"},
    {"name": "games", "description": "Mini-games and gambling"},
    {"name": "notifications", "description": "User notifications"},
    {"name": "admin", "description": "Administration and moderation"},
    {"name": "ws", "description": "WebSocket real-time feeds"},
    {"name": "stats", "description": "Server statistics and leaderboards"},
    {"name": "groups", "description": "Mining group management"},
    {"name": "security", "description": "Security system  -  threat detection and enforcement"},
    {"name": "constants", "description": "Public business constants for the frontend"},
    {"name": "nfts", "description": "NFT collections, marketplace, and ownership"},
    {"name": "predictions", "description": "Prediction markets and betting"},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialize and tear down shared resources."""

    # If running inside the bot process, the bot attaches itself after create_app()
    # In that case, skip creating our own pools -- we'll use the bot's DB
    if hasattr(app.state, 'bot') and app.state.bot is not None:
        log.info("Running inside bot process  -  using bot's database connection")
        # Ensure db_pool attribute exists even when skipping standalone init
        if not hasattr(app.state, 'db_pool'):
            app.state.db_pool = None

        # Still connect to Redis even when embedded in bot process
        settings = get_settings()
        try:
            app.state.redis = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
            )
            await app.state.redis.ping()
            log.info("Redis connected.")
        except Exception as exc:
            log.warning("Redis unavailable (%s)  -  running without cache/pubsub", exc)
            app.state.redis = None

        idempotency.init(app.state.redis)

        # Initialize security engine
        await _init_security_engine(app)

        yield

        await _shutdown_security_engine(app)
        if app.state.redis is not None:
            await app.state.redis.aclose()
        return

    settings = get_settings()

    # --- Security check: reject default JWT secret in production ---
    if settings.JWT_SECRET == "change-me-in-production":
        import os
        if os.getenv("DEBUG", "").upper() != "TRUE":
            raise RuntimeError(
                "JWT_SECRET is still the default value. "
                "Set a secure JWT_SECRET environment variable before running in production. "
                "To bypass in development, set DEBUG=true."
            )
        log.warning("JWT_SECRET is still using the default value; auth is not production-safe.")

    # --- Startup ---
    # PostgreSQL (graceful: allow startup without DB for dev/swagger browsing)
    try:
        log.info("Connecting to PostgreSQL...")
        app.state.db_pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=2,
            max_size=20,
        )
        log.info("PostgreSQL pool created.")
    except Exception as exc:
        log.warning("PostgreSQL unavailable (%s)  -  running without database", exc)
        app.state.db_pool = None

    # Redis (graceful: allow startup without Redis)
    try:
        log.info("Connecting to Redis...")
        app.state.redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
        await app.state.redis.ping()
        log.info("Redis connected.")
    except Exception as exc:
        log.warning("Redis unavailable (%s)  -  running without cache/pubsub", exc)
        app.state.redis = None

    idempotency.init(app.state.redis)

    # Initialize security engine
    await _init_security_engine(app)

    yield

    # --- Shutdown ---
    await _shutdown_security_engine(app)
    if app.state.redis is not None:
        log.info("Closing Redis connection...")
        await app.state.redis.aclose()

    if app.state.db_pool is not None:
        log.info("Closing PostgreSQL pool...")
        await app.state.db_pool.close()

    log.info("Shutdown complete.")


async def _init_security_engine(app: FastAPI) -> None:
    """Initialize the security engine and attach it to app state."""
    if not get_settings().SECURITY_SYSTEM:
        log.info("Security engine disabled via SECURITY_SYSTEM=false")
        app.state.security_engine = None
        app.state.security_db = None
        return
    try:
        from security.engine import SecurityEngine
        from database.security import SecurityRepository

        redis = getattr(app.state, "redis", None)
        db_repo = None

        # Try to create SecurityRepository from available pool
        pool = getattr(app.state, "db_pool", None)
        bot = getattr(app.state, "bot", None)
        if pool:
            db_repo = SecurityRepository(pool)
        elif bot and getattr(bot, "db", None):
            bot_pool = getattr(bot.db, "_pool", None)
            if bot_pool:
                db_repo = SecurityRepository(bot_pool)

        engine = SecurityEngine(redis=redis, db=db_repo)
        await engine.start()
        app.state.security_engine = engine
        app.state.security_db = db_repo
        log.info("Security engine initialized.")
    except Exception as exc:
        log.warning("Security engine initialization failed (%s)  -  running without security", exc)
        app.state.security_engine = None
        app.state.security_db = None


async def _shutdown_security_engine(app: FastAPI) -> None:
    """Shut down the security engine."""
    engine = getattr(app.state, "security_engine", None)
    if engine:
        await engine.stop()


def create_app() -> FastAPI:
    """Application factory: build and return the configured FastAPI instance."""
    settings = get_settings()

    app = FastAPI(
        title="Discoin API",
        version="2.0.0",
        description=(
            "Discord economy bot with crypto markets, mining, staking, "
            "AMM liquidity pools, lending, gambling, and a web dashboard."
        ),
        docs_url=None,  # disabled  -  themed Swagger UI registered below
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )

    # --- Themed Swagger UI (dark/light mode support) ---
    setup_swagger_ui_theme(
        app,
        docs_path="/api/docs",
        title="Discoin API",
        swagger_ui_parameters={
            "persistAuthorization": True,
            "filter": True,
            "deepLinking": True,
            "displayRequestDuration": True,
            "docExpansion": "none",
            "defaultModelsExpandDepth": 0,
        },
    )

    # --- Exception handlers ---
    app.add_exception_handler(AppError, app_error_handler)

    # --- Middleware (order matters: last added = first executed) ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityMiddleware)
    app.add_middleware(EventPublishingMiddleware)

    # --- Routers ---
    prefix = "/api/v2"
    app.include_router(auth_router, prefix=prefix)
    # Also mount auth at /api so the v1 redirect URI (/api/auth/callback) works
    app.include_router(auth_router, prefix="/api", include_in_schema=False)
    app.include_router(users_router, prefix=prefix)
    app.include_router(portfolio_router, prefix=prefix)
    app.include_router(market_router, prefix=prefix)
    app.include_router(trading_router, prefix=prefix)
    app.include_router(pools_router, prefix=prefix)
    app.include_router(staking_router, prefix=prefix)
    app.include_router(mining_router, prefix=prefix)
    app.include_router(blockchain_router, prefix=prefix)
    app.include_router(savings_router, prefix=prefix)
    app.include_router(lending_router, prefix=prefix)
    app.include_router(contracts_router, prefix=prefix)
    app.include_router(shop_router, prefix=prefix)
    app.include_router(games_router, prefix=prefix)
    app.include_router(notifications_router, prefix=prefix)
    app.include_router(admin_router, prefix=prefix)
    app.include_router(ws_router, prefix=prefix)
    app.include_router(groups_router, prefix=prefix)
    app.include_router(stats_router, prefix=prefix)
    app.include_router(security_router, prefix=prefix)
    app.include_router(constants_router, prefix=prefix)
    app.include_router(nfts_router, prefix=prefix)
    app.include_router(predictions_router, prefix=prefix)
    app.include_router(events_router, prefix=prefix)
    app.include_router(vaults_router, prefix=prefix)
    app.include_router(paypal_router, prefix=prefix)
    # TradingView UDF live feed (open-CORS, no auth) -- mounted at
    # ``/api/v2/udf``. Speaks the Charting Library Datafeed spec so any
    # external TradingView client can pull live OHLC + symbol-search from
    # the bot's existing provider stack (Yahoo + CoinGecko + DexScreener).
    app.include_router(udf_router, prefix=prefix)
    app.include_router(v3_router, prefix=prefix)

    # --- Health check ---
    @app.get("/health", tags=["health"])
    @app.get("/api/v2/health", tags=["health"])
    async def health_check(request: Request) -> dict:
        """Health check endpoint for load balancers and monitoring."""
        result: dict = {"status": "healthy", "version": "2.0.0"}

        # Database check
        pool = getattr(request.app.state, "db_pool", None)
        bot = getattr(request.app.state, "bot", None)
        if pool:
            try:
                async with pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                result["db"] = "connected"
            except Exception:
                result["db"] = "error"
                result["status"] = "degraded"
        elif bot and getattr(bot, "db", None):
            try:
                await bot.db.fetch_val("SELECT 1")
                result["db"] = "connected"
            except Exception:
                result["db"] = "error"
                result["status"] = "degraded"
        else:
            result["db"] = "unavailable"
            result["status"] = "degraded"

        # Redis check
        redis = getattr(request.app.state, "redis", None)
        if redis:
            try:
                await redis.ping()
                result["redis"] = "connected"
            except Exception:
                result["redis"] = "error"
        else:
            result["redis"] = "unavailable"

        # Bot check (when running in-process)
        if bot:
            result["bot"] = "ready" if bot.is_ready() else "connecting"
            result["guilds"] = len(bot.guilds) if bot.is_ready() else 0
            result["startup_phase"] = getattr(bot, "startup_phase", "unknown")

        return result

    # --- Static frontend (Next.js static export) ---
    # Serve MkDocs documentation site at /docs (built in Docker docs stage)
    _docs_site = pathlib.Path(__file__).resolve().parent.parent / "docs-site"
    if _docs_site.is_dir():
        app.mount("/docs", StaticFiles(directory=str(_docs_site), html=True), name="mkdocs")

    # Docker: COPY --from=frontend places build output in api/static/
    # Local dev: api/static/ may not exist (run `cd frontend && npm run build`)
    _static_dir = pathlib.Path(__file__).resolve().parent.parent / "static"

    if _static_dir.is_dir():
        # Serve uploaded NFT gallery images at /nft-images/<guild_id>/<SYMBOL>/
        _nft_images_dir = _static_dir / "nft-images"
        _nft_images_dir.mkdir(exist_ok=True)
        app.mount("/nft-images", StaticFiles(directory=str(_nft_images_dir)), name="nft-images")

        # Serve Next.js _next/ assets (JS, CSS, media)
        _next_dir = _static_dir / "_next"
        if _next_dir.is_dir():
            app.mount("/_next", StaticFiles(directory=str(_next_dir)), name="next-assets")

        # Serve root-level static files (favicon, logo, manifest, etc.)
        # Uses a middleware instead of a catch-all route so it doesn't
        # swallow SPA routes like /dashboard/*.
        _STATIC_EXTENSIONS = {
            ".ico", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
            ".webmanifest", ".xml", ".txt", ".woff", ".woff2",
        }

        @app.middleware("http")
        async def serve_root_static(request: Request, call_next):
            """Intercept GET requests for root-level static assets before
            they hit the SPA catch-all. Only fires for paths with a known
            static file extension (e.g. /logo.jpg, /favicon.ico)."""
            if request.method == "GET":
                req_path = request.url.path.lstrip("/")
                ext = pathlib.Path(req_path).suffix.lower()
                if ext in _STATIC_EXTENSIONS and "/" not in req_path:
                    candidate = _static_dir / req_path
                    if (
                        candidate.resolve().is_relative_to(_static_dir.resolve())
                        and candidate.is_file()
                    ):
                        return FileResponse(str(candidate))
            return await call_next(request)

        def _resolve_static(subpath: str) -> FileResponse:
            """Try exact file, then directory/index.html, then dynamic-route
            fallback (Next.js static export uses _ as the catch-all param),
            then dashboard index."""
            _root = _static_dir.resolve()

            candidate = (_static_dir / subpath).resolve()
            # Path traversal guard: reject paths that escape the static directory
            if not candidate.is_relative_to(_root):
                return FileResponse(str(_static_dir / "index.html"))

            if candidate.is_dir():
                idx = candidate / "index.html"
                if idx.resolve().is_relative_to(_root) and idx.is_file():
                    return FileResponse(str(idx))
            if candidate.is_file():
                return FileResponse(str(candidate))
            # Dynamic route fallback: replace the last path segment with _
            # (Next.js static export generates /profile/_/index.html for [userId])
            parts = subpath.rstrip("/").split("/")
            if len(parts) >= 2:
                dynamic = "/".join(parts[:-1] + ["_"])
                dyn_idx = (_static_dir / dynamic / "index.html").resolve()
                if dyn_idx.is_relative_to(_root) and dyn_idx.is_file():
                    return FileResponse(str(dyn_idx))
            # Fall back to dashboard index for client-side routing
            fallback = _static_dir / "dashboard" / "index.html"
            if fallback.is_file():
                return FileResponse(str(fallback))
            return FileResponse(str(_static_dir / "index.html"))

        # SPA catch-all: serve index.html for / and /dashboard/* routes
        # Must support both GET and HEAD (Next.js prefetches with HEAD)
        # This must be registered LAST so API routes take priority
        @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
        async def serve_root():
            index = _static_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return HTMLResponse("<h1>Dashboard not built</h1>", status_code=404)

        @app.api_route("/dashboard", methods=["GET", "HEAD"], include_in_schema=False)
        async def serve_dashboard_root():
            return _resolve_static("dashboard")

        @app.api_route("/dashboard/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        async def serve_dashboard(request: Request, path: str = ""):
            return _resolve_static(f"dashboard/{path}")

    # Absorb Cloudflare-injected beacon requests so they don't log 404s.
    # Cloudflare injects /cdn-cgi/rum and /cdn-cgi/beacon/expect-ct when the
    # site runs behind its proxy. The backend has no handler for these paths
    # so they produce noisy 404s. Return 204 No Content to silence them.
    @app.api_route("/cdn-cgi/{path:path}", methods=["GET", "HEAD", "POST"], include_in_schema=False)
    async def suppress_cloudflare_beacon(path: str) -> Response:
        return Response(status_code=204)

    return app


# Default app instance for uvicorn (e.g. `uvicorn api.v2.main:app`)
app = create_app()
