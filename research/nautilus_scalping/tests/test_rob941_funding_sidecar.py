"""ROB-941 (AC8) — PIT-safe funding-rate sidecar: no lookahead, realized-crossing only.

Built on ``funding_oi_archive.parse_funding_csv`` (already unit-tested for CSV
parsing itself). This module tests only the PIT query semantics: entry-gate uses
last-KNOWN rate (never a future row), and PnL uses realized crossings strictly
inside the holding window.
"""

from funding_oi_archive import parse_funding_csv
from rob941_funding_sidecar import FundingSidecar

CSV = (
    "calc_time,funding_interval_hours,last_funding_rate\n"
    "1751328000007,8,0.00010000\n"
    "1751356800002,8,0.00010000\n"
    "1751385600000,8,0.00001379\n"
    "1751414400001,8,-0.00005510\n"
)


def _sidecar() -> FundingSidecar:
    return FundingSidecar.from_rows("XRPUSDT", parse_funding_csv(CSV))


def test_last_known_rate_returns_most_recent_row_at_or_before_ts():
    sc = _sidecar()
    r = sc.last_known_rate(1751356800002)
    assert r.calc_time == 1751356800002  # known exactly AT calc_time


def test_last_known_rate_never_leaks_the_next_future_row():
    sc = _sidecar()
    r = sc.last_known_rate(1751356800002 - 1)  # 1ms before the 2nd row becomes known
    assert (
        r.calc_time == 1751328000007
    )  # must fall back to the prior row, not leak forward


def test_last_known_rate_before_any_data_is_none():
    sc = _sidecar()
    assert sc.last_known_rate(1751328000007 - 1) is None


def test_last_known_rate_after_all_data_returns_the_last_row():
    sc = _sidecar()
    r = sc.last_known_rate(1751414400001 + 999_999)
    assert r.calc_time == 1751414400001


def test_realized_crossings_includes_only_events_strictly_inside_the_hold_window():
    sc = _sidecar()
    # holding window spans exactly the 2nd and 3rd funding events, not the 1st or 4th
    crossings = sc.realized_crossings(1751356800002, 1751414400001)
    assert [r.calc_time for r in crossings] == [1751356800002, 1751385600000]


def test_realized_crossings_empty_when_no_event_falls_inside():
    sc = _sidecar()
    crossings = sc.realized_crossings(1751328000007 + 1, 1751356800002)
    assert crossings == ()


def test_realized_crossings_empty_for_degenerate_or_inverted_window():
    sc = _sidecar()
    assert sc.realized_crossings(1751356800002, 1751356800002) == ()
    assert sc.realized_crossings(1751414400001, 1751328000007) == ()


def test_from_rows_rejects_conflicting_duplicate_calc_time():
    import pytest

    csv = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        "1700000000000,8,0.0001\n"
        "1700000000000,8,0.0002\n"  # same calc_time, different rate -> conflict
    )
    with pytest.raises(ValueError, match="conflicting"):
        FundingSidecar.from_rows("XRPUSDT", parse_funding_csv(csv))
