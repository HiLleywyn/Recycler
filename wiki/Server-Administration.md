# Server Administration

This page is for server admins who want to add Discoin to their Discord
server and configure it. It covers inviting the bot, the permissions and
intents it needs, initial setup, feed channels, AI scam detection, the
economy security monitor, and a pointer to the common `,admin` commands.

If you are hosting or contributing to the bot itself (Docker, environment
variables, the codebase), see the [Developer Guide](Developer-Guide).

All admin commands require the **Manage Server** permission. The command
prefix is configurable per server with `,admin setprefix` -- examples on
this page use a comma (`,`) prefix.

## Inviting the bot

Discoin runs as a single shared bot that any server can invite. Trading,
gambling, banking, jobs, and basic features are free everywhere; cost-heavy
features are gated behind a per-guild premium subscription (see
[Premium](Premium)).

Invite the bot using an OAuth2 URL with these scopes:

- `bot`
- `applications.commands`

### Required intents

In the Discord Developer Portal, under **Bot > Privileged Gateway Intents**,
enable all three:

| Intent | Why it is needed |
|---|---|
| Presence Intent | Bot activity status |
| Server Members Intent | Member cache for scam timeouts and guild-aware features |
| Message Content Intent | Reading message text for commands and scam detection |

### Recommended bot permissions

| Permission | Why it is needed |
|---|---|
| View Channels | Read channels to post into them |
| Send Messages | Post command responses and feeds |
| Send Messages in Threads | Post into thread-based feed channels |
| Embed Links | Rich embed responses |
| Attach Files | Chart image uploads |
| Read Message History | Context for AI replies and fuzzy matching |
| Add Reactions | Drop claim buttons and interactive UIs |
| Use External Emojis | Token and network emojis |
| Manage Messages | Delete scam messages (auto-moderation) |
| Moderate Members | Time out users flagged by AI scam detection |
| Manage Webhooks | Market-maker bot webhook for the trade feed |

## First-time setup

Once the bot is in your server, run:

```
,admin setup
```

This initializes the guild settings. Discoin creates a default economy with
built-in tokens, pools, and pricing automatically, so the bot is usable
right away. View all current settings at any time:

```
,admin settings
```

This shows a paginated overview of the prefix, currency name, embed color,
feed channels, module toggles, halted networks, and disabled tokens.

### Server identity (optional)

| Command | What it does |
|---|---|
| `,admin setprefix <prefix>` | Change the command prefix (max 5 characters) |
| `,admin setname <name>` | Display name shown in help embeds |
| `,admin setcurrencyname <name>` | Rename the base currency label (default USD) |
| `,admin setcolor <#hex>` | Accent color for embeds |

## Feed channels

Discoin can post activity feeds to dedicated channels. Without channel
configuration, feeds are simply not posted -- commands still work anywhere.
Each feed type can point to any text channel or thread (including forum
posts; paste the post ID for those).

Set a single feed:

```
,admin setchannel trade #market-feed
,admin setchannel gambling #casino
```

Or point every feed to one channel:

```
,admin setchannel all #bot-feed
```

### Feed types

| Type | Description |
|---|---|
| `trade` | Buy/sell/swap execution feed |
| `mine` | Mining block events |
| `staking` | Staking reward and slash events |
| `validators` | Validator block confirmations |
| `gambling` | Game results |
| `pools` | LP add/remove events |
| `crypto` | Price movement alerts |
| `drops` | Drop claimed events log |
| `dropsspawn` | Where drop messages appear for users to claim |
| `faucet` | Where the auto-faucet drops appear |
| `job` | Job and career feed |
| `contracts` | Smart contract event feed |
| `wallet` | DeFi wallet event feed |
| `error` | Bot error log feed |
| `whale` | Whale alerts feed |
| `reports` | Reports feed |
| `nft` | NFT activity feed |
| `predictions` | Prediction markets feed |
| `events` | Market events feed |
| `ape` | Ape / degen feed |
| `vault` | Vault level-up feed |
| `grouphall` | Group Hall parent channel |
| `income` | Silent chat income channel |
| `changelog` | Daily changelog auto-post channel |

You can also set whole categories at once: `economy`, `earning`, `fun`,
`bot`, and `collectibles` (for example, `,admin setchannel economy #channel`).

## AI scam detection

Discoin's scam detection uses AI to classify messages that contain URLs.
It requires the bot operator to have set `OPENROUTER_API_KEY` (see the
[Developer Guide](Developer-Guide)).

How it works: every message is checked for URLs with a fast regex gate
(`discord.gg` invites are excluded). Messages with URLs go to the AI
classifier, which returns a scam/not-scam verdict from the full message
context. If a message is flagged, it is deleted, the user is timed out,
a reply is posted, and mods are alerted. Users with the **Manage Messages**
permission are never flagged.

Configure it with the `,admin security scam` commands:

```
,admin security scam               scam settings overview
,admin security scam on            enable scam detection
,admin security scam off           disable scam detection
,admin security scam channel #ch   set the mod alert channel
,admin security scam timeout 60    time out scammers for 60 minutes
,admin security scam timeout 0     no timeout (delete and alert only)
,admin security scam notify @mod   toggle a mod on DM alerts
,admin security scam log [n]       recent scam log
```

The timeout duration ranges from 0 to 10,080 minutes (0 = off, max 7 days).
A moderate value (30 to 60 minutes) is a sensible starting point.

## Economy security monitor

The economy security monitor is a passive background system that scans
transaction patterns and alerts admins. It never mutes, times out, or
penalizes players -- it only observes and reports. It runs automatically
on all servers and needs no configuration.

It periodically scans recent ledger activity and flags patterns such as
income velocity, gambling velocity, wash trading, transfer rings, LP churn,
whale concentration, and transaction floods. When something suspicious is
detected, an AI-generated summary is sent as a DM to the configured report
target. A per-user cooldown prevents alert spam.

The bot also includes structural anti-abuse protections that are always on:
atomic balance operations, pool circuit breakers, LP locks, swap volume
caps, and an anti-bot CAPTCHA system for gambling. These are not
configurable.

## Common admin commands

The `,admin` group covers a wide range of staff actions. Run `,admin help`
for the full interactive reference. Frequently used commands:

| Command | Purpose |
|---|---|
| `,admin give <@user> <amount> [token]` | Credit a player |
| `,admin take <@user> <amount> [token]` | Debit a player |
| `,admin setbal <@user> <amount>` | Set a player's balance |
| `,admin setjob <@user> <tier>` | Set a player's job tier |
| `,admin setprice <SYM> <price>` | Set a token price |
| `,admin module <name> <on/off>` | Enable or disable a feature module |
| `,admin halt network <net> <on/off>` | Halt or resume a whole network |
| `,admin halt token <SYM> <on/off>` | Disable or re-enable a single token |
| `,admin event trigger/clear/status` | Manage market events |
| `,admin perm add/remove/clear <command> @role` | Restrict commands to roles |
| `,admin grouptoken enable/disable <SYM>` | Control group token trading |
| `,admin whalethreshold <usd>` | Set the whale alert threshold |
| `,admin reportsfeed <categories>` | Choose which report categories post to the feed |
| `,admin premium grant <guild_id> [days]` | Grant premium (bot owner only) |

### Important: prefix-only command groups

The `admin` and `gm` command groups are **prefix-only**. They must be
invoked with the bot prefix (for example `,admin`, `,gm`) and are never
available as slash commands. This is intentional -- it keeps staff tooling
out of the slash command surface.

## See also

- [Developer Guide](Developer-Guide)
- [Getting Started](Getting-Started)
- [Premium](Premium)
- [Commands](Commands)
