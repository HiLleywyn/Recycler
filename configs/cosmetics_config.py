"""V3 Pillar 4: profile cosmetics catalogue.

Declarative. Four slots:
    title   - short text suffix on the profile card
    banner  - background art for the profile card (color + accent)
    frame   - border style around the avatar
    sigil   - small emblem stamped in the corner

Each entry has an unlock_source tag so the grant pipeline knows which
system triggers it:
    achievement:<id>     - unlocked by an achievement
    season:<n>           - unlocked by placing in season N
    mastery:<track>:<lv> - unlocked by hitting a mastery level
    shop:<price_usd>     - purchasable from the cosmetics shop
    system               - granted by admin / for everyone
"""
from __future__ import annotations


# Titles -- short suffix shown under the player name.
#
# Every title now carries three weight-bearing fields beyond the label:
#   * ``epithet``       - a one-line flavour quote rendered under the name
#                         on profile / level / payout cards. Gives the
#                         title a voice instead of being a bare label.
#   * ``effect_key`` +  - a passive bonus routed through the SAME namespace
#     ``effect_value``    that ``mastery_config.NODES`` uses, so the
#                         existing ``services.mastery.passives`` /
#                         ``apply`` consumers pick it up automatically
#                         once ``services.cosmetics.title_passives`` is
#                         merged in.
#   * ``unlock``        - reworked: shop:N is reserved for the flex /
#                         legendary tier (Patron / Kingmaker / Tycoon /
#                         Mythical). Every themed title now gates on a
#                         real in-game achievement so it MEANS something
#                         to wear -- you can't just buy "Cat Lord", you
#                         earn it.
TITLES: dict[str, dict] = {
    "novice": {
        "label": "Novice", "unlock": "system",
        "epithet": "Every chain starts with a first transaction.",
    },
    "early_adopter": {
        "label": "Early Adopter", "unlock": "system",
        "epithet": "Mined the genesis block when the genesis block was a Discord message.",
    },
    "fisher_apex": {
        "label": "Apex Fisher", "unlock": "mastery:fisher:50",
        "epithet": "The water doesn't owe you anything. You take it anyway.",
        "effect_key": "luck.rare_catch", "effect_value": 0.05,
    },
    "farmer_apex": {
        "label": "Apex Farmer", "unlock": "mastery:farmer:50",
        "epithet": "Compounded soil, compounded yield.",
        "effect_key": "luck.crop_double", "effect_value": 0.03,
    },
    "delver_apex": {
        "label": "Apex Delver", "unlock": "mastery:delver:50",
        "epithet": "Depth is a state of mind. Also: a state of HP.",
        "effect_key": "combat.dungeon_dmg", "effect_value": 0.05,
    },
    "trader_apex": {
        "label": "Apex Trader", "unlock": "mastery:trader:50",
        "epithet": "Bought the dip you didn't know you saw coming.",
        "effect_key": "econ.auction_fee_cut", "effect_value": 0.05,
    },
    "gambler_apex": {
        "label": "Apex Gambler", "unlock": "mastery:gambler:50",
        "epithet": "House edge is just a suggestion at this point.",
        "effect_key": "combat.gamba_payout", "effect_value": 0.03,
    },
    "raider_apex": {
        "label": "Apex Raider", "unlock": "mastery:raider:50",
        "epithet": "Other people's wallets are a renewable resource.",
        "effect_key": "combat.exploit_def", "effect_value": 0.05,
    },
    "tamer_apex": {
        "label": "Apex Tamer", "unlock": "mastery:tamer:50",
        "epithet": "They follow you because you remembered their birthday.",
        "effect_key": "combat.buddy_dmg", "effect_value": 0.05,
    },
    "validator_apex": {
        "label": "Apex Validator", "unlock": "mastery:validator:50",
        "epithet": "The unsexy work that holds the chain up.",
        "effect_key": "econ.interest_bonus", "effect_value": 0.05,
    },
    "crafter_apex": {
        "label": "Apex Crafter", "unlock": "mastery:crafter:50",
        "epithet": "Built tools the bot didn't know it shipped with.",
        "effect_key": "utility.crafting_speed", "effect_value": 0.05,
    },
    "season_champ": {
        "label": "Season Champion", "unlock": "season:winner",
        "epithet": "Held first place when the lights went out.",
        "effect_key": "econ.daily_bonus", "effect_value": 0.05,
    },
    "season_top3": {
        "label": "Season Top 3", "unlock": "season:podium",
        "epithet": "Top three is still on the leaderboard PNG, baby.",
        "effect_key": "econ.daily_bonus", "effect_value": 0.02,
    },
    "war_champion": {
        "label": "War Champion", "unlock": "clan_war:winner",
        "epithet": "Carried a clan to the wire and didn't blink.",
        "effect_key": "combat.exploit_def", "effect_value": 0.05,
    },
    "first_to_lp": {
        "label": "Liquidity Pioneer", "unlock": "achievement:first_lp",
        "epithet": "Provided the first drop before anyone knew it was a pool.",
        "effect_key": "econ.lp_yield_bonus", "effect_value": 0.05,
    },
    "civic_drag": {
        "label": "Civic Donor", "unlock": "achievement:bottleneck_drag_500k",
        "epithet": "Lifetime drag into the community pool clears half a million.",
        "effect_key": "econ.interest_bonus", "effect_value": 0.03,
    },
    "node_unlocker": {
        "label": "Apex Pathfinder", "unlock": "achievement:unlock_10_nodes",
        "epithet": "Mapped ten branches of the tree and called it a Tuesday.",
        "effect_key": "utility.cooldown_cut", "effect_value": 0.03,
    },
    "philanthropist": {
        "label": "Philanthropist", "unlock": "achievement:donate_1m",
        "epithet": "A million given is a million remembered.",
        "effect_key": "econ.daily_bonus", "effect_value": 0.05,
    },
    "ironwill": {
        "label": "Iron Will", "unlock": "achievement:defend_10_raids",
        "epithet": "Ten raiders bounced off, ten raiders went back to bed.",
        "effect_key": "combat.exploit_def", "effect_value": 0.10,
    },
    "lucky": {
        "label": "Lucky Streak", "unlock": "achievement:gamba_win_streak_10",
        "epithet": "Ten in a row. The dealer asked you to leave.",
        "effect_key": "luck.gamba_streak", "effect_value": 0.03,
    },

    # ── Themed achievement-gated titles ────────────────────────────
    # Gated on real play loops (buddy battles, gamba sessions, etc.)
    # so wearing them means something. ``shop:N`` is reserved below
    # for the legendary flex tier.
    # Themed titles -- mix of achievement-gated and shop-bought. The
    # three with ``achievement:<id>`` here tie to badges that already
    # fire ``services.achievements.bump()`` (buddy_champion = 25 wins,
    # new_best_friend = first adopt, robin_hood = 10 exploit wins) and
    # the new ``cosmetics_for_achievement`` hook in
    # services/achievements.py auto-grants the title when the badge
    # lands. The rest stay on ``shop:N`` because their natural gate
    # (long savings streak, vault stone L5, mastery L50 triple, etc.)
    # doesn't have an achievement entry yet. Add the catalog entry
    # later and flip the unlock string -- no other code needs to know.
    "cat_lord": {
        "label": "Cat Lord", "unlock": "achievement:buddy_champion",
        "theme": "cats", "rarity": "rare",
        "epithet": "Nine lives, zero regrets, full collar.",
        "effect_key": "combat.buddy_dmg", "effect_value": 0.05,
    },
    "kitten": {
        "label": "Kitten", "unlock": "achievement:new_best_friend",
        "theme": "cats", "rarity": "common",
        "epithet": "Small paws, smaller patience.",
        "effect_key": "utility.cooldown_cut", "effect_value": 0.02,
    },
    "moonchaser": {
        "label": "Moonchaser", "unlock": "shop:9000",
        "theme": "moons", "rarity": "rare",
        "epithet": "Caught the Blood Moon mid-pump.",
        "effect_key": "luck.gamba_streak", "effect_value": 0.03,
    },
    "lunatic": {
        "label": "Lunatic", "unlock": "shop:4500",
        "theme": "moons", "rarity": "common",
        "epithet": "Five world events deep. Hasn't slept since.",
        "effect_key": "utility.expedition_speed", "effect_value": 0.03,
    },
    "sea_turtle": {
        "label": "Sea Turtle", "unlock": "shop:6000",
        "theme": "turtles", "rarity": "common",
        "epithet": "Slow money beats fast money. The compounder wins.",
        "effect_key": "econ.interest_bonus", "effect_value": 0.03,
    },
    "shellkeeper": {
        "label": "Shellkeeper", "unlock": "shop:13500",
        "theme": "turtles", "rarity": "rare",
        "epithet": "What's inside the shell is none of your business.",
        "effect_key": "econ.interest_bonus", "effect_value": 0.05,
    },
    "star_walker": {
        "label": "Star Walker", "unlock": "shop:10500",
        "theme": "stars", "rarity": "rare",
        "epithet": "Hit L25 on a track. Then hit it on another.",
        "effect_key": "luck.rare_catch", "effect_value": 0.03,
    },
    "constellation": {
        "label": "Constellation", "unlock": "shop:18000",
        "theme": "stars", "rarity": "epic",
        "epithet": "Three tracks at L50. The sky is a list of your accomplishments.",
        "effect_key": "luck.dungeon_loot", "effect_value": 0.05,
    },
    "tidemaster": {
        "label": "Tidemaster", "unlock": "shop:10500",
        "theme": "ocean", "rarity": "rare",
        "epithet": "Pulled a hundred grand of liquidity out of nowhere.",
        "effect_key": "econ.lp_yield_bonus", "effect_value": 0.05,
    },
    "deep_diver": {
        "label": "Deep Diver", "unlock": "shop:15000",
        "theme": "ocean", "rarity": "epic",
        "epithet": "Hit floor 50 with no save and no save.",
        "effect_key": "luck.dungeon_loot", "effect_value": 0.10,
    },
    "captain": {
        "label": "Captain", "unlock": "achievement:robin_hood",
        "theme": "pirates", "rarity": "rare",
        "epithet": "Ten clean raids. Your tab at the tavern is full.",
        "effect_key": "combat.exploit_def", "effect_value": 0.05,
    },
    "first_mate": {
        "label": "First Mate", "unlock": "shop:5400",
        "theme": "pirates", "rarity": "common",
        "epithet": "Rode the ship, swung the cutlass, drank the rum.",
        "effect_key": "combat.dungeon_dmg", "effect_value": 0.03,
    },
    "high_roller": {
        "label": "High Roller", "unlock": "shop:16500",
        "theme": "gambling", "rarity": "epic",
        "epithet": "You don't fold. You levitate.",
        "effect_key": "combat.gamba_payout", "effect_value": 0.05,
    },
    "card_shark": {
        "label": "Card Shark", "unlock": "shop:9600",
        "theme": "gambling", "rarity": "rare",
        "epithet": "Twenty-five wins at the felt and the felt knows your name.",
        "effect_key": "luck.gamba_streak", "effect_value": 0.05,
    },
    "senator": {
        "label": "Senator", "unlock": "shop:19500",
        "theme": "politics", "rarity": "epic",
        "epithet": "Ten votes cast. None of them with conviction.",
        "effect_key": "econ.daily_bonus", "effect_value": 0.05,
    },
    "lobbyist": {
        "label": "Lobbyist", "unlock": "shop:8400",
        "theme": "politics", "rarity": "rare",
        "epithet": "Proposed it, polled it, passed it, pocketed it.",
        "effect_key": "econ.auction_fee_cut", "effect_value": 0.05,
    },

    # ── Legendary flex titles (USD shop, no gameplay gate) ─────────
    # Kept on shop:N intentionally -- these are pure status symbols
    # for someone with deep pockets. They still grant a passive so
    # they're not just a sticker.
    "patron": {
        "label": "Patron", "unlock": "shop:250000", "rarity": "legendary",
        "epithet": "Funded the lights, the booze, and the bot.",
        "effect_key": "econ.daily_bonus", "effect_value": 0.10,
    },
    "kingmaker": {
        "label": "Kingmaker", "unlock": "shop:150000", "rarity": "legendary",
        "epithet": "Crowned three of your friends and forgot which.",
        "effect_key": "econ.auction_fee_cut", "effect_value": 0.10,
    },
    "tycoon": {
        "label": "Tycoon", "unlock": "shop:200000", "rarity": "legendary",
        "epithet": "Wealth so loud the chart skips a candle.",
        "effect_key": "econ.interest_bonus", "effect_value": 0.10,
    },
    "myth": {
        "label": "Mythical", "unlock": "shop:500000", "rarity": "legendary",
        "epithet": "Half the server thinks you're a server alt.",
        "effect_key": "econ.lp_yield_bonus", "effect_value": 0.15,
    },
}


# Banners -- background. Color = primary, accent = secondary.
BANNERS: dict[str, dict] = {
    "midnight":      {"label": "Midnight",       "color": 0x2c3e50, "accent": 0x3498db, "unlock": "system"},
    "obsidian":      {"label": "Obsidian",       "color": 0x000000, "accent": 0xC0C0C0, "unlock": "system"},
    "sunset":        {"label": "Sunset",         "color": 0x8B0000, "accent": 0xf39c12, "unlock": "shop:5000"},
    "aurora":        {"label": "Aurora",         "color": 0x16527a, "accent": 0x2ecc71, "unlock": "season:winner"},
    "carbon":        {"label": "Carbon",         "color": 0x111111, "accent": 0xf1c40f, "unlock": "shop:15000"},
    "blood":         {"label": "Blood Moon",     "color": 0x4a0606, "accent": 0xff4444, "unlock": "achievement:bloodmoon_kill"},
    "harvest":       {"label": "Harvest Gold",   "color": 0x4d4011, "accent": 0xf1c40f, "unlock": "mastery:farmer:25"},
    "ocean":         {"label": "Ocean Deep",     "color": 0x0a2540, "accent": 0x1abc9c, "unlock": "mastery:fisher:25"},
    "obsidian":      {"label": "Obsidian",       "color": 0x0c0c0c, "accent": 0x9b59b6, "unlock": "mastery:delver:25"},
    "platinum":      {"label": "Platinum",       "color": 0x2a2a2a, "accent": 0xE5E4E2, "unlock": "achievement:top_gdp"},
    "crimson":       {"label": "Crimson Tide",   "color": 0x3d0a0a, "accent": 0xe74c3c, "unlock": "clan_war:winner"},
    "verdant":       {"label": "Verdant",        "color": 0x103820, "accent": 0x4CAF50, "unlock": "shop:10000"},
    "neon":          {"label": "Neon",           "color": 0x0a0a2a, "accent": 0xFF00FF, "unlock": "shop:60000", "rarity": "epic"},

    # ── Premium pixel-art banners (real artwork rendered into the card) ──
    # ``pattern`` tells services/profile_render to draw an actual scene
    # onto the banner background -- stars, moons, sun, trees -- on top
    # of the base ``color``. ``accent`` is the artwork's highlight color.
    # These are intentionally the most expensive shop tier.
    "starfield":     {"label": "Starfield",      "color": 0x05061a, "accent": 0xFFFFFF, "unlock": "shop:45000",  "pattern": "stars",      "rarity": "legendary"},
    "crescent_moon": {"label": "Crescent Moon",  "color": 0x070418, "accent": 0xF5E8B4, "unlock": "shop:55000",  "pattern": "moon",       "rarity": "legendary"},
    "blazing_sun":   {"label": "Blazing Sun",    "color": 0x10182d, "accent": 0xFFC125, "unlock": "shop:60000",  "pattern": "sun",        "rarity": "legendary"},
    "deep_forest":   {"label": "Deep Forest",    "color": 0x062014, "accent": 0x4caf50, "unlock": "shop:50000",  "pattern": "trees",      "rarity": "legendary"},
    "pirate_seas":   {"label": "Pirate Seas",    "color": 0x05101c, "accent": 0xb87333, "unlock": "shop:75000",  "pattern": "pirate_ship","rarity": "legendary"},
    "ocean_horizon": {"label": "Ocean Horizon",  "color": 0x06223a, "accent": 0x29c2d8, "unlock": "shop:50000",  "pattern": "waves",      "rarity": "legendary"},
    "cat_meadow":    {"label": "Cat Meadow",     "color": 0x111c10, "accent": 0xFFB6C1, "unlock": "shop:45000",  "pattern": "cats",       "rarity": "legendary"},
    "casino_table":  {"label": "Casino Table",   "color": 0x0a3018, "accent": 0xFFD700, "unlock": "shop:55000",  "pattern": "cards",      "rarity": "legendary"},

    # ── Themed shop banners (prices bumped 3x in V3 polish pass) ──────
    "whisker_dawn":  {"label": "Whisker Dawn",   "color": 0x2a1f2c, "accent": 0xFFB6C1, "unlock": "shop:19500", "theme": "cats",     "rarity": "rare"},
    "tabby_alley":   {"label": "Tabby Alley",    "color": 0x3a2818, "accent": 0xD2691E, "unlock": "shop:9000",  "theme": "cats",     "rarity": "common"},
    "lunar_glow":    {"label": "Lunar Glow",     "color": 0x10162a, "accent": 0xC0C0FF, "unlock": "shop:22500", "theme": "moons",    "rarity": "rare"},
    "blood_moon_b":  {"label": "Eclipse",        "color": 0x1a0808, "accent": 0xFF6347, "unlock": "shop:25500", "theme": "moons",    "rarity": "rare"},
    "shell_sand":    {"label": "Shell Sand",     "color": 0x4a3818, "accent": 0x00CED1, "unlock": "shop:15000", "theme": "turtles",  "rarity": "rare"},
    "reef_bloom":    {"label": "Reef Bloom",     "color": 0x103040, "accent": 0x00FF7F, "unlock": "shop:18000", "theme": "turtles",  "rarity": "rare"},
    "stellar":       {"label": "Stellar",        "color": 0x0a0a3a, "accent": 0xFFFFFF, "unlock": "shop:24000", "theme": "stars",    "rarity": "rare"},
    "supernova":     {"label": "Supernova",      "color": 0x1a0a3a, "accent": 0xFFA500, "unlock": "shop:36000", "theme": "stars",    "rarity": "epic"},
    "deep_ocean":    {"label": "Deep Ocean",     "color": 0x051d3a, "accent": 0x1abc9c, "unlock": "shop:21000", "theme": "ocean",    "rarity": "rare"},
    "abyssal_blue":  {"label": "Abyssal",        "color": 0x020a1a, "accent": 0x4682B4, "unlock": "shop:28500", "theme": "ocean",    "rarity": "rare"},
    "jolly_roger":   {"label": "Jolly Roger",    "color": 0x0a0a0a, "accent": 0xFFFFFF, "unlock": "shop:27000", "theme": "pirates",  "rarity": "rare"},
    "treasure_chest": {"label": "Treasure Chest","color": 0x3a280a, "accent": 0xFFD700, "unlock": "shop:33000", "theme": "pirates",  "rarity": "epic"},
    "vegas_strip":   {"label": "Vegas Strip",    "color": 0x2a0a3a, "accent": 0xFF1493, "unlock": "shop:28500", "theme": "gambling", "rarity": "rare"},
    "casino_floor":  {"label": "Casino Floor",   "color": 0x1a0a2a, "accent": 0xFFD700, "unlock": "shop:24000", "theme": "gambling", "rarity": "rare"},
    "capitol":       {"label": "Capitol",        "color": 0x0a1a3a, "accent": 0xC0C0C0, "unlock": "shop:31500", "theme": "politics", "rarity": "rare"},
    "smoke_filled":  {"label": "Smoke-Filled Room", "color": 0x282010, "accent": 0xCD5C5C, "unlock": "shop:22500", "theme": "politics", "rarity": "rare"},
}


# Frames -- avatar border style.
FRAMES: dict[str, dict] = {
    "simple":     {"label": "Simple",         "color": 0x808080, "ring_width": 3, "unlock": "system"},
    "gold":       {"label": "Gold",           "color": 0xf1c40f, "ring_width": 4, "unlock": "shop:7500"},
    "platinum":   {"label": "Platinum",       "color": 0xE5E4E2, "ring_width": 5, "unlock": "achievement:net_worth_1m"},
    "diamond":    {"label": "Diamond",        "color": 0x5DADEC, "ring_width": 5, "unlock": "achievement:net_worth_10m"},
    "rainbow":    {"label": "Prism",          "color": 0x9b59b6, "ring_width": 6, "unlock": "achievement:rainbow_event"},
    "ember":      {"label": "Ember",          "color": 0xff4444, "ring_width": 4, "unlock": "mastery:raider:25"},
    "frost":      {"label": "Frost",          "color": 0x5DADEC, "ring_width": 4, "unlock": "mastery:fisher:25"},
    "abyss":      {"label": "Abyssal",        "color": 0x2c3e50, "ring_width": 5, "unlock": "mastery:delver:50"},

    # ── Themed shop frames (prices bumped 3x + rarity tagged) ──────
    "tabby":      {"label": "Tabby",           "color": 0xD2691E, "ring_width": 4, "unlock": "shop:10500", "theme": "cats",     "rarity": "rare"},
    "crescent":   {"label": "Crescent",        "color": 0xC0C0FF, "ring_width": 4, "unlock": "shop:13500", "theme": "moons",    "rarity": "rare"},
    "shell":      {"label": "Shell",           "color": 0x00CED1, "ring_width": 5, "unlock": "shop:12000", "theme": "turtles",  "rarity": "rare"},
    "comet":      {"label": "Comet",           "color": 0xFFA500, "ring_width": 5, "unlock": "shop:16500", "theme": "stars",    "rarity": "epic"},
    "coral":      {"label": "Coral",           "color": 0x1abc9c, "ring_width": 4, "unlock": "shop:13500", "theme": "ocean",    "rarity": "rare"},
    "anchor_chain": {"label": "Anchor Chain",  "color": 0xC0C0C0, "ring_width": 5, "unlock": "shop:18000", "theme": "pirates",  "rarity": "epic"},
    "cards":      {"label": "Cards",           "color": 0xFFD700, "ring_width": 4, "unlock": "shop:15000", "theme": "gambling", "rarity": "epic"},
    "eagle":      {"label": "Eagle",           "color": 0xC0C0C0, "ring_width": 6, "unlock": "shop:25500", "theme": "politics", "rarity": "epic"},
    # Legendary frames
    "diamond":    {"label": "Diamond",         "color": 0xb9f2ff, "ring_width": 7, "unlock": "shop:80000", "rarity": "legendary"},
    "obsidian_ring": {"label": "Obsidian Ring", "color": 0x111111, "ring_width": 8, "unlock": "shop:65000", "rarity": "legendary"},
}


# Sigils -- corner emblem (unicode glyph rendered into a colored disc).
SIGILS: dict[str, dict] = {
    "star":        {"label": "Star",          "glyph": "*", "color": 0xf1c40f, "unlock": "system"},
    "anchor":      {"label": "Anchor",        "glyph": "A", "color": 0x1abc9c, "unlock": "mastery:fisher:25"},
    "leaf":        {"label": "Leaf",          "glyph": "L", "color": 0x4CAF50, "unlock": "mastery:farmer:25"},
    "sword":       {"label": "Sword",         "glyph": "S", "color": 0xe74c3c, "unlock": "mastery:delver:25"},
    "chart":       {"label": "Chart",         "glyph": "U", "color": 0x3498db, "unlock": "mastery:trader:25"},
    "die":         {"label": "Die",           "glyph": "D", "color": 0xf39c12, "unlock": "mastery:gambler:25"},
    "skull":       {"label": "Skull",         "glyph": "X", "color": 0x8B0000, "unlock": "mastery:raider:25"},
    "paw":         {"label": "Paw",           "glyph": "P", "color": 0xe91e63, "unlock": "mastery:tamer:25"},
    "scale":       {"label": "Scale",         "glyph": "B", "color": 0x95a5a6, "unlock": "mastery:validator:25"},
    "hammer":      {"label": "Hammer",        "glyph": "H", "color": 0xe67e22, "unlock": "mastery:crafter:25"},
    "crown":       {"label": "Crown",         "glyph": "K", "color": 0xf1c40f, "unlock": "season:winner"},
    "shield":      {"label": "Shield",        "glyph": "G", "color": 0x3498db, "unlock": "achievement:defend_10_raids"},
    "flame":       {"label": "Flame",         "glyph": "F", "color": 0xff4444, "unlock": "shop:8000"},
    "snowflake":   {"label": "Snowflake",     "glyph": "I", "color": 0xE0FFFF, "unlock": "shop:8000"},
    "wave":        {"label": "Wave",          "glyph": "~", "color": 0x1abc9c, "unlock": "shop:8000"},
    "lightning":   {"label": "Lightning",     "glyph": "Z", "color": 0xf1c40f, "unlock": "achievement:fastest_swap"},

    # ── Themed shop sigils (prices bumped 3x + rarity tagged) ──────
    "cat":         {"label": "Cat",            "glyph": "C", "color": 0xFFB6C1, "unlock": "shop:6000",  "theme": "cats",     "rarity": "common"},
    "moon":        {"label": "Moon",           "glyph": "M", "color": 0xC0C0FF, "unlock": "shop:7500",  "theme": "moons",    "rarity": "rare"},
    "turtle":      {"label": "Turtle",         "glyph": "T", "color": 0x00CED1, "unlock": "shop:7500",  "theme": "turtles",  "rarity": "rare"},
    "star_shop":   {"label": "Five-Star",      "glyph": "5", "color": 0xFFD700, "unlock": "shop:10500", "theme": "stars",    "rarity": "rare"},
    "ocean_wave":  {"label": "Tidewave",       "glyph": "W", "color": 0x1abc9c, "unlock": "shop:9000",  "theme": "ocean",    "rarity": "rare"},
    "pirate_skull": {"label": "Cross Bones",   "glyph": "X", "color": 0xFFFFFF, "unlock": "shop:12000", "theme": "pirates",  "rarity": "rare"},
    "dice":        {"label": "Dice",           "glyph": "D", "color": 0xFF1493, "unlock": "shop:10500", "theme": "gambling", "rarity": "rare"},
    "gavel":       {"label": "Gavel",          "glyph": "G", "color": 0xC0C0C0, "unlock": "shop:13500", "theme": "politics", "rarity": "rare"},
    # Legendary sigils
    "phoenix":     {"label": "Phoenix",        "glyph": "P", "color": 0xff5722, "unlock": "shop:40000", "rarity": "legendary"},
    "dragon":      {"label": "Dragon",         "glyph": "D", "color": 0x8B0000, "unlock": "shop:50000", "rarity": "legendary"},
    "infinity":    {"label": "Infinity",       "glyph": "∞", "color": 0xffffff, "unlock": "shop:35000", "rarity": "legendary"},
}


# ── Theme registry (for shop categorisation) ───────────────────────────
THEMES: dict[str, dict] = {
    "cats":      {"label": "Cats",      "color": 0xFFB6C1, "emoji": "C"},
    "moons":     {"label": "Moons",     "color": 0xC0C0FF, "emoji": "M"},
    "turtles":   {"label": "Turtles",   "color": 0x00CED1, "emoji": "T"},
    "stars":     {"label": "Stars",     "color": 0xFFD700, "emoji": "*"},
    "ocean":     {"label": "Ocean",     "color": 0x1abc9c, "emoji": "~"},
    "pirates":   {"label": "Pirates",   "color": 0xCCCCCC, "emoji": "P"},
    "gambling":  {"label": "Gambling",  "color": 0xFF1493, "emoji": "$"},
    "politics":  {"label": "Politics",  "color": 0xC0C0C0, "emoji": "!"},
}


# Slot -> catalogue map for the cog.
SLOTS = {
    "title":  TITLES,
    "banner": BANNERS,
    "frame":  FRAMES,
    "sigil":  SIGILS,
}


def all_items() -> dict[str, dict]:
    """Return every cosmetic across every slot keyed by ``slot/id``."""
    out: dict[str, dict] = {}
    for slot, cat in SLOTS.items():
        for cid, entry in cat.items():
            out[f"{slot}/{cid}"] = entry | {"slot": slot, "id": cid}
    return out
