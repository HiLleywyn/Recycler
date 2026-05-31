# Copilot Instructions for Discoin

## Project Overview

Discoin is a Discord economy bot with a full-stack architecture:
- **Backend**: Python 3.12  -  discord.py bot + FastAPI REST API
- **Frontend**: Next.js 16 + TypeScript + Tailwind CSS
- **Database**: PostgreSQL (asyncpg) with Redis caching
- **Deployment**: Docker multi-stage build, Railway.app

## Code Review Guidelines

When reviewing pull requests, Copilot should check for:

### Economic Correctness (Critical)
- Total supply invariants: mint, burn, and transfer operations must net correctly
- No paths where user balances can go negative or unbounded
- Fee calculations are precise  -  no rounding errors that accumulate over time
- AMM pool math (constant product formula) is preserved after swaps
- Staking reward calculations do not create inflation beyond configured rates
- Lending collateral ratios are enforced on all code paths
- NFT minting, marketplace sales, and transfers use atomic transactions with rollback
- NFT marketplace prices are in the network's native coin, not USD
- Prediction market payouts correctly deduct the 5% house cut before distribution

### Python Backend
- All database operations use `asyncpg` connection pools  -  never raw connections
- Pydantic v2 models for request/response validation in FastAPI routes
- Rate limiting middleware is applied to public API endpoints
- JWT auth tokens are validated with proper expiry checks
- No secrets or credentials hardcoded  -  all config via environment variables
- Discord command handlers use proper error handling and user feedback
- Async/await is used correctly  -  no blocking calls in async contexts

### Frontend (TypeScript/React)
- Components use TypeScript with strict typing  -  no `any` types
- API calls go through the axios client with proper error handling
- Zustand stores follow the existing patterns in `frontend/stores/`
- Next.js App Router conventions are followed

### General
- No `.env` files, credentials, or secrets committed
- Database schema changes include migration considerations
- New API endpoints have corresponding test coverage
- Docker build is not broken by changes

## Running Tests

### Python Tests
```bash
uv pip install --system -r requirements.txt -r requirements-test.txt
python -m pytest tests/ -v --tb=short
```

Tests use `pytest` with `pytest-asyncio` (async mode: auto). Fixtures are in `tests/conftest.py`.

Key test areas:
- `test_swap_economics.py`  -  AMM swap math and fee calculations
- `test_services_*.py`  -  Core business logic (trade, transfer, stake, savings)
- `test_api_auth.py`, `test_jwt.py`, `test_totp.py`  -  Authentication flows
- `test_rate_limit.py`  -  API rate limiting middleware
- `test_amount_parser.py`  -  User input parsing
- `test_net_worth.py`  -  Portfolio valuation

### Frontend Lint
```bash
cd frontend && npm install && npm run lint
```

## PR Automation Expectations

When Copilot is assigned to review a PR:

1. **Run the test suite**  -  Ensure `python -m pytest tests/ -v --tb=short` passes
2. **Check economic invariants**  -  Use the rules from the `Invario` agent (`.github/agents/Invario.agent.md`) for any changes touching `cogs/`, `services/`, `core/framework/chain_engine.py`, or `database/`
3. **Flag security concerns**  -  SQL injection via raw queries, missing auth checks, exposed secrets
4. **Validate schema changes**  -  Any modifications to `database/schema.sql` should be backward-compatible or have a migration plan
5. **Verify Docker compatibility**  -  Changes to dependencies must be reflected in `requirements.txt` and not break the Dockerfile

## File Structure Reference

```
main.py                    # Bot entry point
core/config.py                  # Centralized configuration
core/framework/                 # Bot infrastructure (chain engine, redis bus, AI client)
cogs/                      # Discord command modules (bank, crypto, trade, stake, etc.)
database/                  # PostgreSQL layer (schema.sql, mixin-based architecture)
  schema.sql               # Full database schema
api/v2/                    # FastAPI REST API (auth, routers, services, middleware)
services/                  # Shared business logic (swap, trade, transfer)
frontend/                  # Next.js dashboard
tests/                     # pytest test suite
```

## Coding Conventions

- Python: snake_case for functions/variables, PascalCase for classes
- Use `Decimal` for all financial calculations  -  never `float`
- Database queries go through the mixin classes in `database/`, not inline SQL
- Discord cog commands follow the pattern in existing `cogs/*.py` files
- API routes are versioned under `api/v2/routers/`
- Configuration values come from `core/config.py` which reads from environment variables

## Branch Strategy

- `master`  -  production branch
- `claude/**`  -  AI-assisted development branches
- PRs target `master` and require CI to pass before merge
