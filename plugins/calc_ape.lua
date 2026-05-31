--[[
  plugins/calc_ape.lua -- APE gambling expected value and outcome breakdown.

  Registers: calc.ape_ev
  Category:  economy
  Risk:      read

  Uses the exact probability weights and payout ranges from cogs/earn.py.
  Outcome weights (sum = 10000):
    rugged     8400  (84.00%) -- lose entire bet
    break_even  949  ( 9.49%) -- 0.8x-1.5x return
    moon        450  ( 4.50%) -- 5x-12x
    legendary   100  ( 1.00%) -- 15x-30x
    drained     100  ( 1.00%) -- lose bet + all DeFi holdings (treated as 0x)
    ascended      1  ( 0.01%) -- 50x-100x

  EV comment from source: ~0.72x entry (house edge ~28%).
--]]

-- Outcome table: {name, weight, lo_mult, hi_mult, description}
local OUTCOMES = {
    {name="rugged",     weight=8400, lo=0.0,   hi=0.0,   desc="lose entire bet"},
    {name="drained",    weight=100,  lo=0.0,   hi=0.0,   desc="lose bet AND all DeFi holdings are wiped"},
    {name="break_even", weight=949,  lo=0.8,   hi=1.5,   desc="0.8x-1.5x return"},
    {name="moon",       weight=450,  lo=5.0,   hi=12.0,  desc="5x-12x multiplier"},
    {name="legendary",  weight=100,  lo=15.0,  hi=30.0,  desc="15x-30x multiplier"},
    {name="ascended",   weight=1,    lo=50.0,  hi=100.0, desc="50x-100x multiplier"},
}
local WEIGHT_TOTAL = 10000

tool_api.register({
    name     = "calc.ape_ev",
    summary  = "APE gambling expected value calculator. Given a bet size, returns the exact probability and payout breakdown for every outcome, expected return, house edge %, and net EV in USD. Use when players ask if APE is worth it or want to understand the odds.",
    risk     = "read",
    category = "economy",
    params   = {
        {
            name        = "bet_amount",
            type        = "float",
            required    = true,
            description = "The APE entry cost in DSD. Scales with job tier -- base is $50 at Homeless.",
            min         = 0.01,
        },
        {
            name        = "rug_king_bonus_pct",
            type        = "float",
            required    = false,
            default     = 0,
            description = "King of Rugs payout bonus percentage (0 unless the player holds the rug king role). Multiplies all non-zero payouts.",
            min         = 0,
            max         = 100,
        },
    },
    handler = function(args)
        local bet   = tonumber(args.bet_amount)
        local bonus = (tonumber(args.rug_king_bonus_pct) or 0) / 100.0

        if not bet or bet <= 0 then
            return tool_api.fail("bet_amount must be > 0")
        end

        local bonus_mult = 1.0 + bonus

        local ev_sum     = 0.0
        local outcomes   = {}

        for i = 1, #OUTCOMES do
            local o    = OUTCOMES[i]
            local prob = o.weight / WEIGHT_TOTAL

            -- Apply rug king bonus to non-zero payouts only
            local lo = o.lo * (o.lo > 0 and bonus_mult or 1.0)
            local hi = o.hi * (o.hi > 0 and bonus_mult or 1.0)

            local payout_lo = bet * lo
            local payout_hi = bet * hi
            -- Expected payout for this outcome = midpoint of range * probability
            local mid_payout = bet * ((lo + hi) / 2.0)
            ev_sum = ev_sum + (prob * mid_payout)

            outcomes[i] = {
                outcome    = o.name,
                prob_pct   = o.weight / 100.0,  -- e.g. 84.00
                payout_lo  = payout_lo,
                payout_hi  = payout_hi,
                desc       = o.desc,
            }
        end

        -- EV metrics
        local ev_return  = ev_sum            -- expected gross return
        local ev_profit  = ev_sum - bet      -- net expected gain (negative = house edge)
        local ev_ratio   = ev_sum / bet      -- return-on-bet, e.g. 0.72
        local house_edge = (1.0 - ev_ratio) * 100.0

        -- Break-even win rate: how often moon+ must hit to cover losses
        local moon_prob = 0.0
        for i = 1, #OUTCOMES do
            local o = OUTCOMES[i]
            if o.lo > 1.0 then
                moon_prob = moon_prob + (o.weight / WEIGHT_TOTAL)
            end
        end

        tool_api.log(string.format(
            "calc.ape_ev: bet=%.2f, ev_return=%.2f (%.1f%%), house_edge=%.1f%%",
            bet, ev_return, ev_ratio * 100.0, house_edge
        ))

        return tool_api.ok({
            bet_amount       = bet,
            expected_return  = ev_return,
            expected_profit  = ev_profit,
            ev_ratio         = ev_ratio,
            house_edge_pct   = house_edge,
            moon_plus_prob   = moon_prob * 100.0,
            rug_king_active  = bonus > 0,
            rug_king_bonus   = bonus * 100.0,
            outcomes         = outcomes,
        })
    end,
})
