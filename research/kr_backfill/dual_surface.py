"""Kiwoom mock/live provenance and pacing contracts for the KR backfill.

This module is deliberately offline-friendly.  It contains no client creation
or network calls; the collector supplies the client and invokes the pacer.
Keeping the surface contract here makes it possible to test the dangerous
parts of the dual-pipe without touching either Kiwoom host.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Surface = Literal["mock", "live"]
SURFACES: tuple[Surface, Surface] = ("mock", "live")

# The live probe reached 0.5 s without a 429, but only had eight calls per
# step.  That is not enough evidence to call 0.5 s a proven safe upper bound.
# Therefore the live lane backs off monotonically to 1.0 s and then 2.0 s.
# There is intentionally no automatic recovery after a successful response.
SURFACE_INITIAL_PACE_SECONDS: dict[Surface, float] = {
    "mock": 2.0,
    "live": 0.5,
}
BACKOFF_PACE_SECONDS: tuple[float, float] = (1.0, 2.0)
MAX_SURFACE_429 = 3

BAR_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
)


class SurfaceContractError(ValueError):
    """The immutable split or surface value is invalid."""


class SurfaceAuthError(RuntimeError):
    """A surface returned 401; the lane must stop immediately."""


class SurfaceBackoffExhausted(RuntimeError):
    """A surface returned too many 429s; the lane must stop and be reported."""


class OverlapMismatch(RuntimeError):
    """The intentional cross-surface exact-equality sample disagreed."""


def validate_surface(surface: str) -> Surface:
    if surface not in SURFACES:
        raise SurfaceContractError(
            f"surface must be one of {SURFACES!r}; got {surface!r}"
        )
    return surface  # type: ignore[return-value]


def http_status_from_exception(exc: BaseException) -> int | None:
    """Extract an HTTP status without relying on a concrete HTTP library."""

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"\b(401|429)\b", str(exc))
    return int(match.group(1)) if match else None


class SurfacePacer:
    """One independent, monotonic pacer for exactly one Kiwoom surface."""

    def __init__(self, surface: Surface, *, clock: Any = time.monotonic) -> None:
        self.surface = validate_surface(surface)
        self.interval = SURFACE_INITIAL_PACE_SECONDS[self.surface]
        self.calls = 0
        self.consecutive_429 = 0
        self.backoff_level = 0
        self._last = 0.0
        self._clock = clock
        self._wait_lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._wait_lock:
            delta = self._clock() - self._last
            if delta < self.interval:
                await asyncio.sleep(self.interval - delta)
            self._last = self._clock()
            self.calls += 1

    def note_status(self, status_code: int | None) -> None:
        """Apply only fail-closed status handling and monotonic backoff."""

        if status_code == 401:
            raise SurfaceAuthError(f"{self.surface}: HTTP 401; stop surface and report")
        if status_code != 429:
            # Keep both the interval and the 429 count monotonic.  A successful
            # call is not evidence that the earlier 0.5 s boundary is safe;
            # automatic recovery is NO, and a later recurrence still advances
            # the same fail-closed backoff ladder.
            return

        self.consecutive_429 += 1
        if self.consecutive_429 >= MAX_SURFACE_429:
            raise SurfaceBackoffExhausted(
                f"{self.surface}: {MAX_SURFACE_429} HTTP 429 responses; "
                "stop surface and report"
            )
        self.backoff_level = min(self.consecutive_429, len(BACKOFF_PACE_SECONDS))
        self.interval = max(self.interval, BACKOFF_PACE_SECONDS[self.backoff_level - 1])

    def note_exception(self, exc: BaseException) -> None:
        self.note_status(http_status_from_exception(exc))


def validate_surface_rows(rows: list[dict[str, str]]) -> dict[str, list[Surface]]:
    """Validate an immutable split CSV and return symbol → allowed surfaces.

    Bulk symbols have exactly one surface.  Only explicit overlap rows may
    carry both surfaces, and those rows must carry both (not an accidental
    duplicate of the same surface).
    """

    if not rows:
        raise SurfaceContractError("surface split is empty")
    required = {"ticker", "surface"}
    missing = required - set(rows[0])
    if missing:
        raise SurfaceContractError(f"surface split missing columns: {sorted(missing)}")

    by_symbol: dict[str, list[tuple[Surface, str]]] = {}
    for row in rows:
        symbol = str(row.get("ticker", "")).strip()
        if not symbol:
            raise SurfaceContractError("surface split contains an empty ticker")
        surface = validate_surface(str(row.get("surface", "")).strip())
        kind = str(row.get("assignment_kind", "bulk")).strip() or "bulk"
        if kind not in {"bulk", "overlap"}:
            raise SurfaceContractError(
                f"{symbol}: assignment_kind must be bulk or overlap, got {kind!r}"
            )
        by_symbol.setdefault(symbol, []).append((surface, kind))

    result: dict[str, list[Surface]] = {}
    for symbol, entries in by_symbol.items():
        surfaces = [surface for surface, _kind in entries]
        kinds = {kind for _surface, kind in entries}
        if len(surfaces) != len(set(surfaces)):
            raise SurfaceContractError(f"{symbol}: duplicate surface assignment")
        if len(surfaces) == 1:
            if kinds != {"bulk"}:
                raise SurfaceContractError(
                    f"{symbol}: overlap assignment must name both surfaces"
                )
        elif surfaces != list(SURFACES) or kinds != {"overlap"}:
            raise SurfaceContractError(
                f"{symbol}: cross-surface split is forbidden unless it is an "
                "explicit mock+live overlap sample"
            )
        result[symbol] = surfaces
    return result


def assert_bulk_assignment_is_single_surface(rows: list[dict[str, str]]) -> None:
    """Adversarial guard used by tests and the collector before any fetch."""

    validate_surface_rows(rows)


def compare_overlap_exact(
    symbol: str,
    mock_rows: dict[Any, dict[str, float]],
    live_rows: dict[Any, dict[str, float]],
) -> dict[str, Any]:
    """Compare a Kiwoom overlap sample with integer/exact equality semantics."""

    mock_times = set(mock_rows)
    live_times = set(live_rows)
    if mock_times != live_times:
        raise OverlapMismatch(
            f"{symbol}: overlap coverage differs "
            f"mock_only={len(mock_times - live_times)} "
            f"live_only={len(live_times - mock_times)}"
        )
    compared = 0
    for timestamp in sorted(mock_times):
        for field in BAR_FIELDS:
            compared += 1
            if mock_rows[timestamp].get(field) != live_rows[timestamp].get(field):
                raise OverlapMismatch(
                    f"{symbol}: exact overlap mismatch at {timestamp!s} field={field}"
                )
    return {"symbol": symbol, "rows": len(mock_rows), "cells_compared": compared}


@dataclass(frozen=True)
class SurfaceManifest:
    """Batch-level provenance sidecar for rows in research.kr_candles_1m."""

    batch_id: str
    assignment_path: str
    assignment_sha256: str
    surfaces: tuple[Surface, Surface] = SURFACES
    manifest_surface_field: str = "surface"
    manifest_surface_granularity: str = "batch_and_progress_event"
    cross_surface_split_prevented: bool = True
    overlap_sample_size: int = 2
    overlap_every_batches: int = 50
    overlap_mismatch_action: str = "stop_and_report"
    auto_recovery: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "kiwoom_dual_surface.v1",
            "batch_id": self.batch_id,
            "assignment_path": self.assignment_path,
            "assignment_sha256": self.assignment_sha256,
            "surfaces": list(self.surfaces),
            "surface": "per_batch_and_progress_event",
            "manifest_surface_field": self.manifest_surface_field,
            "manifest_surface_granularity": self.manifest_surface_granularity,
            "cross_surface_split_prevented": self.cross_surface_split_prevented,
            "overlap_sample": {
                "size": self.overlap_sample_size,
                "every_completed_batches": self.overlap_every_batches,
                "reason": (
                    "The 2026-08-03 equality result is historical evidence; "
                    "a small live-vs-mock exact check is repeated during bulk."
                ),
            },
            "overlap_mismatch_action": self.overlap_mismatch_action,
            "backoff": {
                "initial_seconds": dict(SURFACE_INITIAL_PACE_SECONDS),
                "steps_seconds": list(BACKOFF_PACE_SECONDS),
                "per_surface": True,
                "auto_recovery": self.auto_recovery,
                "probe_sample_note": (
                    "0.5s was observed for only 8 calls per step; safe upper "
                    "bound remains unproven."
                ),
            },
        }


def write_surface_manifest(path: Path, manifest: SurfaceManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def assignment_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class KiwoomSurfaceClient:
    """Normalize mock ``post_api`` and live ``post_chart`` for the fetcher."""

    def __init__(self, surface: Surface, client: Any) -> None:
        self.surface = validate_surface(surface)
        self.client = client

    async def post_api(self, **kwargs: Any) -> dict[str, Any]:
        if self.surface == "mock":
            return await self.client.post_api(**kwargs)
        kwargs.pop("path", None)
        return await self.client.post_chart(**kwargs)


def read_assignment_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    validate_surface_rows(rows)
    return rows
