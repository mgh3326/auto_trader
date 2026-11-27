"""hk 6968: one event is counted once; unrelated items stay apart."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.news_event_key import (
    EventCandidate,
    assign_event_keys,
    normalize_event_title,
    single_disclosure_ref,
)

pytestmark = pytest.mark.unit

RECEIPT = "20261126000101"
NOON_KST = datetime(2026, 11, 26, 3, 0, tzinfo=UTC)


def _item(
    title: str,
    *,
    url: str = "https://e/1",
    at: datetime | None = NOON_KST,
    documents: dict | None = None,
) -> EventCandidate:
    return EventCandidate(
        title=title, url=url, published_at=at, source_document_ids=documents
    )


def _keys(*items: EventCandidate, market: str = "kr", symbol: str = "005930"):
    return [key.key for key in assign_event_keys(market, symbol, items)]


@pytest.mark.parametrize(
    "follow_up",
    [
        "[속보] 삼성전자 2조원 공급계약 체결",
        "삼성전자, 2조원 공급계약 체결(종합)",
        "삼성전자 2조원 공급계약 체결 (종합2보)",
        "속보: 삼성전자 2조원 ‘공급계약’ 체결…",
        "[단독]【상보】삼성전자 2조원 공급계약 체결.",
    ],
)
def test_follow_up_headlines_share_a_key(follow_up):
    first, second = _keys(
        _item("삼성전자, 2조원 공급계약 체결", url="https://e/1"),
        _item(follow_up, url="https://e/2"),
    )

    assert first == second


def test_unrelated_articles_get_different_keys():
    keys = _keys(
        _item("삼성전자, 2조원 공급계약 체결", url="https://e/1"),
        _item("삼성전자, 자사주 1조원 매입 결정", url="https://e/2"),
    )

    assert len(set(keys)) == 2


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("삼성전자 장중 +3% 마감 전망", "삼성전자 장중 -3% 마감 전망"),
        ("삼성전자 목표가 8만원 상향", "삼성전자 목표가 9만원 상향"),
        ("삼성전자(005930) 신고가 경신", "삼성전자(005935) 신고가 경신"),
    ],
)
def test_meaningful_differences_survive_normalisation(left, right):
    assert normalize_event_title(left) != normalize_event_title(right)


def test_same_title_on_another_market_day_or_symbol_is_another_event():
    title = "삼성전자 52주 신고가 경신"
    today = _item(title, url="https://e/1")
    later = _item(title, url="https://e/2", at=datetime(2026, 11, 27, 3, tzinfo=UTC))

    assert len(set(_keys(today, later))) == 2
    assert _keys(today) != _keys(today, symbol="000660")


def test_market_day_is_local_not_utc():
    title = "Apple wins large cloud contract"
    # 23:30 UTC and 00:30 UTC straddle UTC midnight but are one New York day.
    items = (
        _item(title, url="https://e/1", at=datetime(2026, 11, 26, 23, 30, tzinfo=UTC)),
        _item(title, url="https://e/2", at=datetime(2026, 11, 27, 0, 30, tzinfo=UTC)),
    )

    assert len(set(_keys(*items, market="us", symbol="AAPL"))) == 1
    assert len(set(_keys(*items, market="crypto", symbol="KRW-BTC"))) == 2


def test_same_receipt_groups_rewritten_headlines_across_days():
    documents = {"rcept_no": [RECEIPT]}
    keys = _keys(
        _item("삼성전자 대규모 수주 공시", url="https://e/1", documents=documents),
        _item(
            "삼성, 2조 계약 따냈다 시장 반응",
            url="https://e/2",
            at=datetime(2026, 11, 28, tzinfo=UTC),
            documents={"dart_id": RECEIPT, "document_id": "provider-77"},
        ),
    )

    assert keys == [f"ev1:kr:005930:doc:dart:{RECEIPT}"] * 2


def test_follow_up_without_an_id_joins_the_receipt_key():
    title = "삼성전자 2조원 공급계약 체결"
    keys = _keys(
        _item(f"[속보] {title}", url="https://e/1"),
        _item(title, url="https://e/2", documents={"rcept_no": [RECEIPT]}),
    )

    assert keys == [f"ev1:kr:005930:doc:dart:{RECEIPT}"] * 2


def test_ambiguous_title_bucket_does_not_pick_a_receipt():
    title = "삼성전자 주요사항 보고서 제출"
    keys = _keys(
        _item(title, url="https://e/1", documents={"rcept_no": [RECEIPT]}),
        _item(title, url="https://e/2", documents={"rcept_no": ["20261126000102"]}),
        _item(title, url="https://e/3"),
    )

    assert len(set(keys)) == 3
    assert ":title:" in keys[2]


@pytest.mark.parametrize(
    "documents",
    [
        {"rcept_no": [RECEIPT, "20261126000102"]},  # round-up
        {"document_id": "provider-77"},  # not a disclosure id
        {"rcept_no": "not-a-receipt"},
        {"rcept_no": [True, {"x": 1}]},
        None,
    ],
)
def test_no_single_disclosure_reference(documents):
    assert single_disclosure_ref(documents) is None


@pytest.mark.parametrize(
    ("title", "at"),
    [("삼성전자 2조원 공급계약 체결", None), ("[속보] 공시", NOON_KST)],
)
def test_unknown_time_or_generic_title_is_never_merged(title, at):
    keys = assign_event_keys(
        "kr",
        "005930",
        [
            _item(title, url="https://e/1", at=at),
            _item(title, url="https://e/2", at=at),
        ],
    )

    assert keys[0].key != keys[1].key
    assert {key.basis for key in keys} == {"article"}


def test_keys_do_not_depend_on_order_or_other_items():
    left = _item("삼성전자 2조원 공급계약 체결", url="https://e/1")
    right = _item("삼성전자 자사주 1조원 매입", url="https://e/2")

    assert _keys(left, right) == list(reversed(_keys(right, left)))
    assert _keys(left, right)[0] == _keys(left)[0]
