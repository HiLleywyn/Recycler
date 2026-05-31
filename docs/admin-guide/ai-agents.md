# AI Control Surface

Discoin's AI lives behind a single staff-facing command group: `,ai`. This is
where you toggle feature flags, pick per-guild models, swap the heal-AI
backend, inspect the agent tool registry, and read the audit feed.

All `,ai` commands require **Manage Server** and are prefix-only. There are no
slash-command equivalents.

> **Moved from `,admin ai`.** Every subcommand that used to live under
> `,admin ai` is now on `,ai`. The old group has been removed.

---

## Quick Reference

```
,ai                          # dropdown help menu (CategoryPaginator)
,ai help                     # same as ,ai

,ai status                   # feature flags + OpenRouter key status
,ai toggle <flag>            # mm | chat | commentary | flavor | events
,ai test                     # send a test prompt to the current provider
,ai prompt <feature> [text|reset]
,ai persona [name]           # display name (blank resets)
,ai clearhistory [@user]     # wipe ,ask history
,ai reloadtools              # hot-reload tools.json + Lua plugins

,ai heal status
,ai heal backend <openrouter|ollama>
,ai heal model <name>
,ai heal baseurl <url|reset>
,ai heal reset

,ai model list
,ai model show <category>
,ai model set <category> <provider:model | catalog-index>
,ai model reset <category>

,ai tools list [category]
,ai tools info <tool_name>

,ai audit [limit]            # recent AI-scope staff actions
```

`,ai` with no subcommand opens the dropdown help menu directly.

---

## Feature flags

The chat/commentary/events/flavor/mm flags live on the guild row and gate
AI traffic feature-by-feature.

| Flag | Gates |
|---|---|
| `mm` | Market maker AI personas trading on the orderbook |
| `chat` | `,ask` and @-mention chat replies |
| `commentary` | Scheduled market commentary drops |
| `events` | Trade/event narration flavor text |
| `flavor` | `,work` and passive flavor text |

```
,ai status           # show current state of all five
,ai toggle chat      # flip one flag
,ai test             # round-trip a prompt through the current chat model
```

`,ai prompt <feature> <text>` sets a custom system prompt for one of the
feature families. `,ai prompt <feature> reset` clears it back to the default.
Valid features: `chat`, `commentary`, `events`, `flavor`.

`,ai persona <name>` renames the assistant's display name. Pass no argument
to reset. `,ai clearhistory [@user]` wipes `,ask` conversation history --
omit the user to wipe the whole guild.

---

## Model picker

Different AI jobs have different sweet-spots, so Discoin routes traffic by
**category**. Each category can be pinned to a specific `provider:model` at
the guild level; unset categories fall through to the env default.

Persisted in the `ai_model_defaults` table (primary key: `guild_id, category`).

### Categories

| Key | Label | What it drives |
|---|---|---|
| `chat` | Chat / Persona | `,ask` replies and mentions |
| `tools` | Tool Calling | Agent tool-loop dispatcher |
| `vision` | Vision / Image Describe | `vision.describe_image` tool |
| `image` | Image Generation | `image.generate` tool |
| `search` | Web Search | `data.web_search` tool |
| `code` | Code / Diagnose | `.health analyze` + heal AI |
| `reason` | Reasoning / Risk | `risk.analyze` + economic reasoning |
| `automation` | Automation / Workflow | Background task queue + triggers |
| `defi` | Crypto / DeFi Execution | Swap, stake, LP planning |
| `economy_sim` | Game Economy Simulation | What-if shock modelling |

### Picking a model

```
,ai model list                 # one-line summary per category
,ai model show chat            # curated catalog for one category (numbered)
,ai model set chat 2           # pick catalog entry #2
,ai model set chat openrouter:google/gemini-2.5-flash
,ai model reset chat           # fall back to env default
```

`,ai model set` accepts either a numeric index from `,ai model show`, or an
explicit `provider:model` string. Valid providers: `openrouter`, `ollama`.

The curated catalog is maintained in `core/framework/ai/models.py::_CATALOG`.
Update that list when new frontier models land -- the first entry in each
category is the recommended default and is highlighted in the dropdown.

---

## Heal AI backend

`.health analyze` uses a dedicated AI provider separate from the chat
backend. This lets you run a cheap chat model and a strong diagnostic
model at the same time.

```
,ai heal status                          # show current backend / model / base URL
,ai heal backend ollama                  # switch provider
,ai heal model qwen2.5-coder:32b         # change model for active backend
,ai heal baseurl http://ollama:11434     # override base URL (or "reset")
,ai heal reset                           # wipe all heal overrides
```

Supported backends: `openrouter`, `ollama`.

---

## Agent tools

Agent tools are the functions the AI can call during a tool loop. Every tool
has a risk level:

| Icon | Level | Semantics |
|---|---|---|
| 🟢 | `read` | Pure reads. No state change. |
| 🔵 | `safe` | Writes bounded, idempotent state (e.g. scheduling a task). |
| 🟠 | `mutate` | Writes user/guild economy state. |
| 🔴 | `danger` | Irreversible or high-blast-radius. Requires approval flow. |

```
,ai tools list                 # every registered tool, grouped by category
,ai tools list defi            # filter by category
,ai tools info wallet.balance  # full schema for one tool
```

### Hot reload

```
,ai reloadtools
```

This reloads:

1. `tools.json` (keyword-triggered context fragments injected into system prompts)
2. Native Python tools registered via `@tool` in `core/framework/agent_tools/tools/`
3. Lua plugin tools under `core/framework/agent_tools/lua_plugins/`

The response embed shows added/removed tools and Lua plugin counts so you
can confirm the reload picked up your edits without restarting the bot.

---

## tools.json

`tools.json` lives at the project root. Each entry injects a prompt fragment
into the system prompt when any trigger keyword matches the user's message
(word-boundary matched, case-insensitive).

```json
{
  "key": "mining",
  "name": "Mining",
  "triggers": ["mine", "mining", "rig", "hashrate"],
  "context": "MINING EXPERTISE:\nRigs scale from GTX1060 (12 MH/s, $2.5K) to ..."
}
```

| Field | Type | Description |
|---|---|---|
| `key` | string | **Required.** Internal identifier. Must be unique. |
| `name` | string | Display name. Defaults to `key` title-cased. |
| `triggers` | array of strings | Lowercase substrings. Word-boundary matched. |
| `context` | string | Prompt fragment injected when any trigger matches. |

To add a tool: edit `tools.json`, then run `,ai reloadtools`. No restart.

Multiple tools can match the same message -- all their contexts are merged
into the system prompt for that turn.

---

## Audit feed

Every mutation done through `,ai` is written to `staff_audit_log` with
`scope = 'ai'`. View it with:

```
,ai audit          # last 50 entries
,ai audit 200      # up to 250
```

The feed captures model picks (`model_set`, `model_reset`), prompt changes,
persona changes, history wipes, heal backend swaps, tool reloads, and any
other `,ai` command that mutates state. Severity is rendered as:

| Icon | Severity |
|---|---|
| 🟢 | info |
| 🟡 | warn |
| 🔴 | danger |

Other scopes have their own feeds: `,admin audit`, `,mod audit`,
`,drs audit`, `,dev audit`. Each is restricted to its own scope so an
admin feed doesn't leak dev-tool noise and vice versa.

---

## Environment variables

The AI stack is configured via env vars, overridden per-guild via the model
picker:

| Var | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Required for any OpenRouter traffic |
| `OPENROUTER_MODEL` | Default `chat` + `tools` model |
| `TOOLS_MODEL` | Default `code` / `reason` / `automation` / `defi` / `economy_sim` model |
| `VISION_MODEL` | Default `vision` model (Ollama by default) |
| `IMAGE_GEN_MODEL` | Default `image` model |
| `SEARCH_MODEL` | Default `search` model |
| `OLLAMA_BASE_URL` | Ollama endpoint for local models |

If the env for a category is unset and no guild override exists, the first
entry of the curated catalog for that category wins.
