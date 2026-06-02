# Installing Recycler

Recycler runs as a single process: a Discord bot plus an embedded REST
API / health endpoint. It needs **PostgreSQL** and a **Discord bot token**.
It is built on the public `hilleywyn/framework` package, so installs pull that
from git.

- [1. Create the Discord application](#1-create-the-discord-application)
- [2. Local development](#2-local-development)
- [3. Docker](#3-docker)
- [4. Railway](#4-railway)
- [5. Auren](#5-auren)

---

## 1. Create the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   -> **New Application** -> **Bot** -> **Reset Token** and copy it into
   `DISCORD_TOKEN`.
2. Under **Bot -> Privileged Gateway Intents**, enable:
   - **Server Members Intent** -- ban sync and member-scoped permission
     overwrites in backups/restores.
   - **Message Content Intent** -- message sync and chatlog archiving.
3. Invite the bot with the **Administrator** permission (it creates/deletes
   roles, channels and webhooks). The `.about` / `.invite` commands print a
   ready-made invite URL once it's running.

## 2. Local development

```bash
git clone https://github.com/hilleywyn/recycler
cd recycler
python -m venv .venv && source .venv/bin/activate

# Install the shared framework (public repo -- no token needed):
pip install "bot-framework @ git+https://github.com/hilleywyn/framework.git@main"
pip install -r requirements.txt

cp .env.example .env          # set DISCORD_TOKEN + DATABASE_URL at minimum
python main.py
```

Migrations run automatically on boot against `DATABASE_URL`. A local Postgres
is the quickest path:

```bash
docker run -d --name recycler-db -e POSTGRES_PASSWORD=clank \
  -e POSTGRES_DB=recycler -p 5432:5432 postgres:16
# DATABASE_URL=postgresql://postgres:clank@localhost:5432/recycler
```

Validate the manifest any time with:

```bash
python -m core.framework.manifest auren.json
```

## 3. Docker

The image installs the framework (public) from git at build time, so no token
or build secret is needed:

```bash
docker build \
  --build-arg FRAMEWORK_REF=main \
  -t recycler .

docker run --env-file .env -p 8080:8080 recycler
```

The build runs the test suite and fails if it's red. `/health` is exposed for
the container healthcheck.

## 4. Railway

1. **New Project -> Deploy from GitHub repo** and pick `recycler`.
   Railway uses the `Dockerfile` (configured in `railway.toml`).
2. Add a **PostgreSQL** plugin. Railway sets `DATABASE_URL` automatically; the
   data plane enables SSL for Railway hosts on its own.
3. Set service **Variables**:
   - `DISCORD_TOKEN` -- your bot token.
   - `PREFIX` -- defaults to `.`.
   - `CLANK_API_KEY` -- optional, enables `/api/v2`.
4. (Optional) set the **Build** variable `FRAMEWORK_REF` to pick the framework
   ref to install (branch/tag/commit; defaults to `main`).
5. Deploy. The healthcheck path is `/health`; first boot runs migrations.

## 5. Auren

Recycler ships a `auren.json` manifest, so the Auren control plane can
deploy and manage it like any other managed bot:

1. In Auren, add a managed bot pointing at this repo. Auren reads
   `auren.json` for the bot identity, the `features` (cogs) to load, the
   `credentials` it must collect (`DISCORD_TOKEN`) and the `settings` to render
   as a dynamic config UI.
2. Provide `DISCORD_TOKEN` when prompted; Auren provisions Postgres
   (`provision.database = "postgres"`) and injects `DATABASE_URL`.
3. Adjust settings (prefix, backup cap, API key) from the Auren settings UI
   -- each field in the manifest maps to a control and is pushed to the running
   bot via `bot.settings`.

The same manifest is what `main.py` boots from locally, so there is no drift
between a standalone run and a Auren-managed deployment.
