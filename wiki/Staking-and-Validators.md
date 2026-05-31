# Staking and Validators

Discoin's Proof-of-Stake (PoS) layer lets you put network tokens to work. There
are two ways to earn from it: stake into yield farms for hands-off APY, or run
(or delegate to) a player validator that processes the on-chain mempool and
earns gas fees. This page covers both.

> The command prefix shown here is `,` but it is configurable per server. If
> `,stake` does nothing, ask an admin what prefix the bot uses.

## Yield farming (PoS staking)

Yield farms are the simplest way to earn passive yield on a PoS network token.
You deposit a token into a farm and it pays out on an hourly tick.

### How to stake

You need a DeFi wallet on the farm's network and some of that network's token in
it (use the on-chain wallet, not your USD wallet).

| Command | What it does |
|---|---|
| `,stake list` | Browse every yield farm by network (aliases: `farmlist`, `nodelist`) |
| `,stake farm <FARM_ID> <amount\|all>` | Stake into a farm (aliases: `stake`, `node`) |
| `,stake unstake <FARM_ID> <amount\|all>` | Withdraw from a farm (aliases: `unnode`, `unfarm`) |
| `,stake unstake everything` | Unstake all unlocked positions at once |
| `,stake mine` | View your active positions (aliases: `mynodes`, `myfarms`, `mystakes`) |

Examples:

```
,stake list             browse all farms
,stake farm LIDO 1.5    stake 1.5 ARC in Lido
,stake unstake LIDO all withdraw from LIDO
,stake mine             your active positions
```

### Farms by network

Only PoS networks support staking. Proof-of-Work coins (like MTA and SUN) are
mined, not staked - see [Mining](Mining).

- **Arcadia Network** - deposit `ARC`: LIDO (~4% APY), CBETH (~3.6%),
  RKTPL (~5.8%), EIGENV (~12%), SWISE (~9%).
- **Discoin Network** - deposit `DSC`: DSCV1 (~5%), DSCV2 (~9%),
  DSCV3 (~12%), DSCV4 (~14%).

Percentages are approximate annual yield. Higher headline yield comes with
higher slash risk. Deposits carry a 24 hour lock.

### Hourly tick, rewards, and slashing

Every hour each farm rolls a payout for everyone parked in it:

```
if random() < uptime_rate: REWARD
else:                      SLASH

Reward = deposit * reward_rate / 24
Slash  = deposit * slash_rate
```

Most ticks pay a reward; occasionally a farm "goes down" and slashes a small
slice of your deposit. The higher the advertised APY, the higher the slash
chance, so spreading deposits across farms is a real risk-management tool.

Every reward payout also grants **+10 XP** to your Lockstone if you own one, and
each Lockstone level adds **+1.5%** to your staking rewards. See
[Shop and Items](Shop-and-Items) for stones.

### HOT / COLD ticks and validator heat

On top of the base roll, each farm rolls a per-tick event:

- HOT (~5%): that tick pays **2.00x** and heat rises **+0.20**.
- COLD (~5%): that tick pays **0.40x** and heat drops **-0.20**.
- Normal (~90%): pays **1.00x**, no heat change.

The roll is shared by everyone on the farm. On top of that, each farm carries a
persistent **heat** meter between **-1.00** and **+1.00** that decays 8% per
tick toward zero (`heat_next = heat_now * 0.92 + event_delta`). Heat tilts every
tick by up to **+/-15%**:

```
tick_pay = base * event_mult * (1 + heat * 0.15)
```

`,stake list` and `,stake mine` show each farm's heat bar. The practical takeaway:
chase persistently hot farms and bail on persistently cold ones, but treat a
single COLD tick on a hot farm as noise.

### Auto-compound

Instead of letting rewards land in your wallet, auto-compound restakes them into
the same farm each tick. See [DeFi](DeFi) for the `,autocompound` commands.

## Player validators (Active PoS)

A validator is a player-run node that processes the on-chain mempool and earns
gas fees. Running one is gated: you need a **DeFi wallet** and the **Validator Op**
job tier (see [Economy](Economy) for jobs).

### Validator commands

| Command | Alias | What it does |
|---|---|---|
| `,stake validator register <network> <amount\|all>` | `vreg` | Register your validator with staked tokens |
| `,stake validator unregister <network>` | `vunreg` | Shut down your validator, refund stake and delegations |
| `,stake validator commission <network> <rate>` | `vcomm` | Set the rate you keep vs delegators |
| `,stake validator list [network]` | `vals` | List validators on a network |
| `,stake validator stats` | `vstats` | Your validator performance |
| `,stake validator networks` | `vnetworks` | Networks that support validators |
| `,stake validator mempool [network]` | | View pending mempool actions |
| `,stake validator submit <type> <net> <gas> <payload>` | | Submit a custom action |

Supported networks: `arc`, `sun`, `mta`, `dsc`.

### How block production works

Every **120 seconds**, one validator per network is selected to process the
mempool. Selection weight is your personal stake plus delegated stake. If you
produced the previous block, your weight is cut to 10% for the next round to
keep block production from concentrating.

Gas fees from a confirmed block are split:

```
Gas split per block:
  10% -> selected validator
    +- 80% kept by the validator
    +- 20% paid to that validator's delegators
  90% -> guild treasury
```

Each confirmed block also grants the validator **+10 XP** toward their Lockstone.

### The gas-fee economic loop

Token sends and swaps queue in the mempool whenever validators are active and
pay a gas fee in USD:

| Priority | Gas units | Notes |
|---|---|---|
| `high` | 3x | First priority |
| `medium` | 2x | Default |
| `low` | 1x | Last priority |

USD transfers are always instant and off-chain. The USD gas fee is not refunded.
Tokens stay locked until the action is confirmed or rejected. That fee flow -
players paying gas, validators and the treasury earning it - is what funds the
guild treasury and keeps the PoS economy moving.

### Delegation

If you do not want to run a validator yourself, you can delegate tokens to one
and earn a share of its gas rewards.

| Command | Alias | What it does |
|---|---|---|
| `,stake validator delegate @val <network> <amount\|all>` | `vdel` | Delegate tokens to a validator |
| `,stake validator undelegate @val <network> <amount\|all>` | `vundel` | Withdraw your delegation |
| `,stake validator delegations` | `mydels` | List your active delegations |

Delegators split the 20% delegator slice of each block proportionally to how
much they delegated. The minimum delegation is **50 tokens** and a delegation is
locked for **24 hours** after you make it. If the validator unregisters, your
delegation is refunded immediately.

### Slashing and jail

Validators are punished for submitting work that gets rejected:

```
Rejected submission -> -1% validator stake
                    -> -1% delegator stakes (slashed proportionally)
3rd slash           -> validator auto-deactivated, all delegations refunded
```

Delegating is lower-risk than running a validator, but it is not risk-free: if
your chosen validator gets slashed, your delegation is slashed alongside it.
Pick reliable validators and check `,stake validator stats` before delegating.

### Wealth Bottleneck

Validator and delegator block rewards are scaled by your wealth-leaderboard rank
before they are credited - the same curve that gates savings interest and farm
yield. The richest players keep a small fraction; the poorest get a community
pool top-up. Run `,help wealth` in the bot for the full curve.

## See also

- [DeFi](DeFi)
- [Mining](Mining)
- [Economy](Economy)
- [Shop and Items](Shop-and-Items)
