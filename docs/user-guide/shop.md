# Items & Shop

The Discoin shop sells special items that provide passive bonuses. Items are purchased by staking a **stablecoin** (DSD or USDC) — the stablecoin is locked inside the item and returned (minus sell fee) when you sell it.

## Browsing the Shop

View all available items:

```
.shop list
```

Alias: `.shop`

## Buying Items

Buy an item using any accepted stablecoin:

```
.shop buy <item> [currency]
```

Examples:

```
.shop buy hashstone DSD
.shop buy hashstone USDC
.shop buy hashstone        (defaults to DSD)
```

The stablecoin is **staked** (locked) inside the item — not burned. A buy fee (3-6% depending on item) goes to the guild treasury.

## Selling Items

Sell an item to recover your staked stablecoin (refunded as DSD):

```
.shop sell <item>
```

A sell fee (4-6%) is deducted from the returned amount and sent to the guild treasury.

## Transferring Items

Send an item to another player:

```
.shop transfer <item> @user
```

Transfers cost a flat stablecoin gas fee (varies by item), paid from your DSD wallet.

## Leveled Items (Stones)

Stones are the core items in Discoin. They level from 1 to 100 by earning XP through gameplay. Each level multiplies the stone's stat bonuses.

### Leveling curve

XP required for level N to N+1 = N × base_xp. Early levels are fast; later levels require significantly more XP. Total XP to max level ranges from ~250,000 to ~400,000 depending on the stone.

### Leveling up

When your stone has enough XP, pay any stablecoin to claim the next level:

```
.inventory levelup <item> [currency]
```

Aliases: `.inventory lvlup`, `.inventory upgrade`

**Level-up cost** = 10% of the staked stablecoin value per level. Cost scales up as your stone gets more valuable. Pay with any accepted stablecoin (DSD, USDC).

---

## Hashstone

*"A crystallized fragment forged from raw hashpower."*

The Hashstone levels up as you mine on any PoW network, proportional to your hashrate share.

| Property | Value |
|---|---|
| Cost | 7,500 stablecoin (staked) |
| Buy/Sell Fee | 5% |
| Transfer Fee | 150 stablecoin |
| Max Level | 100 |
| XP Source | Mining (35 XP per block share) |

### Stats (per level)

| Stat | Per Level | At Level 100 |
|---|---|---|
| Work/Daily Bonus | +0.3% | +30% |
| Mining Bonus | +0.24% | +24% |

!!! tip "Best for miners"
    If you spend most of your time mining, the Hashstone is your best investment. The mining bonus increases your effective hashrate, and the work/daily bonus stacks with other stone bonuses.

---

## Lockstone

*"A shard forged from staking pressure."*

The Lockstone levels up through staking activity and PoS validator block processing.

| Property | Value |
|---|---|
| Cost | 6,000 stablecoin (staked) |
| Buy/Sell Fee | 5% |
| Transfer Fee | 120 stablecoin |
| Max Level | 100 |
| XP Source | Staking rewards (30 XP) and validator blocks (35 XP) |

### Stats (per level)

| Stat | Per Level | At Level 100 |
|---|---|---|
| Work/Daily Bonus | +0.24% | +24% |
| Stake Bonus | +0.3% | +30% |

!!! tip "Best for stakers"
    If you delegate tokens to validators or run your own validator, the Lockstone amplifies your staking income while also boosting your work/daily earnings.

---

## Vaultstone

*"A gem crystallized from compounding interest."*

The Vaultstone levels up passively through savings interest and lending activity.

| Property | Value |
|---|---|
| Cost | 5,000 stablecoin (staked) |
| Buy/Sell Fee | 4% |
| Transfer Fee | 100 stablecoin |
| Max Level | 100 |
| XP Source | Savings interest (40 XP per interest tick) |

### Stats (per level)

| Stat | Per Level | At Level 100 |
|---|---|---|
| Work/Daily Bonus | +0.2% | +20% |
| Interest Bonus | +0.36% | +36% |

!!! tip "Best for savers"
    The Vaultstone is the cheapest stone and has the fastest leveling curve. Since savings interest is passive, this stone levels itself while you do other things.

---

## Liqstone

*"A prism refracting liquidity flows."*

The Liqstone levels up by providing liquidity to pools. XP accrues hourly based on LP value × hold time.

| Property | Value |
|---|---|
| Cost | 8,000 stablecoin (staked) |
| Buy/Sell Fee | 5% |
| Transfer Fee | 150 stablecoin |
| Max Level | 100 |
| XP Source | LP activity (value × hold time) |

---

## Stacking Stone Bonuses

You can own one of each stone type simultaneously. Their `work_daily_bonus` stats stack additively:

| Stones Owned (all at Lv 100) | Combined Work/Daily Bonus |
|---|---|
| Hashstone only | +30% |
| Hashstone + Lockstone | +54% |
| All three stones | +74% |

Other stats (mining, staking, interest) do not overlap — each stone specializes in its own area.

---

## Consumable Items

Consumable items are single-use and stack in your inventory. They activate automatically when their trigger condition is met.

### Validator Guard

Protects against validator slashing. When your validator would be penalized, one guard is auto-consumed to absorb the slash.

| Property | Value |
|---|---|
| Cost | 450 stablecoin |
| Buy Fee | 3% |
| Max Stack | 50 |

### Yield Guard

Protects your savings principal. If a borrower defaults and the savings pool takes a loss, one guard absorbs the hit.

| Property | Value |
|---|---|
| Cost | 400 stablecoin |
| Buy Fee | 3% |
| Max Stack | 50 |

### Using consumables

Consumables activate automatically — you do not need to manually use them. Buy them and they sit in your inventory until needed.

View your inventory:

```
.inventory
```

---

## Disabled Items

The following items exist in the configuration but are currently **disabled** and cannot be purchased:

- **Gambastone** (10,000 stablecoin) — reduces house edge in gambling games
- **Charm** (~438 stablecoin) — timed 45-minute consumable buff reducing gambling house edge

These may be re-enabled in a future update.

---

## Acquiring Stablecoins for the Shop

All shop items accept any **stablecoin** ($1 peg): **DSD** (Disdollar) or **USDC** (USD Coin).

Ways to acquire stablecoins:
1. **Buy DSD directly**: `.trade buy DSD 500`
2. **Buy USDC directly**: `.trade buy USDC 500`
3. **Swap** any token for a stablecoin: `.trade swap ARC USDC 1`
4. **Earn DSD** passively from Discoin Network savings interest

Both DSD and USDC are worth $1 and interchangeable at the shop.
