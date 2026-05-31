# Admin Commands Reference

Complete reference for all `,admin` subcommands. Every command requires the **Manage Server** permission
and must be run inside a guild (not DMs).

Run `,admin` with no arguments to see an interactive paginated reference with dropdown category navigation.

> **Note on prefixes:** All example commands in this document use `,` as the prefix (the default). Servers
> can configure a custom prefix  -  wherever you see `,command`, substitute your server's configured prefix.
> In bot channels (no-prefix mode), omit the prefix entirely.

---

## Currency Management

### give

Add currency or tokens to a user's balance.

```
,admin give @Alice 500
,admin give @Alice 500 USD
,admin give @Bob 10 ARC
```

- Token defaults to `USD` if omitted.
- For non-USD tokens, the amount is added to CeFi holdings and circulating supply is updated.

### take

Remove currency or tokens from a user. Caps at the user's current balance (never goes negative).

```
,admin take @Bob 100
,admin take @Bob 5 ARC
```

### setbal

Set an exact balance for a user, overwriting whatever they have.

```
,admin setbal @Alice 1000 USD
,admin setbal @Alice 0.5 MTA
```

### setprice

Override a token's market price. Updates the price row and adjusts day high/low.

```
,admin setprice ARC 2000
,admin setprice SUN 150.50
```

!!! warning
    This is a hard override. The price engine will continue drifting from the new value on the next tick. Use this for corrections, not permanent pegs.

---

## Token Management

### addtoken

Add a custom token to the server using key=value syntax.

```
,admin addtoken symbol=DOGE name="Dogecoin" emoji=🐕 network="Arcadia Network" type=PoS price=0.08 vol=0.05
```

**Required keys:** `symbol`, `name`, `price`

**Optional keys:**

| Key | Description | Default |
|---|---|---|
| `emoji` | Display emoji | `●` |
| `network` | Network name or `none` for orphan tokens | `none` |
| `type` / `consensus` | `PoW`, `PoS`, or token type (`meme`, `defi`, `governance`, etc.) | `PoS` |
| `vol` | Daily price volatility coefficient (0.0--0.15) | `0.05` |
| `max_supply` | Hard cap on total tokens (0 = unlimited) | `0` |
| `initial_supply` / `supply` | Starting circulating supply | `0` |
| `burn_rate` | Percentage burned on every transfer (0--0.10) | `0` |
| `fee` | Transfer fee percentage (0--0.10) | `0` |

When a token is added to a network that has a stablecoin, a TOKEN/STABLECOIN AMM pool is automatically seeded.

### removetoken

Remove a custom token and wipe its price data.

```
,admin removetoken DOGE
```

### listtokens

List all tokens grouped by network. Shows both built-in and custom tokens (custom tokens are tagged `[CUSTOM]`).

```
,admin listtokens
```

### Token Contracts

View or modify per-token contract rules (transfer fees, burn rates, supply caps).

```
,admin contract ARC
,admin setcontract ARC transfer_fee 0.01
,admin setcontract ARC burn_rate 0.005
,admin setcontract ARC max_supply 1000000
,admin clearcontract ARC
```

Fields for `setcontract`:

| Field | Range | Description |
|---|---|---|
| `transfer_fee` | 0--0.10 (0--10%) | Fee deducted on every transfer |
| `burn_rate` | 0--0.10 (0--10%) | Percentage burned per transfer |
| `max_supply` | 0 = unlimited | Hard cap on total token supply |

---

## Network Management

### addnetwork

Add a custom PoS network for your server.

```
,admin addnetwork "Polygon Network" MATIC 🟣
```

### removenetwork

```
,admin removenetwork "Polygon Network"
```

### listnetworks

Show all built-in and custom networks with their stake tokens.

```
,admin listnetworks
```

---

## Validator Management

### addvalidator

```
,admin addvalidator LIDO "Lido Finance" arc 99.9 4.0 0.5 🔵
```

Arguments: `<ID> <name> <network> <uptime%> <reward%> <slash%> [emoji]`

- Uptime, reward, and slash are entered as percentages (e.g. `99.9` becomes `0.999` internally).
- ID max 20 characters, name max 50 characters.

### removevalidator

Remove a validator without refunding stakes. Use `clearvalidator` if you want refunds.

```
,admin removevalidator LIDO
```

### clearvalidator

Clear all stakes on a validator, refund all holders, then delete the validator.

```
,admin clearvalidator LIDO
```

### updatevalidator

Update a single field on a validator.

```
,admin updatevalidator LIDO reward_rate 0.00011
,admin updatevalidator LIDO uptime_rate 99.5
```

Rate fields (`uptime_rate`, `reward_rate`, `slash_rate`) accept percentages.

### clearstakes

Clear stakes for a specific user or an entire validator. Refunds are applied to holdings.

```
,admin clearstakes @Alice
,admin clearstakes LIDO
```

### recoverstakes

Recover funds from stakes whose validator no longer exists (e.g. after a migration). Refunds each player's orphaned stake to their DeFi wallet.

```
,admin recoverstakes
```

---

## Chain Management

### chain info

View status of all PoW chains (block height, difficulty, hashrate, reward).

```
,admin chain
```

### chain set

Modify a PoW chain configuration value at runtime.

```
,admin chain set SUN warmup_blocks 10
,admin chain set MTA solo_share_cap 0.8
```

Valid keys: `warmup_blocks`, `solo_share_cap`, `initial_difficulty`, `initial_reward`, `electricity_rate`, `electricity_scaling`, `target_block_time`, `max_group_share`.

### chain reset

Reset a single chain to block 0. Resets difficulty, block height, and circulating supply tracking. Player balances are **not** affected.

```
,admin chain reset SUN
```

!!! danger
    This deletes all mined block history for the chain. Use `,admin backup create` first.

### chain resetall

Reset ALL PoW chains to block 0 and recalculate circulating supply from actual player holdings.

```
,admin chain resetall
```

---

## Supply Management

### supply check

View circulating supply vs max supply for all tokens (or a specific one).

```
,admin supply check
,admin supply check SUN
```

### supply reset

Reset circulating supply for a token AND wipe all player balances of that token. Supply resets to 50% of max_supply.

```
,admin supply reset SUN
```

!!! danger
    This wipes all player CeFi holdings, DeFi holdings, and stakes of the specified token. Cannot be undone.

### supply recalculate

Recalculate circulating supply from actual player holdings for all tokens. Does not change any balances -- only fixes the supply tracker.

```
,admin supply recalculate
```

---

## Pool Management

### removepool

Delete a liquidity pool entirely.

```
,admin removepool ARC USD
```

### rebalancepool

Rebalance a pool's reserves to imply a new price while preserving the constant product (k).

```
,admin rebalancepool ARC USD 2000
```

---

## Block Management

### blockstatus

View the latest chain block for every network: block number, PoS/PoW status, transaction count, hash, and timestamp.

```
,admin blockstatus
```

### bundle

Force-seal chain blocks on all networks immediately. Normally blocks auto-seal every 30 minutes when there are pending transactions.

```
,admin bundle
```

### reject

Reject a pending mempool action by ID. Refunds locked tokens but not gas fees.

```
,admin reject 42
```

---

## Reports

Reports are sent to the admin's DMs with interactive buttons (Accept, Reject, In Progress, Resolve, Close, Reward, Message Reporter) and a tag selector dropdown.

### Viewing reports

```
,admin reports                       # all reports
,admin reports bugs                  # filter by category
,admin reports open                  # filter by status
,admin reports bugs open             # filter by both
,admin reports search @user          # reports by a specific user
,admin reports search 42             # view report #42
```

Categories: `bugs`, `suggestions`, `users`, `other`

Statuses: `open`, `accepted`, `in_progress`, `resolved`, `closed`, `rejected`

### Managing reports

```
,admin reports delete 42             # delete a specific report
,admin reports clear                 # delete ALL reports (with confirmation)
,admin reports clear bugs            # delete all bug reports
,admin reports clear bugs resolved   # delete resolved bug reports
,admin reports export                # export all reports as CSV
,admin reports export bugs open      # export filtered reports
```

---

## Backup

### backup create

Trigger a manual database backup.

```
,admin backup create
```

### backup list

List all existing backups.

```
,admin backup list
```

### backup restore

Restore from a backup file. The bot will restart after a successful restore.

```
,admin backup restore discoin_2024-01-15.sql
```

!!! danger
    Only use restore in emergencies. This overwrites the current database and restarts the bot.

---

## Reset Commands

### resetuser

Wipe all economy data for a single user. Requires confirmation.

```
,admin resetuser @Alice
```

### resetserver

Wipe ALL server data -- balances, crypto, stakes, loans, pools, mining, everything. Requires confirmation. Default pools and prices are reseeded after the wipe.

```
,admin resetserver
```

!!! danger
    This cannot be undone. Create a backup first with `,admin backup create`.

### reseteconomy

Wipe all user data (balances, holdings, stakes, items, loans, mining rigs, savings, validators, custom tokens, networks, and user settings) but **keep** pools and prices intact.

```
,admin reseteconomy
```

---

## Session Log

Export the session debug log with a parsed summary. Only available in debug mode (`DEBUG=TRUE`).

```
,admin log
```

The output includes:

- Session start time and uptime
- Command usage statistics and top users
- Error log with tracebacks
- Discord rate limit warnings
- Blockchain activity (validator blocks, chain bundles, mempool, mining)
- Event breakdown by category (economy, trading, staking, pools, contracts)

---

## Gas & Validator Commands

### `,gas [network]`

Shows current gas fees, mempool depth, and a tier recommendation for each network (or a
specific network if provided).

**Examples:**

- `,gas`  -  all networks
- `,gas arc`  -  Arcadia Network only

**Output includes:** base fee, low/medium/high tier costs for send and swap, mempool depth,
recommendation (Low/Medium/High), and a cross-reference to `,chain mine status` for PoW
mining costs.

Also note: use `,admin setchannel validators #channel` to set the channel where validator
block confirmations and slash events are posted.

---

## Diagnostics

### health

Run a full server health diagnostic.

```
,admin health
```

### diag

Unified diagnostics group combining health, logs, errors, and reports.

```
,admin diag health
,admin diag log
,admin diag errors summary
,admin diag reports bugs open
```

---

## Developer Commands (`,dev`)

Developer-only commands restricted to `REPORT_TARGET_USER_ID`. These provide deep system diagnostics and maintenance tools.

### Diagnostics

| Command | Description |
|---|---|
| `,dev status` | Run comprehensive diagnostics (6+ pages), results sent via DM |
| `,dev heartbeat` | Show all background task loop heartbeat timestamps |
| `,dev check <system>` | Check an individual system |
| `,dev config` | View/set dev settings (auto-DM interval, etc.) |

**Available systems for `,dev check`:** `events`, `mining`, `staking`, `validators`, `prices`, `savings`, `lending`, `security`, `faucet`, `chains`, `pools`, `errors`

### Maintenance (formerly admin-only)

| Command | Description |
|---|---|
| `,dev log` | Session log export + parsed activity summary |
| `,dev errors` | Error tracker  -  `summary`, `cmds`, `bot`, `export`, `clear` |
| `,dev diag` | Diagnostics router  -  `health`, `errors`, `reports` |
| `,dev dbcleanup [days]` | Purge old DB rows (transactions, game results, candles) |

### Configuration

| Command | Description |
|---|---|
| `,dev config interval <hours>` | Set auto status DM interval (default: 4h, min: 0.25h) |
| `,dev config dm on\|off` | Toggle auto-DM reports |
| `,dev config channel on\|off` | Toggle channel error posting |

The bot automatically DMs the developer a status summary every 4 hours (configurable). This includes task loop health, diagnostic results, and any stale/failed background tasks.

---

## Player Status (`,status`)

Any player can run `,status` to see live service health across 3 pages:

**Page 1 -- Overview:**

- Discord connection latency and uptime
- Server stats (users, tokens, pools)
- Latest trade activity
- Token prices with daily % change (top 6)

**Page 2 -- System Health:**

- Background service indicators (green/yellow/red based on last heartbeat):
  - Price Engine, PoW Mining, PoS Staking, Chain Blocks, Savings Interest, Loan Interest, Security Monitor, Validator Rewards, PoS Validators, Faucet Drops
- Market Events status (active event with timer, idle, or module disabled)

**Page 3 -- Economy Snapshot:**

- PoW chains (block height, difficulty, hashrate per chain)
- Validator counts and total staked (PoW + PoS)
- Liquidity pool TVL
- Events config (module status, frequency, disabled count)

No sensitive information is exposed.

---

## Economy Dashboard (`,economy`)

Server-wide economy stats with category navigation. Any player can run this.

```
,economy
,econ
,stats
,serverstats
```

Categories shown:

- **Money** -- Total cash in circulation, wallet/bank split, active loans
- **Trading** -- 24h volume, trade count, top movers by daily % change
- **Pools** -- All liquidity pools with TVL per pair and total TVL
- **Mining** -- Network hashrate, miner count, chain block heights and difficulty
- **Staking** -- PoW and PoS validator counts, total staked value
- **Gambling** -- 24h games played and total wagered

---

## Trade History (`,trade history`)

View your recent trades. Supports filtering by type.

```
,trade history
,trade history buy
,trade history sell
,trade history swap
```

Shows: trade type, amounts, tokens, gas fees, and how long ago each trade happened.

---

## Leaderboard (`,leaderboard`)

Aliases: `,lb`, `,top`

```
,lb                     Net worth (default)
,lb ARC                 Holdings of specific token
,lb hashrate            Mining hashrate
,lb trading             Realized trading P&L
,lb gambling            Net gambling profit
,lb staking             Total staked value
```

---

## Gambling Stats (`,play stats`)

Aliases: `,gambstats`, `,gstats`

```
,play stats
,play stats @user
```

Shows per-game breakdowns (W/L, win rate, wagered, P&L, best win/worst loss), current and best win/loss streaks, and the 5 most recent games with results.

---

## Bot Channel (No-Prefix Mode)

Designate channels where players type commands without any prefix  -  just `work`, `buy 10 arc`, `help`, etc.

```
,admin botchannel #bot-commands      Toggle no-prefix mode for a channel
,admin botchannel                    List current bot channels
```

- Run the command again on the same channel to remove it.
- In bot channels, prefixed commands (like `,work`) are silently ignored. The bot hints: "No prefix needed in this channel."
- Normal conversation that doesn't match a command name is silently ignored.
- All commands work the same way  -  just without the prefix.

---

## Item Shop

Players buy leveled gems and consumables from the shop using **DSD** (Disdollar) or **USDC**  -  stablecoins pegged to $1. All prices are in USD-equivalent stablecoin.

```
,shop                     Browse all available items
,buy <item>               Purchase an item
,sell <item>              Sell a leveled item back (minus sell fee)
,items                    View your owned items and their levels
```

### Available Items

| Item | Emoji | Cost (DSD) | Type | Levels up via |
|---|---|---|---|---|
| Hashstone | ⛏️ | 3,750 | Leveled gem | PoW mining (any chain) |
| Lockstone | 🔒 | 3,000 | Leveled gem | Staking & PoS validator activity |
| Vaultstone | 🏦 | 2,500 | Leveled gem | Savings & lending activity |
| Liqstone | 🌊 | 4,000 | Leveled gem | Providing liquidity (LP) |
| Validator Guard | 🛡️ | 900 | Consumable | Auto-absorbs one slash penalty |
| Yield Guard | 🔐 | 750 | Consumable | Auto-absorbs one savings/lending loss |

All leveled gems cap at **level 100**. Stats scale linearly with level (e.g. a Hashstone at level 50 grants +12% hashrate and +15% work/daily earnings). Buy and sell fees are 4--6% to the guild treasury.

Items marked **disabled** (Gambastone, Charm, Gambling Save) are not purchasable even if listed.

---

## Eat the Rich

> **Prefix-only commands**  -  these commands do not have slash command equivalents.

Eat the Rich is a class-warfare wealth game. There is **no opt-in**  -  everyone is fair game. The game is built so you only ever *want* to punch **up**: both your success odds and your payout scale with how much richer the target is. The poorest player still active in the server (active = used any economy command in the last 30 days) is fully uneatable.

A successful eat removes a **gross** slice of the target's liquid wealth -- gap-scaled, and large enough to matter against a leaderboard of billionaires. The gross is then split four ways by the tactic you pick. The stake shown on each tactic button is collateral: it is **returned in full on a win**, so a successful eat can never leave the attacker poorer. On a loss the attacker forfeits **30% of the stake**, half of which goes to the target.

A 2-minute cooldown applies between eat attempts; a mistargeted or cancelled attempt refunds it.

### `,eat @user`

Aliases: `,eattherich`, `,rob`, `,devour`

Pick a tactic from the button menu. Each button is labelled with its exact stake. The tactic decides how the gross steal is split between your cut, a burn, the salad bowl, and an airdrop to the poorest active players:

| Tactic | Stake | Base odds | Keep | Burn | Salad bowl | Airdrop |
|---|---|---|---|---|---|---|
| Type 1 -- Skim | 1% of wallet (min $50) | 60% | 10% | 0% | 90% | 0% |
| Type 2 -- Shakedown | 3% of wallet (min $150) | 58% | 20% | 5% | 50% | 25% |
| Type 3 -- Guillotine | 5% of wallet (min $300) | 55% | 50% | 25% | 25% | 0% |

```
,eat @Alice
,rob @Alice
,devour @Alice
```

- A wealth-gap bonus of up to **+20%** is added on top of the base success chance.
- If the target has an active security detail, success chance is reduced by 75% -- unless the attacker has cased the joint with `,eat prep`, which walks straight past it.
- Punching **down** (a target who is not richer than you) is allowed but the anti-whale rule halves your odds, and a loss costs a flat **class traitor fee** on top of the stake penalty. The poorest active player cannot be targeted at all.

### `,eat bite @user [wallet|crypto|defi|bank|stakes]`

Aliases: `,eat pool`, `,eat snatch`

A precision strike on one named balance pool. The overall net-worth gate and the same Type 1/2/3 tactic buttons apply, but the gross comes only from the chosen pool and is split + paid in that pool's asset  -  USD for `wallet` / `bank`, tokens for `crypto` / `defi`. `stakes` cannot be bitten: staked wealth is always safe. If the chosen pool holds under $100 the attempt fails gracefully, refunds the cooldown, and charges no stake.

```
,eat bite @Alice crypto
,eat bite @Alice bank
```

### `,eat prep` -> `,eat cook`

The two-stage powerup chain. Each command spends a fee, then **charges** for about 5 minutes before the powerup is *armed*. An armed powerup is consumed by your next eat, bite, or salad.

- **`,eat prep`** (alias `,eat case`) -- case the joint. Your next eat reveals the target's full holdings and walks straight past any security detail.
- **`,eat cook`** (aliases `,eat books`, `,eat scheme`) -- cook the books. Requires an armed prep first. Your next eat is uncapped and the slice that would burn lands in your own cut instead. Cook is also the key that unlocks `,eat rich`.

### `,eat salad`

Aliases: `,eat bowl`, `,eat saladbowl`

Shows the **salad bowl** -- a shared, multi-currency pot that fills with a slice of every eat. The embed lists every currency in the bowl and carries an "Eat the Bowl" button that triggers the `,eat rich` gamble.

### `,eat rich`

Alias: `,eat eatbowl`

A 1% gamble on the whole salad bowl. Requires an armed cook (and consumes both prep and cook). Win: take 5% of every currency in the bowl while the other 95% burns forever. Loss: 5% of the bowl burns forever.

### `,eat defend`

Aliases: `,eat fortify`, `,eat bunker`, `,eat shield`, plus the standalone `,fortify` / `,bunker` / `,defend`

Hire a private security detail that reduces the odds of anyone eating you  -  plain eat or targeted bite  -  by 75% for **2 hours**. Costs **$500** from your wallet. There is a **4-hour cooldown** between hires.

```
,eat defend
,fortify
```

### `,eat stats [@user]`

Aliases: `,eat record`, `,eatstats`, `,richstats`, `,classwar`

View Eat the Rich statistics for yourself or another player: eats attempted/won, win rate, total wealth devoured, times hunted, times survived, total lost, and net.

```
,eat stats
,eat stats @Alice
```

### `,eat history`

Aliases: `,eat menu`, `,eat recent`, `,eathistory`, `,themenu`

View the 10 most recent eats in the server, showing eater, target, tactic or bitten pool, and outcome (devoured / got away / security blocked).

```
,eat history
```

### `,eat lb`

The wealth-devoured leaderboard  -  the same board as `,lb eat`.

---

## NFT Management

Create and manage NFT collections.

```
,admin nft create <symbol> <name> <network> <mint_price> <mint_token> [max_supply]
```

**Example:** `,admin nft create PUNKS "Discoin Punks" ARC 0.05 ARC 100`

- Network must be ARC or DSC (PoS only)
- Mint price is what players pay per mint (in mint_token)
- Max supply is optional  -  omit for unlimited

```
,admin nft setimage <symbol> <url>      Set the collection's image URL
,admin nft delete <symbol>              Delete a collection (irreversible)
```

!!! note "Player deployment"
    Protocol Dev and Exploiter tier players can also deploy collections via `,nft deploy`  -  they pay gas but don't need admin permissions.

---

## Prediction Markets

Create and manage prediction markets.

```
,admin predict create <question>         Create a new prediction market
,admin predict resolve <id> <YES|NO>     Resolve a market with the outcome
,admin predict cancel <id>               Cancel a market (refunds all bets)
,admin predict list                      List all markets (including resolved)
```

---

## Market Events

Full admin control over the market events system.

### Core Commands

```
,admin event status                      View current event, settings, and disabled list
,admin event trigger <type>              Trigger a market event (e.g. bull_run, bear_market)
,admin event clear                       End the current event early
,admin event list                        List all event types with disabled badges
```

### Disable / Enable Individual Events

Block specific events from triggering randomly. Disabled events can still be triggered manually.

```
,admin event disable <type>              Block event from random triggers
,admin event enable <type>               Re-enable for random triggers
,admin event disable all                 Disable all events from random triggers
,admin event enable all                  Re-enable all events
```

Example: `,admin event disable black_swan` to prevent catastrophic market crashes.

### Frequency Control

Adjust how often random events trigger per price tick.

```
,admin event frequency                   View current setting + presets
,admin event frequency <preset|value>    Set frequency
```

**Presets:**

| Preset | Probability | Approx. Interval |
|--------|------------|-------------------|
| `off` | 0 | Never (manual only) |
| `low` | 0.0002 | ~5 hours |
| `default` | 0.0005 | ~2 hours |
| `high` | 0.001 | ~1 hour |
| `max` | 0.005 | ~12 minutes (chaos) |

Custom values accepted (e.g. `,admin event frequency 0.001`). Max: 0.01.

**Event types:** bull_run, bear_market, fed_rate_hike, fed_rate_cut, black_swan, whale_pump, rug_pull, pandemic, regulation, adoption, etf_approved, exchange_hack

Events modify price volatility and add directional bias. They auto-expire after their duration.

---

## Module Toggles

Enable or disable features for your server.

```
,admin module <name> <on|off>            Toggle a module
```

**Modules:** gambling, lending, staking, mining, faucet, drops, savings, validators, pools, contracts, groups, chart, crypto, daily, work, economy, chain, shop, games, ape, nft, predictions, events

---

## Feed Channels

Set dedicated channels for activity feeds.

```
,admin setchannel <type|category|all> #channel
```

**Individual feeds:** trade, mine, staking, validators, gambling, pools, crypto, drops, dropsspawn, faucet, job, contracts, wallet, error, whale, reports, nft, predictions, events, ape

**Categories:**
- `economy`  -  trade, crypto, pools, wallet, whale, contracts
- `earning`  -  mine, staking, job, validators
- `fun`  -  gambling, drops, dropsspawn, faucet, ape
- `bot`  -  error, reports, events
- `collectibles`  -  nft, predictions
- `all`  -  every feed

---

## Announcements & DMs

```
,admin announce <message>                Send an announcement embed to the current channel
,admin dm @user <message>                Send a DM to a player from the bot
```

## Beta Features (`,admin beta`)

Beta features are opt-in system modules. Server admins always have access; other users need an explicit grant.

```
,admin beta features                         List all available beta features
,admin beta list                             Show current grants for this server
,admin beta grant <feature> @user/@role      Grant access to a feature
,admin beta revoke <feature> @user/@role     Revoke access
,admin beta clear <feature>                  Remove all grants for a feature
```

### Available features

| Feature | Description |
|---|---|
| `command_chains` | Multi-command chains (`&&`, `>`, `;`, `\|\|`, `\|`, `+`) |
| `internal_commands` | Internal bot commands (`bot <cmd>`, `/discoin`) |
| `auto_compound` | Auto-restake staking rewards into the same farm |
| `price_alerts` | DM notifications when tokens hit price targets |
| `gm_commands` | Game Helper (`,gm`) command group  -  see [Game Helpers](#game-helpers-gm) below |

## Game Helpers (`,gm`)

The Game Helper system is a **beta feature** (`gm_commands`). Enable it with:

```
,admin beta grant gm_commands @YourHelperRole
```

Once enabled, designated helpers can use `,gm` commands to assist players without needing full admin access. Server admins always have access regardless of grants.

### Adding helpers

```
,admin helpers add @user     Make a player a Game Helper
,admin helpers remove @user  Remove Game Helper status
,admin helpers list          List all current helpers
```

### What helpers can do

| Command | Description |
|---|---|
| `,gm lookup @user` | View any player's balance and profile (read-only) |
| `,gm stakes @user` | View a player's staking positions |
| `,gm cooldown @user` | Reset a player's command cooldowns |
| `,gm reports` | View recent bug reports |
| `,gm announce <message>` | Send a game announcement to the current channel |
| `,gm log` | View the helper audit log |

All helper actions are logged to the audit trail.

### What helpers cannot do

- Edit balances, tokens, or economy settings
- Grant or revoke permissions
- Access admin configuration
- Create or delete tokens, validators, or pools
