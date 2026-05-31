# Player Overview

Welcome to **Discoin** -- a Discord economy bot with crypto markets, mining, staking, DeFi, gambling, and more. Everything runs inside your Discord server using simple text commands.

## What Can You Do?

| Activity | Description |
|---|---|
| **Earn** | Work, claim dailies, and climb the job ladder |
| **Trade** | Buy, sell, and swap tokens on simulated crypto markets |
| **Mine** | Purchase rigs and mine SUN or MTA on proof-of-work networks |
| **Stake** | Delegate tokens to validators and earn passive rewards |
| **DeFi** | Provide liquidity, borrow against collateral, deposit into savings |
| **Gamble** | Coinflip, dice, slots, roulette, blackjack, mines |
| **Shop** | Buy Hashstone, Lockstone, Vaultstone, and consumable items |
| **Report** | Submit bug reports and claim bounties |

## How the Economy Works

Discoin simulates a multi-network crypto economy with 12 networks. Four are crypto-style chains where coins are actively traded:

| Network | Tokens | Type |
|---|---|---|
| **Sun Network** | SUN | PoW (mineable) |
| **Moneta Chain** | MTA | PoW (mineable) |
| **Arcadia Network** | ARC, USDC, VTR, STR | PoS (stakeable) |
| **Discoin Network** | DSC, DSD, DSY, DEGEN, DRIP, DFUN | PoS (stakeable) |

The other eight are gameplay-system networks tied to a minigame -- Moon, Lure (fishing), Crypt (dungeons), Buddy (buddies), Harvest (farming), Forge (crafting), Gamba (gambling), and Sage (learn-and-earn) -- whose tokens are mostly earned by playing.

Token prices fluctuate over time using a simulated price oracle. You earn **USD** (the base fiat currency) from working and dailies, then use USD to buy tokens, provide liquidity, or gamble.

## Getting Started as a New Player

When you first interact with the bot, an account is created automatically with a starting balance of **$1,000 USD**.

### Your first steps

1. **Check your balance** to see what you have:

    ```
    .balance
    ```

2. **Work** to earn your first coins:

    ```
    .earn work
    ```

3. **Claim your daily reward** (once every 24 hours):

    ```
    .earn daily
    ```

4. **Deposit** some money into your bank for safekeeping:

    ```
    .bank deposit 500
    ```

5. **Buy your first token** when you have enough USD:

    ```
    .trade buy SUN 100
    ```

!!! tip "Streaks matter"
    Claiming `.earn daily` every day builds a streak that increases your reward by **$10 per day**, up to a 365-day streak. Missing two days resets the streak.

!!! note "Command prefix"
    The prefix shown throughout this guide is `.` but your server may use a different prefix. The bot's prefix is configurable by server admins.

## Progression Path

Discoin has a **job ladder** that unlocks higher earnings, more mining rig slots, and perks like fee rebates and staking bonuses. You start as **Homeless** and can promote all the way to **Exploiter**.

To promote, you need a minimum number of work completions and a minimum net worth. Use `.earn jobs` to see the full ladder and `.earn promote` when you qualify.

### Suggested progression

| Stage | What to focus on |
|---|---|
| Early game | `.earn work` and `.earn daily` to build USD |
| Mid game | Buy tokens with `.trade buy`, start mining with `.chain mine buy` |
| Late game | Stake tokens, provide LP, run validators, buy stones from the shop |

## Key Concepts

- **Wallet vs Bank**: Your wallet is for spending. Your bank is for saving and collateral. Deposit and withdraw freely.
- **CeFi Holdings vs DeFi Wallets**: Tokens you buy with `.trade buy` go into your CeFi portfolio. To send tokens peer-to-peer or interact with on-chain features, create a DeFi wallet with `.wallet create`.
- **Gas fees**: Transactions on the blockchain cost a small gas fee, which varies by network.
- **Transaction hashes**: Every trade, transfer, and action is logged on the chain and can be looked up with `.chain tx`.
