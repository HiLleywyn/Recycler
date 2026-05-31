# Prediction Markets

Prediction markets let you bet on real-world outcomes, Polymarket-style. Winnings are proportional to your share of the winning pool (parimutuel system).

The predictions module can be toggled on/off per server by admins using `admin module predictions`.

## How It Works

1. An admin creates a market with a question (e.g., "Will MTA hit $200k by July?")
2. Players bet on **YES** or **NO** using USD from their wallet
3. When the outcome is known, the admin resolves the market
4. Winners split the total pool proportional to their bet size (minus 5% house cut)
5. Winners receive a DM notification when the market is resolved (if `predictions` DMs are enabled)

### Payout Example

```
Market: "Will MTA hit $200k by July?"
  YES pool: $5,000  |  NO pool: $3,000
  Total: $8,000

If YES wins:
  Payout pool = $8,000 - 5% = $7,600
  A player who bet $500 on YES gets: ($500 / $5,000) * $7,600 = $760
```

## Commands

### Browse markets

See all open prediction markets:

```
.predict list
```

### View market details

View a market's question, odds, pool sizes, and your bets:

```
.predict view <id>
```

### Place a bet

Bet USD on a market outcome:

```
.predict bet <id> <YES|NO> <amount>
```

Bets come from your wallet balance. You can bet multiple times on the same market.

### Check your bets

See all your active bets across all markets:

```
.predict mybets
```

## Tips

- **Early bets get better odds**  -  the less money in your chosen pool, the higher your share of the payout
- **Diversify**  -  you can bet on both YES and NO to hedge
- **Watch the odds**  -  `.predict view` shows implied probabilities based on current pool sizes
- The 5% house cut goes to the server treasury

## DM Notifications

Enable prediction result notifications with:

```
.notify predictions on
```

You will receive a DM when a market you bet on is resolved, showing whether you won and your payout amount.

## Admin Controls

Admins can manage prediction markets with:

- `.admin predict create <question>`  -  create a new market
- `.admin predict resolve <id> <YES|NO>`  -  resolve with winning outcome
- `.admin predict cancel <id>`  -  cancel and refund all bets
- `.admin predict close <id>`  -  close to new bets
- `.admin predict list`  -  list all markets
- `.admin module predictions on|off`  -  toggle the predictions module
