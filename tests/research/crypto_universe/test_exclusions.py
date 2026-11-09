from datetime import date, timedelta

import pytest

from research.crypto_universe.exclusions import crypto_exclusion_reason

pytestmark = pytest.mark.unit


def _closes(count: int, *, cutoff: date, value: float = 1.0):
    start = cutoff - timedelta(days=count - 1)
    return [(start + timedelta(days=i), value) for i in range(count)]


def test_thirty_completed_daily_closes_prove_usd_peg():
    cutoff = date(2026, 11, 8)
    assert (
        crypto_exclusion_reason(
            "NEWUSD", _closes(30, cutoff=cutoff, value=1.01), cutoff=cutoff
        )
        == "stable_peg_rule"
    )


def test_one_depeg_session_keeps_base():
    cutoff = date(2026, 11, 8)
    closes = _closes(30, cutoff=cutoff)
    closes[12] = (closes[12][0], 1.05)
    assert crypto_exclusion_reason("NEWUSD", closes, cutoff=cutoff) is None


def test_missing_session_does_not_prove_peg():
    cutoff = date(2026, 11, 8)
    assert (
        crypto_exclusion_reason("NEWUSD", _closes(29, cutoff=cutoff), cutoff=cutoff)
        is None
    )


@pytest.mark.parametrize("base", ["BFUSD", "XUSD", "PYUSD", "USDD", "USDG", "U"])
def test_known_usd_stable_bases_are_static_exclusions(base):
    assert crypto_exclusion_reason(base, cutoff=date(2026, 11, 8)) == "stable_list"


def test_wrapped_duplicate_is_excluded_but_paxg_is_kept():
    cutoff = date(2026, 11, 8)
    assert crypto_exclusion_reason("WBTC", cutoff=cutoff) == "wrapped_duplicate"
    assert crypto_exclusion_reason("PAXG", cutoff=cutoff) is None
