# Command reference

Default prefix is `.` (configurable). Most management commands require a
server permission; the bot also needs the matching permission itself.

## Backups (`.backup`, `.bk`)

| Command | Permission | What it does |
|---|---|---|
| `.backup create [chatlog[:N]]` | Administrator | Snapshot the server. Add `chatlog` or `chatlog:100` to also archive recent messages. |
| `.backup load <id> [messages]` | Administrator | **Destructive.** Rebuild the server from a backup; add `messages` to also replay archived messages. |
| `.backup list` | -- | Your backups. |
| `.backup info <id>` | -- | Details for one backup. |
| `.backup delete <id>` | -- | Delete one of your backups. |
| `.backup interval <hours> [keep]` | Administrator | Automatic backups every N hours, keeping the newest `keep` (default 7). `.backup interval off` stops. |

## Templates (`.template`, `.tpl`)

| Command | Permission | What it does |
|---|---|---|
| `.template create <name> \| <description>` | Administrator | Publish a structure-only template from this server. |
| `.template load <id>` | Administrator | **Destructive.** Apply a template to this server. |
| `.template browse [query]` | -- | Browse / search community templates. |
| `.template info <id>` | -- | Details for one template. |
| `.template delete <id>` | -- | Delete one of your templates. |

## Chatlog (`.chatlog`, `.cl`)

| Command | Permission | What it does |
|---|---|---|
| `.chatlog create [#channel] [limit]` | Manage Messages | Archive the last `limit` messages (default: here, 100). |
| `.chatlog load <id> [#channel]` | Manage Webhooks | Replay an archive into a channel via webhook. |
| `.chatlog list` | -- | Your chatlogs. |
| `.chatlog delete <id>` | -- | Delete one of your chatlogs. |

## Sync (`.sync`)

| Command | Permission | What it does |
|---|---|---|
| `.sync messages <#source> <#target>` | Manage Server | Mirror new messages from source to target via webhook. |
| `.sync bans <source_guild_id> <target_guild_id>` | Bot owner | Propagate bans/unbans between guilds the bot is in. |
| `.sync list` | -- | Sync links in this server. |
| `.sync remove <id>` | Manage Server | Remove a sync link. |

## Import / Export

| Command | Permission | What it does |
|---|---|---|
| `.export <backup_id>` | -- | Download a backup as a JSON file. |
| `.import` | Administrator | Import a backup from an attached JSON file. |

## Settings (`.settings`, `.set`)

| Command | Permission | What it does |
|---|---|---|
| `.settings` | Manage Server | Show this server's configuration. |
| `.set prefix <p>` | Manage Server | Set a per-guild prefix. |
| `.set log <#channel>` | Manage Server | Set the log channel (`none` to clear). |
| `.set containment <#channel>` | Manage Server | Set the containment channel. |
| `.set containmentlog <#channel>` | Manage Server | Set the containment log channel. |

## Containment (`.clank`, alias `.clanker`)

The full ported containment subset. Highlights:

| Command | What it does |
|---|---|
| `.clank add <@user> [reason]` | Contain an account. |
| `.clank remove <@user>` | Release an account. |
| `.clank list` | Active contained accounts. |
| `.clank info <@user>` | Record, score and evidence for an account. |
| `.clank scan` | Score active accounts / a guarded role band. |
| `.clank chart` | Containment analytics chart. |
| `.clank help` | Full containment help. |

## REST API

With `CLANK_API_KEY` set, send it as the `X-API-Key` header.

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /health` | public | Bot status, guild count, readiness. |
| `GET /api/v2/backups?owner_id=<id>` | key | A user's backups. |
| `GET /api/v2/backups/{id}` | key | One backup (with its data). |
| `GET /api/v2/templates?q=<query>` | key | Browse templates. |
| `GET /api/v2/templates/{id}` | key | One template. |
| `GET /api/v2/guilds/settings/schema` | key | The editable per-guild settings schema. |
| `GET /api/v2/guilds/{guild_id}/settings` | key | A server's editable settings. |
| `PATCH /api/v2/guilds/{guild_id}/settings` | key | Update a server's settings (validated, partial). |
