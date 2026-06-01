# Changelog

## [recycler] -- 2026-06-01

### Changes
- **Recycler adopts the server-tools build.** The Recycler repo is repurposed
  from the old `,clanker` containment bot into the free server-management bot:
  backups, templates, chatlogs, sync, import/export and settings. The previous
  framework-only Clanktank source (single `cogs/clanktank.py`, the slim clanker
  schema and `bot_manifest.py`) is replaced by the self-contained server-tools
  app (its own `clanklib/`, `api/v2`, slim server-only `database/` and cogs).
  Identity is rebranded to **Recycler** throughout (`sojourns.json` slug/name,
  app name, webhook + backup labels), so the bot deploys via Sojourns under its
  own `recycler` slug just like before. The `.clank` containment + moderation
  suite now lives in the separate **Clanksimus Prime** bot.

## [server-tools build] -- 2026-06-01

### Changes
- **Split into the server-tools bot ("Recycler").** This build keeps backups,
  templates, chatlog, sync, import/export and settings. The `.clank` containment
  system, the `mod` command set, the `modlog` audit suite and the `.init` wizard
  -- along with their migrations, the centralized logger and the per-guild clank
  settings -- have been removed; they live in a separate bot (Clanktank). Guild
  settings are trimmed to prefix + log channel; the invite no longer requests
  moderation/audit permissions.

## [main] -- 2026-06-01

### New Features
- **Per-guild settings API**: A key-protected REST surface (`/api/v2/guilds/{id}/settings`, with a `/schema` endpoint) to read and update each server's config (prefix, log + containment channels) from outside Discord, sharing one validated schema with the `.set` command.
- **Live settings**: Configuration changes made in the Sojourns control panel
  now take effect immediately, without a redeploy. Cogs read runtime config
  (prefix, backup cap, API key, client id) from the live settings layer rather
  than a boot-time environment snapshot, so the Sojourns control link's
  per-heartbeat sync actually reaches the commands.
- **Server backups**: Create full, restorable guild snapshots (settings, roles,
  categories, channels, permission overwrites and optional recent messages),
  manually or on a recurring interval with rolling retention.
- **Community templates**: Publish and apply structure-only server blueprints;
  free for everyone, browsable and searchable.
- **Chatlog archives**: Save a channel's recent messages and replay them into
  any channel via webhooks.
- **Sync**: Mirror messages between channels and propagate bans/unbans between
  guilds.
- **Import / Export**: Download a backup as portable JSON and import one back.
- **Settings**: Per-guild configuration (prefix, log channel, containment
  channels) shown and edited in a Components V2 panel.
- **`.clank` containment**: Ported the full Discoin clanktank subset (scam/bot
  containment, evidence, account-linking, escape room) under the `.clank`
  command group (alias `.clanker`).
- **REST API**: Embedded FastAPI app with a public `/health` endpoint and
  key-protected `/api/v2` reads for backups and templates.

### Documentation
- Thick, end-to-end deployment guide (`docs/deployment.md`) covering all four
  pathways - local/bare-metal, Docker, Railway and Sojourns - with
  prerequisites, post-deploy verification, upgrade/rollback and troubleshooting.
- Installation quick-start, full configuration reference and a complete command
  reference.

### Reliability
- **`.init` now actually applies permissions**: the wizard makes the Clanker
  role a real jail role -- it denies the role View Channel on every existing
  channel (paced) and grants it only in the tank, so a clanked user (stripped to
  @everyone + Clanker) can no longer see the rest of the server. Revert undoes
  the lockdown along with everything else it created.
- **Bulk ban + ban-sync backfill** on the paced runner: `massban <ids> [reason]`
  (hierarchy-checked, deduped, capped, with a live progress message) and
  `sync backfill <link_id>` to apply a source guild's existing bans to the
  target -- both paced so they can't trigger a rate-limit ban.
- **Paced mass actions**: bursting hundreds of clanks/bans/timeouts at once
  earns a multi-hour Cloudflare 429 (we hit a 2h ban cleaving 500 accounts). A
  new `BulkRunner` (`clanklib/ratelimit.py`) serializes and paces big batches,
  backs off on 429, and aborts the run after a few consecutive rate limits (or a
  long global retry-after) instead of escalating into a longer ban. Wired into
  cluster cleave (with a live progress message), clutch mass-clank, and clad
  bulk-timeout.

### Guided setup
- **`.init`**: a one-command guided setup. Creates a **Clanktank** category
  (the tank, its escape-room thread, and a staff-only clank-logs channel) with
  the Clanker role locked out of every other channel, and a mod/admin-only
  **Mod Logs** category with one auto-routed channel per log category
  (security, moderation, member, message, ...). Pick which categories to
  provision, Confirm before anything is created, then **Keep** or **Revert** --
  Revert deletes exactly what the run created (never anything pre-existing) and
  clears the settings it wrote. A mid-run failure auto-rolls back. Does not
  touch the scam-report/hunter channel. Uses Manage Roles + Manage Channels; no
  Administrator.

### Moderation commands
- **Mod command set** (`cogs/mod.py`), Components V2 native: `ban`, `unban`,
  `softban`, `kick`, `timeout`/`mute`, `untimeout`/`unmute`, `warn`,
  `warnings`, `delwarn`, `purge`, `slowmode`, `lock`, `unlock`.
- **Locked down**: every command requires the matching guild permission AND the
  matching bot permission, and every member action passes a role-hierarchy
  guard (you cannot action the server owner, the bot, yourself, anyone at or
  above your top role, or anyone above the bot). Durations accept `10m`, `1h`,
  `2d`; timeouts clamp to Discord's 28-day max; purge caps at 200.
- Warnings persist (`mod_warnings`), can be listed and individually removed, and
  every action is recorded through the central mod log.

### Moderation logging
- **Comprehensive mod log**: A new centralized logger (`bot.modlog`) records
  every tracked event under one standardized schema (short event id, UTC
  timestamp, category, severity, actor, target, channel, summary, metadata)
  and renders it as a Components V2 panel. Categories: security, moderation,
  member, message, role, channel, command, configuration, AI, infrastructure,
  clanktank, analytics.
- **Event coverage**: member join/leave, bans/unbans, timeouts, role and
  nickname changes, role create/delete/update (with permission deltas),
  channel create/delete, and message delete/edit/bulk-delete are all logged,
  with the acting moderator attributed from the audit log where available.
- **Configuration changes are now logged**: every `.set` change (and the
  `.modlog` routing changes) records a configuration event, so channel/role
  config edits are auditable.
- **Operator controls**: `.modlog` shows the current routing and 24h stats;
  `.modlog channel`, `.modlog route <category> #ch`, `.modlog mute/unmute
  <category>`, `.modlog timeline [@user]`, `.modlog stats [hours]`,
  `.modlog prune <days>` and `.modlog test`. Muted categories are still
  recorded for the timeline.
- Per-category log routing and a default mod log channel, configurable in both
  Discord and the Sojourns web UI.
- **Tamper-evident audit chain**: every event stores the hash of the previous
  event plus its own; `.modlog verify` walks the chain and reports the first
  break (an altered or deleted row).
- **Realtime alerts**: ALERT/CRITICAL events (and everything during an incident)
  are mirrored to a configurable alert channel with an optional role ping
  (`.modlog alert channel/role`).
- **Anomaly detection**: join floods (raids), mass-ban bursts and message-purge
  bursts trip a CRITICAL security event automatically.
- **Incident mode** (`.modlog incident on/off`): unmutes every category and
  mirrors all events to the alert channel for the duration of a situation.
- **Invite attribution**: member-join events record the invite code and who
  invited them (best-effort, needs Manage Server).

### Changes
- Components V2 is the default UI across every command.
- Templated for Sojourns via `sojourns.json` (validated in CI).
- **Role-based clanker hunters**: The scam-hunter system now keys off a
  configurable hunter role instead of a per-user whitelist. Anyone wearing the
  role can report in the hunter channel and is immune to automatic clanking;
  the old `.clank hunter add/remove` user commands are replaced by
  `.clank hunter role @role`.
- **Full config surface, Discord and web**: Every containment/moderation option
  is now editable both in Discord (`.set ...`) and in the Sojourns web UI, and
  the two surfaces write the same per-guild keys. New/exposed options: clanker
  role, clanker category, clanktank channel, clanker log channel, escape-room
  thread, reflection period, hunter role, hunter channel, mod log channel.
- **Reflection period default is now 5 minutes** (was 8), and is configurable
  per server (`.set reflection <minutes>` or the web UI).

### Fixes
- **Web-UI settings now actually apply**: settings pushed from the Sojourns
  control plane used manifest env-style keys (`CLANK_ESCAPE_THREAD_ID`) while
  the bot read canonical lowercase keys (`clank_escape_thread`); the two
  namespaces never met, so values set in the web UI silently no-op'd. The DB
  layer now normalises control-plane keys onto the canonical keys.
