# DeFi

Discoin's DeFi layer is where your tokens earn yield without being mined or
staked with a validator. It covers AMM liquidity pools, a USD savings pool,
auto-compounding, on-chain smart contracts, collateralized lending, and the Moon
Network. Most of it needs a DeFi wallet on the relevant network.

> The command prefix shown here is `,` but it is configurable per server. If
> `,trade pool` does nothing, ask an admin what prefix the bot uses.

## Liquidity pools (AMM)

Pools are automated market makers: you deposit two tokens, become a liquidity
provider (LP), and earn a share of every swap fee that routes through the pool.

### Pool commands

| Command | Alias | What it does |
|---|---|---|
| `,trade pool list [all\|network\|TOKEN]` | `pool ls` | Browse pools |
| `,trade pool add <A> <B> <amt_a\|all> <amt_b\|all>` | `pool addlp` | Add liquidity |
| `,trade pool remove <A> <B> <shares\|all>` | `pool removelp` | Remove liquidity |
| `,trade pool remove everything` | | Remove all LP from all pools |
| `,trade pool lock <A> <B> <7\|30\|90>` | | Time-lock a position for a Liqstone XP boost |
| `,trade pool unlock <A> <B>` | | Break a lock early (burns 10% of your LP shares) |
| `,trade pool price <PAIR>` | | Show a pool's price |

Examples:

```
,trade pool list arc               Arcadia pools
,trade pool add ARC USDC 0.5 2000  add liquidity
,trade pool remove ARC USDC all    withdraw all LP
,trade pool lock ARC USDC 30       lock 30 days for 2.5x Liqstone XP
```

A DeFi wallet is required and tokens are drawn from your on-chain wallet.

### LP shares and yield

When you add liquidity you receive LP shares:

```
First deposit:  LP = sqrt(amt_A * amt_B)
Later deposits: LP = total * min(a/res_A, b/res_B)
```

Swap fees (0.3% per swap) accumulate inside the pool reserves, so your LP shares
slowly gain underlying value between deposits. A green marker in `,trade pool list`
flags pools you have a position in.

### Time-lock boost

Locking a position commits it for a fixed term in exchange for a Liqstone XP
multiplier: **7d -> 1.5x**, **30d -> 2.5x**, **90d -> 4.0x**. An active lock blocks
`pool remove` until it expires. `pool unlock` breaks a lock early and burns 10%
of your LP shares (which boosts the remaining LPs). Lapsed locks auto-expire with
no penalty.

### Bootstrap incentive and user-token bonus

Empty or quiet pools pay **up to 5x base LP yield** to whoever seeds them, decaying
as the pool reaches **$10,000** of liquidity and **$5,000** of recent volume. The
first seeder into a brand-new pool wins the largest reward. Separately, holding
LP in pools that include a user-created token grants **+0.001% work/daily per $1**
of LP value (capped at +8%) plus **+30% Liqstone XP** weight on those positions.

### Creating pools

New pools are auto-seeded for Moon Network group tokens and player-deployed
tokens at creation. Manually creating a pool is gated to the Exploiter job tier
(see [Progression](Progression)).

> LP that is added automatically when you buy a hashstone, lockstone,
> vaultstone, or liqstone is locked and cannot be removed manually while you
> hold that item - sell the stone to unlock it.

## Savings

The savings pool is a USD account that earns variable interest. Borrowers (see
Lending below) pay into it, so more borrowing means a higher yield for savers.

| Command | Alias | What it does |
|---|---|---|
| `,save <amount\|all>` | `bank savings deposit` | Deposit USD into savings |
| `,unsave [amount\|all]` | `bank savings withdraw` | Withdraw to your wallet |
| `,savings` | `mysavings` | Your balance and live rates |
| `,rates` | `apy` | The full rate curve |

### Variable APY (utilization model)

Rates follow an Vantor-style "kink" model driven by utilization
(`total_borrowed / total_deposited`):

```
  0% util  -> savings  0.00%/day
 50% util  -> savings  0.65%/day
 80% util  -> savings  1.44%/day  <- kink
 90% util  -> savings  7.70%/day
100% util  -> savings 15.3%/day
```

Owning a Vaultstone grants **+10 XP** per interest tick. Savings interest is
also scaled by your wealth-leaderboard rank: the poorer half of the leaderboard
earns the listed APY plus a community-pool top-up, while the top of the
leaderboard keeps less. Run `,help wealth` for the full curve.

## Auto-compound

Auto-compound restakes your staking rewards into the same farm each hourly tick
instead of paying them to your wallet, compounding the yield.

| Command | Alias | What it does |
|---|---|---|
| `,autocompound on [farm\|all]` | `ac on` | Enable for one farm or all farms |
| `,autocompound off [farm\|all]` | `ac off` | Disable |
| `,autocompound status` | `ac status` | View settings and lifetime totals |

You get a DM whenever a compound fires, listing each position and amount. Enable
per-farm with the farm ID, e.g. `,autocompound on ARC-V1`.

## Smart contracts

Smart contracts are on-chain programs you deploy and call through the PoS
mempool. They run atomically and roll back on revert.

| Command | Alias | What it does |
|---|---|---|
| `,contract deploy <name> <network> [type]` | `ct` | Deploy a contract |
| `,contract call <address> <function>` | | Call a contract function |
| `,contract info <address>` | | State, balance, owner |
| `,contract list [network]` | `ls` | List contracts |
| `,contract events <address> [limit]` | `log` | View emitted events |
| `,contract txs <address> [limit]` | `history` | Transaction history |
| `,contract fund <address> <TOKEN> <amount>` | | Fund a contract |
| `,contract withdraw <address> <TOKEN> <amount>` | | Withdraw from a contract |
| `,contract pause <address>` / `,contract resume <address>` | | Pause or resume |

Deploy and call accept a `gas high|medium|low` flag; deploy also takes
`desc "..."` and `def {json}`, and call takes repeatable `arg key=val` flags.

### Built-in templates

- `limit_order` - place, execute, and cancel limit orders.
- `escrow` - deposit, release, and refund escrowed funds.
- `vesting` - fund and claim time-locked vesting.
- `multisig` - setup, deposit, approve, execute, and revoke.

```
,contract deploy MyEscrow arc escrow desc "Holds funds"
,contract call 0xabc place arg token=ARC arg amount=1
```

Custom contracts are built from an op set covering token receive/send, AMM and
oracle trades (swap/buy/sell), assertions (`require`, `require_caller`,
`require_price`, `require_time`), persistent storage (`set_state`/`get_state`),
event logs (`emit`), and `vested_claim`.

## Lending

You can borrow USD against your bank balance as collateral. Loans live under the
`,bank loan` group.

| Command | Alias | What it does |
|---|---|---|
| `,bank loan borrow <amount>` | | Borrow USD against your bank |
| `,bank loan repay [amount\|all]` | | Repay your loan |
| `,bank loan status` | `debt`, `info` | View your active loan |

When you borrow, collateral is locked from your bank. The maximum loan-to-value
(LTV) is **65%**, so borrowing $650 locks $1,000 of collateral. Interest accrues
on a regular tick at the same dynamic borrow rate the savings pool uses, so the
amount you owe grows over time.

If your LTV climbs to **80%** or higher, the loan is **liquidated**: your
collateral is seized and a **5%** liquidation penalty is burned. Repay early or
top up your bank to stay clear of the threshold. `,bank loan status` shows your
current LTV and the liquidation line.

## Moons and the Moon Network

The Moon Network has its own native yield token, MOON, with a two-tier system.

### Lunar Mint (Tier 1)

Stake your mining-group tokens to mint MOON on an hourly tick.

| Command | What it does |
|---|---|
| `,moon stake <GROUP_SYM> <amt\|all>` | Open or top up a Lunar Mint position |
| `,moon unstake <GROUP_SYM> [amt\|all]` | Withdraw (5% burn if held under 48h) |
| `,moon info [GROUP_SYM]` / `,moon list` | View your positions |
| `,moon autocompound on\|off` | Auto-stake earned MOON into the Moon Pool |

Emissions are valued by a 24h TWAP of your staked tokens (so a quick pump cannot
farm inflated MOON), with a 12h warmup ramp, an activity bonus of up to +25% for
active groups, and up to +30% from your server's Moon Network vault level.
Output is capped per user (500/day), per guild (10,000/day), and by MOON's 1B
max supply.

### Moon Pool (Tier 2)

Stake MOON to earn a basket of major network tokens.

| Command | What it does |
|---|---|
| `,moon pool stake <amt\|all>` | Stake MOON into the pool (minimum 10 MOON) |
| `,moon pool unstake [amt\|all]` | Withdraw (5% burn if held under 48h) |
| `,moon pool info` | Your position, share, and next-tick yield |
| `,moon burn <amt\|all>` | Destroy MOON for a USD-equal slice of every guild group token |

The Moon Pool earmarks 25% of every Moon Network vault inflow into a
distributable balance that drips out over roughly 7 days. Stakers earn a basket
of **MTA / ARC / DSC / SUN** (split by equal USD value) on each hourly tick,
paid into their respective network wallets.

### Wrapped coins

Group tokens trade on Moon Network against synthetic 1:1 wrappers of native
coins. `,moon wrap mta` / `,moon wrap sun` mint MMTA / MSUN, and
`,moon unwrap mmta` / `,moon unwrap msun` bridge back to native MTA / SUN. The
wrap is fee-free with a 1:1 peg kept honest by arbitrage.

## See also

- [Staking and Validators](Staking-and-Validators)
- [Trading](Trading)
- [Economy](Economy)
- [Mining](Mining)
