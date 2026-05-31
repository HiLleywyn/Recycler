"""
UI constants  -  embed colors, pagination limits.

Pure Python. No discord.py imports.
"""
from __future__ import annotations

# ── Core semantic colors ───────────────────────────────────────────────────────
C_SUCCESS: int = 0x2ecc71
C_ERROR: int   = 0xe74c3c
C_WARNING: int = 0xe67e22
C_INFO: int    = 0x3498db
C_GOLD: int    = 0xf1c40f
C_PURPLE: int  = 0x9b59b6
C_TEAL: int    = 0x1abc9c
C_NAVY: int    = 0x2c3e50
C_PINK: int    = 0xe91e63
C_NEUTRAL: int = 0x95a5a6
C_AMBER: int   = 0xf39c12

# ── Trade / direction aliases ──────────────────────────────────────────────────
C_BUY: int  = C_SUCCESS
C_SELL: int = C_ERROR

# ── Extended palette ───────────────────────────────────────────────────────────
C_BLURPLE: int   = 0x5865F2  # Discord brand color (help, dev, admin tools)
C_GRAY: int      = 0x808080  # Muted / neutral (vault defaults, event phase ends)
C_DARK_BLUE: int = 0x34495E  # Chain explorer, block info
C_STEEL: int     = 0x2980B9  # Networks display
C_SUBTLE: int    = 0x72767D  # Very muted Discord gray
C_CHART_BG: int  = 0x161B22  # Trade chart canvas background

C_CRIMSON: int   = 0x8B0000  # Critical / fatal error severity
C_BLACK: int     = 0x000000  # Total loss / wallet drained / void

# ── Market event colors ────────────────────────────────────────────────────────
C_BULL: int        = 0x00FF88  # Bullish rally phases
C_BEAR: int        = 0xFF4444  # Bearish / crash phases
C_VOLATILE: int    = 0xFFAA00  # High-volatility phases
C_CATASTROPHE: int = 0xFF0000  # Black-swan / catastrophic events

# ── Rarity tier colors ────────────────────────────────────────────────────────
# Single source of truth for rarity-tier accent colors. All cogs that surface a
# rarity (farming crops, crafting recipes, dungeon loot, NFT cards, ...) MUST
# resolve through these constants so the same tier looks identical everywhere.
C_RARITY_COMMON: int    = C_NEUTRAL  # gray
C_RARITY_UNCOMMON: int  = C_SUCCESS  # green
C_RARITY_RARE: int      = C_INFO     # blue
C_RARITY_EPIC: int      = C_PURPLE   # purple (was raw 0x9B59B6 in cogs)
C_RARITY_LEGENDARY: int = C_GOLD     # yellow

# Lookup keyed by lowercase tier name. `RARITY_COLORS.get(tier, C_NEUTRAL)` is
# the canonical access pattern -- never copy-paste a {tier: color} dict.
RARITY_COLORS: dict[str, int] = {
    "common":    C_RARITY_COMMON,
    "uncommon":  C_RARITY_UNCOMMON,
    "rare":      C_RARITY_RARE,
    "epic":      C_RARITY_EPIC,
    "legendary": C_RARITY_LEGENDARY,
}

# Single colored-circle emoji per rarity. Matches the RARITY_COLORS palette
# (gray / green / blue / purple / yellow) and is the canonical glyph anywhere
# rarity is rendered next to text -- recipe lists, NFT cards, dungeon loot,
# crafting tables, etc. Never copy-paste a per-cog rarity emoji table.
RARITY_DOT: dict[str, str] = {
    "common":    "\U0001F7E4",  # brown circle (gray-tone in most themes)
    "uncommon":  "\U0001F7E2",  # green circle
    "rare":      "\U0001F535",  # blue circle
    "epic":      "\U0001F7E3",  # purple circle
    "legendary": "\U0001F7E1",  # yellow circle
}

# Square equivalent for grid-style displays (NFT collections, inventory
# galleries) where a square reads better than a circle. Same color order.
RARITY_SQUARE: dict[str, str] = {
    "common":    "⬜",       # white large square
    "uncommon":  "\U0001F7E9",   # green square
    "rare":      "\U0001F7E6",   # blue square
    "epic":      "\U0001F7EA",   # purple square
    "legendary": "\U0001F7E8",   # yellow square
}

# Three-letter abbreviation that fits a fixed-width code-block table column.
RARITY_ABBR: dict[str, str] = {
    "common":    "com",
    "uncommon":  "unc",
    "rare":      "rar",
    "epic":      "epi",
    "legendary": "leg",
}

# ── System / module section colors ────────────────────────────────────────────
# Showcase tabs, leaderboards, and per-system summary embeds use these so a
# given subsystem (farming, dungeon, crafting, ...) reads the same color across
# the bot.
C_FARMING: int  = 0x4CAF50   # Material-green crop fields
C_DUNGEON: int  = C_PURPLE   # purple delves
C_CRAFTING: int = C_WARNING  # forge / amber heat
C_FISHING: int  = C_INFO     # blue water
C_BUDDY: int    = C_PINK     # buddies / pets

# ── Buddy arena tier colors ───────────────────────────────────────────────────
# Surfaced on `,buddy arena`, tier-up announcements, and the Buddy showcase
# tab. Hex values match the metallic palette players already see on the
# tier emojis (3rd-place medal, 2nd, 1st, gemstone, gem).
C_TIER_BRONZE: int   = 0xCD7F32
C_TIER_SILVER: int   = 0xC0C0C0
C_TIER_GOLD: int     = 0xFFD700
C_TIER_PLATINUM: int = 0xE5E4E2
C_TIER_DIAMOND: int  = 0x5DADEC

ARENA_TIER_COLORS: dict[str, int] = {
    "bronze":   C_TIER_BRONZE,
    "silver":   C_TIER_SILVER,
    "gold":     C_TIER_GOLD,
    "platinum": C_TIER_PLATINUM,
    "diamond":  C_TIER_DIAMOND,
}
