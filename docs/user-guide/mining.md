# Mining

Mining is Discoin's proof-of-work system. You buy rigs, assign them to a PoW network (SUN or MTA), and earn block rewards proportional to your hashrate share. Mining runs automatically in the background -- you just need to own rigs.

## Networks

There are two mineable networks:

| Network | Token | Target Block Time | Initial Reward | Initial Difficulty |
|---|---|---|---|---|
| **Sun Network** | SUN | 10 minutes | 50 SUN/block | 60,000 MH-s |
| **Moneta Chain** | MTA | 10 minutes | 6.25 MTA/block | 1,000,000 MH-s |

SUN is the easier network to mine with mid-tier rigs. MTA requires high-end GPUs or ASICs to be competitive.

### Halvings

Block rewards halve every **210,000 blocks** on both networks. SUN has a floor of 0.001 SUN per block; MTA has a floor of 1 satoshi (0.00000001 MTA).

### Difficulty retargeting

Difficulty adjusts automatically:

- **SUN**: retargets every 144 blocks (~1 day)
- **MTA**: retargets every 2,016 blocks (~2 weeks)

When more miners join, difficulty increases. When miners leave, it decreases. This keeps block times near the 10-minute target.

## Mining Rigs

All mining commands live under `.chain mine`. View available rigs:

```
.chain mine rigs
```

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

!!! tip "Rig slot limits"
    Your job tier determines how many rigs you can own. You start with **2 slots** as Homeless and unlock up to **128 slots** as Exploiter. Promote to expand your mining operation.

## Buying and Selling Rigs

Buy a rig:

```
.chain mine buy <rig_id>
```

Example:

```
.chain mine buy GTX1060
```

Sell a rig:

```
.chain mine sell <rig_id>
```

Rigs are bought with USD from your wallet.

## Assigning Rigs to a Network

By default, new rigs mine the Sun Network. Reassign a rig to a different PoW network:

```
.chain mine assign <network>
```

Networks: `sun` or `mta`

Example:

```
.chain mine assign mta
```

## Mining Modes

You can mine in three modes:

### Solo mining

Mine independently. You keep the full block reward when you find a block, but blocks may take longer if your hashrate is small:

```
.chain mine solo
```

!!! note "Solo share cap"
    A single solo miner is capped at earning **20% of SUN** or **15% of MTA** network block rewards. This prevents one whale miner from taking everything.

### Pool mining

Join a public mining pool. Rewards are shared proportionally among all pool members:

```
.chain mine pool
```

Pool mining provides more consistent income than solo mining. Rewards are distributed proportionally to your hashrate share.

### Group mining

Join a private mining group for shared rewards and group bonuses:

```
.chain mine group
```

Mining groups can purchase upgrades that benefit all members (see below).

## Mining Status

Check your current mining setup and earnings:

```
.chain mine status
```

Shows your rigs, total hashrate, assigned network, mining mode, and recent earnings.

View your mining history:

```
.chain mine history
```

Check network-wide mining stats:

```
.chain mine network
```

Aliases: `.chain mine net`

## Electricity Costs

Rigs consume electricity, charged in USD per kWh each mining tick:

- **SUN Network**: $0.16/kWh
- **MTA Network**: $0.22/kWh

Each additional rig scales electricity cost by 8% (diminishing returns on stacking many rigs). Mining group upgrades like Solar Panels can reduce electricity costs.

## Warmup Period

New networks have a warmup period where block rewards ramp from 0% to 100%:

- **SUN**: 200 blocks
- **MTA**: 500 blocks

This prevents early miners from earning full rewards before the network stabilizes.

## Mining Groups

Mining groups let players collaborate. The group founder can invite members and purchase upgrades for the group using SUN from the group treasury.

### Group upgrades

Groups can purchase tiered upgrades:

| Upgrade | Tier | Cost (SUN) | Effect |
|---|---|---|---|
| Overclock Module | 1-3 | 300 - 4,000 | +8% to +38% hashrate |
| Reward Splitter | 1-2 | 500 - 2,000 | +5% to +13% block rewards |
| Barracks Expansion | 1-2 | 250 - 1,000 | +5 to +15 member slots |
| XP Amplifier | 1-2 | 400 - 1,800 | +12% to +32% item XP |
| Lucky Drill Bit | 1-2 | 600 - 2,500 | +5% to +13% critical block chance |
| Solar Panels | 1-3 | 200 - 3,000 | 12% to 55% electricity reduction |

Each upgrade tier requires the previous tier. Effects stack additively across all purchased upgrades.

## Hashstone

The **Hashstone** is a special item that boosts your mining. It levels up as you mine, providing scaling bonuses. See the [Shop page](shop.md) for details.
