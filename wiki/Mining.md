# Mining

Mining is Discoin's proof-of-work system. You buy mining rigs, assign them to a
PoW network, and earn block rewards in proportion to your share of the network
hashrate. Mining runs in the background - once you own rigs and pick a mode, you
keep earning whether or not you are typing commands.

> The command prefix shown here is a comma (`,`). The prefix is configurable
> per server, so your server may use a different one.

## What you mine

There are two mineable proof-of-work networks, and they pay out their native
coins **SUN** and **MTA**. Neither coin can be bought or swapped - mining (or the
faucet) is the only way to bring them into existence.

| Network | Coin | Target block time | Difficulty retarget | Designed for |
|---|---|---|---|---|
| Sun | SUN | 10 min | every 144 blocks (~1 day) | mid-tier GPUs |
| Moneta | MTA | 10 min | every 2,016 blocks (~2 weeks) | A100 / H100 / ASIC class |

Both networks halve their block reward every 210,000 blocks, just like real
Moneta. SUN is the casual, accessible chain. MTA starts at a much higher
difficulty floor, so it needs high-end rigs to be competitive but pays a
premium coin.

### Difficulty retargeting

Difficulty adjusts on a fixed schedule to keep block times near the 10-minute
target. When more hashrate joins a chain, difficulty rises; when miners leave,
it falls. MTA's longer 2,016-block window means it adjusts more slowly and
rewards patient, well-equipped miners.

## Mining commands

All mining commands live under the `,chain mine` group.

| Command | Action |
|---|---|
| `,chain mine rigs` | Rig catalog plus the quantities you own |
| `,chain mine buy <RIG_ID> [qty] [mta\|sun]` | Buy rigs |
| `,chain mine sell <RIG_ID> [qty\|all]` | Sell rigs back at 50% of price |
| `,chain mine assign <qty\|all> <RIG_ID> <mta\|sun>` | Move rigs between chains |
| `,chain mine status` | Your hashrate, mode, and earnings |
| `,chain mine history` (`hist`) | Your last 10 blocks |
| `,chain mine solo` / `pool` / `group` | Switch your mining mode |
| `,chain mine network <net>` (`net`) | Network-wide stats |

Examples:

```
,chain mine buy RTX4090 2 mta     buy 2 rigs, assigned to MTA
,chain mine assign 5 RTX3090 mta  move 5 RTX 3090s to MTA
,chain mine assign all H100 sun   move all H100s to SUN
```

## Rigs

Rigs are graphics cards and ASICs. Higher tiers cost more but provide far more
hashrate, which is your share of the network's mining power.

| Rig | Tier | Hashrate (MH/s) | Power (W) | Price (USD) |
|---|---|---|---|---|
| GTX 1060 | 1 | 15 | 120 | $1,800 |
| GTX 1080 | 2 | 40 | 150 | $4,500 |
| RTX 2080 | 3 | 110 | 180 | $11,000 |
| RTX 3090 | 4 | 320 | 270 | $30,000 |
| RTX 4090 | 5 | 950 | 340 | $80,000 |
| A100 PCIe | 6 | 3,500 | 300 | $280,000 |
| H100 NVL | 7 | 15,000 | 550 | $1,100,000 |
| Antminer S19 | 8 | 70,000 | 3,200 | $4,800,000 |

Mid-tier GPUs (RTX 2080 through RTX 4090) are viable on SUN. MTA's high
difficulty floor means the A100, H100, and the Antminer S19 ASIC are where it
becomes worthwhile - the ASIC is the ideal MTA tier.

Selling a rig refunds 50% of its purchase price, so think of the other half as
the cost of running it.

### Rig slots

You can only run as many rigs as your **rig slot** count allows, and that count
is tied to your [job](Economy) tier. The entry-level job starts you with 2
slots; the top job tier unlocks up to 128. Promote your job to expand your
mining operation.

## Mining modes

Switch modes any time with `,chain mine solo`, `pool`, or `group`.

| Mode | How rewards work | Best for |
|---|---|---|
| Solo | Individual block rolls, full reward per block, high variance | Big hashrate, comfortable with swings |
| Pool | Steady proportional income, low variance (default for new miners) | Most players, predictable earnings |
| Group | Pooled within your mining group, split by the group's weight mode | Coordinated groups |

Group mining needs **2 or more active members** in the group before it pays
out. See [Groups and Social](Groups-and-Social) for how mining groups work.

## Hashrate and earnings

Your reward share on a chain is your hashrate divided by the total hashrate
mining that chain. Add rigs to grow your share, or assign rigs to a thinner
network where competition is lower. Network anti-dominance caps limit how much
of a chain a single miner or group can capture, so spreading out can pay off.

Mining also earns **Hashstone XP** on every PoW chain, proportional to your
hashrate share. The Hashstone is a leveled item that boosts your mining and work
output - see [Shop and Items](Shop-and-Items) and [Progression](Progression).

## The crypto faucet

The faucet is free crypto, dropped on a timer in a dedicated channel - it is the
easiest way for a new player to get their first SUN or MTA without owning a rig.

- A faucet appears automatically on a regular interval and stays claimable for a
  short collect window. Be quick: click before the window closes.
- Everyone who claims gets an **equal share in USD value**, then that value is
  converted into a **random token** for each claimer. You might land MTA, DSC,
  ARC, SUN, or something else.
- Group (community) tokens are in the rotation too, but roll in at half USD
  value.
- Players can also start an `,airdrop <amount> [symbol]` to donate their own
  tokens as a public drop where every claimer gets an equal share of a fixed
  token.

Faucet payouts auto-scale with the server's economy: a poor server gets generous
drops, a wealthy or supply-heavy server gets smaller ones. A server admin must
set the faucet channel with `,admin setchannel drops` for it to appear.

## See also

- [Trading](Trading)
- [Staking and Validators](Staking-and-Validators)
- [Groups and Social](Groups-and-Social)
- [Progression](Progression)
