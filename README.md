# Recycler

**Recycler** is the clanker economy bot, packaged as its own standalone
bot with the AI brain lifted out. It is the full Discord economy --
tokens, networks, trading, staking, shops, minigames, NFTs, seasons,
quests, the lot -- minus a built-in model. Every AI feature is served by
the **Sojourns** platform over an OpenAI-compatible HTTP backend.

The split is deliberate:

- **Recycler** owns the economy. It is large, self-contained and runs
  the entire game on its own.
- **Sojourns** owns the AI. The Disco / AI Chat brain, the clanker
  pattern controller, conversational memory and the tool loop all live
  there, alongside Archimedes.

**AI is optional.** Recycler boots and runs the complete economy with no
AI configured at all. Turn the Sojourns backend on and the AI-gated
features (chat, Disco, pattern control, AI helpers) light up and extend
the bot -- turn it off and those features degrade gracefully while
everything else keeps working.

## How the AI is wired

Recycler funnels every model call through one seam --
`core/framework/ai/client.py` -- which posts to an OpenAI-compatible
backend. Three env vars point it at Sojourns:

| Variable | Default | Purpose |
|---|---|---|
| `AI_BACKEND_URL` | `https://openrouter.ai/api/v1` | The `/v1` base URL of the Sojourns AI backend. The OpenRouter default lets Recycler run standalone with no Sojourns. |
| `AI_BACKEND_KEY` | (falls back to `OPENROUTER_API_KEY`) | Bearer token the Sojourns backend expects (its own `AI_BACKEND_KEY`). |
| `AI_ENABLED` | on when a key is present | Master switch. `AI_ENABLED=0` runs a pure-economy bot with zero AI calls. |

On the Sojourns side, the matching backend is
`sojourns/ai_backend_api.py`, mounted at `POST /v1/chat/completions`
(plus `/v1/models` and `/ai/backend/health`). It speaks the standard
OpenAI chat-completions contract -- text, tool calls, usage and SSE
streaming -- and is driven by the same model chain that powers
Archimedes. So pointing Recycler at
`https://<your-sojourns-host>/v1` is all it takes.

```
Recycler (economy)                 Sojourns (AI)
  cogs/*  ->  core/framework/ai/client.py
                 |  POST {AI_BACKEND_URL}/chat/completions
                 v
            sojourns/ai_backend_api.py  ->  ai/client.py (model chain)
```

## Quick start

```sh
git clone <this-repo>
cd Recycler
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
$EDITOR .env   # set DISCORD_TOKEN, DATABASE_URL, and (optional) the AI
               # backend: AI_BACKEND_URL=https://<sojourns-host>/v1
               # plus AI_BACKEND_KEY to match Sojourns' AI_BACKEND_KEY
python main.py
```

To run **without AI**, leave `AI_BACKEND_URL`/keys blank (or set
`AI_ENABLED=0`). The economy runs in full; AI-gated commands report that
AI is unavailable rather than failing the bot.

## Deploying on Railway

Both bots are Dockerfied and run as **two services in one Railway
project**. Sojourns is the controlling platform (and AI host); Recycler
is the economy bot beside it. Recycler reaches the AI over Railway
private networking:

```
Railway project
+- sojourns   (Dockerfile, WEB_ENABLED=true, WEB_PORT=8080)  <- controlling platform / AI
+- recycler   (Dockerfile, AI_BACKEND_URL=http://sojourns.railway.internal:8080/v1)
```

1. Deploy Sojourns with `WEB_ENABLED=true` and set its `AI_BACKEND_KEY`.
2. Deploy Recycler; set `AI_BACKEND_URL=http://sojourns.railway.internal:8080/v1`
   and the **same** `AI_BACKEND_KEY`. Attach a volume at `/data`
   (`recycler_data`) for the economy database.
3. To run Recycler with no AI at all, leave the backend vars blank or set
   `AI_ENABLED=0`.

`railway.toml` and `Dockerfile` ship with the repo. (Down the line
Sojourns will host the bots directly; until then Railway is the host and
Sojourns is the control plane.)

## Relationship to Discoin

Recycler is the economy half of what used to be the all-in-one Discoin
("clanker") bot. Discoin remains as the original reference build; Recycler
is the separated, AI-out package whose model features are provided by
Sojourns. The internal package layout (`core/`, `cogs/`, `services/`,
`configs/`, `database/`) is preserved so the economy is bit-for-bit the
proven build -- the only behavioural change is that AI inference now
travels to Sojourns instead of being embedded.

See `CLAUDE.md` for the engineering guidelines (framework conventions,
formatting, DB patterns) that still apply unchanged.
