"""The pre-registered specification, frozen before any result was computed.

🔴 These constants are the pre-registration.  Changing one after seeing an
output is a new study round and must be labelled as such in the report; it is
not an edit to this file.
"""

from __future__ import annotations

from typing import Final

# --- event definition (identical across all three markets) -----------------
SPIKE_RETURN_MIN: Final = 0.12  # close/prev_close - 1
RSI_PERIOD: Final = 14
RSI_MIN: Final = 75.0
LEVEL_WINDOW: Final = 120  # trailing sessions used for the S/R proxy
RESISTANCE_COUNT_MAX: Final = 0  # "no named resistance overhead"

# How "명명 저항 0" is operationalised.  Both arms are fixed here before any
# P&L was computed; the report carries both.
#
#   "any"   — literal: zero resistance clusters above the decision price.
#   "named" — zero *corroborated* clusters above it, i.e. ignoring `weak`
#             single-source ones.  On a bar that closes at a new window high
#             the only "resistance" the production clustering reports is
#             `fib_0`, which IS that same bar's own intraday high.  Counting a
#             bar's own high as overhead resistance is a tautology, not a
#             named level, so "named" is the headline arm.
RESISTANCE_RULES: Final = ("any", "named")
RESISTANCE_RULE_DEFAULT: Final = "named"
NAMED_STRENGTHS: Final = ("moderate", "strong")

# --- the three options -----------------------------------------------------
TRIM_FRACTION: Final = 0.10
REBUY_STRENGTH: Final = "strong"  # nearest strong support below the event price
# Secondary arm: `strong` needs three independent sources inside one 2% band
# and is rare, which would leave option (3) mostly "unavailable".  The
# moderate-or-better fallback is reported alongside, never instead.
REBUY_STRENGTH_FALLBACK: Final = ("moderate", "strong")
HORIZONS: Final = (7, 30)  # sessions after the event bar

# --- underwater cost-basis sensitivity grid --------------------------------
# Synthetic: the average cost sits this far ABOVE the event price.
COST_BASIS_GRID: Final = (0.10, 0.20, 0.30)

# --- control group ---------------------------------------------------------
CONTROLS_PER_EVENT: Final = 5
RANDOM_SEED: Final = 20260821

# --- execution bases (equities only; crypto trades continuously) -----------
BASIS_EVENT_CLOSE: Final = "event_close"
BASIS_NEXT_OPEN: Final = "next_open"
BASES: Final = (BASIS_EVENT_CLOSE, BASIS_NEXT_OPEN)
