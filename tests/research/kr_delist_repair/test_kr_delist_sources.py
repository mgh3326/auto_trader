"""Parsers, outcome vocabulary and resolution precedence."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from research.kr_delist_repair import kind_parse
from research.kr_delist_repair.cli import NOTICE_TITLE
from research.kr_delist_repair.model import (
    KindDelistRow,
    KindDisclosure,
    PayoutSemantics,
    TerminalOutcome,
)
from research.kr_delist_repair.outcome import classify
from research.kr_delist_repair.resolve import Presence, resolve_all

EXCEL = """
<table><tr><th>번호</th><th>회사명</th><th>종목코드</th><th>폐지일자</th><th>폐지사유</th><th>비고</th></tr>
<tr><td>2</td><td>예스티</td><td style="mso-number-format:'@'">122640</td><td>2015-12-16</td><td>코스닥시장 이전상장</td><td>예스티</td></tr>
<tr><td>1</td><td>GT&amp;T</td><td>053870</td><td>2015-10-15</td><td>자본전액잠식</td><td></td></tr>
</table>"""

BODY = """
<html><style>.P{x}</style><table>
<tr><td>상장폐지</td></tr>
<tr><td>1.상장폐지 대상 주권 및 주식수</td></tr>
<tr><td>주권종류</td><td>주식수</td><td>단축코드</td></tr>
<tr><td>노루홀딩스2우선주(신형)</td><td>92,540</td><td>A000327</td></tr>
<tr><td>2.상장폐지 사유</td><td>존속기간 만료</td></tr>
<tr><td>3.상장폐지일</td><td>2017-09-28</td></tr>
</table></html>"""


def test_delist_excel_parses_codes_dates_and_entities() -> None:
    rows = kind_parse.parse_delist_excel(EXCEL, "KOSDAQ")
    assert [(r.symbol, r.delist_date, r.name) for r in rows] == [
        ("122640", date(2015, 12, 16), "예스티"),
        ("053870", date(2015, 10, 15), "GT&T"),
    ]


def test_delist_excel_rejects_an_unexpected_header() -> None:
    with pytest.raises(ValueError, match="header"):
        kind_parse.parse_delist_excel(EXCEL.replace("종목코드", "코드"), "KOSDAQ")


def test_notice_body_yields_exact_code_date_and_reason() -> None:
    (rec,) = kind_parse.parse_disclosure_body(
        BODY, "20170925000286", "2017-09-25 17:26"
    )
    assert rec.symbol == "000327"
    assert rec.delist_date == date(2017, 9, 28)
    assert rec.reason == "존속기간 만료"
    assert rec.issue_name == "노루홀딩스2우선주(신형)"


KOSDAQ_BODY = """
<table><tr><td>상장폐지</td></tr><tr><td>1.회사명</td><td>(주)루트로닉</td></tr>
<tr><td>2.주권종류 및 발행주식수</td></tr>
<tr><td>주권종류</td><td>단축코드</td><td>주식수(주)</td><td>상장일</td></tr>
<tr><td>보통주</td><td>A085370</td><td>25,871,695</td><td>2006-07-04</td></tr>
<tr><td>종류주식3</td><td>A08537M</td><td>153,536</td><td>2016-12-15</td></tr>
<tr><td>3.액면가(원)</td><td>500</td></tr>
<tr><td>5.상장폐지사유</td><td>타법인의 완전자회사로 편입</td></tr>
<tr><td>6.상장폐지일</td><td>2023-10-27</td></tr></table>"""


def test_kosdaq_notice_layout_is_read_by_header() -> None:
    recs = kind_parse.parse_disclosure_body(KOSDAQ_BODY, "2", "2023-10-10 18:00")
    assert [(r.symbol, r.issue_name) for r in recs] == [
        ("085370", "보통주"),
        ("08537M", "종류주식3"),
    ]
    assert {r.delist_date for r in recs} == {date(2023, 10, 27)}


@pytest.mark.parametrize(
    ("title", "matches"),
    [
        ("상장폐지", True),
        ("상장폐지(노루홀딩스2우선주(신형))", True),
        ("[정정]상장폐지 (X우)", True),
        ("상장폐지예고", False),
        ("주식워런트증권 상장폐지조치", False),
        ("기타시장안내(상장폐지 우려 예고)", False),
    ],
)
def test_notice_title_filter(title: str, matches: bool) -> None:
    assert bool(NOTICE_TITLE.match(title)) is matches


def test_notice_body_without_an_issue_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="short code"):
        kind_parse.parse_disclosure_body(
            BODY.replace("A000327", "-"), "1", "2017-09-25 17:26"
        )


def test_search_page_empty_marker_and_rows() -> None:
    assert kind_parse.parse_disclosure_search("<p>조회된 결과값이 없습니다.</p>") == []
    page = (
        "<tr><td>1</td><td>2017-09-25 17:26</td><td>노루홀딩스</td>"
        "<td><a onclick=\"openDisclsViewer('20170925000286','')\">상장폐지(노루홀딩스2우선주(신형))</a></td>"
        "<td>유가증권시장본부</td></tr>"
    )
    assert kind_parse.parse_disclosure_search(page) == [
        (
            "20170925000286",
            "2017-09-25 17:26",
            "노루홀딩스",
            "상장폐지(노루홀딩스2우선주(신형))",
            "유가증권시장본부",
        )
    ]
    with pytest.raises(ValueError):
        kind_parse.parse_disclosure_search("<html>error</html>")


def test_kis_master_layout() -> None:
    suffix = 10
    line = "005930   KR7005930003삼성전자" + "x" * suffix
    pref = "08537M   KR708537M003루트로닉3우C" + "y" * suffix
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("kospi_code.mst", "\n".join([line, pref]).encode("cp949"))
    assert kind_parse.parse_kis_master(buf.getvalue(), suffix) == {
        "005930": "삼성전자",
        "08537M": "루트로닉3우C",
    }


@pytest.mark.parametrize(
    ("reason", "name", "symbol", "expected"),
    [
        ("피흡수합병(스팩소멸합병)", "하나스팩", "123450", TerminalOutcome.SPAC_MERGER),
        ("피흡수합병", "A", "123450", TerminalOutcome.MERGED_ABSORBED),
        (
            "지주회사(최대주주등)의 완전자회사화 등",
            "A",
            "123450",
            TerminalOutcome.SHARE_EXCHANGE,
        ),
        ("신청에 의한 상장폐지", "A", "123450", TerminalOutcome.VOLUNTARY),
        ("상장폐지 신청('22.08.31)", "A", "123450", TerminalOutcome.VOLUNTARY),
        (
            "감사의견 거절(감사범위 제한)",
            "A",
            "123450",
            TerminalOutcome.STATUTORY_CAUSE,
        ),
        ("해산사유 발생(파산선고)", "A", "123450", TerminalOutcome.STATUTORY_CAUSE),
        ("해산 사유 발생", "A", "123450", TerminalOutcome.DISSOLUTION),
        (
            "상장예비심사 청구서 미제출로 관리종목 지정 후 1개월 이내 동 사유 미해소",
            "엔에이치스팩7호",
            "123450",
            TerminalOutcome.SPAC_DEADLINE,
        ),
        (
            "존속기간 만료",
            "노루홀딩스2우B",
            "000327",
            TerminalOutcome.PREFERRED_CONVERSION,
        ),
        ("존속기간 만료", "동북아12호선박투자", "083370", TerminalOutcome.DISSOLUTION),
        ("상장주식수 미달", "X우", "000545", TerminalOutcome.PREFERRED_FLOAT),
        ("코스닥시장 이전상장", "A", "123450", TerminalOutcome.MARKET_TRANSFER),
        ("유가증권시장 상장", "A", "123450", TerminalOutcome.MARKET_TRANSFER),
        (
            "상장예비심사 청구서 미제출로 관리종목 지정 후 1개월 이내 동 사유 미해소",
            "케이프이에스제4호",
            "347140",
            TerminalOutcome.SPAC_DEADLINE,
        ),
        (
            "법정제출기한까지 사업보고서를 제출하지 아니한 후 10일이내에 제출하지 아니함",
            "차이나그레이트",
            "900040",
            TerminalOutcome.STATUTORY_CAUSE,
        ),
        ("기타", "A", "123450", TerminalOutcome.UNCLASSIFIED),
    ],
)
def test_outcome_vocabulary(
    reason: str, name: str, symbol: str, expected: TerminalOutcome
) -> None:
    assert classify(reason, name=name, symbol=symbol)[0] is expected


def test_dissolution_payout_is_not_guessed() -> None:
    assert classify("해산 사유 발생", name="삼성물산", symbol="000830")[1] is (
        PayoutSemantics.AMBIGUOUS
    )


CAL = [date(2020, 3, d) for d in (2, 3, 4, 5, 6)]


def _p(symbol: str, last: date) -> Presence:
    return Presence(symbol, CAL[0], last, ("KOSPI",), True)


def _kind(symbol: str, when: date, reason: str = "감사의견 거절") -> KindDelistRow:
    return KindDelistRow("KOSPI", symbol, f"n{symbol}", when, reason, "")


def _resolve(targets, kind_rows=(), disclosures=()):  # type: ignore[no-untyped-def]
    rows = resolve_all(
        targets=list(targets),
        calendar=CAL,
        activity={},
        kind_rows=list(kind_rows),
        disclosures=list(disclosures),
        master_names={},
    )
    return {r.symbol: r for r in rows}


def test_kind_row_matching_the_next_session_resolves() -> None:
    r = _resolve([_p("000100", CAL[2])], [_kind("000100", CAL[3])])["000100"]
    assert r.resolved and r.date_basis == "kind_delist_list"
    assert r.corpus_date_check == "equals_next_session"
    assert r.information_available_at == "2020-03-05T00:00:00+09:00"


def test_date_disagreeing_with_the_corpus_stays_unresolved() -> None:
    r = _resolve([_p("000100", CAL[1])], [_kind("000100", CAL[3])])["000100"]
    assert not r.resolved
    assert "disagrees with corpus calendar" in r.unresolved_reason


def test_notice_body_resolves_a_preferred_share() -> None:
    notice = KindDisclosure(
        "1", "000105", "n우", CAL[3], "상장주식수 미달", "2020-02-20 18:00"
    )
    r = _resolve([_p("000105", CAL[2])], disclosures=[notice])["000105"]
    assert r.resolved and r.date_basis == "kind_disclosure_body"
    assert r.information_available_at == "2020-02-20T18:00:00+09:00"


def test_preferred_inherits_a_same_session_parent_row() -> None:
    r = _resolve([_p("000105", CAL[2])], [_kind("000100", CAL[3], "피흡수합병")])[
        "000105"
    ]
    assert r.resolved and r.parent_symbol == "000100"
    assert r.terminal_outcome == "merged_absorbed"


def test_parent_row_on_another_session_is_not_inherited() -> None:
    r = _resolve([_p("000105", CAL[1])], [_kind("000100", CAL[3])])["000105"]
    assert not r.resolved


def test_symbol_alive_at_scope_end_takes_a_later_kind_row() -> None:
    r = _resolve([_p("000200", CAL[-1])], [_kind("000200", date(2025, 6, 2))])["000200"]
    assert r.resolved and r.corpus_date_check == "not_checkable_main_scope_alive"


def test_kind_row_before_the_last_presence_is_ignored() -> None:
    r = _resolve([_p("000300", CAL[-1])], [_kind("000300", CAL[1])])["000300"]
    assert not r.resolved


def test_market_transfer_only_is_not_a_terminal_event() -> None:
    r = _resolve(
        [_p("000400", CAL[2])], [_kind("000400", CAL[3], "유가증권시장 상장")]
    )["000400"]
    assert not r.resolved and "market-transfer" in r.unresolved_reason
