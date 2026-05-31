# FAQ

Quick answers to the questions new and returning Discoin players ask most. Each answer links to a fuller page if you want the detail.

All examples use a comma `,` prefix. The prefix is configurable per server - run `/help` to see your server's current prefix.

## How do I start playing?

There is no sign-up. The first time you use any command, Discoin creates your account automatically with a small starting USD balance. From there, run `,daily` for a free reward, `,work` for a paycheck, and `,buy SUN 1` to pick up your first token. The full walkthrough is on the [Getting Started](Getting-Started) page.

## What is the command prefix, and why don't slash commands do anything?

Slash commands (typed with `/`) are intentionally informational only - `/help`, `/balance`, `/leaderboard`, `/notify`, `/inventory`, `/report`, and `/2fa` just display information. Every action that earns, spends, trades, or changes your account uses the prefix instead, for example `,work` or `,buy ARC 1`. This wiki writes examples with `,`, but server admins can change the prefix; run `/help` to see your server's prefix. See [Getting Started](Getting-Started) for more.

## What is the difference between Free and Premium?

Discoin's core economy - earning, trading, mining, staking, gambling, the shop, and progression - is fully playable for free. Premium adds convenience and cosmetic extras on top. For the exact list of what Premium includes, see the [Premium](Premium) page.

## How do I make money early on?

Stack several income streams instead of waiting on one cooldown. Claim `,daily` every day to build a streak, run `,work` whenever it is off cooldown, grab `,faucet` drops in the faucet channel, and chat naturally in the income channel for small silent credits. Once you have capital, mining, staking, savings, and liquidity pools earn passively. The [Economy](Economy) page breaks down every source.

## What is net worth?

Net worth is the single number that sums everything you own: your wallet and bank USD, CeFi crypto, DeFi wallet balances, stakes, validator positions, LP positions, mining rigs, savings, items, and NFTs, minus any loan liability. It is computed in one place so it is always consistent, and it drives the leaderboard, the Wealth Bottleneck, and the Eat the Rich targeting rules. See [Economy](Economy) and [Progression](Progression).

## I lost my daily streak - why?

Your `,daily` streak only survives if you claim again within the grace window. The cooldown is 24 hours, but the streak resets if more than 48 hours pass since your last claim. So you have a one-day buffer, but miss two days in a row and the streak drops back to 1. The maximum streak is 365 days. See [Economy](Economy) for streak bonuses and how streaks shorten your work cooldown.

## How do I trade tokens?

Use `,buy <SYM> <amount>` and `,sell <SYM> <amount>` to trade against the simulated markets, and `,swap` to exchange one token for another. Prices move over time on a price oracle, so timing matters. The full trading flow, fees, and chart tools are on the [Trading](Trading) page.

## What is the difference between wallet, bank, and a DeFi wallet?

Your wallet holds spendable USD, and your bank is safer USD storage you move money in and out of with `,deposit` and `,withdraw`. Tokens you `,buy` sit in your CeFi portfolio (custodied). A DeFi wallet is a separate on-chain address you create with `,wallet create <network>` so you can send tokens peer-to-peer and use on-chain features. Moving a token from CeFi to a DeFi wallet charges a small platform fee; moving back is free. See [Economy](Economy) and [Trading](Trading).

## Is my progress shared across servers?

No. Discoin runs a separate, per-guild economy. Your balance, jobs, stakes, leaderboard rank, and the Wealth Bottleneck pool all belong to the server you played them in. Playing in another server means a fresh account there. See [Getting Started](Getting-Started).

## Where is the web dashboard?

Discoin has a companion web dashboard for viewing your portfolio outside Discord. Run `,info` (aliases `,about`, `,dashboard`) to get the dashboard link for your instance. Dashboard logins can be protected with two-factor authentication via `,2fa`. See [Server Administration](Server-Administration).

## How do I report a bug?

Use `,report <category> <message>` to file a report directly from Discord. Categories are `bugs`, `suggestions`, `users`, and `other`. For example: `,report bugs The swap command shows the wrong slippage`. You can browse public reports with `,reports`, and submit against a specific bug bounty with `,bugbounty <id> <message>`. See [Server Administration](Server-Administration).

## See also

- [Getting Started](Getting-Started)
- [Economy](Economy)
- [Trading](Trading)
- [Commands](Commands)
