--[[
  plugins/calc_yield.lua -- Yield and APY projection calculator.

  Registers: calc.yield_projection
  Category:  economy
  Risk:      read

  Calculates both simple and compound yield projections given a principal,
  daily rate, and time horizon. Useful for staking, savings, yield farming,
  and validator delegation ROI questions.

  Simple interest:   earned = principal * rate * days
  Compound interest: final  = principal * (1 + daily_rate) ^ days
  APY (compound):    apy    = (1 + daily_rate)^365 - 1
--]]

tool_api.register({
    name     = "calc.yield_projection",
    summary  = "Yield and APY calculator. Given principal, daily rate %, and days, returns total earned, final value, and effective APY for both simple and compound interest. Use for staking, savings, yield farming, and validator reward questions.",
    risk     = "read",
    category = "economy",
    params   = {
        {
            name        = "principal",
            type        = "float",
            required    = true,
            description = "Starting amount (USD or token units, human scale).",
            min         = 0.01,
        },
        {
            name        = "daily_rate_pct",
            type        = "float",
            required    = true,
            description = "Daily yield rate as a percentage. E.g. 5 means 5%/day, 0.1 means 0.1%/day. Discoin yield farming is 3-9%/day.",
            min         = 0.001,
            max         = 50.0,
        },
        {
            name        = "days",
            type        = "int",
            required    = false,
            default     = 30,
            description = "Number of days to project. Default 30. Capped at 3650 (10 years).",
            min         = 1,
            max         = 3650,
        },
    },
    handler = function(args)
        local principal = tonumber(args.principal)
        local rate_pct  = tonumber(args.daily_rate_pct)
        local days      = math.floor(tonumber(args.days) or 30)

        if not principal or principal <= 0 then
            return tool_api.fail("principal must be > 0")
        end
        if not rate_pct or rate_pct <= 0 then
            return tool_api.fail("daily_rate_pct must be > 0")
        end
        if days < 1 or days > 3650 then
            return tool_api.fail("days must be 1 to 3650")
        end

        local r = rate_pct / 100.0  -- decimal

        -- Simple interest
        local simple_earned = principal * r * days
        local simple_final  = principal + simple_earned
        local simple_apy    = r * 365.0 * 100.0  -- no compounding, just annualised

        -- Daily compound interest: final = principal * (1 + r)^days
        local compound_final  = principal * ((1.0 + r) ^ days)
        local compound_earned = compound_final - principal
        -- Effective APY with daily compounding
        local compound_apy    = ((1.0 + r) ^ 365.0 - 1.0) * 100.0

        -- How much more compound beats simple as a % of simple earnings
        local compound_advantage_pct = 0.0
        if simple_earned > 0 then
            compound_advantage_pct = ((compound_earned - simple_earned) / simple_earned) * 100.0
        end

        -- Daily breakdown for short projections (up to 7 days)
        local daily_compound = {}
        if days <= 7 then
            local running = principal
            for d = 1, days do
                local day_earned = running * r
                running = running + day_earned
                daily_compound[d] = {
                    day          = d,
                    earned       = day_earned,
                    running_total = running,
                }
            end
        end

        -- Risk notes
        local slash_note = nil
        if rate_pct >= 3.0 then
            slash_note = "Yield farming rates above 3%/day carry slash risk. Yield Guard consumables protect principal from haircuts."
        end

        tool_api.log(string.format(
            "calc.yield_projection: principal=%.2f, rate=%.3f%%/day, days=%d, compound_final=%.2f",
            principal, rate_pct, days, compound_final
        ))

        return tool_api.ok({
            principal        = principal,
            days             = days,
            daily_rate_pct   = rate_pct,
            simple = {
                total_earned = simple_earned,
                final_value  = simple_final,
                apy_pct      = simple_apy,
            },
            compound = {
                total_earned = compound_earned,
                final_value  = compound_final,
                apy_pct      = compound_apy,
            },
            compound_advantage_pct = compound_advantage_pct,
            daily_breakdown        = #daily_compound > 0 and daily_compound or nil,
            slash_risk_note        = slash_note,
        })
    end,
})
