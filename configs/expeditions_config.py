"""
expeditions_config.py  -  AI Buddy Expedition catalog.

Configures the destinations a buddy can be deployed to, the species
affinity table that lets a Reef-aligned buddy hit harder on the Coral
Reef, the duration ladder + reward scaling, and the procedural story
event templates the collect path samples to render the run log.

Pure data + tiny pure helpers. The service (``services/expeditions.py``)
imports from here at runtime and re-imports cheap; no DB or network IO
lives in this module.
"""
from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------
# Each destination is one row on the ``,expedition send`` dropdown. The
# loot_weights dict gives the relative chance of each loot bucket on a
# single draw. ``species_affinity`` is the species-affinity tag that
# triggers the +25% loot quantity / +1 rarity bias for matching buddies.

DESTINATIONS: Final[dict[str, dict]] = {
    "forest": {
        "key":          "forest",
        "name":         "Whispering Forest",
        "emoji":        "\U0001F332",                  # evergreen tree
        "min_level":    1,
        "blurb":        "Quiet, dappled, full of curious herbs.",
        # Reweighted May 2026: dead-loot draws (`nothing`) cut in half so
        # players feel like every expedition pulls something. Productive
        # buckets pick up the slack proportionally.
        "loot_weights": {
            "crop":    0.50,
            "fish":    0.12,
            "ore":     0.06,
            "rune":    0.12,
            "junk":    0.15,
            "nothing": 0.05,
        },
        "species_affinity": "forest",
    },
    "reef": {
        "key":          "reef",
        "name":         "Coral Reef",
        "emoji":        "\U0001FAB8",                  # coral
        "min_level":    3,
        "blurb":        "Warm currents and unusually social fish.",
        "loot_weights": {
            "fish":    0.60,
            "junk":    0.15,
            "rune":    0.12,
            "crop":    0.04,
            "ore":     0.04,
            "nothing": 0.05,
        },
        "species_affinity": "reef",
    },
    "mine": {
        "key":          "mine",
        "name":         "Forgotten Mine",
        "emoji":        "\U000026CF",                  # pick
        "min_level":    6,
        "blurb":        "Cold, narrow, lit by glowing ore veins.",
        "loot_weights": {
            "ore":     0.50,
            "rune":    0.22,
            "junk":    0.10,
            "crop":    0.05,
            "fish":    0.05,
            "nothing": 0.08,
        },
        "species_affinity": "mine",
    },
    "ruins": {
        "key":          "ruins",
        "name":         "Ancient Ruins",
        "emoji":        "\U0001F3DB",                  # classical building
        "min_level":    10,
        "blurb":        "Crumbling pillars, restless echoes, real loot.",
        "loot_weights": {
            "rune":    0.45,
            "ore":     0.17,
            "fish":    0.15,
            "crop":    0.10,
            "junk":    0.06,
            "nothing": 0.07,
        },
        "species_affinity": "ruins",
    },
    "volcano": {
        "key":          "volcano",
        "name":         "Smoldering Caldera",
        "emoji":        "\U0001F30B",                  # volcano
        "min_level":    15,
        "blurb":        "Scorched rock, heat shimmer, ore like you've never seen.",
        "loot_weights": {
            "ore":     0.52,
            "rune":    0.22,
            "crop":    0.12,
            "fish":    0.04,
            "junk":    0.06,
            "nothing": 0.04,
        },
        "species_affinity": "volcano",
    },
    "void": {
        "key":          "void",
        "name":         "The Void Rift",
        "emoji":        "\U0001F300",                  # cyclone / void swirl
        "min_level":    20,
        "blurb":        "Starless. Loud with silence. The loot is worth the fear.",
        "loot_weights": {
            "rune":    0.55,
            "ore":     0.18,
            "fish":    0.12,
            "crop":    0.08,
            "junk":    0.04,
            "nothing": 0.03,
        },
        "species_affinity": "void",
    },
}


def destination_meta(key: str) -> dict | None:
    return DESTINATIONS.get((key or "").lower())


# ---------------------------------------------------------------------------
# Species affinity
# ---------------------------------------------------------------------------
# Each entry maps buddies_config.SPECIES key -> its preferred destination.
# A buddy on its preferred destination gets +25% loot quantity and a
# rarity-bump on the per-draw item sample. Species not listed default to
# "neutral" (no bonus, no penalty). Tags match destination keys 1:1 so the
# comparison is a straight string equality at runtime.

SPECIES_AFFINITY: Final[dict[str, str]] = {
    # Forest -- ground / sky / mammal / plant types.
    "zenny":       "forest",
    "nimbus":      "forest",
    "fox":         "forest",
    "wolf":        "forest",
    "chungus":     "forest",
    "donkey":      "forest",
    "thornling":   "forest",
    # Reef -- aquatic / shellfish / cephalopod.
    "crab":        "reef",
    "shrimp":      "reef",
    "octopus":     "reef",
    "lobster":     "reef",
    "wecco":       "reef",
    # Mine -- rock / construct / cave-dweller.
    "cobble":      "mine",
    "glitch":      "mine",
    "robo":        "mine",
    # Ruins -- arcane / spectral / scaled.
    "pyper":       "ruins",
    "shrek":       "ruins",
    "spiderlenny": "ruins",
    "gloomer":     "ruins",
    "blazer":      "ruins",
    "draclet":     "ruins",
    # Volcano -- fire / magma / heat-resistant.
    "salamander":  "volcano",
    "ignis":       "volcano",
    "molten":      "volcano",
    # Void -- cosmic / shadow / ethereal.
    "voidling":    "void",
    "nullfox":     "void",
    "eclipse":     "void",
}


def species_affinity(species: str) -> str:
    return SPECIES_AFFINITY.get((species or "").lower(), "neutral")


# ---------------------------------------------------------------------------
# Duration ladder
# ---------------------------------------------------------------------------
# Player picks one of these from the send picker. ``draws`` is the number
# of independent loot rolls -- longer runs draw more, but per-draw odds
# are unchanged so a 12h run is just "many short runs stitched together"
# rather than a strictly better run. ``xp_gain`` and ``happiness_delta``
# are flat; the buddy comes home stronger and slightly tired regardless of
# loot luck.

DURATIONS: Final[tuple[dict, ...]] = (
    {"key": "1h",  "label": "1 hour",   "seconds": 3600,    "draws": 3,
     "xp_gain": 80,   "happiness_delta": +5},
    {"key": "4h",  "label": "4 hours",  "seconds": 14400,   "draws": 8,
     "xp_gain": 280,  "happiness_delta": +3},
    {"key": "8h",  "label": "8 hours",  "seconds": 28800,   "draws": 14,
     "xp_gain": 520,  "happiness_delta": -2},
    {"key": "12h", "label": "12 hours", "seconds": 43200,   "draws": 22,
     "xp_gain": 800,  "happiness_delta": -8},
    {"key": "24h", "label": "24 hours", "seconds": 86400,   "draws": 40,
     "xp_gain": 1400, "happiness_delta": -20},
)


def duration_meta(key: str) -> dict | None:
    for d in DURATIONS:
        if d["key"] == (key or "").lower():
            return dict(d)
    return None


# ---------------------------------------------------------------------------
# Per-bucket sampling pools
# ---------------------------------------------------------------------------
# When a draw lands on "rune" or "ore" we pay out a scalar amount; when
# it lands on a catalog bucket (crop / fish / junk) we sample one item
# key from a per-destination biased list. Bias just reorders the catalog
# so a Reef draw is more likely to surface ocean species than a Forest
# draw -- the actual catalog dicts (FISH / CROPS / JUNK) live in the
# game configs and are imported lazily inside the service.

# Per-destination favored fish keys. A run rolls in this order: favored ->
# any-other-zone-fish -> if both empty, pick whatever's in FISH.
FAVORED_FISH: Final[dict[str, tuple[str, ...]]] = {
    "forest":  ("trout", "perch", "carp", "minnow", "bass", "catfish"),
    "reef":    ("sardine", "anchovy", "mackerel", "herring", "lobster",
                "swordfish", "salmon"),
    "mine":    ("eel", "catfish", "carp"),
    "ruins":   ("pike", "swordfish", "salmon", "eel", "kraken"),
    "volcano": ("eel", "lavafish", "pike"),
    "void":    ("kraken", "leviathan", "pike"),
}

# Per-destination favored crops. Forest crops all the way; ruins gets a
# hint of late-season harvest.
FAVORED_CROPS: Final[dict[str, tuple[str, ...]]] = {
    "forest":  ("wheat", "carrot", "potato", "tomato", "corn", "pumpkin",
                "sunflower", "lavender", "mushroom"),
    "reef":    ("seaweed", "wheat", "rose"),
    "mine":    ("potato", "eggplant", "mushroom"),
    "ruins":   ("pumpkin", "sunflower", "eggplant", "pepper"),
    "volcano": ("ghost_chili", "dragonfruit", "pepper", "crystalmint"),
    "void":    ("ambrosia", "world_tree", "dreamroot", "crystalmint"),
}

# Ore picks per destination. Mine gets the full range with skew toward
# mid; ruins biases gold; reef / forest only ever drop copper trickle.
FAVORED_ORE: Final[dict[str, tuple[str, ...]]] = {
    "forest":  ("COPPER",),
    "reef":    ("COPPER",),
    "mine":    ("COPPER", "COPPER", "SILVER", "SILVER", "GOLD"),
    "ruins":   ("SILVER", "GOLD", "GOLD"),
    "volcano": ("SILVER", "GOLD", "GOLD", "GOLD"),
    "void":    ("GOLD", "GOLD", "GOLD"),
}

# Junk pool (one-of). All four destinations share the same JUNK catalog
# but the salvage value scales with destination tier so a Ruins junk
# pull pays better than a Forest one.
JUNK_POOL: Final[tuple[str, ...]] = (
    "boot", "bottle", "can", "tire", "cart", "bag", "phone", "wig", "duck",
)


# ---------------------------------------------------------------------------
# Reward magnitudes
# ---------------------------------------------------------------------------
# Per-draw payouts when a draw lands on the scalar buckets. Multiplied
# by the duration's ``draws`` counter and the affinity bonus. Tuned so
# a 12h Mine run on a mine-affinity buddy nets about 12-18 ore plus a
# few hundred RUNE and a small chance at a legendary junk -- enough to
# matter, not enough to replace active play.

ORE_PER_DRAW: Final[dict[str, tuple[int, int]]] = {
    # destination -> (min_qty, max_qty). Bumped ~30-40% in May 2026 so a
    # 12h Mine run on a mine-affinity buddy nets a meaningful per-loop
    # haul (target: 18-30 ore, 6-15 RUNE, plus the occasional legendary).
    "forest":  (2, 4),
    "reef":    (2, 4),
    "mine":    (3, 7),
    "ruins":   (2, 5),
    "volcano": (4, 10),
    "void":    (3,  8),
}

RUNE_PER_DRAW: Final[dict[str, tuple[float, float]]] = {
    "forest":  (1.0,  2.5),
    "reef":    (1.0,  3.0),
    "mine":    (2.0,  5.0),
    "ruins":   (3.0,  9.0),
    "volcano": (4.0, 12.0),
    "void":    (6.0, 20.0),
}


# ---------------------------------------------------------------------------
# Procedural story templates
# ---------------------------------------------------------------------------
# Per-destination event lines. The collect path samples N (typically the
# floor of duration.draws / 4, clamped to [3, 5]) lines, substitutes the
# buddy's name + species + a sampled loot item, and joins them with
# blank lines so each event renders as its own paragraph.
#
# {name}    -> buddy display name
# {species} -> buddy species (raw key, lowercased)
# {dest}    -> destination display name
# {item}    -> a sampled item from this run's loot (or "a glittering
#              something" if the run dropped nothing)

EVENTS: Final[dict[str, tuple[str, ...]]] = {
    "volcano": (
        "{name} skirted a lava tongue and returned with pockets full of {item}.",
        "A geyser of steam launched {name} three feet into the air. They landed on their feet, barely.",
        "{name} cooled a magma pool with sheer staring force. It did not work. They went around.",
        "{name} discovered a thermal vent venting {item} upward like a slow-motion volcano gift.",
        "A scorched salamander regarded {name} from atop a boulder. Neither blinked. {name} left first.",
        "The heat shimmer tricked {name} into chasing a mirage for six minutes. The {item} beneath it was real.",
        "{name} cracked open a cooled lava pod and found {item} inside, still warm.",
        "{name} learned that rocks near magma glow, and that glowing rocks are worth {item}.",
    ),
    "void": (
        "{name} reached into the dark and pulled out {item} from nothing in particular.",
        "A sound with no source followed {name} for twenty minutes, then left without explanation.",
        "{name} found a floating {item} suspended mid-rift, as if waiting.",
        "The void whispered something. {name} chose not to listen. Smart.",
        "{name} watched a star blink out and reappear. The {item} below it was new.",
        "A crack in space led {name} to a pocket of light containing {item} and a single, confused fern.",
        "{name} felt gravity reverse briefly. Used the moment to grab {item} from an upside-down ledge.",
        "The rift folded around {name} for a second. When it unfolded, {name} had {item} and no memory of how.",
    ),
    "forest": (
        "{name} found a patch of wild {item} growing under a rotted log.",
        "A startled deer crossed {name}'s path; {name} barked / chirped / squeaked back politely.",
        "{name} drank from a moonlit stream and felt strangely refreshed.",
        "{name} got tangled in vines and freed themselves with a triumphant shake.",
        "{name} chased a butterfly for ten full minutes. Productivity: low. Joy: high.",
        "{name} discovered an unattended picnic and helped themselves to {item}.",
        "{name} climbed a tree to see the horizon. The view was worth the climb.",
        "{name} mistook a stump for a friend and apologised after several minutes.",
        "{name} found {item} in an abandoned forager's pouch.",
    ),
    "reef": (
        "{name} dove into a coral garden and surfaced clutching {item}.",
        "{name} held a long, silent staring contest with a passing eel.",
        "A school of {item} darted past; {name} caught the slowest one.",
        "{name} navigated a kelp maze using only their nose. (Buddy noses are very sensitive.)",
        "{name} negotiated peace between a hermit crab and a clownfish. Lasting impact: doubtful.",
        "A current carried {name} past a wreck where they pried loose {item}.",
        "{name} surfaced briefly to wave at the moon and immediately dove again.",
        "{name} found a tide-pool buffet of {item} and ate dignifiedly until full.",
    ),
    "mine": (
        "{name} skidded down a rubble slope and landed atop {item}.",
        "A faint humming led {name} to a pocket vein of {item}.",
        "{name} startled a sleeping cave bat. The bat startled back. No injuries.",
        "{name} squeezed through a crack barely wider than themselves and emerged victorious.",
        "A stalactite snapped above {name}; {name} dodged with anime-protagonist grace.",
        "{name} dug into the wall on a hunch and pulled out {item}.",
        "{name} got lost for an hour, found a shortcut, and came back with {item}.",
        "{name} encountered an old miner's ghost. They had a lovely chat.",
    ),
    "ruins": (
        "{name} brushed dust off a glyph and the room briefly hummed; {item} fell from a niche.",
        "{name} solved a tile puzzle by sitting on the heaviest tile.",
        "{name} discovered an inscribed stone that read: 'do not lick the stone'. {name} did not lick it. (Probably.)",
        "A dormant guardian statue tracked {name} with empty eye-sockets. {name} did not break eye contact and won.",
        "{name} found {item} sealed inside a clay jar marked 'do not open'.",
        "{name} translated a fragment of ancient text by chewing it thoughtfully.",
        "{name} climbed a fallen pillar to inspect a fresco of an old ruler petting a buddy.",
        "An ancient mechanism activated as {name} passed; a hidden drawer slid out containing {item}.",
    ),
}


# Generic opener / closer flavor used regardless of destination.
OPENERS: Final[tuple[str, ...]] = (
    "{name} set out for the {dest} just after sunrise.",
    "{name} packed light: a snack, two questions, and an open mind.",
    "{name} left in good spirits, tail twitching with anticipation.",
    "{name} departed for the {dest} -- destination unknown to them, mostly.",
)

CLOSERS: Final[tuple[str, ...]] = (
    "{name} returned at twilight, satisfied and a little muddy.",
    "{name} came home tired but full of stories, mostly nonsense, all true.",
    "{name} wandered back through the door, dropped their loot, and demanded a snack.",
    "{name} returned with the steady gait of someone who has Seen Things.",
)


# Quantities the lexicon-side helpers expect to read at module import.
__all__ = [
    "DESTINATIONS", "destination_meta",
    "SPECIES_AFFINITY", "species_affinity",
    "DURATIONS", "duration_meta",
    "FAVORED_FISH", "FAVORED_CROPS", "FAVORED_ORE", "JUNK_POOL",
    "ORE_PER_DRAW", "RUNE_PER_DRAW",
    "EVENTS", "OPENERS", "CLOSERS",
]
