# WebSocket API

Real-time feeds are available via WebSocket at `/api/v2/ws`.

## Connection

```javascript
const ws = new WebSocket('ws://localhost:8080/api/v2/ws?token=YOUR_JWT');
```

The JWT is required for authentication. The connection is scoped to the guild in the JWT.

## Events

Once connected, you receive real-time events as JSON:

```json
{
  "event": "trade_executed",
  "guild_id": "123456789",
  "data": {
    "user_id": 987654321,
    "symbol": "ARC",
    "action": "buy",
    "amount": 1.5,
    "price": 3900.00
  },
  "ts": 1711400000.123
}
```

## Event Types

| Event | Description |
|---|---|
| `prices_updated` | Token prices changed |
| `trade_executed` | Buy/sell completed |
| `swap_executed` | Token swap completed |
| `transfer_sent` | USD transfer between users |
| `block_bundled` | New chain block sealed |
| `block_mined` | PoW block mined |
| `stake_created` | Tokens staked |
| `stake_removed` | Tokens unstaked |
| `stake_reward` | Staking reward distributed |
| `lp_added` | Liquidity added to pool |
| `lp_removed` | Liquidity removed |
| `game_result` | Gambling game completed |
| `whale_alert` | Large transaction detected |
| `drop_spawned` | Money drop appeared |
| `badge_earned` | User earned a badge |
| `security_alert` | Economy security flag |

Events originating from both Discord commands and API requests are published to the same feed.
