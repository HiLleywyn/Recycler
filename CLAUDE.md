# CLAUDE.md - Recycler

Recycler is the standalone **`,clanker` containment bot** (Clanktank). It runs on
the shared `bot-framework` and ships **nothing from the economy** - its own slim,
clanker-only database and a single cog.

## Git & commits - hard rules

- **Author AND committer are always `HiLleywyn <lleywyn@proton.me>`.** Never
  commit as `Claude` / `noreply@anthropic.com`. Use
  `git -c user.name="HiLleywyn" -c user.email="lleywyn@proton.me" commit`.
- **Never** put `https://claude.ai/code/session_*` links in any committed
  artifact.
- **Never** put a model identifier in committed code/docs.
- Develop on a feature branch; open a PR to `main` ready for review.

## What this bot is (and isn't)

- `cogs/clanktank.py` - the only cog; every `,clanker` feature.
- `sojourns.json` - the manifest: identity, `features: ["cogs.clanktank"]`,
  credentials, `provision.database = postgres`, and the clanker settings schema.
- `database/` - Recycler's **own** slim data layer: `schema.sql` (framework
  runtime tables only) + `migrations/0285-0295` (the clanker tables, verbatim
  from the framework) + a `Database` implementing exactly the surface the
  framework + clanktank call. It shadows the framework's bundled economy
  `database` package, so Recycler carries no economy schema.
- `main.py` - `run_manifest(fallback_cogs=COGS, ...)`.
- `requirements.txt` - pins `bot-framework` to a **specific commit SHA** (not
  `@main`). Bumping the SHA is what adopts a new framework version AND busts the
  Docker pip-cache so the redeploy actually pulls it.

## Hard rules

- **No economy.** Recycler ships no `services` package and no economy tables. If
  you add anything that imports `services.*` or an economy table, it's wrong -
  it belongs in Disco. The framework gates economy boot on `has_economy()`, which
  is False here.
- **Slim DB stays slim.** Only add a table to `database/schema.sql` if the
  framework runtime or clanktank actually queries it. Clanker schema changes go
  in a new verbatim migration mirroring the framework's.
- Plain ASCII in source - no em/en dashes.

## AI (via Sojourns)

The clanker AI routes through Sojourns when `SOJOURNS_AI_BASE_URL` is set. With
`SOJOURNS_PROVISION_SECRET` matching the platform, the key is derived
automatically (no manual provisioning). See `.env.example`.
