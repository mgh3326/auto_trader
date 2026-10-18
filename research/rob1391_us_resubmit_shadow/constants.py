"""Pre-registered constants (ROB-1383 prereg v1_review, operator Q-1018-1 = A).

Every value here is sealed into the T0 manifest. Changing one after the seal is
a pre-registration change, which needs operator approval and a new epoch --
`manifest.verify_constants` refuses a ledger whose sealed values differ.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

EXPERIMENT_ID: Final = "rob1383-us-original-limit-resubmit-shadow-v1"
SCHEMA_VERSION: Final = 1

# §4 기간 -- 20 consecutive US sessions, <=3 parents/day, <=60 total.
WINDOW_SESSIONS: Final = 20
MAX_PARENTS_PER_SESSION: Final = 3
MAX_PARENTS_TOTAL: Final = 60

# §1 -- C gets 0 extra sessions, T gets D1..D3.
RESUBMIT_SESSIONS: Final = 3
# §3 M2 -- valuation at the close of the 20th US session after D0.
EVAL_SESSION_OFFSET: Final = 20
# Auxiliary (separated) metric: fill session + 20 sessions.
AUX_FILL_OFFSET: Final = 20

# §1 parent dedupe (v1 definition carried over).
DEDUPE_MAX_SESSION_GAP: Final = 5
DEDUPE_ANCHOR_BAND: Final = Decimal("0.05")

# §1 liquidity filter (research contract, identical for both arms).
LIQUIDITY_LOOKBACK_SESSIONS: Final = 20
MIN_MEDIAN_DOLLAR_VOLUME_USD: Final = Decimal("10000000")
MAX_SPREAD_BP: Final = Decimal("30")

# §3 conservative fill model.
VOLUME_CAP_FRACTION: Final = Decimal("0.01")
SLIPPAGE_BP_MAIN: Final = Decimal("10")
SLIPPAGE_BP_SENSITIVITY: Final = (Decimal("0"), Decimal("20"))

# §4 virtual risk budget.
E0_USD: Final = Decimal("10000")
LOSS_STOP_EQUITY_FRACTION: Final = Decimal("0.05")  # T equity <= E0 * 0.95
LOSS_STOP_PARENT_FRACTION: Final = Decimal("0.05")  # parent net <= -0.05 * B_i

# §3 statistics.
SEED: Final = 1383
BOOTSTRAP_REPLICATES: Final = 10_000
HOLM_ALPHAS: Final = (0.025, 0.05)
CI_LEVEL: Final = 0.95
MIN_PARENTS_FOR_CANDIDATE: Final = 30
MIN_SYMBOLS_FOR_CANDIDATE: Final = 10

FILL_MODEL_ID: Final = (
    "conservative_v1:low<=P-1tick&valid_ask<=P&qty<=floor(1%vol)&int&price=P"
)
OPTIMISTIC_FILL_MODEL_ID: Final = "optimistic_v1:low<=P&qty=remaining&price=P"

# DEVIATION_PROPOSED (report): the live exit policy is session judgment and
# cannot be replayed deterministically, so both arms hold to D+20 and are
# marked to market net of liquidation cost.
EXIT_POLICY_ID: Final = "hold_to_d20_mark_to_market_net_liquidation_v1"

PRICE_ONE_DOLLAR: Final = Decimal("1")
TICK_AT_OR_ABOVE_1: Final = Decimal("0.01")
TICK_BELOW_1: Final = Decimal("0.0001")

BP: Final = Decimal("0.0001")


def sealed_constants() -> dict[str, str | int | float | list]:
    """The constant block sealed into the manifest (stringified decimals)."""

    return {
        "experiment_id": EXPERIMENT_ID,
        "schema_version": SCHEMA_VERSION,
        "window_sessions": WINDOW_SESSIONS,
        "max_parents_per_session": MAX_PARENTS_PER_SESSION,
        "max_parents_total": MAX_PARENTS_TOTAL,
        "resubmit_sessions": RESUBMIT_SESSIONS,
        "eval_session_offset": EVAL_SESSION_OFFSET,
        "aux_fill_offset": AUX_FILL_OFFSET,
        "dedupe_max_session_gap": DEDUPE_MAX_SESSION_GAP,
        "dedupe_anchor_band": str(DEDUPE_ANCHOR_BAND),
        "liquidity_lookback_sessions": LIQUIDITY_LOOKBACK_SESSIONS,
        "min_median_dollar_volume_usd": str(MIN_MEDIAN_DOLLAR_VOLUME_USD),
        "max_spread_bp": str(MAX_SPREAD_BP),
        "volume_cap_fraction": str(VOLUME_CAP_FRACTION),
        "slippage_bp_main": str(SLIPPAGE_BP_MAIN),
        "slippage_bp_sensitivity": [str(x) for x in SLIPPAGE_BP_SENSITIVITY],
        "e0_usd": str(E0_USD),
        "loss_stop_equity_fraction": str(LOSS_STOP_EQUITY_FRACTION),
        "loss_stop_parent_fraction": str(LOSS_STOP_PARENT_FRACTION),
        "seed": SEED,
        "bootstrap_replicates": BOOTSTRAP_REPLICATES,
        "holm_alphas": list(HOLM_ALPHAS),
        "ci_level": CI_LEVEL,
        "min_parents_for_candidate": MIN_PARENTS_FOR_CANDIDATE,
        "min_symbols_for_candidate": MIN_SYMBOLS_FOR_CANDIDATE,
        "fill_model_id": FILL_MODEL_ID,
        "optimistic_fill_model_id": OPTIMISTIC_FILL_MODEL_ID,
        "exit_policy_id": EXIT_POLICY_ID,
    }
