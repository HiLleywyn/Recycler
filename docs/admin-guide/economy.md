# Economy Settings

This page covers the knobs available for tuning your server's economy: auto-delete, fees, whale alerts, drops, work/daily income, pool management, and the wealth equalizer + yield-throttle stack.

!!! info "Where do these values live?"
    Hardcoded business rules (swap fees, lock periods, game payouts) live in the `constants/` package. Environment-overridable settings live in `.env` / `core/config.py`. Security thresholds use `SEC_*` env vars in `security/config.py`. See [Configuration Reference](../getting-started/configuration.md) for the full picture.

---

## Auto-Delete

Control whether command messages and bot replies are automatically deleted after a delay. Useful for keeping channels clean.

### View current settings

```
.admin autodelete
```

### Configure each category

```
.admin autodelete commands 10        # delete user commands after 10 seconds
.admin autodelete replies 30         # delete bot replies after 30 seconds
.admin autodelete aicommands 15      # delete .ask commands after 15 seconds
.admin autodelete aireplies 60       # delete AI replies after 60 seconds
```

To disable any category:

```
.admin autodelete commands off
.admin autodelete replies off
.admin autodelete aicommands off
.admin autodelete aireplies off
```

- Duration range: 1--3600 seconds (1 second to 1 hour).
- AI commands (`.ask`) and AI replies have independent settings from regular commands.

!!! tip
    Setting `commands 5` and `replies 30` keeps your channel tidy while giving users enough time to read responses.

---

## Fee Configuration

Fees are configured through environment variables and token contract parameters. Here is an overview of the fee layers in the system.

### Platform fees (CeFi/DeFi transfers and trades)

Set via environment variables:

| Variable | Default | Description |
|---|---|---|
| `WALLET_PLATFORM_FEE_PCT` | `0.002` (0.2%) | Percentage of USD value per transaction |
| `WALLET_PLATFORM_FEE_MIN` | `0.10` | Minimum fee floor ($0.10) |
| `WALLET_PLATFORM_FEE_MAX` | `20.00` | Maximum fee cap ($20.00) |

One quarter of every platform fee is deposited to the Community Reserve (savings pool).

### Token-level fees

Per-token fees are set with the contract system:

```
.admin setcontract ARC transfer_fee 0.01    # 1% fee on ARC transfers
.admin setcontract ARC burn_rate 0.005      # 0.5% burned per transfer
```

Both `transfer_fee` and `burn_rate` accept values from 0 to 0.10 (0--10%).

### Swap fees

The base swap fee is `DEFAULT_SWAP_FEE` (0.3%) defined in `constants/trading.py`. The `FEE_BURN_FRACTION` config (default 25%) burns a portion of all swap fees, creating deflationary pressure.

### Built-in token fees

Each built-in token has a `tx_fee_rate` and `gas_fee` defined in the token config. For example, SUN has a 0.1% transfer fee and $0.01 base gas cost.

---

## Whale Alert Thresholds

Whale alerts trigger when a single transaction exceeds a USD value threshold.

### Set the threshold

```
.admin whalethreshold 10000
```

Transactions at or above this value (in equivalent USD) will:

1. Post an alert to the whale alerts channel (if configured via `.admin setchannel whale #channel`)
2. Trigger the economy security monitor for whale concentration tracking

### Default threshold

The default is set by the `WHALE_ALERT_THRESHOLD_USD` environment variable (default `$50,000`). The `.admin whalethreshold` command overrides this per-server.

!!! tip
    For smaller economies, set this lower (e.g. `5000` or `1000`). For mature economies with large balances, keep it higher to reduce noise.

---

## Drop Settings

Drops are periodic currency events that appear in a channel for users to claim.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `AUTO_DROP_INTERVAL` | `1800` (30 min) | Seconds between automatic drops |
| `DROP_MIN` | `100` | Minimum drop amount |
| `DROP_MAX` | `2000` | Maximum drop amount |
| `DROP_COLLECT_WINDOW` | `30` | Seconds users have to claim a drop |

### Channel configuration

Drops require two channels:

```
.admin setchannel dropsspawn #drop-here     # where drops appear for claiming
.admin setchannel drops #drop-log           # activity log of claimed drops
```

If no spawn channel is set, automatic drops are not posted.

!!! tip
    Set `DROP_COLLECT_WINDOW` to a short value (15--30 seconds) to reward active members and make drops feel competitive.

---

## Work and Daily Tuning

### Daily claim

| Config | Default | Description |
|---|---|---|
| `DAILY_AMOUNT` | `200` | Base daily claim amount |
| `DAILY_STREAK_BONUS` | `10.0` | Bonus per consecutive day streak |
| `DAILY_MAX_STREAK` | `365` | Maximum streak days counted |
| `DAILY_COOLDOWN` | `86400` | Cooldown in seconds (24 hours) |

When `DAILY_SCALING_ENABLED` is true (default), daily rewards are tilted toward poorer players: the per-player **net worth** ratio versus the server median (read from `services.net_worth.compute_bulk_net_worth`) drives a relative multiplier of up to 2x for the poorest. The shared whale-yield-throttle curve (`Config.WEALTH_YIELD_THROTTLE_CURVE`) is then stacked on top so the wealthiest log-decay toward the throttle floor (default `WEALTH_YIELD_THROTTLE_FLOOR = 0.10`). This curve also gates `,work`, savings interest, validator + delegator block rewards, and LP yield -- one tunable controls every passive surface.

### Work command

| Config | Default | Description |
|---|---|---|
| `WORK_COOLDOWN` | `900` (15 min) | Seconds between work commands |
| `WORK_PROGRESSIVE_TAX_THRESHOLD` | `5,000` | Earnings above this are taxed |
| `WORK_PROGRESSIVE_TAX_RATE` | `0.65` | 65% tax on excess above threshold |

### Per-job daily caps

Each job has a daily income cap defined in `WORK_DAILY_CAP`. For example, the Airdrop Farmer job caps at $6,000/day. An aggregate cap of `AGGREGATE_DAILY_INCOME_CAP` ($1M/day) applies across all income sources per player.

---

## Pool Management

### Seeding

When you add a custom token on a network with a stablecoin, a TOKEN/STABLECOIN pool is automatically seeded using the `POOL_SEED_STABLECOIN` formula (default $500,000 in each side).

### Pool safety parameters

These are system-level configs (environment or `core/config.py`):

| Config | Default | Description |
|---|---|---|
| `MAX_SWAP_FRACTION` | `0.15` (15%) | Max percentage of a pool's reserve that can be swapped in one transaction |
| `USER_SWAP_HOURLY_LIMIT_USD` | `$500,000` | Per-user rolling 1-hour swap volume cap |
| `LOW_LIQUIDITY_THRESHOLD` | `$100,000` | Pools below this TVL get stricter limits |
| `LOW_LIQUIDITY_SWAP_FRACTION` | `0.05` (5%) | Max swap fraction for low-liquidity pools |

### LP protections

| Config | Default | Description |
|---|---|---|
| `LP_LOCK_SECONDS` | `7200` (2 hours) | Minimum hold time after adding LP |
| `LP_MAX_CONCENTRATION` | `0.50` (50%) | No single LP can own more than 50% of a pool |
| `LP_LARGE_REMOVAL_THRESHOLD` | `0.25` (25%) | Removals exceeding this fraction of pool need throttling |
| `LP_LARGE_REMOVAL_COOLDOWN` | `600` (10 min) | Cooldown between large LP removals |

### Admin pool commands

```
.admin removepool ARC USD              # delete a pool
.admin rebalancepool ARC USD 2000      # rebalance to new price (preserves k)
```

---

## Inflation & Distribution Controls

Discoin keeps wealth distribution sound with a paired system: a **daily wealth tax + UBI loop** drains principal off the top and stipends the bottom; a per-tick **whale yield throttle** slows the regrowth across every passive and active income surface.

### Wealth Equalizer (daily)

A guild-scoped background task fires every `WEALTH_EQUALIZER_INTERVAL_HOURS` (default 24) and runs two phases:

1. **Marginal-bracket tax** on every player's full **net worth** (read from `services.net_worth.compute_bulk_net_worth`, so wealth held in stones, rigs, LPs, NFTs, etc. counts -- not just cash on hand). The owed amount is then drained across every liquidatable asset class in priority order: **wallet -> bank -> USD savings -> CeFi crypto -> DeFi wallet holdings -> stone staked_amount (hash / lock / vault / liq) -> mining rigs (sold back at 50% book value)**. Crypto holdings are sold at oracle price (`update_holding` / `update_wallet_holding` decrement circulating supply on the burn). LP positions and NFTs are skipped by the drain because mid-cycle LP unwinds have too many price-impact failure modes; they still count toward the *owed* amount via NW, the drain just can't physically pull from them. Per-cycle drain is hard-capped at `WEALTH_TAX_MAX_DRAIN_PCT` (25%) of NW so a single cycle can never wipe a player out -- a $1B-NW holder pays at most $250M per cycle.
2. **UBI stipend** -- the resulting redistribution pool is split among every active player who is **not** in the top `WEALTH_UBI_TOP_EXCLUSION_COUNT` (default 10) of the guild net-worth leaderboard, with active = `last_activity` within the last `WEALTH_UBI_ACTIVE_DAYS` days (default 7). Distribution is rank-weighted with a linear inverse-rank curve: poorest eligible player gets weight `n`, richest eligible gets weight `1`, sum `n*(n+1)/2` -- so poorer players receive a proportionally larger share. Each share is then clamped into `[WEALTH_UBI_MIN_PAYOUT, WEALTH_UBI_MAX_PAYOUT]`; shares above the max are clamped (leftover stays in the pool). **Small-pool fallback:** if the poorest player's natural rank-weighted share would already be below `WEALTH_UBI_MIN_PAYOUT`, the rank curve is dropped for the cycle and the pool pays flat `WEALTH_UBI_MIN_PAYOUT` to the poorest k recipients it can afford -- so a slow-growing pool still helps the bottom of the leaderboard instead of accumulating forever. Spend cap of `WEALTH_UBI_PAYOUT_FRACTION` per cycle (default 80%) preserves carry-over.

Default brackets (`Config.WEALTH_TAX_BRACKETS`, applied against **net worth**):

| Net worth | Daily rate |
|---|---|
| `<= $50k` | exempt |
| `$50k - $250k` | 0.5%/day |
| `$250k - $1M` | 1.5%/day |
| `$1M - $10M` | 3.0%/day |
| `$10M - $100M` | 6.0%/day |
| `> $100M` | 10.0%/day |

UBI exclusion is `WEALTH_UBI_TOP_EXCLUSION_COUNT = 3` (only the top three holders by NW are excluded; everyone else active in the last `WEALTH_UBI_ACTIVE_DAYS` is in).

Audit and visibility surfaces:

- `,wealth` -- pool, brackets, last cycle.
- `,wealth flow` -- per-cycle `taxed -> UBI -> pool` arrows + top payers/recipients.
- `,wealth top` -- all-time tax-paid and UBI-received leaderboards.
- `,wealth me` -- the calling player's lifetime contribution / receipts.
- `,economy` -> Health tab -- Gini coefficient, top-1/8/25 concentration, P50/P90/P99 percentiles, live throttle curve.

### Whale Yield Throttle (per-tick)

`Config.WEALTH_YIELD_THROTTLE_CURVE` defines a piecewise-linear net-worth -> yield-multiplier curve (default: x1.00 below $50k, x0.70 at $1M, x0.35 at $10M, x0.15 at $100M). Past the last bracket the curve log-decays toward `WEALTH_YIELD_THROTTLE_FLOOR` (default 0.10). The shared `services.wealth_equalizer.yield_multiplier` is consumed by:

| Surface | Where applied |
|---|---|
| `,daily` | base reward, stacked on top of the boost-the-poor wealth-curve scaling |
| `,work` | final reward, after WORK_PROGRESSIVE_TAX |
| Savings interest tick | per-user `interest_delta_rate` before the +1 |
| Validator block rewards | `adjusted_validator_reward` (trimmed amount routes to treasury) |
| Delegator block rewards | per-delegator `payout` (trimmed amount routes to treasury) |
| LP yield (`services/lp_yield.py`) | `payout_usd` per LP position |

Net worth is read once per guild per tick from a 5-minute cache (`services.wealth_equalizer.cached_bulk_net_worth`) and invalidated whenever a tax cycle finishes so the post-tax world is reflected immediately.

### Other levers

- **Progressive work tax** -- Work income above `WORK_PROGRESSIVE_TAX_THRESHOLD` ($5,000) is taxed at 65%, compressing top-tier earnings.
- **Per-job daily caps** -- Each job in `WORK_DAILY_CAP` enforces a daily ceiling on work payouts.

!!! tip
    To dial pressure up: lower the bracket lower-bounds in `WEALTH_TAX_BRACKETS`, raise the rates, or steepen the throttle curve. To dial back: raise the lower-bounds or shift the throttle multipliers up. To kill the system entirely: `WEALTH_EQUALIZER_ENABLED = False` and `WEALTH_YIELD_THROTTLE_ENABLED = False`.

### Game-token closed-loop sinks (Phase 3 of the cycle)

The seven earn-only network coins (REEL, RUNE, HRV, FORGE, BUD, DFUN, GBC) are minted continuously by per-game stake-yield ticks. The shipped sinks (gear, slots, conversions) are fixed-price one-shots, so without a brake circulating supply runs away. Phase 3 of the daily wealth-equalizer cycle calls `services.token_health.game_token_burn_phase`, which:

1. For every token in `Config.GAME_TOKEN_BURN_RATES` with rate > 0:
2. Bulk-loads every holder's CeFi + DeFi balance.
3. Burns `(balance - threshold) * rate` from each holder above the threshold.
4. Decrements `crypto_prices.circulating_supply` by the burned amount so the burn is a real sink, not a transfer.
5. Logs each burn into `wealth_redistribution_log` with `kind = 'token_burn'` so `,wealth flow` and `,wealth top` surface the activity alongside the USD tax + UBI rows.

Defaults (per-cycle, marginal):

| Token | Rate | Exempt below |
|---|---|---|
| `GBC` | 5% | 1,000 |
| `RUNE` | 5% | 50,000 |
| `DFUN` | 3% | 500 |
| `HRV` | 2% | 50,000 |
| `FORGE` | 2% | 50,000 |
| `REEL` | 1% | 50,000 |
| `BUD` | 0% (exempt) | -- |

Tunable via `Config.GAME_TOKEN_BURN_RATES` and `Config.GAME_TOKEN_BURN_THRESHOLDS`. Kill switch: `GAME_TOKEN_BURN_ENABLED = False`.

### Adaptive faucet

Auto-faucet drops auto-scale with the server's per-active-player USD supply. The multiplier is `clamp(REFERENCE / (REFERENCE + per_capita), MIN_MULT, MAX_MULT)` and stacks multiplicatively with the admin `faucet_multiplier` override. Defaults: `FAUCET_ADAPTIVE_REFERENCE_USD = 50_000`, `FAUCET_ADAPTIVE_MIN_MULT = 0.20`, `FAUCET_ADAPTIVE_MAX_MULT = 3.00`. Kill switch: `FAUCET_ADAPTIVE_ENABLED = False`.

### Inflation telemetry

A periodic snapshot task (`cogs/wealth_equalizer.snapshot_task`, default every 6 hours) writes a row to `economy_health_snapshots` with the live distributional metrics. The `,economy` Health tab reads the last 14 days and renders 24h + 7d delta arrows for total supply, Gini, and top-8 concentration. The snapshot loop runs even if `WEALTH_EQUALIZER_ENABLED = False` so operators can run telemetry without redistribution.

---

## Oracle and Price Engine

The price engine uses Geometric Brownian Motion with mean-reversion and regime-based volatility caps:

| Config | Default | Description |
|---|---|---|
| `PRICE_TICK_SECONDS` | `15` | Seconds between price ticks |
| `ORACLE_TWAP_WINDOW` | `80` ticks | Candle history for time-weighted average price |
| `ORACLE_REVERSION_STRENGTH` | `0.02` | 2% pull toward TWAP per tick |
| `ORACLE_DAILY_MAX_DRIFT` | `0.30` | +/-30% circuit breaker from daily open |
| `ORACLE_CAP_NORMAL` | `0.015` | 1.5% max change per tick in normal conditions |
| `ORACLE_CAP_CAUTIOUS` | `0.010` | 1.0% cap when 1--2 standard deviations from mean |
| `ORACLE_CAP_CONTAINMENT` | `0.005` | 0.5% cap when >2 standard deviations out |
| `ORACLE_RECOVERY_CAP` | `0.05` | Upward-only daily drift cap while token is in depeg mode |

Pool oracle rebalancing triggers when the AMM price deviates from the oracle by more than `POOL_ARB_THRESHOLD` (0.5%), with a cooldown of `POOL_ARB_COOLDOWN` (2 minutes) between rebalances.

---

## Depeg Protection

Tokens that trade far below their all-time high (ATH) are subject to extra controls that prevent players from accumulating cheap positions and realising game-breaking gains on a rapid price recovery.

> ⚠️ **Important:** The per-player daily buy cap is tracked in memory and resets on bot restart. A determined player who times purchases around restarts could partially bypass the cap. For stronger enforcement, a future database-backed migration is recommended.

### How it works

1. **ATH tracking**  -  every price update records the all-time high in the `crypto_prices.ath` column.
2. **Depeg detection**  -  a token is considered *depegged* when `current_price < ATH × DEPEG_THRESHOLD`.
3. **Daily buy cap**  -  while a token is depegged, each player may spend at most `DEPEG_DAILY_BUY_USD` (rolling 24-hour window) buying that token. The cap is enforced on both `.buy` and `trade buy`.
4. **Recovery speed limit**  -  the oracle's upward daily circuit breaker is tightened from `ORACLE_DAILY_MAX_DRIFT` to `ORACLE_RECOVERY_CAP` while the token is depegged, so the price cannot moon back to ATH in a single session.

### Settings

| Config | Default | Description |
|---|---|---|
| `DEPEG_THRESHOLD` | `0.30` | Token is depegged when price < 30% of ATH |
| `DEPEG_DAILY_BUY_USD` | `500.0` | Max USD any one user may spend buying a depegged token per 24 hours |
| `ORACLE_RECOVERY_CAP` | `0.05` | Upward daily drift limit while depegged (replaces `ORACLE_DAILY_MAX_DRIFT` on the upside) |

### Effect on gameplay

| Scenario | Without depeg protection | With depeg protection |
|---|---|---|
| SUN drops to $0.012 (1.2% of $1 ATH) | Players can dump all savings to buy SUN | Limited to $500/day per player |
| Price recovers to $1 next day | Early buyers x83 in one session | Recovery capped at +5% per day; gains accumulate slowly over many days |
| Downward price moves | Unrestricted | Unchanged  -  the downward cap stays at ±30% |

---

## Savings and Lending Rates

Discoin uses an Vantor V2-style utilization kink model for savings and lending rates:

| Parameter | Value | Description |
|---|---|---|
| Optimal utilization | 80% | Kink point where slope steepens |
| Base rate | 0.05%/day | Minimum borrow rate |
| Slope 1 | 0.15%/day | Rate at kink = base + slope1 = 0.20%/day |
| Slope 2 | 1.5%/day | Steep slope above kink (up to ~1.7%/day at 100% util) |
| Reserve factor | 15% | Protocol reserve from interest |
| Base savings rate | 0.0165%/day (~6% APY) | Guaranteed floor for passive savers |
| Min deposit | $1.00 | Minimum savings deposit |

---

## AI Features

AI-powered features (market maker decisions, chat, commentary, event narration, work flavor text) can be toggled per-server. All AI config lives under the `,ai` command group -- see [ai-agents.md](ai-agents.md) for the full reference.

```
,ai status                        # view current AI config + flags
,ai toggle mm                     # toggle market maker AI
,ai toggle chat                   # toggle ,ask chat command
,ai toggle commentary             # toggle market commentary
,ai toggle events                 # toggle trade narration
,ai toggle flavor                 # toggle work flavor text
,ai test                          # test current chat provider
,ai prompt chat <text>            # custom system prompt for chat
,ai prompt chat reset             # reset one feature to default
,ai persona "Trader Joe"          # set AI display name
,ai clearhistory                  # clear all AI conversation history
,ai clearhistory @user            # clear for one user
,ai model set chat openrouter:google/gemini-2.5-flash   # pick a model per category
,ai audit                         # recent AI-scope staff actions
```

Requires `OPENROUTER_API_KEY` in your environment (or an Ollama endpoint for local models via `,ai heal` / `,ai model set`).

---

## Market Maker Personas

AI-driven market maker personas trade via webhooks, appearing as distinct characters in your trade channel.

```
.admin mmwebhook create             # create webhook in trade channel
.admin mmwebhook status             # view webhook config
.admin mmwebhook delete             # remove webhook

.admin persona list                 # list all personas
.admin persona create "Bull Mike" bull 🐂
.admin persona setprompt "Bull Mike" "You are an aggressive bull trader who loves leverage."
.admin persona setavatar "Bull Mike" https://example.com/avatar.png
.admin persona settradebias "Bull Mike" bear
.admin persona toggle "Bull Mike"   # enable/disable
.admin persona delete "Bull Mike"
```

Trade bias options: `bull`, `bear`, `neutral`, `random`.
