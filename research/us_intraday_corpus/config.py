"""Literal configuration for us-intraday-corpus-v1.

Every value here is transcribed verbatim from the orch-mock brief (2026-08-03 14:50 KST) §1.
Nothing in this module may be inferred, defaulted or widened at runtime.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Final

# --- §1 literals -----------------------------------------------------------
CORPUS_ID: Final = "us-intraday-corpus-v1"
PURPOSE: Final = "EXPLORATORY_BACKTEST_RESEARCH_ONLY"
SURVIVORSHIP_BIASED: Final = True

SOURCE_PRODUCT: Final = "alpaca_data_api (feed=sip)"
DATA_HOST: Final = "data.alpaca.markets"
DATA_FEED: Final = "sip"

# Hosts that must never be contacted by this corpus builder. Enforced in
# alpaca_data.assert_data_host() on every single request.
FORBIDDEN_HOSTS: Final = frozenset(
    {
        "api.alpaca.markets",
        "paper-api.alpaca.markets",
        "broker-api.alpaca.markets",
        "broker-api.sandbox.alpaca.markets",
    }
)

CODE_DIR: Final = Path(__file__).resolve().parent
ARTIFACT_ROOT: Final = Path("/Users/mgh3326/work/herdr-artifacts/us-intraday-corpus-v1")

UNIVERSE_FILE: Final = Path(
    "/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/inputs/common_stock_universe.csv"
)
UNIVERSE_FILE_SHA256: Final = (
    "ac0871793604693381048cd30472059a71a76b4bec6b942502b3b23f9d9ba3b7"
)
UNIVERSE_COUNT: Final = 5355

# The sister corpus we read (read-only) to derive the 1Min top-500 selection.
# NOTE: only `dataset/` may be read. `holdout/` is forbidden on both path and
# date axes -- see selection.py.
SISTER_CORPUS_ROOT: Final = Path("/Users/mgh3326/work/herdr-artifacts/us-corpus-v1")
SISTER_DATASET_DIR: Final = SISTER_CORPUS_ROOT / "dataset"
SISTER_HOLDOUT_DIR: Final = SISTER_CORPUS_ROOT / "holdout"

START_DATE: Final = _dt.date(2016, 1, 1)
CUTOFF_DATE: Final = _dt.date(2026, 7, 31)

TRAIN: Final = (_dt.date(2016, 1, 1), _dt.date(2022, 12, 31))
VALIDATION: Final = (_dt.date(2023, 1, 1), _dt.date(2024, 12, 31))
HOLDOUT: Final = (_dt.date(2025, 1, 1), _dt.date(2026, 7, 31))
FORWARD_OOS_START: Final = _dt.date(2026, 8, 3)

# 1Min universe selection window (exploration only -- strictly before HOLDOUT).
TOP500_WINDOW: Final = (_dt.date(2024, 1, 1), _dt.date(2024, 12, 31))
TOP500_COUNT: Final = 500

# Timestamp policy. Direct product of ROB-1206: store UTC verbatim, derive
# session_date in America/New_York. A KST anchor would shift every row by a day.
TIMESTAMP_STORAGE_TZ: Final = "UTC"
SESSION_DATE_TZ: Final = "America/New_York"
FORBIDDEN_SESSION_TZ: Final = "Asia/Seoul"

# --- budget rails (hard caps -- not raisable by this worker) ----------------
MIN_REQUEST_INTERVAL_SEC: Final = 0.35
MAX_REQUESTS: Final = 80_000
MAX_WALL_CLOCK_HOURS: Final = 12
MAX_ARTIFACT_GIB: Final = 25

# --- scope decision (operator, 2026-08-03) ---------------------------------
# Option C: collect 1Min for the top 500 only. 1Hour collection is dropped.
#
# This is a DEFERRAL, not a permanent abandonment, and the distinction is
# recorded in the manifest so a future reader does not mistake one for the
# other:
#   * the top-500's hourly bars are DERIVABLE LOCALLY by resampling their 1Min
#     data, so collecting them over the wire would be redundant;
#   * the remaining ~4,855 symbols' hourly bars are simply not fetched yet.
#     They are recorded as a DATA_GAP to be filled if and when an hourly
#     hypothesis actually needs them.
# The blocker was arithmetic, not preference: measured 1Hour throughput tops
# out at ~416 rows/request, so the full-universe hourly corpus needs
# 130k-246k requests against MAX_REQUESTS=80000.
SCOPE_DECISION: Final = "C_1M_TOP500_ONLY"
HOUR_COLLECTION_ABANDONED: Final = True
HOUR_DERIVABLE_FROM_1M: Final = True
HOUR_DATA_GAP: Final = {
    "scope": "1Hour bars for universe symbols outside the 1Min top-500",
    "symbols_not_collected": UNIVERSE_COUNT - TOP500_COUNT,
    "status": "DEFERRED_NOT_ABANDONED",
    "reason": (
        "measured 1Hour throughput is <=416 rows/request, so the full "
        "5,355-symbol hourly corpus needs 130k-246k requests against "
        "MAX_REQUESTS=80000 (1.6x-3.1x over) before 1Min is added"
    ),
    "resume_condition": "an hourly-timeframe hypothesis actually requires them",
    "top500_hourly_note": (
        "the top-500's hourly bars are not a gap: they are derivable locally "
        "by resampling this corpus's 1Min data"
    ),
}

SOURCE_FALLBACK: Final = "NONE"
OPERATING_DB_WRITES: Final = 0
BROKER_OR_ACCOUNT_CALLS: Final = 0

TERMINAL_VERDICTS: Final = (
    "READY_FOR_RESEARCH",
    "BUILT_WITH_GAPS",
    "BLOCKED_PRECONDITION",
)

# --- derived artifact paths ------------------------------------------------
INPUTS_DIR: Final = ARTIFACT_ROOT / "inputs"
DATASET_DIR: Final = ARTIFACT_ROOT / "dataset"
HOLDOUT_DIR: Final = ARTIFACT_ROOT / "holdout"
REPORTS_DIR: Final = ARTIFACT_ROOT / "reports"
STAGING_DIR: Final = ARTIFACT_ROOT / "_staging"
MANIFEST_PATH: Final = ARTIFACT_ROOT / "manifest.json"
CHECKSUMS_PATH: Final = ARTIFACT_ROOT / "checksums.sha256"
ACCESS_LOG_PATH: Final = ARTIFACT_ROOT / "holdout-access.log"

JOB_EVENTS_DIR: Final = Path(
    "/Users/mgh3326/work/herdr-inbox/jobs/us-intraday-corpus-v1-20260803-1450/events"
)
PROGRESS_PATH: Final = JOB_EVENTS_DIR / "progress.md"


def is_holdout_date(day: _dt.date) -> bool:
    """True when `day` falls in the sealed holdout window."""
    return HOLDOUT[0] <= day <= HOLDOUT[1]


def is_exploration_date(day: _dt.date) -> bool:
    """True when `day` is inside train+validation (the readable window)."""
    return TRAIN[0] <= day <= VALIDATION[1]
