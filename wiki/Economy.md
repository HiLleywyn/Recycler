# Economy

The economy is the backbone of Discoin: where your USD lives, how you
earn it, and how the bot keeps a healthy balance between new players and
whales. This page covers wallets and banking, daily rewards and streaks,
the job ladder, the leaderboard, the Wealth Bottleneck, and the chain
explorer.

All examples use a comma `,` prefix. The prefix is configurable per
server - run `/help` to see your server's current prefix.

## Wallet vs bank

Every player has two USD balances:

- **Wallet** - your spending money. Used to buy tokens, gamble, pay
  fees, and send transfers.
- **Bank** - safer storage. Move money here when you do not need it
  spendable.

You move USD between them freely. Note that transfers to other players
send from your **wallet**, not your bank.

## Checking your balance

```
,balance              summary view
,balance crypto       crypto holdings with PnL %
,balance mining       rigs and hashrate
,balance network arc  Arcadia only
```

`,balance` (alias `,bal`) shows a paginated view of your net worth.
Flags let you focus on one slice:

| Flag | Shows |
|---|---|
| `crypto` | Crypto holdings with profit/loss percentage |
| `staking` / `nodes` | Staking and validator positions |
| `mining` | Rigs and hashrate |
| `network <net>` | Filter to one network (`arc`, `sun`, `mta`, `dsc`) |

`,balance` is also available as a slash command, `/balance`, in
read-only form.

## Banking commands

| Command | Aliases | Effect |
|---|---|---|
| `,deposit <amount\|all>` | `,dep` | Wallet to bank |
| `,withdraw <amount\|all>` | `,with` | Bank to wallet |
| `,transfer @user <amount>` | `,give`, `,pay` | Send USD to another player |
| `,move <amount\|all> <token> <from> <to>` | `,mv` | Move any token between storage |
| `,move everything <from> <to>` | | Move all assets between storage at once |

### Storage codes

`,move` uses short storage codes: `cash` / `c`, `bank` / `b`,
`wallet` / `w`, and `vault` / `v`.

```
,move 100 USD cash bank   wallet to bank
,move all USD bank cash   bank to wallet
,move 50 USD cash vault   into savings vault
,move 1 ARC bank wallet   CeFi to DeFi wallet (platform fee applies)
,move 1 ARC wallet bank   DeFi to CeFi (free)
,move everything b w      all assets bank to wallet at once
```

Moving a token from CeFi (bank/cash) into a DeFi wallet charges a small
platform fee, deducted automatically. Moving back from DeFi to CeFi is
free.

## CeFi holdings vs on-chain DeFi wallets

Tokens you `,buy` go into your **CeFi** portfolio - simple, custodied
balances. To send tokens peer-to-peer or use on-chain features, you
create a **DeFi wallet**, a network-scoped address you control.

```
,wallet create arc        create an Arcadia DeFi wallet
,wallet list              list your wallets
,send @Alice 5 arc        send native ARC on-chain
,send @Bob 10 arc USDC    send USDC on Arcadia
```

CeFi and DeFi are covered in depth on the [Trading](Trading) page. For
the economy, just remember: CeFi is custodied, DeFi is on-chain, and
both count toward your net worth.

## Daily rewards and streaks

`,daily` claims a reward once every 24 hours. Claiming on consecutive
days builds a **streak**, and each streak day adds a bonus on top of the
base reward:

```
Reward = BASE + (streak - 1) x STREAK_BONUS
```

The streak resets if you miss more than 48 hours since your last claim,
so there is a small grace window beyond the 24-hour cooldown. The
maximum streak is 365 days.

### Streak-driven work cooldown reduction

Your daily streak also shortens the cooldown on `,work`. The reduction
is tiered and stacks on top of your job's base cooldown:

| Streak | Work cooldown reduction |
|---|---|
| 1 - 6 days | none |
| 7+ days | 5% faster |
| 14+ days | 10% faster |
| 30+ days | 15% faster |
| 60+ days | 20% faster |
| 90+ days | 25% faster |
| 180+ days | 30% faster |

The reduction applies proportionally to any job's cooldown, and is
capped at 30%. When you are on cooldown, the bot shows your current
streak bonus in the reply.

## Work, jobs, and promotions

`,work` earns job pay on a cooldown. Pay scales with your job tier, and
there is a roughly 10% chance of an interactive risk prompt: take the
safe payout, or gamble for 2x on a 50/50.

`,job` shows your current title, pay, and perks. `,jobs` (or
`,job list`) shows every tier. `,promote` advances you when you qualify
- promotions require both a minimum number of completed work shifts and
a minimum net worth.

### The job ladder

Discoin has 24 job tiers, from Homeless up to Satoshi. Each tier raises
your pay per shift, your mining rig slots, and unlocks perks. A sample:

| Job | Notes |
|---|---|
| Homeless | Starter tier, lowest pay, 2 rig slots |
| Airdrop Farmer | Early promotion, more rig slots |
| Whitelist Farmer | Daily-reward bonus perk kicks in |
| Discord Mod | Reduced swap fees |
| DeFi Degen | Staking bonus unlocks; longer work cooldown |
| Trader | Mining bonus unlocks |
| Protocol Dev | Can deploy NFT collections / tokens |
| Exploiter | Can create custom AMM pools, 128 rig slots |

Run `,jobs` for the full ladder with exact pay ranges and requirements.

### Job perks

Higher tiers unlock cumulative perks:

- `daily_bonus` - percentage multiplier on daily rewards
- `swap_fee` - reduced swap fee rate
- `stake_bonus` - multiplier on staking rewards
- `mining_bonus` - multiplier on mining hashrate
- `interest_bonus` - multiplier on savings APY
- `rig_slots` - maximum number of mining rigs
- `can_deploy_token` - deploy NFT collections, unlocks at Protocol Dev
- `can_create_pool` - create custom AMM pools, unlocks at Exploiter

### Income caps

Each job tier has a daily work income cap to prevent runaway grinding.
If you hit your cap for the day, work earnings are reduced until the
next reset.

## The Wealth Bottleneck

Discoin keeps the economy healthy with the **Wealth Bottleneck**, a
rank-based throttle on fresh income. It replaced the older daily wealth
tax and yield throttle.

The key idea: **your existing holdings are never drained**. Your stones,
bags, rigs, NFTs, savings, validator stakes, delegations, LP positions,
and game-token stakes are permanently off-limits. Instead, every fresh
USD-equivalent gain you earn is multiplied by a curve based on your rank
on the wealth leaderboard:

```
  0% (poorest)   x1.50   +50% boost
 25% (lower half)x1.20   +20% boost
 50% (median)    x1.00   neutral
 75% (top 25%)   x0.85   -15% drag
 90% (top 10%)   x0.55   -45% drag
 99% (top 1%)    x0.20   -80% drag
100% (richest)   x0.10   -90% drag
```

- **Drag** - if you are above the median, a slice of each gain comes off
  the top and flows into a per-guild **community pool**.
- **Boost** - if you are below the median, you get the full gain plus a
  top-up from that community pool (capped at 100% of the gain). When the
  pool is empty, the boost falls to zero until it refills.

This acts as the game's UBI: the wealth tax used to drain principal;
now the bottleneck just slows regrowth at the top and accelerates it at
the bottom. The multiplier applies to `,work`, `,beg`, `,ape`, `,daily`,
`,faucet`, drops, realized trade profit, and stake / LP / mining /
savings yield. It only activates once a guild has at least 5 ranked
holders.

Check your standing with `,bottleneck` (aliases `,wealth`, `,bn`):

```
,bottleneck            your rank, multiplier, recent flow
,bottleneck curve      the full multiplier curve
,bottleneck pool       community-pool snapshot + 24h flow
,bottleneck me         your last 14 days of drag/boost
,bottleneck recent     last 25 events across the guild
```

The `,economy` Health tab shows the live curve and pool. Every income
embed footer shows the multiplier that was applied, so nothing happens
silently.

## Leaderboard

`,leaderboard` (aliases `,lb`, `,top`) ranks the top 50 players by net
worth. Categories let you rank by other metrics:

```
,lb               net worth ranking
,lb trading       rank by realized P&L
,lb gambling      rank by net gambling profit
,lb work          rank by shifts completed
,lb hashrate      rank by mining power
,lb lp            rank by LP pool value
,lb staking       rank by staked value
,lb streaks       longest daily streaks
,lb rugpull       time holding King / Queen of Rugs
,lb eat           net wealth devoured in Eat the Rich
,lb token <SYM>   rank by holdings of a token
```

`,leaderboard` is also available read-only as `/leaderboard`.

## The chain explorer

Every trade, transfer, and on-chain action produces a transaction hash
and is recorded on a ledger. Chain blocks are produced roughly every 30
minutes.

```
,chain block arc         latest ARC block
,chain block 5 arc       block #5 on Arcadia
,chain tx arc:abc123...  look up a transaction by hash
```

Mining blocks (proof of work) and chain blocks (the ledger) are separate
systems. Chain blocks start as Pending and become Mined once a PoW miner
confirms them. Transaction hash prefixes are `arc:`, `dsc:`, `sun:`,
`mta:`, and `usd:`.

## See also

- [Getting Started](Getting-Started)
- [Trading](Trading)
- [Progression](Progression)
- [Commands](Commands)
