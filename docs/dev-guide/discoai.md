# DiscoAI — memory + learning sidecar

DiscoAI is **not** a chat model.  Generation still runs through the
existing `core.framework.ai` (OpenRouter) pipeline in `cogs/help.py`.  What
DiscoAI adds is a persistent memory layer that the cloud model can
draw on and a capture pipeline so every chat turn gets logged for
later curation.

## What's in the box

```
ai/
├── __init__.py           -- public surface (MemoryService, TrainingLogger,
│                            ToolRegistry, DiscoAISettings, scope helpers)
├── config.py             -- typed settings loaded from Config env vars
├── memory.py             -- short-term Redis turns + long-term Postgres
│                            facts + episodic summaries
├── tools.py              -- ToolRegistry + default tool set (Discoin
│                            FastAPI calls + memory read/write)
└── training_logger.py    -- append-only writer for disco_training_turns
                             + 👍/👎 feedback + ShareGPT export

cogs/disco_ai.py          -- memory admin commands + passive listener
database/migrations/0123_disco_ai.sql
                          -- disco_facts, disco_episodes,
                             disco_training_turns, disco_passive_channels
```

## Tables

| Table | What's in it |
|---|---|
| `disco_facts` | `(scope, key, value, confidence, source)`.  UPSERTed by `remember_fact` / `,disco_remember`.  Surfaced into the `,ask` system prompt on every turn. |
| `disco_episodes` | Summarized conversation moments, tagged for retrieval.  Written by `,disco_listen`-enabled channels. |
| `disco_training_turns` | Every `,ask` reply (system prompt + user message + assistant reply + `feedback_score`).  Read by `scripts/export_training_data.py`. |
| `disco_passive_channels` | Per-channel opt-in for episode capture. |

## Scopes

Strings the `MemoryService` uses to namespace Postgres rows:

- `user:<id>` — per-user, cross-guild
- `user:<id>:guild:<id>` — per-user-per-guild (most useful)
- `guild:<id>` — per-server facts
- `lore` — canonical Discoin facts everyone shares

## Commands

| Command | Who | What |
|---|---|---|
| `,disco_forget` | Anyone | Clear *your* short-term memory in this channel |
| `,disco_facts [scope]` | Manage Server | List long-term facts |
| `,disco_remember <scope> <key> <value>` | Manage Server | Upsert a fact |
| `,disco_listen on\|off` | Manage Server | Toggle passive episode capture (requires `DISCOAI_PASSIVE_LEARNING=true`) |

The `,disco_remember` command is how you seed canonical lore before
training data accumulates:

```
,disco_remember lore dsd_definition  DSD is the Discoin Network stablecoin used for shop purchases.
,disco_remember guild:123  culture  This server is degen-friendly; P&L screenshots are welcome.
```

Every fact you save shows up in the `,ask` system prompt the next time
someone asks.

## How "learning" actually happens today

Two loops, both passive:

1. **Fact injection** — `cogs/help.py::ask_cmd` asks the DiscoAI cog for
   `facts_for_prompt()` before building the OpenRouter payload.  Any
   `disco_facts` rows matching the user/guild scopes get spliced into
   the system prompt.  The cloud model sees them on every turn.
2. **Turn capture** — when OpenRouter's reply comes back, the cog logs
   the full `(system, user, assistant)` trace to `disco_training_turns`.
   The export script turns that into ShareGPT JSONL whenever you want
   a training corpus.

The cog also exposes `ToolRegistry` so future integrations can hand
the existing tool schema to OpenRouter's tool-call loop.

## Env vars

| Variable | Default | What |
|---|---|---|
| `DISCOAI_SHORT_TERM_TURNS` | `12` | Per-(guild, channel, user) Redis ring |
| `DISCOAI_SHORT_TERM_TTL_S` | `3600` | Redis key TTL |
| `DISCOAI_PASSIVE_LEARNING` | `false` | Enable `,disco_listen` |
| `DISCOAI_RATE_LIMIT_PER_USER_PER_MIN` | `8` | Reserved; not currently enforced (the OpenRouter path has its own quota) |
| `DISCOAI_API_BASE_URL` | `http://127.0.0.1:$PORT` | Discoin FastAPI the default tool set calls back into |

## Export for later fine-tuning

```bash
python -m scripts.export_training_data \
    --since 2026-01-01 --min-score 1 --out training.jsonl
```

Writes one ShareGPT-format conversation per line.  Run this when you
want to hand-train a LoRA on top of whatever base model you pick —
DiscoAI itself does not include a trainer anymore.
