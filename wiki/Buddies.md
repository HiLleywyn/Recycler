# Buddies

Buddies are companion creatures that live alongside your Discoin economy
account. Each player keeps a small collection of buddies that level up,
gain personality, fight, breed, and earn their own token currency on the
**Buddy Network**. Your active buddy also quietly buffs the rest of the
game - chat XP, work payouts, trade rebates, fishing, farming, delves,
crafting, and battles all read bonuses from whichever buddy you have set
active.

All commands below use the `,` prefix. The command prefix is configurable
per server, so if your server uses something else, swap it in.

Open the live buddy panel any time with `,buddy` (or `,buddy stats`). The
panel has Feed / Pet / Talk / Refresh buttons and re-renders in place - it
is the main way you interact with your active buddy.

## Free vs Premium

Basic buddy management is **free everywhere**: hatching, renaming, storage,
the BUD token economy, the leaderboard, the buddy shop, and buddy staking.

The following subsystems are **Premium** (a per-server subscription - see
[Premium](Premium)):

- Buddy battles and the arena (`,buddy battle`, `,buddy arena`, the battle
  leaderboard)
- Buddy breeding, eggs, and the nest (`,buddy nest`, `,buddy egg`)
- The buddy auction house / marketplace (`,buddy gift`, `,buddy list`,
  `,buddy buy`, `,buddy market`)
- `,buddy talk` (AI chat with your buddy)

Premium-gated sections are marked **(Premium)** below.

## Getting your first buddy

Use `,buddy hatch` to hatch a buddy. Your first **3 lifetime hatches are
free**. After that, hatching costs money (starting at $10,000) and the
price doubles for each paid hatch in a row; wait 7 days without hatching
and the price resets back to base. Your first buddy becomes active
automatically; later hatches slot in as resting collection members that
you promote from the panel.

There are several other ways to obtain buddies:

- **Eggs** - eggs you collect from fishing or from the nest can be hatched
  with `,buddy egg hatch` (or `,buddy hatch`).
- **Capture** - wild buddies caught while fishing, farming, or delving
  auto-route into an open battle slot, then storage, then are rejected if
  both are full.
- **Adoption** - browse the server shelter with `,buddy shelter` and adopt
  a surrendered buddy with `,buddy adopt <id>`.
- **Auction house** - buy a buddy listed by another player (Premium).

If a buddy you owned was lost to a server leave or ban, you can get it back
within 24 hours with `,buddy reclaim`.

| Command | What it does |
|---|---|
| `,buddy hatch` | Hatch a new buddy (first 3 free, then doubling cost) |
| `,buddy reroll` | Reroll a fresh hatch, free up to 3 times (old buddy discarded) |
| `,buddy adopt <id>` | Adopt a buddy from the shelter |
| `,buddy shelter` | Browse adoptable buddies in this server's shelter |
| `,buddy surrender` | Send your active buddy to the shelter (irreversible) |
| `,buddy reclaim` | Recover a buddy lost to a leave/ban within 24h |

## Stats, types, rarities, and leveling

Every buddy has a **species** (flavor: appearance, ability, and a bonus
lane) and a **rarity tier** rolled independently at hatch time. There are
27 species, each tied to an affinity type used for expeditions and the
roster filter (Forest, Reef, Mine, Ruins, and others). Browse the full
roster with `,buddy species`, which has a type-select dropdown.

There are 5 rarity tiers, from Common up to Legendary. Rarer buddies have
higher base HP/ATK, slower mood decay, faster energy regen, more chat XP,
stronger abilities, and more "signature" bonus lanes. Any species can roll
at any tier.

Buddies gain XP from chat activity and level up to a maximum of level 50.
Each level grants one stat point. Spend points with `,buddy upgrade`
across three tracks:

- **Hardiness** - more max HP
- **Power** - more attack
- **Vigor** - more speed

Allocations are sticky - they survive species swaps and level changes.
Reset all spent points with `,buddy respec` (costs USD, doubling per
respec) so you can reallocate from scratch.

| Command | What it does |
|---|---|
| `,buddy` / `,buddy stats` | Open the live buddy panel |
| `,buddy species` | Interactive roster of every species, filter by type |
| `,buddy upgrade` | Spend earned stat points (Hardiness / Power / Vigor) |
| `,buddy respec` | Refund all spent stat points (USD, doubling per use) |
| `,buddy rename <name>` | Rename your active buddy (flat USD fee) |
| `,buddy swap <species>` | Pay to change species; keeps level, stats, rarity |
| `,buddy find [query]` | Locate one of your buddies by id, name, or species |

## Mood, feeding, and care

Each buddy tracks **hunger**, **happiness**, and **energy**. These mood
stats decay over time when the buddy is left alone, and a well-cared-for
buddy slowly regenerates energy on its own. If hunger or happiness drops to
0, the buddy's bonus multiplier is cut, and a neglected buddy can eventually
run away to the shelter.

Care for your buddy from the live panel using the Feed, Pet, and Talk
buttons - each one nudges the mood stats up and is on a short cooldown so
the panel cannot be spammed.

## Buddy shop and gear

The **buddy shop** (`,buddy shop`) sells capacity upgrades and a battle
attractor, all priced in BUD and bought with a quick-buy modal:

- **Battle slot** - one extra active slot (`,buddy slot battle buy`)
- **Storage slot** - more buddy storage rows (`,buddy slot storage buy`)
- **Egg storage** - more banked-egg rows (`,buddy slot eggs buy`)
- **Nest slot** - one more simultaneous nest (`,buddy slot nest buy`)
- **Battle attractor** - a timed buff for wild encounters (`,buddy attractor buy`)

`,buddy slot` shows the current state of all four capacity ladders.

**Gear** equips items into two slots - an `accessory` (cosmetic) and a
`charm` (passive bonus). The starter gear shop (`,buddy gear shop`) sells
three tiers of basic gear (Apprentice / Initiate / Adept) priced in DSD;
stronger gear is crafted (see [Shop and Items](Shop-and-Items)). Buying or
equipping gear is buy-and-equip - it lands directly on your active buddy
and replaces whatever was in that slot, with no refund.

| Command | What it does |
|---|---|
| `,buddy shop` | Browse the buddy shop (slot upgrades + attractor, BUD) |
| `,buddy slot` | Show all four capacity ladders |
| `,buddy gear` | Show your active buddy's equipped gear |
| `,buddy gear shop` | Browse starter gear sold for DSD |
| `,buddy gear buy <item>` | Buy and equip a starter gear item |
| `,buddy gear equip <item>` | Equip a gear item you already have |
| `,buddy gear unequip <slot>` | Remove gear from accessory or charm |

## Storage

Stored buddies do not count against your battle slot cap, do not decay, and
cannot fight until withdrawn. Use storage to keep spare collectible buddies
without surrendering them.

| Command | What it does |
|---|---|
| `,buddy storage` | Button-driven storage panel (deposit / withdraw / eggs) |
| `,buddy store <id>` | Stash an owned buddy into storage |
| `,buddy retrieve <id>` | Withdraw a stored buddy back into your owned pool |
| `,buddy storage eggs` | Browse banked eggs (held + buddy egg storage) |

## Battles, bosses, and tournaments (Premium)

**Buddy battles** are turn-based PvP between your active buddy and another
player's. Stats, level, and species ability decide the winner, and the
play-by-play renders as a battle scene with HP bars and action buttons.
Friendly duels pay XP and USD; staked duels have both sides ante the same
amount and the winner takes the pot (draws refund both stakes).

The **arena** sends your buddy against level-matched AI on the Buddy
Network. Wins mint **BUD** into your wallet; losses cost nothing. Lifetime
arena wins climb a tier ladder that multiplies your BUD reward. There is
also a once-per-day **arena boss** with a large payout.

The **arena map** is a branching world of zones you travel between, with
tier-matched fights, region bosses, special locations (item shop, healer,
trader, daily dig), and a battle consumable inventory. The **Champion
Tournament** is a bracket you play through against scaling AI.

| Command | What it does |
|---|---|
| `,buddy battle` | Battle hub - rules and how to fight |
| `,buddy battle fight @rival [amount]` | Challenge a player (optionally staked) |
| `,buddy arena` | Arena hub - mechanics and tier ladder |
| `,buddy arena fight` | Queue a PvE arena fight for BUD |
| `,buddy arena boss` | Fight the once-per-day arena boss |
| `,buddy arena lb` | Arena leaderboard |
| `,buddy arena streaks` | Arena win-streak rankings |
| `,buddy map` | Show the arena map (current zone + neighbours) |
| `,buddy map travel <zone>` | Travel to a neighbouring zone |
| `,buddy map battle` | Fight a tier-matched AI in your current zone |
| `,buddy map boss` | Fight the boss of your current zone |
| `,buddy map items` | List your battle consumable inventory |
| `,buddy map visit` | Use the special location at your current zone |
| `,buddy tourney` | Show the Champion Tournament bracket |
| `,buddy tourney start` | Begin or resume the bracket |
| `,buddy tourney fight` | Play the current bracket round |
| `,buddy battles` | Server leaderboard ranked by battle wins |

## Breeding, the nest, and eggs (Premium)

The **nest** lets you breed buddies. Deposit two owned parents (each at
least level 5, opposite genders) into a nest slot and pay a BUD fee; after
an incubation period an egg becomes ready to collect. The egg's rarity is
rolled at deposit but stays hidden until it hatches. Nest slot capacity
starts at 1 and can be raised in the buddy shop.

Collected and caught eggs go into your egg storage - a small **held**
container that overflows automatically into a larger **banked** container.
Eggs hatch into new buddies with `,buddy egg hatch`.

| Command | What it does |
|---|---|
| `,buddy nest` | Show the status of every nest slot |
| `,buddy nest deposit <id1> <id2>` | Start incubating an egg from two parents |
| `,buddy nest collect [slot]` | Collect a ready egg |
| `,buddy nest cancel [slot]` | Abandon a nest slot (fee is not refunded) |
| `,buddy egg` | Open the egg picker panel (hatch / sell / gift) |
| `,buddy egg hatch [species]` | Hatch the oldest held egg |
| `,buddy egg deposit [n] [species]` | Move held eggs into banked storage |
| `,buddy egg withdraw [n]` | Move banked eggs back to held |
| `,buddy egg sell [amount]` | Sell held eggs for LURE |
| `,buddy egg gift @user [amount]` | Gift held eggs to another player (Premium) |

## Buddy staking and the Buddy Network economy

The Buddy Network uses several tokens: **BUD** (the Buddy coin), **FREN**,
and **BBT** (the Buddy Battle Token). Stake FREN or BBT to passively earn
BUD, then spend BUD on shop upgrades or cash it out.

- `,buddy stake` opens the unified stake panel; `,buddy stake fren <amt>`
  or `,buddy stake bbt <amt>` lock tokens, and `,buddy stake everything`
  locks all of both at once.
- `,buddy claim` collects your accrued BUD yield without unstaking.
- `,buddy unstake` releases staked tokens.
- `,buddy convert` is a burn-swap between BUD and various partner tokens.
- `,buddy cashout` burns BUD to credit your USD wallet at the oracle price.
- `,buddy quote` and `,buddy pools` preview swap rates and view markets.

| Command | What it does |
|---|---|
| `,buddy stake [sym] [amt]` | Stake FREN/BBT to earn BUD, or show the panel |
| `,buddy unstake` | Release staked tokens |
| `,buddy claim` | Claim accrued BUD yield |
| `,buddy convert` | Burn-swap BUD against a partner token |
| `,buddy quote` | Preview a swap rate |
| `,buddy pools` | View Buddy Network swap pools |
| `,buddy cashout [amount]` | Burn BUD for USD wallet credit |

See [DeFi](DeFi) and [Economy](Economy) for how staking and burn-swaps fit
the wider economy.

## Auction house and gifting (Premium)

Buddy and egg trading has been consolidated into the server-wide auction
house. Use `,ah list buddy <id_or_name> <price>` to list a buddy, `,ah` to
browse, and `,ah buy <listing_id>` to purchase. The older `,buddy list`,
`,buddy market`, `,buddy buy`, and `,buddy delist` commands now point you
to the auction house (and only drain old legacy listings).

You can also gift a buddy directly to another player with `,buddy gift
@user [id]` for a flat USD fee.

## Talking to your buddy (Premium)

`,buddy talk [message]` chats with your active buddy using AI. The buddy
remembers the conversation and reacts in character to whatever you say.
Talking also nudges your buddy's mood up, and shares a cooldown with the
Talk button on the panel.

## See also

- [Activities](Activities) - fishing, farming, and delves, where wild
  buddies and eggs can be found
- [Premium](Premium) - which features need a server subscription
- [Progression](Progression) - levels, XP, and how buddies buff your account
- [Commands](Commands) - the full Discoin command reference
