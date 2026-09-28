"""#925 — approval window generalized to session x venue (KRX after-market)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.brokers.toss.market_calendar import parse_kr_market_calendar
from app.services.krx_after_market import KrxAfterTradability
from app.services.nxt_preflight import NxtTradability
from app.services.order_proposals import approval_window as policy
from app.services.order_proposals.approval_window import (
    ApprovalWindowCode,
    evaluate_approval_window,
    recheck_approval_window_decision,
)

KST = policy._KST
DAY = datetime(2026, 7, 23, tzinfo=KST)


def _at(hour: int, minute: int = 0, second: int = 0, micro: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=second, microsecond=micro)


def _w(start: str, end: str) -> dict[str, str]:
    return {"startTime": start, "endTime": end}


def _calendar(after_start: str = "15:30"):
    """Production-shaped Toss integrated calendar (NXT after from 15:30)."""

    def day(date: str) -> dict[str, object]:
        return {
            "date": date,
            "integrated": {
                "preMarket": _w(f"{date}T08:00:00+09:00", f"{date}T08:50:00+09:00"),
                "regularMarket": _w(f"{date}T09:00:00+09:00", f"{date}T15:30:00+09:00"),
                "afterMarket": _w(
                    f"{date}T{after_start}:00+09:00", f"{date}T20:00:00+09:00"
                ),
            },
        }

    return parse_kr_market_calendar(
        {"today": day("2026-07-23"), "nextBusinessDay": day("2026-07-24")}
    )


def _nxt(eligible: bool, *, fresh: bool = True) -> NxtTradability:
    return NxtTradability(
        nxt_eligible=eligible,
        nxt_trading_suspended=False,
        asof=_at(7) if fresh else datetime(2026, 7, 20, tzinfo=KST),
    )


def _krx(**overrides) -> KrxAfterTradability:
    fields = {
        "listed": True,
        "exchange": "KOSPI",
        "security_type": "STOCK",
        "krx_trading_suspended": False,
        "asof": _at(7),
        "list_source": "krx-notice-test",
    }
    fields.update(overrides)
    return KrxAfterTradability(**fields)


def _group(symbol: str = "375500", valid_until: datetime | None = None):
    return SimpleNamespace(
        market="equity_kr",
        account_mode="toss_live",
        symbol=symbol,
        valid_until=valid_until or datetime(2026, 7, 24, 20, tzinfo=KST),
        action="place",
        order_type="limit",
        exit_intent=None,
        exit_reason=None,
    )


class _Fakes:
    def __init__(self, monkeypatch, *, nxt=None, krx=None, calendar=None):
        self.krx_calls = 0
        self._nxt = nxt
        self._krx = krx
        cal = calendar if calendar is not None else _calendar()

        async def calendar_reader(market, query_date):
            return cal

        async def nxt_reader(symbols):
            if isinstance(self._nxt, Exception):
                raise self._nxt
            return {} if self._nxt is None else {symbols[0]: self._nxt}

        async def krx_reader(symbols):
            self.krx_calls += 1
            if isinstance(self._krx, Exception):
                raise self._krx
            return {} if self._krx is None else {symbols[0]: self._krx}

        monkeypatch.setattr(policy, "get_toss_market_calendar", calendar_reader)
        monkeypatch.setattr(policy, "get_kr_nxt_tradability", nxt_reader)
        monkeypatch.setattr(policy, "get_kr_krx_after_tradability", krx_reader)


async def _resolve(at: datetime, symbol: str = "375500"):
    return await policy.resolve_submission_session(_group(symbol), now=at)


# --- KRX after-market only (NXT known false, KRX list true) -----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("at", "allowed"),
    [
        (_at(15, 59, 59, 999999), False),
        (_at(16, 0), True),
        (_at(19, 59, 59, 999999), True),
        (_at(20, 0), False),
    ],
)
async def test_krx_only_after_window_boundaries(monkeypatch, at, allowed):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx())
    evidence = await _resolve(at)
    assert evidence.known is True
    assert evidence.allowed_now is allowed
    assert evidence.allowed_sessions == ("regular", "krx_after")
    if allowed:
        assert evidence.current_session == "krx_after"
        assert evidence.allowed_until == _at(20, 0)
        assert evidence.detail == "krx_after_tradable"
        assert evidence.source.endswith("+krx_after_list")


@pytest.mark.asyncio
async def test_krx_only_before_16_defers_to_16_not_the_nxt_1530_window(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx())
    evidence = await _resolve(_at(15, 45))
    assert evidence.current_session == "nxt_after"
    assert evidence.allowed_now is False
    assert evidence.next_allowed_at == _at(16, 0)
    decision = await evaluate_approval_window(_group(), now=_at(15, 45))
    assert decision.code is ApprovalWindowCode.DEFER_SESSION_CLOSED


@pytest.mark.asyncio
async def test_krx_only_after_20_defers_to_next_regular_open(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx())
    evidence = await _resolve(_at(20, 0))
    assert evidence.next_allowed_at == datetime(2026, 7, 24, 9, 0, tzinfo=KST)


@pytest.mark.asyncio
@pytest.mark.parametrize("at", [_at(8, 0), _at(8, 10), _at(8, 49, 59, 999999)])
async def test_morning_stays_nxt_only_for_krx_after_names(monkeypatch, at):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx())
    evidence = await _resolve(at)
    assert evidence.current_session == "nxt_premarket"
    assert evidence.allowed_now is False
    assert evidence.next_allowed_at == _at(9, 0)
    decision = await evaluate_approval_window(_group(), now=at)
    assert decision.code is ApprovalWindowCode.DEFER_SESSION_CLOSED


@pytest.mark.asyncio
async def test_0850_gap_is_closed_for_krx_after_names(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx())
    evidence = await _resolve(_at(8, 50))
    assert evidence.current_session == "closed"
    assert evidence.allowed_now is False
    assert evidence.next_allowed_at == _at(9, 0)


@pytest.mark.asyncio
async def test_krx_only_regular_session_advertises_krx_after_next(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx())
    evidence = await _resolve(_at(10, 0))
    assert evidence.allowed_now is True
    assert evidence.allowed_sessions == ("regular", "krx_after")
    assert evidence.allowed_until == _at(15, 30)
    assert evidence.next_allowed_at == _at(16, 0)


# --- fail-closed: ETF, unknown eligibility, stale lists ---------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("krx", "detail"),
    [
        (_krx(security_type="ETF"), "not_nxt_eligible"),
        (_krx(security_type="ETN"), "not_nxt_eligible"),
        (_krx(security_type=None), "not_nxt_eligible"),
        (_krx(listed=False), "not_nxt_eligible"),
        (_krx(listed=None, asof=None), "not_nxt_eligible"),
        (_krx(asof=datetime(2026, 7, 1, tzinfo=KST)), "not_nxt_eligible"),
        (_krx(krx_trading_suspended=None), "not_nxt_eligible"),
        (_krx(krx_trading_suspended=True), "not_nxt_eligible"),
        (None, "not_nxt_eligible"),
        (RuntimeError("relation does not exist"), "not_nxt_eligible"),
    ],
)
async def test_after_hours_blocked_without_positive_krx_evidence(
    monkeypatch, krx, detail
):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=krx)
    for at in (_at(16, 0), _at(17, 0), _at(19, 59, 59)):
        evidence = await _resolve(at)
        assert evidence.allowed_now is False
        assert evidence.allowed_sessions == ("regular",)
        assert evidence.detail == detail
        decision = await evaluate_approval_window(_group(), now=at)
        assert decision.code is ApprovalWindowCode.DEFER_SESSION_CLOSED
        assert decision.evidence.next_allowed_at == datetime(
            2026, 7, 24, 9, 0, tzinfo=KST
        )


@pytest.mark.asyncio
async def test_etf_in_regular_session_keeps_regular_only_stamp(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx(security_type="ETF"))
    evidence = await _resolve(_at(10, 0))
    assert evidence.allowed_sessions == ("regular",)
    assert evidence.next_allowed_at == datetime(2026, 7, 24, 9, 0, tzinfo=KST)


@pytest.mark.asyncio
async def test_nxt_unknown_and_krx_unknown_stays_calendar_unknown(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(True, fresh=False), krx=None)
    decision = await evaluate_approval_window(_group(), now=_at(17, 0))
    assert decision.code is ApprovalWindowCode.CALENDAR_UNKNOWN
    assert decision.detail == "nxt_capability_stale"


@pytest.mark.asyncio
async def test_nxt_unknown_but_krx_proven_allows_only_the_krx_window(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(True, fresh=False), krx=_krx())
    allowed = await evaluate_approval_window(_group(), now=_at(17, 0))
    assert allowed.code is ApprovalWindowCode.ALLOW
    assert allowed.evidence.current_session == "krx_after"
    # Outside the KRX venue window the NXT uncertainty still fails closed.
    for at in (_at(15, 45), _at(8, 10)):
        decision = await evaluate_approval_window(_group(), now=at)
        assert decision.code is ApprovalWindowCode.CALENDAR_UNKNOWN


# --- NXT names are unchanged ------------------------------------------------


@pytest.mark.asyncio
async def test_nxt_names_unchanged_and_skip_the_krx_lookup(monkeypatch):
    fakes = _Fakes(monkeypatch, nxt=_nxt(True), krx=_krx(security_type="ETF"))
    for at in (_at(8, 0), _at(10, 0), _at(15, 45), _at(17, 0)):
        evidence = await _resolve(at, symbol="005930")
        assert evidence.allowed_now is True
        assert evidence.allowed_sessions == (
            "nxt_premarket",
            "regular",
            "nxt_after",
        )
        assert evidence.current_session in {"nxt_premarket", "regular", "nxt_after"}
        assert "+krx_after_list" not in evidence.source
    after = await _resolve(_at(17, 0), symbol="005930")
    assert after.allowed_until == _at(20, 0)
    assert fakes.krx_calls == 0


@pytest.mark.asyncio
async def test_policy_stamp_distinguishes_krx_after_capability(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx())
    with_krx = await evaluate_approval_window(_group(), now=_at(10, 0))
    _Fakes(monkeypatch, nxt=_nxt(False), krx=None)
    without_krx = await evaluate_approval_window(_group(), now=_at(10, 0))
    assert with_krx.policy_stamp != without_krx.policy_stamp


@pytest.mark.asyncio
async def test_recheck_closes_the_krx_window_exactly_at_20(monkeypatch):
    _Fakes(monkeypatch, nxt=_nxt(False), krx=_krx())
    group = _group()
    decision = await evaluate_approval_window(group, now=_at(19, 59, 59))
    assert decision.code is ApprovalWindowCode.ALLOW
    still = recheck_approval_window_decision(
        group, decision, now=_at(19, 59, 59, 999999)
    )
    assert still.code is ApprovalWindowCode.ALLOW
    closed = recheck_approval_window_decision(group, decision, now=_at(20, 0))
    assert closed.code is ApprovalWindowCode.DEFER_SESSION_CLOSED

    short_group = _group(valid_until=_at(20, 0) + timedelta(hours=1))
    short = await evaluate_approval_window(short_group, now=_at(19, 59, 59))
    expired_window = recheck_approval_window_decision(
        short_group, short, now=_at(20, 0)
    )
    assert expired_window.code is ApprovalWindowCode.NO_EXECUTABLE_WINDOW
