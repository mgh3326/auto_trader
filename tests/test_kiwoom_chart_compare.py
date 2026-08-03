# tests/test_kiwoom_chart_compare.py
"""Fixture-driven tests for the mock-vs-live Kiwoom chart comparison harness.

No network: payloads are synthetic (shaped from the official ka10080/ka10081
response examples) and the orchestrator's fetchers and sleep are injected.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kiwoom.chart_compare import (
    CROSSCHECKABLE_SYMBOLS,
    FROZEN_KIS_SAMPLE_PATH,
    MAX_CONCURRENCY,
    MAX_TOTAL_CALLS,
    MIN_CALL_INTERVAL_SECONDS,
    SYMBOLS_PER_MARKET,
    CallBudgetExceeded,
    ChartKind,
    FrozenBar,
    SymbolCandidate,
    Verdict,
    adjudicate_mismatches,
    compare_chart_payloads,
    crosscheck_daily_row_against_frozen,
    extract_rows,
    load_frozen_kis_sample,
    normalize_value,
    run_pairwise_comparison,
    select_comparison_symbols,
)

pytestmark = pytest.mark.unit


def _daily_row(dt: str, *, close="70100", volume="9263135", open_="69800"):
    return {
        "cur_prc": close,
        "trde_qty": volume,
        "trde_prica": "648525",
        "dt": dt,
        "open_pric": open_,
        "high_pric": "70500",
        "low_pric": "69600",
        "pred_pre": "+600",
        "pred_pre_sig": "2",
        "trde_tern_rt": "+0.16",
    }


def _daily_payload(rows):
    return {
        "stk_cd": "005930",
        "stk_dt_pole_chart_qry": list(rows),
        "return_code": 0,
        "return_msg": "정상적으로 처리되었습니다",
    }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_minute_sign_prefix_is_a_direction_marker_not_a_negative_price():
    """ka10080 returns "-78800" for a down-tick at price 78800."""

    assert normalize_value("-78800", field_name="cur_prc") == Decimal("78800")
    assert normalize_value("+78800", field_name="cur_prc") == Decimal("78800")
    assert normalize_value("78800", field_name="open_pric") == Decimal("78800")


def test_pred_pre_keeps_its_sign():
    assert normalize_value("-600", field_name="pred_pre") == Decimal("-600")
    assert normalize_value("+600", field_name="pred_pre") == Decimal("600")


@pytest.mark.parametrize("raw", [None, "", "   ", "N/A", "--"])
def test_unparseable_values_become_none(raw):
    assert normalize_value(raw, field_name="cur_prc") is None


def test_signed_mock_and_unsigned_live_are_not_a_mismatch():
    """A sign-prefix difference alone must not be reported as a data difference."""

    mock = _daily_payload([_daily_row("20260731", close="-70100")])
    live = _daily_payload([_daily_row("20260731", close="70100")])

    result = compare_chart_payloads(
        symbol="005930", kind=ChartKind.DAILY, mock_payload=mock, live_payload=live
    )
    assert result.mismatches == ()
    assert result.rows_identical


# ---------------------------------------------------------------------------
# Pairwise comparison
# ---------------------------------------------------------------------------


def test_identical_payloads_compare_clean():
    rows = [_daily_row("20260731"), _daily_row("20260730")]
    result = compare_chart_payloads(
        symbol="005930",
        kind=ChartKind.DAILY,
        mock_payload=_daily_payload(rows),
        live_payload=_daily_payload(rows),
    )

    assert result.rows_identical
    assert result.mock_row_count == 2
    assert result.live_row_count == 2
    assert result.common_row_keys == ("20260730", "20260731")
    assert "IDENTICAL" in result.summary()


def test_field_level_mismatch_is_pinpointed():
    mock = _daily_payload([_daily_row("20260731", volume="9263135")])
    live = _daily_payload([_daily_row("20260731", volume="9263200")])

    result = compare_chart_payloads(
        symbol="005930", kind=ChartKind.DAILY, mock_payload=mock, live_payload=live
    )

    assert not result.rows_identical
    assert len(result.mismatches) == 1
    mismatch = result.mismatches[0]
    assert (mismatch.row_key, mismatch.field_name) == ("20260731", "trde_qty")
    assert (mismatch.mock_raw, mismatch.live_raw) == ("9263135", "9263200")
    assert result.mismatched_row_keys == ("20260731",)


def test_row_coverage_difference_is_reported_per_side():
    mock = _daily_payload([_daily_row("20260731"), _daily_row("20260730")])
    live = _daily_payload([_daily_row("20260731"), _daily_row("20260729")])

    result = compare_chart_payloads(
        symbol="005930", kind=ChartKind.DAILY, mock_payload=mock, live_payload=live
    )

    assert result.keys_only_in_mock == ("20260730",)
    assert result.keys_only_in_live == ("20260729",)
    assert result.common_row_keys == ("20260731",)
    assert not result.rows_identical


def test_delisted_empty_response_does_not_crash():
    """Mock returned a 1-row EMPTY payload for the delisted control symbol."""

    empty = {"stk_cd": "051170", "stk_dt_pole_chart_qry": [], "return_code": 0}
    populated = _daily_payload([_daily_row("20260731")])

    result = compare_chart_payloads(
        symbol="051170",
        kind=ChartKind.DAILY,
        mock_payload=empty,
        live_payload=populated,
    )
    assert result.mock_row_count == 0
    assert result.live_row_count == 1
    assert result.keys_only_in_live == ("20260731",)


def test_missing_list_key_yields_no_rows():
    assert extract_rows({"return_code": 0}, ChartKind.DAILY) == []
    assert extract_rows({"stk_dt_pole_chart_qry": None}, ChartKind.DAILY) == []


def test_minute_kind_keys_rows_by_contract_time():
    minute_row = {
        "cur_prc": "-78800",
        "trde_qty": "7913",
        "cntr_tm": "20250917132000",
        "open_pric": "-78850",
        "high_pric": "-78900",
        "low_pric": "-78800",
        "acc_trde_qty": "14947571",
        "pred_pre": "-600",
        "pred_pre_sig": "5",
    }
    payload = {"stk_cd": "005930", "stk_min_pole_chart_qry": [minute_row]}

    result = compare_chart_payloads(
        symbol="005930",
        kind=ChartKind.MINUTE,
        mock_payload=payload,
        live_payload=payload,
    )
    assert result.common_row_keys == ("20250917132000",)
    assert result.rows_identical


def test_chart_kind_metadata_matches_official_docs():
    assert ChartKind.DAILY.api_id == "ka10081"
    assert ChartKind.DAILY.list_key == "stk_dt_pole_chart_qry"
    assert ChartKind.MINUTE.list_key == "stk_min_pole_chart_qry"
    assert ChartKind.WEEKLY.list_key == "stk_stk_pole_chart_qry"
    assert ChartKind.MONTHLY.list_key == "stk_mth_pole_chart_qry"


# ---------------------------------------------------------------------------
# Third-source adjudication — never picks a winner without evidence
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen_stub():
    return {
        ("005930", "20260731"): FrozenBar(
            symbol="005930",
            session_date="20260731",
            open=Decimal("69800"),
            high=Decimal("70500"),
            low=Decimal("69600"),
            close=Decimal("70100"),
            volume=Decimal("9263135"),
            value=Decimal("648525000000"),
        )
    }


def test_crosscheck_confirms_a_matching_row(frozen_stub):
    result = crosscheck_daily_row_against_frozen(
        symbol="005930", row=_daily_row("20260731"), frozen=frozen_stub
    )
    assert result.verdict is Verdict.MATCH


def test_crosscheck_flags_a_differing_row(frozen_stub):
    result = crosscheck_daily_row_against_frozen(
        symbol="005930",
        row=_daily_row("20260731", volume="9999999"),
        frozen=frozen_stub,
    )
    assert result.verdict is Verdict.MISMATCH
    assert ("trde_qty", Verdict.MISMATCH) in result.field_verdicts


def test_crosscheck_is_undetermined_for_a_non_crosscheckable_symbol(frozen_stub):
    result = crosscheck_daily_row_against_frozen(
        symbol="060310", row=_daily_row("20260731"), frozen=frozen_stub
    )
    assert result.verdict is Verdict.UNDETERMINED
    assert "not in frozen" in result.reason


def test_crosscheck_is_undetermined_for_a_date_outside_the_sample(frozen_stub):
    result = crosscheck_daily_row_against_frozen(
        symbol="005930", row=_daily_row("20991231"), frozen=frozen_stub
    )
    assert result.verdict is Verdict.UNDETERMINED


def test_adjudication_leaves_unverifiable_disagreements_undetermined():
    """The harness must not declare a winner on mock-vs-live alone."""

    mock = _daily_payload([_daily_row("20260731", volume="1")])
    live = _daily_payload([_daily_row("20260731", volume="2")])
    comparison = compare_chart_payloads(
        symbol="060310", kind=ChartKind.DAILY, mock_payload=mock, live_payload=live
    )

    adjudicated = adjudicate_mismatches(
        comparison=comparison, mock_payload=mock, live_payload=live, frozen={}
    )

    assert len(adjudicated) == 1
    assert adjudicated[0]["mock"] is Verdict.UNDETERMINED
    assert adjudicated[0]["live"] is Verdict.UNDETERMINED


def test_adjudication_names_the_supported_side_when_kis_can_speak(frozen_stub):
    mock = _daily_payload([_daily_row("20260731", volume="9263135")])
    live = _daily_payload([_daily_row("20260731", volume="9999999")])
    comparison = compare_chart_payloads(
        symbol="005930", kind=ChartKind.DAILY, mock_payload=mock, live_payload=live
    )

    adjudicated = adjudicate_mismatches(
        comparison=comparison,
        mock_payload=mock,
        live_payload=live,
        frozen=frozen_stub,
    )

    assert adjudicated[0]["mock"] is Verdict.MATCH
    assert adjudicated[0]["live"] is Verdict.MISMATCH


def test_minute_disagreements_are_never_adjudicated():
    """The frozen sample is daily-only, so minute rows stay UNDETERMINED."""

    row_a = {"cntr_tm": "20250917132000", "cur_prc": "78800", "trde_qty": "1"}
    row_b = {"cntr_tm": "20250917132000", "cur_prc": "78800", "trde_qty": "2"}
    mock = {"stk_min_pole_chart_qry": [row_a]}
    live = {"stk_min_pole_chart_qry": [row_b]}
    comparison = compare_chart_payloads(
        symbol="005930", kind=ChartKind.MINUTE, mock_payload=mock, live_payload=live
    )

    adjudicated = adjudicate_mismatches(
        comparison=comparison, mock_payload=mock, live_payload=live, frozen={}
    )
    assert adjudicated[0]["mock"] is Verdict.UNDETERMINED
    assert adjudicated[0]["live"] is Verdict.UNDETERMINED


# ---------------------------------------------------------------------------
# Frozen sample loading (read-only)
# ---------------------------------------------------------------------------


def test_frozen_sample_loads_and_is_keyed_by_symbol_and_date(tmp_path):
    csv_path = tmp_path / "frozen.csv"
    csv_path.write_text(
        "symbol,session_date,open,high,low,close,volume,value\n"
        "000660,2025-05-20,202500,208000,201500,202000,2820546,575346512520\n",
        encoding="utf-8",
    )

    bars = load_frozen_kis_sample(csv_path)

    assert list(bars) == [("000660", "20250520")]
    bar = bars[("000660", "20250520")]
    assert bar.close == Decimal("202000")
    assert bar.volume == Decimal("2820546")


@pytest.mark.skipif(
    not FROZEN_KIS_SAMPLE_PATH.exists(),
    reason="frozen KIS crosscheck artifact not present on this machine",
)
def test_real_frozen_artifact_parses_and_covers_the_expected_symbols():
    bars = load_frozen_kis_sample()

    assert bars
    assert {symbol for symbol, _ in bars} == set(CROSSCHECKABLE_SYMBOLS)


# ---------------------------------------------------------------------------
# Deterministic symbol selection
# ---------------------------------------------------------------------------


def _candidates():
    return [
        SymbolCandidate(f"kospi{i:02d}", "kospi", Decimal(1000 - i)) for i in range(15)
    ] + [
        SymbolCandidate(f"kosdq{i:02d}", "kosdaq", Decimal(500 - i)) for i in range(15)
    ]


def test_selection_takes_top_turnover_per_market():
    picked = select_comparison_symbols(_candidates())

    assert len(picked) == 2 * SYMBOLS_PER_MARKET
    assert picked[:1] == ("kosdq00",)  # markets iterated in sorted order
    assert "kospi00" in picked
    assert "kospi14" not in picked


def test_selection_is_deterministic_regardless_of_input_order():
    candidates = _candidates()
    forward = select_comparison_symbols(candidates)
    backward = select_comparison_symbols(list(reversed(candidates)))
    assert forward == backward


def test_selection_ties_break_on_symbol_code():
    tied = [
        SymbolCandidate("000660", "kospi", Decimal(100)),
        SymbolCandidate("005930", "kospi", Decimal(100)),
    ]
    assert select_comparison_symbols(tied, per_market=1) == ("000660",)


# ---------------------------------------------------------------------------
# Orchestrator — paced, budgeted, serial; fetchers injected (no network)
# ---------------------------------------------------------------------------


def _fetcher(payload_by_symbol, log, label):
    async def fetch(symbol: str):
        log.append((label, symbol))
        return payload_by_symbol[symbol]

    return fetch


def test_stage_1b_envelope_constants_match_the_brief():
    assert MIN_CALL_INTERVAL_SECONDS == 2.0
    assert MAX_TOTAL_CALLS == 200
    assert MAX_CONCURRENCY == 1
    assert SYMBOLS_PER_MARKET == 10


@pytest.mark.asyncio
async def test_run_paces_calls_and_records_comparisons():
    payloads = {"005930": _daily_payload([_daily_row("20260731")])}
    call_log: list[tuple[str, str]] = []
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    report = await run_pairwise_comparison(
        symbols=["005930"],
        kind=ChartKind.DAILY,
        fetch_mock=_fetcher(payloads, call_log, "mock"),
        fetch_live=_fetcher(payloads, call_log, "live"),
        sleep=sleep,
    )

    assert report.calls_made == 2
    assert call_log == [("mock", "005930"), ("live", "005930")]
    assert slept == [MIN_CALL_INTERVAL_SECONDS]  # paced between the two calls
    assert len(report.comparisons) == 1
    assert report.comparisons[0].rows_identical


@pytest.mark.asyncio
async def test_run_refuses_to_exceed_the_call_budget():
    payloads = {s: _daily_payload([_daily_row("20260731")]) for s in ("A", "B", "C")}

    async def sleep(_seconds: float) -> None:
        return None

    with pytest.raises(CallBudgetExceeded):
        await run_pairwise_comparison(
            symbols=["A", "B", "C"],
            kind=ChartKind.DAILY,
            fetch_mock=_fetcher(payloads, [], "mock"),
            fetch_live=_fetcher(payloads, [], "live"),
            sleep=sleep,
            max_total_calls=4,
        )


@pytest.mark.asyncio
async def test_run_records_fetch_errors_and_continues():
    good = _daily_payload([_daily_row("20260731")])

    async def fetch_mock(symbol: str):
        if symbol == "BAD":
            raise RuntimeError("boom")
        return good

    async def fetch_live(_symbol: str):
        return good

    async def sleep(_seconds: float) -> None:
        return None

    report = await run_pairwise_comparison(
        symbols=["BAD", "005930"],
        kind=ChartKind.DAILY,
        fetch_mock=fetch_mock,
        fetch_live=fetch_live,
        sleep=sleep,
    )

    assert report.errors == [("BAD", "RuntimeError")]
    assert len(report.comparisons) == 1
    assert report.comparisons[0].symbol == "005930"


@pytest.mark.asyncio
async def test_run_adjudicates_mismatches_with_the_frozen_sample(frozen_stub):
    mock_payloads = {"005930": _daily_payload([_daily_row("20260731")])}
    live_payloads = {
        "005930": _daily_payload([_daily_row("20260731", volume="9999999")])
    }

    async def sleep(_seconds: float) -> None:
        return None

    report = await run_pairwise_comparison(
        symbols=["005930"],
        kind=ChartKind.DAILY,
        fetch_mock=_fetcher(mock_payloads, [], "mock"),
        fetch_live=_fetcher(live_payloads, [], "live"),
        frozen=frozen_stub,
        sleep=sleep,
    )

    assert report.adjudications["005930"][0]["mock"] is Verdict.MATCH
    assert report.adjudications["005930"][0]["live"] is Verdict.MISMATCH
    assert "DIFFERS" in report.summary_lines()[0]
