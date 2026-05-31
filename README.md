# Recycler

**Recycler** is the **clanker** bot: the standalone Clanktank system --
scammer / bot-account containment, the `,clanker` command set, evidence
clustering, cases, and the multi-station escape room. It is one focused
Discord bot, split out of the old all-in-one build so the economy
(Discoin) and the clanker (Recycler) run as separate services.

It does exactly one job. The economy, games, NFTs and markets are **not**
here -- those live in Discoin. Recycler runs only the `,clanker`
commands and the containment listeners behind them.

## No built-in AI

Recycler ships with **no model brain of its own**. The clanker has a few
AI-assisted touches (e.g. reformulating an escape-room reflection), and
every one of them is called out to the **Sojourns** platform over an
OpenAI-compatible HTTP backend. AI is **optional**: with it off, or the
Sojourns backend unreachable, the bot runs containment, cases and the
escape room in full -- only the AI-flavoured extras quietly stand down.

All model calls funnel through one seam --
`core/framework/ai/client.py` -- pointed at Sojourns with three vars:

| Variable | Default | Purpose |
|---|---|---|
| `AI_BACKEND_URL` | `https://openrouter.ai/api/v1` | `/v1` base of the Sojourns AI backend. OpenRouter default lets Recycler run standalone. |
| `AI_BACKEND_KEY` | falls back to `OPENROUTER_API_KEY` | bearer token; must match Sojourns' `AI_BACKEND_KEY`. |
| `AI_ENABLED` | on when a key is present | master switch; `AI_ENABLED=0` runs the clanker with zero AI calls. |

The matching backend is in the Sojourns repo
(`sojourns/ai_backend_api.py`, `POST /v1/chat/completions`).

## Architecture

```
Sojourns  ---- platform + AI/API integration both bots call ----
   ^  ^
   |  |  POST {AI_BACKEND_URL}/chat/completions
   |  +-------------------------------+
   |                                  |
Discoin (economy / games / NFTs)   Recycler (clanker / clanktank)
```

- **Discoin** -- the economy, games, NFTs and markets bot.
- **Recycler** -- the clanker / clanktank bot (this repo).
- **Sojourns** -- the platform both bots run against, and the API + AI
  integration both call into. Neither bot embeds a model; both work
  without AI and gain AI-powered features when Sojourns is reachable.

Recycler keeps the shared bot framework (`core/`, `database/`,
`constants/`) as its runtime substrate but loads a **single cog**,
`cogs/clanktank.py`. The cog registry in `core/framework/bot.py` is
trimmed to `cogs.clanktank`, so only the clanker command surface runs.
There is no economy REST API or dashboard here (those stay in Discoin),
so Recycler serves no HTTP and needs no healthcheck.

## Quick start

```sh
git clone <this-repo>
cd Recycler
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
$EDITOR .env   # DISCORD_TOKEN, DATABASE_URL, and (optional) the AI
               # backend: AI_BACKEND_URL=https://<sojourns-host>/v1
               # plus AI_BACKEND_KEY matching Sojourns' AI_BACKEND_KEY
python main.py
```

Run with **no AI** by leaving the backend vars blank or setting
`AI_ENABLED=0`. Containment and the escape room run in full.

## Deploying on Railway

Recycler and the other fleet bots are Dockerfied and run as services in
one Railway project. Sojourns is the controlling platform (and AI host);
Recycler reaches the AI over Railway private networking:

```
Railway project
+- sojourns   (Dockerfile, WEB_ENABLED=true, WEB_PORT=8080)  <- platform / AI
+- recycler   (Dockerfile, AI_BACKEND_URL=http://sojourns.railway.internal:8080/v1)
+- discoin    (Dockerfile, same AI_BACKEND_URL)              <- economy
```

1. Deploy Sojourns with `WEB_ENABLED=true` and set its `AI_BACKEND_KEY`.
2. Deploy Recycler; set `AI_BACKEND_URL=http://sojourns.railway.internal:8080/v1`
   and the **same** `AI_BACKEND_KEY`. Attach a `recycler_data` volume at
   `/data` for the containment database.
3. To run with no AI, leave the backend vars blank or set `AI_ENABLED=0`.

(Down the line Sojourns will host the bots directly; until then Railway
is the host and Sojourns is the control plane.)

## Relationship to Discoin

Recycler is the clanker carved out of the old all-in-one Discoin
("clanker") build. Discoin keeps the economy / games / NFTs and its own
REST API; Recycler takes the containment system and nothing else. Both
now route AI to Sojourns instead of embedding a model.

See `CLAUDE.md` for the engineering guidelines (framework conventions,
formatting, DB patterns) that still apply unchanged.
