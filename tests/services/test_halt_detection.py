"""ROB-1236 — unit tests for the halt-*suspicion* detector.

The acceptance fixture is the real incident: 000880 한화 under a 인적분할
매매거래정지, eight consecutive daily bars with ``volume == 0`` and OHLC frozen
at 83,800, which ``analyze_stock_batch`` reported as ``data_state: "fresh"``.

The other half of these tests is the opposite failure: a normally-traded symbol
must never be flagged, because a false positive silently deletes a real buy
candidate.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from app.services.halt_detection import (
    HALTED_SUSPECT_DATA_STATE,
    KRX_HALT_MASTER_STATUS,
    MIN_FROZEN_SESSIONS,
    REASON_ZERO_VARIATION,
    REASON_ZERO_VOLUME,
    HaltBar,
    classify_bars,
    classify_ohlcv_frame,
    classify_ohlcv_rows,
)

pytestmark = pytest.mark.unit

FROZEN_PRICE = Decimal("83800")


def active_bars(n: int = 60, *, start: Decimal = Decimal("80000")) -> list[HaltBar]:
    """A normally-traded KRX series: real range, real volume, moving close."""
    bars: list[HaltBar] = []
    price = start
    for i in range(n):
        price = price + (Decimal("150") if i % 3 else Decimal("-100"))
        bars.append(
            HaltBar(
                close=price,
                high=price + Decimal("400"),
                low=price - Decimal("350"),
                volume=Decimal("120000"),
                open=price - Decimal("50"),
            )
        )
    return bars


def frozen_bars(n: int, *, price: Decimal = FROZEN_PRICE) -> list[HaltBar]:
    """``n`` halted sessions: zero volume, OHLC pinned to one price."""
    return [
        HaltBar(close=price, high=price, low=price, volume=Decimal("0"), open=price)
        for _ in range(n)
    ]


def rob1236_000880_series() -> list[HaltBar]:
    """The incident series: normal history, then 8 frozen sessions."""
    history = active_bars(192)
    # The last live session closes exactly at the price the halt freezes on.
    history[-1] = HaltBar(
        close=FROZEN_PRICE,
        high=FROZEN_PRICE + Decimal("900"),
        low=FROZEN_PRICE - Decimal("700"),
        volume=Decimal("310000"),
        open=FROZEN_PRICE - Decimal("200"),
    )
    return [*history, *frozen_bars(8)]


# ---------------------------------------------------------------------------
# The incident fixture
# ---------------------------------------------------------------------------


def test_000880_eight_frozen_sessions_is_halted_suspect():
    verdict = classify_bars(rob1236_000880_series())

    assert verdict.suspected is True
    assert verdict.frozen_sessions == 8
    assert REASON_ZERO_VOLUME in verdict.reasons
    assert verdict.bars_examined == 200


def test_000880_evidence_block_never_claims_a_confirmed_halt():
    evidence = classify_bars(rob1236_000880_series()).to_dict()

    assert evidence["suspected"] is True
    assert evidence["krx_halt_master"] == KRX_HALT_MASTER_STATUS == "unavailable"
    assert "not a confirmed trading halt" in evidence["note"]
    assert HALTED_SUSPECT_DATA_STATE == "halted_suspect"


def test_incident_is_caught_well_before_the_run_reaches_eight():
    """N=8 would have fired only on the final day; N=3 catches it mid-halt."""
    history = rob1236_000880_series()[:-8]

    at_two = classify_bars([*history, *frozen_bars(2)])
    at_three = classify_bars([*history, *frozen_bars(3)])

    assert at_two.suspected is False
    assert at_three.suspected is True
    assert MIN_FROZEN_SESSIONS == 3


# ---------------------------------------------------------------------------
# No false positives — the opposite-direction accident
# ---------------------------------------------------------------------------


def test_actively_traded_symbol_is_not_flagged():
    verdict = classify_bars(active_bars())

    assert verdict.suspected is False
    assert verdict.frozen_sessions == 0
    assert verdict.reasons == ()


def test_two_frozen_sessions_below_threshold_are_not_flagged():
    verdict = classify_bars([*active_bars(), *frozen_bars(MIN_FROZEN_SESSIONS - 1)])

    assert verdict.suspected is False
    assert verdict.frozen_sessions == MIN_FROZEN_SESSIONS - 1


def test_old_frozen_run_that_has_since_resumed_is_not_flagged():
    """Only a run ending at the newest bar counts — a resumed symbol is alive."""
    series = [*active_bars(50), *frozen_bars(8), *active_bars(5)]

    verdict = classify_bars(series)

    assert verdict.suspected is False
    assert verdict.frozen_sessions == 0


def test_limit_lock_days_are_not_mistaken_for_a_halt():
    """상한가 잠김 prints open==high==low==close with real volume.

    It is range-less but never at the prior close, which is exactly what the
    unchanged-close clause exists to separate.
    """
    series = active_bars(40)
    price = series[-1].close
    for _ in range(MIN_FROZEN_SESSIONS + 2):
        price = price + Decimal("2000")  # locked limit up, new price each day
        series.append(
            HaltBar(
                close=price,
                high=price,
                low=price,
                volume=Decimal("450000"),
                open=price,
            )
        )

    verdict = classify_bars(series)

    assert verdict.suspected is False


def test_zero_variation_alone_flags_when_volume_is_unavailable():
    """Replay dumps without a volume column still catch a frozen series."""
    series = active_bars(30)
    price = series[-1].close
    series.extend(
        HaltBar(close=price, high=price, low=price, volume=None, open=price)
        for _ in range(MIN_FROZEN_SESSIONS)
    )

    verdict = classify_bars(series)

    assert verdict.suspected is True
    assert verdict.reasons == (REASON_ZERO_VARIATION,)


# ---------------------------------------------------------------------------
# Series too short to judge
# ---------------------------------------------------------------------------


def test_empty_series_is_not_suspected():
    verdict = classify_bars([])

    assert verdict.suspected is False
    assert verdict.bars_examined == 0
    assert verdict.insufficient_bars is True


def test_series_shorter_than_the_threshold_cannot_be_suspected():
    verdict = classify_bars(frozen_bars(MIN_FROZEN_SESSIONS - 1))

    assert verdict.suspected is False
    assert verdict.insufficient_bars is True


# ---------------------------------------------------------------------------
# Input adapters
# ---------------------------------------------------------------------------


def test_classify_ohlcv_rows_matches_the_bar_classifier():
    rows = [
        [str(bar.close), str(bar.high), str(bar.low), str(bar.volume)]
        for bar in rob1236_000880_series()
    ]

    verdict = classify_ohlcv_rows(rows)

    assert verdict.suspected is True
    assert verdict.frozen_sessions == 8


def test_classify_ohlcv_rows_accepts_legacy_three_element_rows():
    """Pre-ROB-1236 replay dumps have no volume — they must still classify."""
    rows = [
        [str(bar.close), str(bar.high), str(bar.low)] for bar in rob1236_000880_series()
    ]

    verdict = classify_ohlcv_rows(rows)

    # Zero-volume evidence is gone, but the frozen OHLC still gives it away.
    assert verdict.suspected is True
    assert verdict.reasons == (REASON_ZERO_VARIATION,)


def test_classify_ohlcv_frame_reads_the_canonical_dataframe():
    bars = rob1236_000880_series()
    frame = pd.DataFrame(
        {
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        }
    )

    verdict = classify_ohlcv_frame(frame)

    assert verdict.suspected is True
    assert verdict.frozen_sessions == 8


def test_classify_ohlcv_frame_tolerates_missing_and_empty_input():
    assert classify_ohlcv_frame(None).suspected is False
    assert classify_ohlcv_frame(pd.DataFrame()).suspected is False
    assert classify_ohlcv_frame(pd.DataFrame({"foo": [1, 2]})).suspected is False
