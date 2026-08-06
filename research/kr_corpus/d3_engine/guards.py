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

    def evidence(self) -> dict[str, int]:
        return {
            "sealed_access_spy": self.sealed_reads,
            "sealed_access_blocked_attempts": self.blocked_attempts,
        }


class SealedAccessGuard:
    """Guard paths, dates, manifests, bars, and metadata keys before access."""

    _SEALED_SEGMENTS = frozenset(
        {
            "holdout",
            "calibration",
            "d3_calibration_2025",
            "calibration_2025",
            "2025",
        }
    )

    def __init__(self, spy: SealedAccessSpy | None = None) -> None:
        self.spy = spy or SealedAccessSpy()

    def assert_exploration_date(self, value: date | datetime) -> None:
        day = value.date() if isinstance(value, datetime) else value
        if day.year >= 2025:
            self.spy.blocked_attempts += 1
            raise SealedAccessBlocked(f"sealed date blocked before read: {day}")

    def assert_exploration_path(self, value: str | Path) -> None:
        path = Path(value).expanduser()
        tokens = {part.casefold() for part in path.parts}
        if tokens & self._SEALED_SEGMENTS or any(
            "holdout" in token or "calibration" in token for token in tokens
        ):
            self.spy.blocked_attempts += 1
            raise SealedAccessBlocked(f"sealed path blocked before read: {path.name}")

    def assert_metadata_key(self, key: str) -> None:
        folded = key.casefold()
        if (
            folded in self._SEALED_SEGMENTS
            or "holdout" in folded
            or "calibration" in folded
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
        return loader()

    def read_manifest(self, *, path: str | Path, loader: Callable[[], T]) -> T:
        self.assert_exploration_path(path)
        return loader()

    def read_metadata(self, mapping: Mapping[str, T], key: str) -> T:
        self.assert_metadata_key(key)
        return mapping[key]
