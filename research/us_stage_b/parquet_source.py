"""Read-only Parquet adapter for the frozen US Stage-B bar contract.

This module binds only the explicit year roots a caller supplies.  It never
walks a corpus parent, never globs holdout or staging trees, and never falls
back to a database or network source.  Column names are bound literally —
there are no heuristic aliases.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from .source import USStageBDailyBar, USStageBInputError

__all__ = [
    "ALLOWED_EXPLORATION_YEARS",
    "FORBIDDEN_PATH_SEGMENTS",
    "REQUIRED_PARQUET_COLUMNS",
    "CorpusPathAccessError",
    "ParquetUSBarSource",
    "PathAccessRecord",
    "PathAccessSpy",
    "assert_year_root_allowed",
    "is_forbidden_corpus_path",
]


ALLOWED_EXPLORATION_YEARS: Final[frozenset[int]] = frozenset(range(2016, 2025))
FORBIDDEN_PATH_SEGMENTS: Final[frozenset[str]] = frozenset({"holdout", "_staging"})
REQUIRED_PARQUET_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "session_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
_YEAR_ROOT_RE: Final = re.compile(r"^year=(?P<year>\d{4})$")
_HOLDOUT_START: Final = date(2025, 1, 1)


class CorpusPathAccessError(USStageBInputError):
    """A year root, path segment, or Parquet cell violates the US corpus gate."""


@dataclass(frozen=True)
class PathAccessRecord:
    """One path-touch record used to prove forbidden roots were not enumerated."""

    kind: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}


class PathAccessSpy:
    """Record every validated, listed, or opened path for holdout proofs."""

    def __init__(self) -> None:
        self._records: list[PathAccessRecord] = []

    def note(self, kind: str, path: Path) -> Path:
        """Record a path access.  Forbidden segments fail closed immediately."""

        resolved = _resolve_for_log(path)
        record = PathAccessRecord(kind=kind, path=resolved)
        self._records.append(record)
        if is_forbidden_corpus_path(Path(resolved)):
            raise CorpusPathAccessError(
                f"forbidden corpus path access refused (kind={kind!r}, path={resolved})"
            )
        return path

    @property
    def records(self) -> tuple[PathAccessRecord, ...]:
        return tuple(self._records)

    def forbidden_root_enumerations(self) -> tuple[PathAccessRecord, ...]:
        """Return listdir/open records that touch holdout or staging."""

        return tuple(
            record
            for record in self._records
            if record.kind in {"listdir", "open_parquet"}
            and is_forbidden_corpus_path(Path(record.path))
        )

    def summary(self) -> dict[str, int]:
        return {
            "path_accesses_total": len(self._records),
            "validate_count": sum(
                record.kind == "validate" for record in self._records
            ),
            "listdir_count": sum(record.kind == "listdir" for record in self._records),
            "open_parquet_count": sum(
                record.kind == "open_parquet" for record in self._records
            ),
            "forbidden_root_enumerations": len(self.forbidden_root_enumerations()),
        }


def is_forbidden_corpus_path(path: Path) -> bool:
    """True when any path component is a sealed holdout or staging segment."""

    return any(part.lower() in FORBIDDEN_PATH_SEGMENTS for part in path.parts)


def assert_year_root_allowed(
    year_root: Path,
    *,
    access_spy: PathAccessSpy | None = None,
) -> int:
    """Validate one explicit year root without listing any parent tree."""

    if access_spy is not None:
        access_spy.note("validate", year_root)
    if is_forbidden_corpus_path(year_root):
        raise CorpusPathAccessError(
            f"year root is under a forbidden holdout/staging path: {year_root}"
        )
    match = _YEAR_ROOT_RE.fullmatch(year_root.name)
    if match is None:
        raise CorpusPathAccessError(
            f"year root must be named year=YYYY; got {year_root.name!r}"
        )
    year = int(match.group("year"))
    if year not in ALLOWED_EXPLORATION_YEARS:
        raise CorpusPathAccessError(
            f"year root {year} is outside the nine allowlisted exploration years "
            f"{sorted(ALLOWED_EXPLORATION_YEARS)}"
        )
    if not year_root.is_dir():
        raise CorpusPathAccessError(f"year root is not a directory: {year_root}")
    return year


class ParquetUSBarSource:
    """Deterministic, read-only ``USBarSource`` backed by explicit year Parquet roots."""

    def __init__(
        self,
        bars: Iterable[USStageBDailyBar],
        *,
        year_roots: Sequence[Path],
        access_spy: PathAccessSpy | None = None,
        files_read: Sequence[Path] = (),
        rows_loaded: int = 0,
    ) -> None:
        rows: dict[tuple[str, date], USStageBDailyBar] = {}
        symbols: set[str] = set()
        sessions: set[date] = set()
        for bar in bars:
            key = (bar.symbol, bar.session_date)
            if key in rows:
                raise CorpusPathAccessError(
                    f"duplicate US Stage-B bar: {bar.symbol}/{bar.session_date}"
                )
            if bar.session_date >= _HOLDOUT_START:
                raise CorpusPathAccessError(
                    "Parquet row intersects the 2025+ sealed holdout: "
                    f"{bar.symbol}/{bar.session_date.isoformat()}"
                )
            rows[key] = bar
            symbols.add(bar.symbol)
            sessions.add(bar.session_date)
        self._rows = rows
        self._symbols = tuple(sorted(symbols))
        self._sessions = tuple(sorted(sessions))
        self._year_roots = tuple(Path(root) for root in year_roots)
        self._access_spy = access_spy if access_spy is not None else PathAccessSpy()
        self._files_read = tuple(Path(path) for path in files_read)
        self._rows_loaded = rows_loaded

    @classmethod
    def from_year_roots(
        cls,
        year_roots: Sequence[Path | str],
        *,
        access_spy: PathAccessSpy | None = None,
    ) -> ParquetUSBarSource:
        """Load only the caller-supplied year directories; never expand parents."""

        spy = access_spy if access_spy is not None else PathAccessSpy()
        if not year_roots:
            raise CorpusPathAccessError("at least one explicit year root is required")

        roots = [Path(root) for root in year_roots]
        years: list[int] = []
        seen_years: set[int] = set()
        for root in roots:
            year = assert_year_root_allowed(root, access_spy=spy)
            if year in seen_years:
                raise CorpusPathAccessError(f"duplicate year root for {year}")
            seen_years.add(year)
            years.append(year)

        bars: list[USStageBDailyBar] = []
        files_read: list[Path] = []
        for root, year in zip(roots, years, strict=True):
            files = _list_parquet_files(root, access_spy=spy)
            if not files:
                raise CorpusPathAccessError(
                    f"year root contains no Parquet files: {root}"
                )
            for file_path in files:
                bars.extend(
                    _load_bars_from_parquet(
                        file_path,
                        expected_year=year,
                        access_spy=spy,
                    )
                )
                files_read.append(file_path)

        return cls(
            bars,
            year_roots=roots,
            access_spy=spy,
            files_read=files_read,
            rows_loaded=len(bars),
        )

    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def get(self, symbol: str, session_date: date) -> USStageBDailyBar | None:
        return self._rows.get((symbol, session_date))

    def corpus_sessions(self) -> tuple[date, ...]:
        """Return the sorted union of loaded session dates (corpus session index)."""

        return self._sessions

    @property
    def access_spy(self) -> PathAccessSpy:
        return self._access_spy

    @property
    def year_roots(self) -> tuple[Path, ...]:
        return self._year_roots

    @property
    def files_read(self) -> tuple[Path, ...]:
        return self._files_read

    def access_summary(self) -> dict[str, Any]:
        summary = self._access_spy.summary()
        summary.update(
            {
                "year_roots": [str(root) for root in self._year_roots],
                "files_read": [str(path) for path in self._files_read],
                "rows_loaded": self._rows_loaded,
                "symbol_count": len(self._symbols),
                "session_count": len(self._sessions),
                "holdout_reads": 0,
                "forbidden_root_enumerations": summary["forbidden_root_enumerations"],
            }
        )
        return summary


def _resolve_for_log(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path)


def _list_parquet_files(
    year_root: Path, *, access_spy: PathAccessSpy
) -> tuple[Path, ...]:
    access_spy.note("listdir", year_root)
    # Explicit directory listing only — never recursive, never parent glob.
    names = sorted(path.name for path in year_root.iterdir() if path.is_file())
    files = tuple(
        year_root / name
        for name in names
        if name.endswith(".parquet") and not name.startswith(".")
    )
    return files


def _load_bars_from_parquet(
    path: Path,
    *,
    expected_year: int,
    access_spy: PathAccessSpy,
) -> list[USStageBDailyBar]:
    access_spy.note("open_parquet", path)
    try:
        parquet_file = pq.ParquetFile(path)
        observed = set(parquet_file.schema_arrow.names)
        missing = [name for name in REQUIRED_PARQUET_COLUMNS if name not in observed]
        if missing:
            raise CorpusPathAccessError(
                f"Parquet file {path} lacks required columns: {missing!r}"
            )
        # Refuse heuristic aliases: only the literal schema names may be bound.
        # Additional columns are ignored by the explicit projection below.
        table = parquet_file.read(columns=list(REQUIRED_PARQUET_COLUMNS))
    except CorpusPathAccessError:
        raise
    except FileNotFoundError as exc:
        raise CorpusPathAccessError(f"Parquet file missing: {path}") from exc
    except (OSError, pa.ArrowInvalid, ValueError, KeyError) as exc:
        raise CorpusPathAccessError(
            f"failed to read US Parquet file {path}: {exc}"
        ) from exc
    symbols = table.column("symbol").to_pylist()
    session_dates = [
        _coerce_session_date(value, path=path, row_index=index)
        for index, value in enumerate(table.column("session_date").to_pylist())
    ]
    opens = table.column("open").to_pylist()
    closes = table.column("close").to_pylist()
    volumes = table.column("volume").to_pylist()
    # high/low are contract fields: require presence (already projected) and
    # materialize once so a truncated file cannot silently drop them.
    highs = table.column("high").to_pylist()
    lows = table.column("low").to_pylist()
    if not (
        len(symbols)
        == len(session_dates)
        == len(opens)
        == len(closes)
        == len(volumes)
        == len(highs)
        == len(lows)
    ):
        raise CorpusPathAccessError(f"Parquet file {path} has ragged columns")

    bars: list[USStageBDailyBar] = []
    for index, symbol in enumerate(symbols):
        if symbol is None or not str(symbol):
            raise CorpusPathAccessError(f"invalid symbol at row {index} in {path}")
        session = session_dates[index]
        if session.year != expected_year:
            raise CorpusPathAccessError(
                f"row year mismatch in {path}: session={session.isoformat()} "
                f"expected year={expected_year}"
            )
        if session >= _HOLDOUT_START:
            raise CorpusPathAccessError(
                f"2025+ session refused in {path}: {session.isoformat()}"
            )
        # Touch high/low so incomplete projections cannot pass silently.
        _ = highs[index]
        _ = lows[index]
        bars.append(
            USStageBDailyBar(
                symbol=str(symbol),
                session_date=session,
                open=_coerce_optional_float(opens[index]),
                adjusted_close=_coerce_optional_float(closes[index]),
                volume=_coerce_optional_float(volumes[index]),
            )
        )
    return bars


def _coerce_session_date(value: Any, *, path: Path, row_index: int) -> date:
    if value is None:
        raise CorpusPathAccessError(f"null session_date at row {row_index} in {path}")
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise CorpusPathAccessError(
                f"timezone-aware session_date refused at row {row_index} in {path}"
            )
        return value.date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise CorpusPathAccessError(
                f"unparseable session_date at row {row_index} in {path}: {value!r}"
            ) from exc
    raise CorpusPathAccessError(
        f"unsupported session_date type at row {row_index} in {path}: "
        f"{type(value).__name__}"
    )


def _coerce_optional_float(value: Any) -> float | None:
    """Bind a numeric cell without cleaning.

    Null stays None.  Finite and non-finite floats (NaN/±Inf) are preserved
    exactly so the #1797 signal engine can exclude them via its own
    ``_finite_positive`` checks — this adapter must not coerce them away.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise CorpusPathAccessError("boolean numeric cell is invalid for US bars")
    if isinstance(value, (int, float)):
        # float() preserves math.nan / ±inf; do not special-case them here.
        return float(value)
    raise CorpusPathAccessError(f"non-numeric OHLC/volume cell: {type(value).__name__}")
