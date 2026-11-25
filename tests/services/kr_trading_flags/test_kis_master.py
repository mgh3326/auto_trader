"""hk 6503: KIS master flag tail parser (layout from the public spec)."""

from __future__ import annotations

import pytest

from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.kis_master import (
    KOSDAQ_TAIL_LENGTH,
    KOSPI_TAIL_LENGTH,
    parse_kis_master_line,
)

pytestmark = pytest.mark.unit


def _line(symbol: str, tail_len: int, patch: dict[int, str]) -> str:
    """Build one master row: head (symbol+code+name) + tail with chars at offsets."""
    head = symbol.ljust(9) + "KR7" + symbol.ljust(9, "0")[:9] + "  " + "테스트종목"
    tail = [" "] * tail_len
    for offset, text in patch.items():
        for i, ch in enumerate(text):
            tail[offset + i] = ch
    return head + "".join(tail)


def _states(obs: list[v.FlagObservation]) -> dict[str, tuple[str, str]]:
    return {o.category: (o.state, o.raw_code) for o in obs}


def test_kospi_flags_map_from_spec_offsets() -> None:
    # 거래정지 Y (60), 정리매매 N (61), 관리종목 Y (62), 시장경고 01 (63-64)
    line = _line("224060", KOSPI_TAIL_LENGTH, {60: "Y", 61: "N", 62: "Y", 63: "01"})
    obs = parse_kis_master_line(line, exchange="KOSPI")
    assert obs is not None
    assert {o.symbol for o in obs} == {"224060"}
    assert all(o.source == v.SOURCE_KIS_MASTER for o in obs)
    assert _states(obs) == {
        v.CATEGORY_TRADING_HALT: ("halted", "Y"),
        v.CATEGORY_DELISTING_TRADE: ("none", "N"),
        v.CATEGORY_MANAGEMENT: ("designated", "Y"),
        v.CATEGORY_MARKET_ALERT: ("caution", "01"),
    }


def test_kosdaq_flags_use_kosdaq_offsets() -> None:
    # KOSDAQ: 거래정지 55, 정리매매 56, 관리 57, 시장경고 58-59
    line = _line("068270", KOSDAQ_TAIL_LENGTH, {55: "N", 56: "Y", 57: "N", 58: "03"})
    obs = parse_kis_master_line(line, exchange="KOSDAQ")
    assert obs is not None
    assert _states(obs) == {
        v.CATEGORY_TRADING_HALT: ("none", "N"),
        v.CATEGORY_DELISTING_TRADE: ("in_progress", "Y"),
        v.CATEGORY_MANAGEMENT: ("none", "N"),
        v.CATEGORY_MARKET_ALERT: ("risk", "03"),
    }


def test_blank_or_unexpected_values_are_unmapped_not_false() -> None:
    line = _line("005930", KOSPI_TAIL_LENGTH, {60: " ", 61: "X", 62: "Y", 63: "99"})
    obs = parse_kis_master_line(line, exchange="KOSPI")
    assert obs is not None
    states = _states(obs)
    assert states[v.CATEGORY_TRADING_HALT] == (v.STATE_UNMAPPED, "")
    assert states[v.CATEGORY_DELISTING_TRADE] == (v.STATE_UNMAPPED, "X")
    assert states[v.CATEGORY_MARKET_ALERT] == (v.STATE_UNMAPPED, "99")


def test_short_or_invalid_rows_return_none() -> None:
    assert parse_kis_master_line("short", exchange="KOSPI") is None
    bad_symbol = _line("12", KOSPI_TAIL_LENGTH, {})
    assert parse_kis_master_line(bad_symbol, exchange="KOSPI") is None


def test_unknown_exchange_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_kis_master_line("x" * 300, exchange="NXT")
