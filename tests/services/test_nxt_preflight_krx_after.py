"""#969 — the Toss order-tool NXT preflight knows the KRX after-market.

Rule under test (same as the #925 approval window): in the after-hours
session a non-NXT name is allowed exactly when the time is inside the Toss
integrated after window clipped to 16:00-20:00 KST AND the #925 resolver
proves it KRX after-market tradable. 08:00-08:50 stays NXT-only. Unknown,
missing, unreadable or stale lists never allow and carry their own reason.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling import account_routing_tools as art
from app.mcp_server.tooling import orders_toss_variants as otv
from app.services import nxt_preflight_krx_after as krx_pf
from app.services.brokers.toss.market_calendar import (
    kr_toss_session_for,
    parse_kr_market_calendar,
)
from app.services.krx_after_market import KrxAfterTradability
from app.services.nxt_preflight import (
    RETRY_AT_REGULAR,
    ROUTE_VIA_KIS,
    KrxAfterEvidence,
    NxtTradability,
    evaluate_nxt_preflight,
)
from app.services.order_proposals import approval_window as policy

KST = policy._KST
DAY = datetime(2026, 7, 23, tzinfo=KST)
SYMBOL = "375500"


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


def _nxt(eligible: bool, *, suspended: bool | None = False) -> NxtTradability:
    return NxtTradability(
        nxt_eligible=eligible, nxt_trading_suspended=suspended, asof=_at(7)
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


LISTED = _krx()
UNLISTED = _krx(listed=False)
STALE = _krx(asof=datetime(2026, 7, 1, tzinfo=KST))
LIST_EMPTY = _krx(listed=None, asof=None, list_source=None)
ETF_LISTED = _krx(security_type="ETF")
LOOKUP_ERROR = RuntimeError('relation "krx_after_market_eligibility" missing')


class _Fakes:
    """One set of fakes shared by the approval window and both preflight callers."""

    def __init__(self, monkeypatch, *, nxt=None, krx=None, calendar=None):
        self.krx_calls = 0
        self.nxt = nxt
        self.krx = krx
        self.calendar = calendar if calendar is not None else _calendar()

        async def calendar_reader(market, query_date):
            return self.calendar

        async def nxt_reader(symbols, db=None):
            if isinstance(self.nxt, Exception):
                raise self.nxt
            return {} if self.nxt is None else {symbols[0]: self.nxt}

        async def krx_reader(symbols, db=None):
            self.krx_calls += 1
            if isinstance(self.krx, Exception):
                raise self.krx
            return {} if self.krx is None else {symbols[0]: self.krx}

        async def session_reader(moment):
            return kr_toss_session_for(moment, calendar=self.calendar) or "closed"

        monkeypatch.setattr(policy, "get_toss_market_calendar", calendar_reader)
        monkeypatch.setattr(policy, "get_kr_nxt_tradability", nxt_reader)
        monkeypatch.setattr(policy, "get_kr_krx_after_tradability", krx_reader)
        monkeypatch.setattr(krx_pf, "get_toss_market_calendar", calendar_reader)
        monkeypatch.setattr(otv, "get_kr_toss_session_from_toss", session_reader)
        monkeypatch.setattr(otv, "get_kr_nxt_tradability", nxt_reader)
        monkeypatch.setattr(art, "get_kr_toss_session_from_toss", session_reader)
        monkeypatch.setattr(art, "get_kr_nxt_tradability", nxt_reader)
        self.session_reader = session_reader


async def _toss_verdict(fakes: _Fakes, at: datetime, symbol: str = SYMBOL):
    context = await otv._nxt_preflight_context(symbol, "kr", now=at)
    assert context is not None
    return context[0]


@pytest.fixture
def _warn_mode(monkeypatch):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "warn", raising=False)


# --- pure rule --------------------------------------------------------------


@pytest.mark.unit
def test_pure_rule_allows_only_proven_krx_evidence_in_window():
    allowed = evaluate_nxt_preflight(
        "nxt_after",
        _nxt(False),
        KrxAfterEvidence(in_window=True, allow=True, detail="krx_after_tradable"),
    )
    assert allowed.block is False
    assert allowed.reason == "krx_after_tradable"
    assert allowed.session == "krx_after"
    assert allowed.alternatives == ()
    assert allowed.to_dict()["krx_after_detail"] == "krx_after_tradable"

    outside = evaluate_nxt_preflight(
        "nxt_after",
        _nxt(False),
        KrxAfterEvidence(in_window=False, allow=True, detail="krx_after_tradable"),
    )
    assert outside.block is True
    assert outside.reason == "not_nxt_eligible"
    assert "krx_after_detail" not in outside.to_dict()

    premarket = evaluate_nxt_preflight(
        "nxt_premarket",
        _nxt(False),
        KrxAfterEvidence(in_window=True, allow=True, detail="krx_after_tradable"),
    )
    assert premarket.block is True
    assert premarket.reason == "not_nxt_eligible"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ("krx_after_capability_lookup_failed", "krx_after_capability_unknown"),
        ("krx_after_capability_unavailable", "krx_after_capability_unknown"),
        ("krx_after_capability_list_missing", "krx_after_capability_unknown"),
        ("krx_after_capability_stale", "krx_after_capability_stale"),
        ("not_krx_after_listed", "not_nxt_eligible"),
        ("etf_etn_excluded", "not_nxt_eligible"),
    ],
)
def test_pure_rule_reason_mapping(detail, reason):
    verdict = evaluate_nxt_preflight(
        "nxt_after",
        _nxt(False),
        KrxAfterEvidence(in_window=True, allow=False, detail=detail),
    )
    assert verdict.block is True
    assert verdict.reason == reason
    assert verdict.session == "nxt_after"
    assert verdict.alternatives == (RETRY_AT_REGULAR, ROUTE_VIA_KIS)
    assert verdict.to_dict()["krx_after_detail"] == detail


@pytest.mark.unit
def test_pure_rule_keeps_suspended_reason_for_a_known_ineligible_list():
    verdict = evaluate_nxt_preflight(
        "nxt_after",
        _nxt(True, suspended=True),
        KrxAfterEvidence(in_window=True, allow=False, detail="not_krx_after_listed"),
    )
    assert verdict.block is True
    assert verdict.reason == "nxt_trading_suspended"


# --- boundaries through the Toss caller ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("at", "block", "reason", "session"),
    [
        (_at(15, 59, 59, 999999), True, "not_nxt_eligible", "nxt_after"),
        (_at(16, 0), False, "krx_after_tradable", "krx_after"),
        (_at(19, 59, 59, 999999), False, "krx_after_tradable", "krx_after"),
        # 20:00 is outside every after window: the preflight does not judge a
        # closed session (unchanged); the approval window defers it.
        (_at(20, 0), False, None, "closed"),
        (_at(8, 0), True, "not_nxt_eligible", "nxt_premarket"),
        (_at(8, 49, 59, 999999), True, "not_nxt_eligible", "nxt_premarket"),
        (_at(8, 50), False, None, "closed"),
    ],
)
async def test_krx_listed_name_boundaries(
    monkeypatch, _warn_mode, at, block, reason, session
):
    fakes = _Fakes(monkeypatch, nxt=_nxt(False), krx=LISTED)
    verdict = await _toss_verdict(fakes, at)
    assert verdict.block is block
    assert verdict.reason == reason
    assert verdict.session == session


@pytest.mark.asyncio
async def test_krx_window_is_calendar_based_not_clock_based(monkeypatch, _warn_mode):
    """An integrated after window ending 18:00 closes the KRX window at 18:00."""
    fakes = _Fakes(monkeypatch, nxt=_nxt(False), krx=LISTED)
    fakes.calendar = _calendar()
    short = parse_kr_market_calendar(
        {
            "today": {
                "date": "2026-07-23",
                "integrated": {
                    "regularMarket": _w(
                        "2026-07-23T09:00:00+09:00", "2026-07-23T15:30:00+09:00"
                    ),
                    "afterMarket": _w(
                        "2026-07-23T15:30:00+09:00", "2026-07-23T18:00:00+09:00"
                    ),
                },
            }
        }
    )
    fakes.calendar = short
    assert (await _toss_verdict(fakes, _at(17, 59, 59))).block is False
    # 18:00-20:00: no after window at all -> closed, not a KRX allow.
    late = await _toss_verdict(fakes, _at(18, 30))
    assert late.session == "closed"
    evidence = await krx_pf.resolve_krx_after_evidence(SYMBOL, now=_at(18, 30))
    assert evidence.in_window is False


@pytest.mark.asyncio
async def test_no_krx_window_on_a_day_without_after_market(monkeypatch):
    fakes = _Fakes(monkeypatch, nxt=_nxt(False), krx=LISTED)
    fakes.calendar = parse_kr_market_calendar(
        {
            "today": {
                "date": "2026-07-23",
                "integrated": {
                    "regularMarket": _w(
                        "2026-07-23T09:00:00+09:00", "2026-07-23T15:30:00+09:00"
                    ),
                },
            }
        }
    )
    evidence = await krx_pf.resolve_krx_after_evidence(SYMBOL, now=_at(17, 0))
    assert evidence.in_window is False
    assert evidence.allow is False
    assert fakes.krx_calls == 0


# --- list states (fail-closed) ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("krx", "block", "reason", "detail"),
    [
        (LISTED, False, "krx_after_tradable", "krx_after_tradable"),
        (UNLISTED, True, "not_nxt_eligible", "not_krx_after_listed"),
        (ETF_LISTED, True, "not_nxt_eligible", "etf_etn_excluded"),
        (STALE, True, "krx_after_capability_stale", "krx_after_capability_stale"),
        (
            LIST_EMPTY,
            True,
            "krx_after_capability_unknown",
            "krx_after_capability_list_missing",
        ),
        (
            None,
            True,
            "krx_after_capability_unknown",
            "krx_after_capability_unavailable",
        ),
        (
            LOOKUP_ERROR,
            True,
            "krx_after_capability_unknown",
            "krx_after_capability_lookup_failed",
        ),
    ],
)
async def test_list_states_in_the_krx_window(
    monkeypatch, _warn_mode, krx, block, reason, detail
):
    fakes = _Fakes(monkeypatch, nxt=_nxt(False), krx=krx)
    for at in (_at(16, 0), _at(17, 30), _at(19, 59, 59, 999999)):
        verdict = await _toss_verdict(fakes, at)
        assert verdict.block is block
        assert verdict.reason == reason
        assert verdict.krx_after_detail == detail
        if block:
            assert verdict.alternatives == (RETRY_AT_REGULAR, ROUTE_VIA_KIS)


@pytest.mark.asyncio
async def test_symbol_missing_from_nxt_universe_still_uses_the_krx_list(
    monkeypatch, _warn_mode
):
    """No NXT row reads as not NXT-tradable (unchanged), then the KRX rule."""
    fakes = _Fakes(monkeypatch, nxt=None, krx=LISTED)
    assert (await _toss_verdict(fakes, _at(17, 0))).block is False
    fakes.krx = LIST_EMPTY
    blocked = await _toss_verdict(fakes, _at(17, 0))
    assert blocked.reason == "krx_after_capability_unknown"


@pytest.mark.asyncio
async def test_resolver_crash_never_reaches_the_callers_fail_open(
    monkeypatch, _warn_mode
):
    """A raising capability resolver must block, not return None (= skip)."""
    _Fakes(monkeypatch, nxt=_nxt(False), krx=LISTED)

    async def _boom(symbol, *, now):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(krx_pf, "resolve_krx_after_capability", _boom)
    escaped: BaseException | None = None
    context = None
    try:
        context = await otv._nxt_preflight_context(SYMBOL, "kr", now=_at(17, 0))
    except Exception as exc:  # noqa: BLE001 - the assertion below is the check
        escaped = exc
    assert escaped is None, f"resolver crash escaped the preflight: {escaped!r}"
    assert context is not None
    verdict = context[0]
    assert verdict.block is True
    assert verdict.reason == "krx_after_capability_unknown"


@pytest.mark.asyncio
async def test_calendar_crash_keeps_the_nxt_block(monkeypatch, _warn_mode):
    fakes = _Fakes(monkeypatch, nxt=_nxt(False), krx=LISTED)

    async def _boom(market, query_date):
        raise RuntimeError("calendar exploded")

    monkeypatch.setattr(krx_pf, "get_toss_market_calendar", _boom)
    verdict = await _toss_verdict(fakes, _at(17, 0))
    assert verdict.block is True
    assert verdict.reason == "not_nxt_eligible"
    assert fakes.krx_calls == 0


# --- NXT names unchanged ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "at", [_at(8, 0), _at(8, 30), _at(10, 0), _at(15, 45), _at(16, 0), _at(19, 30)]
)
async def test_nxt_names_unchanged_and_skip_the_krx_lookup(monkeypatch, _warn_mode, at):
    fakes = _Fakes(monkeypatch, nxt=_nxt(True), krx=LOOKUP_ERROR)
    verdict = await _toss_verdict(fakes, at, symbol="005930")
    assert verdict.block is False
    assert verdict.reason is None
    assert verdict.session in {"nxt_premarket", "regular", "nxt_after"}
    assert verdict.to_dict() == {
        "block": False,
        "reason": None,
        "session": verdict.session,
        "alternatives": [],
        "advisory": False,
    }
    assert fakes.krx_calls == 0


@pytest.mark.asyncio
async def test_nxt_suspended_outside_the_krx_window_unchanged(monkeypatch, _warn_mode):
    fakes = _Fakes(monkeypatch, nxt=_nxt(True, suspended=True), krx=LISTED)
    verdict = await _toss_verdict(fakes, _at(8, 10))
    assert verdict.reason == "nxt_trading_suspended"
    assert verdict.to_dict() == {
        "block": True,
        "reason": "nxt_trading_suspended",
        "session": "nxt_premarket",
        "alternatives": [RETRY_AT_REGULAR, ROUTE_VIA_KIS],
        "advisory": False,
    }
    assert fakes.krx_calls == 0


# --- parity with the approval window -----------------------------------------


_GRID_TIMES = [
    _at(8, 0),
    _at(8, 30),
    _at(8, 49, 59, 999999),
    _at(15, 30),
    _at(15, 45),
    _at(15, 59, 59, 999999),
    _at(16, 0),
    _at(17, 0),
    _at(19, 59, 59, 999999),
]
_GRID_CAPS = [
    ("nxt_true", _nxt(True), LISTED),
    ("nxt_true_krx_unknown", _nxt(True), LOOKUP_ERROR),
    ("nxt_false_listed", _nxt(False), LISTED),
    ("nxt_false_unlisted", _nxt(False), UNLISTED),
    ("nxt_false_stale", _nxt(False), STALE),
    ("nxt_false_list_empty", _nxt(False), LIST_EMPTY),
    ("nxt_false_missing", _nxt(False), None),
    ("nxt_false_lookup_error", _nxt(False), LOOKUP_ERROR),
    ("nxt_false_etf", _nxt(False), ETF_LISTED),
    ("nxt_suspended_listed", _nxt(True, suspended=True), LISTED),
    ("nxt_suspended_unlisted", _nxt(True, suspended=True), UNLISTED),
    ("nxt_absent_listed", None, LISTED),
    ("nxt_absent_list_empty", None, LIST_EMPTY),
]


def _group(symbol: str = SYMBOL):
    return SimpleNamespace(
        market="equity_kr",
        account_mode="toss_live",
        symbol=symbol,
        valid_until=datetime(2026, 7, 24, 20, tzinfo=KST),
        action="place",
        order_type="limit",
        exit_intent=None,
        exit_reason=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("at", _GRID_TIMES)
@pytest.mark.parametrize(("label", "nxt", "krx"), _GRID_CAPS)
async def test_parity_with_approval_window_in_off_hours_sessions(
    monkeypatch, _warn_mode, at, label, nxt, krx
):
    """In every NXT/after-hours slice the preflight allows iff the window does."""
    _Fakes(monkeypatch, nxt=nxt, krx=krx)
    evidence = await policy.resolve_submission_session(_group(), now=at)
    verdict = await _toss_verdict(None, at)
    assert (not verdict.block) is evidence.allowed_now, (label, at, verdict, evidence)
    if evidence.allowed_now and evidence.current_session == "krx_after":
        assert verdict.session == "krx_after"


# --- both callers return the same verdict ------------------------------------


@pytest.fixture
def _routing_fakes(monkeypatch):
    async def _fake_resolve_price(symbol, market, price):
        return 70000.0, "test"

    async def _fake_capital(*, include_manual=False):
        return {}

    async def _fake_holdings(*, market, include_current_price, minimum_value):
        return []

    async def _fake_user_setting(_key):
        return {}

    monkeypatch.setattr(art, "_resolve_price", _fake_resolve_price)
    monkeypatch.setattr(art, "get_available_capital_impl", _fake_capital)
    monkeypatch.setattr(art, "_get_holdings_impl", _fake_holdings)
    monkeypatch.setattr(art, "get_user_setting", _fake_user_setting)
    monkeypatch.setattr(
        art, "suggest_account_from_snapshot", lambda _inp: {"account_mode": "toss_live"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "at", [_at(8, 0), _at(15, 45), _at(16, 0), _at(17, 0), _at(19, 59, 59, 999999)]
)
@pytest.mark.parametrize(("label", "nxt", "krx"), _GRID_CAPS)
async def test_both_callers_return_the_same_verdict(
    monkeypatch, _warn_mode, _routing_fakes, at, label, nxt, krx
):
    _Fakes(monkeypatch, nxt=nxt, krx=krx)
    monkeypatch.setattr(art, "now_kst", lambda: at)
    toss = (await _toss_verdict(None, at)).to_dict()
    routed = await art.suggest_order_account_impl(
        symbol=SYMBOL, market="kr", side="buy", quantity=1
    )
    assert routed["nxt_preflight"] == toss, label


# --- mode handling on the Toss order tools -----------------------------------


def _neutralize_preview(monkeypatch):
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [], raising=True)

    async def _no_price_ctx(client, symbol):
        return None, None, None

    class _Guard:
        ok = True
        warnings: list = []
        error_message = None

    async def _no_warnings(client, symbol, *, market, side):
        return _Guard()

    monkeypatch.setattr(otv, "_preview_price_context", _no_price_ctx)
    monkeypatch.setattr(otv, "check_warnings_guard", _no_warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["warn", "optional", "required"])
@pytest.mark.parametrize(
    ("krx", "warned", "reason"),
    [
        (LISTED, False, "krx_after_tradable"),
        (LIST_EMPTY, True, "krx_after_capability_unknown"),
        (STALE, True, "krx_after_capability_stale"),
    ],
)
async def test_preview_annotates_by_verdict_in_every_mode(
    monkeypatch, mode, krx, warned, reason
):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", mode, raising=False)
    _Fakes(monkeypatch, nxt=_nxt(False), krx=krx)
    _neutralize_preview(monkeypatch)
    monkeypatch.setattr(otv, "now_kst", lambda: _at(17, 0))
    res = await otv.toss_preview_order(
        symbol=SYMBOL, side="buy", order_type="market", quantity=1
    )
    assert res["success"] is True
    assert ("nxt_session_not_tradable" in res["order_warnings"]) is warned
    assert res["nxt_preflight"]["reason"] == reason
    assert res["nxt_preflight"]["block"] is warned


@pytest.mark.asyncio
async def test_preview_skips_preflight_when_mode_off(monkeypatch):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "off", raising=False)
    fakes = _Fakes(monkeypatch, nxt=_nxt(False), krx=LIST_EMPTY)
    _neutralize_preview(monkeypatch)
    monkeypatch.setattr(otv, "now_kst", lambda: _at(17, 0))
    res = await otv.toss_preview_order(
        symbol=SYMBOL, side="buy", order_type="market", quantity=1
    )
    assert res["nxt_preflight"] is None
    assert fakes.krx_calls == 0


def _place_harness(monkeypatch):
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [], raising=True)
    monkeypatch.setattr(
        otv.settings, "toss_live_order_mutations_enabled", True, raising=False
    )
    placed = {"called": False}

    class _Client:
        async def place_order(self, payload):
            placed["called"] = True
            raise RuntimeError("fake broker: nothing is sent in tests")

    async def _no_warnings(client, symbol, *, market, side):
        class _G:
            ok = True
            warnings: list = []
            error_message = None

        return _G()

    async def _no_opp(client, symbol, side, base):
        return None

    monkeypatch.setattr(otv, "check_warnings_guard", _no_warnings)
    monkeypatch.setattr(otv, "_opposite_pending_error", _no_opp)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        yield _Client()

    monkeypatch.setattr(otv, "_client_context", _ctx)
    monkeypatch.setattr(otv, "now_kst", lambda: _at(17, 0))
    return placed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("krx", "reason"),
    [
        (LIST_EMPTY, "krx_after_capability_unknown"),
        (STALE, "krx_after_capability_stale"),
        (LOOKUP_ERROR, "krx_after_capability_unknown"),
        (UNLISTED, "not_nxt_eligible"),
    ],
)
async def test_required_mode_place_blocks_unproven_krx_names(monkeypatch, krx, reason):
    monkeypatch.setattr(
        otv.settings, "toss_nxt_preflight_mode", "required", raising=False
    )
    _Fakes(monkeypatch, nxt=_nxt(False), krx=krx)
    placed = _place_harness(monkeypatch)
    res = await otv.toss_place_order(
        symbol=SYMBOL,
        side="buy",
        order_type="market",
        quantity=1,
        dry_run=False,
        confirm=True,
    )
    assert res["success"] is False
    assert res["error_code"] == "nxt_session_not_tradable"
    assert f"({reason})" in res["error"]
    assert res["session"] == "nxt_after"
    assert placed["called"] is False


@pytest.mark.asyncio
async def test_required_mode_place_passes_the_preflight_for_a_listed_name(
    monkeypatch,
):
    """The preflight no longer stops a proven KRX name; the next gate runs."""
    monkeypatch.setattr(
        otv.settings, "toss_nxt_preflight_mode", "required", raising=False
    )
    _Fakes(monkeypatch, nxt=_nxt(False), krx=LISTED)
    placed = _place_harness(monkeypatch)
    res = await otv.toss_place_order(
        symbol=SYMBOL,
        side="buy",
        order_type="market",
        quantity=1,
        dry_run=False,
        confirm=True,
    )
    # The fake broker was reached (and raised): the preflight let it through.
    assert placed["called"] is True
    assert res.get("error_code") != "nxt_session_not_tradable", res
