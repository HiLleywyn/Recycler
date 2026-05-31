# Discoin -- developer task runner.
#
# All targets are .PHONY (no file-based deps). Targets are short, predictable,
# and intentionally thin so behaviour stays defined in pyproject.toml,
# pytest.ini, mkdocs.yml, docker-compose.yml, and the scripts under scripts/.

PYTHON ?= python
UV     ?= uv
PYTEST ?= $(PYTHON) -m pytest

.DEFAULT_GOAL := help

.PHONY: help install install-test install-docs run test test-fast lint \
	docs docs-serve docs-build up down logs build clean changelog \
	migrate-config-v2 reset-chains check-ascii

help:  ## Show this help.
	@grep -E '^[a-zA-Z][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) \
		| awk -F':.*## ' '{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Dependencies ──────────────────────────────────────────────────────────────

install:  ## Install runtime dependencies (uv preferred).
	$(UV) pip install --system -r requirements.txt

install-test:  ## Install runtime + test dependencies.
	$(UV) pip install --system -r requirements.txt -r requirements-test.txt

install-docs:  ## Install runtime + docs dependencies.
	$(UV) pip install --system -r requirements.txt -r requirements-docs.txt

# ── Run ───────────────────────────────────────────────────────────────────────

run:  ## Run the bot locally (expects .env).
	$(PYTHON) main.py

# ── Tests ─────────────────────────────────────────────────────────────────────

test:  ## Run the full test suite.
	$(PYTEST) tests/ -v --tb=short

test-fast:  ## Run tests, stop on first failure.
	$(PYTEST) tests/ -x --tb=short

# ── Lint / static checks ──────────────────────────────────────────────────────

lint: check-ascii  ## Run repo lint checks.

check-ascii:  ## Fail if em/en dashes or Unicode minus sneak into source files.
	@! grep -RPn --include='*.py' --include='*.sh' --include='*.md' \
		'[\x{2013}\x{2014}\x{2212}]' \
		. 2>/dev/null \
		|| (echo "Found em/en dashes or Unicode minus -- use ASCII '-' only." && exit 1)

# ── Docs ──────────────────────────────────────────────────────────────────────

docs: docs-build  ## Alias for docs-build.

docs-serve:  ## Serve docs locally with live reload.
	mkdocs serve

docs-build:  ## Build static docs site to ./site.
	mkdocs build

# ── Docker / Compose ──────────────────────────────────────────────────────────

up:  ## Start the full stack (postgres + redis + bot) via docker-compose.
	docker compose up -d

down:  ## Stop the docker-compose stack.
	docker compose down

logs:  ## Tail bot container logs.
	docker compose logs -f bot

build:  ## Build the bot Docker image.
	docker compose build

# ── One-off scripts ───────────────────────────────────────────────────────────

changelog:  ## Auto-update CHANGELOG.md from recent git history.
	$(PYTHON) scripts/update_changelog.py

migrate-config-v2:  ## Dry-run the v2 token-config migration. Append APPLY=1 to apply.
	$(PYTHON) scripts/migrate_config_v2.py $(if $(APPLY),--apply,)

reset-chains:  ## Reset all PoW chains to block 0 (does not touch player balances).
	$(PYTHON) scripts/reset_chains.py

# ── Housekeeping ──────────────────────────────────────────────────────────────

clean:  ## Remove Python caches and build artifacts.
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	rm -rf site/ build/ dist/ .eggs/ *.egg-info
