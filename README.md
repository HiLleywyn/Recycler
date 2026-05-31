# Recycler

**Recycler** is the `,clanker` containment bot — the Clanktank
scammer/bot-account detection, scoring and escape-room system, extracted out
of Discoin into its own bot. It runs **all `,clanker` features and nothing
else**, on the shared [`bot-framework`](https://github.com/HiLleywyn/Framework),
and is managed from [Sojourns](https://github.com/HiLleywyn/Sojourns).

## What's here

```
cogs/clanktank.py   ← every ,clanker feature (the only cog)
bot_manifest.py     ← APP_NAME + COGS = ["cogs.clanktank"]
main.py             ← 3 lines: hand the manifest to the framework
requirements.txt    ← bot-framework, built from the Framework repo
Dockerfile          ← installs the framework from git, runs the bot
```

That's the whole bot. The runtime, the data plane (PostgreSQL schema +
migrations, including the clanker tables), the AI bridge, prefix routing,
error tracking and graceful shutdown all come from the framework.

## How it's built

The Dockerfile **builds the framework from the Framework repo** — `pip install`
resolves `bot-framework @ git+https://github.com/HiLleywyn/Framework.git@<ref>`
from `requirements.txt`, which clones and builds it. Recycler adds only its
clanker source on top.

```bash
docker build -t recycler .
docker run --env-file .env recycler
```

Point `DATABASE_URL` / `REDIS_URL` at a managed PostgreSQL + Redis. The
framework applies `schema.sql` + migrations (incl. clanker tables)
automatically on first connect.

## AI is routed through Sojourns

The clanker AI feature (escape-room prompt reformulation + scan scoring) goes
through the framework's AI bridge. Set the Sojourns env vars and that AI is
**called from the Sojourns API** — the platform's OpenAI-compatible proxy
gateway / AI controller — instead of OpenRouter:

```
SOJOURNS_AI_BASE_URL=https://your-sojourns-host
SOJOURNS_AI_API_KEY=<tenant token Sojourns issued for Recycler>
SOJOURNS_BOT_ID=recycler
```

See `.env.example` for the full configuration.

## Managed from Sojourns

Recycler is one of the first two bots registered in the Sojourns management
platform (alongside Disco). Sojourns is its AI controller and its management
surface — start/stop, status, config and AI usage are all driven from there.
