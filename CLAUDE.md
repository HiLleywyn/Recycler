# Recycler -- Claude Code Guidelines

> This repo is the **server-tools** bot: backups, templates, chatlogs, sync,
> import/export and settings. The `.clank` containment system and the moderation
> + audit-logging suite live in a separate bot, **Clanksimus Prime**
> (`hilleywyn/clanksimus-prime`). If you are looking for `.clank`/`mod`/`modlog`,
> you are in the wrong repo.

Recycler is a free Discord server-management bot (backups, templates,
chatlogs, sync, import/export, settings), built on the shared **bot framework**
(`hilleywyn/framework`) and templated for the **Sojourns** platform via
`sojourns.json`.

## Default UI -- Components V2 (hard rule)

**Components V2 is the default UI.** Build every user-facing message with
`core.framework.components` (`Container().text(...).section(...).separator()...`
sent via `send_v2` / `edit_v2`), never with `discord.Embed`. The cogs in this
repo are the reference pattern -- match them. Reach for an embed only if a
feature genuinely requires one (there are currently none). This needs
`discord.py>=2.6`, which the Dockerfile and `requirements.txt` pin.

## Git & commits -- hard rules

- **Author AND committer are always `HiLleywyn <lleywyn@proton.me>`.** Never
  commit as `Claude` / `noreply@anthropic.com`. Use
  `git -c user.name="HiLleywyn" -c user.email="lleywyn@proton.me" commit`.
- **Never** put `https://claude.ai/code/session_*` links in any committed
  artifact (commit messages, PR bodies, comments).
- **Never** put a model identifier (`claude-*`, "Claude", versions) in committed
  code, docs, or commit messages.
- Develop on a feature branch; open a PR to `main` ready for review.
- Update `CHANGELOG.md` in the same commit as any user-visible change.

## Architecture

- `main.py` -- three lines: `run_manifest()` boots from `sojourns.json`
  (its `features` is the cog list) with a fallback cog list.
- `sojourns.json` -- the manifest. Source of truth for cogs + settings; the
  Sojourns control plane reads the same file. Validate with
  `python -m core.framework.manifest sojourns.json`.
- `cogs/` -- the Components V2 server tools: `backups.py`, `templates.py`,
  `chatlog.py`, `sync.py`, `importexport.py`, plus `settings.py` and `meta.py`.
- `clanklib/serializer.py` -- guild <-> JSON (the engine behind backups +
  templates). Pure serialize; explicit `RestoreOptions` for the destructive
  restore path.
- `database/` -- a **slim** data plane (no economy): `database.py`
  (`PgDatabase`: pool, file migration runner, query helpers, the guild-settings
  cache, repo accessors), `base.py` (`PgBaseRepo`), feature repos, and
  `migrations/*.sql` (the `0001-0005` server tables). The framework imports
  `database.Database` lazily.
- `api/v2/main.py` -- `create_app(bot)` FastAPI app the framework auto-mounts
  on `API_PORT`; `/health` is public, `/api/v2/*` needs `X-API-Key`.

## Conventions

- Plain ASCII in source -- no em/en dashes or Unicode minus signs.
- Use the framework, don't reimplement it: colors and `fmt_*` come from
  `core.framework.ui`; UI from `core.framework.components`; cog bases from
  `core.framework.cogs` (`GuildCog` for guild-only features).
- `log = logging.getLogger(__name__)` -- never `print()`.
- Management commands require a guild permission (`manage_guild` /
  `administrator` / `manage_webhooks`) and the matching `bot_has_guild_permissions`.
- No premium gating anywhere. Caps (e.g. `BACKUP_MAX_PER_USER`) are
  abuse-prevention only and configurable.

## Serializer is the heart of this bot

`clanklib/serializer.py` turns a guild into JSON and back; backups, templates
and import/export all build on it. Keep the serialize path pure and gate every
destructive restore action behind an explicit `RestoreOptions` flag.
