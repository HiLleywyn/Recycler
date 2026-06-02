# Recycler

A free, modern Discord **server-management bot** -- backups, templates,
chatlogs, sync, import/export and settings -- rendered in Discord's
**Components V2** UI, with **no premium tiers and no paywalls**. Built on the
shared [`bot framework`](https://github.com/hilleywyn/framework) and templated
for the [Sojourns](https://github.com/hilleywyn/sojourns) platform.

> Inspired by what Xenon does for server management -- rebuilt on a modern,
> open stack with the premium gating removed.

Recycler is the **server-tools** half of the project. Moderation, audit logging
and the `.clank` account-containment system live in a separate bot,
**Clanksimus Prime** (`hilleywyn/clanksimus-prime`).

## Features

| Area | What it does | Commands |
|---|---|---|
| **Backups** | Full guild snapshots (settings, roles, channels, overwrites, optional messages); manual or on an interval | `.backup create` `.backup load` `.backup list` `.backup info` `.backup delete` `.backup interval` |
| **Templates** | Shareable, structure-only blueprints anyone can apply | `.template create` `.template load` `.template browse` `.template info` `.template delete` |
| **Chatlog** | Archive a channel's messages and replay them via webhook | `.chatlog create` `.chatlog load` `.chatlog list` `.chatlog delete` |
| **Sync** | Mirror messages between channels and propagate bans between guilds | `.sync messages` `.sync bans` `.sync list` `.sync remove` |
| **Import/Export** | Move backups in and out as portable JSON files | `.export <id>` `.import` (attach a file) |
| **Settings** | Per-guild configuration in a Components V2 panel | `.settings` `.set prefix` `.set log` |
| **REST API** | Read backups/templates over HTTP | `GET /api/v2/...` (see docs) |

## Quick start

```bash
git clone https://github.com/hilleywyn/recycler
cd recycler
cp .env.example .env          # fill in DISCORD_TOKEN + DATABASE_URL
# install the framework (public) + deps, then run:
pip install "bot-framework @ git+https://github.com/hilleywyn/framework.git@main"
pip install -r requirements.txt
python main.py
```

Or build the container (Railway-ready -- no build args needed):

```bash
docker build -t recycler .
docker run --env-file .env -p 8080:8080 recycler
```

## Documentation

- **[`docs/deployment.md`](docs/deployment.md)** -- the thick, end-to-end
  deployment guide: prerequisites and all four pathways (local/bare-metal,
  Docker, Railway, Sojourns), post-deploy verification, upgrades/rollback and
  troubleshooting. **Start here.**
- [`docs/configuration.md`](docs/configuration.md) -- every environment
  variable, grouped by feature, with defaults and effects.
- [`docs/commands.md`](docs/commands.md) -- the complete command reference.
- [`docs/install.md`](docs/install.md) -- a condensed install quick-start.

## How it's built

`main.py` boots from `sojourns.json` through the framework's shared runtime
(`run_manifest`). The manifest's `features` list is the set of cogs to load and
doubles as the deployment contract the Sojourns control plane reads. The data
plane is a slim, economy-free Postgres layer with a file-based migration runner.
The UI is Components V2 throughout (`core.framework.components`).

See [`CLAUDE.md`](CLAUDE.md) for contributor conventions.

## License

Free to self-host and modify.
