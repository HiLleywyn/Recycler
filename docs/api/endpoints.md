# API Endpoints

Base URL: `http://localhost:8080/api/v2`

For the full interactive reference with request/response schemas, use the [Swagger UI](/api/docs) when the server is running.

## Endpoint Domains

### Market Data

| Method | Path | Description |
|---|---|---|
| GET | `/market/prices` | All token prices |
| GET | `/market/prices/{symbol}` | Single token price |
| GET | `/market/candles/{symbol}` | OHLCV candlestick data |
| GET | `/market/networks` | Network list and stats |

### Trading

| Method | Path | Description |
|---|---|---|
| POST | `/trading/buy` | Buy tokens with USD |
| POST | `/trading/sell` | Sell tokens for USD |
| POST | `/trading/swap` | Swap between token pairs |
| POST | `/trading/transfer` | Transfer USD to another user |

### Pools (AMM)

| Method | Path | Description |
|---|---|---|
| GET | `/pools` | List all liquidity pools |
| GET | `/pools/{pool_id}` | Pool details and reserves |
| GET | `/pools/my-positions` | Your LP positions |
| POST | `/pools/add-liquidity` | Add liquidity to a pool |
| POST | `/pools/remove-liquidity` | Remove liquidity |

### Staking

| Method | Path | Description |
|---|---|---|
| GET | `/staking/validators` | List validators |
| GET | `/staking/my-stakes` | Your active stakes |
| POST | `/staking/stake` | Stake tokens |
| POST | `/staking/unstake` | Unstake tokens |
| POST | `/staking/delegate` | Delegate to a validator |
| POST | `/staking/undelegate` | Remove delegation |

### Mining

| Method | Path | Description |
|---|---|---|
| GET | `/mining/networks` | PoW network stats |
| GET | `/mining/miners` | Active miners leaderboard |
| POST | `/mining/buy-rig` | Purchase mining rig |
| POST | `/mining/sell-rig` | Sell mining rig |
| POST | `/mining/set-network` | Reassign rigs to chain |
| POST | `/mining/set-mode` | Set solo/pool/group mode |

### Games

| Method | Path | Description |
|---|---|---|
| POST | `/games/coinflip/play` | Play coinflip |
| POST | `/games/slots/play` | Play slots |
| POST | `/games/dice/play` | Play dice |
| POST | `/games/roulette/play` | Play roulette |
| POST | `/games/blackjack/start` | Start blackjack hand |
| POST | `/games/blackjack/action` | Hit/stand |
| POST | `/games/mines/start` | Start mines game |
| POST | `/games/mines/reveal` | Reveal a tile |
| POST | `/games/mines/cashout` | Cash out mines |

All games use provably fair outcomes (HMAC-SHA256 server seeds).

### Savings & Lending

| Method | Path | Description |
|---|---|---|
| GET | `/savings/my-deposits` | Your savings deposits |
| POST | `/savings/deposit` | Deposit to savings |
| POST | `/savings/withdraw` | Withdraw from savings |
| GET | `/lending/stats` | Protocol lending stats |
| GET | `/lending/loans` | Active USD loans |
| POST | `/lending/borrow` | Borrow against collateral |
| POST | `/lending/repay` | Repay loan |

### Users & Portfolio

| Method | Path | Description |
|---|---|---|
| GET | `/users/me` | Your profile |
| GET | `/users/{id}/profile` | User public profile |
| GET | `/users/{id}/holdings` | User holdings |
| GET | `/portfolio/net-worth` | Full net worth breakdown |
| GET | `/portfolio/history` | Portfolio value over time |

### Admin

| Method | Path | Description |
|---|---|---|
| GET | `/admin/settings` | Guild settings |
| PATCH | `/admin/settings` | Update settings |
| GET | `/admin/auto-delete` | Auto-delete config |
| PATCH | `/admin/auto-delete` | Update auto-delete |
| PATCH | `/admin/chain/{symbol}` | Update chain config |
| POST | `/admin/chain/{symbol}/reset` | Reset chain to block 0 |

### Admin Ops (Remote Management)

All ops endpoints require an admin JWT (`is_admin` or `is_owner` claim). See [Admin Ops API](../admin-guide/ops-api.md) for full details.

| Method | Path | Description |
|---|---|---|
| GET | `/admin/ops/overview` | Status snapshot of all services (Postgres, Redis, Bot, API) |
| GET | `/admin/ops/postgres` | Postgres diagnostics  -  version, size, connections, table sizes, slow queries |
| POST | `/admin/ops/postgres/query` | Execute a read-only SQL query (`SELECT`/`EXPLAIN`/`WITH`/`SHOW` only) |
| GET | `/admin/ops/redis` | Redis diagnostics  -  memory, key prefixes, hit rate, sample keys |
| POST | `/admin/ops/redis/command` | Execute a safe read-only Redis command |
| GET | `/admin/ops/bot` | Bot diagnostics  -  guilds, cogs, uptime, background tasks, recent errors |
| POST | `/admin/ops/bot/reload-cog` | Hot-reload a cog without restarting the bot |
| POST | `/admin/ops/bot/sync-commands` | Force-sync slash commands to Discord |
| GET | `/admin/ops/api` | API server introspection  -  routes, middleware stack |

### Stats

| Method | Path | Description |
|---|---|---|
| GET | `/stats/leaderboard` | Net worth leaderboard |
| GET | `/stats/overview` | Server economy overview |

## Rate Limits

- **Unauthenticated**: 30 requests / 10 seconds
- **Authenticated**: 60 requests / 10 seconds
- **Admin**: 240 requests / 10 seconds

Rate limit headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`

## PoS Validators & Mempool

### GET /api/v2/staking/pos-validators

Returns all player-run PoS validators for the guild.

**Response:**
```json
[
  {
    "user_id": 123456789,
    "network": "Arcadia Network",
    "stake_token": "ARC",
    "stake_amount": 250.0,
    "is_active": true,
    "total_blocks_validated": 47,
    "total_rewards_earned": 12.8,
    "slash_count": 0,
    "delegation_count": 3,
    "total_delegated": 120.0
  }
]
```

### POST /api/v2/staking/delegate

Delegate tokens to a player validator.

**Body:** `{ "validator_user_id": int, "network": str, "amount": float }`

### POST /api/v2/staking/undelegate

Undelegate from a player validator. Early unstake within 48h incurs a 5% burn.

**Body:** `{ "validator_user_id": int, "network": str, "amount": float }`

### GET /api/v2/staking/my-delegations

Returns the authenticated user's active delegations.

**Response:**
```json
[
  {
    "validator_user_id": 123456789,
    "network": "Arcadia Network",
    "token": "ARC",
    "amount": 50.0,
    "locked_until": "2026-03-31T12:00:00Z",
    "total_earned": 0.42
  }
]
```

### GET /api/v2/blockchain/mempool

Returns pending unconfirmed transactions, ordered by gas fee descending.

**Query params:** `network` (optional), `limit` (default 50)

### GET /api/v2/stats/reserve

Public. Returns treasury balance and gas fee statistics.

**Response:**
```json
{
  "treasury_balance": 12450.50,
  "total_gas_collected": 8920.30,
  "total_distributed_to_validators": 8028.27,
  "total_burned": 342.15
}
```

---

## Event Publishing

All successful state-changing API requests (POST/PATCH/DELETE) automatically publish events to the bot's Redis event bus via middleware. This means:

- The economy security monitor sees API activity
- WebSocket feeds update in real time
- Whale alerts fire for large API transactions
- Anti-bot game lockouts are enforced on API gambling endpoints
