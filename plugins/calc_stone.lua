--[[
  plugins/calc_stone.lua -- Stone item stat calculator.

  Registers: calc.stone_stats
  Category:  economy
  Risk:      read

  Calculates stat bonuses and XP requirements for all four leveled stone types
  at any level. Values mirrored exactly from items_config.py.

  Leveling formula (same for all stones):
    XP to go from level N to N+1  =  N * xp_per_level_base
    XP to reach level L from 1    =  sum(N * base for N = 1..L-1)
                                   =  base * (L-1)*L/2   (triangular number)
--]]

-- Stone definitions mirrored from items_config.py
local STONES = {
    hashstone = {
        name          = "Hashstone",
        emoji         = "Hashstone",
        cost_dsd      = 7500,
        buy_fee_pct   = 5,   -- percent
        sell_fee_pct  = 5,
        xp_base       = 80,  -- XP for level N->N+1 = N * xp_base
        max_level     = 100,
        -- stat_name -> bonus per level (multiply by level for total)
        stats = {
            {key="work_daily_bonus", label="Work/Daily bonus",    per_level_pct=0.3,   max_pct=30.0},
            {key="mining_bonus",     label="Mining hashrate bonus", per_level_pct=0.24, max_pct=24.0},
        },
    },
    lockstone = {
        name          = "Lockstone",
        emoji         = "Lockstone",
        cost_dsd      = 6000,
        buy_fee_pct   = 5,
        sell_fee_pct  = 5,
        xp_base       = 65,
        max_level     = 100,
        stats = {
            {key="work_daily_bonus", label="Work/Daily bonus",  per_level_pct=0.24, max_pct=24.0},
            {key="stake_bonus",      label="Staking rewards",   per_level_pct=0.3,  max_pct=30.0},
        },
    },
    vaultstone = {
        name          = "Vaultstone",
        emoji         = "Vaultstone",
        cost_dsd      = 5000,
        buy_fee_pct   = 4,
        sell_fee_pct  = 4,
        xp_base       = 50,
        max_level     = 100,
        stats = {
            {key="work_daily_bonus", label="Work/Daily bonus",  per_level_pct=0.2,  max_pct=20.0},
            {key="interest_bonus",   label="Savings interest",  per_level_pct=0.36, max_pct=36.0},
        },
    },
    liqstone = {
        name          = "Liqstone",
        emoji         = "Liqstone",
        cost_dsd      = 8000,
        buy_fee_pct   = 5,
        sell_fee_pct  = 5,
        xp_base       = 70,
        max_level     = 100,
        stats = {
            {key="work_daily_bonus",  label="Work/Daily bonus",    per_level_pct=0.15,  max_pct=15.0},
            {key="swap_fee_discount", label="Swap fee discount",   per_level_pct=0.1,   max_pct=10.0},
            {key="lp_reward_bonus",   label="LP reward share",     per_level_pct=0.2,   max_pct=20.0},
        },
    },
}

-- Total XP needed to reach level `to_lv` from level `from_lv`
-- Uses triangular number formula: base * (sum from N=from_lv to to_lv-1)
-- sum(N, from_lv, to_lv-1) = (to_lv-1)*to_lv/2 - (from_lv-1)*from_lv/2
local function xp_between(stone, from_lv, to_lv)
    if to_lv <= from_lv then return 0 end
    local b = stone.xp_base
    local hi = to_lv - 1
    local lo = from_lv - 1
    -- Sum of N for N = from_lv..to_lv-1  =  sum(1..hi) - sum(1..lo)
    local sum_hi = (hi * (hi + 1)) / 2
    local sum_lo = (lo * (lo + 1)) / 2
    return b * (sum_hi - sum_lo)
end

tool_api.register({
    name     = "calc.stone_stats",
    summary  = "Stone item stat calculator. Given stone type and current level, returns current stat bonuses, XP to next level, and total XP to max. If target_level is set, also shows bonus gains and XP needed to reach it.",
    risk     = "read",
    category = "economy",
    params   = {
        {
            name        = "stone_type",
            type        = "str",
            required    = true,
            description = "Stone type: hashstone, lockstone, vaultstone, or liqstone.",
            choices     = {"hashstone", "lockstone", "vaultstone", "liqstone"},
        },
        {
            name        = "current_level",
            type        = "int",
            required    = true,
            description = "The stone's current level (1 to 100).",
            min         = 1,
            max         = 100,
        },
        {
            name        = "target_level",
            type        = "int",
            required    = false,
            default     = 0,
            description = "Optional: show stats and XP cost to reach this level. 0 means just show current level info.",
            min         = 0,
            max         = 100,
        },
    },
    handler = function(args)
        local stone_key = tostring(args.stone_type or "")
        local cur_lv    = math.floor(tonumber(args.current_level) or 1)
        local tgt_lv    = math.floor(tonumber(args.target_level) or 0)

        local s = STONES[stone_key]
        if not s then
            return tool_api.fail(
                "Unknown stone type '" .. stone_key .. "'. " ..
                "Valid types: hashstone, lockstone, vaultstone, liqstone."
            )
        end
        if cur_lv < 1 or cur_lv > s.max_level then
            return tool_api.fail(
                "current_level must be 1 to " .. s.max_level
            )
        end

        -- Current stat bonuses at cur_lv
        local cur_stats = {}
        for _, stat in ipairs(s.stats) do
            cur_stats[stat.key] = {
                bonus_pct = stat.per_level_pct * cur_lv,
                label     = stat.label,
            }
        end

        -- XP to go from cur_lv to cur_lv+1 (next level cost)
        local xp_to_next = (cur_lv < s.max_level)
            and (cur_lv * s.xp_base)
            or 0  -- already max

        -- Total XP accumulated from level 1 to cur_lv
        local xp_accumulated = xp_between(s, 1, cur_lv)

        -- Total XP still needed to hit max level
        local xp_to_max = xp_between(s, cur_lv, s.max_level)

        -- Max-level stats for reference
        local max_stats = {}
        for _, stat in ipairs(s.stats) do
            max_stats[stat.key] = {
                bonus_pct = stat.max_pct,
                label     = stat.label,
            }
        end

        local result = {
            stone            = s.name,
            current_level    = cur_lv,
            max_level        = s.max_level,
            cost_dsd         = s.cost_dsd,
            buy_fee_pct      = s.buy_fee_pct,
            sell_fee_pct     = s.sell_fee_pct,
            xp_per_level     = s.xp_base,
            current_stats    = cur_stats,
            max_stats        = max_stats,
            xp_to_next_level = xp_to_next,
            xp_accumulated   = xp_accumulated,
            xp_to_max        = xp_to_max,
            at_max           = (cur_lv >= s.max_level),
        }

        -- Target level upgrade details
        if tgt_lv > 0 then
            if tgt_lv <= cur_lv then
                return tool_api.fail("target_level must be greater than current_level (" .. cur_lv .. ")")
            end
            if tgt_lv > s.max_level then
                return tool_api.fail("target_level cannot exceed max level (" .. s.max_level .. ")")
            end

            local xp_needed = xp_between(s, cur_lv, tgt_lv)
            local tgt_stats = {}
            local gains     = {}
            for _, stat in ipairs(s.stats) do
                local cur_val = stat.per_level_pct * cur_lv
                local tgt_val = stat.per_level_pct * tgt_lv
                tgt_stats[stat.key] = {bonus_pct = tgt_val, label = stat.label}
                gains[stat.key]     = {gain_pct  = tgt_val - cur_val, label = stat.label}
            end

            result.upgrade = {
                target_level  = tgt_lv,
                xp_needed     = xp_needed,
                target_stats  = tgt_stats,
                stat_gains    = gains,
                levels_gained = tgt_lv - cur_lv,
            }
        end

        tool_api.log(string.format(
            "calc.stone_stats: %s lv%d -> xp_to_next=%d, xp_to_max=%d",
            stone_key, cur_lv, xp_to_next, xp_to_max
        ))

        return tool_api.ok(result)
    end,
})
