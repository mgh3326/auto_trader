import json

from app.services.action_report.remote_debug_audit.naver_quote import (
    NAVER_EXTRACT_JS,
    NAVER_NAME_SELECTORS,
    NAVER_PRICE_SELECTORS,
    NAVER_READY_JS,
    NaverQuote,
    naver_url,
    parse_naver_quote,
)


def test_naver_url_uses_item_main_with_code() -> None:
    assert (
        naver_url("005930") == "https://finance.naver.com/item/main.naver?code=005930"
    )


def test_parse_valid_json_string() -> None:
    raw = json.dumps({"code": "005930", "name": "삼성전자", "price_text": "81,000"})
    q = parse_naver_quote(raw)
    assert q == NaverQuote(code="005930", name="삼성전자", price=81000.0)


def test_parse_accepts_dict_too() -> None:
    raw = {"code": "000660", "name": "SK하이닉스", "price_text": "175,500"}
    q = parse_naver_quote(raw)
    assert q is not None and q.price == 175500.0


def test_parse_missing_price_returns_quote_with_none_price() -> None:
    raw = json.dumps({"code": "999999", "name": None, "price_text": None})
    q = parse_naver_quote(raw)
    assert q is not None and q.code == "999999" and q.price is None


def test_parse_garbage_returns_none() -> None:
    assert parse_naver_quote("not-json") is None
    assert parse_naver_quote(None) is None
    assert parse_naver_quote(123) is None


def test_extract_js_wires_every_price_and_name_selector_variant() -> None:
    # Legacy selectors preserved, and at least one fallback variant added.
    assert ".no_today .blind" in NAVER_PRICE_SELECTORS
    assert ".wrap_company h2" in NAVER_NAME_SELECTORS
    assert len(NAVER_PRICE_SELECTORS) > 1
    assert len(NAVER_NAME_SELECTORS) > 1
    for sel in NAVER_PRICE_SELECTORS:
        assert sel in NAVER_EXTRACT_JS, sel
    for sel in NAVER_NAME_SELECTORS:
        assert sel in NAVER_EXTRACT_JS, sel


def test_ready_js_probes_price_selectors_and_returns_boolean() -> None:
    for sel in NAVER_PRICE_SELECTORS:
        assert sel in NAVER_READY_JS, sel
    # Returns a boolean (selector present) so the CDP poll loop can gate on it.
    assert "!==null" in NAVER_READY_JS.replace(" ", "")
