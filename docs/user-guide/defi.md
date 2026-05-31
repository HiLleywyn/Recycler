# DeFi: Pools, Lending & Savings

This page covers Discoin's decentralized finance features: liquidity pools, borrowing, and savings accounts.

## Liquidity Pools

Liquidity pools power the token swap system. Each pool holds reserves of two tokens. When players swap, they trade against the pool's reserves. Liquidity providers earn a share of swap fees.

### Listing pools

View all available pools:

```
.trade pool list
```

Aliases: `.trade pool ls`

### Checking pool price

View the current price and reserve info for a specific pool:

```
.trade pool price <PAIR>
```

### Adding liquidity

Deposit tokens into a pool to become a liquidity provider (LP):

```
.trade pool add <TOKEN_A> <TOKEN_B> <amount_a|all> <amount_b|all>
```

Use `all` for either amount to deposit your entire balance of that token.

When you add liquidity, you receive LP shares proportional to your contribution. You earn a portion of all swap fees collected by the pool.

!!! warning "LP lock period"
    After adding liquidity, your position is **locked for 2 hours**. You cannot remove liquidity during this period. This prevents flash-liquidity attacks.

!!! note "Concentration limit"
    No single LP can hold more than **50%** of a pool's total liquidity. This prevents monopolization.

### Removing liquidity

Withdraw your liquidity from a pool:

```
.trade pool remove <TOKEN_A> <TOKEN_B> <shares|all>
```

Use `all` to withdraw your entire LP position.

Aliases: `.trade pool removelp`, `.trade pool rmlp`

- Large removals (more than 25% of the pool) have a **10-minute cooldown** between them
- You receive both tokens in the pool proportional to your share

### Impermanent loss

When you provide liquidity, the ratio of your two tokens changes as the pool price moves. If the price moves significantly from when you deposited, you may end up with fewer total tokens than if you had just held them. This is called **impermanent loss**.

The loss is "impermanent" because it reverses if the price returns to your entry point. However, if you withdraw while the price is different, the loss becomes permanent.

!!! tip "Offsetting IL"
    Swap fees earned as an LP can offset impermanent loss. High-volume pools generate more fees, which helps compensate for price movements.

### Creating pools

Players with the **Exploiter** job tier can create new pools:

```
.trade pool create <tokenA> <tokenB> <amountA> <amountB>
```

## Savings Accounts

Savings accounts let you earn interest on your USD or SUN deposits. Interest rates are dynamic, based on an Vantor V2-style utilization model.

### How rates work

The savings rate depends on how much of the savings pool is being borrowed:

- **Low utilization** (few borrowers): lower rates, guaranteed floor of ~6% APY
- **Optimal utilization** (80%): moderate rates (~73% APY borrow rate)
- **High utilization** (above 80%): rates climb steeply to incentivize deposits

View current rates:

```
.bank savings rates
```

Aliases: `.rates`

### Depositing into savings

Deposit USD into your savings account:

```
.bank savings deposit <amount>
```

Shortcut: `.save <amount>`

Minimum deposit: **$1.00**.

### Withdrawing from savings

Withdraw USD from savings:

```
.bank savings withdraw <amount>
```

Shortcut: `.unsave <amount>`

### Checking your savings

View your savings balance and earned interest:

```
.bank savings
```

### Vaultstone bonus

Owning a **Vaultstone** increases your savings interest rate. At max level, a Vaultstone adds up to **+36%** interest. See the [Shop page](shop.md) for details.

!!! tip "Yield Guard"
    Buy a **Yield Guard** from the shop to protect your savings principal. If a borrower defaults and the savings pool takes a loss, one guard is auto-consumed to shield your deposit. Stack up to 50 guards.

## Lending

Borrow tokens against your bank balance as collateral.

### Borrowing (USD collateral)

Borrow tokens by posting USD from your bank as collateral:

```
.bank loan borrow <token> <amount>
```

Shortcut: `.borrow <token> <amount>`

- **Maximum LTV**: 65% (you can borrow up to 65% of your collateral's value)
- **Daily interest rate**: 2%
- **Collateral seasoning**: your bank deposit must be at least 1 hour old before it counts as eligible collateral
- Borrowable tokens: ARC, DSC, and USD

### Repaying loans

Repay your outstanding loan:

```
.bank loan repay <amount>
```

Shortcut: `.repay <amount>`

### Checking loan status

View your current loan details:

```
.bank loan status
```

Aliases: `.bank loan debt`, `.bank loan info`

### Liquidation

If your loan-to-value ratio exceeds the **liquidation threshold** (80% LTV), your collateral is automatically liquidated:

- A **5% liquidation penalty** is burned
- Interest is checked every 30 minutes
- Remaining collateral (minus penalty) is returned to you

!!! warning "Monitor your loans"
    If token prices move against you, your LTV can exceed the liquidation threshold. Check `.bank loan status` regularly and repay early to avoid liquidation.

## Reserve system

A portion of all platform fees (25%) is deposited into the Community Reserve. This reserve acts as a protocol safety net and helps fund the savings pool floor rate.
