# Recycler — the ,clanker containment bot.
#
# The framework is BUILT FROM THE FRAMEWORK REPO at image-build time (see the
# pip install of bot-framework below). Recycler itself ships only its feature
# code (cogs/clanktank.py) + manifest + entrypoint.

FROM python:3.12-slim-bookworm AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ── Build + install the framework straight from the Framework repo ────────────
# requirements.txt pins `bot-framework @ git+https://github.com/HiLleywyn/Framework.git@<ref>`.
# git is needed so pip can clone + build it. This single step pulls in the whole
# shared runtime + data plane (core, constants, security, database).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Recycler's own (clanker-only) source ──────────────────────────────────────
COPY . .

# Recycler is cloud-native: point it at a managed PostgreSQL + Redis via
# DATABASE_URL / REDIS_URL. The framework's data plane runs schema.sql +
# migrations (including the clanker tables) automatically on first connect.
ENV APP_NAME=Recycler \
    PREFIX="," \
    DATABASE_URL=postgresql://recycler:recycler@localhost:5432/recycler \
    REDIS_URL=redis://localhost:6379 \
    API_PORT= \
    DEBUG=false

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "main.py"]
