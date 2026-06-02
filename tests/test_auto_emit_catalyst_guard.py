"""ROB-408 Slice 2 — catalyst 가드 auto_emit 배선 테스트."""

import datetime as dt
from types import SimpleNamespace

import pytest

from app.services.action_report.snapshot_backed.auto_emit import (
    EvidenceAutoEmitter,
    _catalyst_events_for_symbol,
)

TODAY = dt.date(2026, 6, 2)
NOW = dt.datetime(2026, 6, 2, 10, 0)


# ---------------------------------------------------------------------------
# Task 1 fixtures
# ---------------------------------------------------------------------------


def _market_payload(events):
    return {"market": "kr", "events": events}


def _ev(symbol, category, date_str, title="t"):
    return {
        "symbol": symbol,
        "category": category,
        "event_date": date_str,
        "title": title,
        "source": "manual",
    }


# ---------------------------------------------------------------------------
# Task 1 tests: _catalyst_events_for_symbol
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filters_by_category_symbol_and_window():
    payload = _market_payload(
        [
            _ev("035420", "conference", "2026-06-05"),  # in window, catalyst
            _ev("035420", "earnings", "2026-06-05"),  # non-catalyst category
            _ev("005930", "conference", "2026-06-05"),  # other symbol
            _ev("035420", "lockup_expiry", "2026-06-30"),  # out of window (>7d)
        ]
    )
    out = _catalyst_events_for_symbol(payload, "035420", now_date=TODAY, within_days=7)
    assert len(out) == 1
    assert out[0].category == "conference"
    assert out[0].days_until == 3
    assert out[0].polarity == "positive"


@pytest.mark.unit
def test_empty_when_no_market_payload_or_no_events():
    assert _catalyst_events_for_symbol(None, "035420", now_date=TODAY, within_days=7) == []
    assert _catalyst_events_for_symbol({}, "035420", now_date=TODAY, within_days=7) == []


@pytest.mark.unit
def test_skips_malformed_events():
    payload = _market_payload(
        [
            {"symbol": "035420", "category": "conference"},  # missing event_date
            {
                "symbol": "035420",
                "category": "conference",
                "event_date": "not-a-date",
            },
            _ev("035420", "conference", "2026-06-03"),
        ]
    )
    out = _catalyst_events_for_symbol(payload, "035420", now_date=TODAY, within_days=7)
    assert [e.days_until for e in out] == [1]


# ---------------------------------------------------------------------------
# Task 2 fixtures
# ---------------------------------------------------------------------------


def _snap(kind, payload, *, symbol=None):
    return SimpleNamespace(
        snapshot_kind=kind,
        symbol=symbol,
        payload_json=payload,
        snapshot_uuid=None,
    )


def _portfolio(holdings_list):
    # _held_kis_symbols 요구: primary_source=="kis" + holdings=list[dict{ticker,sellable_quantity}]
    return _snap("portfolio", {"primary_source": "kis", "holdings": holdings_list})


def _quote(symbol):
    return _snap(
        "symbol",
        {
            "symbol": symbol,
            "quote": {
                "status": "ok",
                "best_bid": 1000,
                "best_ask": 1001,
                "bid_depth": 5,
                "ask_depth": 5,
                "spread_bps": 5,
            },
        },
        symbol=symbol,
    )


def _market(events):
    return _snap("market", {"market": "kr", "events": events})


def _cat(symbol, category, date_str):
    return {
        "symbol": symbol,
        "category": category,
        "event_date": date_str,
        "title": "t",
        "source": "manual",
    }


# ---------------------------------------------------------------------------
# Task 2 tests: propose() with catalyst guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sell_held_with_positive_catalyst_attaches_warning():
    holdings = [{"ticker": "035420", "sellable_quantity": 10}]
    snapshots = [
        _portfolio(holdings),
        _quote("035420"),
        _market([_cat("035420", "conference", "2026-06-05")]),  # +3d positive
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snapshots, request_market="kr", account_scope=None, now=NOW
    )
    sell = [i for i in items if i.symbol == "035420" and i.intent == "sell_review"]
    assert sell, "sell_review item expected"
    uc = sell[0].evidence_snapshot.get("upcoming_catalyst")
    assert uc is not None
    assert uc["flag"] == "upcoming_positive_catalyst"
    assert uc["nearest_days"] == 3
    # verdict 불변
    assert sell[0].side == "sell"
    assert sell[0].intent == "sell_review"


@pytest.mark.unit
def test_sell_without_catalyst_has_no_attachment():
    holdings = [{"ticker": "035420", "sellable_quantity": 10}]
    snapshots = [_portfolio(holdings), _quote("035420"), _market([])]
    items = EvidenceAutoEmitter().propose(
        snapshots=snapshots, request_market="kr", account_scope=None, now=NOW
    )
    sell = [i for i in items if i.symbol == "035420"]
    assert sell
    assert "upcoming_catalyst" not in sell[0].evidence_snapshot
