# Gambling

The Discoin casino is a set of six betting games you can play with any token in your wallet. Every game runs through one command surface, `,play`, and shares the same house rules. Gambling is a fast way to grow (or shrink) a balance, and it feeds your gambling stats, leaderboards, and group totals.

> The command prefix shown here is `,`. Server admins can change the prefix, so your server may use a different character.

## General rules

All gambling uses `,play <game>` (aliases: `,gamble`, `,games`).

| Rule | Value |
|---|---|
| Command | `,play <game> <amount> [token] [options]` |
| Minimum bet | `$1.00` |
| House edge | 5% across every game |
| Default token | USD |
| Accepted tokens | Any token you hold (USD, SUN, ARC, DSC, and more) |

Amounts accept the standard shorthand used everywhere in Discoin: `all`, `half`, `quarter`, `1.5k`, `2m`, `$500`, and plain numbers. See [Command Chaining](Groups-and-Social) amount notes if you want to pipe winnings straight into another command.

## The games

| Game | Command | Aliases | How it works |
|---|---|---|---|
| Coinflip | `,play coinflip <amount> [token] [heads\|tails] [mode]` | `cf`, `flip` | Heads or tails with five betting modes. |
| Slots | `,play slots <amount> [token]` | `sl` | Spin three reels and match symbols. |
| Dice | `,play dice <amount> [token] [mode]` | -- | Roll 1-100 with six betting modes. |
| Roulette | `,play roulette <amount> [token] <bet_type> [detail]` | `rou` | European wheel, numbers 0-36. |
| Blackjack | `,play blackjack <amount> [token]` | `bj` | Beat the dealer to 21. |
| Mines | `,play mines <amount> [bombs] [token]` | -- | Reveal safe tiles on a bomb grid. |

### Coinflip

A classic 50/50 flip with five modes. Use `,play help coinflip` for full payout tables.

| Mode | Example | Result |
|---|---|---|
| classic | `,play cf 100` | Standard 50/50, 1.95x payout |
| streak | `,play cf 100 streak 5` | Land 5 of the same side in a row (32x) |
| don | `,play cf 100 don` | Double-or-nothing |
| trio | `,play cf 100 trio hht` | Match an exact 3-coin pattern (8x) |
| rainbow | `,play cf 100 rainbow 3` | Hit exactly 3 of 5 heads (binomial odds) |

### Slots

Spin three reels of fruit, diamonds, and sevens.

```
,play slots 100
```

| Result | Payout |
|---|---|
| 3 of a kind | 5x your bet (three diamonds is the jackpot) |
| 2 of a kind | 0.5x your bet |
| No match | Lose your bet |

### Dice

Roll a number from 1 to 100 and bet on the outcome. Six modes are available. Use `,play help dice` for full payout tables.

| Mode | Example | Result |
|---|---|---|
| classic | `,play dice 100` | Standard 2x (~50%) |
| classic (multiplier) | `,play dice 100 10` | Pick a multiplier, 10x (~10%) |
| over | `,play dice 100 over 65` | Roll above 65 (2.86x) |
| under | `,play dice 100 under 30` | Roll below 30 (3.45x) |
| range | `,play dice 100 range 30 60` | Roll inside the range (3.23x) |
| exact | `,play dice 100 exact 77` | Pick one exact number (100x) |
| odd/even | `,play dice 100 odd` | Bet on parity (~2x) |
| ladder | `,play dice 100 ladder 3` | Hit 3 ascending rolls in a row (6.18x) |

### Roulette

European roulette on a wheel numbered 0-36.

```
,play roulette 100 red
,play roulette 50 number 17
```

| Bet type | Payout | Covers |
|---|---|---|
| `number <0-36>` | 35x | A single number |
| `red` / `black` | 1x | 18 numbers |
| `odd` / `even` | 1x | 18 numbers |
| `dozen <1-3>` | 2x | 1-12, 13-24, or 25-36 |
| `column <1-3>` | 2x | One vertical column |

### Blackjack

Play a hand against the dealer using interactive Hit and Stand buttons.

- Get closer to 21 than the dealer without going over.
- A natural blackjack pays 1.5x, a normal win pays 0.95x, and a tie refunds your bet.
- The dealer hits on 16 or below and stands on 17 or above.

```
,play blackjack 500
```

### Mines

A 24-tile grid hides a number of bombs. Click tiles to reveal them, and your multiplier climbs with every safe tile. Cash out whenever you like.

```
,play mines 100        5 bombs (default)
,play mines 500 10     10 bombs, higher multiplier
,play mines 100 1 ARC  1 bomb, bet in ARC
```

- You choose 1 to 20 bombs. More bombs means a higher multiplier per safe tile, but a higher chance of busting.
- Cash out at any time to lock in your winnings.
- The game has a 2 minute timeout that triggers an automatic cash-out or forfeit.

## Gambling stats

Every wager is recorded. Review your performance with `,play stats` (aliases: `,gambstats`, `,gstats`).

```
,play stats                  your all-time stats
,play stats daily            scoped to today
,play stats dice weekly      one game, one period
,play stats @user            another player's record
,play stats group monthly    your group's combined stats
,play stats lb dice weekly   leaderboard ranked by profit and loss
```

- **Periods:** `daily`, `weekly`, `monthly`, `yearly`
- **Games:** `coinflip`, `dice`, `slots`, `roulette`, `blackjack`, `mines`
- Stats show total wagered, profit and loss, win rate, and a per-game breakdown.

## Tips

- Start small. The 5% house edge means the casino wins on average over time, so treat gambling as entertainment, not income.
- Mines rewards patience: cashing out early on a low-bomb board is the safest play.
- If your server has a [group](Groups-and-Social) with a Hall, the Hall Hearth and Grand Vault upgrades add a gambling bonus while you play inside the Hall.

## See also

- [Economy](Economy)
- [Groups-and-Social](Groups-and-Social)
- [Activities](Activities)
- [Commands](Commands)
