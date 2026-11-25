"""hk 6345: Go(fillwire market-tap JSONL) ↔ Python(research 분봉 표) 느슨한 전환 비교 (순수).

설계 `docs/design/6321-go-quote-collector.md` §6 의 side-by-side replay 중 판정 로직만
담는다. 입력은 이미 읽힌 행이고 DB·네트워크·파일 접근이 없다. 읽기 CLI 는
`scripts/compare_go_py_minutes.py` 가 맡는다.

판정(운영자 2026-11-25 완화 기준):
- BLOCK: 한쪽에는 있고 다른 쪽에는 **통째로** 없는 종목 또는 (종목, venue, 거래일) 세션.
- PASS: 그 밖의 전부. 행 수 허용오차 밖, 표본 OHLCV 불일치, value 차이, 부분 결손,
  수신 지연은 전부 `listed` 로만 남고 막지 않는다.

Go JSONL 행에는 수신 시각이 없다(`internal/marketminute/row.go` 직렬화 순서 고정).
그래서 수신 지연 차이는 계산하지 않고 `unavailable` 로 보고한다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

PASS: Final = "PASS"
BLOCK: Final = "BLOCK"

# 세션별 행 수 허용오차: max(절대 2 봉, 큰 쪽 행 수의 2% 올림). 상세는 런북.
COUNT_TOLERANCE_ABS: Final = 2
COUNT_TOLERANCE_REL: Final = Decimal("0.02")

KR_VENUES: Final = frozenset({"KRX", "NTX"})
US_VENUES: Final = frozenset({"NASD", "NYSE", "AMEX"})
PRICE_FIELDS: Final = ("open", "high", "low", "close", "volume")

# Go 쪽 실패 코드 → hk 6299 진단 어휘(fetch_error·no_trades_reported·cut_by_budget·
# not_selected). 어휘에 없는 코드는 None 으로 두고 이유를 적는다. 6299 어휘는
# 0행 요청 종목의 사유 네 개뿐이라 이 표가 전부 1:1 이 아니다.
FAILURE_CODE_MAP: Final[Mapping[str, tuple[str | None, str]]] = {
    "rate_limited": (
        "fetch_error",
        "429·KIS EGW00201. 6299 에 rate 어휘가 없어 fetch_error 로 접고 코드는 보고에 남긴다.",
    ),
    "circuit_open": (
        "fetch_error",
        "회로 열림. 6299 에 어휘가 없어 fetch_error 로 접고 코드는 보고에 남긴다.",
    ),
    "budget_cut": ("cut_by_budget", "구독 슬롯·예산 부족으로 대상에서 빠짐."),
    "feed_gap": (
        None,
        "웹소켓 끊김/무수신 또는 기대 분 누락. 0행 사유가 아니라 갭 구간이므로 6299 에 대응 없음.",
    ),
    "no_trades": (
        "no_trades_reported",
        "브로커가 정상 응답했으나 그 분 거래 없음. 결손 아님.",
    ),
    "fetch_error": ("fetch_error", "그 밖의 오류. 예외 클래스만 기록."),
    "unsupported": (
        None,
        "소스가 세션·TR 을 제공하지 않음(예: 야간 선물). 6299 어휘가 아니라 unsupported 그대로 보고.",
    ),
    # 아래 네 개는 Go 클라이언트가 내는 클래스(internal/marketminute/client.go)로
    # 티켓 목록 밖이다. 전부 fetch_error 계열로 접는다.
    "token_unavailable": ("fetch_error", "토큰 없음(티켓 목록 밖의 Go 클래스)."),
    "malformed_response": ("fetch_error", "응답 파싱 불가(티켓 목록 밖의 Go 클래스)."),
    "canceled": ("fetch_error", "취소/시간 초과(티켓 목록 밖의 Go 클래스)."),
    "credential_host": (
        "fetch_error",
        "자격증명 호스트 거부(티켓 목록 밖의 Go 클래스).",
    ),
}

SESSION_DIAGNOSTIC_WORDS: Final = frozenset(
    {"fetch_error", "no_trades_reported", "cut_by_budget", "not_selected"}
)


class CompareInputError(ValueError):
    """입력 행이 비교 계약을 지키지 않는다(형식·정밀도·시각 오류)."""


@dataclass(frozen=True)
class MinuteBar:
    """양쪽 소스 공통 정규화 행. 가격은 정확한 Decimal, 시각은 UTC 봉 시작."""

    time_utc: datetime
    session_date: date
    symbol: str
    venue: str
    session_segment: str
    source: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal | None

    @property
    def key(self) -> tuple[datetime, str, str]:
        return (self.time_utc, self.symbol, self.venue)

    @property
    def session(self) -> tuple[str, str, date]:
        return (self.symbol, self.venue, self.session_date)


def _decimal(name: str, raw: object) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, str | Decimal):
        raise CompareInputError(f"{name}: exact decimal string or Decimal required")
    try:
        number = Decimal(raw) if isinstance(raw, str) else raw
    except InvalidOperation as exc:
        raise CompareInputError(f"{name}: not a decimal") from exc
    if not number.is_finite():
        raise CompareInputError(f"{name}: non-finite value")
    return number


def _optional_decimal(name: str, raw: object) -> Decimal | None:
    if raw is None:
        return None
    return _decimal(name, raw)


def _utc(name: str, raw: object) -> datetime:
    if isinstance(raw, str):
        if not raw.endswith("Z"):
            raise CompareInputError(f"{name}: UTC 'Z' suffix required")
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise CompareInputError(f"{name}: not RFC 3339") from exc
    elif isinstance(raw, datetime):
        parsed = raw
    else:
        raise CompareInputError(f"{name}: timestamp required")
    if parsed.tzinfo is None:
        raise CompareInputError(f"{name}: timezone-aware timestamp required")
    return parsed.astimezone(UTC)


def _session_date(name: str, raw: object) -> date:
    if isinstance(raw, datetime):
        raise CompareInputError(f"{name}: date required, not datetime")
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise CompareInputError(f"{name}: not YYYY-MM-DD") from exc
    raise CompareInputError(f"{name}: date required")


def _text(name: str, raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise CompareInputError(f"{name}: non-empty string required")
    return raw


def _bar(row: Mapping[str, object]) -> MinuteBar:
    return MinuteBar(
        time_utc=_utc("time_utc", row.get("time_utc")),
        session_date=_session_date("session_date", row.get("session_date")),
        symbol=_text("symbol", row.get("symbol")),
        venue=_text("venue", row.get("venue")),
        session_segment=_text("session_segment", row.get("session_segment")),
        source=_text("source", row.get("source")),
        open=_decimal("open", row.get("open")),
        high=_decimal("high", row.get("high")),
        low=_decimal("low", row.get("low")),
        close=_decimal("close", row.get("close")),
        volume=_decimal("volume", row.get("volume")),
        value=_optional_decimal("value", row.get("value")),
    )


def parse_go_row(row: Mapping[str, object]) -> MinuteBar:
    """Go JSONL 한 줄(dict)을 정규화한다. 10진 필드는 문자열이어야 한다."""
    return _bar(row)


def parse_python_row(row: Mapping[str, object]) -> MinuteBar:
    """research 표 한 행(dict, asyncpg 값)을 정규화한다. NUMERIC 은 Decimal 로 온다."""
    return _bar(row)


@dataclass(frozen=True)
class SessionCount:
    symbol: str
    venue: str
    session_date: date
    go_rows: int
    py_rows: int
    within_tolerance: bool


@dataclass(frozen=True)
class CompareReport:
    verdict: str
    missing_symbols_on_go: tuple[str, ...]
    missing_symbols_on_py: tuple[str, ...]
    missing_sessions_on_go: tuple[tuple[str, str, date], ...]
    missing_sessions_on_py: tuple[tuple[str, str, date], ...]
    sessions: tuple[SessionCount, ...]
    counts_outside_tolerance: tuple[tuple[str, str, date], ...]
    matched_bars: int
    ohlcv_equal: int
    ohlcv_mismatch: int
    ohlcv_mismatch_keys: tuple[str, ...]
    value_equal: int
    value_differs: int
    value_one_side_null: int
    segment_mismatch: int
    source_mismatch: int
    rows_missing_on_go: int
    rows_missing_on_py: int
    partial_gap_keys_on_go: tuple[str, ...]
    partial_gap_keys_on_py: tuple[str, ...]
    duplicate_go_keys: int
    duplicate_py_keys: int
    latency: Mapping[str, str] = field(
        default_factory=lambda: {
            "status": "unavailable",
            "reason": "go_jsonl_has_no_received_at",
        }
    )

    @property
    def blocking(self) -> bool:
        return self.verdict == BLOCK


def _tolerance(go_rows: int, py_rows: int) -> int:
    larger = max(go_rows, py_rows)
    relative = math.ceil(COUNT_TOLERANCE_REL * larger)
    return max(COUNT_TOLERANCE_ABS, relative)


def _key_label(key: tuple[datetime, str, str]) -> str:
    time_utc, symbol, venue = key
    return f"{time_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} {symbol} {venue}"


def _dedupe(
    rows: Sequence[MinuteBar],
) -> tuple[dict[tuple[datetime, str, str], MinuteBar], int]:
    kept: dict[tuple[datetime, str, str], MinuteBar] = {}
    duplicates = 0
    for bar in rows:
        if bar.key in kept:
            duplicates += 1
            continue
        kept[bar.key] = bar
    return kept, duplicates


def compare_minutes(
    go: Sequence[MinuteBar],
    py: Sequence[MinuteBar],
    *,
    key_sample_limit: int = 20,
) -> CompareReport:
    """두 소스의 같은 거래일 행을 (time_utc, symbol, venue) 로 맞춰 판정한다.

    `go` 와 `py` 는 같은 거래일·시장으로 미리 걸러진 행이어야 한다.
    """
    go_by_key, duplicate_go = _dedupe(go)
    py_by_key, duplicate_py = _dedupe(py)

    go_symbols = {b.symbol for b in go_by_key.values()}
    py_symbols = {b.symbol for b in py_by_key.values()}
    missing_symbols_on_go = tuple(sorted(py_symbols - go_symbols))
    missing_symbols_on_py = tuple(sorted(go_symbols - py_symbols))

    go_counts: Counter[tuple[str, str, date]] = Counter(
        b.session for b in go_by_key.values()
    )
    py_counts: Counter[tuple[str, str, date]] = Counter(
        b.session for b in py_by_key.values()
    )
    # 종목 자체가 통째로 없는 세션은 종목 단위로만 셈(중복 집계 방지).
    shared_symbols = go_symbols & py_symbols
    all_sessions = set(go_counts) | set(py_counts)
    sessions_in_shared = {s for s in all_sessions if s[0] in shared_symbols}
    missing_sessions_on_go = tuple(
        sorted(s for s in sessions_in_shared if py_counts[s] and not go_counts[s])
    )
    missing_sessions_on_py = tuple(
        sorted(s for s in sessions_in_shared if go_counts[s] and not py_counts[s])
    )

    session_rows: list[SessionCount] = []
    outside: list[tuple[str, str, date]] = []
    for session in sorted(sessions_in_shared):
        g, p = go_counts[session], py_counts[session]
        if not (g and p):
            continue
        within = abs(g - p) <= _tolerance(g, p)
        session_rows.append(
            SessionCount(
                symbol=session[0],
                venue=session[1],
                session_date=session[2],
                go_rows=g,
                py_rows=p,
                within_tolerance=within,
            )
        )
        if not within:
            outside.append(session)

    ohlcv_equal = ohlcv_mismatch = value_equal = value_differs = 0
    value_one_side_null = segment_mismatch = source_mismatch = 0
    mismatch_keys: list[str] = []
    matched = 0
    for key, g_bar in go_by_key.items():
        p_bar = py_by_key.get(key)
        if p_bar is None:
            continue
        matched += 1
        if all(getattr(g_bar, f) == getattr(p_bar, f) for f in PRICE_FIELDS):
            ohlcv_equal += 1
        else:
            ohlcv_mismatch += 1
            if len(mismatch_keys) < key_sample_limit:
                mismatch_keys.append(_key_label(key))
        if g_bar.value is None and p_bar.value is None:
            value_equal += 1
        elif g_bar.value is None or p_bar.value is None:
            value_one_side_null += 1
        elif g_bar.value == p_bar.value:
            value_equal += 1
        else:
            value_differs += 1
        if g_bar.session_segment != p_bar.session_segment:
            segment_mismatch += 1
        if g_bar.source != p_bar.source:
            source_mismatch += 1

    # 한 쪽에만 있는 행. 통째로 빠진 세션·종목 안의 행은 세지 않는다(위에서 이미 차단).
    blocked_sessions = set(missing_sessions_on_go) | set(missing_sessions_on_py)

    def _partial(
        own: Mapping[tuple[datetime, str, str], MinuteBar],
        other: Mapping[tuple[datetime, str, str], MinuteBar],
    ) -> list[tuple[datetime, str, str]]:
        return sorted(
            key
            for key, bar in own.items()
            if key not in other
            and bar.symbol in shared_symbols
            and bar.session not in blocked_sessions
        )

    only_py = _partial(py_by_key, go_by_key)
    only_go = _partial(go_by_key, py_by_key)

    blocking = bool(
        missing_symbols_on_go
        or missing_symbols_on_py
        or missing_sessions_on_go
        or missing_sessions_on_py
    )
    return CompareReport(
        verdict=BLOCK if blocking else PASS,
        missing_symbols_on_go=missing_symbols_on_go,
        missing_symbols_on_py=missing_symbols_on_py,
        missing_sessions_on_go=missing_sessions_on_go,
        missing_sessions_on_py=missing_sessions_on_py,
        sessions=tuple(session_rows),
        counts_outside_tolerance=tuple(outside),
        matched_bars=matched,
        ohlcv_equal=ohlcv_equal,
        ohlcv_mismatch=ohlcv_mismatch,
        ohlcv_mismatch_keys=tuple(mismatch_keys),
        value_equal=value_equal,
        value_differs=value_differs,
        value_one_side_null=value_one_side_null,
        segment_mismatch=segment_mismatch,
        source_mismatch=source_mismatch,
        rows_missing_on_go=len(only_py),
        rows_missing_on_py=len(only_go),
        partial_gap_keys_on_go=tuple(_key_label(k) for k in only_py[:key_sample_limit]),
        partial_gap_keys_on_py=tuple(_key_label(k) for k in only_go[:key_sample_limit]),
        duplicate_go_keys=duplicate_go,
        duplicate_py_keys=duplicate_py,
    )


@dataclass(frozen=True)
class CutoverRow:
    check: str
    threshold: str
    status: str
    blocking: bool


def cutover_table(report: CompareReport) -> tuple[CutoverRow, ...]:
    """운영자 스모크 수준 전환 표. 막는 행은 종목·세션 커버 두 개뿐이다."""
    symbols_status = (
        "BLOCK"
        if report.missing_symbols_on_go or report.missing_symbols_on_py
        else "ok"
    )
    sessions_status = (
        "BLOCK"
        if report.missing_sessions_on_go or report.missing_sessions_on_py
        else "ok"
    )
    tolerance_pct = format(float(COUNT_TOLERANCE_REL) * 100, "g")
    return (
        CutoverRow(
            "같은 종목 커버 (양방향)",
            "통째로 없는 종목 0",
            symbols_status,
            blocking=True,
        ),
        CutoverRow(
            "같은 세션 커버 (양방향)",
            "통째로 없는 (종목, venue, 거래일) 0",
            sessions_status,
            blocking=True,
        ),
        CutoverRow(
            "세션별 행 수",
            f"|go-py| <= max({COUNT_TOLERANCE_ABS} 봉, 큰 쪽의 {tolerance_pct}% 올림)",
            "listed" if report.counts_outside_tolerance else "ok",
            blocking=False,
        ),
        CutoverRow(
            "닫힌 봉 OHLCV 표본 일치",
            "표본 전부 일치(불일치는 목록에만 남김)",
            "listed" if report.ohlcv_mismatch else "ok",
            blocking=False,
        ),
        CutoverRow(
            "value 차이",
            "목록만 기록",
            "listed" if (report.value_differs or report.value_one_side_null) else "ok",
            blocking=False,
        ),
        CutoverRow(
            "수신 지연 차이",
            "Go JSONL 에 수신 시각 없음",
            "unavailable",
            blocking=False,
        ),
        CutoverRow(
            "부분 결손 (한쪽에만 있는 행)",
            "목록만 기록",
            "listed"
            if (report.rows_missing_on_go or report.rows_missing_on_py)
            else "ok",
            blocking=False,
        ),
    )
