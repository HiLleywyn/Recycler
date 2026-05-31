# Trading

This page covers buying, selling, and swapping tokens, managing your portfolio, and using DeFi wallets to send tokens peer-to-peer.

## Tokens

Discoin has 40 built-in tokens across 12 networks. Most trading happens on the four crypto-style networks, whose core tokens are:

| Token | Network | Type |
|---|---|---|
| SUN | Sun Network | PoW (mineable) |
| MTA | Moneta Chain | PoW (mineable) |
| ARC | Arcadia Network | PoS (stakeable) |
| USDC | Arcadia Network | Stablecoin |
| VTR | Arcadia Network | DeFi token |
| DSC | Discoin Network | PoS (stakeable) |
| DSD | Discoin Network | Stablecoin |
| DSY | Discoin Network | Yield token |

The Arcadia and Discoin networks carry more tradeable tokens (such as STR, DEGEN, DRIP, and DFUN), and the eight gameplay-system networks (Moon, Lure, Crypt, Buddy, Harvest, Forge, Gamba, Sage) have their own minigame tokens.

Prices fluctuate continuously via a simulated oracle. Stablecoins (USDC, DSD) are pegged to $1.

## Checking Prices

View current prices for all tokens:

```
.trade prices
```

Get detailed info on a specific token:

```
.trade info SUN
```

Aliases: `.trade token`, `.trade ti`

## Buying Tokens

Buy tokens directly with USD from your wallet:

```
.trade buy <token> <amount>
```

The `<amount>` is in token units. You can also specify a USD amount with the `$` prefix:

```
.trade buy SUN $500
```

Use `all` to spend your entire wallet balance:

```
.trade buy ARC all
```

You can write the arguments in either order:

```
.trade buy 10 SUN
.trade buy SUN 10
```

!!! note "Directly buyable tokens"
    Only network coins (SUN, MTA, ARC, DSC) and stablecoins (USDC, DSD) can be bought directly with USD. For other tokens like VTR, DSY, use `.trade swap`.

### Fees

Every buy incurs a small platform fee (0.2% of USD value, minimum $0.10, maximum $20.00) plus network gas fees that vary by token.

## Selling Tokens

Sell tokens back for USD:

```
.trade sell <token> <amount>
```

Same argument flexibility as buying -- use `$` prefix for USD amounts, `all` for your full holding:

```
.trade sell SUN all
.trade sell MTA $1000
```

## Swapping Tokens

Swap one token for another through an AMM liquidity pool:

```
.trade swap <from_token> <to_token> <amount>
```

Example -- swap 50 SUN for ARC:

```
.trade swap SUN ARC 50
```

Shortcut (works without the `trade` prefix):

```
.swap SUN ARC 50
```

### How AMM Pricing Works

Swaps use an **Automated Market Maker (AMM)** model. Each liquidity pool holds reserves of two tokens. The price you get depends on the ratio of reserves in the pool:

- **Small swaps** get prices close to the oracle price
- **Large swaps** move the price against you (slippage)
- Maximum swap size is **15% of the pool reserve** (5% for low-liquidity pools)

### Slippage

Slippage is the difference between the expected price and the price you actually get. Larger trades relative to pool size cause more slippage. The bot shows you the expected output and effective price before you confirm.

### Swap fees

Swap fees are deducted from the trade. 25% of swap fees are permanently burned (deflationary). Higher job tiers reduce your swap fee rate -- Exploiters pay 0% swap fees.

## Price Charts

View a candlestick chart for any token:

```
.trade chart <token> [timeframe]
```

Available timeframes: `1m`, `5m`, `15m`, `1h`, `4h`, `1d`

```
.trade chart SUN 1h
```

Shortcut: `.chart SUN 1h` or `.c SUN`

## Portfolio

View your token holdings and their current USD value:

```
.trade portfolio
```

Aliases: `.trade port`, `.trade holdings`

Shows each token you hold, its quantity, current price, and total USD value.

## DeFi Wallets

Tokens bought through `.trade buy` go into your **CeFi holdings**. To send tokens to other players or interact with on-chain features, you need a **DeFi wallet**.

### Creating a wallet

```
.wallet create <network> [label]
```

Networks: `arc`, `sun`, `mta`, `dsc`

Example:

```
.wallet create arc My ARC Wallet
```

You get one wallet per network. Each wallet has a unique address like `arc:abc123def456`.

### Listing your wallets

```
.wallet list
```

Shows all your wallets, their addresses, networks, labels, and current holdings.

### Moving tokens to your DeFi wallet

Move tokens from CeFi holdings into your DeFi wallet:

```
.wallet deposit <token> <amount>
```

Move tokens back from DeFi to CeFi:

```
.wallet withdraw <token> <amount>
```

### Looking up a wallet address

```
.wallet info <address>
```

Shows the owner and network for any wallet address.

### Deleting a wallet

```
.wallet delete <address>
```

## Sending Tokens

Send tokens from your DeFi wallet to another player's DeFi wallet:

```
.send <@user> <amount> <network> [token]
.send <wallet_address> <amount> [token]
```

Examples:

```
.send @Lleywyn 5 arc
.send @Lleywyn $25 arc
.send arc:abc123def456 5
.send @Lleywyn 5 dsc DSY
```

If you omit the token, it defaults to the native network token (ARC for Arcadia, SUN for Sun Network, etc.).

!!! warning "DeFi wallet required"
    Both sender and recipient must have a DeFi wallet on the same network. Create one with `.wallet create <network>` first.

## Liquidity Pools

For details on providing liquidity, see the [DeFi page](defi.md).

Quick reference for pool commands under the trade group:

```
.trade pool list                                      -- list all pools
.trade pool add <A> <B> <amt_a|all> <amt_b|all>       -- add liquidity
.trade pool remove <A> <B> <shares|all>               -- remove liquidity
.trade pool price <PAIR>                               -- check pool price
.trade pool create <tokenA> <tokenB> <amtA> <amtB>    -- create a new pool (Exploiter only)
```
