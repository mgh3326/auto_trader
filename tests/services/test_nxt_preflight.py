from __future__ import annotations

import datetime as dt

import pytest

from app.services.nxt_preflight import (
    ROUTE_VIA_KIS,
    RETRY_AT_REGULAR,
    NxtTradability,
    evaluate_nxt_preflight,
)

_KST = dt.timezone(dt.timedelta(hours=9))
_NOW = dt.datetime(2026, 7, 3, 8, 30, tzinfo=_KST)


def _trad(eligible: bool, suspended: bool | None = None, asof=_NOW) -> NxtTradability:
    return NxtTradability(
        nxt_eligible=eligible, nxt_trading_suspended=suspended, asof=asof
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "session,eligible,suspended,expect_block,expect_reason",
    [
        ("nxt_premarket", False, None, True, "not_nxt_eligible"),
        ("nxt_after", False, None, True, "not_nxt_eligible"),
        ("nxt_premarket", True, True, True, "nxt_trading_suspended"),
        ("nxt_after", True, True, True, "nxt_trading_suspended"),
        ("nxt_premarket", True, None, False, None),
        ("nxt_after", True, False, False, None),
        ("regular", False, None, False, None),
        ("closed", False, None, False, None),
    ],
)
def test_verdict_matrix(session, eligible, suspended, expect_block, expect_reason):
    verdict = evaluate_nxt_preflight(session, _trad(eligible, suspended))
    assert verdict.block is expect_block
    assert verdict.reason == expect_reason
    if expect_block:
        assert verdict.alternatives == (RETRY_AT_REGULAR, ROUTE_VIA_KIS)
        assert verdict.advisory is False
    else:
        assert verdict.alternatives == ()


@pytest.mark.unit
def test_fail_open_when_session_unavailable():
    verdict = evaluate_nxt_preflight(None, _trad(False))
    assert verdict.block is False
    assert verdict.advisory is True
    assert verdict.reason == "nxt_session_unavailable"
    assert verdict.session is None


@pytest.mark.unit
def test_nxt_tradable_and_stale_and_public_fields():
    fresh = _trad(True, None, asof=_NOW)
    assert fresh.nxt_tradable is True
    assert fresh.is_stale(now=_NOW) is False
    stale = _trad(True, None, asof=_NOW - dt.timedelta(days=5))
    assert stale.is_stale(now=_NOW) is True
    missing = _trad(True, None, asof=None)
    assert missing.is_stale(now=_NOW) is True
    fields = fresh.public_fields(now=_NOW)
    assert fields == {
        "nxt_tradable": True,
        "nxt_tradable_source": "kr_symbol_universe",
        "nxt_tradable_asof": _NOW.isoformat(),
        "nxt_tradable_stale": False,
    }
    # suspended overrides eligible
    assert _trad(True, True).nxt_tradable is False
