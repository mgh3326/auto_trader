# app/services/brokers/kiwoom/chart_compare.py
"""Mock-vs-live Kiwoom chart comparison harness (Stage 1a: offline only).

Pure comparison logic plus an orchestrator whose fetchers are injected, so this
module performs **no network I/O of its own** and is fully exercised by fixtures.
Stage 1b supplies real fetchers; nothing here calls a broker.

Design rule carried over from the brief: when mock and live disagree, this
module never declares a winner. A row is ``MATCH`` / ``MISMATCH`` only when an
independent third source (the frozen KIS sample) can adjudicate it; otherwise it
is ``UNDETERMINED``.

Unit notes taken from the official Kiwoom docs (ka10080-ka10083):

* Minute rows carry a *sign prefix* on price fields (``"-78800"``) that encodes
  전일대비 direction, not a negative price. Magnitude is compared, not the sign.
* Daily ``trde_prica`` is 백만원, while weekly/monthly ``trde_prica`` is raw 원
  (the official examples differ this way). Mock-vs-live comparison is unaffected
  — both sides use the same unit — but the frozen KIS ``value`` column is raw 원,
  so ``trde_prica`` is deliberately left OUT of the third-source crosscheck: the
  백만원 rounding makes exact adjudication impossible, and a scaled comparison
  would manufacture false MISMATCHes. OHLC + volume carry the crosscheck.
"""

from __future__ import annotations

import csv
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Final

# --------------------------------------------------------------------------
# Stage 1b execution envelope (constants only — nothing here executes them)
# --------------------------------------------------------------------------

#: fable measured HTTPStatusError on rapid consecutive calls; 2.0s was clean.
MIN_CALL_INTERVAL_SECONDS: Final[float] = 2.0

#: Hard ceiling on total broker calls for a Stage 1b run (mock + live combined).
MAX_TOTAL_CALLS: Final[int] = 200

#: Stage 1b runs strictly serially.
MAX_CONCURRENCY: Final[int] = 1

#: Comparison breadth: 10 KOSPI + 10 KOSDAQ.
SYMBOLS_PER_MARKET: Final[int] = 10

#: Symbols the frozen KIS sample can adjudicate (3-way crosscheck).
CROSSCHECKABLE_SYMBOLS: Final[frozenset[str]] = frozenset(
    {"005930", "000660", "035420", "068270"}
)

#: Read-only artifact. Never written or moved by this code.
FROZEN_KIS_SAMPLE_PATH: Final[Path] = Path(
    "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/crosscheck/kis_frozen_sample.csv"
)
FROZEN_KIS_SAMPLE_SHA256: Final[str] = (
    "e648cffb1aeb501939b1ebb89abaec820f3833c52705bdb9651ba045d851f0b4"
)

#: Delisted control: mock returned a 1-row EMPTY response for this symbol.
DELISTED_PROBE_SYMBOL: Final[str] = "051170"


class ChartKind(Enum):
    """Chart TR, its response list key, row key field, and compared fields."""

    DAILY = ("ka10081", "stk_dt_pole_chart_qry", "dt")
    MINUTE = ("ka10080", "stk_min_pole_chart_qry", "cntr_tm")
    WEEKLY = ("ka10082", "stk_stk_pole_chart_qry", "dt")
    MONTHLY = ("ka10083", "stk_mth_pole_chart_qry", "dt")

    def __init__(self, api_id: str, list_key: str, key_field: str) -> None:
        self.api_id = api_id
        self.list_key = list_key
        self.key_field = key_field


#: Price/volume fields whose sign prefix is a direction marker, not a negation.
_MAGNITUDE_FIELDS: Final[tuple[str, ...]] = (
    "cur_prc",
    "open_pric",
    "high_pric",
    "low_pric",
    "trde_qty",
    "trde_prica",
    "acc_trde_qty",
)

#: Fields whose sign is meaningful.
_SIGNED_FIELDS: Final[tuple[str, ...]] = ("pred_pre",)


class Verdict(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNDETERMINED = "UNDETERMINED"


# --------------------------------------------------------------------------
# Value normalization
# --------------------------------------------------------------------------


def normalize_value(raw: Any, *, field_name: str) -> Decimal | None:
    """Parse a Kiwoom numeric string. Returns ``None`` when unparseable/empty.

    Magnitude fields drop the leading direction sign; signed fields keep it.
    """

    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if field_name in _MAGNITUDE_FIELDS:
        return abs(value)
    return value


def _comparable_fields(rows: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Union of numeric-ish keys present across rows, in stable order."""

    known = (*_MAGNITUDE_FIELDS, *_SIGNED_FIELDS, "pred_pre_sig", "trde_tern_rt")
    present: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in known:
            if key in row and key not in seen:
                seen.add(key)
                present.append(key)
    return tuple(present)


# --------------------------------------------------------------------------
# Pairwise comparison
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldMismatch:
    row_key: str
    field_name: str
    mock_raw: Any
    live_raw: Any


@dataclass(frozen=True)
class ChartComparison:
    """Field-by-field mock-vs-live result. Deliberately opinion-free."""

    symbol: str
    kind: ChartKind
    mock_row_count: int
    live_row_count: int
    common_row_keys: tuple[str, ...]
    keys_only_in_mock: tuple[str, ...]
    keys_only_in_live: tuple[str, ...]
    compared_fields: tuple[str, ...]
    mismatches: tuple[FieldMismatch, ...]
    mock_return_code: Any = None
    live_return_code: Any = None

    @property
    def rows_identical(self) -> bool:
        return (
            not self.mismatches
            and not self.keys_only_in_mock
            and not self.keys_only_in_live
        )

    @property
    def mismatched_row_keys(self) -> tuple[str, ...]:
        return tuple(sorted({m.row_key for m in self.mismatches}))

    def summary(self) -> str:
        state = "IDENTICAL" if self.rows_identical else "DIFFERS"
        return (
            f"{self.symbol} {self.kind.name}: {state} "
            f"(mock={self.mock_row_count} rows, live={self.live_row_count} rows, "
            f"common={len(self.common_row_keys)}, "
            f"mismatched_rows={len(self.mismatched_row_keys)})"
        )


def extract_rows(payload: dict[str, Any], kind: ChartKind) -> list[dict[str, Any]]:
    """Pull the chart row list out of a raw Kiwoom response payload."""

    rows = payload.get(kind.list_key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _index_rows(
    rows: Sequence[dict[str, Any]], kind: ChartKind
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get(kind.key_field, "")).strip()
        if key:
            indexed[key] = row
    return indexed


def compare_chart_payloads(
    *,
    symbol: str,
    kind: ChartKind,
    mock_payload: dict[str, Any],
    live_payload: dict[str, Any],
) -> ChartComparison:
    """Compare two raw Kiwoom chart responses field by field."""

    mock_rows = extract_rows(mock_payload, kind)
    live_rows = extract_rows(live_payload, kind)
    mock_index = _index_rows(mock_rows, kind)
    live_index = _index_rows(live_rows, kind)

    common = tuple(sorted(set(mock_index) & set(live_index)))
    only_mock = tuple(sorted(set(mock_index) - set(live_index)))
    only_live = tuple(sorted(set(live_index) - set(mock_index)))
    fields = _comparable_fields([*mock_rows, *live_rows])

    mismatches: list[FieldMismatch] = []
    for key in common:
        mock_row = mock_index[key]
        live_row = live_index[key]
        for field_name in fields:
            mock_raw = mock_row.get(field_name)
            live_raw = live_row.get(field_name)
            if mock_raw is None and live_raw is None:
                continue
            mock_value = normalize_value(mock_raw, field_name=field_name)
            live_value = normalize_value(live_raw, field_name=field_name)
            if mock_value is None or live_value is None:
                # Unparseable on either side is only a mismatch if the raw
                # strings also differ; identical junk is not a discrepancy.
                if str(mock_raw or "").strip() == str(live_raw or "").strip():
                    continue
                mismatches.append(FieldMismatch(key, field_name, mock_raw, live_raw))
                continue
            if mock_value != live_value:
                mismatches.append(FieldMismatch(key, field_name, mock_raw, live_raw))

    return ChartComparison(
        symbol=symbol,
        kind=kind,
        mock_row_count=len(mock_rows),
        live_row_count=len(live_rows),
        common_row_keys=common,
        keys_only_in_mock=only_mock,
        keys_only_in_live=only_live,
        compared_fields=fields,
        mismatches=tuple(mismatches),
        mock_return_code=mock_payload.get("return_code"),
        live_return_code=live_payload.get("return_code"),
    )


# --------------------------------------------------------------------------
# Third-source adjudication (frozen KIS sample, read-only)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenBar:
    symbol: str
    session_date: str  # YYYYMMDD
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal  # raw 원


def load_frozen_kis_sample(
    path: Path | str = FROZEN_KIS_SAMPLE_PATH,
) -> dict[tuple[str, str], FrozenBar]:
    """Load the read-only frozen KIS daily sample, keyed by (symbol, YYYYMMDD).

    The file is opened read-only and never modified or moved.
    """

    bars: dict[tuple[str, str], FrozenBar] = {}
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row["symbol"]).strip()
            session_date = str(row["session_date"]).strip().replace("-", "")
            bars[(symbol, session_date)] = FrozenBar(
                symbol=symbol,
                session_date=session_date,
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=Decimal(row["volume"]),
                value=Decimal(row["value"]),
            )
    return bars


@dataclass(frozen=True)
class CrosscheckResult:
    symbol: str
    row_key: str
    verdict: Verdict
    reason: str
    field_verdicts: tuple[tuple[str, Verdict], ...] = ()


#: Kiwoom daily row field -> frozen-sample column.
_DAILY_TO_FROZEN: Final[tuple[tuple[str, str], ...]] = (
    ("open_pric", "open"),
    ("high_pric", "high"),
    ("low_pric", "low"),
    ("cur_prc", "close"),
    ("trde_qty", "volume"),
)


def crosscheck_daily_row_against_frozen(
    *,
    symbol: str,
    row: dict[str, Any],
    frozen: dict[tuple[str, str], FrozenBar],
) -> CrosscheckResult:
    """Adjudicate one Kiwoom daily row against the frozen KIS sample.

    Returns ``UNDETERMINED`` whenever the third source cannot speak — an
    unknown symbol, a date outside the sample, or an unparseable field. This
    function never guesses which side is right.
    """

    row_key = str(row.get("dt", "")).strip()
    if symbol not in CROSSCHECKABLE_SYMBOLS:
        return CrosscheckResult(
            symbol, row_key, Verdict.UNDETERMINED, "symbol not in frozen KIS sample"
        )
    bar = frozen.get((symbol, row_key))
    if bar is None:
        return CrosscheckResult(
            symbol, row_key, Verdict.UNDETERMINED, "session date not in frozen sample"
        )

    field_verdicts: list[tuple[str, Verdict]] = []
    for kiwoom_field, frozen_attr in _DAILY_TO_FROZEN:
        actual = normalize_value(row.get(kiwoom_field), field_name=kiwoom_field)
        if actual is None:
            field_verdicts.append((kiwoom_field, Verdict.UNDETERMINED))
            continue
        expected = getattr(bar, frozen_attr)
        field_verdicts.append(
            (kiwoom_field, Verdict.MATCH if actual == expected else Verdict.MISMATCH)
        )

    verdicts = [v for _, v in field_verdicts]
    if Verdict.MISMATCH in verdicts:
        verdict, reason = Verdict.MISMATCH, "at least one field differs from KIS"
    elif all(v is Verdict.UNDETERMINED for v in verdicts):
        verdict, reason = Verdict.UNDETERMINED, "no field was parseable"
    else:
        verdict, reason = Verdict.MATCH, "all parseable fields match KIS"
    return CrosscheckResult(symbol, row_key, verdict, reason, tuple(field_verdicts))


def adjudicate_mismatches(
    *,
    comparison: ChartComparison,
    mock_payload: dict[str, Any],
    live_payload: dict[str, Any],
    frozen: dict[tuple[str, str], FrozenBar],
) -> tuple[dict[str, Any], ...]:
    """For each mismatched daily row, say which side (if either) KIS supports.

    Rows the frozen sample cannot adjudicate come back ``UNDETERMINED`` on both
    sides — the harness does not pick a winner from mock-vs-live alone.
    """

    if comparison.kind is not ChartKind.DAILY:
        return tuple(
            {
                "row_key": key,
                "mock": Verdict.UNDETERMINED,
                "live": Verdict.UNDETERMINED,
                "reason": "third-source crosscheck is daily-only",
            }
            for key in comparison.mismatched_row_keys
        )

    mock_index = _index_rows(
        extract_rows(mock_payload, comparison.kind), comparison.kind
    )
    live_index = _index_rows(
        extract_rows(live_payload, comparison.kind), comparison.kind
    )

    results: list[dict[str, Any]] = []
    for key in comparison.mismatched_row_keys:
        mock_row = mock_index.get(key, {})
        live_row = live_index.get(key, {})
        mock_verdict = crosscheck_daily_row_against_frozen(
            symbol=comparison.symbol, row=mock_row, frozen=frozen
        )
        live_verdict = crosscheck_daily_row_against_frozen(
            symbol=comparison.symbol, row=live_row, frozen=frozen
        )
        results.append(
            {
                "row_key": key,
                "mock": mock_verdict.verdict,
                "live": live_verdict.verdict,
                "reason": (
                    mock_verdict.reason
                    if mock_verdict.verdict is Verdict.UNDETERMINED
                    else live_verdict.reason
                ),
            }
        )
    return tuple(results)


# --------------------------------------------------------------------------
# Deterministic symbol selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolCandidate:
    symbol: str
    market: str  # "kospi" | "kosdaq"
    turnover: Decimal


def select_comparison_symbols(
    candidates: Iterable[SymbolCandidate],
    *,
    per_market: int = SYMBOLS_PER_MARKET,
) -> tuple[str, ...]:
    """Pick the top-turnover symbols per market, deterministically.

    Ties break on the symbol code ascending, so the same input always yields
    the same output regardless of iteration order.
    """

    by_market: dict[str, list[SymbolCandidate]] = {}
    for candidate in candidates:
        by_market.setdefault(candidate.market.lower(), []).append(candidate)

    picked: list[str] = []
    for market in sorted(by_market):
        ranked = sorted(by_market[market], key=lambda c: (-c.turnover, c.symbol))
        picked.extend(c.symbol for c in ranked[:per_market])
    return tuple(picked)


# --------------------------------------------------------------------------
# Orchestrator (fetchers injected — no network in this module)
# --------------------------------------------------------------------------

FetchFn = Callable[[str], Awaitable[dict[str, Any]]]
SleepFn = Callable[[float], Awaitable[None]]


class CallBudgetExceeded(RuntimeError):
    """Raised when a run would exceed ``MAX_TOTAL_CALLS``."""


@dataclass
class RunReport:
    comparisons: list[ChartComparison] = field(default_factory=list)
    adjudications: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    calls_made: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        return [c.summary() for c in self.comparisons]


async def run_pairwise_comparison(
    *,
    symbols: Sequence[str],
    kind: ChartKind,
    fetch_mock: FetchFn,
    fetch_live: FetchFn,
    frozen: dict[tuple[str, str], FrozenBar] | None = None,
    sleep: SleepFn,
    min_interval_seconds: float = MIN_CALL_INTERVAL_SECONDS,
    max_total_calls: int = MAX_TOTAL_CALLS,
) -> RunReport:
    """Run mock/live comparison serially with a paced, budgeted call loop.

    ``fetch_mock`` / ``fetch_live`` are injected, so Stage 1a exercises this
    with fixtures and Stage 1b supplies real clients. ``sleep`` is injected too
    so tests never actually wait.
    """

    report = RunReport()
    frozen = frozen or {}

    for symbol in symbols:
        if report.calls_made + 2 > max_total_calls:
            raise CallBudgetExceeded(
                f"Stage 1b call budget {max_total_calls} would be exceeded "
                f"at symbol {symbol!r} (calls so far: {report.calls_made})"
            )
        try:
            if report.calls_made:
                await sleep(min_interval_seconds)
            mock_payload = await fetch_mock(symbol)
            report.calls_made += 1

            await sleep(min_interval_seconds)
            live_payload = await fetch_live(symbol)
            report.calls_made += 1
        except Exception as exc:  # noqa: BLE001 - recorded, run continues
            report.errors.append((symbol, type(exc).__name__))
            continue

        comparison = compare_chart_payloads(
            symbol=symbol,
            kind=kind,
            mock_payload=mock_payload,
            live_payload=live_payload,
        )
        report.comparisons.append(comparison)
        if comparison.mismatches:
            report.adjudications[symbol] = adjudicate_mismatches(
                comparison=comparison,
                mock_payload=mock_payload,
                live_payload=live_payload,
                frozen=frozen,
            )
    return report
