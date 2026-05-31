# Activities

Discoin's activities are self-contained minigames that sit on top of the core
economy. Each one is a small world with its own network, its own earn-only
tokens, and its own gameplay loop. They give you something to do beyond charting
token prices, and everything they produce eventually feeds back into your net
worth, your [Progression](Progression), and the [Shop](Shop-and-Items).

All five activities below are **Premium-gated**: a server needs an active
Premium subscription (or to be the host server) before they can be used. See
the [Premium](Premium) page for the full free-versus-paid breakdown and how to
subscribe.

> Command examples use the `,` prefix. The prefix is configurable per server,
> so yours may differ.

## Fishing (Lure Network)

Fishing is a timing minigame. You cast a line, watch the animated frames, and
hit the **HOOK** button the moment **STRIKE!** appears. Hook inside the
sweet-spot window for a size and payout bonus; miss it and the fish gets away.
Catching fish back to back builds a combo multiplier that resets on a miss or
after an hour idle.

| Command | What it does |
|---|---|
| `,fish` (alias `,cast`) | Cast your line |
| `,fish inv` | Show fish, junk, and bait you are holding |
| `,fish sell all` / `,fish sell junk` / `,fish sell <key>` | Sell catches for cash |
| `,fish shop` / `,fish buy rod` / `,fish buy <bait> <qty>` | Browse and buy rods and bait |
| `,fish bait <key\|none>` | Equip or unequip bait |
| `,fish trap` / `,fish trap place <key>` / `,fish trap collect` | Place and haul crab traps |
| `,fish zones` / `,fish zone <key>` | List and switch fishing zones |
| `,fish stats [@user]` / `,fish lb` / `,fish lb biggest` | Stats and leaderboards |

The loop: equip a rod and bait, cast, hook, sell your haul, then reinvest into
better rods and deeper zones. Better rods unlock harder zones that pay more and
hold rarer fish. You can pull up fish (Common to Legendary), junk that salvages
for small change, money bags and mystery boxes that pay cash straight to your
wallet, and very rarely a buddy egg that hatches a water-type buddy. Tier-6 and
deeper zones can roll sea monster encounters, and rod augments add line, lure,
and reel bonuses. Fishing earns the **LURE** token; legendary catches splash to
the whole server.

## Delve Crawler (Crypt Network)

The Delve Crawler is an ASCII dungeon crawler. You pick a permanent class
(warrior, mage, or rogue), start a run on Floor 1, and advance room by room
through mobs, ore veins, shrines, chests, stairs, and bosses.

| Command | What it does |
|---|---|
| `,delve class warrior\|mage\|rogue` | One-time permanent class pick |
| `,delve start` / `,delve next` / `,delve descend` / `,delve rest` | Run lifecycle |
| `,delve attack` / `,delve skill` / `,delve flee` / `,delve capture` | Combat |
| `,delve mine` / `,delve open` | Mine ore veins, crack chests |
| `,delve shop` / `,delve buy` / `,delve equip` / `,delve inv` | Gear and inventory |
| `,delve stake` / `,delve swap` / `,delve cashout` | Crypt Network token economy |
| `,delve party` / `,delve summon` / `,delve release` | Manage captured buddies |
| `,delve arena` | Ranked PvP using your delve combat profile |

The loop: dive, fight, mine, loot, descend, and rest at the surface to bank your
progress. Floors 5, 10, 15, and 20 are bosses. The Crypt Network has four
earn-only tokens: **COPPER**, **SILVER**, and **GOLD** ore tiers plus the
network coin **RUNE**. Mine ore, swap or stake it for RUNE yield, and cash RUNE
out to your USD wallet. Mobs taken below 30 percent HP can be captured as
buddies. Delve Arena adds ranked PvP that reuses your build.

## Farming (Harvest Network)

Farming is a plant-and-wait minigame driven by seasons and weather. You start
with a free plot tile in the Meadow, buy seed packets, plant them, optionally
water and fertilize, then harvest when crops ripen.

| Command | What it does |
|---|---|
| `,farm` | Open the field view (plots, weather, season) |
| `,farm plant <slot> <crop>` / `,farm plant all <crop>` | Sow seed packets |
| `,farm water [slot]` / `,farm fertilize <slot>` | Tend your tiles |
| `,farm harvest [slot]` | Harvest ripe tiles |
| `,farm shop` / `,farm buy plot` / `,farm buy seed <crop> <qty>` | Buy plots, seeds, fertilizer |
| `,farm zones` / `,farm zone <key>` / `,farm crops` | Zones and the crop catalog |
| `,farm sell <crop\|all>` / `,farm process <recipe>` / `,farm bag` | Market and processing |
| `,farm stake` / `,farm swap` / `,farm cashout` | Harvest Network token economy |

The loop: plant in-season for a yield bonus, weather the rolls (sunny, rain,
drought, locusts, and more), harvest, sell, and reinvest into more tiles and
better tools. Higher zones unlock rarer crops but harsher conditions. Bad
weather can spawn pests you battle with the same combat buttons as delves;
captured pests become grass-type buddies. The Harvest Network uses **HRV** (the
network coin) and the earn-only **SEED** token, both of which can be staked,
swapped, and cashed out.

## Crafting (Forge Network)

Crafting is the sink that ties the other activities together. Fishing, farming,
and delves all produce stacks of surplus material; the Forge Network lets you
combine those inputs into useful crafted items.

| Command | What it does |
|---|---|
| `,craft` | Forge view (level, balances, stake) |
| `,craft list [specialty]` / `,craft book` / `,craft info <key>` | Browse recipes |
| `,craft make <key> [qty]` | Consume inputs, mint INGOT, deposit the output |
| `,craft apply <key> [qty]` | Spend a crafted item back into its source game |
| `,craft bag` / `,craft history` | Crafted-item inventory and recent crafts |
| `,craft specialties` / `,craft specialize <key>` / `,craft despecialize <key>` | Manage specialties |
| `,craft stake` / `,craft swap` / `,craft claim` / `,craft cashout` | Forge token economy |

The loop: gather inputs from your other activities, run a recipe, then apply the
output where it belongs (bait to fishing, fertilizer to farming, potions to
delves, treats to buddies). Crafting splits across six specialty tracks
(Smithing, Alchemy, Cooking, Fletching, Tinkering, Enchanting); you can hold two
active specialties at once. Recipes pay out **INGOT** (the earn-only crafting
reward), charge a small **FGD** stable fee, and INGOT can be staked or swapped
into **FORGE**, the Forge Network coin, which cashes out to USD.

## Expeditions

Expeditions are the hands-off activity. Instead of an active minigame, you send
your active buddy on a 1 to 12 hour autonomous run to one of four destinations
(Whispering Forest, Coral Reef, Forgotten Mine, Ancient Ruins). They come back
with a procedural story log, a weighted loot drop, and some XP.

| Command | What it does |
|---|---|
| `,expedition` (aliases `,exped`, `,trek`) | Status panel: active runs, pending collects, send button |
| `,expedition send` | Open the picker to choose a buddy and destination |
| `,expedition collect [id]` | Collect a finished run, or all ready runs when no id is given |
| `,expedition history` | Last 10 collected runs |

The loop: pick a buddy, pick a destination, wait, then collect. Loot pools draw
from the fishing, farming, and delve economies, so expeditions are a passive way
to top up materials while you do other things. A buddy whose species matches a
destination's affinity gets a loot quantity and rarity bonus. Because buddies
are involved, expeditions pair naturally with the [Buddies](Buddies) system.

## See also

- [Premium](Premium) - what unlocks these activities and how to subscribe
- [Progression](Progression) - achievements, quests, mastery, and seasons that activities feed
- [Buddies](Buddies) - raising the companions you send on expeditions
- [Commands](Commands) - the full command reference
