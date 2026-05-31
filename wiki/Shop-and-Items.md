# Shop and Items

The Item Shop sells gear that makes you stronger across the whole bot. The headline items are the leveled **Stones** -- gems you stake a balance into and level up over time for a permanent, scaling bonus. The shop also stocks single-use **consumables** that protect you from losses.

> The command prefix shown here is `,`. Server admins can change the prefix, so your server may use a different character.

## Browsing and buying

| Command | What it does |
|---|---|
| `,shop` | Browse every item with your ownership status |
| `,shop buy <item> [currency]` | Acquire an item by staking a balance |
| `,shop sell <item>` | Sell a stone back, refunding the stake minus a sell fee |
| `,shop transfer <item> @user` | Peer-to-peer transfer of an item (gas fee applies) |
| `,inventory` / `,inv` | View your items: level, XP bar, staked amount, bonuses |
| `,inventory levelup <item> [currency]` | Pay to claim a level once the XP threshold is met |
| `,inventory use <item>` | Activate a consumable |

## Stones: leveled gear

A Stone is a leveled item. When you buy one, the purchase cost is **staked, not burned** -- it is locked into the stone and refunded (minus a 5% sell fee) if you ever sell. Each stone earns XP from one specific activity, levels from 1 up to 100, and grants a permanent stat bonus that scales with its level.

### How the stake / level / XP model works

- **Staked amount:** the balance locked into the stone. It starts at the purchase cost and grows every time you level up, because level-up costs are added to it. Selling refunds the full staked amount minus the sell fee.
- **Level:** every stone goes from 1 to 100. Your stat bonus is `per-level value x current level`.
- **XP:** earned passively by doing the stone's activity. The XP needed for level N to N+1 is `N x xp_per_level_base`, so each level costs more than the last.
- **Level-up:** once you have enough XP, claim the level by paying a stablecoin cost. `,inventory` shows an up-arrow when a stone is ready.

### Stone types

There are four core stones plus five themed minigame stones plus three meta stones. Every stone caps at level 100, is transferable, and also carries a small `+work/daily` bonus.

| Stone | Cost | Paid in | Levels up via | Headline bonus (per level / max) |
|---|---|---|---|---|
| Hashstone | ~$7,500 | MTA or SUN | Mining | +0.24% hashrate (max +24%), +0.30% work/daily |
| Lockstone | ~$6,000 | DSC or ARC | Staking and validating | +0.30% node yield (max +30%), +0.24% work/daily |
| Vaultstone | $5,000 | USD | Savings interest | +0.36% interest (max +36%), +0.20% work/daily |
| Liqstone | $8,000 | DSD or USDC | Holding LP value | -0.10% swap fee, +0.50% LP fee share, +0.15% work/daily |
| Tidestone | ~$6,500 | REEL | `,fish` casts | +0.30% fish payout, +0.15% fish combo |
| Heartstone | ~$5,500 | BUD | Buddy chats, feeds, level-ups | +0.30% buddy XP, +0.40% mood decay resist |
| Cryptstone | ~$7,000 | RUNE | Dungeon kills, captures, mining, bosses | +0.30% ore qty, +0.20% dungeon ATK, +0.15% capture chance |
| Bloodstone | ~$7,500 | BBT | Buddy battle rounds and wins | +0.25% battle ATK, +0.20% battle HP, +0.30% USD prize |
| Bloomstone | ~$6,500 | HRV | `,farm` plant, harvest, process, pest kills | +0.30% crop yield, +0.30% SEED drop |
| Gavelstone | $8,000 | USD | Auction House buys and settled sales | +0.20% buyer rebate, +0.20% seller bonus |
| Anvilstone | ~$7,500 | FORGE | `,craft` actions | +0.30% craft output, +0.20% craft skill XP |
| Chimerastone | $7,000 | USD | `,swap` actions | +0.10% extra swap fee discount |

When a stone accepts more than one currency, the first one listed is used if you do not specify. Costs shown as "~$" are USD-equivalent targets converted to the chosen token at the live oracle price at purchase time.

### What the core stones do

- **Hashstone** boosts your mining hashrate. The more hashrate you have, the larger your share of every block, so a high-level Hashstone compounds your mining income. See [Mining](Mining).
- **Lockstone** boosts validator and yield-farming returns. It earns XP from both yield-farming ticks and validating blocks. See [Staking-and-Validators](Staking-and-Validators).
- **Vaultstone** boosts your savings interest rate. It is the cheapest and fastest stone to level because savings is passive. See [DeFi](DeFi).
- **Liqstone** is liquidity-provider gear. It lowers your swap fees and raises your share of LP trading fees. It needs a 1 hour minimum hold before XP accrues, which discourages churn. See [DeFi](DeFi).

The five themed minigame stones tie into the bot's [Activities](Activities), and the meta stones (Gavelstone, Anvilstone, Chimerastone) reward economy actions. Every stone also adds a small flat boost to `,work` and `,daily`, so owning a full set lifts your baseline income.

### Levelling up

```
,inventory levelup hashstone        claim a level, paying in the stored currency
,autolevelup on                     auto-claim levels the moment XP and funds are ready
```

- **Level-up cost** is 5% of your current staked amount per level. That cost is added to the staked total, which raises your sell value.
- **Auto-levelup** (`,autolevelup on`) claims levels for you as soon as both the XP and the funds are available. It tries the stone's stored currency first, then walks every other accepted currency, and both CeFi and DeFi balances count toward funds.
- Toggle level-up DM notifications with `,notify itemlevelup on/off`.

### Fees

| Fee | Amount | Where it goes |
|---|---|---|
| Buy fee | 5% of cost (4% on Vaultstone) | Guild treasury |
| Sell fee | 5% of staked amount (4% on Vaultstone) | Guild treasury, refunded in DSD |
| Level-up cost | 5% of current staked amount per level | Added to your staked total |
| Transfer gas | Flat $100 - $160 (per stone, in DSD) | Network fee |

Selling a stone returns the staked amount (your initial stake plus every level-up cost you paid) minus the sell fee.

## Consumables

Consumables are stackable single-use items. Two of them are pure insurance and trigger automatically the moment you would otherwise take a loss.

| Consumable | Cost | Max stack | Effect |
|---|---|---|---|
| Validator Guard | $450 | 50 | Absorbs one validator slash event |
| Yield Guard | $400 | 50 | Absorbs one savings or lending loss |

- **Validator Guard** protects your validator. If your node would be slashed for downtime or a double-sign, one guard is consumed instead and the penalty is cancelled.
- **Yield Guard** protects your savings principal. If a lending loss or pool haircut would cut into your deposit, one guard absorbs the hit.

Both are auto-consumed when the matching event happens, so there is nothing to activate. They cannot be sold or transferred -- buy several to build up a stack of protection. Use `,inventory use <item>` for consumables that need to be triggered manually.

## See also

- [Mining](Mining)
- [Staking-and-Validators](Staking-and-Validators)
- [DeFi](DeFi)
- [Progression](Progression)
