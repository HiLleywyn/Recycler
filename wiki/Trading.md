# Trading

Trading is how you move value around the Discoin economy: buy tokens with USD,
sell them back, swap one token for another through liquidity pools, watch prices
on charts, and move tokens between players. This page covers the simulated token
market. For real-world market data see the Real Markets section near the bottom.

> The command prefix shown here is a comma (`,`). The prefix is configurable
> per server, so your server may use a different one.

## The token market and the 12 networks

Discoin simulates a full crypto economy across **12 networks** with 40
built-in tokens (plus group tokens and player-deployed tokens minted at
runtime). The networks split into two groups.

**Crypto-style networks** behave like a real exchange. Their core coins have a
live price feed, can be bought and sold for USD, and trade in AMM pools.

| Network | Core tokens | Notes |
|---|---|---|
| Moneta | MTA | Proof-of-work, mine-only coin, ASIC-optimized |
| Sun | SUN | Proof-of-work, mine-only coin |
| Arcadia | ARC, USDC, VTR | Proof-of-stake; USDC is the $1 stablecoin |
| Discoin | DSC, DSD, DSY | Proof-of-stake; DSD is the $1 stablecoin, DSY is a yield token |

**Gameplay-system networks** are tied to a specific minigame. Their tokens are
mostly **earn-only**: you get them by playing the activity, not by buying with
USD. Most have an oracle-priced "cash out to USD" off-ramp instead.

| Network | Tokens | Earned via |
|---|---|---|
| Moon | MOON, mMTA, mSUN, group tokens | Lunar Mint staking, MTA/SUN wrapping |
| Lure | LURE, REEL | Fishing |
| Crypt | COPPER, SILVER, GOLD, RUNE | Dungeon delving |
| Buddy | BUD, FREN, BBT | Buddies, arena battles |
| Harvest | HRV, SEED | Farming |
| Forge | FORGE, INGOT, FGD | Crafting (FGD is a $1 stablecoin) |
| Gamba | gamba tokens | Gambling activities |
| Sage | SAGE, EDU | Crypto learn-and-earn quizzes |

See [Economy](Economy) for the full currency cheatsheet and [DeFi](DeFi) for
liquidity pools and on-chain features.

## Checking prices

| Command | What it shows |
|---|---|
| `,crypto` (`,prices`, `,market`) | Full market, grouped by network |
| `,prices arc` | One network only (`arc`, `dsc`, `sun`, `mta`) |
| `,prices VTR` | A single token |
| `,tokeninfo ARC` (`,ti`) | Price, supply, fees, and LP liquidity for a token |
| `,portfolio` (`,port`) | Your holdings with current USD value |

Prices update on a fixed tick via a simulated price oracle, so quotes move
continuously. Stablecoins (USDC, DSD, FGD) stay pegged near $1.

## Buying and selling

Buy tokens with USD from your wallet:

```
,buy <SYM> <amount>
,buy <amount> <SYM>
```

The argument order is flexible. The amount can be a plain number, the word
`all`, or a dollar figure with a `$` prefix.

```
,buy ARC 0.5            buy 0.5 ARC with USD
,buy ARC $500           buy $500 worth of ARC
,buy DSC 100 with SUN   pay with SUN instead of USD
,buy USDC 1000 yes      skip the confirmation prompt
```

Selling always pays out in **USD**:

```
,sell ARC 0.5           sell 0.5 ARC
,sell ARC $1000         sell $1000 worth
,sell MTA all yes       sell every MTA, skip confirm
,sell everything        sell all of your CeFi holdings at once
```

Flags: `yes` or `-y` skips the confirmation prompt. On a buy, `with <SYM>` pays
using another token instead of USD.

### What you can buy directly

Only the **network coins and stablecoins** can be bought and sold directly with
`,buy` / `,sell`: `SUN`, `MTA`, `ARC`, `DSC`, `USDC`, and `DSD`. Every other
tradeable token (`VTR`, `DSY`, and so on) is **swap-only** - you reach it by
swapping through an AMM pool. Gameplay-system tokens are earn-only and have no
`,buy` path at all.

## Swapping tokens

Swap routes one token into another through AMM liquidity pools:

```
,swap <FROM> <TO> <amount|all>
```

```
,swap USDC ARC 500      buy ARC on the Arcadia Network
,swap ARC VTR 1        ARC into Vantor
,swap DSD DSC 100       on the Discoin Network
,swap DSC DSY all yes   swap everything, skip confirm
```

Flags: `yes` skips the confirmation, `min <amt>` sets a minimum-output slippage
guard, and `gas high|medium|low` picks a gas tier for the mempool.

Swaps are **same-network only**. Cross-network swaps are blocked, and the
pure proof-of-work coins **MTA and SUN cannot be swapped**.

### AMM pricing, slippage, and fees

Each liquidity pool holds reserves of two tokens, and the price you get depends
on the pool's reserve ratio:

- Small swaps trade close to the oracle price.
- Large swaps move the price against you. That gap is **slippage**.
- The bot shows the expected output and effective price before you confirm, so
  use the `min` flag to protect yourself on volatile pairs.

A **swap fee** is taken out of every trade. A share of each swap fee is
permanently **burned**, which makes the affected tokens deflationary over time.
Higher [job](Economy) tiers reduce your swap fee rate.

## Price charts

`,chart` (alias `,c`) renders a candlestick chart for any pair:

```
,chart <PAIR> [timeframe] [indicators/flags...]
```

```
,chart ARCUSD 4h macd rsi vwap
,chart MTAUSD 1d ichimoku supertrend wide
,chart DSCUSD 1h compare:MTA compare:ARC wide
,chart SUNUSD 4h in:MTA heikinashi
```

Timeframes: `1m`, `5m`, `15m`, `1h`, `4h`, `1d`.

The chart engine supports 20+ technical indicators (overlays like `ema20`,
`bb`, `vwap`, `ichimoku`, and oscillators like `rsi`, `macd`, `stoch`, `adx`),
multiple chart styles (`candles`, `line`, `area`, `bars`, `heikinashi`),
size and theme flags (`wide`, `tall`, `light`, `dark`), `compare:<SYM>` overlays
(up to 3), and `in:<SYM>` to re-quote a series in another token's terms.

## Market events

Market events fire randomly (roughly every 2 hours) or are triggered by admins.
Each event moves through phases that change market **volatility** and apply a
daily **price bias** to the whole simulated economy.

| Event | Volatility | Direction | Approx duration |
|---|---|---|---|
| Bull Run | Elevated | Bullish rally | ~45 min |
| Bear Market | High | Bearish selloff | ~45 min |
| Fed Rate Hike | High | Bearish | ~20 min |
| Fed Rate Cut | Calm to elevated | Bullish | ~23 min |
| Black Swan | Extreme | Severe multi-phase crash | ~20 min |
| Whale Pump | High | Sharp pump, then dump risk | ~10 min |
| Rug Pull | Extreme | Brief pump, then a hard crash | ~15 min |
| Global Pandemic | High | Bearish, with a stimulus rebound | ~55 min |
| New Regulation | Elevated | Bearish | ~20 min |
| Mass Adoption | Calm to elevated | Bullish | ~30 min |
| ETF Approved | Elevated to high | Bullish, then sell-the-news | ~25 min |
| Going to the Moon | High | Extreme sustained rally (very rare) | ~100 min |
| Exchange Hack | Extreme | Very bearish | ~20 min |

Each event runs through several timed phases, so volatility and price
direction shift as it plays out -- the table shows the overall character.

Check the current event with `,event`, or browse every event type with
`,event list`. Trading during a high-volatility event is riskier but can be
much more profitable if you call the direction right.

## DeFi wallets and sending tokens

Tokens bought with `,buy` land in your **CeFi holdings**. To move tokens to
other players or use on-chain features, you need a **DeFi wallet**.

| Command | Action |
|---|---|
| `,wallet create <network> [label]` | Create a wallet (`arc`, `sun`, `mta`, `dsc`) |
| `,wallet list` | List your wallets, addresses, and holdings |
| `,wallet info <address>` | Look up the owner and network of an address |
| `,wallet deposit <token> <amount>` | Move tokens from CeFi into the DeFi wallet |
| `,wallet withdraw <token> <amount>` | Move tokens from DeFi back to CeFi |
| `,wallet delete <address>` | Delete a wallet |

Each wallet has a unique address like `arc:abc123def456`.

Send tokens peer-to-peer with `,send`:

```
,send @Lleywyn 5 arc
,send @Lleywyn $25 arc
,send arc:abc123def456 5
,send @Lleywyn 5 dsc DSY
```

If you omit the token, it defaults to the native network coin. Both the sender
and the recipient need a DeFi wallet on the same network.

## Real Markets ($ prefix)

The `$` prefix is a completely separate namespace that queries **live
real-world markets** - crypto, stocks, ETFs, forex, commodities, indices,
perpetual futures, and oracle feeds. It is fully isolated from the simulated
game market: `$` commands never affect your in-game balance or net worth.

| Command | What it does |
|---|---|
| `$chart SYMBOL [tf]` | Real candlestick chart with indicators |
| `$scan SYMBOL [tf]` | Pattern and indicator scout (append `ai` for AI commentary) |
| `$info SYMBOL` | Full asset snapshot, auto-detects crypto/stock/ETF/perp |
| `$market <sub>` | Market-wide views: `fear`, `heatmap`, `gainers`, `top`, `dom` |
| `$compare A B` | Normalized comparison across 2-4 symbols |
| `$watch add SYM <price> above\|below` | Personal price alert |
| `$query <question>` | Professional AI market Q&A with trusted-source citations |
| `$status` | Diagnose data-provider health |

The `$` namespace is prefix-only - it contributes no slash commands. Admins can
gate which channels accept `$` traffic with `$channels add`.

## See also

- [Economy](Economy)
- [DeFi](DeFi)
- [Mining](Mining)
- [Progression](Progression)
