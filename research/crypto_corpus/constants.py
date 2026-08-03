"""Literal scope and immutable bounds for ``crypto-corpus-v1``.

The values in this module are the signed job literals.  They are deliberately
not CLI options: changing a bound requires a new, separately authorized job.
"""

from __future__ import annotations

from datetime import UTC, datetime

CORPUS_ID = "crypto-corpus-v1"
PURPOSE = "EXPLORATORY_BACKTEST_RESEARCH_ONLY"
AUTH = "NONE"
VENUES = ("upbit_krw", "binance_usdt_spot")

ARTIFACT_ROOT = "/Users/mgh3326/work/herdr-artifacts/crypto-corpus-v1"
HOLDOUT_DIR = f"{ARTIFACT_ROOT}/holdout"
HOLDOUT_ACCESS_LOG = f"{ARTIFACT_ROOT}/holdout-access.log"
PROGRESS_LOG = (
    "/Users/mgh3326/work/herdr-inbox/jobs/crypto-corpus-v1-20260803-1125/"
    "events/progress.md"
)

MAX_REQUESTS = 60_000
MAX_WALL_CLOCK_SECONDS = 12 * 60 * 60
MAX_ARTIFACT_BYTES = 15 * 1024 * 1024 * 1024
UPBIT_MIN_REQUEST_INTERVAL_SECONDS = 0.3
BINANCE_MIN_REQUEST_INTERVAL_SECONDS = 0.25

CUTOFF_END = datetime(2026, 8, 1, tzinfo=UTC)  # exclusive
HOLDOUT_START = datetime(2025, 1, 1, tzinfo=UTC)
HOUR_WINDOW_START = datetime(2023, 8, 1, tzinfo=UTC)
EXPLORATION_END = HOLDOUT_START  # exclusive
UPBIT_DAILY_EARLIEST = datetime(2017, 10, 24, tzinfo=UTC)
BINANCE_DAILY_EARLIEST = datetime(2017, 7, 14, tzinfo=UTC)

UPBIT_PAGE_SIZE = 200
BINANCE_PAGE_SIZE = 1_000

UPBIT_MARKETS_URL = "https://api.upbit.com/v1/market/all?isDetails=true"
UPBIT_DAYS_URL = "https://api.upbit.com/v1/candles/days"
UPBIT_HOURS_URL = "https://api.upbit.com/v1/candles/minutes/60"
BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# The probes are deliberately fixed historic market identifiers.  Their raw
# request/response evidence, not this label, determines the availability
# verdict recorded in the manifest.
UPBIT_DELISTED_PROBE_MARKET = "KRW-NPXS"
BINANCE_DELISTED_PROBE_SYMBOL = "BCCUSDT"


def utc_iso(value: datetime) -> str:
    """Return a canonical UTC timestamp with a literal ``Z`` suffix."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
