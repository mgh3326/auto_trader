"""Parser for NASDAQ Trader symbol directory files (network-free)."""

from __future__ import annotations

import gzip
from datetime import date

import pytest

from research.us_universe import symdir
from research.us_universe.symdir import (
    SERIES_NASDAQ,
    SERIES_OTHER,
    SymbolDirectoryError,
    decode_body,
    parse_symbol_directory,
)

from .us_universe_fixtures import nasdaq_body, other_body

DAY = date(2026, 10, 30)


@pytest.mark.parametrize("old", [False, True])
def test_nasdaq_header_vintages_parse_by_name(old: bool) -> None:
    obs = parse_symbol_directory(SERIES_NASDAQ, nasdaq_body(DAY, ("AAPL",), old=old))
    row = {r.symbol: r for r in obs.rows}["AAPL"]
    assert obs.as_of == DAY
    assert obs.created_at == "2026-10-30T17:03"
    assert row.exchange == "NASDAQ"
    assert row.etf is (None if old else False)


@pytest.mark.parametrize("old", [False, True])
def test_other_header_vintages_parse_by_name(old: bool) -> None:
    obs = parse_symbol_directory(SERIES_OTHER, other_body(DAY, ("BRK.B",), old=old))
    row = {r.symbol: r for r in obs.rows}["BRK.B"]
    assert row.exchange == "NYSE"
    assert row.etf is False


def test_symbols_are_db_dot_format_and_test_issues_dropped() -> None:
    test_issue = "ZVZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N"
    obs = parse_symbol_directory(
        SERIES_NASDAQ, nasdaq_body(DAY, ("BRK-A",), extra=test_issue)
    )
    symbols = {r.symbol for r in obs.rows}
    assert "BRK.A" in symbols and "BRK-A" not in symbols
    assert "ZVZZT" not in symbols


def test_missing_footer_is_rejected_as_truncated() -> None:
    body = nasdaq_body(DAY, ("AAPL",))
    truncated = body[: body.index("File Creation Time")]
    with pytest.raises(SymbolDirectoryError, match="footer"):
        parse_symbol_directory(SERIES_NASDAQ, truncated)


def test_partial_file_below_floor_is_rejected() -> None:
    body = "\n".join(
        [
            "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size",
            "AAPL|Apple Inc. - Common Stock|Q|N|N|100",
            "File Creation Time: 1030202617:03|||||",
        ]
    )
    with pytest.raises(SymbolDirectoryError, match="refusing partial file"):
        parse_symbol_directory(SERIES_NASDAQ, body)


def test_html_error_page_is_rejected() -> None:
    with pytest.raises(SymbolDirectoryError):
        parse_symbol_directory(SERIES_OTHER, "<html><body>Not found</body></html>\n")


def test_gzip_encoded_capture_is_decoded() -> None:
    body = nasdaq_body(DAY, ("AAPL",))
    assert decode_body(gzip.compress(body.encode())) == body


def test_common_stock_heuristic_matches_screener_classifier() -> None:
    from app.services import us_common_stock_classifier as app_cls

    assert symdir.EXCLUDED_SYMBOL_MARKERS == app_cls._EXCLUDED_SYMBOL_MARKERS
    assert symdir.EXCLUDED_NAME_MARKERS == app_cls._EXCLUDED_NAME_MARKERS
    cases = [
        ("AAPL", "Apple Inc. - Common Stock", "N", "N"),
        ("SPY", "SPDR S&P 500 ETF Trust", "Y", "N"),
        ("ABR$D", "Arbor Realty 6.375% Series D Preferred", "N", "N"),
        ("XYZW", "XYZ Corp Warrant", "N", "N"),
        ("ZVZZT", "NASDAQ TEST STOCK", "N", "Y"),
    ]
    for symbol, name, etf, test in cases:
        assert symdir.looks_like_common_stock(
            symbol, name, etf=etf == "Y", test_issue=test == "Y"
        ) == app_cls._looks_like_common_stock(symbol, name, etf=etf, test_issue=test)


def test_symbols_colliding_after_normalisation_are_rejected() -> None:
    body = nasdaq_body(DAY, ("ABC-A",), extra="ABC.A|ABC Class A|Q|N|N|100|N|N")
    with pytest.raises(SymbolDirectoryError, match="both normalise"):
        parse_symbol_directory(SERIES_NASDAQ, body)


def test_exact_duplicate_line_keeps_first_occurrence() -> None:
    body = nasdaq_body(DAY, ("AAA",), extra="AAA|AAA Fund ETF|Q|N|N|100|Y|N")
    rows = {r.symbol: r for r in parse_symbol_directory(SERIES_NASDAQ, body).rows}
    assert rows["AAA"].security_name == "AAA Inc. - Common Stock"
