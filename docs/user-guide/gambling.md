# Gambling

Discoin features six gambling games, all under the `.play` command group. All games use a cryptographically secure random number generator (`secrets.SystemRandom`) for provably fair outcomes.

## General Rules

- **Minimum bet**: $1.00
- **Maximum bet**: $500,000
- **House edge**: 5% across all games
- All bets are placed with USD from your wallet

## Games

### Coinflip

Simple heads or tails. 50/50 odds, pays 1.9x (after house edge):

```
.play coinflip <amount>
```

Aliases: `.play cf`, `.play flip`, `.cf`, `.flip`

### Dice

Roll a number 1-100. You win if the roll is above a threshold:

```
.play dice <amount>
```

Alias: `.dice`

### Slots

Spin a 3-reel slot machine with symbols: cherry, lemon, orange, grapes, diamond, and 7:

```
.play slots <amount>
```

Aliases: `.play sl`, `.sl`

Payouts scale with the rarity of the match:

- Three 7s: highest payout
- Three diamonds: second highest
- Mixed matches: lower payouts

### Roulette

Bet on a standard 37-number roulette wheel (0-36):

```
.play roulette <amount> <bet_type> [detail]
```

Alias: `.play rou`, `.rou`

Bet types:

| Bet Type | Example | Payout |
|---|---|---|
| `number` | `.play roulette 100 number 17` | 35:1 |
| `red` | `.play roulette 100 red` | 1:1 |
| `black` | `.play roulette 100 black` | 1:1 |
| `odd` | `.play roulette 100 odd` | 1:1 |
| `even` | `.play roulette 100 even` | 1:1 |
| `dozen` | `.play roulette 100 dozen 2` | 2:1 |
| `column` | `.play roulette 100 column 1` | 2:1 |

### Blackjack

Play a hand of blackjack against the dealer:

```
.play blackjack <amount>
```

Aliases: `.play bj`, `.bj`

Interactive buttons appear for **Hit** and **Stand**. Standard blackjack rules apply:

- Face cards (J, Q, K) count as 10
- Aces count as 11, reduced to 1 if you would bust
- Dealer stands on 17
- Blackjack (21 with first 2 cards) pays bonus
- 60-second timeout per hand (auto-stand on timeout)

### Mines

An interactive grid game. A 5x5 grid (24 tiles + cash out button) hides a number of bombs. Reveal safe tiles to increase your multiplier, or cash out at any time:

```
.play mines <amount> [bombs]
```

Alias: `.mines`

- Default bomb count: **3**
- Bombs range: **1 to 20**
- 24 total tiles, grid is 5 columns by 5 rows (bottom-right is Cash Out)
- Each safe tile increases your multiplier using a provably fair formula
- Hit a bomb and you lose your bet
- Cash out at any time to lock in your winnings (capped at max bet)
- 120-second timeout (auto-cash-out)

Example with 5 bombs:

```
.play mines 1000 5
```

!!! tip "Risk vs reward"
    More bombs = higher multiplier per safe tile, but higher chance of hitting one. Fewer bombs = safer but smaller payouts.

## Gambling Stats

View your gambling history and performance:

```
.play stats
```

Aliases: `.play history`, `.play gambstats`, `.gambstats`

Shows your total games played, wins, losses, and net profit/loss across all game types.

## Provably Fair

All gambling outcomes use Python's `secrets.SystemRandom`, a cryptographically secure RNG backed by the operating system's entropy source. This is not predictable or seedable like the standard `random` module.

- Mines bomb placements use `secrets.SystemRandom.sample()`
- Card draws, dice rolls, and roulette spins all use the secure RNG
- No server seed manipulation is possible

## Anti-Bot Protection

Gambling commands include anti-bot CAPTCHA checks. If the system detects automated or suspiciously fast gameplay, it may present a verification challenge before allowing the bet to proceed.

## Max Bet Scaling

The maximum bet is a hard cap of **$500,000** per wager. Mines payouts are also capped at the max bet amount, regardless of the multiplier achieved.

!!! note "Stone bonuses"
    The Gambastone (which would have reduced house edge) and Charm (timed gambling buff) are currently **disabled**. Check the shop for the latest available items.
