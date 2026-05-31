# ── Stage 1: build Next.js v2 dashboard (static export) ───────────────────────
FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline
COPY frontend/ .
RUN npm run build

# ── Stage 2: Python dependencies (uv sync from lockfile) ─────────────────────
FROM python:3.12-slim-bookworm AS deps

WORKDIR /app

# Pull the uv / uvx binaries from the official Astral image. uv is ~10-100x
# faster than pip and has a much better resolver + cache story for Docker.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg gosu \
        redis-server redis-tools \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg \
    && echo "deb https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        postgresql-18 postgresql-client-18 \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

# uv: install into the system interpreter, compile bytecode, no cache layer.
# UV_PROJECT_ENVIRONMENT points uv sync at the system Python site-packages so
# packages land where plain `python` can find them (no .venv created).
ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local

# Playwright + Chromium live in their OWN layer BEFORE pyproject.toml/uv.lock
# are copied, so bumping any Python dep does not invalidate the ~500MB
# Chromium download. This layer only rebuilds when the pinned playwright
# version or the base image changes.
ARG PLAYWRIGHT_VERSION=1.48.0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv pip install --system "playwright==${PLAYWRIGHT_VERSION}" \
    && playwright install chromium \
    && playwright install-deps chromium \
    && chmod -R 755 /ms-playwright

COPY pyproject.toml uv.lock .
RUN uv sync --frozen --no-group test --no-group docs

# ── Stage 3: build documentation (cached unless docs/ or mkdocs.yml change) ──
FROM deps AS docs

WORKDIR /app

RUN uv sync --frozen --group docs

# Copy only what mkdocs needs - cache busts only when docs or config change
COPY mkdocs.yml .
COPY docs/ docs/

# Build static docs site
RUN mkdocs build --strict --site-dir /app/docs-site 2>&1 || true

# ── Stage 4: run tests (build aborts here if any test fails) ─────────────────
FROM deps AS test

WORKDIR /app

RUN uv sync --frozen --group test

COPY . .
RUN python -m pytest tests/ -q --no-header --tb=short \
    && echo "tests-passed" > /tmp/tests-passed

# ── Stage 5: final image ─────────────────────────────────────────────────────
FROM deps AS final

LABEL org.opencontainers.image.title="Discoin" \
      org.opencontainers.image.description="Discord Economy Bot"

WORKDIR /app

# Copy application source
COPY . .

# Inject the Next.js static export - served by FastAPI at / and /dashboard
COPY --from=frontend /build/out ./api/static

# Inject built documentation site - served at /docs
COPY --from=docs /app/docs-site ./api/docs-site

# Ensure the test stage ran (and passed) before we finalise the image
COPY --from=test /tmp/tests-passed /tmp/tests-passed

# Persistent data dir + non-root user
RUN mkdir -p /data/pgdata /data/backups \
    && useradd -u 1000 -s /bin/false -M discoin \
    && chown -R discoin /app \
    && chown -R postgres:postgres /data/pgdata \
    && sed -i 's/\r//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Runtime defaults - secrets supplied at runtime via .env / secret manager
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    \
    DATABASE_URL=postgresql://discoin:discoin@localhost:5432/discoin \
    REDIS_URL=redis://localhost:6379 \
    DB_SSL_VERIFY=0 \
    REDIS_SSL_VERIFY=0 \
    PREFIX= \
    SLASH_GUILD_ID= \
    REPORT_TARGET_USER_ID= \
    \
    API_PORT=8080 \
    DASHBOARD_URL= \
    \
    DISCORD_CLIENT_ID= \
    DISCORD_CLIENT_SECRET= \
    DISCORD_REDIRECT_URI=http://localhost:8080/api/auth/callback \
    JWT_SECRET=change-me-in-production \
    JWT_EXPIRE_SECONDS=604800 \
    \
    OPENROUTER_MODEL=openrouter/hunter-alpha \
    AI_MM_ENABLED=1 \
    AI_CHAT_ENABLED=1 \
    AI_COMMENTARY_ENABLED=1 \
    AI_FLAVOR_ENABLED=0 \
    AI_EVENTS_ENABLED=1 \
    \
    STARTING_BALANCE=1000 \
    DAILY_AMOUNT=1000 \
    WORK_COOLDOWN=900 \
    \
    AUTO_DROP_INTERVAL=1800 \
    DROP_MIN=100 \
    DROP_MAX=2000 \
    DROP_COLLECT_WINDOW=30 \
    \
    MAX_BET=500000 \
    \
    ANTIBOT_MIN_GAMES=50 \
    ANTIBOT_MAX_GAMES=100 \
    HASHSTONE_XP_RATE=40.0 \
    \
    CHAIN_BLOCK_INTERVAL=1800 \
    \
    AUTO_SEED_POOLS=false \
    POOL_SEED_STABLECOIN=500000 \
    \
    BACKUP_INTERVAL_HOURS=6 \
    BACKUP_KEEP=7 \
    BACKUP_MAX_AGE_DAYS=0 \
    \
    WALLET_PLATFORM_FEE_PCT=0.002 \
    WALLET_PLATFORM_FEE_MIN=0.10 \
    WALLET_PLATFORM_FEE_MAX=20.00 \
    \
    REAL_MARKET_ENABLED=true \
    REAL_MARKET_API_BASE=https://api.coingecko.com/api/v3 \
    REAL_MARKET_HTTP_TIMEOUT=10 \
    REAL_MARKET_CACHE_TTL_OHLC=60 \
    REAL_MARKET_CACHE_TTL_OVIEW=60 \
    REAL_MARKET_CACHE_TTL_NEWS=300 \
    REAL_MARKET_CACHE_TTL_SYMBOL=86400 \
    COINGECKO_API_KEY= \
    \
    DEBUG=false

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -sf http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
