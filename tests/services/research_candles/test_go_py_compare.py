"""hk 6345: Go JSONL ↔ research 표 느슨한 전환 비교의 순수 판정 테스트."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.research_candles import go_py_compare as cmp

pytestmark = pytest.mark.unit

KR_DAY = date(2026, 11, 24)

# fillwire internal/marketminute/testdata/bars/kr_accumulated_difference.golden.jsonl
# 첫 줄을 그대로 옮긴 것(origin/main). 파서가 실제 Go 바이트 형식을 읽는지 고정한다.
GO_GOLDEN_LINE = (
    '{"time_utc":"2026-11-24T00:00:00Z","session_date":"2026-11-24",'
    '"symbol":"005930","venue":"KRX","session_segment":"KRX_REGULAR",'
    '"source":"KIS","open":"100","high":"101","low":"99","close":"100",'
    '"volume":"10","value":"100"}'
)


def _start(minute: int, session_date: date = KR_DAY) -> datetime:
    base = datetime(session_date.year, session_date.month, session_date.day, tzinfo=UTC)
    return base + timedelta(minutes=minute)


def _bar(
    minute: int,
    *,
    symbol: str = "005930",
    venue: str = "KRX",
    session_date: date = KR_DAY,
    segment: str = "KRX_REGULAR",
    source: str = "KIS",
    close: str = "100",
    value: str | None = "100",
) -> cmp.MinuteBar:
    return cmp.MinuteBar(
        time_utc=_start(minute, session_date),
        session_date=session_date,
        symbol=symbol,
        venue=venue,
        session_segment=segment,
        source=source,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=Decimal("10"),
        value=None if value is None else Decimal(value),
    )


def _session(
    symbol: str = "005930",
    venue: str = "KRX",
    minutes: int = 30,
    session_date: date = KR_DAY,
) -> list[cmp.MinuteBar]:
    return [
        _bar(m, symbol=symbol, venue=venue, session_date=session_date)
        for m in range(minutes)
    ]


# ---- acceptance 1: verdict semantics --------------------------------------


def test_identical_inputs_pass_with_nothing_listed():
    rows = _session() + _session(symbol="000660")
    report = cmp.compare_minutes(rows, list(rows))

    assert report.verdict == cmp.PASS
    assert not report.blocking
    assert report.ohlcv_mismatch == 0
    assert report.rows_missing_on_go == 0 and report.rows_missing_on_py == 0
    assert report.counts_outside_tolerance == ()
    assert report.matched_bars == len(rows)


def test_whole_missing_session_on_go_blocks():
    py = _session() + _session(session_date=date(2026, 11, 25))
    go = _session()  # 11-25 세션 통째로 없음
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.BLOCK
    assert report.blocking
    assert report.missing_sessions_on_go == (("005930", "KRX", date(2026, 11, 25)),)


def test_whole_missing_symbol_on_go_blocks():
    py = _session() + _session(symbol="000660")
    go = _session()
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.BLOCK
    assert report.missing_symbols_on_go == ("000660",)
    # 통째로 빠진 종목의 세션은 종목 단위 차단에 포함되고 세션으로 중복 집계되지 않는다.
    assert report.missing_sessions_on_go == ()


def test_whole_missing_venue_session_blocks():
    py = _session(venue="KRX") + _session(venue="NTX")
    go = _session(venue="KRX")
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.BLOCK
    assert report.missing_sessions_on_go == (("005930", "NTX", KR_DAY),)


def test_go_only_session_also_blocks_both_directions():
    go = _session() + _session(session_date=date(2026, 11, 25))
    py = _session()
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.BLOCK
    assert report.missing_sessions_on_py == (("005930", "KRX", date(2026, 11, 25)),)


def test_one_mismatched_bar_among_many_passes_with_mismatch_listed():
    py = _session(minutes=40)
    go = [_bar(m, close="555") if m == 7 else _bar(m) for m in range(40)]
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.PASS
    assert report.ohlcv_mismatch == 1
    assert report.ohlcv_equal == 39
    assert report.ohlcv_mismatch_keys == ("2026-11-24T00:07:00Z 005930 KRX",)


def test_value_only_differences_pass_and_are_listed():
    py = _session(minutes=20)
    go = [_bar(m, value="999") if m < 5 else _bar(m) for m in range(20)]
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.PASS
    assert report.value_differs == 5
    assert report.ohlcv_mismatch == 0


def test_value_null_on_one_side_passes_and_is_listed():
    py = _session(minutes=10)
    go = [_bar(m, value=None) if m == 3 else _bar(m) for m in range(10)]
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.PASS
    assert report.value_one_side_null == 1
    assert report.value_differs == 0


def test_partial_gap_is_listed_and_never_blocks():
    py = _session(minutes=30)
    go = [_bar(m) for m in range(30) if m != 12]
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.PASS
    assert report.rows_missing_on_go == 1
    assert report.partial_gap_keys_on_go == ("2026-11-24T00:12:00Z 005930 KRX",)


def test_row_count_outside_tolerance_passes_and_is_listed():
    py = _session(minutes=100)
    go = _session(minutes=60)  # 40 봉 차이, 한 세션 안
    report = cmp.compare_minutes(go, py)

    assert report.verdict == cmp.PASS
    assert report.counts_outside_tolerance == (("005930", "KRX", KR_DAY),)


# ---- tolerance -------------------------------------------------------------


@pytest.mark.parametrize(
    ("go_rows", "py_rows", "within"),
    [
        (100, 100, True),
        (100, 102, True),  # 절대 2 봉
        (100, 103, True),  # 2% of 103 = ceil(2.06)=3 → 허용
        (100, 105, False),  # 5 봉 차이, 2% 도 넘음
        (0, 2, True),  # 0 이면 세션 쌍에 들어가지 않으므로 허용 계산만 확인
    ],
)
def test_tolerance_rule(go_rows, py_rows, within):
    assert (abs(go_rows - py_rows) <= cmp._tolerance(go_rows, py_rows)) is within


def test_tolerance_is_stated_in_cutover_table():
    report = cmp.compare_minutes(_session(), _session())
    rows = cmp.cutover_table(report)
    tolerance_row = next(r for r in rows if r.check == "세션별 행 수")
    assert "max(2 봉" in tolerance_row.threshold
    assert "2%" in tolerance_row.threshold


# ---- duplicates ------------------------------------------------------------


def test_duplicate_go_keys_are_counted_and_first_kept():
    base = _session(minutes=5)
    go = base + [_bar(2, close="777")]
    report = cmp.compare_minutes(go, base)

    assert report.duplicate_go_keys == 1
    assert report.ohlcv_mismatch == 0  # 첫 행이 유지되어 원본과 같다
    assert report.verdict == cmp.PASS


# ---- acceptance: the cutover table ----------------------------------------


def test_cutover_table_blocks_only_coverage_rows():
    py = _session() + _session(symbol="000660")
    go = _session()
    rows = cmp.cutover_table(cmp.compare_minutes(go, py))

    blocking = [r for r in rows if r.blocking]
    assert [r.check for r in blocking] == [
        "같은 종목 커버 (양방향)",
        "같은 세션 커버 (양방향)",
    ]
    assert blocking[0].status == "BLOCK"
    assert blocking[1].status == "ok"


def test_cutover_table_latency_is_unavailable_from_go_jsonl():
    rows = cmp.cutover_table(cmp.compare_minutes(_session(), _session()))
    latency = next(r for r in rows if r.check == "수신 지연 차이")
    assert latency.status == "unavailable"
    assert latency.blocking is False


def test_cutover_table_pass_has_no_blocking_status():
    report = cmp.compare_minutes(_session(), _session())
    rows = cmp.cutover_table(report)
    assert report.verdict == cmp.PASS
    assert all(r.status != "BLOCK" for r in rows)


# ---- parsing ----------------------------------------------------------------


def test_parses_the_real_fillwire_golden_line():
    row = cmp.parse_go_row(json.loads(GO_GOLDEN_LINE))

    assert row.time_utc == datetime(2026, 11, 24, 0, 0, tzinfo=UTC)
    assert row.session_date == KR_DAY
    assert row.symbol == "005930"
    assert row.venue == "KRX"
    assert row.session_segment == "KRX_REGULAR"
    assert row.source == "KIS"
    assert row.close == Decimal("100")
    assert row.value == Decimal("100")


def test_go_null_value_parses_to_none():
    raw = json.loads(GO_GOLDEN_LINE)
    raw["value"] = None
    assert cmp.parse_go_row(raw).value is None


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("close", 100.0),  # float 금지: 정확한 10진 문자열만
        ("close", True),  # bool 은 int 로 세므로 거부
        ("close", "NaN"),
        ("close", "Infinity"),
        ("close", "not-a-number"),
        ("time_utc", "2026-11-24T00:00:00"),  # Z 없음
        ("time_utc", "2026-11-24T00:00:00+09:00"),  # Z 가 아님
    ],
)
def test_parser_rejects_inexact_or_unanchored_values(field, bad):
    raw = json.loads(GO_GOLDEN_LINE)
    raw[field] = bad
    with pytest.raises(cmp.CompareInputError):
        cmp.parse_go_row(raw)


def test_python_row_requires_aware_timestamp():
    row = {
        "time_utc": datetime(2026, 11, 24, 0, 0),  # naive
        "session_date": KR_DAY,
        "symbol": "005930",
        "venue": "KRX",
        "session_segment": "KRX_REGULAR",
        "source": "KIS",
        "open": Decimal("1"),
        "high": Decimal("1"),
        "low": Decimal("1"),
        "close": Decimal("1"),
        "volume": Decimal("1"),
        "value": None,
    }
    with pytest.raises(cmp.CompareInputError):
        cmp.parse_python_row(row)


def test_python_row_accepts_decimal_and_aware_timestamp():
    row = {
        "time_utc": datetime(2026, 11, 24, 0, 0, tzinfo=UTC),
        "session_date": KR_DAY,
        "symbol": "005930",
        "venue": "KRX",
        "session_segment": "KRX_REGULAR",
        "source": "KIS",
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100"),
        "volume": Decimal("10"),
        "value": Decimal("100"),
    }
    bar = cmp.parse_python_row(row)
    assert bar.key == (datetime(2026, 11, 24, 0, 0, tzinfo=UTC), "005930", "KRX")


# ---- acceptance 3: failure-code mapping table ------------------------------

TICKET_CODES = (
    "rate_limited",
    "circuit_open",
    "feed_gap",
    "no_trades",
    "budget_cut",
    "fetch_error",
    "unsupported",
)


def test_every_ticket_failure_code_has_a_mapping_row():
    assert set(TICKET_CODES) <= set(cmp.FAILURE_CODE_MAP)


@pytest.mark.parametrize(
    ("go_code", "expected"),
    [
        ("rate_limited", "fetch_error"),
        ("circuit_open", "fetch_error"),
        ("budget_cut", "cut_by_budget"),
        ("no_trades", "no_trades_reported"),
        ("fetch_error", "fetch_error"),
        ("feed_gap", None),
        ("unsupported", None),
    ],
)
def test_failure_code_maps_to_hk6299_word_or_none(go_code, expected):
    mapped, note = cmp.FAILURE_CODE_MAP[go_code]
    assert mapped == expected
    assert note  # 모든 행에는 이유가 있다


def test_mapped_words_are_only_hk6299_diagnostic_words_or_none():
    for code, (mapped, _note) in cmp.FAILURE_CODE_MAP.items():
        assert mapped is None or mapped in cmp.SESSION_DIAGNOSTIC_WORDS, code


def test_go_client_classes_outside_the_ticket_fold_into_fetch_error():
    for code in (
        "token_unavailable",
        "malformed_response",
        "canceled",
        "credential_host",
    ):
        assert cmp.FAILURE_CODE_MAP[code][0] == "fetch_error", code


# ---- review r1: an empty or unmatched comparison is never a PASS ------------


def test_empty_both_sides_is_inconclusive_not_pass():
    report = cmp.compare_minutes([], [])

    assert report.verdict == cmp.INCONCLUSIVE
    assert not report.blocking
    assert report.inconclusive_reasons == (
        cmp.REASON_GO_ROWS_EMPTY,
        cmp.REASON_PY_ROWS_EMPTY,
    )
    rows = cmp.cutover_table(report)
    evidence = next(r for r in rows if r.check.startswith("비교 근거"))
    assert evidence.status == "INCONCLUSIVE"


def test_empty_go_with_python_rows_blocks_and_stays_blocked():
    report = cmp.compare_minutes([], _session())

    assert report.verdict == cmp.BLOCK
    assert report.missing_symbols_on_go == ("005930",)
    assert cmp.REASON_GO_ROWS_EMPTY in report.inconclusive_reasons


def test_no_overlapping_bars_is_inconclusive():
    go = [_bar(m) for m in range(100, 105)]  # 같은 세션, 겹치는 봉 없음
    py = [_bar(m) for m in range(0, 5)]
    report = cmp.compare_minutes(go, py)

    assert report.matched_bars == 0
    assert report.verdict == cmp.INCONCLUSIVE
    assert report.inconclusive_reasons == (cmp.REASON_NO_MATCHED_BARS,)


def test_requested_symbol_absent_on_both_sides_is_inconclusive():
    rows = _session()
    report = cmp.compare_minutes(
        rows, list(rows), requested_symbols=["005930", "999999"]
    )

    assert report.verdict == cmp.INCONCLUSIVE
    assert report.requested_symbols_absent_on_both == ("999999",)
    assert report.inconclusive_reasons == (cmp.REASON_REQUESTED_ABSENT_ON_BOTH,)


def test_requested_symbol_present_on_one_side_still_blocks_not_inconclusive():
    rows = _session()
    py = rows + _session(symbol="000660")
    report = cmp.compare_minutes(rows, py, requested_symbols=["000660"])

    assert report.verdict == cmp.BLOCK
    assert report.requested_symbols_absent_on_both == ()


def test_clean_pass_has_no_inconclusive_reasons():
    rows = _session()
    report = cmp.compare_minutes(rows, list(rows), requested_symbols=["005930"])

    assert report.verdict == cmp.PASS
    assert report.inconclusive_reasons == ()
    assert report.requested_symbols_absent_on_both == ()
