"""hk 6508: pure parser over saved FnGuide HTML (no network)."""

from __future__ import annotations

import pytest

from app.services.fnguide_consensus.parser import (
    RAW_EMBEDDED_MAX_BYTES,
    PageRejected,
    classify_page,
    parse_charts,
    parse_page,
    to_exact_decimal,
)
from tests.services.fnguide_consensus._helpers import (
    FETCHED,
    OBSERVED,
    load_fixture,
    parse_fixture,
)

pytestmark = pytest.mark.unit


def _values(parsed, section, item, qualifier=""):
    return {
        v.column_key: v
        for v in parsed.values
        if v.section == section
        and v.item_name == item
        and v.item_qualifier == qualifier
    }


def test_005930_target_price_and_opinion_rows_are_exact_decimal_strings() -> None:
    parsed = parse_fixture("005930_consensus.html", symbol="005930")

    assert parsed.status == "parsed"
    assert parsed.reason is None
    assert parsed.company_name == "삼성전자"

    target = _values(parsed, "consensus", "목표주가")
    assert {k: v.value_text for k, v in target.items()} == {
        "VAL1": "458181.82",
        "VAL2": "495227.27",
        "VAL3": "491875.00",
        "VAL4": "390000.00",
        "VAL5": "136038.46",
    }
    assert {v.unit for v in target.values()} == {"(원)"}
    assert {v.digit for v in target.values()} == {0}
    assert all(v.value_reason is None for v in target.values())

    opinion = _values(parsed, "consensus", "투자의견(점수)")
    assert opinion["VAL1"].value_text == "4.00"
    assert opinion["VAL3"].value_text == "4.04"
    assert {v.digit for v in opinion.values()} == {2}
    assert opinion["VAL1"].unit is None


def test_header_labels_are_kept_as_the_page_gives_them() -> None:
    parsed = parse_fixture("005930_consensus.html", symbol="005930")

    target = _values(parsed, "consensus", "목표주가")
    assert target["VAL2"].column_label == "1개월전"
    assert target["VAL5"].column_label == "1년전"
    # The page's own header text for VAL1 is kept verbatim, not reinterpreted.
    assert target["VAL1"].column_label == "당기순이익"
    assert parsed.headers["cnsTrend"][0] == {
        "ID": "ACC_NM",
        "NM": "2026/11/24",
        "DIGIT": "0",
    }


def test_028300_has_a_reason_and_no_fabricated_consensus_rows() -> None:
    parsed = parse_fixture("028300_consensus.html", symbol="028300")

    assert parsed.reason == "all_consensus_values_null"
    assert not any(v.section == "consensus" for v in parsed.values)
    assert all(v.item_name != "목표주가" for v in parsed.values)
    # The chart values that do exist on the page are still recorded.
    assert parsed.value_count > 0
    assert parsed.skipped_items > 0


def test_reports_are_deduplicated_with_date_and_broker() -> None:
    parsed = parse_fixture("005930_consensus.html", symbol="005930")

    first = parsed.reports[0]
    assert first.broker == "다올투자증권"
    assert first.analyst == "남대종"
    assert first.title == "넘쳐나는 FCF에 대한 고민"
    assert first.report_date is not None
    assert first.report_date.isoformat() == "2026-11-24"
    assert first.source_report_id == "1153965"
    assert len({r.report_hash for r in parsed.reports}) == len(parsed.reports)


def test_invest_chart_rows_keep_qualifiers_and_strip_thousands_separators() -> None:
    parsed = parse_fixture("005930_invest.html", symbol="005930", page="Invest")

    stock_high = _values(parsed, "invStockIndex", "주가", "ETC=최고|ROW=1|SEQ=1")
    assert stock_high["VAL1"].item_qualifier == "ETC=최고|ROW=1|SEQ=1"
    assert stock_high["VAL1"].value_text == "79800"
    assert stock_high["VAL1"].column_label == "2022/12"
    assert stock_high["VAL1"].unit == "(원)"

    retained = _values(parsed, "invDividend", "이익유보율")
    assert retained["VAL1"].value_text == "33143.62"
    assert retained["VAL1"].column_label == "2021/12"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.50", ("1234.50", None)),
        ("458181.82", ("458181.82", None)),
        ("-37.5", ("-37.5", None)),
        (None, (None, "missing")),
        ("", (None, "missing")),
        ("-", (None, "missing")),
        ("abc", (None, "unparsable")),
        ("1e5", (None, "unparsable")),
        ("12%", (None, "unparsable")),
        (True, (None, "unparsable")),
        (12.5, (None, "unparsable")),
        ("１２３", (None, "unparsable")),
        ("٣٤", (None, "unparsable")),
    ],
)
def test_to_exact_decimal(raw, expected) -> None:
    assert to_exact_decimal(raw) == expected


def test_page_for_another_company_is_rejected() -> None:
    with pytest.raises(PageRejected) as excinfo:
        parse_page(
            load_fixture("005930_consensus.html"),
            symbol="000660",
            page="Consensus",
            observed_date=OBSERVED,
            fetched_at=FETCHED,
            source_url="https://wcomp.fnguide.com/x",
        )
    assert excinfo.value.kind == "code_mismatch"


def test_login_page_is_rejected_not_parsed() -> None:
    html = "<html><head><title>로그인 | FnGuide</title></head><body></body></html>"
    assert classify_page(html, "005930") == ("로그인 | FnGuide", "login_or_missing")


def test_header_and_data_must_be_adjacent_members_of_one_object() -> None:
    separated = 'x: {"header":[{"ID":"VAL1","NM":"a"}], "other": 1, "data":[{"NAME":"n","VAL1":"1"}]}'
    charts, unparsed = parse_charts(separated)
    assert "x" not in charts
    assert unparsed == 0

    adjacent = 'x: {"header":[{"ID":"VAL1","NM":"a"}],"data":[{"NAME":"n","VAL1":"1"}]}'
    charts, _ = parse_charts(adjacent)
    assert charts["x"].data == [{"NAME": "n", "VAL1": "1"}]

    reversed_order = 'y: {"data":[{"NAME":"m","VAL1":"2"}],"header":[{"CD":"VAL1","YYMM":"2020/12"}]}'
    charts, _ = parse_charts(reversed_order)
    assert charts["y"].header == [{"CD": "VAL1", "YYMM": "2020/12"}]


def test_string_values_with_brackets_do_not_break_matching() -> None:
    html = (
        'z: {"header":[{"ID":"VAL1","NM":"a]b"}],"data":[{"NAME":"x[y]","VAL1":"5"}]}'
    )
    charts, unparsed = parse_charts(html)
    assert unparsed == 0
    assert charts["z"].data == [{"NAME": "x[y]", "VAL1": "5"}]


def test_oversized_raw_embedded_is_dropped_with_a_reason() -> None:
    padding = "a" * (RAW_EMBEDDED_MAX_BYTES + 10)
    html = (
        "<html><head><title>테스트(005930) | Invest</title></head><body>"
        f'<script>w: {{"header":[{{"ID":"VAL1","NM":"{padding}"}}],'
        '"data":[{"NAME":"n","VAL1":"1"}]}</script></body></html>'
    )
    parsed = parse_page(
        html,
        symbol="005930",
        page="Invest",
        observed_date=OBSERVED,
        fetched_at=FETCHED,
        source_url="https://wcomp.fnguide.com/x",
    )
    assert parsed.raw_embedded is None
    assert parsed.raw_reason == "over_bound"
    assert parsed.value_count == 1


def test_fixture_raw_embedded_is_stored_within_bound() -> None:
    parsed = parse_fixture("005930_consensus.html", symbol="005930")
    assert parsed.raw_embedded is not None
    assert parsed.raw_reason is None
    assert set(parsed.raw_embedded["charts"]) == {
        "perforTrend",
        "perforTrendChart",
        "cnsTrendChart",
        "cnsTrend",
    }
