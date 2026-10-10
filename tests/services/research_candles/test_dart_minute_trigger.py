"""#1086 trigger: #1054 type mapping, listed/symbol filters, D0/D+1, enqueue."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.research_candles.dart_minute_trigger import (
    TARGET_COHORTS,
    DartRow,
    SymbolIndex,
    UniverseRow,
    classify_dart_title,
    parse_rcept_dt,
    plan_collection_requests,
    schedule_collections_for_dart_rows,
)

# Titles as they appear on the DART list page; expected = #1054 r3 cohort_of.
TITLE_CASES = [
    ("단일판매ㆍ공급계약체결", "SUPPLY_MAND"),
    ("단일판매ㆍ공급계약체결(자회사의 주요경영사항)", "SUPPLY_MAND"),
    ("단일판매ㆍ공급계약체결(자율공시)", "SUPPLY_VOL"),
    ("[기재정정]단일판매ㆍ공급계약체결", "AMEND"),
    ("단일판매ㆍ공급계약체결(정정)", "AMEND"),
    ("[첨부추가]단일판매ㆍ공급계약체결", "AMEND"),
    ("단일판매ㆍ공급계약해지", "SUPPLY_CANCEL"),
    ("주요사항보고서(자기주식취득결정)", "BUYBACK_DIRECT"),
    ("자기주식취득결정(자회사의 주요경영사항)", "BUYBACK_SUBSIDIARY"),
    ("자기주식취득신탁계약체결결정", "BUYBACK_TRUST_NEW"),
    ("자기주식취득신탁계약체결결정(종속회사의 주요경영사항)", "BUYBACK_SUBSIDIARY"),
    ("자기주식취득신탁계약해지결정", "BUYBACK_TRUST_END"),
    ("자기주식취득결과보고서", "BUYBACK_DONE"),
    ("주요사항보고서(자기주식소각결정)", "BUYBACK_CANCEL_SHARES"),
    ("영업(잠정)실적(공정공시)", "EARNINGS"),
    ("연결재무제표기준영업(잠정)실적(공정공시)", "EARNINGS"),
    ("매출액또는손익구조30%(대규모법인은15%)이상변동", "EARNINGS"),
    ("주요사항보고서(유상증자결정)", "RIGHTS"),
    ("유상증자결정(종속회사의주요경영사항)", "OTHER"),
    ("주요사항보고서(무상증자결정)", "OTHER"),
    ("임원ㆍ주요주주특정증권등소유상황보고서", "OTHER"),
]


@pytest.mark.parametrize(("title", "cohort"), TITLE_CASES)
def test_classifier_matches_1054_r3(title, cohort):
    assert classify_dart_title(title) == cohort


def test_target_cohorts_match_the_research_type_mapping():
    # Brief: supply contract mandatory + voluntary, buyback decision,
    # provisional earnings, rights issue. The research's buyback decision
    # cohorts are the direct decision (primary) and the trust-contract
    # decision (descriptive); subsidiary variants and amendments are unscored.
    assert TARGET_COHORTS == {
        "SUPPLY_MAND",
        "SUPPLY_VOL",
        "BUYBACK_DIRECT",
        "BUYBACK_TRUST_NEW",
        "EARNINGS",
        "RIGHTS",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-21 17:19", "2026-08-21T17:19:00"),
        ("2026-08-21T17:19:00", "2026-08-21T17:19:00"),
        ("2026-08-21 17:19:00", "2026-08-21T17:19:00"),
        ("20260821", None),
        ("", None),
    ],
)
def test_parse_rcept_dt(raw, expected):
    parsed = parse_rcept_dt(raw)
    assert (parsed.isoformat() if parsed else None) == expected


UNIVERSE = SymbolIndex(
    [
        UniverseRow("005930", "삼성전자", True),
        UniverseRow("005935", "삼성전자우", False),
        UniverseRow("375500", "DL이앤씨", None),
        UniverseRow("111111", "동명", True),
        UniverseRow("222222", "동명", True),
        UniverseRow("333333", "한국우", None),
    ]
)

# Fri 2026-09-25 -> next session Mon 2026-09-28 in this fake calendar.
_SESSIONS = [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)]


def _on_or_after(day):
    return next((d for d in _SESSIONS if d >= day), None)


def _next(day):
    return next((d for d in _SESSIONS if d > day), None)


def _row(
    rcept_no, title, name="삼성전자", cls="유", dt="2026-09-24 10:29", symbol=None
):
    return DartRow(
        rcept_no=rcept_no,
        title=title,
        company_name=name,
        corp_cls=cls,
        rcept_dt=dt,
        symbol=symbol,
    )


def _plan(rows, **kw):
    return plan_collection_requests(
        rows,
        symbol_index=UNIVERSE,
        session_on_or_after=_on_or_after,
        next_session=_next,
        **kw,
    )


def test_type_filter_listed_filter_and_symbol_mapping():
    plan = _plan(
        [
            _row("1", "단일판매ㆍ공급계약체결"),
            _row("2", "[기재정정]단일판매ㆍ공급계약체결"),
            _row("3", "임원ㆍ주요주주특정증권등소유상황보고서"),
            _row("4", "단일판매ㆍ공급계약체결", cls="기"),  # KONEX
            _row("5", "단일판매ㆍ공급계약체결", cls="E"),
            _row("6", "단일판매ㆍ공급계약체결", name="동명"),
            _row("7", "단일판매ㆍ공급계약체결", name="없는회사"),
            _row("8", "단일판매ㆍ공급계약체결", name="한국우"),
            _row("9", "단일판매ㆍ공급계약체결", dt="2026-09-24 00:00"),
            _row("10", "단일판매ㆍ공급계약체결", dt="20260924"),
            _row("11", "주요사항보고서(유상증자결정)", name="DL이앤씨"),
        ]
    )
    assert [(r.symbol, r.rcept_nos) for r in plan.requests] == [
        ("005930", ("1",)),
        ("375500", ("11",)),
    ]
    assert {(s.rcept_no, s.reason) for s in plan.skipped} == {
        ("2", "non_target_type"),
        ("3", "non_target_type"),
        ("4", "not_kospi_kosdaq"),
        ("5", "not_kospi_kosdaq"),
        ("6", "ambiguous_name"),
        ("7", "unmapped_name"),
        ("8", "unmapped_name"),
        ("9", "midnight_placeholder"),
        ("10", "no_rcept_time"),
    }


def test_d0_d1_for_intraday_after_hours_and_weekend_filings():
    plan = _plan(
        [
            _row("a", "단일판매ㆍ공급계약체결", dt="2026-09-24 10:29"),
            _row(
                "b", "영업(잠정)실적(공정공시)", dt="2026-09-25 17:15", name="DL이앤씨"
            ),
            _row(
                "c",
                "주요사항보고서(자기주식취득결정)",
                dt="2026-09-26 09:00",
                name="동명",
                symbol="444444",
            ),
        ]
    )
    got = {r.symbol: (r.d0, r.d1) for r in plan.requests}
    assert got["005930"] == (date(2026, 9, 24), date(2026, 9, 25))
    # after-hours Friday: D0 = Friday, D+1 = Monday (entry bar lives on D+1)
    assert got["375500"] == (date(2026, 9, 25), date(2026, 9, 28))
    # Saturday filing: D0 = Monday; an explicit stored symbol wins over the name
    assert got["444444"] == (date(2026, 9, 28), date(2026, 9, 29))


def test_same_symbol_same_d0_is_one_request_with_merged_provenance():
    plan = _plan(
        [
            _row("2", "단일판매ㆍ공급계약체결(자율공시)", dt="2026-09-24 14:00"),
            _row("1", "단일판매ㆍ공급계약체결", dt="2026-09-24 09:10"),
            _row("3", "단일판매ㆍ공급계약체결", dt="2026-09-25 09:10"),
        ]
    )
    assert [(r.d0, r.rcept_nos, r.cohorts) for r in plan.requests] == [
        (date(2026, 9, 24), ("1", "2"), ("SUPPLY_MAND", "SUPPLY_VOL")),
        (date(2026, 9, 25), ("3",), ("SUPPLY_MAND",)),
    ]


def test_cohort_subset_filter_and_unknown_cohort_rejected():
    rows = [
        _row("1", "단일판매ㆍ공급계약체결"),
        _row("2", "주요사항보고서(유상증자결정)", name="DL이앤씨"),
    ]
    plan = _plan(rows, cohorts=frozenset({"RIGHTS"}))
    assert [r.symbol for r in plan.requests] == ["375500"]
    with pytest.raises(ValueError):
        _plan(rows, cohorts=frozenset({"OTHER"}))


def test_no_session_is_skipped_not_guessed():
    plan = _plan([_row("1", "단일판매ㆍ공급계약체결", dt="2026-12-24 10:00")])
    assert plan.requests == []
    assert [s.reason for s in plan.skipped] == ["no_session"]


def test_from_market_event_reads_payload_time_and_class():
    event = SimpleNamespace(
        source_event_id="20260824800113",
        title="단일판매ㆍ공급계약체결",
        company_name="DL이앤씨",
        symbol=None,
        release_time_utc=None,
        raw_payload_json={"rcept_dt": "2026-08-24T10:29:00", "corp_cls": "유"},
    )
    row = DartRow.from_market_event(event)
    assert (row.rcept_no, row.corp_cls, row.rcept_dt) == (
        "20260824800113",
        "유",
        "2026-08-24T10:29:00",
    )


@pytest.mark.asyncio
async def test_schedule_hands_each_request_to_enqueue_only(monkeypatch):
    import app.services.research_candles.dart_minute_trigger as trig

    monkeypatch.setattr(trig, "first_session_on_or_after", _on_or_after)
    monkeypatch.setattr(trig, "next_trading_session", lambda market, d: _next(d))
    enqueued = []

    async def enqueue(request):
        enqueued.append(request)

    plan = await schedule_collections_for_dart_rows(
        [_row("1", "단일판매ㆍ공급계약체결"), _row("2", "기타")],
        symbol_index=UNIVERSE,
        enqueue=enqueue,
    )
    assert enqueued == plan.requests
    assert [r.symbol for r in enqueued] == ["005930"]


def test_real_xkrx_calendar_matches_the_1054_samsung_case():
    # 20260821000616: Fri 2026-08-21 17:19 buyback, research entry 2026-08-24T09:05.
    plan = plan_collection_requests(
        [
            _row(
                "20260821000616",
                "주요사항보고서(자기주식취득결정)",
                dt="2026-08-21 17:19",
            )
        ],
        symbol_index=UNIVERSE,
    )
    (request,) = plan.requests
    assert (request.symbol, request.d0, request.d1) == (
        "005930",
        date(2026, 8, 21),
        date(2026, 8, 24),
    )
