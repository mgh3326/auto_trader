"""Fail-closed holdout/calibration access guard with serializable spy evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


class SealedAccessBlocked(PermissionError):
    code = "SEALED_ACCESS_BLOCKED"


@dataclass(slots=True)
class SealedAccessSpy:
    """Counts actual sealed reads; checks happen before any loader/key access."""

    sealed_reads: int = 0
    blocked_attempts: int = 0
    path_checks: int = 0
    date_checks: int = 0
    metadata_key_checks: int = 0
    actual_file_reads: int = 0
    manifest_reads: int = 0
    parquet_reads: int = 0
    bar_rows_read: int = 0
    metadata_key_reads: int = 0
    sealed_file_reads: int = 0
    sealed_manifest_reads: int = 0
    sealed_parquet_reads: int = 0
    sealed_bar_rows_read: int = 0
    sealed_metadata_key_reads: int = 0

    def evidence(self) -> dict[str, int]:
        return {
            "sealed_access_spy": self.sealed_reads,
            "sealed_access_blocked_attempts": self.blocked_attempts,
            "sealed_access_path_checks": self.path_checks,
            "sealed_access_date_checks": self.date_checks,
            "sealed_access_metadata_key_checks": self.metadata_key_checks,
            "measured_file_reads": self.actual_file_reads,
            "measured_manifest_reads": self.manifest_reads,
            "measured_parquet_reads": self.parquet_reads,
            "measured_bar_rows_read": self.bar_rows_read,
            "measured_metadata_key_reads": self.metadata_key_reads,
            "sealed_file_reads": self.sealed_file_reads,
            "sealed_manifest_reads": self.sealed_manifest_reads,
            "sealed_parquet_reads": self.sealed_parquet_reads,
            "sealed_bar_rows_read": self.sealed_bar_rows_read,
            "sealed_metadata_key_reads": self.sealed_metadata_key_reads,
        }


class SealedAccessGuard:
    """Guard paths, dates, manifests, bars, and metadata keys before access."""

    _SEALED_SEGMENTS = frozenset(
        {
            "holdout",
            "calibration",
            "d3_calibration_2025",
            "calibration_2025",
            "prospective",
            "2025",
        }
    )

    def __init__(self, spy: SealedAccessSpy | None = None) -> None:
        self.spy = spy or SealedAccessSpy()

    def assert_exploration_date(self, value: date | datetime) -> None:
        self.spy.date_checks += 1
        day = value.date() if isinstance(value, datetime) else value
        if day.year >= 2025:
            self.spy.blocked_attempts += 1
            raise SealedAccessBlocked(f"sealed date blocked before read: {day}")

    def assert_exploration_path(self, value: str | Path) -> None:
        self.spy.path_checks += 1
        path = Path(value).expanduser().resolve(strict=False)
        tokens = {part.casefold() for part in path.parts}
        if tokens & self._SEALED_SEGMENTS or any(
            "holdout" in token or "calibration" in token or "prospective" in token
            for token in tokens
        ):
            self.spy.blocked_attempts += 1
            raise SealedAccessBlocked(f"sealed path blocked before read: {path.name}")

    def assert_metadata_key(self, key: str) -> None:
        self.spy.metadata_key_checks += 1
        folded = key.casefold()
        if (
            folded in self._SEALED_SEGMENTS
            or "holdout" in folded
            or "calibration" in folded
            or "prospective" in folded
            or "2025" in folded
        ):
            self.spy.blocked_attempts += 1
            raise SealedAccessBlocked(
                f"sealed metadata key blocked before lookup: {key}"
            )

    def read_bar(
        self, *, path: str | Path, session: date, loader: Callable[[], T]
    ) -> T:
        self.assert_exploration_path(path)
        self.assert_exploration_date(session)
        result = loader()
        self._record_actual_path_read(path, read_kind="bar")
        self.spy.bar_rows_read += 1
        return result

    def read_manifest(self, *, path: str | Path, loader: Callable[[], T]) -> T:
        self.assert_exploration_path(path)
        result = loader()
        self._record_actual_path_read(path, read_kind="manifest")
        self.spy.manifest_reads += 1
        return result

    def read_file(self, *, path: str | Path, loader: Callable[[], T]) -> T:
        """Instrument one non-Parquet exploration file read."""

        self.assert_exploration_path(path)
        result = loader()
        self._record_actual_path_read(path, read_kind="file")
        return result

    def read_parquet(self, *, path: str | Path, loader: Callable[[], T]) -> T:
        """Instrument one Parquet parse after its path gate passes."""

        self.assert_exploration_path(path)
        result = loader()
        self._record_actual_path_read(path, read_kind="parquet")
        self.spy.parquet_reads += 1
        return result

    def record_bar_rows(self, sessions: list[date] | tuple[date, ...]) -> None:
        """Measure rows already decoded by a gated exploration Parquet read."""

        for session in sessions:
            self.spy.date_checks += 1
            if session.year >= 2025:
                self.spy.sealed_reads += 1
                self.spy.sealed_bar_rows_read += 1
                raise SealedAccessBlocked(
                    f"sealed bar date observed during measured read: {session}"
                )
        self.spy.bar_rows_read += len(sessions)

    def read_metadata(self, mapping: Mapping[str, T], key: str) -> T:
        self.assert_metadata_key(key)
        result = mapping[key]
        self.spy.metadata_key_reads += 1
        return result

    def _record_actual_path_read(self, value: str | Path, *, read_kind: str) -> None:
        """Count the completed read, including a post-read symlink recheck."""

        path = Path(value).expanduser().resolve(strict=False)
        tokens = {part.casefold() for part in path.parts}
        if tokens & self._SEALED_SEGMENTS or any(
            "holdout" in token or "calibration" in token or "prospective" in token
            for token in tokens
        ):
            self.spy.sealed_reads += 1
            if read_kind == "manifest":
                self.spy.sealed_manifest_reads += 1
            elif read_kind == "parquet":
                self.spy.sealed_parquet_reads += 1
            elif read_kind == "bar":
                self.spy.sealed_bar_rows_read += 1
            else:
                self.spy.sealed_file_reads += 1
            raise SealedAccessBlocked(
                f"sealed path resolved during measured read: {path.name}"
            )
        self.spy.actual_file_reads += 1
