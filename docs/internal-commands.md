# Internal Commands (Natural Language Interface)

Discoin includes a natural-language command module that lets users interact with the bot conversationally.

## How to Invoke

| Method | Example |
|--------|---------|
| **Prefix** | `bot balance`, `disco buy ARC 1` |
| **@Mention** | `@Discoin show my balance` |
| **Keyword** | `discoin top gainers`, `assistant my rigs` |
| **Slash command** | `/discoin prompt: market overview` |

The prefixes `bot`, `disco`, `discoin`, and `assistant` are all recognized (case-insensitive).

## How It Works

```
User message
    |
    v
PromptInterpreter.parse()
    |
    +--> 1. Multi-step rule-based planner (fast, 0.90-0.96 confidence)
    |        Splits "move X then sell it all" into ordered steps
    |
    +--> 2. Rule-based regex matching (fast, 0.80-0.99 confidence)
    |        Direct aliases and pattern matching
    |
    +--> 3. LLM fallback (if OPENROUTER_API_KEY set)
    |        AI classifies ambiguous inputs
    |
    v
InternalToolExecutor.execute()
    |
    +--> Replay commands: "bot buy ARC 1" -> ".buy ARC 1"
    +--> Native commands: "bot top gainers" -> compute from DB
    +--> Multi-step: execute each step in order
```

## Supported Prompts

### Balance & Portfolio
- "show my balance", "how much do I have"
- "what's my portfolio worth", "my net worth"
- "show my holdings", "what tokens do I own"

### Trading
- "buy 100 dollars of ARC", "sell all my SOL"
- "swap 500 USDC to LINK", "what's the price of MTA"
- "show top gainers", "biggest losers today"

### Mining
- "show my rigs", "mining status"
- "buy 2 RTX4090", "how much hashrate do I have"

### Multi-Step
- "buy ARC with 500 then stake it all"
- "sell all SOL and transfer half to @user"
- "move my ARC to DeFi wallet then add liquidity"

The planner carries context across clauses ("it", "that", wallet locations, last token mentioned).
