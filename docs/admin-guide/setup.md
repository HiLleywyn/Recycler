# Server Setup

This guide walks you through initial Discoin configuration for your Discord server. All admin commands require the **Manage Server** permission.

---

## First Steps

Once Discoin joins your server it creates a default economy with built-in tokens, pools, and pricing. There is no explicit setup wizard -- the bot is immediately usable. Your first tasks are setting a prefix, pointing feed channels, and toggling modules you do not need.

View all current settings at any time:

```
.admin settings
```

This shows a paginated overview of prefix, currency name, embed color, feed channels, module toggles, AI flags, halted networks, and disabled tokens.

---

## Setting the Prefix

The default prefix comes from the `PREFIX` environment variable (default `$`). Each server can override it:

```
.admin setprefix !
```

- Maximum 5 characters.
- After changing, all commands use the new prefix (e.g. `!balance`).
- The bot always responds to mentions as a fallback, so you cannot lock yourself out.

!!! tip
    Pick a prefix that does not collide with other bots in your server. Single-character prefixes like `.` or `!` are fast to type.

---

## Server Identity

Customize how Discoin presents itself in your server:

```
.admin setname Moonbase Economy
.admin setcurrencyname Credits
.admin setcolor #FF6B00
```

| Command | What it does | Limit |
|---|---|---|
| `.admin setname <name>` | Display name shown in help embeds | -- |
| `.admin setcurrencyname <name>` | Rename the base currency label (default "USD") | 20 chars |
| `.admin setcolor <#hex>` | Accent color for all embeds | Valid hex |

---

## Configuring Channels

Discoin can post activity feeds to dedicated channels. Without channel configuration, feeds are simply not posted -- commands still work anywhere.

### Setting a single feed

```
.admin setchannel trade #market-feed
.admin setchannel gambling #casino
.admin setchannel whale #whale-alerts
```

### Setting all feeds to one channel

```
.admin setchannel all #bot-feed
```

This points every feed type to the same channel in one command.

### Available feed types

| Type | Description |
|---|---|
| `trade` | Buy/sell/swap execution feed |
| `mine` | Mining block events |
| `staking` | Staking reward/slash events |
| `validators` | Validator block confirmations |
| `gambling` | Game results |
| `pools` | LP add/remove events |
| `crypto` | Price movement alerts |
| `drops` | Drop claimed events log |
| `dropsspawn` | Where drops appear for users to claim |
| `job` | Job and career feed |
| `contracts` | Smart contract event feed |
| `wallet` | DeFi wallet events |
| `error` | Bot error log |
| `whale` | Whale alert feed |
| `reports` | Report status updates |

!!! tip
    Channels support both text channels and threads (including forum posts). Paste the thread ID or use a channel mention.

---

## Enabling and Disabling Modules

Toggle entire feature sets on or off per server. Disabled modules reject all related commands.

```
.admin module gambling off
.admin module mining on
```

### Available modules

| Module | Controls |
|---|---|
| `gambling` | All gambling games (coinflip, slots, crash, etc.) |
| `lending` | Loan system |
| `staking` | Staking and delegation |
| `mining` | PoW mining |
| `drops` | Automatic and manual drops |
| `savings` | Savings deposits and interest |
| `validators` | Validator registration and blocks |
| `pools` | AMM liquidity pools |
| `contracts` | Smart contract deployment and calls |
| `groups` | Mining groups |
| `chart` | Price chart generation |
| `crypto` | Crypto trading (buy/sell/swap) |
| `daily` | Daily claim command |
| `work` | Work command |
| `economy` | Core economy (balance, transfer, etc.) |
| `chain` | Chain block system |

State values accepted: `on`, `off`, `enable`, `disable`, `1`, `0`, `true`, `false`.

!!! warning
    Disabling `economy` shuts down nearly everything. Only do this if you are performing maintenance.

---

## Halting Networks and Tokens

For emergencies or maintenance you can halt an entire network or disable a single token without turning off full modules.

### Halt a network

```
.admin halt network sol on
```

All buy/sell/swap/stake/contract actions on that network are rejected until resumed:

```
.admin halt network sol off
```

Valid networks: `arc`, `sol`, `bnb`, `sun`, `mta`, `avax`, `pol`, `atom`, `sui`, `apt`, `near`.

### Disable a token

```
.admin halt token ARC on
.admin halt token ARC off
```

### View active halts

```
.admin halt
```

---

## Reports Feed

Configure which report categories are posted to your reports channel:

```
.admin setchannel reports #mod-reports
.admin reportsfeed bugs,suggestions
.admin reportsfeed all
```

Without arguments, `.admin reportsfeed` shows the current configuration. Valid categories: `bugs`, `suggestions`, `users`, `other`.

---

## Permissions

Restrict specific commands to certain roles. Members without an allowed role are blocked; admins with Manage Server are always exempt.

```
.admin perm add gamble @Adults
.admin perm remove gamble @Adults
.admin perm clear gamble
.admin perm
```

| Subcommand | Effect |
|---|---|
| `add <command> @role` | Only that role (and admins) can use the command |
| `remove <command> @role` | Remove the role from the restriction list |
| `clear <command>` | Remove all restrictions, open to everyone |
| *(no subcommand)* | List all current restrictions |

!!! tip
    You can stack multiple roles on one command. A user needs at least one of the allowed roles to proceed.

---

## What to Configure First

A recommended setup checklist:

1. **Set your prefix** -- `.admin setprefix .`
2. **Create feed channels** -- at minimum `trade`, `whale`, and `drops`
3. **Disable unused modules** -- if your server does not need lending or contracts, turn them off
4. **Set a whale threshold** -- `.admin whalethreshold 10000` to alert on large transactions
5. **Enable scam detection** -- `.admin scam on` (requires `OPENROUTER_API_KEY`)
6. **Configure permissions** -- lock gambling behind an age-verified role if needed
