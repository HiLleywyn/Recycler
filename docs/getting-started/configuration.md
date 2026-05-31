# Configuration Reference

Discoin uses a layered configuration system. Understanding which layer owns a value tells you where to look and where to change it.

---

## Configuration Layers

| Layer | Location | What lives here | Override method |
|---|---|---|---|
| **Business constants** | `constants/` package | Rates, limits, fees, colors, game rules | Edit the Python file directly |
| **Runtime config** | `core/config.py` | Tokens, networks, pools, env-loaded settings | `.env` variables |
| **Security thresholds** | `security/config.py` | Abuse detection windows, threat scoring | `SEC_*` env variables |
| **Per-server settings** | Database (`guild_settings`) | Prefix, channels, toggles, admin overrides | `.admin` commands |

### When to use which layer

- **Changing a swap fee or slash rate?** Edit `constants/trading.py` or `constants/validators.py`.
- **Changing a Discord token or database URL?** Set it in `.env` (loaded by `core/config.py`).
- **Tuning anti-abuse thresholds?** Set `SEC_*` env variables (loaded by `security/config.py`).
- **Changing per-server prefix or channels?** Use `.admin` commands in Discord.

---

## The `constants/` Package

Pure Python modules with zero framework imports. Every business constant lives here exactly once. Cogs, services, API routers, and the frontend all import from this package.

### `constants/validators.py`  -  PoS Validator Rules

| Constant | Value | Description |
|---|---|---|
| `VALIDATOR_TICK` | `120` | Seconds between validator reward ticks |
| `VALIDATOR_REWARD` | `0.90` | Validator's share of block reward (90%) |
| `TREASURY_CUT` | `0.10` | Treasury's share of block reward (10%) |
| `MIN_STAKE` | `100.0` | Minimum SUN to register a validator |
| `MIN_VALIDATORS` | `2` | Minimum validators required for block production |
| `STAKE_LOCK_SECS` | `86400` | Stake lock period (24 hours) |
| `MAX_SLASH_COUNT` | `5` | Slashes before validator is jailed |
| `SLASH_RATE` | `0.05` | Percentage slashed per offense (5%) |
| `SLASH_DECAY_SECS` | `604800` | Slash count decay period (7 days) |
| `MAX_MEMPOOL` | `50` | Maximum pending transactions in mempool |
| `DELEGATION_VALIDATOR_KEEP` | `0.80` | Validator keeps 80% of delegated rewards |
| `DELEGATION_POOL_SHARE` | `0.20` | Delegators share 20% of rewards |
| `DELEGATION_LOCK_SECS` | `86400` | Delegation lock period (24 hours) |
| `MIN_DELEGATION` | `50.0` | Minimum delegation amount |
| `MAX_DELEGATIONS` | `3` | Maximum delegations per user |
| `GAS_TIERS` | `high/medium/low` | Gas fee multipliers (0.50 / 0.20 / 0.05) |

### `constants/trading.py`  -  Swap and Trade Rules

| Constant | Value | Description |
|---|---|---|
| `DEFAULT_SWAP_FEE` | `0.003` | AMM swap fee (0.3%) |
| `PLATFORM_FEE_RATIO` | `0.1` | Platform's share of swap fees (10%) |
| `ARB_FEE` | `0.003` | Arbitrage rebalance fee |
| `SLIPPAGE_WARN` | `0.02` | Warn user when slippage exceeds 2% |
| `PRICE_FLOOR` | `0.001` | Minimum token price |
| `USD_PRECISION` | `2` | Decimal places for USD amounts |
| `TOKEN_PRECISION` | `8` | Decimal places for token amounts |
| `MIN_TRADE_USD` | `0.01` | Minimum trade size |
| `QUOTE_EXPIRY_SECS` | `5` | Swap quote validity window |

### `constants/economy.py`  -  Lock Periods, Cooldowns, Limits

| Constant | Value | Description |
|---|---|---|
| `STAKE_LOCK_PERIOD` | `86400` | Stake lock duration (24 hours) |
| `CHAIN_SWITCH_COOLDOWN` | `600` | Mining chain switch cooldown (10 min) |
| `MIN_COLLATERAL_RATIO` | `1.5` | Minimum loan collateral ratio (150%) |
| `BASE_DEPOSIT_APY` | `0.05` | Base savings deposit APY (5%) |
| `BASE_BORROW_APY` | `0.08` | Base borrow APY (8%) |
| `AI_COOLDOWN_SECS` | `5` | Cooldown between AI commands |
| `AI_QUOTA_LIMIT` | `25` | AI requests per hour per user |
| `REPORT_COOLDOWN` | `300` | Cooldown between user reports (5 min) |
| `WS_HEARTBEAT_INTERVAL` | `30` | WebSocket heartbeat interval |
| `ERROR_MAX_PER_GUILD` | `500` | Max error log entries per guild |
| `ADMIN_MAX_UPLOAD_BYTES` | `24 MB` | Maximum admin upload file size |

### `constants/games.py`  -  Gambling Rules

| Constant | Value | Description |
|---|---|---|
| `MINES_TOTAL_TILES` | `24` | Total tiles in mines game |
| `MINES_DEFAULT_BOMBS` | `3` | Default bomb count |
| `MINES_HOUSE_EDGE` | `0.05` | House edge (5%) |
| `MINES_TIMEOUT_SECS` | `120` | Game timeout (2 min) |
| `SLOT_THREE_OF_A_KIND_PAYOUT` | `5.0` | Slots 3-of-a-kind multiplier |
| `SLOT_PAIR_PAYOUT` | `1.5` | Slots pair multiplier |
| `CROSS_GAME_WINDOW` | `300` | Anti-bot detection window (5 min) |
| `CROSS_GAME_LIMIT` | `40` | Max games in anti-bot window |

### `constants/ui.py`  -  Embed Colors

| Constant | Hex | Usage |
|---|---|---|
| `C_SUCCESS` | `#2ecc71` | Success embeds, buy confirmations |
| `C_ERROR` | `#e74c3c` | Error embeds, sell confirmations |
| `C_WARNING` | `#e67e22` | Warning embeds |
| `C_INFO` | `#3498db` | Informational embeds |
| `C_GOLD` | `#f1c40f` | Gold/premium embeds |
| `C_PURPLE` | `#9b59b6` | Staking/DeFi embeds |
| `C_TEAL` | `#1abc9c` | Mining embeds |
| `C_AMBER` | `#f39c12` | Market/trading embeds |

### `constants/security.py`  -  Security Re-exports

This module re-exports everything from `security/config.py` for import consistency. See the [Security Thresholds](#security-thresholds) section below.

---

## Environment Variables (`.env`)

These are loaded by `core/config.py` at startup via `python-dotenv`.

### Required

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your Discord bot token |
| `DATABASE_URL` | PostgreSQL connection string (default: `postgresql://discoin:discoin@localhost:5432/discoin`) |
| `DISCORD_CLIENT_ID` | OAuth2 Client ID for dashboard login |
| `DISCORD_CLIENT_SECRET` | OAuth2 Client Secret |
| `DISCORD_REDIRECT_URI` | OAuth2 redirect (default: `http://localhost:8080/api/auth/callback`) |
| `JWT_SECRET` | Secret for signing dashboard JWTs. Use `openssl rand -hex 32` |

### Optional: Core

| Variable | Default | Description |
|---|---|---|
| `PREFIX` | `$` | Command prefix (e.g. `.` for `.balance`) |
| `REDIS_URL` | `redis://localhost:6379` | Redis for event bus and API pub/sub |
| `TX_SALT` | `econbot-default-salt` | Salt for transaction hashes. Set once, never change |
| `SLASH_GUILD_ID` | (none) | Guild ID for instant slash sync during development |
| `REPORT_TARGET_USER_ID` | (none) | Discord user ID to receive report DMs and security alerts |
| `API_PORT` | `8080` | Port for web dashboard and REST API |
| `API_KEY` | (none) | Admin API key for dashboard write operations |
| `DASHBOARD_URL` | (none) | Public URL of dashboard (used in Discord embeds) |
| `DEV_STATUS_DM_INTERVAL` | `4` | Hours between auto status DMs to the bot developer |

### Optional: Economy

| Variable | Default | Description |
|---|---|---|
| `STARTING_BALANCE` | `1000` | USD given to new users on first command |
| `DAILY_AMOUNT` | `1000` | Base `.daily` claim amount |
| `WORK_COOLDOWN` | `900` | Seconds between `.work` (15 min) |
| `MAX_BET` | `500000` | Maximum gambling bet |
| `AUTO_DROP_INTERVAL` | `1800` | Seconds between automatic money drops (30 min) |
| `DROP_MIN` / `DROP_MAX` | `100` / `2000` | Random drop value range |
| `DROP_COLLECT_WINDOW` | `30` | Seconds the claim button stays open |
| `CHAIN_BLOCK_INTERVAL` | `1800` | Seconds between block seals (30 min) |

### Optional: AI

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | (none) | OpenRouter API key. Blank disables all AI features |
| `OPENROUTER_MODEL` | `openrouter/hunter-alpha` | AI model for all completions |
| `AI_MM_ENABLED` | `1` | AI market maker bot (auto-trades) |
| `AI_CHAT_ENABLED` | `1` | `.ask` command |
| `AI_COMMENTARY_ENABLED` | `1` | AI market commentary after large price moves |
| `AI_FLAVOR_ENABLED` | `0` | AI flavor text on `.work` responses |
| `AI_EVENTS_ENABLED` | `1` | AI narration of notable events |

### Optional: Anti-Bot

| Variable | Default | Description |
|---|---|---|
| `ANTIBOT_MIN_GAMES` | `50` | Minimum consecutive games before CAPTCHA threshold |
| `ANTIBOT_MAX_GAMES` | `100` | Maximum consecutive games before CAPTCHA threshold |

### Optional: Infrastructure

| Variable | Default | Description |
|---|---|---|
| `AUTO_SEED_POOLS` | `false` | Auto-seed all AMM pools with $500k on startup |
| `POOL_SEED_STABLECOIN` | `500000` | Stablecoin depth for auto-seeding |
| `BACKUP_INTERVAL_HOURS` | `6` | Automatic DB backup interval |
| `BACKUP_KEEP` | `7` | Number of backup files to retain |

---

## Security Thresholds

All security constants live in `security/config.py` and can be overridden with `SEC_` prefixed environment variables.

For example, to change the income velocity limit:
```bash
SEC_INCOME_VELOCITY_LIMIT=30
```

### Detection Windows

| Variable | Env Override | Default | Description |
|---|---|---|---|
| `SCAN_INTERVAL_SECONDS` | `SEC_SCAN_INTERVAL` | `120` | Seconds between security scans |
| `LOOKBACK_SECONDS` | `SEC_LOOKBACK` | `300` | Transaction lookback window (5 min) |

### Economy Abuse Detection

| Variable | Env Override | Default | Description |
|---|---|---|---|
| `INCOME_VELOCITY_LIMIT` | `SEC_INCOME_VELOCITY_LIMIT` | `20` | Max income events per scan window |
| `GAMBLING_VELOCITY_LIMIT` | `SEC_GAMBLING_VELOCITY_LIMIT` | `50` | Max gambling events per scan window |
| `WASH_TRADE_MIN_CYCLES` | `SEC_WASH_TRADE_MIN_CYCLES` | `6` | Minimum cycles to flag wash trading |
| `TRANSFER_RING_MIN` | `SEC_TRANSFER_RING_MIN` | `4` | Minimum participants to flag transfer ring |
| `LP_CHURN_MIN` | `SEC_LP_CHURN_MIN` | `4` | Minimum LP add/remove cycles to flag |
| `TX_FLOOD_LIMIT` | `SEC_TX_FLOOD_LIMIT` | `80` | Max transactions per scan window |

### Threat Response Levels

| Level | Threshold | Action |
|---|---|---|
| Level 1 | 21 points | Log and monitor |
| Level 2 | 41 points | Throttle (10 min) |
| Level 3 | 61 points | Freeze account (15 min) |
| Level 4 | 81 points | Flag for admin review (1 hour) |
| Level 5 | 91 points | Emergency lockdown (30 min) |

All thresholds and durations are overridable via `SEC_LEVEL_1_THRESHOLD`, `SEC_THROTTLE_DURATION`, etc.

---

## Frontend Constants

The frontend fetches shared business constants from the API:

```
GET /api/v2/constants
```

This returns validator, trading, game, and economy constants so the dashboard stays in sync with the bot without duplicating values.

Use the `useConstants()` hook in React components:

```tsx
import { useConstants } from "@/lib/constants";

function MyComponent() {
  const { data } = useConstants();
  // data.validators.MAX_SLASH_COUNT, data.trading.DEFAULT_SWAP_FEE, etc.
}
```

---

## Quick Reference: Where to Find Things

| I want to change... | Look in... |
|---|---|
| Swap fee, slippage threshold | `constants/trading.py` |
| Validator rewards, slash rules | `constants/validators.py` |
| Stake lock periods, cooldowns | `constants/economy.py` |
| Game payouts, house edge | `constants/games.py` |
| Embed colors | `constants/ui.py` |
| Security thresholds | `security/config.py` (or `SEC_*` env vars) |
| Discord token, DB URL, API keys | `.env` |
| Token definitions, networks, pools | `core/config.py` |
| Per-server prefix, channels | `.admin` commands in Discord |
