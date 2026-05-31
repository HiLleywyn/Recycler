"""sage_config.py  -  Question banks + game constants for the Sage Network.

The three Sage games share a single config surface:

* ``PATTERNS`` - classical chart patterns (head & shoulders, cup & handle,
  double top, etc.) for ,pattern. Each entry carries the data points used
  by services.sage_render to draw the chart and the multi-choice distractors.
* ``INDICATORS`` - indicator readings for ,gauge. Each entry has a series of
  human-readable rows (RSI = 78, MACD bear cross, etc.) and the correct
  bear/neutral/bull label.
* ``TOKENOMICS`` - synthetic tokenomics cards for ,tknom. Stats include
  supply / mint / burn / lock / founder share; the player picks one of
  Inflationary / Deflationary / Stable / Rug Risk.

Per CLAUDE.md the data lives in this module so swapping question banks
or extending difficulty is a config-only change.
"""
from __future__ import annotations

from typing import Final


# ============================================================================
# Game IDs
# ============================================================================
GAME_PATTERN: Final[str] = "pattern"
GAME_GAUGE:   Final[str] = "gauge"
GAME_TKNOM:   Final[str] = "tknom"
GAME_CYCLE:   Final[str] = "cycle"
GAMES: Final[tuple[str, ...]] = (GAME_PATTERN, GAME_GAUGE, GAME_TKNOM, GAME_CYCLE)

GAME_TITLES: Final[dict[str, str]] = {
    GAME_PATTERN: "Pattern Lab",
    GAME_GAUGE:   "Indicator Gauge",
    GAME_TKNOM:   "Tokenomics Card",
    GAME_CYCLE:   "Cycle Phase",
}
GAME_EMOJIS: Final[dict[str, str]] = {
    GAME_PATTERN: "\U0001F4C8",   # chart up
    GAME_GAUGE:   "\U0001F4CA",   # bar chart
    GAME_TKNOM:   "\U0001F9EE",   # abacus
    GAME_CYCLE:   "\U0001F300",   # cyclone
}


# ============================================================================
# Pattern bank
# ============================================================================
# Each entry:
#   key:             stable id
#   name:            human-readable pattern name (the correct answer text)
#   shape:           drawing primitive recognised by services.sage_render
#   bias:            "bull" | "bear" | "neutral" (used for distractor cross-pick)
#   explanation:     post-round educational blurb shown after a guess
#   distractors:     list of other pattern keys that are visually plausible
PATTERNS: Final[list[dict]] = [
    {
        "key":  "head_and_shoulders",
        "name": "Head and Shoulders",
        "shape": "head_and_shoulders",
        "bias":  "bear",
        "explanation": (
            "Three peaks where the middle is the highest and the outer two "
            "are roughly equal. Forms after an uptrend and typically signals "
            "a reversal lower once the neckline (the trough between the "
            "shoulders) breaks."
        ),
        "distractors": ["inverse_head_and_shoulders", "triple_top", "double_top"],
    },
    {
        "key":  "inverse_head_and_shoulders",
        "name": "Inverse Head and Shoulders",
        "shape": "inverse_head_and_shoulders",
        "bias":  "bull",
        "explanation": (
            "Mirror image of head and shoulders that forms after a downtrend. "
            "Three troughs with the middle lowest; a break above the neckline "
            "is the classic reversal signal higher."
        ),
        "distractors": ["head_and_shoulders", "triple_bottom", "double_bottom"],
    },
    {
        "key":  "double_top",
        "name": "Double Top",
        "shape": "double_top",
        "bias":  "bear",
        "explanation": (
            "Two peaks at roughly the same level separated by a trough. "
            "A close below the trough line is the confirmation -- the second "
            "attempt at the highs failed, sellers take over."
        ),
        "distractors": ["double_bottom", "head_and_shoulders", "triple_top"],
    },
    {
        "key":  "double_bottom",
        "name": "Double Bottom",
        "shape": "double_bottom",
        "bias":  "bull",
        "explanation": (
            "Two troughs at roughly the same level separated by a peak. "
            "Sellers exhausted on the second test of the lows; a close above "
            "the peak confirms the reversal higher."
        ),
        "distractors": ["double_top", "inverse_head_and_shoulders", "triple_bottom"],
    },
    {
        "key":  "ascending_triangle",
        "name": "Ascending Triangle",
        "shape": "ascending_triangle",
        "bias":  "bull",
        "explanation": (
            "Flat resistance overhead with a rising trendline of higher lows. "
            "Buyers keep stepping in earlier each test -- a breakout above the "
            "flat line typically resolves higher."
        ),
        "distractors": ["descending_triangle", "symmetrical_triangle", "rising_wedge"],
    },
    {
        "key":  "descending_triangle",
        "name": "Descending Triangle",
        "shape": "descending_triangle",
        "bias":  "bear",
        "explanation": (
            "Flat support underneath with a falling trendline of lower highs. "
            "Sellers willing to take less each rally -- a breakdown through "
            "the flat line typically resolves lower."
        ),
        "distractors": ["ascending_triangle", "symmetrical_triangle", "falling_wedge"],
    },
    {
        "key":  "symmetrical_triangle",
        "name": "Symmetrical Triangle",
        "shape": "symmetrical_triangle",
        "bias":  "neutral",
        "explanation": (
            "Converging lower highs and higher lows. A coiling consolidation "
            "that resolves in the direction of the prevailing trend; the "
            "pattern itself does not predict direction, only continuation."
        ),
        "distractors": ["ascending_triangle", "descending_triangle", "pennant"],
    },
    {
        "key":  "cup_and_handle",
        "name": "Cup and Handle",
        "shape": "cup_and_handle",
        "bias":  "bull",
        "explanation": (
            "U-shaped basing 'cup' followed by a short, shallow pullback "
            "(the 'handle'). A break above the cup's rim, on volume, is "
            "the canonical bullish breakout."
        ),
        "distractors": ["double_bottom", "rounding_bottom", "inverse_head_and_shoulders"],
    },
    {
        "key":  "rounding_bottom",
        "name": "Rounding Bottom",
        "shape": "rounding_bottom",
        "bias":  "bull",
        "explanation": (
            "Long, smooth U-shape with no distinct handle. Implies a gradual "
            "shift from seller control to buyer control. Slower to resolve "
            "than a cup and handle but the same underlying structure."
        ),
        "distractors": ["cup_and_handle", "double_bottom", "rounding_top"],
    },
    {
        "key":  "rising_wedge",
        "name": "Rising Wedge",
        "shape": "rising_wedge",
        "bias":  "bear",
        "explanation": (
            "Both trendlines slope up but the lower line is steeper -- price "
            "is climbing but momentum is narrowing. Usually resolves to the "
            "downside as the buyers run out of fuel."
        ),
        "distractors": ["falling_wedge", "ascending_triangle", "bull_flag"],
    },
    {
        "key":  "falling_wedge",
        "name": "Falling Wedge",
        "shape": "falling_wedge",
        "bias":  "bull",
        "explanation": (
            "Both trendlines slope down but the upper line is steeper -- price "
            "is dropping but selling pressure is fading. Usually resolves to "
            "the upside as sellers exhaust."
        ),
        "distractors": ["rising_wedge", "descending_triangle", "bear_flag"],
    },
    {
        "key":  "bull_flag",
        "name": "Bull Flag",
        "shape": "bull_flag",
        "bias":  "bull",
        "explanation": (
            "Sharp impulse leg up (the pole) followed by a tight, downward-"
            "sloping consolidation (the flag). Continuation pattern: the "
            "break out of the flag typically projects the pole's height."
        ),
        "distractors": ["bear_flag", "pennant", "rising_wedge"],
    },
    {
        "key":  "bear_flag",
        "name": "Bear Flag",
        "shape": "bear_flag",
        "bias":  "bear",
        "explanation": (
            "Sharp impulse leg down (the pole) followed by a tight, upward-"
            "sloping consolidation (the flag). Continuation pattern: a "
            "break below the flag projects another leg lower."
        ),
        "distractors": ["bull_flag", "pennant", "falling_wedge"],
    },
    {
        "key":  "pennant",
        "name": "Pennant",
        "shape": "pennant",
        "bias":  "neutral",
        "explanation": (
            "Tight, symmetrical consolidation after a sharp move (either "
            "direction). Like a flag but with converging trendlines. Usually "
            "resolves in the direction of the preceding impulse."
        ),
        "distractors": ["symmetrical_triangle", "bull_flag", "bear_flag"],
    },
    {
        "key":  "triple_top",
        "name": "Triple Top",
        "shape": "triple_top",
        "bias":  "bear",
        "explanation": (
            "Three peaks at roughly the same level. Stronger resistance "
            "signal than a double top because price failed three times. "
            "Breakdown below the swing lows confirms the reversal."
        ),
        "distractors": ["double_top", "head_and_shoulders", "rounding_top"],
    },
    {
        "key":  "triple_bottom",
        "name": "Triple Bottom",
        "shape": "triple_bottom",
        "bias":  "bull",
        "explanation": (
            "Three troughs at roughly the same level. Strong support; "
            "buyers defended the level three times. A break above the swing "
            "highs confirms the reversal higher."
        ),
        "distractors": ["double_bottom", "inverse_head_and_shoulders", "rounding_bottom"],
    },
    {
        "key":  "rounding_top",
        "name": "Rounding Top",
        "shape": "rounding_top",
        "bias":  "bear",
        "explanation": (
            "Long, smooth dome shape with no distinct handle. Slow, gradual "
            "topping process -- buyer control fades into seller control. "
            "Mirror image of the rounding bottom."
        ),
        "distractors": ["rounding_bottom", "double_top", "triple_top", "bump_and_run_top"],
    },
    # ── Expansion bank (added with cycle + compound rollout) ───────────────
    {
        "key":  "broadening_top",
        "name": "Broadening Top",
        "shape": "broadening_top",
        "bias":  "bear",
        "explanation": (
            "Megaphone formation: a series of higher highs paired with lower "
            "lows, so price range is expanding instead of coiling. Late-cycle "
            "exhaustion signature: too many traders are aggressive in both "
            "directions and the noise eventually resolves down."
        ),
        "distractors": ["broadening_bottom", "diamond_top", "three_drives_top"],
    },
    {
        "key":  "broadening_bottom",
        "name": "Broadening Bottom",
        "shape": "broadening_bottom",
        "bias":  "bull",
        "explanation": (
            "Inverse megaphone after a downtrend: each swing is wider but "
            "the lows fail to break the prior low decisively. Volatility "
            "expansion with seller exhaustion underneath usually resolves up."
        ),
        "distractors": ["broadening_top", "diamond_bottom", "triple_bottom"],
    },
    {
        "key":  "diamond_top",
        "name": "Diamond Top",
        "shape": "diamond_top",
        "bias":  "bear",
        "explanation": (
            "Rare reversal: price first broadens (megaphone), then contracts "
            "(triangle), so the chart traces a diamond. Marks indecision at "
            "the highs followed by failed continuation -- classic distribution."
        ),
        "distractors": ["broadening_top", "head_and_shoulders", "diamond_bottom"],
    },
    {
        "key":  "diamond_bottom",
        "name": "Diamond Bottom",
        "shape": "diamond_bottom",
        "bias":  "bull",
        "explanation": (
            "Broadening into contracting structure at a low. Capitulation "
            "first, then base-building -- accumulation hiding inside the "
            "noise. Mirror image of the diamond top; resolves up on breakout."
        ),
        "distractors": ["broadening_bottom", "inverse_head_and_shoulders", "diamond_top"],
    },
    {
        "key":  "island_reversal_top",
        "name": "Island Reversal (Top)",
        "shape": "island_reversal_top",
        "bias":  "bear",
        "explanation": (
            "Gap up that prints the high, then a gap down that leaves the "
            "high session stranded as an 'island' above empty price. The "
            "two gaps imply trapped longs with nowhere to exit on the way down."
        ),
        "distractors": ["double_top", "rounding_top", "head_and_shoulders"],
    },
    {
        "key":  "island_reversal_bottom",
        "name": "Island Reversal (Bottom)",
        "shape": "island_reversal_bottom",
        "bias":  "bull",
        "explanation": (
            "Gap down to the low, then a gap up that strands the bottom "
            "candles. The two gaps mean capitulation sellers got no fill on "
            "the bounce and shorts are now stuck under price."
        ),
        "distractors": ["double_bottom", "rounding_bottom", "inverse_head_and_shoulders"],
    },
    {
        "key":  "flag_pole",
        "name": "Flag Pole",
        "shape": "flag_pole",
        "bias":  "bull",
        "explanation": (
            "Near-vertical impulse leg with no real consolidation -- pure "
            "momentum drive. Often the 'pole' before a flag forms; if no "
            "flag follows, the next leg usually arrives directly after a "
            "shallow pause."
        ),
        "distractors": ["bull_flag", "rising_wedge", "ascending_triangle"],
    },
    {
        "key":  "bart_pattern",
        "name": "Bart Pattern",
        "shape": "bart_pattern",
        "bias":  "neutral",
        "explanation": (
            "Vertical pump, flat consolidation up high, vertical dump back "
            "to start -- the silhouette of Bart Simpson's head. Almost "
            "always a single-actor liquidity grab: no follow-through either "
            "direction once the dump completes."
        ),
        "distractors": ["double_top", "flag_pole", "island_reversal_top"],
    },
    {
        "key":  "three_drives_top",
        "name": "Three Drives (Top)",
        "shape": "three_drives_top",
        "bias":  "bear",
        "explanation": (
            "Three successive higher highs, each on weaker momentum. "
            "Harmonic-style topping pattern: buyers throw three sequential "
            "attempts at the highs and each closes shorter than the last. "
            "Reversal on the third drive's fail."
        ),
        "distractors": ["triple_top", "rising_wedge", "head_and_shoulders"],
    },
    {
        "key":  "bump_and_run_top",
        "name": "Bump and Run (Top)",
        "shape": "bump_and_run_top",
        "bias":  "bear",
        "explanation": (
            "Steady uptrend ('lead-in'), then a sharp parabolic acceleration "
            "('bump'), then a collapse back through the lead-in trendline "
            "('run'). The parabolic phase always overextends and the run "
            "phase reclaims it violently."
        ),
        "distractors": ["rising_wedge", "rounding_top", "head_and_shoulders"],
    },
]

PATTERN_BY_KEY: Final[dict[str, dict]] = {p["key"]: p for p in PATTERNS}

# Difficulty: how many distractor options to choose between (always 4 total).
# Rounds 1-3: easier (cross-bias picks heavily filtered); 4+: full mix.
PATTERN_OPTION_COUNT: Final[int] = 4


# ============================================================================
# Indicator bank (Gauge game)
# ============================================================================
# Each entry:
#   key:           stable id
#   title:         indicator-card header
#   rows:          list of (label, value, hint) tuples to display
#   answer:        "bear" | "neutral" | "bull"
#   explanation:   educational blurb shown after the guess
INDICATORS: Final[list[dict]] = [
    {
        "key":   "rsi_oversold",
        "title": "Daily RSI · 14",
        "rows":  [
            ("RSI(14)",       "26.4", "oversold"),
            ("Trend",         "Downtrend over 30d", ""),
            ("Recent close",  "Higher low forming", ""),
        ],
        "answer": "bull",
        "explanation": (
            "RSI below 30 is the classic oversold zone -- when it pairs with "
            "a higher low on the price chart, it's a mean-reversion signal. "
            "Not a top-tick buy, but the snap-back bias is to the upside."
        ),
    },
    {
        "key":   "rsi_overbought",
        "title": "Daily RSI · 14",
        "rows":  [
            ("RSI(14)",       "82.1", "overbought"),
            ("Trend",         "Strong uptrend 6w", ""),
            ("Divergence",    "Lower-high vs price HH", ""),
        ],
        "answer": "bear",
        "explanation": (
            "RSI above 70 is the canonical overbought zone, and the bearish "
            "divergence (price made a higher high but RSI made a lower high) "
            "is the cherry on top -- momentum is fading even as price extends."
        ),
    },
    {
        "key":   "rsi_neutral",
        "title": "Daily RSI · 14",
        "rows":  [
            ("RSI(14)",       "49.8", "midline"),
            ("Trend",         "Choppy 3w", ""),
            ("ATR",            "Below 30d avg", ""),
        ],
        "answer": "neutral",
        "explanation": (
            "RSI hovering near 50 with no divergence and dropping volatility "
            "is textbook chop -- the market hasn't picked a side. Wait for a "
            "break of the range; the indicator gives you no edge here."
        ),
    },
    {
        "key":   "macd_bull_cross",
        "title": "Daily MACD · 12/26/9",
        "rows":  [
            ("MACD line",     "Crossed signal up", ""),
            ("Histogram",     "Turning positive", ""),
            ("Zero line",     "Below, but rising", ""),
        ],
        "answer": "bull",
        "explanation": (
            "Bullish MACD cross below the zero line is an early-trend signal: "
            "short-term momentum has flipped up before price has fully "
            "recovered. Strong follow-through if price holds the cross."
        ),
    },
    {
        "key":   "macd_bear_cross",
        "title": "Daily MACD · 12/26/9",
        "rows":  [
            ("MACD line",     "Crossed signal down", ""),
            ("Histogram",     "Flipping negative", ""),
            ("Zero line",     "Above, but falling", ""),
        ],
        "answer": "bear",
        "explanation": (
            "Bearish MACD cross above the zero line is the early-stage trend "
            "rollover -- short-term momentum has rolled before price has "
            "broken. Confirmation comes when the histogram pushes deeper red."
        ),
    },
    {
        "key":   "bb_squeeze",
        "title": "Bollinger Bands · 20/2",
        "rows":  [
            ("Band width",    "12m low",  "squeeze"),
            ("Price",         "Mid-band", ""),
            ("Volume",         "Compressing", ""),
        ],
        "answer": "neutral",
        "explanation": (
            "Tight Bollinger Band squeeze signals a volatility expansion is "
            "coming, but the bands don't tell you which direction. Wait for "
            "the band-break on volume -- before that, you're guessing."
        ),
    },
    {
        "key":   "bb_breakout_up",
        "title": "Bollinger Bands · 20/2",
        "rows":  [
            ("Price",         "Closed above upper band", ""),
            ("Band width",     "Expanding", ""),
            ("Volume",          "2x 20d avg", ""),
        ],
        "answer": "bull",
        "explanation": (
            "Strong close above the upper Bollinger Band on expanding volume "
            "is a momentum-breakout signal -- typically continuation, NOT a "
            "fade. Tag is not a top; rejection wick would be."
        ),
    },
    {
        "key":   "volume_dry_up",
        "title": "Volume Profile",
        "rows":  [
            ("Down-day volume", "Falling 4w", ""),
            ("Up-day volume",   "Steady", ""),
            ("OBV",             "Flat-to-rising", ""),
        ],
        "answer": "bull",
        "explanation": (
            "Volume drying up on the down-days while up-day volume holds is "
            "selling exhaustion -- distribution has run its course. OBV "
            "staying firm confirms accumulation under the surface."
        ),
    },
    {
        "key":   "volume_climax",
        "title": "Volume Profile",
        "rows":  [
            ("Up-day volume",   "5x 20d avg", "climax"),
            ("Price",            "Parabolic",  ""),
            ("RSI(14)",          "88.0",       "overbought"),
        ],
        "answer": "bear",
        "explanation": (
            "Volume climax on a parabolic blow-off with deeply overbought RSI "
            "is the textbook exhaustion signature -- buyers all-in at the "
            "wrong moment. Reversal risk is elevated, not bullish continuation."
        ),
    },
    {
        "key":   "ma_golden_cross",
        "title": "Moving Averages",
        "rows":  [
            ("50d MA",  "Crossed above 200d", "golden cross"),
            ("Price",   "Above both MAs", ""),
            ("Slope",   "Both rising", ""),
        ],
        "answer": "bull",
        "explanation": (
            "Golden cross -- 50-day moving average crossing above the 200-day -- "
            "is a long-horizon trend confirmation. Lagging, but the slope of "
            "both MAs rising is what makes this a real signal vs noise."
        ),
    },
    {
        "key":   "ma_death_cross",
        "title": "Moving Averages",
        "rows":  [
            ("50d MA",  "Crossed below 200d", "death cross"),
            ("Price",   "Below both MAs", ""),
            ("Slope",   "Both falling", ""),
        ],
        "answer": "bear",
        "explanation": (
            "Death cross -- 50-day moving average crossing below the 200-day -- "
            "with both slopes pointing down is bearish trend confirmation. "
            "Often a great fade after the initial momentum, but the bias is "
            "lower until price reclaims the 200d."
        ),
    },
    {
        "key":   "funding_extreme",
        "title": "Perp Funding Rate",
        "rows":  [
            ("Funding",   "+0.18% 8h",   "max longs"),
            ("OI",        "ATH",          ""),
            ("Long/short", "78/22",       ""),
        ],
        "answer": "bear",
        "explanation": (
            "Extreme positive funding with open interest at all-time-high and "
            "a lopsided long/short ratio is the canonical squeeze setup -- "
            "everyone is long, so the path of least pain is a flush lower to "
            "liquidate the late chasers."
        ),
    },
    {
        "key":   "funding_negative",
        "title": "Perp Funding Rate",
        "rows":  [
            ("Funding",    "-0.12% 8h",   "max shorts"),
            ("OI",         "Climbing",     ""),
            ("Long/short",  "28/72",       ""),
        ],
        "answer": "bull",
        "explanation": (
            "Negative funding with OI rising and a lopsided short ratio is "
            "the canonical short-squeeze setup -- shorts are paying longs "
            "to hold, so any pump cascades into stop-runs higher."
        ),
    },
    {
        "key":   "stoch_neutral_range",
        "title": "Stochastic · 14/3/3",
        "rows":  [
            ("%K",   "47.0", ""),
            ("%D",   "51.0", ""),
            ("Cross", "Inside range", ""),
        ],
        "answer": "neutral",
        "explanation": (
            "Stochastic mid-range with %K and %D coiled around 50 gives no "
            "edge -- not oversold, not overbought, no cross. Wait for a hook "
            "out of one of the extremes before betting on direction."
        ),
    },
    {
        "key":   "obv_divergence_bull",
        "title": "On-Balance Volume",
        "rows":  [
            ("Price",  "Lower low",       ""),
            ("OBV",    "Higher low",      "bullish divergence"),
            ("Trend",  "30d downtrend",   ""),
        ],
        "answer": "bull",
        "explanation": (
            "Bullish OBV divergence -- price keeps printing lower lows but "
            "the cumulative volume curve is making higher lows -- means net "
            "buying is happening under the surface. Smart money loads on weakness."
        ),
    },
    {
        "key":   "obv_divergence_bear",
        "title": "On-Balance Volume",
        "rows":  [
            ("Price",  "Higher high",      ""),
            ("OBV",    "Lower high",       "bearish divergence"),
            ("Trend",  "Late uptrend",     ""),
        ],
        "answer": "bear",
        "explanation": (
            "Bearish OBV divergence -- price keeps printing higher highs but "
            "OBV is making lower highs -- means net selling is happening into "
            "the rally. Smart money is distributing while retail chases."
        ),
    },
    {
        "key":   "atr_dropping",
        "title": "Average True Range · 14",
        "rows":  [
            ("ATR(14)",  "60d low",     ""),
            ("Range",     "Tightening",  ""),
            ("Trend",      "Sideways",    ""),
        ],
        "answer": "neutral",
        "explanation": (
            "ATR at a 60-day low means realised volatility has compressed -- "
            "but volatility expansion comes in both directions. Without a "
            "directional confirm, the bias is sideways consolidation, not "
            "a setup either way."
        ),
    },
    {
        "key":   "vwap_reclaim",
        "title": "Daily VWAP",
        "rows":  [
            ("Price",    "Reclaimed VWAP", ""),
            ("Volume",    "Above 20d avg",  ""),
            ("Open",      "Below VWAP",     ""),
        ],
        "answer": "bull",
        "explanation": (
            "Reclaiming daily VWAP from below on above-average volume is a "
            "session-control flip -- average buyer is now in profit, sellers "
            "from the lower zone are likely to cap losses. Short-term bull."
        ),
    },
    # ── Expansion bank (added with cycle + compound rollout) ───────────────
    {
        "key":   "cvd_divergence_bull",
        "title": "Spot CVD vs Price",
        "rows":  [
            ("Price",   "Lower low",       ""),
            ("Spot CVD", "Higher low",     "bullish divergence"),
            ("Perp CVD", "Lower low",      ""),
        ],
        "answer": "bull",
        "explanation": (
            "Spot cumulative volume delta is making higher lows while price "
            "and perp CVD print lower lows -- real-money buyers are "
            "accumulating into derivative-driven weakness. The flush is "
            "fed by leverage; the bid underneath is spot."
        ),
    },
    {
        "key":   "cvd_divergence_bear",
        "title": "Spot CVD vs Price",
        "rows":  [
            ("Price",    "Higher high",      ""),
            ("Spot CVD",  "Lower high",       "bearish divergence"),
            ("Perp CVD",  "Higher high",      ""),
        ],
        "answer": "bear",
        "explanation": (
            "Price is grinding to new highs but spot CVD is rolling over "
            "into lower highs -- the rally is being carried by perp longs, "
            "not real spot demand. Distribution above; the bid is hollow."
        ),
    },
    {
        "key":   "oi_unwind",
        "title": "Perp Open Interest",
        "rows":  [
            ("OI",        "Crashing -25% in 1h", "squeeze unwind"),
            ("Funding",   "Flipped negative",     ""),
            ("Price",     "Holding higher",       ""),
        ],
        "answer": "bull",
        "explanation": (
            "Open interest collapsing on a price hold means short positions "
            "got liquidated en masse and the fuel for further downside is "
            "gone. Funding turning negative on the unwind also flips "
            "shorts into the new exit liquidity for longs."
        ),
    },
    {
        "key":   "fear_greed_extreme_fear",
        "title": "Crypto Fear & Greed Index",
        "rows":  [
            ("Index",       "8 / 100",        "extreme fear"),
            ("30d trend",   "Sliding lower",  ""),
            ("Headlines",   "Apocalyptic",    ""),
        ],
        "answer": "bull",
        "explanation": (
            "Sentiment indices are contrarian by design: extreme-fear "
            "prints below 10 historically mark generational accumulation "
            "zones. When everyone has already sold, there is nobody left "
            "to feed the next leg lower."
        ),
    },
    {
        "key":   "fear_greed_extreme_greed",
        "title": "Crypto Fear & Greed Index",
        "rows":  [
            ("Index",       "94 / 100",       "extreme greed"),
            ("Search vol",  "ATH",            "retail FOMO"),
            ("Funding",     "Pinned positive", ""),
        ],
        "answer": "bear",
        "explanation": (
            "Extreme greed with retail FOMO at an all-time high and funding "
            "pinned positive is the canonical local top signature. When "
            "everyone has already bought, there is nobody left to bid the "
            "next leg higher."
        ),
    },
    {
        "key":   "ichimoku_bullish_kumo",
        "title": "Ichimoku Cloud · Daily",
        "rows":  [
            ("Price vs Kumo", "Closed above cloud", "bullish breakout"),
            ("Tenkan/Kijun",  "Bullish cross",      ""),
            ("Future Kumo",    "Green",              ""),
        ],
        "answer": "bull",
        "explanation": (
            "All three Ichimoku confirmations line up: price closed above a "
            "thickening cloud, tenkan crossed kijun upward, and the forward "
            "cloud is green. Triple-confirmed trend continuation higher."
        ),
    },
    {
        "key":   "ichimoku_bearish_kumo",
        "title": "Ichimoku Cloud · Daily",
        "rows":  [
            ("Price vs Kumo", "Closed below cloud", "bearish breakdown"),
            ("Tenkan/Kijun",  "Bearish cross",      ""),
            ("Future Kumo",    "Red",                ""),
        ],
        "answer": "bear",
        "explanation": (
            "Bearish triple-confirmation: price lost the cloud on a daily "
            "close, tenkan crossed kijun down, and the forward Kumo flipped "
            "red. Trend has rolled over in the Ichimoku framework."
        ),
    },
    {
        "key":   "adx_no_trend",
        "title": "ADX · 14",
        "rows":  [
            ("ADX",       "13.2",       "no trend"),
            ("+DI / -DI", "21 / 22",    "crossing"),
            ("Range",     "6w sideways", ""),
        ],
        "answer": "neutral",
        "explanation": (
            "ADX below 20 means there is no meaningful trend in either "
            "direction, and +DI / -DI crossing inside a range is noise. "
            "Trade the range edges; do not bet on a breakout direction "
            "until ADX rises above 25."
        ),
    },
    {
        "key":   "dxy_bearish",
        "title": "Cross-Asset · DXY",
        "rows":  [
            ("DXY",         "Lost 200d MA",   "trend rollover"),
            ("US 2y yield", "Falling fast",   ""),
            ("MTA corr",     "-0.71 (30d)",   "inverse"),
        ],
        "answer": "bull",
        "explanation": (
            "Crypto trades as the inverse of the dollar index over rolling "
            "30-day windows; DXY losing its 200d MA with falling yields "
            "loosens financial conditions globally. Inverse correlation "
            "makes this a tailwind for MTA and the high-beta alt complex."
        ),
    },
    {
        "key":   "eth_btc_breakout",
        "title": "Cross-Pair · ARC/MTA",
        "rows":  [
            ("ARC/MTA",       "Broke 90d resistance", "regime shift"),
            ("MTA dominance",  "Falling",             ""),
            ("Alt season idx", "62 / 100",            "alt season"),
        ],
        "answer": "bull",
        "explanation": (
            "ARC/MTA breaking out with MTA dominance falling is the "
            "classic alt-season trigger -- capital is rotating down the "
            "risk curve. Historically the start of a high-beta phase "
            "for the broader alt complex."
        ),
    },
]

INDICATOR_BY_KEY: Final[dict[str, dict]] = {i["key"]: i for i in INDICATORS}

# Always 3 options for the gauge game (bear / neutral / bull).
GAUGE_OPTIONS: Final[tuple[str, ...]] = ("bear", "neutral", "bull")
GAUGE_OPTION_LABELS: Final[dict[str, str]] = {
    "bear":    "Bearish",
    "neutral": "Neutral",
    "bull":    "Bullish",
}
GAUGE_OPTION_EMOJI: Final[dict[str, str]] = {
    "bear":    "\U0001F53B",   # down red triangle
    "neutral": "\U0001F7E1",   # yellow circle
    "bull":    "\U0001F53A",   # up red triangle (intentional contrast)
}


# ============================================================================
# Tokenomics bank
# ============================================================================
# Each entry:
#   key:           stable id
#   title:         token-card header (synthetic ticker)
#   stats:         dict of stat -> display string
#   answer:        "inflate" | "deflate" | "stable" | "rug"
#   explanation:   educational blurb shown after the guess
TOKENOMICS: Final[list[dict]] = [
    {
        "key":   "high_mint_low_burn",
        "title": "MOONX · Genesis Card",
        "stats": {
            "Supply":         "100M (uncapped)",
            "Daily mint":     "+0.30%",
            "Burn rate":      "0.00%",
            "Locked LP":      "60% / 12 months",
            "Founder share":  "10% / 6mo cliff",
        },
        "answer": "inflate",
        "explanation": (
            "Uncapped supply with 0.30% daily mint and no burn -- that's a "
            "~3x supply expansion in a year. Even with a healthy 60% locked "
            "LP, the inflation pressure dominates. Price holds only if "
            "demand grows faster than 30% per quarter."
        ),
    },
    {
        "key":   "high_burn_capped",
        "title": "ASHIB · Genesis Card",
        "stats": {
            "Supply":         "1B (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "2.00% per tx",
            "Locked LP":      "75% / 24 months",
            "Founder share":  "5% / 12mo cliff",
        },
        "answer": "deflate",
        "explanation": (
            "Hard-capped supply, no mint, 2% burn on every transfer, and a "
            "long LP lock = aggressive deflation. The more it trades, the "
            "scarcer it gets. Risk shifts from supply to demand: if volume "
            "dries up, the deflation thesis stalls."
        ),
    },
    {
        "key":   "balanced_stable",
        "title": "PARI · Genesis Card",
        "stats": {
            "Supply":         "21M (hard cap)",
            "Daily mint":     "0.05%",
            "Burn rate":      "0.05% per tx",
            "Locked LP":      "80% / 36 months",
            "Founder share":  "7% / 24mo cliff",
        },
        "answer": "stable",
        "explanation": (
            "Hard cap with mint and burn rates roughly equal, very long LP "
            "lock, modest founder share on a long cliff. Net supply is "
            "near-flat -- this is what 'sound money' tokenomics looks like. "
            "Price moves on demand, not supply shocks."
        ),
    },
    {
        "key":   "rug_signature",
        "title": "ZOOMR · Genesis Card",
        "stats": {
            "Supply":         "1B (mintable by owner)",
            "Daily mint":     "owner-discretion",
            "Burn rate":      "0.00%",
            "Locked LP":      "0% (none)",
            "Founder share":  "65% / no cliff",
        },
        "answer": "rug",
        "explanation": (
            "Owner-discretion minting, zero LP locked, 65% founder share with "
            "no cliff. Every red flag in one card -- the founder can dump "
            "their bag AND mint unlimited supply at any time. This isn't "
            "investing, it's volunteering."
        ),
    },
    {
        "key":   "low_mint_long_lock",
        "title": "STDY · Genesis Card",
        "stats": {
            "Supply":         "50M (hard cap)",
            "Daily mint":     "0.02%",
            "Burn rate":      "0.20% per tx",
            "Locked LP":      "90% / 48 months",
            "Founder share":  "4% / 36mo cliff",
        },
        "answer": "deflate",
        "explanation": (
            "Mint at 0.02% is dwarfed by 0.20% per-transaction burn. As long "
            "as the token trades, the burn outpaces the mint -- net "
            "deflationary. Very long LP lock and small founder share "
            "minimise dump risk; the bottleneck is demand sustainability."
        ),
    },
    {
        "key":   "inflate_no_cliff",
        "title": "ZAPI · Genesis Card",
        "stats": {
            "Supply":         "500M (soft cap)",
            "Daily mint":     "+0.12%",
            "Burn rate":      "0.05% per tx",
            "Locked LP":      "40% / 6 months",
            "Founder share":  "15% / unlocked",
        },
        "answer": "inflate",
        "explanation": (
            "Mint at 0.12% daily is more than 2x the burn rate, AND the 15% "
            "founder share is unlocked. Net inflation is the supply story; "
            "the unlocked founder bag is the volatility story. Even healthy "
            "demand will struggle to outrun both at once."
        ),
    },
    {
        "key":   "rug_lp_unlocked",
        "title": "FAST · Genesis Card",
        "stats": {
            "Supply":         "1B (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "0.00%",
            "Locked LP":      "10% / 1 month",
            "Founder share":  "40% / 1mo cliff",
        },
        "answer": "rug",
        "explanation": (
            "Hard cap and no mint look fine on the surface, but only 10% LP "
            "is locked (for just 1 month) and the founder controls 40% of "
            "supply with a 1-month cliff. As soon as the cliff passes, the "
            "exit ramp is wide open and the LP can be pulled. Classic rug "
            "geometry hiding behind a clean supply curve."
        ),
    },
    {
        "key":   "stable_modest",
        "title": "BASE · Genesis Card",
        "stats": {
            "Supply":         "100M (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "0.10% per tx",
            "Locked LP":      "50% / 12 months",
            "Founder share":  "8% / 12mo cliff",
        },
        "answer": "deflate",
        "explanation": (
            "Hard cap with no new mint and a 0.10% per-transaction burn is "
            "deflationary by construction. Half the LP is locked for a year "
            "and founder share is modest with a matching cliff -- the "
            "supply story is clean, the burn rate is the alpha."
        ),
    },
    {
        "key":   "stable_neutral",
        "title": "EVEN · Genesis Card",
        "stats": {
            "Supply":         "10M (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "0.00%",
            "Locked LP":      "70% / 24 months",
            "Founder share":  "10% / 18mo cliff",
        },
        "answer": "stable",
        "explanation": (
            "Hard cap, no mint, no burn = supply is fixed forever. With 70% "
            "of LP locked for two years and a modest founder share on an 18-"
            "month cliff, the only variables are demand and float. Pure "
            "store-of-value tokenomics."
        ),
    },
    {
        "key":   "inflate_emission",
        "title": "FARM · Genesis Card",
        "stats": {
            "Supply":         "Uncapped",
            "Daily mint":     "+0.50%",
            "Burn rate":      "0.10% per tx",
            "Locked LP":      "55% / 18 months",
            "Founder share":  "9% / 12mo cliff",
        },
        "answer": "inflate",
        "explanation": (
            "Uncapped supply with 0.50% daily emissions far outpaces a 0.10% "
            "burn -- net supply expansion is roughly 0.40% daily, ~250% per "
            "year. Tokens like this only hold price when reward demand "
            "(staking, LP farming) absorbs the new supply. Most don't."
        ),
    },
    {
        "key":   "rug_no_lock",
        "title": "FOMO · Genesis Card",
        "stats": {
            "Supply":         "10M (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "1.00% per tx",
            "Locked LP":      "0% (none)",
            "Founder share":  "30% / unlocked",
        },
        "answer": "rug",
        "explanation": (
            "Aggressive burn looks great on paper, but ZERO locked LP means "
            "the deployer can pull liquidity at any second, and 30% of "
            "supply is in unlocked founder hands. Don't be the exit liquidity "
            "for someone else's marketing budget."
        ),
    },
    {
        "key":   "deflate_modest",
        "title": "QUIT · Genesis Card",
        "stats": {
            "Supply":         "21M (hard cap)",
            "Daily mint":     "0.01%",
            "Burn rate":      "0.50% per tx",
            "Locked LP":      "65% / 24 months",
            "Founder share":  "6% / 24mo cliff",
        },
        "answer": "deflate",
        "explanation": (
            "Very small mint vs an aggressive 0.50% per-transaction burn -- "
            "the burn dominates by a factor of 50x. Hard cap caps the worst "
            "case, long LP lock and matching founder cliff are clean. The "
            "supply curve only goes one way: down."
        ),
    },
    # ── Expansion bank (added with cycle + compound rollout) ───────────────
    {
        "key":   "vesting_cliff_imminent",
        "title": "PUMP · Genesis Card",
        "stats": {
            "Supply":         "200M (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "0.05% per tx",
            "Locked LP":      "60% / 12 months",
            "Founder share":  "25% / cliff in 30d",
        },
        "answer": "inflate",
        "explanation": (
            "Headline supply curve looks clean (hard cap, no mint), but a "
            "25% founder allocation cliff-unlocks in 30 days. That single "
            "event quintuples freely-tradeable supply overnight. Holders "
            "front-run the unlock, so the practical curve is inflationary "
            "into the date."
        ),
    },
    {
        "key":   "rebase_stable",
        "title": "ELAS · Genesis Card",
        "stats": {
            "Supply":         "Elastic / rebase",
            "Daily mint":     "Target-pegged",
            "Burn rate":      "Target-pegged",
            "Locked LP":      "70% / 24 months",
            "Founder share":  "8% / 18mo cliff",
        },
        "answer": "stable",
        "explanation": (
            "Rebase mechanics expand and contract supply daily to track a "
            "target price; net supply is roughly flat once the mechanism "
            "stabilises. Clean LP lock + reasonable founder share + cliff "
            "= a stable-by-design supply curve, with peg risk replacing "
            "supply risk."
        ),
    },
    {
        "key":   "buyback_burn_modest",
        "title": "SHRINK · Genesis Card",
        "stats": {
            "Supply":         "500M (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "Revenue buyback + burn",
            "Locked LP":      "70% / 36 months",
            "Founder share":  "9% / 24mo cliff",
        },
        "answer": "deflate",
        "explanation": (
            "Hard cap with no mint, plus a revenue-funded buyback-and-burn "
            "loop. As long as the protocol generates fees, circulating "
            "supply contracts monotonically. Mid-tier LP lock and matched "
            "founder cliff keep dump risk modest."
        ),
    },
    {
        "key":   "staking_apr_unsustainable",
        "title": "YIELD · Genesis Card",
        "stats": {
            "Supply":         "Uncapped",
            "Daily mint":     "+0.85% to stakers",
            "Burn rate":      "0.00%",
            "Locked LP":      "30% / 6 months",
            "Founder share":  "12% / 6mo cliff",
        },
        "answer": "inflate",
        "explanation": (
            "9999%-APR-style staking emissions with no offsetting burn is "
            "pure dilution dressed up as yield -- you mint to pay yourself "
            "back in your own debasing token. Uncapped supply + short LP "
            "lock means the curve only goes one way."
        ),
    },
    {
        "key":   "dao_treasury_locked",
        "title": "GOVR · Genesis Card",
        "stats": {
            "Supply":         "50M (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "0.00%",
            "Locked LP":      "80% / 48 months",
            "Founder share":  "DAO-controlled (vote-gated)",
        },
        "answer": "stable",
        "explanation": (
            "Hard cap, no mint, no burn, with the founder allocation under "
            "DAO control (vote-gated). Supply is fixed and the largest "
            "holder has to coordinate consensus to move tokens. As clean "
            "as governance tokenomics gets."
        ),
    },
    {
        "key":   "airdrop_dumpfest",
        "title": "DROP · Genesis Card",
        "stats": {
            "Supply":         "1B (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "0.00%",
            "Locked LP":      "20% / 3 months",
            "Founder share":  "5%, plus 40% airdropped unlocked",
        },
        "answer": "inflate",
        "explanation": (
            "Hard cap looks reassuring, but a 40% unlocked airdrop is "
            "effectively a 40% supply unlock on day one -- recipients have "
            "no cost basis, so most sell immediately. Functional supply "
            "going to market is inflationary, even with the cap headline."
        ),
    },
    {
        "key":   "multi_year_lock_clean",
        "title": "VAULT · Genesis Card",
        "stats": {
            "Supply":         "30M (hard cap)",
            "Daily mint":     "0.00%",
            "Burn rate":      "0.00%",
            "Locked LP":      "85% / 60 months",
            "Founder share":  "6% / 48mo cliff",
        },
        "answer": "stable",
        "explanation": (
            "Five-year LP lock with most of the supply staked or otherwise "
            "illiquid, hard cap, no mint or burn. Whatever floats trades "
            "against a thin float -- supply curve itself is bedrock flat. "
            "Demand drives price, not unlocks."
        ),
    },
    {
        "key":   "migration_dilute_v2",
        "title": "MIGR2 · Genesis Card",
        "stats": {
            "Supply":         "v2 swap: 1 v1 -> 2 v2",
            "Daily mint":     "Variable (migration claim)",
            "Burn rate":      "0.05% per tx",
            "Locked LP":      "55% / 12 months",
            "Founder share":  "10% / 12mo cliff",
        },
        "answer": "inflate",
        "explanation": (
            "Migration v1 -> v2 at a 1:2 swap ratio mathematically doubles "
            "supply, regardless of burn rate. The 0.05% per-tx burn cannot "
            "catch a one-time 100% supply expansion in any realistic "
            "timeframe -- net curve is inflationary for years."
        ),
    },
]

TOKENOMICS_BY_KEY: Final[dict[str, dict]] = {t["key"]: t for t in TOKENOMICS}

# Always 4 options for tokenomics.
TKNOM_OPTIONS: Final[tuple[str, ...]] = ("inflate", "deflate", "stable", "rug")
TKNOM_OPTION_LABELS: Final[dict[str, str]] = {
    "inflate": "Inflationary",
    "deflate": "Deflationary",
    "stable":  "Stable",
    "rug":     "Rug Risk",
}
TKNOM_OPTION_EMOJI: Final[dict[str, str]] = {
    "inflate": "\U0001F4C8",   # chart up
    "deflate": "\U0001F525",   # fire
    "stable":  "\U00002696",   # balance scale
    "rug":     "\U0001F6A9",   # red flag
}


# ============================================================================
# Compound patterns (Pattern Lab, higher-round difficulty)
# ============================================================================
# A compound is one chart spliced from two single-pattern recipes. The player
# answers TWO multi-choice rounds back-to-back (left half, then right half).
# Both correct = a single round resolution paying COMPOUND_REWARD_MULT x the
# base round reward. Either wrong ends the run.
#
# Each entry:
#   key:           stable id
#   name:          human-readable compound name shown in the explanation
#   stages:        list[dict] of stage definitions, each:
#                    shape    : key in services.sage_render._SHAPE_RECIPES
#                    answer   : pattern key in PATTERN_BY_KEY (the right answer)
#                    x_range  : (x0, x1) in 0..1 -- where on the combined chart
#                               this stage's recipe is drawn
#   explanation:   educational blurb shown after the compound resolves
COMPOUND_PATTERNS: Final[list[dict]] = [
    {
        "key":   "bear_flag_to_ascending_triangle",
        "name":  "Bear Flag -> Ascending Triangle",
        "stages": [
            {"shape": "bear_flag",          "answer": "bear_flag",          "x_range": (0.00, 0.50)},
            {"shape": "ascending_triangle", "answer": "ascending_triangle", "x_range": (0.50, 1.00)},
        ],
        "explanation": (
            "Continuation-down leg exhausted into a basing structure. The "
            "ascending triangle on the right says buyers are stepping in "
            "earlier each test, so the bear-flag continuation thesis is "
            "now invalidated and the bias has flipped up."
        ),
    },
    {
        "key":   "head_shoulders_to_bull_flag",
        "name":  "Head and Shoulders -> Bull Flag",
        "stages": [
            {"shape": "head_and_shoulders", "answer": "head_and_shoulders", "x_range": (0.00, 0.55)},
            {"shape": "bull_flag",          "answer": "bull_flag",          "x_range": (0.55, 1.00)},
        ],
        "explanation": (
            "The textbook H&S top resolved lower as expected, but the "
            "follow-through impulse and bull-flag consolidation say "
            "sellers got trapped at the lows. The pattern flipped from "
            "reversal-down into continuation-up."
        ),
    },
    {
        "key":   "double_bottom_to_bull_flag",
        "name":  "Double Bottom -> Bull Flag",
        "stages": [
            {"shape": "double_bottom", "answer": "double_bottom", "x_range": (0.00, 0.50)},
            {"shape": "bull_flag",     "answer": "bull_flag",     "x_range": (0.50, 1.00)},
        ],
        "explanation": (
            "Reversal-up base completes, impulse leg fires, and a tight "
            "bull-flag consolidation forms above prior resistance. Two "
            "bullish patterns stacked end-to-end -- the cleanest possible "
            "bottom-and-continuation read."
        ),
    },
    {
        "key":   "cup_handle_to_pennant",
        "name":  "Cup and Handle -> Pennant",
        "stages": [
            {"shape": "cup_and_handle", "answer": "cup_and_handle", "x_range": (0.00, 0.62)},
            {"shape": "pennant",        "answer": "pennant",        "x_range": (0.62, 1.00)},
        ],
        "explanation": (
            "Basing structure completes with the cup rim breakout, then "
            "the pennant offers a second-chance entry as the trend pauses "
            "to digest. Continuation pattern stacked on a reversal base."
        ),
    },
    {
        "key":   "rising_wedge_to_bear_flag",
        "name":  "Rising Wedge -> Bear Flag",
        "stages": [
            {"shape": "rising_wedge", "answer": "rising_wedge", "x_range": (0.00, 0.50)},
            {"shape": "bear_flag",    "answer": "bear_flag",    "x_range": (0.50, 1.00)},
        ],
        "explanation": (
            "The rising wedge resolved down as expected (sellers absorbed "
            "the narrowing momentum), and the bear-flag on the right "
            "confirms continuation lower. Trend-rollover then trend-down."
        ),
    },
    {
        "key":   "descending_triangle_to_falling_wedge",
        "name":  "Descending Triangle -> Falling Wedge",
        "stages": [
            {"shape": "descending_triangle", "answer": "descending_triangle", "x_range": (0.00, 0.50)},
            {"shape": "falling_wedge",       "answer": "falling_wedge",       "x_range": (0.50, 1.00)},
        ],
        "explanation": (
            "Bearish descending triangle breaks down, but the follow-"
            "through prints a falling wedge -- selling pressure is fading "
            "even as price drips lower. Setup for a bullish reversal "
            "from the wedge's breakout."
        ),
    },
    {
        "key":   "triple_top_to_bear_flag",
        "name":  "Triple Top -> Bear Flag",
        "stages": [
            {"shape": "triple_top", "answer": "triple_top", "x_range": (0.00, 0.58)},
            {"shape": "bear_flag",  "answer": "bear_flag",  "x_range": (0.58, 1.00)},
        ],
        "explanation": (
            "Three failed attempts at the highs gave way to a clean break "
            "down, then a bear-flag consolidation -- the textbook "
            "distribution-into-markdown sequence. Continuation bias is "
            "lower out of the flag."
        ),
    },
    {
        "key":   "inverse_hns_to_bull_flag",
        "name":  "Inverse Head and Shoulders -> Bull Flag",
        "stages": [
            {"shape": "inverse_head_and_shoulders", "answer": "inverse_head_and_shoulders", "x_range": (0.00, 0.55)},
            {"shape": "bull_flag",                   "answer": "bull_flag",                   "x_range": (0.55, 1.00)},
        ],
        "explanation": (
            "Bottoming structure completes, impulse fires above the "
            "neckline, then a tight bull-flag forms in the new range. "
            "Bull-flag projection from the flagpole typically targets the "
            "head-to-neckline distance added to the breakout."
        ),
    },
    {
        "key":   "bull_flag_to_symmetrical_triangle",
        "name":  "Bull Flag -> Symmetrical Triangle",
        "stages": [
            {"shape": "bull_flag",            "answer": "bull_flag",            "x_range": (0.00, 0.50)},
            {"shape": "symmetrical_triangle", "answer": "symmetrical_triangle", "x_range": (0.50, 1.00)},
        ],
        "explanation": (
            "The bull-flag continuation resolved up as expected, then "
            "price coiled into a symmetrical triangle -- a neutral "
            "consolidation that typically resolves in the prevailing "
            "direction. Bias remains up unless the lower trendline breaks."
        ),
    },
    {
        "key":   "double_top_to_rising_wedge",
        "name":  "Double Top -> Rising Wedge",
        "stages": [
            {"shape": "double_top",   "answer": "double_top",   "x_range": (0.00, 0.55)},
            {"shape": "rising_wedge", "answer": "rising_wedge", "x_range": (0.55, 1.00)},
        ],
        "explanation": (
            "Failed second top followed by a dead-cat rally that traces a "
            "rising wedge -- narrowing momentum on the bounce. Classic "
            "lower-high distribution structure that usually resolves to "
            "a fresh leg down."
        ),
    },
]

COMPOUND_BY_KEY: Final[dict[str, dict]] = {c["key"]: c for c in COMPOUND_PATTERNS}

# Reward multiplier for a fully-correct compound round (both halves right).
COMPOUND_REWARD_MULT: Final[float] = 1.5


def compound_chance_for_round(round_index: int) -> float:
    """Probability that a Pattern-Lab round is rendered as a compound.

    Disabled in the warm-up rounds, ramps up to majority-compound at
    high streaks. Returns a probability in 0..1.
    """
    r = int(round_index)
    if r < 5:
        return 0.0
    if r < 10:
        return 0.30
    if r < 15:
        return 0.60
    return 0.85


# ============================================================================
# Cycle Phase bank
# ============================================================================
# Each entry mirrors the gauge schema (title + rows + answer + explanation).
# The player classifies the market state given a snapshot of macro / on-chain
# / sentiment metrics. Four answer buckets.
CYCLE_PHASES: Final[list[dict]] = [
    {
        "key":   "deep_accumulation",
        "title": "Cycle Snapshot · Macro Trough",
        "rows":  [
            ("MVRV-Z",        "-0.4",     "deep value"),
            ("MTA dominance", "62%",      "rising"),
            ("Fear & Greed",  "12",       "extreme fear"),
            ("ARC/MTA",       "0.038",    "bleeding"),
        ],
        "answer": "accumulation",
        "explanation": (
            "Deeply negative MVRV-Z with capitulation sentiment and "
            "rising MTA dominance is the textbook accumulation footprint: "
            "weak hands are out, smart money is buying MTA first while "
            "alts still bleed."
        ),
    },
    {
        "key":   "stealth_accumulation",
        "title": "Cycle Snapshot · Quiet Base",
        "rows":  [
            ("MVRV-Z",        "0.1",      "neutral"),
            ("MTA dominance", "58%",      "stable"),
            ("Fear & Greed",  "29",       "fear"),
            ("Realised cap",  "Rising slowly", ""),
        ],
        "answer": "accumulation",
        "explanation": (
            "Realised cap drifting up while sentiment stays fearful and "
            "MVRV-Z is near zero means coins are moving into longer-term "
            "wallets at a higher cost basis. Classic stealth accumulation "
            "after the capitulation phase."
        ),
    },
    {
        "key":   "early_markup",
        "title": "Cycle Snapshot · Trend Confirm",
        "rows":  [
            ("MVRV-Z",        "1.2",      "trend forming"),
            ("MTA dominance", "55%",      "falling"),
            ("Fear & Greed",  "62",       "greed"),
            ("ARC/MTA",       "Breaking 90d", "rotation"),
        ],
        "answer": "markup",
        "explanation": (
            "MTA dominance is rolling over with ARC/MTA breaking out, and "
            "MVRV-Z has reclaimed positive territory. Capital is rotating "
            "down the risk curve -- the markup phase has begun and the "
            "broader alt complex is leveraged to it."
        ),
    },
    {
        "key":   "mid_markup",
        "title": "Cycle Snapshot · Trend Body",
        "rows":  [
            ("MVRV-Z",        "2.4",      "elevated"),
            ("MTA dominance", "48%",      "falling"),
            ("Fear & Greed",  "78",       "greed"),
            ("Alt season",    "70 / 100", "alt season"),
        ],
        "answer": "markup",
        "explanation": (
            "Alt season indicator above 70 with MTA dominance still "
            "falling means the trend is in its body, not its edge. MVRV-Z "
            "is hot but historically can run much higher -- this is the "
            "middle of the markup, not the top."
        ),
    },
    {
        "key":   "blowoff_distribution",
        "title": "Cycle Snapshot · Parabolic",
        "rows":  [
            ("MVRV-Z",        "6.8",      "extreme"),
            ("MTA dominance", "41%",      "ATH alts"),
            ("Fear & Greed",  "94",       "extreme greed"),
            ("Funding",       "Pinned positive", "max longs"),
        ],
        "answer": "distribution",
        "explanation": (
            "Extreme MVRV-Z, extreme greed, peak alt dominance, and pinned "
            "funding all together = the distribution phase signature. "
            "Late-cycle euphoria with retail at maximum exposure -- the "
            "cycle is selling to itself."
        ),
    },
    {
        "key":   "distribution_topping",
        "title": "Cycle Snapshot · Late Cycle",
        "rows":  [
            ("MVRV-Z",        "4.1",      "high"),
            ("LTH supply",    "Distributing", ""),
            ("Fear & Greed",  "82",       "greed"),
            ("OI",            "ATH",      "max exposure"),
        ],
        "answer": "distribution",
        "explanation": (
            "Long-term holder supply distributing (smart money selling) "
            "with OI at all-time highs is the canonical top-formation "
            "footprint. Price can still print higher highs while supply "
            "transfers from old hands to new -- the actual top often "
            "lags the distribution signal."
        ),
    },
    {
        "key":   "early_markdown",
        "title": "Cycle Snapshot · Trend Roll",
        "rows":  [
            ("MVRV-Z",        "1.6",      "falling"),
            ("MTA dominance", "50%",      "rising"),
            ("Fear & Greed",  "31",       "fear"),
            ("ARC/MTA",       "Losing trend", "alts bleeding"),
        ],
        "answer": "markdown",
        "explanation": (
            "MTA dominance rising while alt ratios bleed is the early-"
            "markdown signature: capital flees the risk curve back into "
            "MTA. Sentiment has flipped to fear, but MVRV-Z still says "
            "valuations have further to compress."
        ),
    },
    {
        "key":   "mid_markdown",
        "title": "Cycle Snapshot · Markdown Body",
        "rows":  [
            ("MVRV-Z",        "0.4",      "compressing"),
            ("Realised cap",  "Falling",  "capitulation"),
            ("Fear & Greed",  "18",       "extreme fear"),
            ("Spot volume",   "Drying up", ""),
        ],
        "answer": "markdown",
        "explanation": (
            "Realised cap actively falling means coins are moving at a "
            "loss -- the capitulation phase is happening in real time. "
            "MVRV-Z compressing toward zero with dry volume says the "
            "markdown still has room before the cycle resets."
        ),
    },
    {
        "key":   "ranging_accumulation",
        "title": "Cycle Snapshot · Range Bottom",
        "rows":  [
            ("MVRV-Z",        "-0.1",     "neutral / low"),
            ("Realised cap",  "Flat",     "post-flush"),
            ("Fear & Greed",  "26",       "fear"),
            ("OI",            "Building from lows", ""),
        ],
        "answer": "accumulation",
        "explanation": (
            "MVRV-Z hovering near zero after a flush, realised cap "
            "stabilising flat, and open interest rebuilding from lows is "
            "the late-stage accumulation footprint. Smart money is "
            "starting to lean long under cover of lingering fear."
        ),
    },
    {
        "key":   "alt_blowoff",
        "title": "Cycle Snapshot · Alt Mania",
        "rows":  [
            ("MTA dominance", "39%",      "ATH alts"),
            ("Alt season",    "92 / 100", "alt mania"),
            ("Fear & Greed",  "91",       "extreme greed"),
            ("Meme volume",    "Outpacing MTA", "rotation"),
        ],
        "answer": "distribution",
        "explanation": (
            "Meme-tier volume eclipsing MTA with alt-season index above 90 "
            "is a hallmark distribution signal -- the most reflexive, "
            "lowest-quality assets are absorbing the marginal flow. End-"
            "of-cycle rotation, not start-of-cycle."
        ),
    },
    {
        "key":   "early_recovery_markup",
        "title": "Cycle Snapshot · Reclaim",
        "rows":  [
            ("MVRV-Z",        "0.6",      "reclaiming"),
            ("MTA dominance", "53%",      "stable"),
            ("Fear & Greed",  "55",       "neutral / greed"),
            ("200wk MA",       "Reclaimed", ""),
        ],
        "answer": "markup",
        "explanation": (
            "Reclaiming the 200-week MA on a green MVRV-Z print marks "
            "the structural transition from base to trend. Sentiment "
            "isn't euphoric yet -- the markup phase is early and most "
            "of the move is still ahead."
        ),
    },
    {
        "key":   "post_top_markdown",
        "title": "Cycle Snapshot · Post-ATH",
        "rows":  [
            ("MVRV-Z",        "3.2",      "fading"),
            ("MTA dominance", "44%",      "rising"),
            ("Fear & Greed",  "48",       "neutral"),
            ("Price vs ATH",  "-22% / 4w", ""),
        ],
        "answer": "markdown",
        "explanation": (
            "First leg down from the cycle high with MTA dominance "
            "rising and MVRV-Z compressing from extreme readings is the "
            "post-top markdown opener. Sentiment hasn't cratered yet -- "
            "that comes later, deeper into the move."
        ),
    },
    {
        "key":   "deep_capitulation",
        "title": "Cycle Snapshot · Capitulation",
        "rows":  [
            ("MVRV-Z",        "-1.1",     "generational"),
            ("Fear & Greed",  "6",        "max fear"),
            ("Realised loss", "Multi-year ATH", "capitulation"),
            ("Spot volume",   "Forced sells", ""),
        ],
        "answer": "accumulation",
        "explanation": (
            "MVRV-Z deep negative with realised loss spiking to a multi-"
            "year high and max-fear sentiment is the historical signature "
            "of a generational accumulation zone. Forced selling is the "
            "buyer's friend -- the floor prints when it has nowhere left "
            "to go."
        ),
    },
]

CYCLE_BY_KEY: Final[dict[str, dict]] = {c["key"]: c for c in CYCLE_PHASES}

CYCLE_OPTIONS: Final[tuple[str, ...]] = (
    "accumulation", "markup", "distribution", "markdown",
)
CYCLE_OPTION_LABELS: Final[dict[str, str]] = {
    "accumulation": "Accumulation",
    "markup":       "Markup",
    "distribution": "Distribution",
    "markdown":     "Markdown",
}
CYCLE_OPTION_EMOJI: Final[dict[str, str]] = {
    "accumulation": "\U0001F9F1",   # brick
    "markup":       "\U0001F680",   # rocket
    "distribution": "\U0001F4B0",   # money bag
    "markdown":     "\U0001F4C9",   # chart down
}


# ============================================================================
# Sage Shop  -  SAGE-priced consumables
# ============================================================================
# A small SAGE sink. Every item is a one-run consumable that only affects the
# Sage games, so spending earned SAGE never leaks power into the wider
# economy. Items are bought into a per-user inventory (sage_items table) and
# spent automatically on the next run:
#   * time_crystal / insight_lens / scholar_draft -- consumed at run start;
#   * second_wind -- consumed only if/when it actually saves a wrong answer.
# Prices are deliberately modest and easy to retune here.
SAGE_SHOP_ITEMS: Final[dict[str, dict]] = {
    "time_crystal": {
        "name":       "Time Crystal",
        "emoji":      "\U000023F1",   # stopwatch
        "price_sage": 1.0,
        "blurb":      "Adds +8s to every round timer for your next run.",
        "aliases":    ("time", "crystal", "tc", "clock"),
    },
    "insight_lens": {
        "name":       "Insight Lens",
        "emoji":      "\U0001F50D",   # magnifying glass
        "price_sage": 2.0,
        "blurb":      "Removes one wrong option from every round for your next run.",
        "aliases":    ("insight", "lens", "il", "5050"),
    },
    "scholar_draft": {
        "name":       "Scholar's Draft",
        "emoji":      "\U0001F4DC",   # scroll
        "price_sage": 3.0,
        "blurb":      "Doubles the Sage XP you earn for your next run.",
        "aliases":    ("scholar", "draft", "sd", "xp"),
    },
    "second_wind": {
        "name":       "Second Wind",
        "emoji":      "\U0001F4A8",   # dash
        "price_sage": 5.0,
        "blurb":      (
            "Forgives your first wrong answer in a run -- the run continues "
            "(that round pays nothing)."
        ),
        "aliases":    ("second", "wind", "sw", "save"),
    },
}

# Stable display / iteration order for the shop embed.
SAGE_SHOP_ORDER: Final[tuple[str, ...]] = (
    "time_crystal", "insight_lens", "scholar_draft", "second_wind",
)

# Per-run timer bonus (seconds) granted by a Time Crystal.
SAGE_TIME_CRYSTAL_BONUS_S: Final[int] = 8
# XP multiplier granted by a Scholar's Draft.
SAGE_SCHOLAR_DRAFT_XP_MULT: Final[float] = 2.0


def resolve_shop_item(text: str) -> str | None:
    """Map free-form user text to a SAGE_SHOP_ITEMS key, or None.

    Accepts the exact key, any registered alias, or a unique key prefix.
    """
    s = (text or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not s:
        return None
    if s in SAGE_SHOP_ITEMS:
        return s
    for key, meta in SAGE_SHOP_ITEMS.items():
        if s in meta.get("aliases", ()):
            return key
    prefix_hits = [k for k in SAGE_SHOP_ITEMS if k.startswith(s)]
    return prefix_hits[0] if len(prefix_hits) == 1 else None


# ============================================================================
# Sage-Mastery XP curve  -  mirrors farming/fishing's level_payout_mult shape
# ============================================================================
SAGE_XP_PER_CORRECT: Final[int] = 5
SAGE_LEVEL_PAYOUT_PER_LEVEL: Final[float] = 0.01
SAGE_XP_CURVE: Final[int] = 50  # XP per level = 50 * level


def sage_level_from_xp(xp: int) -> int:
    """Return the integer Sage level for a cumulative XP total.

    Arithmetic series: level n requires sum_{k=1..n}(SAGE_XP_CURVE * k).
    Lv 1: 0 XP; Lv 2: 50; Lv 3: 150; ... Lv 50: ~63750 XP.
    """
    if xp <= 0:
        return 1
    lvl = 1
    needed = SAGE_XP_CURVE
    while xp >= needed:
        lvl += 1
        needed += SAGE_XP_CURVE * lvl
    return lvl


def sage_xp_progress(xp: int) -> tuple[int, int]:
    """Return ``(xp_into_current_level, xp_to_next_level)``."""
    lvl = sage_level_from_xp(xp)
    floor_xp = SAGE_XP_CURVE * (lvl - 1) * lvl // 2
    next_xp  = SAGE_XP_CURVE * lvl * (lvl + 1) // 2
    return (max(0, int(xp - floor_xp)), max(1, int(next_xp - floor_xp)))


def sage_level_payout_mult(level: int) -> float:
    """Per-Sage-level payout boost. Lv 1 = 1.0x, Lv 50 = ~1.49x."""
    return 1.0 + max(0, int(level) - 1) * SAGE_LEVEL_PAYOUT_PER_LEVEL


# Disco quips for AI refusals when a Sage game is active. Picked at random
# by services/sage.py::random_refusal so the AI says something different
# every time it gets pestered mid-quiz.
AI_REFUSAL_QUIPS: Final[tuple[str, ...]] = (
    "I'm not your study guide. Finish the run first, then I'll roast your wrong picks for free.",
    "Bold of you to ask the bot for the answer to the educational game. Try again, but with thinking.",
    "Cheating in a learn-and-earn game is a special kind of audacity. I respect the hustle. I will not help.",
    "If I gave you the answer, you'd still bet the wrong way live. Read the chart.",
    "No hints. The whole point is the gain you make between guesses. Suffer in silence.",
    "Look at it this way: if I tell you, the EDU is unearned. And unearned EDU spends just like real EDU... which means it doesn't.",
    "I'd help, but the chart isn't open in my context. Also I wouldn't help anyway.",
    "Use your eyes. They came free with the wallet.",
    "Asking me for chart answers is like asking a tax form for an opinion. Try again.",
    "Hard pass. Go pick the wrong one and learn something.",
)


__all__ = [
    "GAME_PATTERN", "GAME_GAUGE", "GAME_TKNOM", "GAME_CYCLE", "GAMES",
    "GAME_TITLES", "GAME_EMOJIS",
    "PATTERNS", "PATTERN_BY_KEY", "PATTERN_OPTION_COUNT",
    "INDICATORS", "INDICATOR_BY_KEY",
    "GAUGE_OPTIONS", "GAUGE_OPTION_LABELS", "GAUGE_OPTION_EMOJI",
    "TOKENOMICS", "TOKENOMICS_BY_KEY",
    "TKNOM_OPTIONS", "TKNOM_OPTION_LABELS", "TKNOM_OPTION_EMOJI",
    "COMPOUND_PATTERNS", "COMPOUND_BY_KEY", "COMPOUND_REWARD_MULT",
    "compound_chance_for_round",
    "CYCLE_PHASES", "CYCLE_BY_KEY",
    "CYCLE_OPTIONS", "CYCLE_OPTION_LABELS", "CYCLE_OPTION_EMOJI",
    "SAGE_SHOP_ITEMS", "SAGE_SHOP_ORDER", "resolve_shop_item",
    "SAGE_TIME_CRYSTAL_BONUS_S", "SAGE_SCHOLAR_DRAFT_XP_MULT",
    "SAGE_XP_PER_CORRECT", "SAGE_LEVEL_PAYOUT_PER_LEVEL",
    "sage_level_from_xp", "sage_xp_progress", "sage_level_payout_mult",
    "AI_REFUSAL_QUIPS",
]
