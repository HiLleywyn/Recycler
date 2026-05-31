"""items_config.py  -  Item Shop item definitions.

Edit this file to add, remove, or tweak items in the Item Shop.
Each item lives under a unique key in SHOP_ITEMS. After changing values,
restart the bot for them to take effect.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ITEM CATEGORIES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  category = "item"         -  leveled gear that gains XP and provides scaling bonuses
  category = "consumable"   -  stackable single-use items consumed automatically or manually

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STAT KEYS  -  valid entries inside each item's "stats" dict
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  work_daily_bonus    -  % multiplier added to /work and /daily earnings
  mining_bonus        -  % multiplier added to mining hashrate
  stake_bonus         -  % multiplier added to validator staking rewards
  interest_bonus      -  % multiplier added to savings account interest

HOW STATS SCALE:

  leveled=True   →  final stat = stat_value × current_level
    Example: work_daily_bonus=0.02 at level 10 → +20% earnings

  leveled=False  →  final stat = stat_value (flat, always on)
    Example: work_daily_bonus=0.05 → +5% regardless of anything

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADDING A NEW ITEM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Copy the TEMPLATE block at the bottom of this file.
  2. Give it a unique lowercase key (e.g. "lucky_charm").
  3. Fill in the fields and set the stats you want active (others stay 0.0).
  4. For non-leveled items, set leveled=False and omit the XP fields.
  5. Restart the bot  -  the item will appear in /shop automatically.

  Note: the "hashstone" key is special  -  it maps to the `hashstones`
  database table (renamed from sunstones). New items will need their own DB table before buy/sell works.

PRICING NOTE  -  all costs are in DSD (Disdollar, the Discoin Network stablecoin, $1 peg).
Converted from original SUN prices using the SUN genesis price of $0.01 per SUN.
"""
from __future__ import annotations

import os

_S = 10 ** 18  # 1 human USD/token = _S raw units

SHOP_ITEMS: dict[str, dict] = {

    # ──────────────────────────────────────────────────────────────────────────
    # Hashstone  -  mining gem (renamed from Sunstone).
    #
    # Players stake DSD stablecoin to acquire one. The staked DSD is locked, not
    # burned  -  you get it back (minus a sell fee) when you sell. The Hashstone
    # levels up via XP earned from mining across any PoW network, proportional
    # to the miner's hashrate share. Each level multiplies every active stat.
    #
    # LEVELING CURVE (quadratic):
    #   Lv 1→2:   80 XP    Lv 10→11:  800 XP    Lv 25→26:  2,000 XP
    #   Lv 50→51: 4,000 XP  Lv 75→76:  6,000 XP  Lv 99→100: 7,920 XP
    #   Total to max: ~400,000 XP
    # ──────────────────────────────────────────────────────────────────────────
    "hashstone": {
        "name":        "Hashstone",
        "emoji":       "⛏️",
        "category":    "item",
        "description": (
            "A crystallized fragment forged from raw hashpower. "
            "Pay in MTA or SUN to acquire one  -  it levels up as you mine any PoW network, boosting your earnings."
        ),

        # ── Pricing & fees ────────────────────────────────────────────────────
        # accepted_currencies lists the symbols the user can pay with.
        # First entry is the default if the user omits the currency arg.
        # cost_stable is interpreted as a USD-equivalent target; the
        # buy path converts to the chosen currency at the live oracle.
        # Legacy stables (DSD/USDC) stay accepted for backward compat
        # with stones bought before this change so auto-levelup still
        # works on existing inventory.
        # Hashstone is mining gear -- pay in the PoW network coins it
        # boosts (MTA, SUN). Legacy DSD/USDC fallback was dropped so the
        # stake currency reflects the stone's actual purpose; existing
        # DSD-staked stones are migrated by 0165_*.sql.
        "accepted_currencies": ("MTA", "SUN"),
        "cost_stable":         int(7_500 * _S), # USD-equivalent cost (raw int)
        "buy_fee_pct":         0.05,             # 5% of cost -> guild treasury on buy
        "sell_fee_pct":        0.05,             # 5% of staked amount -> guild treasury on sell
        "transfer_fee_stable": int(150 * _S),   # flat stablecoin gas fee for peer-to-peer transfers

        # ── Leveling ──────────────────────────────────────────────────────────
        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  80,    # XP needed for level N→N+1 = N × this value
        "xp_per_block_share": float(os.getenv("HASHSTONE_XP_RATE", "35.0")),

        # ── Stats ─────────────────────────────────────────────────────────────
        # For leveled items: final bonus = stat_value × current_level
        "stats": {
            "work_daily_bonus":  0.003,  # +0.3% per level to /work & /daily → max +30% at lv100
            "mining_bonus":      0.0024, # +0.24% per level to hashrate → max +24% at lv100
            "stake_bonus":       0.00,
            "interest_bonus":    0.00,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Lockstone  -  levels up through staking and PoS validator activity.
    #
    # LEVELING CURVE:
    #   Total to max: ~320,000 XP (slightly faster than Hashstone)
    # ──────────────────────────────────────────────────────────────────────────
    "lockstone": {
        # Lockstone is staking gear -- pay in the PoS network coins
        # it boosts (DSC, ARC). Legacy DSD/USDC accepted-currencies
        # were dropped to align stake currency with the stone's actual
        # purpose; existing rows are migrated by 0165_*.sql.
        "name":        "Lockstone",
        "emoji":       "🔒",
        "category":    "item",
        "description": (
            "A shard forged from staking pressure. "
            "Boosts /work, /daily, and staking rewards  -  levels up as you stake and validate."
        ),

        "accepted_currencies": ("DSC", "ARC"),
        "cost_stable":         int(6_000 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(120 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  65,
        "xp_per_stake_reward": 30.0,
        "xp_per_block":        35.0,

        "stats": {
            "work_daily_bonus":  0.0024, # +0.24% per level → max +24% at lv100
            "mining_bonus":      0.00,
            "stake_bonus":       0.003,  # +0.3% per level → max +30% at lv100
            "interest_bonus":    0.00,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Vaultstone  -  levels up through savings and lending activity.
    #
    # LEVELING CURVE:
    #   Total to max: ~250,000 XP (fastest stone  -  savings is passive)
    # ──────────────────────────────────────────────────────────────────────────
    "vaultstone": {
        "name":        "Vaultstone",
        "emoji":       "🏦",
        "category":    "item",
        "description": (
            "A gem crystallized from compounding interest. "
            "Boosts /work, /daily, and savings interest  -  levels up as you save and lend."
        ),

        # Vaultstone is paid out of the bare USD wallet (users.wallet),
        # not wallet_holdings. The shop buy path special-cases the
        # 'USD' currency to debit the bank-style wallet directly.
        "accepted_currencies": ("USD",),
        "cost_stable":         int(5_000 * _S),
        "buy_fee_pct":         0.04,
        "sell_fee_pct":        0.04,
        "transfer_fee_stable": int(100 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  50,
        "xp_per_interest":    40.0,

        "stats": {
            "work_daily_bonus":  0.002,  # +0.2% per level → max +20% at lv100
            "mining_bonus":      0.00,
            "stake_bonus":       0.00,
            "interest_bonus":    0.0036, # +0.36% per level → max +36% at lv100
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Liqstone -- liquidity provision gem.
    #
    # Levels up based on LP value * time held. XP is granted hourly during the
    # staking tick, proportional to the USD value of LP positions. Minimum 1h
    # hold before XP starts accruing (anti-churn). XP capped per tick to
    # prevent whales from maxing instantly.
    #
    # LEVELING CURVE (quadratic, same pattern as others):
    #   Lv 1->2:  70 XP    Lv 10->11: 700 XP    Lv 50->51: 3,500 XP
    #   Total to max: ~350,000 XP
    # ──────────────────────────────────────────────────────────────────────────
    "liqstone": {
        "name":        "Liqstone",
        "emoji":       "🌊",
        "category":    "item",
        "description": (
            "A gem formed from deep liquidity. "
            "Levels up the longer you provide LP. Boosts swap fees, work, and LP rewards."
        ),

        # Liqstone is liquidity gear -- LP returns are dollar-
        # denominated, so the stake currency is the dollar-pegged
        # stables (DSD, USDC). Network coins were dropped so the
        # stake currency reflects the LP rewards path; existing
        # DSC/ARC-staked rows are migrated by 0165_*.sql.
        "accepted_currencies": ("DSD", "USDC"),
        "cost_stable":         int(8_000 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(160 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  70,
        "xp_per_lp_tick":     float(os.getenv("LIQSTONE_XP_RATE", "50.0")),
        "xp_max_per_tick":    400.0,      # cap per hourly tick (anti-whale, bumped from 200)
        "min_hold_secs":      3600,       # 1h minimum hold before XP accrues

        "stats": {
            "work_daily_bonus":  0.0015,   # +0.15% per level
            "swap_fee_discount": 0.001,    # -0.1% swap fee per level
            "lp_reward_bonus":   0.005,    # +0.5% LP fee share per level (buffed from +0.2%)
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Validator Guard  -  consumable insurance for validator slashing.
    #
    # When a validator would be slashed (downtime/double-sign), one guard is
    # auto-consumed to prevent the slash penalty. Purchased in bulk.
    # ──────────────────────────────────────────────────────────────────────────
    "validator_guard": {
        "name":        "Validator Guard",
        "emoji":       "🛡️",
        "category":    "consumable",
        "description": (
            "Insurance against validator slashing. "
            "When your validator would be penalized, one guard is consumed to absorb the slash. "
            "Stackable  -  buy multiple for extended protection."
        ),

        "cost_stable":         int(450 * _S),
        "buy_fee_pct":         0.03,
        "sell_fee_pct":        0.00,   # consumable  -  no sell
        "transfer_fee_stable": 0,      # no transfer

        "leveled":  False,
        "stackable": True,
        "max_stack": 50,

        "stats": {
            "work_daily_bonus":  0.00,
            "mining_bonus":      0.00,
            "stake_bonus":       0.00,
            "interest_bonus":    0.00,
            "slash_protection":  1.0,   # blocks one slash event per guard consumed
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Yield Guard  -  consumable insurance for savings/lending losses.
    #
    # If a borrower defaults or the savings pool takes a loss, one yield guard
    # is auto-consumed to protect your principal from the haircut.
    # ──────────────────────────────────────────────────────────────────────────
    "yield_guard": {
        "name":        "Yield Guard",
        "emoji":       "🔐",
        "category":    "consumable",
        "description": (
            "Insurance for your savings deposits. "
            "If a lending loss would reduce your principal, one guard absorbs the hit. "
            "Stackable  -  buy multiple for continued protection."
        ),

        "cost_stable":         int(400 * _S),
        "buy_fee_pct":         0.03,
        "sell_fee_pct":        0.00,   # consumable  -  no sell
        "transfer_fee_stable": 0,      # no transfer

        "leveled":  False,
        "stackable": True,
        "max_stack": 50,

        "stats": {
            "work_daily_bonus":  0.00,
            "mining_bonus":      0.00,
            "stake_bonus":       0.00,
            "interest_bonus":    0.00,
            "yield_protection":  1.0,   # blocks one savings loss event per guard consumed
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Gamba Shop consumables  -  GBC-priced single-use boosts on the gamba
    # surface (chess / checkers / mines / dice / coinflip / blackjack /
    # roulette / slots). Auto-applied when you trigger the matching event
    # so there is no "arm" workflow to remember. ``gamba_only: True`` so
    # the regular ,shop browser hides them; the gamba cog reads these
    # entries directly from SHOP_ITEMS (single source of truth) for the
    # ,gamba shop browser.
    #
    # cost_stable is denominated in GBC (not USD): the gamba shop treats
    # the value as raw-scaled GBC. Conservative effects so chip stacking
    # never produces an unfair edge -- the math caps small.
    # ──────────────────────────────────────────────────────────────────────────
    "lucky_chip": {
        "name":        "Lucky Chip",
        "emoji":       "\U0001F340",   # four-leaf clover
        "category":    "consumable",
        "gamba_only":  True,
        "description": (
            "+5% to your USD payout on the next single gamba win, any game. "
            "Auto-consumed; no need to activate."
        ),

        "accepted_currencies": ("GBC",),
        "cost_stable":         int(50 * _S),   # 50 GBC
        "buy_fee_pct":         0.00,
        "sell_fee_pct":        0.00,
        "transfer_fee_stable": 0,

        "leveled":   False,
        "stackable": True,
        "max_stack": 50,

        "stats": {
            "gamba_win_bonus": 0.05,
        },
    },
    "house_marker": {
        "name":        "House Marker",
        "emoji":       "\U0001F3F4",   # waving black flag
        "category":    "consumable",
        "gamba_only":  True,
        "description": (
            "Refunds 25% of your bet on the next single gamba loss. "
            "Auto-consumed; no need to activate."
        ),

        "accepted_currencies": ("GBC",),
        "cost_stable":         int(75 * _S),   # 75 GBC
        "buy_fee_pct":         0.00,
        "sell_fee_pct":        0.00,
        "transfer_fee_stable": 0,

        "leveled":   False,
        "stackable": True,
        "max_stack": 50,

        "stats": {
            "gamba_loss_refund": 0.25,
        },
    },
    "side_bet_slip": {
        "name":        "Side Bet Slip",
        "emoji":       "\U0001F3AB",   # ticket
        "category":    "consumable",
        "gamba_only":  True,
        "description": (
            "Doubles the game-themed token minted on your next single gamba win. "
            "Auto-consumed; no need to activate."
        ),

        "accepted_currencies": ("GBC",),
        "cost_stable":         int(40 * _S),   # 40 GBC
        "buy_fee_pct":         0.00,
        "sell_fee_pct":        0.00,
        "transfer_fee_stable": 0,

        "leveled":   False,
        "stackable": True,
        "max_stack": 50,

        "stats": {
            "gamba_token_double": 1.0,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Cosmetic consumables  -  craft-only role-granting single-use items.
    #
    # Each item has a `category: "cosmetic"` and a `role_name` field.
    # Cosmetics are CRAFT-ONLY (``craft_only: True`` -- the shop refuses
    # ,shop buy and the items embed hides them); the only way in is the
    # matching crafting recipe in crafting_config.py with apply target
    # ``cosmetic/<key>``. Stored in users.cosmetics JSONB until used.
    #
    # Using one via ,inventory use <key> grants the named Discord role
    # for ``duration_seconds`` (default 3600s / 1 hour). The role is
    # removed automatically when the grant expires; using the item again
    # before expiry refreshes the timer rather than toggling off.
    # ──────────────────────────────────────────────────────────────────────────
    "glamour_kit": {
        "name":        "Glamour Kit",
        "emoji":       "\U0001F48E",
        "category":    "cosmetic",
        "role_name":   "Glamour",
        "description": (
            "A shimmering case packed with cosmetics. "
            "Grants the Glamour role for 1 hour."
        ),

        "craft_only":          True,
        "duration_seconds":    3600,    # 1 hour
        "transfer_fee_stable": 0,

        "leveled":  False,
        "stackable": True,
        "max_stack": 10,
    },
    "night_crystal": {
        "name":        "Night Crystal",
        "emoji":       "\U0001F311",
        "category":    "cosmetic",
        "role_name":   "Night Crystal",
        "description": (
            "A deep indigo gem that hums with moonlight. "
            "Grants the Night Crystal role for 1 hour."
        ),

        "craft_only":          True,
        "duration_seconds":    3600,
        "transfer_fee_stable": 0,

        "leveled":  False,
        "stackable": True,
        "max_stack": 10,
    },
    "aurora_pass": {
        "name":        "Aurora Pass",
        "emoji":       "\U0001F308",
        "category":    "cosmetic",
        "role_name":   "Aurora",
        "description": (
            "A rare ticket that shimmers with prismatic light. "
            "Grants the Aurora role for 1 hour."
        ),

        "craft_only":          True,
        "duration_seconds":    3600,
        "transfer_fee_stable": 0,

        "leveled":  False,
        "stackable": True,
        "max_stack": 10,
    },

    # ──────────────────────────────────────────────────────────────────────────
    # TEMPLATE  -  copy this block, remove the leading # characters, and fill in
    # your values. Then restart the bot. The item will appear in /shop.
    #
    # New items need a matching database table before buy/sell commands work.
    # ──────────────────────────────────────────────────────────────────────────
    # "my_new_item": {
    #     "name":        "My New Item",
    #     "emoji":       "🌟",
    #     "category":    "item",        # or "consumable"
    #     "description": "A short description shown in /shop.",
    #
    #     "cost_stable":         12.5,   # DSD (stablecoin, $1 peg)
    #     "buy_fee_pct":         0.05,
    #     "sell_fee_pct":        0.05,
    #     "transfer_fee_stable": 0.25,
    #
    #     # Set leveled=False for a flat-bonus item with no XP or level display.
    #     "leveled":            False,
    #     # Remove or comment out the XP fields when leveled=False:
    #     # "max_level":          20,
    #     # "xp_per_level_base":  200,
    #     # "xp_per_block_share": 5.0,
    #
    #     "stats": {
    #         "work_daily_bonus":  0.00,
    #         "mining_bonus":      0.00,
    #         "stake_bonus":       0.00,
    #         "interest_bonus":    0.00,
    #     },
    # },

    # ──────────────────────────────────────────────────────────────────────────
    # Tidestone  -  fishing gem.
    #
    # Levels up via XP earned from ,fish casts (one stone XP per landed catch
    # plus a quality multiplier). Boosts fishing payouts and combo retention
    # without touching the LURE / REEL mint paths -- it only scales the
    # USD-equivalent kicker on each cast so it's a passive amplifier rather
    # than a printer.
    # ──────────────────────────────────────────────────────────────────────────
    "tidestone": {
        "name":        "Tidestone",
        "emoji":       "\U0001F30A",  # ocean wave
        "category":    "item",
        "description": (
            "A briny shard pulled from the deep. Levels up as you cast and "
            "lands quality catches; boosts fishing payouts and combo gain."
        ),

        "accepted_currencies": ("REEL",),
        "cost_stable":         int(6_500 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(130 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  70,
        "xp_per_cast":        20.0,   # baseline per landed cast
        "xp_per_legendary":   400.0,  # bonus on legendary fish
        "xp_per_combo":       4.0,    # multiplier on current combo

        "stats": {
            "fish_payout_bonus":  0.0030,  # +0.30% per level -> max +30%
            "fish_combo_bonus":   0.0015,  # +0.15% per level -> max +15%
            "work_daily_bonus":   0.0010,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Heartstone  -  buddy companionship gem.
    #
    # Levels up via XP earned from buddy interactions (chat XP, feed, pet,
    # talk, level-ups). Boosts buddy chat XP gain and slows mood decay so
    # an owner can step away for longer without their buddy starving down.
    # ──────────────────────────────────────────────────────────────────────────
    "heartstone": {
        "name":        "Heartstone",
        "emoji":       "\U0001F49E",  # sparkling heart
        "category":    "item",
        "description": (
            "A warm gem that holds a piece of every buddy you've raised. "
            "Boosts buddy chat XP and slows mood decay; levels up as you "
            "interact with your buddies."
        ),

        "accepted_currencies": ("BUD",),
        "cost_stable":         int(5_500 * _S),
        "buy_fee_pct":         0.04,
        "sell_fee_pct":        0.04,
        "transfer_fee_stable": int(110 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  60,
        "xp_per_chat":        6.0,    # per buddy chat-XP tick
        "xp_per_feed":        20.0,
        "xp_per_levelup":     150.0,  # buddy level-ups feel chunky

        "stats": {
            "buddy_xp_bonus":          0.0030,  # +0.30% per level
            "buddy_decay_resist":      0.0040,  # +0.40% per level (slows decay)
            "work_daily_bonus":        0.0010,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Cryptstone  -  dungeon delving gem.
    #
    # Levels up via XP earned from dungeon kills, captures, and ore mined.
    # Boosts ore quantity per ,delve mine and the player's dungeon ATK so
    # higher-tier mobs and bosses feel reachable.
    # ──────────────────────────────────────────────────────────────────────────
    "cryptstone": {
        "name":        "Cryptstone",
        "emoji":       "\U0001F48E",  # gem
        "category":    "item",
        "description": (
            "A sharp-edged crystal pulled from a deep vein. Boosts dungeon "
            "ATK and ore yield; levels up as you delve, mine, and slay."
        ),

        "accepted_currencies": ("RUNE",),
        "cost_stable":         int(7_000 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(140 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  75,
        "xp_per_kill":        25.0,
        "xp_per_capture":     45.0,
        "xp_per_mine":        15.0,
        "xp_per_boss":        500.0,  # boss kills feel like a milestone

        "stats": {
            "dungeon_mine_bonus":   0.0030,  # +0.30% ore qty per level
            "dungeon_atk_bonus":    0.0020,  # +0.20% player ATK in dungeon
            "dungeon_capture_bonus": 0.0015, # +0.15% capture chance per level
            "work_daily_bonus":     0.0010,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Bloomstone  -  farming gem (Harvest Network).
    #
    # Levels up via XP earned from planting, harvesting, processing recipes,
    # and slaying farm pests. Boosts crop yield on harvest, SEED drops, and
    # the standard cross-stone work_daily_bonus that every leveled stone
    # carries.
    # ──────────────────────────────────────────────────────────────────────────
    "bloomstone": {
        "name":        "Bloomstone",
        "emoji":       "\U0001F33C",  # blossom
        "category":    "item",
        "description": (
            "A pulse of green bloom locked in stone. Boosts crop yield "
            "and SEED drops; levels up as you plant, harvest, and process."
        ),

        "accepted_currencies": ("HRV",),
        "cost_stable":         int(6_500 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(130 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  70,
        "xp_per_plant":        5.0,
        "xp_per_harvest":     20.0,
        "xp_per_legendary":  400.0,   # legendary-rarity crops only
        "xp_per_recipe":      50.0,   # processed recipe (bread, jam, etc.)
        "xp_per_pest_kill":   15.0,

        "stats": {
            "farm_yield_bonus":     0.0030,  # +0.30% crop qty per level -> +30% at L100
            "farm_seed_drop_bonus": 0.0030,  # +0.30% SEED drop per level
            "work_daily_bonus":     0.0010,  # standard cross-stone token
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Bloodstone  -  buddy battle gem.
    #
    # Levels up via XP earned from buddy battles (PvP, wild fights, dungeon
    # captures-via-battle). Boosts ATK and HP for the OWNER's active buddy
    # in battle, plus the USD prize on a win.
    # ──────────────────────────────────────────────────────────────────────────
    "bloodstone": {
        "name":        "Bloodstone",
        "emoji":       "\U0001FA78",  # drop of blood
        "category":    "item",
        "description": (
            "A dark crimson shard that throbs in time with a pet's heart. "
            "Boosts your active buddy's ATK and HP in battle and the USD "
            "prize on a win; levels up every time your buddy fights."
        ),

        "accepted_currencies": ("BBT",),
        "cost_stable":         int(7_500 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(150 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  80,
        "xp_per_battle_round": 5.0,
        "xp_per_battle_win":   200.0,
        "xp_per_battle_loss":  40.0,
        "xp_per_capture_battle": 80.0,

        "stats": {
            "battle_atk_bonus":    0.0025,  # +0.25% ATK per level
            "battle_hp_bonus":     0.0020,  # +0.20% HP per level
            "battle_prize_bonus":  0.0030,  # +0.30% USD prize per level on win
            "work_daily_bonus":    0.0010,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Gavelstone  -  auction-house meta gem (USD-priced).
    #
    # Levels up via XP from buying and selling on the AH (the seller's
    # stone gets XP when their listing settles, NOT when it's posted -- so
    # spam-listing doesn't farm XP). Pays buyer rebates on every purchase
    # and seller bonuses on every settled sale, both scaling with level.
    # Cost + level-ups are pure USD so the AH meta layer doesn't tilt
    # toward any single network token.
    # ──────────────────────────────────────────────────────────────────────────
    "gavelstone": {
        "name":        "Gavelstone",
        "emoji":       "\U0001FA99",  # coin (gavel-adjacent)
        "category":    "item",
        "description": (
            "An auctioneer's gavel locked in glass. Pays buyer rebates "
            "on every AH purchase and seller bonuses on every settled "
            "sale; levels up as you buy and sell on the auction house."
        ),

        "accepted_currencies": ("USD",),
        "cost_stable":         int(8_000 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(160 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  80,
        "xp_per_buy":         20.0,   # one ah buy action
        "xp_per_sale":        20.0,   # one settled listing of yours

        "stats": {
            "ah_buyer_rebate":  0.0020,  # +0.20% per level rebate to buyer
            "ah_seller_bonus":  0.0020,  # +0.20% per level bonus to seller
            "work_daily_bonus": 0.0010,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Anvilstone  -  crafting meta gem (FORGE-priced).
    #
    # Levels up via XP earned from each ,craft action (one grant per call,
    # regardless of qty -- a bulk craft of 50 grants the same XP as a 1).
    # Boosts per-craft output qty: a level-N Anvilstone effectively prints
    # extra crafted items on top of the recipe's base output, no extra
    # input cost. Pay in FORGE (the Forge-Network coin) so the crafting
    # meta layer keeps its stake currency aligned with the network it
    # boosts -- mirrors how Cryptstone takes RUNE, Tidestone takes REEL,
    # etc. ``cost_stable`` is the USD-equivalent target; the buy + level-
    # up flows convert to FORGE at the live oracle.
    # ──────────────────────────────────────────────────────────────────────────
    "anvilstone": {
        "name":        "Anvilstone",
        "emoji":       "\U0001F528",  # hammer
        "category":    "item",
        "description": (
            "A heat-warped block from a forge that never went cold. "
            "Boosts crafting output qty for free; levels up every time "
            "you craft."
        ),

        "accepted_currencies": ("FORGE",),
        "cost_stable":         int(7_500 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(150 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  75,
        "xp_per_craft":       30.0,   # one ,craft action (any qty)

        "stats": {
            "craft_yield_bonus": 0.0030,  # +0.30% per level extra qty
            "craft_xp_bonus":    0.0020,  # +0.20% per level on craft skill xp
            "work_daily_bonus":  0.0010,
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Chimerastone  -  AMM-swap meta gem (USD-priced).
    #
    # Levels up ONLY through ,swap / ,trade swap actions (not bare
    # ,buy / ,sell). Stacks on top of the existing Liqstone swap-fee
    # discount: where a max Liqstone hits a -10% baseline, a max
    # Chimerastone shaves another -10% off the residual fee. Cost +
    # level-ups are pure USD.
    # ──────────────────────────────────────────────────────────────────────────
    "chimerastone": {
        "name":        "Chimerastone",
        "emoji":       "\U0001F52E",  # crystal ball
        "category":    "item",
        "description": (
            "A faceted shard that splits one token into another. "
            "Reduces AMM swap fees on top of any Liqstone discount; "
            "levels up each time you ,swap."
        ),

        "accepted_currencies": ("USD",),
        "cost_stable":         int(7_000 * _S),
        "buy_fee_pct":         0.05,
        "sell_fee_pct":        0.05,
        "transfer_fee_stable": int(140 * _S),

        "leveled":            True,
        "max_level":          100,
        "xp_per_level_base":  70,
        "xp_per_swap":        25.0,   # one ,swap action

        "stats": {
            "swap_fee_bonus":   0.0010,  # +0.10% per level extra fee discount
            "work_daily_bonus": 0.0010,
        },
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# GROUP HALL UPGRADES  -  purchasable by mining group founders using the group
# reserve (USD equivalent).  Each upgrade permanently enhances the group's
# private Hall thread and stacks additively with other upgrades.
#
# Upgrades are organised into five lines:
#   atmosphere  -  cosmetic + small earnings bonuses while inside the Hall
#   access      -  unlock bot command categories inside the Hall
#   expansion   -  extra member slots + stacked Hall bonuses
#   industry    -  group-wide bonuses on fishing / farming / delves / crafting
#                  (apply to all members anywhere, not just inside the Hall)
#   tribute     -  small system-funded grant into the group reserve every time
#                  a member cashes out from a productive system. The reserve
#                  grows as the group plays, even when no one is mining.
#
# EFFECT KEYS:
#   --- HALL-ONLY (only when commands run inside the Hall thread) ---
#   hall_gambling_bonus    -  % bonus on gambling winnings (0.05 = +5%)
#   hall_daily_bonus       -  % bonus on daily reward
#   hall_work_bonus        -  % bonus on work earnings
#   hall_unlock            -  category unlocked in Hall ("Earn"/"Trading"/"DeFi"/"Play"/"Industry")
#
#   --- GROUP-WIDE (apply to all group members anywhere) ---
#   member_fishing_bonus   -  % bonus on fishing cashout payouts
#   member_farming_bonus   -  % bonus on farming cashout payouts
#   member_dungeon_bonus   -  % bonus on delve / dungeon cashout payouts
#   member_crafting_bonus  -  % bonus on crafting yields
#
#   --- TRIBUTE (system-funded grant into reserve_usd, no tax on the user) ---
#   tribute_fishing_pct    -  % of member's fishing cashout granted to reserve
#   tribute_farming_pct    -  % of member's farming cashout granted to reserve
#   tribute_dungeon_pct    -  % of member's dungeon cashout granted to reserve
#   tribute_crafting_pct   -  % of crafting cashout value granted to reserve
#   tribute_multiplier     -  global multiplier on every tribute_* effect above
#
#   --- META ---
#   group_token_trading    -  True  -> enables the group token for market trading
#   group_max_members      -  +N extra member slots (integer)
# ──────────────────────────────────────────────────────────────────────────────
GROUP_HALL_UPGRADES: dict[str, dict] = {

    # ── Atmosphere line ───────────────────────────────────────────────────────
    "hearth": {
        "name":        "Hall Hearth",
        "emoji":       "🔥",
        "line":        "atmosphere",
        "description": "A roaring fireplace lifts the mood. Members earn +5% on gambling winnings inside the Hall.",
        "cost_usd":    int(35_000 * _S),
        "tier":        1,
        "requires":    [],
        "effect": {
            "hall_gambling_bonus": 0.05,
            "hall_unlock": "Play",
        },
    },
    "trophy_wall": {
        "name":        "Trophy Wall",
        "emoji":       "🏆",
        "line":        "atmosphere",
        "description": "Your victories on display. Members earn +5% on daily rewards inside the Hall.",
        "cost_usd":    int(90_000 * _S),
        "tier":        2,
        "requires":    ["hearth"],
        "effect": {
            "hall_daily_bonus": 0.05,
            "hall_unlock": "Earn",
        },
    },
    "gilded_arch": {
        "name":        "Gilded Arch",
        "emoji":       "✨",
        "line":        "atmosphere",
        "description": "Gold trim on every surface. Members earn +5% on work earnings inside the Hall.",
        "cost_usd":    int(280_000 * _S),
        "tier":        3,
        "requires":    ["trophy_wall"],
        "effect": {
            "hall_work_bonus": 0.05,
            "hall_unlock": "Earn",
        },
    },

    # ── Access line ───────────────────────────────────────────────────────────
    "command_board": {
        "name":        "Command Board",
        "emoji":       "📋",
        "line":        "access",
        "description": "A pinboard of shortcuts. Unlocks earning commands (work, daily, faucet) inside the Hall.",
        "cost_usd":    int(75_000 * _S),
        "tier":        1,
        "requires":    [],
        "effect": {
            "hall_unlock": "Earn",
        },
    },
    "trading_desk": {
        "name":        "Trading Desk",
        "emoji":       "💹",
        "line":        "access",
        "description": "Full trading setup. Unlocks trade commands and enables the group token for open market trading.",
        "cost_usd":    int(225_000 * _S),
        "tier":        2,
        "requires":    ["command_board"],
        "effect": {
            "hall_unlock":         "Trading",
            "group_token_trading": True,
        },
    },
    "defi_terminal": {
        "name":        "DeFi Terminal",
        "emoji":       "🖥️",
        "line":        "access",
        "description": "Advanced financial infrastructure. Unlocks LP and DeFi commands inside the Hall.",
        "cost_usd":    int(650_000 * _S),
        "tier":        3,
        "requires":    ["trading_desk"],
        "effect": {
            "hall_unlock": "DeFi",
        },
    },

    # ── Expansion line ────────────────────────────────────────────────────────
    "member_wing": {
        "name":        "Member Wing",
        "emoji":       "🏗️",
        "line":        "expansion",
        "description": "Expand the Hall with a new wing. +5 member slots for your group.",
        "cost_usd":    int(120_000 * _S),
        "tier":        1,
        "requires":    [],
        "effect": {
            "group_max_members": 5,
        },
    },
    "grand_vault": {
        "name":        "Grand Vault",
        "emoji":       "🏛️",
        "line":        "expansion",
        "description": "A towering vault adds prestige. +8% gambling winnings inside the Hall (stacks with Hall Hearth).",
        "cost_usd":    int(480_000 * _S),
        "tier":        2,
        "requires":    ["hearth", "member_wing"],
        "effect": {
            "hall_gambling_bonus": 0.08,
        },
    },
    "grand_atrium": {
        "name":        "Grand Atrium",
        "emoji":       "🕍",
        "line":        "expansion",
        "description": "Cathedral-scale gathering space. +10 member slots and +3% on every Hall earnings bonus already purchased.",
        "cost_usd":    int(1_400_000 * _S),
        "tier":        3,
        "requires":    ["grand_vault"],
        "effect": {
            "group_max_members":   10,
            "hall_gambling_bonus": 0.03,
            "hall_daily_bonus":    0.03,
            "hall_work_bonus":     0.03,
        },
    },
    "prestige_court": {
        "name":        "Prestige Court",
        "emoji":       "👑",
        "line":        "expansion",
        "description": "A throne room befitting a sovereign guild. +15 member slots and +5% across the board on Hall bonuses.",
        "cost_usd":    int(4_200_000 * _S),
        "tier":        4,
        "requires":    ["grand_atrium"],
        "effect": {
            "group_max_members":   15,
            "hall_gambling_bonus": 0.05,
            "hall_daily_bonus":    0.05,
            "hall_work_bonus":     0.05,
        },
    },

    # ── Atmosphere extension (T4-T5) ──────────────────────────────────────────
    "crystal_chandelier": {
        "name":        "Crystal Chandelier",
        "emoji":       "💎",
        "line":        "atmosphere",
        "description": "Refracted light raises the room's spirits. +10% gambling and +5% daily inside the Hall.",
        "cost_usd":    int(900_000 * _S),
        "tier":        4,
        "requires":    ["gilded_arch"],
        "effect": {
            "hall_gambling_bonus": 0.10,
            "hall_daily_bonus":    0.05,
        },
    },
    "eternal_flame": {
        "name":        "Eternal Flame",
        "emoji":       "🔯",
        "line":        "atmosphere",
        "description": "A shrine that never dims. +5% on gambling, daily, and work inside the Hall (capstone of Atmosphere).",
        "cost_usd":    int(2_500_000 * _S),
        "tier":        5,
        "requires":    ["crystal_chandelier"],
        "effect": {
            "hall_gambling_bonus": 0.05,
            "hall_daily_bonus":    0.05,
            "hall_work_bonus":     0.05,
        },
    },

    # ── Industry line (group-wide bonuses + new command unlocks) ──────────────
    "anglers_dock": {
        "name":        "Angler's Dock",
        "emoji":       "🎣",
        "line":        "industry",
        "description": "A private pier and tackle bench. Every member earns +5% on fishing cashouts (anywhere) and gains the Industry command unlock in the Hall.",
        "cost_usd":    int(180_000 * _S),
        "tier":        1,
        "requires":    [],
        "effect": {
            "member_fishing_bonus": 0.05,
            "hall_unlock":          "Industry",
        },
    },
    "greenhouse_wing": {
        "name":        "Greenhouse Wing",
        "emoji":       "🌱",
        "line":        "industry",
        "description": "Glass roofs over irrigated rows. +5% on farming cashouts for every member.",
        "cost_usd":    int(180_000 * _S),
        "tier":        1,
        "requires":    [],
        "effect": {
            "member_farming_bonus": 0.05,
            "hall_unlock":          "Industry",
        },
    },
    "delve_bastion": {
        "name":        "Delve Bastion",
        "emoji":       "⛓️",
        "line":        "industry",
        "description": "Reinforced gates onto the deep paths. +5% on dungeon / delve cashouts for every member.",
        "cost_usd":    int(220_000 * _S),
        "tier":        1,
        "requires":    [],
        "effect": {
            "member_dungeon_bonus": 0.05,
            "hall_unlock":          "Industry",
        },
    },
    "forge_workshop": {
        "name":        "Forge Workshop",
        "emoji":       "⚒️",
        "line":        "industry",
        "description": "Anvils, bellows, and a master smith. +5% yield on every craft a member completes.",
        "cost_usd":    int(220_000 * _S),
        "tier":        1,
        "requires":    [],
        "effect": {
            "member_crafting_bonus": 0.05,
            "hall_unlock":           "Industry",
        },
    },
    "guild_market": {
        "name":        "Guild Market",
        "emoji":       "🏪",
        "line":        "industry",
        "description": "A bustling members-only bazaar where every productive system stacks. +5% on fishing, farming, delves, and crafting for every member.",
        "cost_usd":    int(1_800_000 * _S),
        "tier":        2,
        "requires":    ["anglers_dock", "greenhouse_wing", "delve_bastion", "forge_workshop"],
        "effect": {
            "member_fishing_bonus":  0.05,
            "member_farming_bonus":  0.05,
            "member_dungeon_bonus":  0.05,
            "member_crafting_bonus": 0.05,
        },
    },
    "master_industries": {
        "name":        "Master Industries",
        "emoji":       "🏗️",
        "line":        "industry",
        "description": "Capstone of the Industry line. +10% on fishing, farming, delves, and crafting -- stacks with every prior tier.",
        "cost_usd":    int(6_000_000 * _S),
        "tier":        3,
        "requires":    ["guild_market"],
        "effect": {
            "member_fishing_bonus":  0.10,
            "member_farming_bonus":  0.10,
            "member_dungeon_bonus":  0.10,
            "member_crafting_bonus": 0.10,
        },
    },

    # ── Tribute line (reserve-growth grants on member productivity) ───────────
    "tithe_box": {
        "name":        "Tithe Box",
        "emoji":       "🪙",
        "line":        "tribute",
        "description": "A simple offering box at the gate. Every member's fishing or farming cashout grants 1% of its value to the reserve as a system-funded tribute (members keep their full payout).",
        "cost_usd":    int(120_000 * _S),
        "tier":        1,
        "requires":    [],
        "effect": {
            "tribute_fishing_pct": 0.01,
            "tribute_farming_pct": 0.01,
        },
    },
    "deep_coffer": {
        "name":        "Deep Coffer",
        "emoji":       "🗝️",
        "line":        "tribute",
        "description": "Iron-bound chest deep in the vault. +1% delve and +1% crafting tribute on every member's cashout (system-funded into the reserve).",
        "cost_usd":    int(260_000 * _S),
        "tier":        2,
        "requires":    ["tithe_box"],
        "effect": {
            "tribute_dungeon_pct":  0.01,
            "tribute_crafting_pct": 0.01,
        },
    },
    "guild_mint": {
        "name":        "Guild Mint",
        "emoji":       "💠",
        "line":        "tribute",
        "description": "A working mint stamps grants daily. Boosts every tribute by +50% (multiplicative, stacks with all four cashout tributes).",
        "cost_usd":    int(900_000 * _S),
        "tier":        3,
        "requires":    ["deep_coffer"],
        "effect": {
            "tribute_multiplier": 0.50,
        },
    },
    "sovereign_treasury": {
        "name":        "Sovereign Treasury",
        "emoji":       "🏦",
        "line":        "tribute",
        "description": "A treasury worthy of a kingdom. Doubles every cashout tribute on top of the Guild Mint and adds an extra +1% across the board.",
        "cost_usd":    int(3_500_000 * _S),
        "tier":        4,
        "requires":    ["guild_mint"],
        "effect": {
            "tribute_fishing_pct":  0.01,
            "tribute_farming_pct":  0.01,
            "tribute_dungeon_pct":  0.01,
            "tribute_crafting_pct": 0.01,
            "tribute_multiplier":   1.00,
        },
    },
}
