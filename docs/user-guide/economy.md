# Economy Basics

This page covers how money works in Discoin: earning, storing, and transferring USD.

## Wallet vs Bank

Every player has two USD balances:

- **Wallet** -- your spending money. Used to buy tokens, gamble, and pay fees.
- **Bank** -- your savings account. Safer storage; also used as collateral for loans.

You can move money between them freely.

## Checking Your Balance

```
.balance
```

Aliases: `.bal`, `.me`, `.wealth`, `.networth`, `.p`

Shows your wallet balance, bank balance, token holdings, net worth, current job, and active streaks all in one view.

## Deposit and Withdraw

Move USD between your wallet and bank:

```
.bank deposit <amount>
.bank withdraw <amount>
```

Use `all` as the amount to move your entire balance:

```
.bank deposit all
.bank withdraw all
```

Aliases: `.deposit`, `.withdraw` (shortcuts that skip the `bank` prefix).

## Transferring USD

Send USD from your wallet to another player:

```
.bank transfer @user <amount>
```

Aliases: `.transfer`, `.give`, `.pay`

Example:

```
.bank transfer @Lleywyn 500
```

!!! warning "Wallet balance only"
    Transfers send from your **wallet**, not your bank. Make sure you have enough in your wallet before sending.

## Moving Tokens Between Storage

The `.bank move` command lets you move any token between your wallet, bank, CeFi holdings, and DeFi wallet:

```
.bank move <amount> <token> <from> <to>
```

## Daily Rewards

Claim a daily reward once every 24 hours:

```
.earn daily
```

- Base reward: **$500**
- Streak bonus: **+$10 per consecutive day**, up to a 365-day streak
- At max streak, your daily reward is **$4,150** before bonuses
- Missing **two consecutive days** resets your streak to zero

!!! tip "Streak protection"
    You have a 48-hour grace window. As long as you claim within 48 hours of your last daily, your streak stays alive. The cooldown is still 24 hours -- you just don't lose your streak immediately.

### Streak perks  -  work cooldown reduction

Your daily streak also reduces the time between `.work` uses. The reduction is tiered and stacks on top of your job's base cooldown:

| Streak | Reduction | Effect on 15-min default |
|--------|-----------|--------------------------|
| 1 - 6    | none      | 15:00 |
| 7 - 13   | 5%        | 14:15 |
| 14 - 29  | 10%       | 13:30 |
| 30 - 59  | 15%       | 12:45 |
| 60 - 89  | 20%       | 12:00 |
| 90 - 179 | 25%       | 11:15 |
| 180+   | 30%       | 10:30 |

The reduction applies proportionally to every job's cooldown (not just the 15-minute default). Maximum reduction is capped at 30%  -  enough to feel meaningful without breaking the economy. When you're on cooldown, the bot shows your current streak bonus in the reply.

### Wealth-curve scaling

Daily rewards are multiplied by a **net-worth-aware curve** before payout. Below the server's median net worth you earn up to **2x**; the wealthiest log-decay toward a **x0.10** floor. The same curve also gates `,work`, savings interest, validator + delegator block rewards, and LP yield -- one tunable controls every passive surface.

Run `.help wealth` for the full curve and the daily wealth tax + UBI loop that pairs with it. `.economy` -> **Health** tab shows where you sit on the curve right now alongside Gini coefficient, top-1/8/25 concentration, and the redistribution pool state.

## Working

Work to earn USD with a cooldown between sessions:

```
.earn work
```

- Pay range depends on your current job tier
- Default cooldown: **15 minutes** (higher job tiers have longer cooldowns)
- Your **daily streak reduces this cooldown**  -  see [Streak perks](#streak-perks-work-cooldown-reduction) above
- ~10% chance of an interactive risk/reward prompt: take the safe payout or gamble for 2x (50/50)

### Stone bonuses

If you own a Hashstone, Lockstone, or Vaultstone, their `work_daily_bonus` stat multiplies your work and daily earnings. At max level, stones can add up to +30% earnings.

## Ape (Degen Mode)

Ape into a random shitcoin. Like buying low-cap gems on-chain.

```
.ape
```

Aliases: `.degen`, `.yolo`, `.earn ape`

### Job-Scaled Cost

The entry cost scales with your job tier -- whales risk more, newcomers risk less. Payouts scale proportionally.

| Job Tier | Entry Cost | Moon Payout (7%) | Legendary (1%) |
|----------|-----------|------------------|----------------|
| Homeless | $20 | $800 - $3.2K | $3.2K - $6K |
| Whitelist Farmer | $50 | $2K - $8K | $8K - $15K |
| Discord Mod | $200 | $8K - $32K | $32K - $60K |
| Trader | $800 | $32K - $128K | $128K - $240K |
| Protocol Dev | $6,000 | $240K - $960K | $960K - $1.8M |
| Exploiter | $12,500 | $500K - $2M | $2M - $3.75M |

| Outcome | Chance | Payout |
|---------|--------|--------|
| Rugged | 80.00% | $0 (lose entry cost) |
| Break Even | 11.99% | 0.8-2x entry back |
| Moon | 7.00% | 10-25x entry |
| Legendary | 1.00% | 25-50x entry |
| Ascended | 0.01% | 80-100x entry |

A confirmation button with full odds preview is shown before entry. You have 30 seconds to decide.

Cooldown: ~2.5 minutes. Win payouts go to your wallet. Big wins (moon+) are posted to the ape feed channel and DM'd to you if you have `$notify ape on`.

## Market Events

Random economic events affect all token prices on the server. Check the current event:

```
.event
```

View all possible event types:

```
.event list
```

Events modify price **volatility** (how wildly prices swing) and add **directional bias** (push prices up or down). They trigger randomly (~once every 2 hours) or can be started by admins.

| Event | Volatility | Direction | Duration |
|-------|-----------|-----------|----------|
| Bull Run | 0.5x (calmer) | Bullish | 30 min |
| Bear Market | 1.5x | Bearish | 30 min |
| Fed Rate Hike | 2.0x | Bearish | 15 min |
| Fed Rate Cut | 0.3x (calm) | Bullish | 20 min |
| Black Swan | 4.0x (extreme) | Very Bearish | 10 min |
| Whale Pump | 2.0x | Very Bullish | 5 min |
| Rug Pull | 3.0x | Bearish | 10 min |
| Global Pandemic | 2.5x | Bearish | 45 min |
| New Regulation | 1.5x | Bearish | 20 min |
| Mass Adoption | 0.5x | Bullish | 30 min |
| ETF Approved | 0.8x | Bullish | 20 min |
| Exchange Hack | 3.5x | Very Bearish | 15 min |

Events auto-expire after their duration. Only one event can be active at a time.

Admins can control events with `.admin event` -- disable specific events, adjust frequency, or turn them off entirely. See the [admin guide](../admin-guide/commands.md) for details.

## Jobs

Jobs determine your earning range, mining rig slots, and unlock perks. View the full ladder:

```
.earn jobs
```

Check your current job:

```
.earn job
```

Promote when you qualify:

```
.earn promote
```

### Job ladder

| Job | Min Works | Min Net Worth | Earn Range | Rig Slots |
|---|---|---|---|---|
| Homeless | 0 | $0 | $5 - $20 | 2 |
| Airdrop Farmer | 5 | $500 | $15 - $50 | 4 |
| Larper | 15 | $2,000 | $30 - $100 | 6 |
| Whitelist Farmer | 30 | $8,000 | $60 - $200 | 8 |
| Shitcoin Trencher | 60 | $25,000 | $100 - $400 | 12 |
| Discord Mod | 100 | $75,000 | $200 - $700 | 16 |
| DeFi Degen | 175 | $200,000 | $400 - $1,200 | 24 |
| Trader | 275 | $600,000 | $700 - $2,000 | 32 |
| Course Seller | 400 | $2,000,000 | $1,000 - $3,500 | 48 |
| Validator Operator | 550 | $7,500,000 | $1,500 - $5,000 | 64 |
| Protocol Dev | 750 | $25,000,000 | $2,500 - $8,000 | 96 |
| Exploiter | 1,000 | $100,000,000 | $4,000 - $12,000 | 128 |

### Job perks

Higher-tier jobs unlock cumulative perks:

- **Daily bonus** -- multiplier on daily reward (up to +50%)
- **Swap fee rebate** -- reduced trading fees on swaps (down to 0% at Exploiter)
- **Stake bonus** -- multiplier on staking rewards (up to +30%)
- **Mining bonus** -- multiplier on mining hashrate (up to +30%)
- **Interest bonus** -- multiplier on savings APY (up to +30%)
- **Can deploy validator** -- unlocked at Validator Operator
- **Can deploy token** -- unlocked at Protocol Dev
- **Can create pool** -- unlocked at Exploiter

## Income Caps and Scaling

### Daily work cap

Each job tier has a daily income cap to prevent runaway inflation from excessive grinding. If you hit your cap for the day, work earnings are reduced until the next reset.

| Job | Daily Cap |
|---|---|
| Homeless | $2,500 |
| Airdrop Farmer | $6,000 |
| Shitcoin Trencher | $70,000 |
| Exploiter | $650,000 |

### Progressive tax

Work earnings above **$5,000** in a single session are taxed at **65%**. This primarily affects top-tier jobs where a single work session can pay $4,000-$12,000.

### Wealth-curve scaling

Both `.work` and `.daily` apply the shared **whale yield throttle** to your final payout: a piecewise-linear curve on your net worth that ramps from full pay at **$50k** to **x0.15** at **$100M** and log-decays toward a **x0.10** floor past that. The same curve gates savings interest, validator/delegator block rewards, and LP yield, so a high net worth player earns at the same fraction across every income surface.

Why this exists: without a flow-side brake, the top of the leaderboard would compound yields faster than anyone else can catch up. The throttle pairs with the daily wealth tax + UBI loop (run `.help wealth`) -- the tax drains the principal, the throttle slows the regrowth.

!!! tip "See where you sit"
    `.wealth` shows the bracket schedule and your last cycle's tax / UBI history. `.economy` -> **Health** tab shows your bracket on the throttle curve along with the server's Gini coefficient and top-1/8/25 concentration.

## Gas Fees & Mempool

When active PoS validators exist on a network, transactions are routed through a
mempool and processed in validator blocks. This is Discoin's on-chain layer.

### Gas tiers

Each transaction specifies a gas tier:

| Tier   | Priority Fee | When to use |
|--------|-------------|-------------|
| low    | +5%         | Mempool depth < 10 |
| medium | +20%        | Default     |
| high   | +50%        | Busy mempool (depth > 30) |

Use `.gas [network]` to check current base fees, mempool depth, and a recommendation.

### The economic loop

```
Validators earn gas (90%)
    → 10% funds protocol treasury
        → treasury funds Protocol Node yields
            → yields attract delegators
                → more liquidity
                    → more transactions
                        → more gas
```

### Base fee adjustment (EIP-1559)

After each block, the base fee adjusts:

- Block > 50% full → base fee increases by 12.5%
- Block < 50% full → base fee decreases (bounded by network minimum)
