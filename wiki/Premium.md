# Premium

Discoin runs as a **single shared bot** that any server can invite. There is no
self-hosting and no separate instance per community. Instead, the cost-heavy and
compute-heavy features are gated behind a per-guild Premium subscription, so each
server decides for itself whether to unlock them.

This page explains exactly what is free everywhere, what Premium unlocks, and
how a server admin turns Premium on.

> Command examples use the `,` prefix. The prefix is configurable per server,
> so yours may differ.

## How it works

Premium is granted at the **server (guild) level**, not the player level. When a
server has Premium, every member of that server gets the unlocked features.
There is no per-user upgrade.

A server can become Premium in three ways:

1. **PayPal subscription** - a server admin subscribes; once payment clears, a
   signature-verified webhook flips Premium on for the whole server.
2. **Host server auto-unlock** - the operator's configured host server (and any
   declared developer servers) are always unlocked with no payment and no
   database row.
3. **Owner grant** - the bot owner can manually grant Premium to any server.

## Free everywhere

These work on every server with no subscription:

- Trading and the token economy
- Gambling
- Bank, wallet, and profile
- Work, daily, and jobs
- Drops and the faucet
- Basic buddy management: hatch, rename, storage, the BUD economy, leaderboard
- The shop and staking

## Premium only

These require an active Premium subscription (or the host/dev unlock):

- **AI** - `,ask`, `,disco`, AI replies and mentions, DiscoAI, agents, plugins,
  web search
- **Fishing** - casts, baits, the fish market (the Lure Network)
- **Farming** - plots, crops, seasons, the HRV and SEED economy (the Harvest
  Network)
- **Crafting** - the forge, specialties, applying crafted gear (the Forge
  Network)
- **Delves** - dungeon runs, classes, room loot (the Crypt Network)
- **Expeditions** - buddy treks for resources
- **Buddy battles and arena** - the buddy battle ladder
- **Buddy breeding** - the nest, daycare, egg deposit/withdraw/hatch
- **Buddy market** - the buddy auction house, listings, gifting, the egg market
- **Buddy AI chat** - `,buddy talk`

The [Activities](Activities) page covers the fishing, farming, crafting, delve,
and expedition systems in detail.

## Premium commands

| Command | Who can run it | What it does |
|---|---|---|
| `,premium` / `,premium status` | Anyone | Show this server's current Premium tier |
| `,premium info` (aliases `,plans`, `,pricing`) | Anyone | Show plans, prices, and what is free vs paid |
| `,premium features` (alias `,what`) | Anyone | List every Premium feature with a description |
| `,premium subscribe [monthly\|yearly]` | Server admin | Generate a PayPal approval link |
| `,premium cancel` (alias `,unsubscribe`) | Server admin | Cancel a PayPal subscription |

## Subscribing as a server admin

Subscribing requires the **Manage Server** permission. The flow:

1. Run `,premium info` to see the available plans and prices.
2. Run `,premium subscribe` for the monthly plan, or `,premium subscribe yearly`
   for the annual plan.
3. The bot replies with a single-use PayPal approval link (it expires within a
   few hours). Click it and approve the subscription in PayPal.
4. Once PayPal confirms payment, a verified webhook activates Premium for your
   server automatically. Every member is unlocked immediately - no further
   action needed.

To stop billing, an admin runs `,premium cancel`. If a Discoin instance does not
have PayPal configured, `,premium subscribe` will say so; on those instances
Premium can only be granted by the bot owner.

## Host server and owner grants

The operator's **host server** is auto-unlocked: every Premium feature is
available there with no subscription and no database record. The same treatment
applies to any servers declared as developer servers, so the operator can run
staging and personal communities without paying themselves.

The **bot owner** can also grant Premium to any server manually using the admin
command group (`,admin premium grant <guild_id> [days]`). This is handy for
comps, partners, or troubleshooting. Owner grants and PayPal subscriptions are
recorded so a server's Premium source is always clear in `,premium status`.

## See also

- [Activities](Activities) - the Premium-gated fishing, farming, crafting, delve, and expedition systems
- [Buddies](Buddies) - buddy battles, breeding, and the buddy marketplace
- [Server Administration](Server-Administration) - admin commands, module toggles, and owner tools
- [FAQ](FAQ) - common questions about access and unlocking features
