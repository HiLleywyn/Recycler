# Getting Started

Welcome to Discoin, a Discord economy bot that simulates a full crypto
economy: trading, mining, staking, DeFi, gambling, jobs, and more. You
play entirely from inside Discord using simple text commands. No setup,
no wallet imports, just type a command and go.

This page is your new-player on-ramp. It covers how commands work, your
starting balance, your first moves, and how to find help.

## Slash commands vs the prefix

Discoin has two ways to type commands, and the difference matters:

- **Slash commands** (type `/` in Discord) are **informational only**.
  They are: `/help`, `/balance`, `/leaderboard`, `/notify`, `/inventory`,
  `/report`, `/reports`, and `/2fa`. They show you things but never
  change your account.
- **The prefix** runs **all actions** - earning, trading, gambling,
  mining, staking, everything. Type the prefix before a command, for
  example `,buy ARC 1`, `,work`, `,daily`, `,play coinflip 100`.

This wiki writes every example with a comma `,` prefix. The prefix is
configurable per server, so your server may use something else. Run
`/help` at any time to see your server's current prefix.

## Your starting balance

The moment you first use a command, Discoin creates your account
automatically with a small starting balance in USD (the base currency).
USD lives in your **wallet** (spendable). You turn that USD into income,
tokens, rigs, and stakes from there.

## First steps

A good opening sequence:

| Step | Command | What it does |
|---|---|---|
| 1 | `,daily` | Claim a free reward, once per 24h. Builds a streak. |
| 2 | `,work` | Earn job pay. Repeats on a short cooldown. |
| 3 | `,job` / `,jobs` | View your job tier and the full job ladder. |
| 4 | `,promote` | Advance to the next job when you qualify. |
| 5 | `,buy SUN 1` | Buy your first crypto token. |
| 6 | `,chain mine rigs` | Browse mining rigs to start earning SUN or MTA. |
| 7 | `,help <category>` | Explore any system in depth. |

You do not need to rush. `,daily` and `,work` alone will steadily grow
your wallet while you learn the rest of the game.

## How to earn

Discoin gives you several income streams from day one:

- **Daily reward** - `,daily` pays out once every 24 hours, and a
  consecutive-day streak makes each claim worth more. See
  [Economy](Economy) for streak details.
- **Work pay** - `,work` pays job income on a short cooldown. Higher
  daily streaks shorten that cooldown.
- **Jobs and promotions** - `,promote` moves you up the job ladder,
  which raises your pay per shift and unlocks perks.
- **Faucet drops** - `,faucet` claims free crypto when a drop appears in
  the server's faucet channel. Each claimer receives a random token.
- **Passive income** - once you have some capital, mining, staking,
  savings, and liquidity pools all earn while you are away. See
  [Mining](Mining), [Staking and Validators](Staking-and-Validators),
  and [DeFi](DeFi).

## Finding help

The in-bot help system is your map of the whole game:

- `,help` shows every category.
- `,help <category>` opens one system in depth, for example
  `,help mining`, `,help trading`, `,help gambling`, `,help wealth`.
- Each help page lists real command syntax, aliases, and flags.

## Checking bot status

Run `,status` to see whether all of Discoin's background services are
running: the price engine, mining, chain blocks, staking, savings, the
security monitor, the faucet, and market events. It has three pages:
Overview, System Health, and an Economy Snapshot. Handy if something
looks frozen.

## Notification settings

Discoin can DM you when things happen - block rewards, incoming
transfers, staking payouts, item level-ups, and more. Manage these with
`,notify`:

```
,notify                     view all settings
,notify mining on           enable mining DMs
,notify staking arc off     mute ARC staking alerts
```

Categories include `mining`, `transfer`, `validator`, `staking`,
`itemlevelup`, `whalealerts`, `2fa`, `events`, `nft`, and `predictions`.
Most are on by default; `events`, `nft`, and `predictions` are off by
default. DMs require your Discord DMs to be open. You can also mute a
category per network, for example only ARC staking.

## See also

- [Economy](Economy)
- [Trading](Trading)
- [Commands](Commands)
- [FAQ](FAQ)
