"""Measured, read-only corpus loader for the D3 primary exploration matrix."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path, PurePosixPath
from typing import Any

from research.kr_corpus.d3_engine.canonical import canonical_bytes
from research.kr_corpus.d3_engine.constants import (
    DECIMAL_PRECISION,
    DECIMAL_ROUNDING,
    RSI_THRESHOLD,
)
from research.kr_corpus.d3_engine.guards import SealedAccessGuard
from research.kr_corpus.d3_engine.indicators import bollinger_bands, fib_levels
from research.kr_corpus.d3_engine.models import DataView
from research.kr_corpus.d3_engine.signals import (
    PriceLevel,
    choose_l2,
    cluster_levels,
    signal_is_eligible,
)

CORPUS_RUN_ID = "kr-corpus-v1-20260803-1001"

CORPUS_BINDINGS = {
    "original_manifest_sha256": (
        "da1ca376ac6693e96d311eda07f9fe96f1cb69fa2e3e8f346ededf96c5d5c54b"
    ),
    "original_checksums_sha256": (
        "9704cc72455bca8bc8bdea78506b16de4d0cdff697661d7ee8a349eb4b311a7f"
    ),
    "derived_manifest_sha256": (
        "25e2e9a5af85d3389488e5b3464d6d69bfaa877abeae7754ce50b8d23d2fd827"
    ),
    "derived_checksums_sha256": (
        "392a237d2614abd7f5df178d5c03f3c6c77a15af8f22e505fa2ca2ebe5ec2950"
    ),
}

EXPECTED_ROWS = {
    DataView.ORIGINAL_VALID_BAR: 5_525_302,
    DataView.CLAMP_ADMIT_V1: 5_564_715,
}

_BASE_COLUMNS = (
    "session",
    "market",
    "ticker",
    "open",
    "high",
    "low",
    "close",
)
_CLAMP_COLUMNS = (
    "source_high",
    "source_low",
    "clamped",
    "clamp_delta_high",
    "clamp_delta_low",
    "clamp_classification",
    "admitted",
)


class PrimaryCorpusInvalid(ValueError):
    code = "RUN_INVALID_PRIMARY_CORPUS"


@dataclass(frozen=True, slots=True)
class PrimaryCorpusPaths:
    original_root: Path
    derived_root: Path

    @classmethod
    def defaults(cls) -> PrimaryCorpusPaths:
        root = Path.home() / "work" / "herdr-artifacts" / "kr-corpus-v1"
        return cls(
            original_root=root / "runs" / CORPUS_RUN_ID,
            derived_root=root / "derived-views" / "clamp-admit-v1",
        )


@dataclass(frozen=True, slots=True)
class ClampRow:
    market: str
    session: date
    symbol: str
    source_high: int
    source_low: int
    high: int
    low: int
    delta_high: int
    delta_low: int
    classification: str
    admitted: bool


@dataclass(frozen=True, slots=True)
class CorpusBar:
    """Compact bar whose Decimal properties satisfy the E1 engine surface."""

    session: date
    symbol: str
    market: str
    open_int: int
    high_int: int
    low_int: int
    close_int: int

    @property
    def open(self) -> Decimal:
        return Decimal(self.open_int)

    @property
    def high(self) -> Decimal:
        return Decimal(self.high_int)

    @property
    def low(self) -> Decimal:
        return Decimal(self.low_int)

    @property
    def close(self) -> Decimal:
        return Decimal(self.close_int)


@dataclass(frozen=True, slots=True)
class SignalSnapshot:
    rsi: Decimal
    l2_price: Decimal
    fib_high: Decimal
    fib_low: Decimal
    previous_close: Decimal


@dataclass(frozen=True, slots=True)
class LoadedCorpusView:
    data_view: DataView
    bars: tuple[CorpusBar, ...]
    signals: dict[tuple[date, str], SignalSnapshot]
    clamp_rows: dict[tuple[date, str], ClampRow]
    market_periods: dict[str, tuple[tuple[date, date, str], ...]]
    manifest_sha256: str
    checksums_sha256: str
    parquet_files: int
    row_count: int
    signal_tape_sha256: str
    access_evidence: dict[str, int]


@dataclass(slots=True)
class _WilderState:
    previous: Decimal | None = None
    delta_count: int = 0
    seed_gain: Decimal = Decimal(0)
    seed_loss: Decimal = Decimal(0)
    average_gain: Decimal | None = None
    average_loss: Decimal | None = None
    value: Decimal | None = None

    def add(self, close: int) -> None:
        current = Decimal(close)
        if self.previous is None:
            self.previous = current
            return
        change = current - self.previous
        gain = max(change, Decimal(0))
        loss = max(-change, Decimal(0))
        self.delta_count += 1
        if self.delta_count <= 14:
            self.seed_gain += gain
            self.seed_loss += loss
            if self.delta_count == 14:
                self.average_gain = self.seed_gain / Decimal(14)
                self.average_loss = self.seed_loss / Decimal(14)
                self.value = _rsi_from_averages(self.average_gain, self.average_loss)
        else:
            assert self.average_gain is not None
            assert self.average_loss is not None
            self.average_gain = (self.average_gain * Decimal(13) + gain) / Decimal(14)
            self.average_loss = (self.average_loss * Decimal(13) + loss) / Decimal(14)
            self.value = _rsi_from_averages(self.average_gain, self.average_loss)
        self.previous = current


def _rsi_from_averages(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_loss == 0:
        return Decimal(100) if average_gain > 0 else Decimal(50)
    if average_gain == 0:
        return Decimal(0)
    return Decimal(100) - Decimal(100) / (Decimal(1) + average_gain / average_loss)


class PrimaryCorpusLoader:
    """Load only explicitly bound exploration roots through a measured guard."""

    def __init__(
        self,
        *,
        paths: PrimaryCorpusPaths | None = None,
        guard: SealedAccessGuard | None = None,
        bindings: dict[str, str] | None = None,
        expected_rows: dict[DataView, int] | None = None,
    ) -> None:
        self.paths = paths or PrimaryCorpusPaths.defaults()
        self.guard = guard or SealedAccessGuard()
        self.bindings = bindings or dict(CORPUS_BINDINGS)
        self.expected_rows = expected_rows or dict(EXPECTED_ROWS)

    def load(
        self,
        data_view: DataView,
        *,
        market_sessions: tuple[date, ...],
    ) -> LoadedCorpusView:
        root, manifest_expected, checksums_expected = self._binding(data_view)
        root = root.expanduser().resolve(strict=True)
        dataset_root = (root / "dataset").resolve(strict=True)
        if dataset_root.parent != root:
            raise PrimaryCorpusInvalid("dataset root escaped bound corpus root")

        manifest_path = root / "manifest.json"
        manifest_raw = self.guard.read_manifest(
            path=manifest_path, loader=manifest_path.read_bytes
        )
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        if manifest_sha != manifest_expected:
            raise PrimaryCorpusInvalid(
                f"manifest sha drift:{manifest_sha}!={manifest_expected}"
            )
        if data_view is DataView.CLAMP_ADMIT_V1:
            self._validate_derived_manifest(manifest_raw)

        checksums_path = root / "checksums.sha256"
        checksums_raw = self.guard.read_file(
            path=checksums_path, loader=checksums_path.read_bytes
        )
        checksums_sha = hashlib.sha256(checksums_raw).hexdigest()
        if checksums_sha != checksums_expected:
            raise PrimaryCorpusInvalid(
                f"checksums sha drift:{checksums_sha}!={checksums_expected}"
            )
        entries = self._dataset_entries(checksums_raw)

        positions = {session: index for index, session in enumerate(market_sessions)}
        if len(positions) != len(market_sessions):
            raise PrimaryCorpusInvalid("market sessions must be unique")
        by_symbol: dict[str, list[CorpusBar]] = defaultdict(list)
        clamp_rows: dict[tuple[date, str], ClampRow] = {}
        total_rows = 0
        for expected_sha, relative in entries:
            partition = self._partition(relative)
            path = (root / relative).resolve(strict=True)
            if path.parent.parent.parent != dataset_root:
                raise PrimaryCorpusInvalid(f"dataset path escaped root:{relative}")
            actual_sha = self.guard.read_file(
                path=path, loader=lambda path=path: _sha256_stream(path)
            )
            if actual_sha != expected_sha:
                raise PrimaryCorpusInvalid(
                    f"parquet checksum drift:{relative}:{actual_sha}!={expected_sha}"
                )
            rows = self.guard.read_parquet(
                path=path,
                loader=lambda path=path, data_view=data_view: _read_parquet_rows(
                    path, data_view
                ),
            )
            for raw in rows:
                try:
                    decoded_session = date.fromisoformat(str(raw["session"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise PrimaryCorpusInvalid(
                        "invalid corpus parquet session"
                    ) from exc
                self.guard.record_bar_rows((decoded_session,))
                bar, clamp = self._bar_from_row(
                    raw,
                    partition=partition,
                    data_view=data_view,
                    market_positions=positions,
                    market_sessions=market_sessions,
                )
                by_symbol[bar.symbol].append(bar)
                if clamp is not None:
                    key = (bar.session, bar.symbol)
                    if key in clamp_rows:
                        raise PrimaryCorpusInvalid(f"duplicate clamped row:{key}")
                    clamp_rows[key] = clamp
            total_rows += len(rows)

        expected_count = self.expected_rows[data_view]
        if total_rows != expected_count:
            raise PrimaryCorpusInvalid(
                f"row count drift:{total_rows}!={expected_count}"
            )

        signals = _prepare_signal_tape(by_symbol, positions)
        by_session: list[list[CorpusBar]] = [[] for _ in market_sessions]
        market_periods: dict[str, tuple[tuple[date, date, str], ...]] = {}
        for symbol, symbol_bars in sorted(by_symbol.items()):
            symbol_bars.sort(key=lambda item: item.session)
            previous: date | None = None
            for bar in symbol_bars:
                if previous == bar.session:
                    raise PrimaryCorpusInvalid(
                        f"duplicate symbol/session across market partitions:{symbol}:{bar.session}"
                    )
                previous = bar.session
                by_session[positions[bar.session]].append(bar)
            market_periods[symbol] = _market_periods(symbol_bars)
        bars = tuple(
            bar
            for session_bars in by_session
            for bar in sorted(session_bars, key=lambda item: item.symbol)
        )
        if len(bars) != total_rows:
            raise AssertionError("bar materialization count drift")

        return LoadedCorpusView(
            data_view=data_view,
            bars=bars,
            signals=signals,
            clamp_rows=clamp_rows,
            market_periods=market_periods,
            manifest_sha256=manifest_sha,
            checksums_sha256=checksums_sha,
            parquet_files=len(entries),
            row_count=total_rows,
            signal_tape_sha256=_signal_tape_sha(signals),
            access_evidence=self.guard.spy.evidence(),
        )

    def _binding(self, data_view: DataView) -> tuple[Path, str, str]:
        if data_view is DataView.ORIGINAL_VALID_BAR:
            return (
                self.paths.original_root,
                self.bindings["original_manifest_sha256"],
                self.bindings["original_checksums_sha256"],
            )
        if data_view is DataView.CLAMP_ADMIT_V1:
            return (
                self.paths.derived_root,
                self.bindings["derived_manifest_sha256"],
                self.bindings["derived_checksums_sha256"],
            )
        raise PrimaryCorpusInvalid(f"unsupported primary data view:{data_view}")

    def _validate_derived_manifest(self, raw: bytes) -> None:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PrimaryCorpusInvalid(
                "derived manifest is not strict UTF-8 JSON"
            ) from exc
        required = {
            "scope": "main_only",
            "source_corpus_id": "kr-corpus-v1",
            "source_run_id": CORPUS_RUN_ID,
            "source_manifest_sha256": self.bindings["original_manifest_sha256"],
            "checksums_sha256": self.bindings["derived_checksums_sha256"],
            "source_valid_bar_view_unchanged": True,
        }
        for key, expected in required.items():
            actual = self.guard.read_metadata(payload, key)
            if actual != expected:
                raise PrimaryCorpusInvalid(
                    f"derived manifest field drift:{key}:{actual!r}!={expected!r}"
                )

    @staticmethod
    def _dataset_entries(raw: bytes) -> tuple[tuple[str, PurePosixPath], ...]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PrimaryCorpusInvalid("checksum list must be UTF-8") from exc
        entries: list[tuple[str, PurePosixPath]] = []
        seen: set[PurePosixPath] = set()
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                expected, raw_relative = line.split(maxsplit=1)
            except ValueError as exc:
                raise PrimaryCorpusInvalid("malformed checksum row") from exc
            relative = PurePosixPath(raw_relative.lstrip("*"))
            if relative.parts and relative.parts[0] != "dataset":
                continue
            if (
                len(expected) != 64
                or any(character not in "0123456789abcdef" for character in expected)
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.suffix != ".parquet"
            ):
                raise PrimaryCorpusInvalid(f"invalid dataset checksum row:{line}")
            if relative in seen:
                raise PrimaryCorpusInvalid(f"duplicate dataset checksum:{relative}")
            seen.add(relative)
            entries.append((expected, relative))
        if not entries:
            raise PrimaryCorpusInvalid("checksum list contains no dataset parquet")
        return tuple(sorted(entries, key=lambda item: str(item[1])))

    @staticmethod
    def _partition(relative: PurePosixPath) -> tuple[str, int, str]:
        if len(relative.parts) != 4:
            raise PrimaryCorpusInvalid(f"unexpected dataset partition:{relative}")
        _, market_part, year_part, ticker_part = relative.parts
        if not market_part.startswith("market=") or not year_part.startswith("year="):
            raise PrimaryCorpusInvalid(f"malformed dataset partition:{relative}")
        if not ticker_part.startswith("ticker=") or not ticker_part.endswith(
            ".parquet"
        ):
            raise PrimaryCorpusInvalid(f"malformed ticker partition:{relative}")
        market = market_part.removeprefix("market=")
        year = int(year_part.removeprefix("year="))
        ticker = ticker_part.removeprefix("ticker=").removesuffix(".parquet")
        if market not in {"KOSPI", "KOSDAQ"}:
            raise PrimaryCorpusInvalid(f"unsupported market partition:{market}")
        if year < 2015 or year > 2024:
            raise PrimaryCorpusInvalid(f"sealed/non-exploration year partition:{year}")
        if re.fullmatch(r"[0-9]{5}[0-9A-Z]", ticker) is None:
            raise PrimaryCorpusInvalid(f"invalid ticker partition:{ticker}")
        return market, year, ticker

    @staticmethod
    def _bar_from_row(
        raw: dict[str, Any],
        *,
        partition: tuple[str, int, str],
        data_view: DataView,
        market_positions: dict[date, int],
        market_sessions: tuple[date, ...],
    ) -> tuple[CorpusBar, ClampRow | None]:
        market, year, ticker = partition
        try:
            session = date.fromisoformat(str(raw["session"]))
            row_market = str(raw["market"])
            row_ticker = str(raw["ticker"])
            open_price = int(raw["open"])
            high = int(raw["high"])
            low = int(raw["low"])
            close = int(raw["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PrimaryCorpusInvalid("invalid corpus parquet row") from exc
        if (row_market, session.year, row_ticker) != (market, year, ticker):
            raise PrimaryCorpusInvalid("parquet row/partition identity mismatch")
        session_position = market_positions.get(session)
        if session_position is None:
            raise PrimaryCorpusInvalid(f"bar outside frozen XKRX axis:{session}")
        session = market_sessions[session_position]
        if min(open_price, high, low, close) <= 0:
            raise PrimaryCorpusInvalid("bar prices must be positive")
        if low > min(open_price, close) or high < max(open_price, close):
            raise PrimaryCorpusInvalid("bar OHLC ordering invalid")
        bar = CorpusBar(
            session=session,
            symbol=ticker,
            market=market,
            open_int=open_price,
            high_int=high,
            low_int=low,
            close_int=close,
        )
        if data_view is DataView.ORIGINAL_VALID_BAR or not bool(raw["clamped"]):
            return bar, None
        clamp = ClampRow(
            market=market,
            session=session,
            symbol=ticker,
            source_high=int(raw["source_high"]),
            source_low=int(raw["source_low"]),
            high=high,
            low=low,
            delta_high=int(raw["clamp_delta_high"]),
            delta_low=int(raw["clamp_delta_low"]),
            classification=str(raw["clamp_classification"]),
            admitted=bool(raw["admitted"]),
        )
        if not clamp.admitted or max(clamp.delta_high, clamp.delta_low) <= 0:
            raise PrimaryCorpusInvalid("clamped row lacks admitted positive delta")
        return bar, clamp


def _sha256_stream(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_parquet_rows(path: Path, data_view: DataView) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    columns = list(_BASE_COLUMNS)
    if data_view is DataView.CLAMP_ADMIT_V1:
        columns.extend(_CLAMP_COLUMNS)
    table = pq.ParquetFile(path).read(columns=columns)
    if table.column_names != columns:
        raise PrimaryCorpusInvalid(f"parquet schema drift:{path.name}")
    return table.to_pylist()


def _prepare_signal_tape(
    by_symbol: dict[str, list[CorpusBar]],
    session_positions: dict[date, int],
) -> dict[tuple[date, str], SignalSnapshot]:
    signals: dict[tuple[date, str], SignalSnapshot] = {}
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.rounding = DECIMAL_ROUNDING
        for symbol, unordered in sorted(by_symbol.items()):
            bars = sorted(unordered, key=lambda item: item.session)
            window: deque[CorpusBar] = deque(maxlen=120)
            wilder = _WilderState()
            previous_position: int | None = None
            for bar in bars:
                position = session_positions[bar.session]
                if previous_position is not None and position != previous_position + 1:
                    window.clear()
                    wilder = _WilderState()
                if len(window) == 120 and wilder.value is not None:
                    rounded_rsi = wilder.value.quantize(
                        Decimal("0.0001"), rounding=DECIMAL_ROUNDING
                    )
                    if rounded_rsi < RSI_THRESHOLD:
                        prior_closes = tuple(Decimal(item.close_int) for item in window)
                        bands = bollinger_bands(prior_closes)
                        high = Decimal(max(item.high_int for item in window))
                        low = Decimal(min(item.low_int for item in window))
                        previous_close = prior_closes[-1]
                        levels = [
                            PriceLevel(price, "fib_family", f"fib_{ratio}")
                            for ratio, price in fib_levels(low, high).items()
                        ]
                        levels.append(PriceLevel(bands.lower, "bb_lower", "bb_lower"))
                        clusters = cluster_levels(levels, close=previous_close)
                        if signal_is_eligible(
                            rsi=rounded_rsi,
                            clusters=clusters,
                            close=previous_close,
                        ):
                            l2 = choose_l2(clusters, close=previous_close)
                            if l2 is None:
                                raise AssertionError("eligible primary signal lacks L2")
                            signals[(bar.session, symbol)] = SignalSnapshot(
                                rsi=rounded_rsi,
                                l2_price=l2.representative,
                                fib_high=high,
                                fib_low=low,
                                previous_close=previous_close,
                            )
                wilder.add(bar.close_int)
                window.append(bar)
                previous_position = position
    return signals


def _signal_tape_sha(
    signals: dict[tuple[date, str], SignalSnapshot],
) -> str:
    digest = hashlib.sha256()
    for (session, symbol), snapshot in sorted(signals.items()):
        digest.update(
            canonical_bytes(
                {
                    "session": session,
                    "symbol": symbol,
                    "rsi": snapshot.rsi,
                    "l2_price": snapshot.l2_price,
                    "fib_high": snapshot.fib_high,
                    "fib_low": snapshot.fib_low,
                    "previous_close": snapshot.previous_close,
                }
            )
        )
    return digest.hexdigest()


def _market_periods(
    bars: list[CorpusBar],
) -> tuple[tuple[date, date, str], ...]:
    periods: list[tuple[date, date, str]] = []
    start = bars[0].session
    end = start
    market = bars[0].market
    for bar in bars[1:]:
        if bar.market != market:
            periods.append((start, end, market))
            start = bar.session
            market = bar.market
        end = bar.session
    periods.append((start, end, market))
    return tuple(periods)


def market_for(view: LoadedCorpusView, *, symbol: str, session: date) -> str | None:
    for start, end, market in view.market_periods.get(symbol, ()):
        if start <= session <= end:
            return market
    return None
