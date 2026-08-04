"""Read-only bounded loader for the finalized KR main snapshot.

The collector's real manifest is a summary object; per-file integrity lives in
``checksums.sha256``.  The frozen Stage-B snapshot is rooted in the reviewed
source literals below: an artifact-only rewrite cannot replace its manifest,
checksum ledger, or the XKRX preflight sequence.  This loader verifies selected
bytes against that ledger before parsing and refuses holdout paths/dates.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pyarrow.parquet as pq

try:
    from .holdout_guard import assert_path_not_holdout, assert_range_not_holdout
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from holdout_guard import assert_path_not_holdout, assert_range_not_holdout
try:
    from .pit import Bar, bars_from_table
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from pit import Bar, bars_from_table

__all__ = [
    "RealMainStageBInput",
    "load_real_main_bars",
    "load_real_main_stage_b_input",
]


@dataclass(frozen=True)
class RealMainStageBInput:
    """Verified main-corpus input plus the evidence needed to judge it."""

    bars: tuple[Bar, ...]
    market_sessions: Mapping[str, tuple[date, ...]]
    coverage: Mapping[str, Any]


@dataclass(frozen=True)
class _TrustedStageBArtifact:
    """Reviewed immutable root for the only currently admissible Stage-B input."""

    checksums_sha256: str
    manifest_sha256: str


# These are hashes of the finalized *main* snapshot, independently recorded in
# the kr-corpus verification report.  Adding a future snapshot is intentionally
# a reviewed source change, not an artifact-only operation.
_TRUSTED_STAGE_B_ARTIFACTS: Final[Mapping[str, _TrustedStageBArtifact]] = {
    "kr-corpus-v1-20260803-1001": _TrustedStageBArtifact(
        checksums_sha256=(
            "9704cc72455bca8bc8bdea78506b16de4d0cdff697661d7ee8a349eb4b311a7f"
        ),
        manifest_sha256=(
            "da1ca376ac6693e96d311eda07f9fe96f1cb69fa2e3e8f346ededf96c5d5c54b"
        ),
    ),
}
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CHECKSUM_CONTROL_PATHS = frozenset({"checksums.sha256", "manifest.json"})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_checksum_path(relative: str, *, line_number: int) -> str:
    """Return one unambiguous POSIX-relative ledger path or fail closed."""
    if not relative or "\\" in relative:
        raise ValueError(
            f"checksum ledger has a non-canonical path at line {line_number}"
        )
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(
            f"checksum ledger has a non-canonical path at line {line_number}"
        )
    canonical = path.as_posix()
    if canonical != relative:
        raise ValueError(
            f"checksum ledger has a non-canonical path at line {line_number}"
        )
    return canonical


def _checksum_index(data: bytes) -> dict[str, str]:
    """Parse the checksum ledger without accepting ambiguous path identities."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("checksum ledger is not valid UTF-8") from exc

    index: dict[str, str] = {}
    canonical_identities: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            digest, relative = line.split("  ", maxsplit=1)
        except ValueError as exc:
            raise ValueError(
                f"checksum ledger is malformed at line {line_number}"
            ) from exc
        if _SHA256_HEX.fullmatch(digest) is None:
            raise ValueError(
                f"checksum ledger has an invalid SHA-256 at line {line_number}"
            )
        relative = _canonical_checksum_path(relative, line_number=line_number)
        if relative in _CHECKSUM_CONTROL_PATHS:
            raise ValueError(f"checksum ledger must not list control file: {relative}")
        identity = unicodedata.normalize("NFC", relative).casefold()
        if identity in canonical_identities:
            raise ValueError(f"checksum ledger duplicate path: {relative}")
        canonical_identities.add(identity)
        index[relative] = digest
    if not index:
        raise ValueError("checksum ledger is empty")
    return index


def _read_regular_artifact_file(*, root: Path, path: Path, kind: str) -> bytes:
    """Read a snapshot-local regular file, refusing symlink path components."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:  # pragma: no cover - internal callers are rooted
        raise ValueError(f"{kind} escapes artifact root") from exc

    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError(
                f"{kind} must not use a symbolic link: {relative.as_posix()}"
            )
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"{kind} is missing: {relative.as_posix()}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{kind} must be a regular file: {relative.as_posix()}")
    return path.read_bytes()


def _load_verified_checksum_index(*, root: Path, run_id: str) -> dict[str, str]:
    """Bind the mutable manifest/ledger pair to the reviewed source root."""
    trusted = _TRUSTED_STAGE_B_ARTIFACTS.get(run_id)
    if trusted is None:
        raise ValueError(f"untrusted Stage-B artifact run id: {run_id}")

    checksum_path = root / "checksums.sha256"
    checksum_bytes = _read_regular_artifact_file(
        root=root,
        path=checksum_path,
        kind="checksum ledger",
    )
    checksums = _checksum_index(checksum_bytes)

    manifest_path = root / "manifest.json"
    manifest_bytes = _read_regular_artifact_file(
        root=root,
        path=manifest_path,
        kind="manifest",
    )
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if manifest.get("scope") != "main":
        raise ValueError("Stage-B manifest must declare main scope")
    if manifest.get("files_list_location") != "checksums.sha256":
        raise ValueError("Stage-B manifest must name checksums.sha256")
    declared_checksum_digest = manifest.get("checksums_sha256")
    actual_checksum_digest = _sha256(checksum_bytes)
    if not isinstance(declared_checksum_digest, str) or (
        _SHA256_HEX.fullmatch(declared_checksum_digest) is None
    ):
        raise ValueError("manifest has no valid checksums_sha256")
    if declared_checksum_digest != actual_checksum_digest:
        raise ValueError("manifest checksum-list digest mismatch")
    if actual_checksum_digest != trusted.checksums_sha256:
        raise ValueError("untrusted checksum ledger digest")
    if _sha256(manifest_bytes) != trusted.manifest_sha256:
        raise ValueError("untrusted manifest digest")
    return checksums


def _checked_bytes(
    *,
    root: Path,
    path: Path,
    checksums: Mapping[str, str],
    kind: str,
) -> bytes:
    relative = path.relative_to(root).as_posix()
    expected = checksums.get(relative)
    if expected is None:
        raise ValueError(f"{kind} missing checksum ledger row: {relative}")
    data = _read_regular_artifact_file(root=root, path=path, kind=kind)
    actual = _sha256(data)
    if actual != expected:
        raise ValueError(f"checksum mismatch: {relative}")
    return data


def _validate_xkrx_session_sequence(sessions: tuple[date, ...]) -> None:
    """Cross-check the signed reference against the pinned XKRX calendar data."""
    try:
        import exchange_calendars as xcals
    except ModuleNotFoundError as exc:  # pragma: no cover - project dependency
        raise ValueError("XKRX calendar validation dependency is unavailable") from exc

    calendar = xcals.get_calendar("XKRX")
    expected = tuple(
        session.date()
        for session in calendar.sessions_in_range(sessions[0], sessions[-1])
    )
    if sessions != expected:
        raise ValueError("market-session reference disagrees with XKRX calendar")


def _load_market_sessions(
    *,
    root: Path,
    checksums: Mapping[str, str],
    window_start: date,
    window_end: date,
    markets: Iterable[str],
) -> dict[str, tuple[date, ...]]:
    """Load the signed XKRX sequence without deriving sessions from one symbol."""
    preflight_bytes = _checked_bytes(
        root=root,
        path=root / "preflight.json",
        checksums=checksums,
        kind="market-session reference",
    )
    try:
        payload = json.loads(preflight_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("market-session reference is not valid JSON") from exc
    if payload.get("session_calendar") != "XKRX":
        raise ValueError("market-session reference must name the XKRX calendar")
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list) or not all(
        isinstance(value, str) for value in raw_sessions
    ):
        raise ValueError("market-session reference has no string session sequence")

    start = window_start.isoformat()
    end = window_end.isoformat()
    selected_raw_sessions = tuple(
        value for value in raw_sessions if start <= value <= end
    )
    try:
        sessions = tuple(date.fromisoformat(value) for value in selected_raw_sessions)
    except ValueError as exc:
        raise ValueError(
            "market-session reference contains an invalid session"
        ) from exc
    if not sessions:
        raise ValueError("market-session reference has no sessions in run window")
    if tuple(sorted(sessions)) != sessions or len(set(sessions)) != len(sessions):
        raise ValueError(
            "market-session reference must be strictly ascending and unique"
        )
    _validate_xkrx_session_sequence(sessions)
    return dict.fromkeys(sorted(set(markets)), sessions)


def load_real_main_bars(
    *,
    artifact_root: Path,
    run_id: str,
    window_start: date,
    window_end: date,
    markets: Iterable[str],
    max_symbols: int,
) -> list[Bar]:
    """Load only the bars from the verified Stage-B main-corpus input."""
    return list(
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=window_start,
            window_end=window_end,
            markets=markets,
            max_symbols=max_symbols,
        ).bars
    )


def load_real_main_stage_b_input(
    *,
    artifact_root: Path,
    run_id: str,
    window_start: date,
    window_end: date,
    markets: Iterable[str],
    max_symbols: int,
) -> RealMainStageBInput:
    """Select symbols once, then load every in-window yearly partition for each."""
    assert_range_not_holdout(window_start, window_end)
    if max_symbols < 1:
        raise ValueError("max_symbols must be positive and explicit")
    if Path(run_id).name != run_id or run_id in {"", ".", ".."}:
        raise ValueError("run id must name exactly one artifact directory")
    root = artifact_root.expanduser().resolve() / "runs" / run_id
    assert_path_not_holdout(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Stage-B artifact root must be a regular directory")
    checksums = _load_verified_checksum_index(root=root, run_id=run_id)
    selected_markets = frozenset(markets)
    if not selected_markets:
        raise ValueError("at least one market must be explicit")
    files = sorted((root / "dataset").glob("market=*/year=*/ticker=*.parquet"))
    symbols_by_market: dict[str, set[str]] = {
        market: set() for market in selected_markets
    }
    partitions_by_key: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    for path in files:
        parts = path.parts
        market = next(
            part.split("=", 1)[1] for part in parts if part.startswith("market=")
        )
        year = int(
            next(part.split("=", 1)[1] for part in parts if part.startswith("year="))
        )
        if year < window_start.year or year > window_end.year:
            continue
        symbol = path.stem.split("=", 1)[1]
        if market not in selected_markets:
            continue
        key = (market, symbol)
        symbols_by_market[market].add(symbol)
        partitions_by_key.setdefault(key, []).append((year, path))

    missing_markets = sorted(
        market for market, symbols in symbols_by_market.items() if not symbols
    )
    if missing_markets:
        raise ValueError(
            f"requested market has no bounded corpus data: {missing_markets}"
        )

    # Select identities, rather than parquet files: every selected identity then
    # loads its complete in-window partition set.
    candidates_by_market = {
        market: sorted(symbols) for market, symbols in symbols_by_market.items()
    }
    selected_keys: list[tuple[str, str]] = []
    cursors = dict.fromkeys(sorted(selected_markets), 0)
    while len(selected_keys) < max_symbols:
        progressed = False
        for market in sorted(selected_markets):
            candidates = candidates_by_market[market]
            while cursors[market] < len(candidates):
                symbol = candidates[cursors[market]]
                cursors[market] += 1
                selected_keys.append((market, symbol))
                progressed = True
                break
            if len(selected_keys) >= max_symbols:
                break
        if not progressed:
            break

    market_sessions = _load_market_sessions(
        root=root,
        checksums=checksums,
        window_start=window_start,
        window_end=window_end,
        markets=selected_markets,
    )
    bars: list[Bar] = []
    symbol_coverage: dict[tuple[str, str], dict[str, Any]] = {
        key: {"bar_count": 0, "years": set()} for key in selected_keys
    }
    for market, symbol in selected_keys:
        partitions = sorted(
            partitions_by_key[(market, symbol)],
            key=lambda item: (item[0], item[1].name),
        )
        for _, path in partitions:
            assert_path_not_holdout(path)
            _checked_bytes(
                root=root,
                path=path,
                checksums=checksums,
                kind="selected parquet",
            )
            table = pq.read_table(path)
            partition_bars = [
                bar
                for bar in bars_from_table(table)
                if window_start <= bar.session_date <= window_end
            ]
            if any(
                bar.market != market or bar.symbol != symbol for bar in partition_bars
            ):
                raise ValueError(
                    f"parquet partition identity mismatch: {path.relative_to(root)}"
                )
            bars.extend(partition_bars)
            coverage = symbol_coverage[(market, symbol)]
            coverage["bar_count"] += len(partition_bars)
            coverage["years"].update(bar.session_date.year for bar in partition_bars)

    market_coverage: dict[str, dict[str, Any]] = {}
    for market in sorted(selected_markets):
        selected_symbols = sorted(
            symbol
            for selected_market, symbol in selected_keys
            if selected_market == market
        )
        symbols: dict[str, dict[str, Any]] = {}
        for symbol in selected_symbols:
            coverage = symbol_coverage[(market, symbol)]
            years = sorted(coverage["years"])
            symbols[symbol] = {
                "bar_count": coverage["bar_count"],
                "year_count": len(years),
                "years": years,
            }
        years = sorted(
            year for coverage in symbols.values() for year in coverage["years"]
        )
        unique_years = sorted(set(years))
        market_coverage[market] = {
            "symbol_count": len(selected_symbols),
            "bar_count": sum(coverage["bar_count"] for coverage in symbols.values()),
            "year_count": len(unique_years),
            "years": unique_years,
            "symbols": symbols,
        }
    coverage: dict[str, Any] = {
        "session_reference": {
            "source": "preflight.json",
            "calendar": "XKRX",
            "markets": {
                market: {
                    "session_count": len(sessions),
                    "first_session": sessions[0].isoformat(),
                    "last_session": sessions[-1].isoformat(),
                }
                for market, sessions in sorted(market_sessions.items())
            },
        },
        "markets": market_coverage,
    }
    return RealMainStageBInput(
        bars=tuple(bars),
        market_sessions=market_sessions,
        coverage=coverage,
    )
