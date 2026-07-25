"""ROB-941 (AC2/AC3) — frozen universe + UTC half-open window (D1 approved 2026-07-17).

Single source of truth other rob941_* modules and the manifest consult. These
constants are literal, not derived from the current wall-clock time — the
window never tracks "latest". Editing them after a manifest/campaign has been
built starts a new lineage (the manifest's ``content_hash`` changes); this
module is not meant to be tuned per-run.
"""

from __future__ import annotations

from datetime import UTC, datetime

WINDOW_START_ISO = "2025-07-01T00:00:00Z"
WINDOW_END_ISO = "2026-07-01T00:00:00Z"  # exclusive


def _iso_to_epoch_ms(iso: str) -> int:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


WINDOW_START_MS = _iso_to_epoch_ms(WINDOW_START_ISO)
WINDOW_END_MS = _iso_to_epoch_ms(WINDOW_END_ISO)  # exclusive

# BTCUSDT/XRPUSDT/DOGEUSDT/SOLUSDT, frozen order (ROB-941 AC2).
UNIVERSE: tuple[str, ...] = ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")

BTC_INELIGIBLE_REASON = "min_notional_50_exceeds_demo_cap_10"


def eligibility(symbol: str) -> dict:
    """Demo-execution eligibility record for ``symbol`` (ROB-941 AC2).

    BTC is ``historical_only``/demo-ineligible with a fixed reason (MIN_NOTIONAL
    50 exceeds the $6-10 demo notional cap); the other 3 frozen symbols are
    demo-eligible. Any symbol outside the frozen universe is a caller error.
    """
    if symbol not in UNIVERSE:
        raise ValueError(f"{symbol!r} is not in the frozen universe {UNIVERSE}")
    if symbol == "BTCUSDT":
        return {
            "historical_only": True,
            "demo_execution_eligible": False,
            "reason": BTC_INELIGIBLE_REASON,
        }
    return {
        "historical_only": False,
        "demo_execution_eligible": True,
        "reason": None,
    }


def months_in_window() -> list[tuple[int, int]]:
    """The 12 calendar (year, month) pairs the half-open window spans exactly.

    ``WINDOW_START_ISO``/``WINDOW_END_ISO`` are both month boundaries, so this
    is a closed set of full calendar months (no partial-month archive needed).
    """
    start = datetime.strptime(WINDOW_START_ISO, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    end = datetime.strptime(WINDOW_END_ISO, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) < (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months
