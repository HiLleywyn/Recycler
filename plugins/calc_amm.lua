--[[
  plugins/calc_amm.lua -- Uniswap-v2 constant-product AMM price impact calculator.

  Registers: calc.amm_swap
  Category:  defi
  Risk:      read

  The AI should call this AFTER fetching pool reserves via defi.pool_info or
  market.pool, then pass those reserves plus the user's desired input amount
  to get the exact output and price impact before any swap is executed.

  Formula: Uniswap v2 constant-product (x * y = k) with configurable LP fee.
    amount_out = (amount_in * fee_mult * reserve_out) / (reserve_in + amount_in * fee_mult)
    price_impact = 1 - (effective_price / spot_price)
--]]

tool_api.register({
    name     = "calc.amm_swap",
    summary  = "Exact AMM swap output and price impact. Given pool reserves and input amount, returns amount_out, price_impact_pct, LP fee, and effective price. Call after fetching reserves via defi.pool_info.",
    risk     = "read",
    category = "defi",
    params   = {
        {
            name        = "reserve_in",
            type        = "float",
            required    = true,
            description = "Current pool reserve of the input token (human units, not raw).",
        },
        {
            name        = "reserve_out",
            type        = "float",
            required    = true,
            description = "Current pool reserve of the output token (human units, not raw).",
        },
        {
            name        = "amount_in",
            type        = "float",
            required    = true,
            description = "Amount of input token the user wants to swap (human units).",
        },
        {
            name        = "fee_bps",
            type        = "int",
            required    = false,
            default     = 30,
            description = "LP fee in basis points. Default 30 = 0.30% (standard Uniswap v2). Use 25 for 0.25% pools.",
            min         = 0,
            max         = 1000,
        },
    },
    handler = function(args)
        local r_in  = tonumber(args.reserve_in)
        local r_out = tonumber(args.reserve_out)
        local a_in  = tonumber(args.amount_in)
        local fee   = tonumber(args.fee_bps) or 30

        if not r_in or r_in <= 0 then
            return tool_api.fail("reserve_in must be > 0")
        end
        if not r_out or r_out <= 0 then
            return tool_api.fail("reserve_out must be > 0")
        end
        if not a_in or a_in <= 0 then
            return tool_api.fail("amount_in must be > 0")
        end
        -- Prevent inputs that would effectively drain the pool (>99% of reserves)
        if a_in >= r_in * 0.99 then
            return tool_api.fail("amount_in is >= 99% of pool reserves -- would drain the pool and produce an invalid result")
        end

        -- Uniswap v2: apply fee to the input, then apply x*y=k
        local fee_mult        = 1.0 - (fee / 10000.0)
        local amount_in_adj   = a_in * fee_mult
        local amount_out      = (amount_in_adj * r_out) / (r_in + amount_in_adj)

        -- Spot price = how many output tokens per input token at zero size
        local spot_price      = r_out / r_in
        -- Effective price = what the user actually receives per input token
        local effective_price = amount_out / a_in
        -- Price impact = how much the execution price degraded from spot
        local impact_pct      = (1.0 - (effective_price / spot_price)) * 100.0
        -- Fee paid to LPs
        local lp_fee          = a_in * (fee / 10000.0)

        -- Post-swap pool state (informational)
        local new_r_in  = r_in + a_in
        local new_r_out = r_out - amount_out

        -- Human-readable severity label
        local severity
        if impact_pct < 0.5 then
            severity = "low (<0.5%)"
        elseif impact_pct < 2.0 then
            severity = "moderate (0.5-2%)"
        elseif impact_pct < 5.0 then
            severity = "high (2-5%) - consider splitting the trade"
        else
            severity = "severe (>5%) - strongly consider a smaller trade size"
        end

        tool_api.log(string.format(
            "calc.amm_swap: %.4f in -> %.4f out, impact=%.3f%%, fee=%.4f",
            a_in, amount_out, impact_pct, lp_fee
        ))

        return tool_api.ok({
            amount_out       = amount_out,
            lp_fee           = lp_fee,
            price_impact_pct = impact_pct,
            impact_severity  = severity,
            spot_price       = spot_price,
            effective_price  = effective_price,
            new_reserve_in   = new_r_in,
            new_reserve_out  = new_r_out,
        })
    end,
})
