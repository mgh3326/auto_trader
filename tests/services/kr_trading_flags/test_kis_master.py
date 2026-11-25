"""hk 6503: KIS master flag tail parser (layout from the public spec).

The rows here are built from the spec's full field-width tables, not from the
parser's own offsets, on newline-stripped lines (what ``_download_mst_lines``
returns). A parser that slices one column too many reads every flag one column
early and fails these tests.
"""

from __future__ import annotations

import pytest

from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.kis_master import (
    KOSDAQ_TAIL_LENGTH,
    KOSPI_TAIL_LENGTH,
    parse_kis_master_line,
)

pytestmark = pytest.mark.unit

# Field widths exactly as in open-trading-api stocks_info/kis_{kospi,kosdaq}_code_mst.py
_KOSPI_WIDTHS = (
    2,
    1,
    4,
    4,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    9,
    5,
    5,
    1,
    1,
    1,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    3,
    1,
    3,
    12,
    12,
    8,
    15,
    21,
    2,
    7,
    1,
    1,
    1,
    1,
    1,
    9,
    9,
    9,
    5,
    9,
    8,
    9,
    3,
    1,
    1,
    1,
)
_KOSDAQ_WIDTHS = (
    2,
    1,
    4,
    4,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    9,
    5,
    5,
    1,
    1,
    1,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    3,
    1,
    3,
    12,
    12,
    8,
    15,
    21,
    2,
    7,
    1,
    1,
    1,
    1,
    9,
    9,
    9,
    5,
    9,
    8,
    9,
    3,
    1,
    1,
    1,
)
# Field index in the spec's column list (0-based).
_KOSPI_IDX = {
    "trading_halt": 34,
    "delisting_trade": 35,
    "management": 36,
    "market_alert": 37,
}
_KOSDAQ_IDX = {
    "trading_halt": 29,
    "delisting_trade": 30,
    "management": 31,
    "market_alert": 32,
}


def test_tail_lengths_are_the_spec_width_sums() -> None:
    assert sum(_KOSPI_WIDTHS) == KOSPI_TAIL_LENGTH == 227
    assert sum(_KOSDAQ_WIDTHS) == KOSDAQ_TAIL_LENGTH == 221


def _offsets(widths: tuple[int, ...]) -> list[int]:
    out, total = [], 0
    for w in widths:
        out.append(total)
        total += w
    return out


def _row(
    symbol: str, widths: tuple[int, ...], idx: dict[str, int], values: dict[str, str]
) -> str:
    """A newline-stripped master row: head (symbol, code, name) + tail at spec widths."""
    head = symbol.ljust(9) + "KR7" + symbol.ljust(9, "0")[:9] + "  " + "테스트종목"
    tail = [" "] * sum(widths)
    offsets = _offsets(widths)
    for field, text in values.items():
        start = offsets[idx[field]]
        width = widths[idx[field]]
        for i, ch in enumerate(text.ljust(width)[:width]):
            tail[start + i] = ch
    return head + "".join(tail)


def _states(obs: list[v.FlagObservation]) -> dict[str, tuple[str, str]]:
    return {o.category: (o.state, o.raw_code) for o in obs}


def test_kospi_full_width_row_maps_every_flag() -> None:
    line = _row(
        "224060",
        _KOSPI_WIDTHS,
        _KOSPI_IDX,
        {
            "trading_halt": "N",
            "delisting_trade": "N",
            "management": "Y",
            "market_alert": "01",
        },
    )
    obs = parse_kis_master_line(line, exchange="KOSPI")
    assert obs is not None
    assert {o.symbol for o in obs} == {"224060"}
    assert all(o.source == v.SOURCE_KIS_MASTER for o in obs)
    assert _states(obs) == {
        v.CATEGORY_TRADING_HALT: ("none", "N"),
        v.CATEGORY_DELISTING_TRADE: ("none", "N"),
        v.CATEGORY_MANAGEMENT: ("designated", "Y"),
        v.CATEGORY_MARKET_ALERT: ("caution", "01"),
    }


def test_kospi_halted_row_is_halted_not_delisting() -> None:
    line = _row(
        "031860",
        _KOSPI_WIDTHS,
        _KOSPI_IDX,
        {
            "trading_halt": "Y",
            "delisting_trade": "N",
            "management": "N",
            "market_alert": "00",
        },
    )
    states = _states(parse_kis_master_line(line, exchange="KOSPI") or [])
    assert states[v.CATEGORY_TRADING_HALT] == ("halted", "Y")
    assert states[v.CATEGORY_DELISTING_TRADE] == ("none", "N")
    assert states[v.CATEGORY_MANAGEMENT] == ("none", "N")
    assert states[v.CATEGORY_MARKET_ALERT] == ("none", "00")


def test_kosdaq_full_width_row_uses_kosdaq_offsets() -> None:
    line = _row(
        "068270",
        _KOSDAQ_WIDTHS,
        _KOSDAQ_IDX,
        {
            "trading_halt": "N",
            "delisting_trade": "Y",
            "management": "N",
            "market_alert": "03",
        },
    )
    obs = parse_kis_master_line(line, exchange="KOSDAQ")
    assert obs is not None
    assert _states(obs) == {
        v.CATEGORY_TRADING_HALT: ("none", "N"),
        v.CATEGORY_DELISTING_TRADE: ("in_progress", "Y"),
        v.CATEGORY_MANAGEMENT: ("none", "N"),
        v.CATEGORY_MARKET_ALERT: ("risk", "03"),
    }


def test_blank_or_unexpected_values_are_unmapped_not_false() -> None:
    line = _row(
        "005930",
        _KOSPI_WIDTHS,
        _KOSPI_IDX,
        {
            "trading_halt": " ",
            "delisting_trade": "X",
            "management": "Y",
            "market_alert": "99",
        },
    )
    states = _states(parse_kis_master_line(line, exchange="KOSPI") or [])
    assert states[v.CATEGORY_TRADING_HALT] == (v.STATE_UNMAPPED, "")
    assert states[v.CATEGORY_DELISTING_TRADE] == (v.STATE_UNMAPPED, "X")
    assert states[v.CATEGORY_MARKET_ALERT] == (v.STATE_UNMAPPED, "99")


def test_short_or_invalid_rows_return_none() -> None:
    assert parse_kis_master_line("short", exchange="KOSPI") is None
    bad_symbol = _row("12", _KOSPI_WIDTHS, _KOSPI_IDX, {})
    assert parse_kis_master_line(bad_symbol, exchange="KOSPI") is None


def test_unknown_exchange_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_kis_master_line("x" * 300, exchange="NXT")
