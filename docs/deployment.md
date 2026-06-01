# Deployment Guide

This is the complete, end-to-end guide to deploying **Recycler**. It
covers all four pathways:

1. [Prerequisites (every pathway)](#1-prerequisites)
2. [Pathway A - Local / bare metal](#pathway-a--local--bare-metal)
3. [Pathway B - Docker](#pathway-b--docker)
4. [Pathway C - Railway](#pathway-c--railway)
5. [Pathway D - Sojourns (managed)](#pathway-d--sojourns-managed)
6. [Post-deploy verification](#6-post-deploy-verification)
7. [Upgrades, backups of the bot, and rollback](#7-upgrades-and-rollback)
8. [Troubleshooting](#8-troubleshooting)

Recycler runs as **one process**: a Discord gateway client plus an
embedded HTTP server (REST API + `/health`). It needs exactly two external
things to run: a **PostgreSQL database** and a **Discord bot token**. Redis is
optional. It is built on the private `hilleywyn/framework` package, so every
install pulls that package from git.

---

## 1. Prerequisites

### 1.1 The Discord application + bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**. Name it, then open the **Bot** tab.
2. Click **Reset Token**, copy the value, and keep it somewhere safe. This is
   your `DISCORD_TOKEN`. **Treat it like a password** - anyone with it controls
   the bot.
3. Scroll to **Privileged Gateway Intents** and enable both that Recycler
   relies on:
   - **Server Members Intent** - needed for ban sync, member-scoped permission
     overwrites in backups/restores, and containment tracking.
   - **Message Content Intent** - needed for message sync, chatlog archiving,
     and `.clank` enforcement.
   Leave **Presence Intent** off (unused).
4. (Optional) On the **OAuth2** tab, copy the **Client ID** into
   `DISCORD_CLIENT_ID`. It is only used to build a clean invite URL before the
   bot has logged in; the running bot derives it automatically otherwise.

### 1.2 Inviting the bot

Recycler creates and deletes roles, channels and webhooks (that is the whole
job), so it needs the **Administrator** permission. Build an invite URL:

```
https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&permissions=8&scope=bot%20applications.commands
```

Once the bot is running you can also just type `.invite` (or `.about`) and it
prints this URL for you. Administrator (`permissions=8`) is required for
backup/template restores; without it, restores will partially fail with
permission errors.

### 1.3 A PostgreSQL database

Any PostgreSQL 13+ works. You need a DSN in this shape:

```
postgresql://USER:PASSWORD@HOST:PORT/DATABASE
```

That becomes `DATABASE_URL`. **Migrations run automatically on every boot** -
you never apply SQL by hand. The data plane enables TLS automatically for
remote hosts (and trusts Railway's self-signed certs); set `DB_SSL_VERIFY=1` to
force full certificate verification.

### 1.4 Access to the framework package

The runtime lives in the **private** `hilleywyn/framework` repo. Every install
path needs read access to it:

- **Local**: none -- the framework repo is public.
- **Docker / Railway**: none -- the image pulls the public framework automatically.

The `FRAMEWORK_REF` setting (default `main`) picks which git ref to install.

---

## Pathway A - Local / bare metal

Best for development and self-hosting on a VM you own.

### A.1 Clone and create a virtualenv

```bash
git clone https://github.com/hilleywyn/recycler
cd recycler
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

### A.2 Install the framework, then the app deps

```bash
# HTTPS with a token:
pip install "bot-framework @ git+https://<TOKEN>@github.com/hilleywyn/framework.git@main"
# ...or over SSH if your key has access:
# pip install "bot-framework @ git+ssh://git@github.com/hilleywyn/framework.git@main"

pip install -r requirements.txt
```

### A.3 Provide a database

Quickest local Postgres:

```bash
docker run -d --name recycler-db \
  -e POSTGRES_PASSWORD=clank -e POSTGRES_DB=recycler \
  -p 5432:5432 postgres:16
# DATABASE_URL=postgresql://postgres:clank@localhost:5432/recycler
```

### A.4 Configure

```bash
cp .env.example .env
```

Edit `.env` and set, at minimum, `DISCORD_TOKEN` and `DATABASE_URL`. The full
list of variables is in [configuration.md](configuration.md); the most common
ones are summarized in [section 5 below](#configuration-quick-reference).

### A.5 Run

```bash
python main.py
```

You should see the framework banner, `Database connected`, migrations being
applied on first boot, and finally the gateway connecting. The HTTP server
listens on `API_PORT` (default `8080`); `curl http://localhost:8080/health`
should return JSON.

Run it under a process manager for persistence:

```ini
# /etc/systemd/system/recycler.service
[Unit]
Description=Recycler
After=network-online.target

[Service]
WorkingDirectory=/opt/recycler
EnvironmentFile=/opt/recycler/.env
ExecStart=/opt/recycler/.venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now recycler
journalctl -u recycler -f       # follow logs
```

---

## Pathway B - Docker

Best for a reproducible single-container deploy anywhere Docker runs.

### B.1 Build

The image installs the framework (public) from git at build time and refreshes
it automatically, so no build args are required:

```bash
docker build -t recycler:latest .
```

What the build does, in order: installs system deps -> installs the framework
from git -> installs `requirements.txt` -> copies the source -> **runs the test
suite and fails the build if it is red**. A green build is a smoke test that the
code imports and the manifest is valid.

### B.2 Run

Put your settings in `.env` (same file as local), then:

```bash
docker run -d --name recycler \
  --env-file .env \
  -p 8080:8080 \
  --restart unless-stopped \
  recycler:latest
```

The container exposes `8080` and has a built-in `HEALTHCHECK` hitting
`/health`. `docker ps` will show `healthy` once it is up.

### B.3 With docker compose (bot + database together)

```yaml
# docker-compose.yml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: clank
      POSTGRES_DB: recycler
    volumes:
      - recycler-db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      retries: 10

  bot:
    build:
      context: .
      args:
        FRAMEWORK_REF: main
    environment:
      DISCORD_TOKEN: ${DISCORD_TOKEN}
      DATABASE_URL: postgresql://postgres:clank@db:5432/recycler
      PREFIX: "."
      API_PORT: "8080"
    ports:
      - "8080:8080"
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

volumes:
  recycler-db:
```

```bash
DISCORD_TOKEN=<token> docker compose up -d --build
docker compose logs -f bot
```

---

## Pathway C - Railway

Best for a hosted deploy with managed Postgres and zero server maintenance.
`railway.toml` in the repo already selects the Dockerfile builder and the
`/health` healthcheck.

### C.1 Create the project

1. In [Railway](https://railway.app), **New Project -> Deploy from GitHub repo**
   and pick `recycler`. Railway reads `railway.toml` and builds with the
   `Dockerfile`.

### C.2 Add PostgreSQL

2. In the project, **New -> Database -> Add PostgreSQL**. Railway provisions it
   and exposes a `DATABASE_URL`. Reference it from the bot service - either
   Railway injects it automatically, or set the bot's `DATABASE_URL` to
   `${{Postgres.DATABASE_URL}}`. TLS to Railway Postgres is handled
   automatically by the data plane.

### C.3 Set service Variables (runtime)

On the bot service's **Variables** tab:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | your bot token (**required**) |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` if not auto-injected |
| `PREFIX` | `.` (or your choice) |
| `API_PORT` | `8080` |
| `CLANK_API_KEY` | optional - set to enable `/api/v2` |
| `BACKUP_MAX_PER_USER` | optional - default `50` |
| containment vars | `CLANKER_ROLE_ID`, `CLANKTANK_CHANNEL_ID`, ... if you use `.clank` |

### C.4 Set Build Variables (the private framework dep)

The image must install the framework from its private repo, so add **build**
variables (Railway passes matching variables as Docker build args):

| Build variable | Value |
|---|---|
| `FRAMEWORK_REF` | `main` (or a tag/branch) |

### C.5 Deploy

3. Trigger the deploy. Watch the build logs - the test stage runs here too.
   First boot applies migrations. The healthcheck path is `/health`; Railway
   marks the service healthy once it responds.
4. (Optional) Under the service **Settings -> Networking**, generate a public
   domain if you want to reach the REST API from outside.

---

## Pathway D - Sojourns (managed)

Best when you want the Sojourns control plane to deploy, configure and manage
the bot. Recycler ships a `sojourns.json` manifest, which is the deployment
contract Sojourns reads.

### D.1 What the manifest declares

`sojourns.json` tells Sojourns:

- **identity** - slug, name, version, repo;
- **`features`** - the exact cog list to load (same list `main.py` boots from);
- **`credentials`** - the secrets to collect (`DISCORD_TOKEN`, marked secret);
- **`provision`** - that it needs a Postgres database;
- **`settings`** - grouped fields (prefix, backup cap, API key, containment
  channels/roles) that Sojourns renders as a dynamic configuration UI and
  pushes to the running bot via `bot.settings`.

Validate it any time:

```bash
python -m core.framework.manifest sojourns.json
```

### D.2 Register and deploy

1. In Sojourns, add a **managed bot** pointing at the `recycler` repo.
   Sojourns parses `sojourns.json` and shows the identity, the feature list, the
   credentials to collect and the settings UI.
2. When prompted, paste `DISCORD_TOKEN`. Sojourns stores it in its vault (the
   field is declared `secret`), provisions Postgres
   (`provision.database = "postgres"`) and injects `DATABASE_URL`.
3. Set any settings in the generated UI - prefix, `BACKUP_MAX_PER_USER`,
   `CLANK_API_KEY`, and the containment channel/role fields. Each maps to a
   control defined in the manifest and is delivered to the bot without a code
   change.
4. Deploy. Sojourns runs the same Dockerfile/runtime; first boot applies
   migrations.

Because `main.py` boots from the **same** `sojourns.json`, there is no drift
between a standalone run and a Sojourns-managed deployment - the manifest is the
single source of truth for both.

---

## 6. Post-deploy verification

Run these regardless of pathway:

1. **Health endpoint** - `curl http://<host>:8080/health` returns
   `{"status":"ok", ...}` with a non-null `bot` name once the gateway is up.
2. **The bot is online** in your server's member list.
3. **A command responds** - type `.help`. You should get a Components V2 panel.
   If you get nothing, check the prefix (`.set prefix` / `PREFIX`) and that
   Message Content Intent is enabled.
4. **Backups work end-to-end** - in a throwaway server: `.backup create`, then
   `.backup list`, then (carefully, it is destructive) `.backup load <id>`.
5. **REST API (if enabled)** - with `CLANK_API_KEY` set:
   ```bash
   curl -H "X-API-Key: <key>" "http://<host>:8080/api/v2/templates"
   ```

---

## 7. Upgrades and rollback

- **Code upgrade**: redeploy the branch/tag. On Railway/Sojourns this is a
  push or a redeploy click; with Docker, rebuild and `docker compose up -d`.
  New migrations apply automatically on boot; existing data is preserved.
- **Framework upgrade**: bump `FRAMEWORK_REF` to the desired ref and rebuild.
- **Rollback**: deploy the previous image/ref. Migrations are forward-only, so
  a rollback that crosses a schema change may need the older schema - prefer
  rolling forward with a fix. Keep a database backup before large upgrades
  (`pg_dump`).
- **The bot's own data** lives entirely in Postgres (backups, templates,
  chatlogs, sync links, settings). Back up the database, not the container.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Build fails at `pip install bot-framework` | Network issue or a bad `FRAMEWORK_REF` | Retry; verify the ref exists on `hilleywyn/framework`. |
| Build fails at the test stage | A real import/manifest error | Read the failing test output; fix before deploy (this is the gate working). |
| `DISCORD_TOKEN is required` at boot | Token unset/empty | Set `DISCORD_TOKEN`. |
| Boot hangs / DB errors | `DATABASE_URL` wrong or DB unreachable | Verify the DSN, host, and that the DB accepts the connection; for self-signed TLS leave `DB_SSL_VERIFY=0`. |
| Bot online but ignores commands | Wrong prefix, or Message Content Intent off | Check `PREFIX` / `.set prefix`; enable the intent in the portal. |
| `.backup load` reports permission errors | Bot lacks Administrator or its top role is too low | Re-invite with Administrator; move the bot's role high. |
| Restores skip some roles | Managed/integration roles, or roles above the bot | Expected - Discord forbids managing those. |
| `/api/v2` returns 403 | `CLANK_API_KEY` not set | Set it to enable the API. |
| `/api/v2` returns 401 | Wrong/missing `X-API-Key` header | Send the header matching `CLANK_API_KEY`. |
| 429 on startup, restart loop | Discord login rate limit | The runtime retries with backoff; if it persists, wait and avoid rapid redeploys. |

For variable-by-variable detail see [configuration.md](configuration.md); for
the full command list see [commands.md](commands.md).

---

## Configuration quick reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | - | Bot token. |
| `DATABASE_URL` | yes | - | Postgres DSN; migrations run on boot. |
| `PREFIX` | no | `.` | Command prefix. |
| `API_PORT` | no | `8080` | REST API + `/health` port. |
| `CLANK_API_KEY` | no | - | Enables `/api/v2`; sent as `X-API-Key`. |
| `BACKUP_MAX_PER_USER` | no | `50` | Per-user backup cap (anti-abuse). |
| `DISCORD_CLIENT_ID` | no | - | For the invite URL before login. |
| `CLANKER_ROLE_ID` | no | - | `.clank` containment role. |
| `CLANKTANK_CHANNEL_ID` | no | - | `.clank` tank channel. |
| `CLANKTANK_LOG_CHANNEL_ID` | no | - | `.clank` mod-log channel. |
| `CLANK_ESCAPE_THREAD_ID` | no | - | `.clank` escape-room thread. |
| `CLANK_ESCAPE_WAIT_MINUTES` | no | `8` | `.clank` reflection wait. |
| `REDIS_URL` | no | - | Enables framework Redis features. |
| `DB_SSL_VERIFY` | no | `0` | `1` forces full DB TLS verification. |
| `FRAMEWORK_REF` (build) | no | `main` | Framework git ref to install. |
