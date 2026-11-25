"""hk 6503 — closed vocabularies for KR 관리·시장경고·정리매매·거래정지 flags.

Each category has a closed state set. A source code that does not map to a
state is stored as ``unmapped`` with its raw code — never guessed into a state.
An absent observation is *unknown*, not ``none``; ``none`` only ever comes from
an explicit source value (KIS ``N``, Naver ``tradeStopYn=N``, alert ``00``).

Pure module: no DB, no network, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

CATEGORY_MANAGEMENT: Final = "management"  # 관리종목
CATEGORY_MARKET_ALERT: Final = "market_alert"  # 투자주의·투자경고·투자위험
CATEGORY_DELISTING_TRADE: Final = "delisting_trade"  # 정리매매
CATEGORY_TRADING_HALT: Final = "trading_halt"  # 거래정지

CATEGORIES: Final = (
    CATEGORY_MANAGEMENT,
    CATEGORY_MARKET_ALERT,
    CATEGORY_DELISTING_TRADE,
    CATEGORY_TRADING_HALT,
)

STATE_UNMAPPED: Final = "unmapped"

STATES_BY_CATEGORY: Final[dict[str, frozenset[str]]] = {
    CATEGORY_MANAGEMENT: frozenset({"designated", "none", STATE_UNMAPPED}),
    CATEGORY_MARKET_ALERT: frozenset(
        {"none", "caution", "warning", "risk", STATE_UNMAPPED}
    ),
    CATEGORY_DELISTING_TRADE: frozenset({"in_progress", "none", STATE_UNMAPPED}),
    CATEGORY_TRADING_HALT: frozenset({"halted", "none", STATE_UNMAPPED}),
}

SOURCE_KIS_MASTER: Final = "kis_master"
SOURCE_NAVER_INTRADAY: Final = "naver_intraday"
SOURCES: Final = frozenset({SOURCE_KIS_MASTER, SOURCE_NAVER_INTRADAY})

#: States the screener excludes. 투자주의 (caution) is a label only.
EXCLUDED_STATES: Final[dict[str, frozenset[str]]] = {
    CATEGORY_MANAGEMENT: frozenset({"designated"}),
    CATEGORY_MARKET_ALERT: frozenset({"warning", "risk"}),
    CATEGORY_DELISTING_TRADE: frozenset({"in_progress"}),
    CATEGORY_TRADING_HALT: frozenset({"halted"}),
}

#: Korean labels for operator-facing output (identifiers stay English).
STATE_LABEL_KO: Final[dict[tuple[str, str], str]] = {
    (CATEGORY_MANAGEMENT, "designated"): "관리",
    (CATEGORY_MARKET_ALERT, "caution"): "투자주의",
    (CATEGORY_MARKET_ALERT, "warning"): "투자경고",
    (CATEGORY_MARKET_ALERT, "risk"): "투자위험",
    (CATEGORY_DELISTING_TRADE, "in_progress"): "정리매매",
    (CATEGORY_TRADING_HALT, "halted"): "거래정지",
}

#: 시장경고 code -> state. ``00`` = no alert. ``01`` is 투자주의 (observed in
#: the Naver fixture, 224060). ``02``/``03`` are the 투자경고/투자위험 codes by
#: KRX convention and are NOT confirmed by a sample — [unverified].
_MARKET_ALERT_CODES: Final[dict[str, str]] = {
    "00": "none",
    "01": "caution",
    "02": "warning",
    "03": "risk",
}


def map_yn(category: str, raw: str | None, *, yes: str, no: str) -> tuple[str, str]:
    """Map a Y/N-style source value. Returns ``(state, raw_code)``."""
    text = (raw or "").strip().upper()
    if text == "Y":
        return yes, text
    if text == "N":
        return no, text
    return STATE_UNMAPPED, text


def map_management_flag_yn(raw: str | None) -> tuple[str, str]:
    """KIS ``관리종목 여부`` Y/N -> designated / none."""
    return map_yn(CATEGORY_MANAGEMENT, raw, yes="designated", no="none")


def map_trading_halt_yn(raw: str | None) -> tuple[str, str]:
    """``거래정지 여부`` Y/N -> halted / none (KIS and Naver ``tradeStopYn``)."""
    return map_yn(CATEGORY_TRADING_HALT, raw, yes="halted", no="none")


def map_delisting_trade_yn(raw: str | None) -> tuple[str, str]:
    """KIS ``정리매매 여부`` Y/N -> in_progress / none."""
    return map_yn(CATEGORY_DELISTING_TRADE, raw, yes="in_progress", no="none")


def map_market_alert_code(raw: str | None) -> tuple[str, str]:
    """시장경고 two-digit code -> none / caution / warning / risk / unmapped."""
    text = (raw or "").strip()
    state = _MARKET_ALERT_CODES.get(text, STATE_UNMAPPED)
    return state, text


def map_naver_management_status(raw: str | None) -> tuple[str, str]:
    """Naver ``manageStatusGb``. Only ``1`` is mapped (fixture 224060).

    Other values (for example ``3`` on 031860, which also carries a trading
    halt) are stored raw as ``unmapped``; their meaning is not documented.
    """
    text = (raw or "").strip()
    if text == "1":
        return "designated", text
    return STATE_UNMAPPED, text


def is_excluded(category: str, state: str) -> bool:
    return state in EXCLUDED_STATES.get(category, frozenset())


def is_valid_state(category: str, state: str) -> bool:
    return state in STATES_BY_CATEGORY.get(category, frozenset())


@dataclass(frozen=True)
class FlagObservation:
    """One source's reading of one category for one symbol.

    ``raw_code`` is the source value exactly as received (after trimming), kept
    for every mapped and unmapped reading so an operator can audit the mapping.
    ``reason_code`` and ``effective_date`` are source-provided only; they are
    never derived.
    """

    symbol: str
    category: str
    state: str
    source: str
    raw_code: str
    reason_code: str | None = None
    effective_date: date | None = None
