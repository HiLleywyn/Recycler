--[[
  plugins/calc_loan.lua -- Collateralized loan health factor and liquidation calculator.

  Registers: calc.loan_health
  Category:  risk
  Risk:      read

  Calculates the health factor of a collateralized position and tells the
  user exactly how much the collateral price has to drop before liquidation.
  Works with any collateral/debt denomination -- just provide USD values.

  Health factor = collateral_usd / (debt_usd * liquidation_threshold)
    HF > 1.0  -- safe
    HF < 1.0  -- liquidatable right now
--]]

tool_api.register({
    name     = "calc.loan_health",
    summary  = "Loan health factor and liquidation price calculator. Given collateral USD value, debt USD value, and optional current token price, returns health factor, safety status, USD/% buffer before liquidation, and the token price that triggers liquidation.",
    risk     = "read",
    category = "risk",
    params   = {
        {
            name        = "collateral_usd",
            type        = "float",
            required    = true,
            description = "Current USD value of the collateral (human units).",
            min         = 0.01,
        },
        {
            name        = "debt_usd",
            type        = "float",
            required    = true,
            description = "Current USD value of the outstanding debt including accrued interest (human units).",
            min         = 0.01,
        },
        {
            name        = "liquidation_threshold",
            type        = "float",
            required    = false,
            default     = 1.25,
            description = "Collateral-to-debt ratio at which liquidation triggers. Default 1.25 means collateral must stay 25% above debt value.",
            min         = 1.0,
            max         = 5.0,
        },
        {
            name        = "collateral_token_price",
            type        = "float",
            required    = false,
            default     = 0,
            description = "Current price of the collateral token in USD. Optional -- if provided, calculates the exact token price that triggers liquidation.",
            min         = 0,
        },
    },
    handler = function(args)
        local col_usd   = tonumber(args.collateral_usd)
        local debt_usd  = tonumber(args.debt_usd)
        local threshold = tonumber(args.liquidation_threshold) or 1.25
        local col_price = tonumber(args.collateral_token_price) or 0

        if not col_usd or col_usd <= 0 then
            return tool_api.fail("collateral_usd must be > 0")
        end
        if not debt_usd or debt_usd <= 0 then
            return tool_api.fail("debt_usd must be > 0")
        end
        if threshold < 1.0 then
            return tool_api.fail("liquidation_threshold must be >= 1.0 (a ratio below 1.0 would mean undercollateralized by design)")
        end

        -- Health factor: how many times over the liquidation threshold the collateral covers the debt.
        -- HF = 1.0 means exactly at the liquidation line.
        local hf = col_usd / (debt_usd * threshold)

        -- The USD collateral value at which liquidation would trigger
        local liq_col_usd = debt_usd * threshold

        -- How much the collateral value can fall before liquidation
        local buffer_usd = col_usd - liq_col_usd
        local buffer_pct = (buffer_usd / col_usd) * 100.0

        -- If a current token price was given, derive the liquidation token price.
        -- col_usd = col_amount * col_price, so col_amount = col_usd / col_price.
        -- Liquidation when col_amount * liq_price = liq_col_usd
        -- => liq_price = liq_col_usd / col_amount = liq_col_usd / (col_usd / col_price)
        --              = col_price * (liq_col_usd / col_usd)
        local liq_token_price = 0
        local price_drop_pct  = 0
        if col_price > 0 then
            liq_token_price = col_price * (liq_col_usd / col_usd)
            price_drop_pct  = ((col_price - liq_token_price) / col_price) * 100.0
        end

        -- Human-readable status
        local status
        if hf >= 2.0 then
            status = "safe - well above liquidation threshold"
        elseif hf >= 1.5 then
            status = "safe"
        elseif hf >= 1.15 then
            status = "warning - approaching liquidation, consider adding collateral or repaying debt"
        elseif hf >= 1.0 then
            status = "danger - very close to liquidation, act now"
        else
            status = "LIQUIDATABLE - health factor is below 1.0, position can be liquidated immediately"
        end

        tool_api.log(string.format(
            "calc.loan_health: HF=%.3f, buffer_usd=%.2f (%.1f%%), liq_price=%.4f",
            hf, buffer_usd, buffer_pct, liq_token_price
        ))

        return tool_api.ok({
            health_factor     = hf,
            status            = status,
            buffer_usd        = buffer_usd,
            buffer_pct        = buffer_pct,
            liq_threshold_usd = liq_col_usd,
            liq_token_price   = liq_token_price,
            price_drop_pct    = price_drop_pct,
        })
    end,
})
