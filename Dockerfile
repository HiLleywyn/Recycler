# Recycler -- single-image deploy (Railway-ready).
#
# Zero-config: the framework (hilleywyn/framework, public) is pulled from its
# default branch and auto-refreshes on every build (see step 1). No build args,
# tokens, or env vars are required to deploy.
#
# Optional build arg:
#   FRAMEWORK_REF  git ref of hilleywyn/framework to install (default: main)
#
# Example:
#   docker build -t recycler .
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PREFIX=. \
    API_PORT=8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 1. Install the shared framework from its public repo.
#
# AUTOMATIC cache-busting -- no operator action, no env vars, no Railway access.
# The framework is installed from git in a layer Docker would normally cache by
# command text, so a redeploy could ship an OLD framework even after
# hilleywyn/framework@main advances. To avoid that, ADD the GitHub commits API
# response for the ref *first*: its body changes the moment a new commit lands
# on the ref, which invalidates this layer's cache on its own. When nothing has
# changed upstream the layer is reused (fast); when it has, pip reinstalls the
# current framework. Every build therefore tracks the live ref hands-free.
ARG FRAMEWORK_REF=main
ADD https://api.github.com/repos/hilleywyn/framework/commits/${FRAMEWORK_REF} /tmp/framework.commit
RUN pip install --no-cache-dir --force-reinstall \
        "bot-framework @ git+https://github.com/hilleywyn/framework.git@${FRAMEWORK_REF}"

# 2. App-level dependencies.
COPY requirements.txt .
RUN pip install -r requirements.txt

# 3. Application source.
COPY . .

# 4. Fail the build if the test suite is red.
RUN pip install pytest pytest-asyncio \
    && python -m pytest -q tests/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf "http://localhost:${API_PORT:-8080}/health" || exit 1

CMD ["python", "main.py"]
