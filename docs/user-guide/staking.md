# Staking & Validators

Staking lets you earn passive income by delegating tokens to validators on PoS networks. Discoin also supports a player-driven validator system where you can register your own validator and process blocks.

## Networks and Stake Tokens

Only PoS networks support staking:

| Network | Stake Token |
|---|---|
| **Arcadia Network** | ARC |
| **Discoin Network** | DSC |

SUN and MTA are PoW tokens -- they cannot be staked, only mined.

## Yield Farm Staking

The simplest way to stake. Deposit tokens into a yield farm and earn rewards over time:

```
.stake farm <validator_id> <amount>
```

Example:

```
.stake farm LIDO 100
```

This stakes 100 ARC with the Lido Finance validator.

### Unstaking

Withdraw your staked tokens:

```
.stake unstake <validator_id> <amount>
```

!!! warning "Early unstake penalty"
    Unstaking within **48 hours** of your initial stake incurs a **5% penalty** that is burned. After 48 hours, you can unstake freely.

### Viewing your stakes

See all your active stake positions:

```
.stake mine
```

Aliases: `.stake mystakes`, `.stake staking`, `.stake staked`

## Validators

Validators are the entities that process transactions on PoS networks. Each validator has different risk/reward profiles.

### Listing validators

View all available validators:

```
.stake list
```

Aliases: `.stake validators`, `.stake valis`

### Validator properties

Each validator has:

- **Uptime rate** -- how reliably it stays online (affects reward consistency)
- **Reward rate** -- daily reward percentage paid to stakers
- **Slash rate** -- daily penalty when the validator goes offline

### Pre-built validators

#### Arcadia Network

| Validator | Risk | Reward Rate | Uptime |
|---|---|---|---|
| Coinbase Prime | Ultra-safe | 0.50%/day | 99.8% |
| Lido Finance | Safe | 0.60%/day | 99.5% |
| Rocket Pool | Moderate | 0.90%/day | 97.5% |
| StakeWise | Moderate-high | 1.30%/day | 93.0% |
| EigenLayer | High risk | 1.80%/day | 88.0% |

#### Discoin Network

| Validator | Risk | Reward Rate | Uptime |
|---|---|---|---|
| Discoin Core | Safe | 0.70%/day | 99.7% |
| Dis Validator | Balanced | 1.00%/day | 97.0% |
| Yield Engine | Higher risk | 1.40%/day | 92.0% |
| DSD Reserve | High risk | 1.90%/day | 88.0% |

#### Sun Network (Mining Pools)

| Pool | Risk | Reward Rate | Uptime |
|---|---|---|---|
| SunPool Prime | Safe | 0.80%/day | 99.7% |
| Solar Hashworks | Moderate | 1.20%/day | 97.5% |
| Nova Hash Pool | High risk | 1.30%/day | 92.0% |

### How rewards work

Rewards are calculated hourly:

```
hourly_reward = stake_amount * reward_rate / divisor / 24 * warmup_factor * (1 + bonus)
```

- **Warmup**: New stakes ramp to full rewards over 12 hours (linear)
- **Job bonus**: Higher-tier jobs provide stake bonus multipliers (up to +30%)
- **Lockstone bonus**: Owning a leveled Lockstone adds up to +30% stake rewards
- **Imbalance bonus**: Validators on underrepresented networks get up to +50% bonus

### Slashing

When a validator goes offline (based on its uptime rate), stakers lose a fraction of their stake:

```
slash_per_tick = slash_rate / 96
```

Higher-risk validators have higher slash rates. The slashing is spread across hourly ticks to prevent catastrophic single-tick losses.

!!! tip "Validator Guard"
    Buy a **Validator Guard** from the shop to protect against slashing. When your validator would be penalized, one guard is auto-consumed to absorb the slash. Stack up to 50 guards.

## Player-Run Validators

Advanced players can register their own validator and earn gas fees from processing transactions.

### Registering a validator

```
.stake validator register <network> <stake_amount>
```

Requirements:

- **Validator Operator** job tier or higher
- Sufficient stake in the network's token (ARC for Arcadia, DSC for Discoin)

### Setting commission

Set your validator's commission rate (the percentage of gas fees you keep):

```
.stake validator commission <rate>
```

### Delegating to a player validator

Other players can delegate their stake to your validator:

```
.stake validator delegate <validator_id> <amount>
```

Undelegate:

```
.stake validator undelegate <validator_id> <amount>
```

### Viewing delegations

See who has delegated to you:

```
.stake validator delegations
```

### Validator block processing

Player validators process pending transactions from the mempool into blocks. View the current mempool:

```
.stake validator mempool
```

Submit a block:

```
.stake validator submit
```

When you submit a block, all pending mempool actions are processed. You earn gas fees from the transactions:

- **10%** of gas fees go to the validator
- **90%** goes to LP/treasury

### Validator stats

Check your validator performance:

```
.stake validator stats
```

View all validator networks:

```
.stake validator networks
```

### Deactivating a validator

```
.stake validator unregister
```

!!! warning "MEV protection"
    The system includes anti-MEV measures: transaction order within the same gas tier is randomized, validators' own transactions execute last in their blocks, and each user is limited to 2 swaps per validator block.

## Player Validators (Active PoS)

In addition to delegating to Protocol Nodes, you can delegate to player-run validators
or register as a validator yourself.

See [Validators  -  Active PoS](validators.md) for full details, including:

- How to register and the 90/10 gas split
- Slash risk (5 slashes = deactivation)
- Delegation mechanics and commission rates
- **Early unstake penalty:** Undelegating within 48 hours of locking incurs a **5% burn** on the amount withdrawn.

## Lockstone

The **Lockstone** is a special item that boosts staking rewards. It levels up as you stake and validate. See the [Shop page](shop.md) for details.
