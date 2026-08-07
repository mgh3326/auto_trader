"""Amendment A2 calibration-scoped sealed-access carve-out.

`d3-amendment-a2-20260808.md` (sha256
``37b3045cd20678815c49066862ddd89dfebd5e852c57d84dcd1ce062b69dc020``) opens
exactly one key into the sealed corpus and nothing else. This module is that
key. It is a *new* surface: ``guards.SealedAccessGuard`` and
``primary.PrimaryPortfolioEngine`` are neither imported-for-mutation nor
edited, and the primary runner keeps its hard-coded deny-list guard.

A2 literal (the tables below map 1:1 onto :class:`CalibrationAccessGuard`):

===========  ==========================================  ======================
Axis         Allowed                                     Still blocked
===========  ==========================================  ======================
date         ``<= 2024`` (indicator warm-up) **and**      ``>= 2026`` entirely;
             the 2025 dates present in the sealed        any 2025 date absent
             calendar csv                                from that csv
             (``17e95d0b30ade5e6...``)
path         the exploration corpus **and** only the      prospective
             files enumerated by the                     partitions; holdout
             ``D3_CALIBRATION_2025`` manifest, scoped     files outside the
             to its 2025 partition                       manifest; every other
                                                         sealed segment
spy          measured — every check and every completed   a self-declared
             read is counted and serialized              statement
===========  ==========================================  ======================

Scope is the pre-approved 2P cell (``B0 x with_contribution x
{original, clamp}``), one-shot. Any other arm, sensitivity, or prospective
extension needs a new upstream signature.

The manifest allow-list is *bootstrapped*: only ``manifest.json`` and
``checksums.sha256`` are readable at construction, and the enumerated file set
is bound exactly once from those two documents
(:meth:`CalibrationAccessGuard.bind_manifest_allowlist`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from research.kr_corpus.backtest.holdout_guard import HOLDOUT_DIR
from research.kr_corpus.d3_engine.guards import (
    SealedAccessBlocked,
    SealedAccessGuard,
    SealedAccessSpy,
)

AMENDMENT_A2_SHA256 = "37b3045cd20678815c49066862ddd89dfebd5e852c57d84dcd1ce062b69dc020"
CALIBRATION_INDEX_SHA256 = (
    "17e95d0b30ade5e6fbd744cf719bc15224ef5177431cc51cfff8f2abb6930508"
)
CALIBRATION_YEAR = 2025
WARMUP_LAST_YEAR = 2024
HOLDOUT_RUN_ID = "kr-corpus-v1-20260803-1001"
MANIFEST_DOCUMENT_NAMES = ("manifest.json", "checksums.sha256")

_YEAR_PARTITION = re.compile(r"^year=(\d{4})$")


class CalibrationScopeError(RuntimeError):
    """The carve-out was configured outside its A2 scope."""

    code = "RUN_INVALID_CALIBRATION_SCOPE"


@dataclass(slots=True)
class CalibrationAccessSpy(SealedAccessSpy):
    """``SealedAccessSpy`` plus the A2 authorized/blocked breakdown.

    Inherited counters keep their meaning. ``sealed_access_spy`` therefore
    counts *authorized* sealed reads here instead of staying at zero: the
    calibration path is the one path allowed to read sealed bytes, so a zero
    there would mean the run never opened its own input.
    """

    warmup_date_checks: int = 0
    calibration_date_checks: int = 0
    blocked_prospective_date_attempts: int = 0
    blocked_offcalendar_2025_date_attempts: int = 0
    exploration_path_checks: int = 0
    calibration_path_checks: int = 0
    blocked_outside_manifest_attempts: int = 0
    blocked_prospective_path_attempts: int = 0
    blocked_sealed_token_attempts: int = 0
    authorized_manifest_document_reads: int = 0
    authorized_calibration_file_reads: int = 0
    authorized_calibration_parquet_reads: int = 0
    authorized_calibration_bar_rows: int = 0
    manifest_allowlist_size: int = 0
    manifest_excluded_out_of_scope: int = 0
    calibration_sessions_authorized: int = 0

    def evidence(self) -> dict[str, int]:
        # Explicit base call: ``@dataclass(slots=True)`` rebuilds the class, so
        # the zero-argument ``super()`` cell no longer matches this instance.
        return {
            **SealedAccessSpy.evidence(self),
            "calibration_warmup_date_checks": self.warmup_date_checks,
            "calibration_authorized_date_checks": self.calibration_date_checks,
            "calibration_blocked_prospective_date_attempts": (
                self.blocked_prospective_date_attempts
            ),
            "calibration_blocked_offcalendar_2025_date_attempts": (
                self.blocked_offcalendar_2025_date_attempts
            ),
            "calibration_exploration_path_checks": self.exploration_path_checks,
            "calibration_authorized_path_checks": self.calibration_path_checks,
            "calibration_blocked_outside_manifest_attempts": (
                self.blocked_outside_manifest_attempts
            ),
            "calibration_blocked_prospective_path_attempts": (
                self.blocked_prospective_path_attempts
            ),
            "calibration_blocked_sealed_token_attempts": (
                self.blocked_sealed_token_attempts
            ),
            "calibration_authorized_manifest_document_reads": (
                self.authorized_manifest_document_reads
            ),
            "calibration_authorized_file_reads": self.authorized_calibration_file_reads,
            "calibration_authorized_parquet_reads": (
                self.authorized_calibration_parquet_reads
            ),
            "calibration_authorized_bar_rows": self.authorized_calibration_bar_rows,
            "calibration_manifest_allowlist_size": self.manifest_allowlist_size,
            "calibration_manifest_excluded_out_of_scope": (
                self.manifest_excluded_out_of_scope
            ),
            "calibration_sessions_authorized": self.calibration_sessions_authorized,
        }


def partition_year(path: Path) -> int | None:
    """Return the ``year=YYYY`` partition of ``path``, or ``None``."""

    for part in path.parts:
        match = _YEAR_PARTITION.fullmatch(part)
        if match is not None:
            return int(match.group(1))
    return None


class CalibrationAccessGuard(SealedAccessGuard):
    """Widen ``SealedAccessGuard`` by exactly the A2 allow-list, nothing more.

    Everything the base guard blocks stays blocked unless it is either a
    ``<= 2024`` warm-up date, a 2025 date on the sealed calendar, or a file the
    ``D3_CALIBRATION_2025`` manifest enumerates inside its 2025 partition.
    """

    def __init__(
        self,
        *,
        calibration_sessions: Iterable[date],
        spy: CalibrationAccessSpy | None = None,
        holdout_root: Path | None = None,
        run_id: str = HOLDOUT_RUN_ID,
    ) -> None:
        super().__init__(spy or CalibrationAccessSpy())
        sessions = frozenset(calibration_sessions)
        if not sessions:
            raise CalibrationScopeError("calibration calendar is empty")
        off_scope = sorted(day for day in sessions if day.year != CALIBRATION_YEAR)
        if off_scope:
            raise CalibrationScopeError(
                f"calibration calendar carries non-2025 dates:{off_scope[:3]}"
            )
        self._sessions = sessions
        self._holdout_root = (
            (holdout_root or HOLDOUT_DIR).expanduser().resolve(strict=False)
        )
        self._run_root = (self._holdout_root / "runs" / run_id).resolve(strict=False)
        self._manifest_documents = frozenset(
            self._run_root / name for name in MANIFEST_DOCUMENT_NAMES
        )
        # Bootstrap: only the two enumerating documents are readable until the
        # file set they enumerate has been bound.
        self._allowed: frozenset[Path] = self._manifest_documents
        self._bound = False
        self.spy.calibration_sessions_authorized = len(sessions)

    # -- allow-list binding -------------------------------------------------

    @property
    def manifest_documents(self) -> frozenset[Path]:
        return self._manifest_documents

    @property
    def allowed_paths(self) -> frozenset[Path]:
        return self._allowed

    @property
    def calibration_sessions(self) -> frozenset[date]:
        return self._sessions

    def bind_manifest_allowlist(
        self, paths: Iterable[Path], *, excluded_out_of_scope: int
    ) -> None:
        """Bind the enumerated 2025 file set. Callable exactly once."""

        if self._bound:
            raise CalibrationScopeError("manifest allow-list is already bound")
        resolved = frozenset(
            Path(item).expanduser().resolve(strict=False) for item in paths
        )
        stray = sorted(
            str(item) for item in resolved if not item.is_relative_to(self._run_root)
        )
        if stray:
            raise CalibrationScopeError(f"allow-list escaped the run root:{stray[:3]}")
        off_scope = sorted(
            str(item)
            for item in resolved
            if (year := partition_year(item)) is not None and year != CALIBRATION_YEAR
        )
        if off_scope:
            raise CalibrationScopeError(
                f"allow-list carries a non-2025 partition:{off_scope[:3]}"
            )
        self._allowed = resolved | self._manifest_documents
        self._bound = True
        self.spy.manifest_allowlist_size = len(self._allowed)
        self.spy.manifest_excluded_out_of_scope = excluded_out_of_scope

    def fresh_clone(self) -> CalibrationAccessGuard:
        """Same A2 scope, zeroed spy.

        Each engine attempt needs its own counters: ``EngineResult.evidence``
        embeds the guard's spy, so a shared accumulating spy would make two
        otherwise identical attempts serialize differently and defeat the
        byte-identical determinism check.
        """

        clone = type(self)(
            calibration_sessions=self._sessions,
            spy=type(self.spy)(),
            holdout_root=self._holdout_root,
        )
        clone.bind_manifest_allowlist(
            self._allowed - self._manifest_documents,
            excluded_out_of_scope=self.spy.manifest_excluded_out_of_scope,
        )
        return clone

    # -- A2 date axis -------------------------------------------------------

    def assert_exploration_date(self, value: date | datetime) -> None:
        self.spy.date_checks += 1
        day = value.date() if isinstance(value, datetime) else value
        if day.year <= WARMUP_LAST_YEAR:
            self.spy.warmup_date_checks += 1
            return
        if day.year == CALIBRATION_YEAR and day in self._sessions:
            self.spy.calibration_date_checks += 1
            return
        self.spy.blocked_attempts += 1
        if day.year > CALIBRATION_YEAR:
            self.spy.blocked_prospective_date_attempts += 1
            reason = "prospective year"
        else:
            self.spy.blocked_offcalendar_2025_date_attempts += 1
            reason = "2025 date absent from the sealed calendar"
        raise SealedAccessBlocked(
            f"calibration date blocked before read: {day} ({reason})"
        )

    # -- A2 path axis -------------------------------------------------------

    def _has_sealed_token(self, path: Path) -> bool:
        """Mirror the base deny-list predicate off the base token set."""

        tokens = {part.casefold() for part in path.parts}
        return bool(tokens & self._SEALED_SEGMENTS) or any(
            "holdout" in token or "calibration" in token or "prospective" in token
            for token in tokens
        )

    def _authorize_path(self, value: str | Path) -> tuple[Path, str]:
        path = Path(value).expanduser().resolve(strict=False)
        if path.is_relative_to(self._holdout_root):
            if path in self._allowed:
                return path, "calibration"
            self.spy.blocked_attempts += 1
            year = partition_year(path)
            if year is not None and year != CALIBRATION_YEAR:
                self.spy.blocked_prospective_path_attempts += 1
                reason = f"non-calibration partition year={year}"
            else:
                self.spy.blocked_outside_manifest_attempts += 1
                reason = "not enumerated by the D3_CALIBRATION_2025 manifest"
            raise SealedAccessBlocked(
                f"sealed path blocked before read: {path.name} ({reason})"
            )
        if self._has_sealed_token(path):
            self.spy.blocked_attempts += 1
            self.spy.blocked_sealed_token_attempts += 1
            raise SealedAccessBlocked(f"sealed path blocked before read: {path.name}")
        return path, "exploration"

    def assert_exploration_path(self, value: str | Path) -> None:
        self.spy.path_checks += 1
        _, kind = self._authorize_path(value)
        if kind == "calibration":
            self.spy.calibration_path_checks += 1
        else:
            self.spy.exploration_path_checks += 1

    def _record_actual_path_read(self, value: str | Path, *, read_kind: str) -> None:
        """Post-read symlink recheck; authorized sealed reads are counted, not raised."""

        path, kind = self._authorize_path(value)
        if kind == "calibration":
            self.spy.sealed_reads += 1
            if path in self._manifest_documents:
                self.spy.authorized_manifest_document_reads += 1
                self.spy.sealed_manifest_reads += 1
            elif read_kind == "parquet":
                self.spy.authorized_calibration_parquet_reads += 1
                self.spy.sealed_parquet_reads += 1
            elif read_kind == "bar":
                self.spy.sealed_bar_rows_read += 1
            else:
                self.spy.authorized_calibration_file_reads += 1
                self.spy.sealed_file_reads += 1
        self.spy.actual_file_reads += 1

    def record_bar_rows(self, sessions: list[date] | tuple[date, ...]) -> None:
        """Gate every decoded bar date; 2025 rows are authorized, not fatal."""

        authorized_2025 = 0
        for session in sessions:
            self.assert_exploration_date(session)
            if session.year == CALIBRATION_YEAR:
                authorized_2025 += 1
        self.spy.bar_rows_read += len(sessions)
        self.spy.authorized_calibration_bar_rows += authorized_2025
