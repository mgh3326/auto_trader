"""hk 6503 — KIS 국내 종목정보 파일(kospi_code.mst / kosdaq_code.mst) flag tail.

Layout source (public spec, read for this change):
  github.com/koreainvestment/open-trading-api
  stocks_info/kis_kospi_code_mst.py, stocks_info/kis_kosdaq_code_mst.py
Each row is ``head + tail``. The tail is the last 228 (KOSPI) or 222 (KOSDAQ)
characters and is read with the fixed-width field table in that spec. Offsets
below are counted from the start of the tail, as the spec's ``read_fwf`` does.

The spec's widths sum to 227 / 221, one byte short of the tail length the
existing universe parser uses (228 / 222). The trailing byte is unused by the
spec, so the flag offsets are unaffected.

[unverified] The spec documents field names and widths, not the sample values.
The code values used here (``Y``/``N`` for 거래정지·정리매매·관리종목, and
``00``/``01``/``02``/``03`` for 시장경고) follow the spec's names and Korean
KRX convention. A live smoke should confirm them before the flags drive any
screener exclusion. A value outside the map is stored raw as ``unmapped``.

Pure module: no network, no DB.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.kr_trading_flags import vocabulary as v

KOSPI_TAIL_LENGTH = 228
KOSDAQ_TAIL_LENGTH = 222


@dataclass(frozen=True)
class _Field:
    offset: int
    width: int


#: Tail offsets (spec column names in comments).
_LAYOUT: dict[str, dict[str, _Field]] = {
    "KOSPI": {
        "trading_halt": _Field(60, 1),  # 거래정지
        "delisting_trade": _Field(61, 1),  # 정리매매
        "management": _Field(62, 1),  # 관리종목
        "market_alert": _Field(63, 2),  # 시장경고
    },
    "KOSDAQ": {
        "trading_halt": _Field(55, 1),  # 거래정지 여부
        "delisting_trade": _Field(56, 1),  # 정리매매 여부
        "management": _Field(57, 1),  # 관리 종목 여부
        "market_alert": _Field(58, 2),  # 시장 경고 구분 코드
    },
}

_TAIL_LENGTH: dict[str, int] = {
    "KOSPI": KOSPI_TAIL_LENGTH,
    "KOSDAQ": KOSDAQ_TAIL_LENGTH,
}


def _symbol_from_head(head: str) -> str | None:
    symbol = head[0:9].rstrip().upper()
    if len(symbol) != 6 or not symbol.isalnum():
        return None
    return symbol


def parse_kis_master_line(
    line: str, *, exchange: str
) -> list[v.FlagObservation] | None:
    """Return the four flag observations for one master row, or ``None``.

    ``None`` means the row is not a usable symbol row (too short, or no valid
    short code). The caller counts it as skipped. The store stamps the
    observation time from the download, not from this parser.
    """
    if exchange not in _TAIL_LENGTH:
        raise ValueError(f"unknown KIS master exchange: {exchange}")
    tail_len = _TAIL_LENGTH[exchange]
    if len(line) <= tail_len:
        return None
    head = line[: len(line) - tail_len]
    tail = line[len(line) - tail_len :]
    symbol = _symbol_from_head(head)
    if symbol is None:
        return None

    layout = _LAYOUT[exchange]

    def field(name: str) -> str:
        spec = layout[name]
        return tail[spec.offset : spec.offset + spec.width]

    obs: list[v.FlagObservation] = []

    state, raw = v.map_management_flag_yn(field("management"))
    obs.append(
        v.FlagObservation(
            symbol=symbol,
            category=v.CATEGORY_MANAGEMENT,
            state=state,
            source=v.SOURCE_KIS_MASTER,
            raw_code=raw,
        )
    )
    state, raw = v.map_market_alert_code(field("market_alert"))
    obs.append(
        v.FlagObservation(
            symbol=symbol,
            category=v.CATEGORY_MARKET_ALERT,
            state=state,
            source=v.SOURCE_KIS_MASTER,
            raw_code=raw,
        )
    )
    state, raw = v.map_delisting_trade_yn(field("delisting_trade"))
    obs.append(
        v.FlagObservation(
            symbol=symbol,
            category=v.CATEGORY_DELISTING_TRADE,
            state=state,
            source=v.SOURCE_KIS_MASTER,
            raw_code=raw,
        )
    )
    state, raw = v.map_trading_halt_yn(field("trading_halt"))
    obs.append(
        v.FlagObservation(
            symbol=symbol,
            category=v.CATEGORY_TRADING_HALT,
            state=state,
            source=v.SOURCE_KIS_MASTER,
            raw_code=raw,
        )
    )
    return obs
