"""ROB-1303 phase 2 — pre-attribution cache with an explicit freshness state.

Stage 1's failure mode was inventing a cause. This layer's failure mode is the
mirror image: a session that reads the cache first will treat an **empty or
stale** answer as "no catalyst", which is a completely different statement from
``unattributed`` ("we looked and the materials did not explain it") — and both
arrive as silence unless the read says which one it is.

So every read returns a :class:`CacheRead` carrying one of three states:

``missing``
    No entry exists for this (market, session_date, symbol). Nothing was ever
    computed. The caller must fall back to a live computation; answering
    "no catalyst" here would be a fabrication by omission.
``stale``
    An entry exists but is older than its own declared refresh cadence. The
    payload is returned *with its age* so the caller can decide.
``fresh``
    Computed within cadence.

The distinction that makes this work: a cached entry saying "no spike" or
"unattributed" is a **real computed answer** and is ``fresh``. Only the absence
of an entry is ``missing``. Conflating the two is exactly the bug this module
exists to prevent.

Storage is JSON files — no migration, so the cache is exercisable today.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.spike_attribution.spec import PRE_REGISTRATION

_CACHE = PRE_REGISTRATION["cache"]

STATE_FRESH = "fresh"
STATE_STALE = "stale"
STATE_MISSING = "missing"

GRACE_SECONDS: int = int(_CACHE["grace_seconds"])
EXPECTED_REFRESH_SECONDS_BY_MODE: dict[str, int] = {
    mode: int(value)
    for mode, value in _CACHE["expected_refresh_seconds_by_mode"].items()
}
MODE_PREOPEN = "preopen"
MODE_INTRADAY = "intraday"

_ENV_CACHE_DIR = "SPIKE_ATTRIBUTION_CACHE_DIR"
_DEFAULT_CACHE_DIR = Path(".cache") / "spike_attribution"


class CacheError(RuntimeError):
    """Raised only for a genuinely unusable store — never for a miss."""


def cache_dir() -> Path:
    """Where entries live. Overridable for tests and for operator runs."""

    override = os.environ.get(_ENV_CACHE_DIR, "").strip()
    return Path(override) if override else _DEFAULT_CACHE_DIR


def expected_refresh_seconds(mode: str) -> int:
    try:
        return EXPECTED_REFRESH_SECONDS_BY_MODE[mode]
    except KeyError as exc:
        raise CacheError(f"unknown refresh mode: {mode!r}") from exc


@dataclass(frozen=True)
class CacheEntry:
    """One symbol's cached attribution for one session."""

    market: str
    session_date: dt.date
    symbol: str
    mode: str
    computed_at: dt.datetime
    spec_sha256: str
    payload: dict[str, Any]
    last_success_at: dt.datetime | None = None
    last_error: str | None = None
    last_error_at: dt.datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "session_date": self.session_date.isoformat(),
            "symbol": self.symbol,
            "mode": self.mode,
            "computed_at": self.computed_at.isoformat(),
            "spec_sha256": self.spec_sha256,
            "payload": self.payload,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_error": self.last_error,
            "last_error_at": (
                self.last_error_at.isoformat() if self.last_error_at else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CacheEntry:
        def _ts(value: Any) -> dt.datetime | None:
            return dt.datetime.fromisoformat(value) if value else None

        computed_at = _ts(raw["computed_at"])
        if computed_at is None:  # pragma: no cover - guarded by writer
            raise CacheError("entry has no computed_at")
        return cls(
            market=raw["market"],
            session_date=dt.date.fromisoformat(raw["session_date"]),
            symbol=raw["symbol"],
            mode=raw["mode"],
            computed_at=computed_at,
            spec_sha256=raw["spec_sha256"],
            payload=raw.get("payload") or {},
            last_success_at=_ts(raw.get("last_success_at")),
            last_error=raw.get("last_error"),
            last_error_at=_ts(raw.get("last_error_at")),
        )


@dataclass(frozen=True)
class CacheRead:
    """The answer to a cache lookup — never a bare payload."""

    state: str
    market: str
    session_date: dt.date
    symbol: str
    entry: CacheEntry | None = None
    age_seconds: float | None = None
    expected_refresh_seconds: int | None = None
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def usable_without_fallback(self) -> bool:
        """Only a fresh entry answers on its own."""
        return self.state == STATE_FRESH

    @property
    def requires_live_fallback(self) -> bool:
        return self.state == STATE_MISSING

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "market": self.market,
            "session_date": self.session_date.isoformat(),
            "symbol": self.symbol,
            "age_seconds": self.age_seconds,
            "expected_refresh_seconds": self.expected_refresh_seconds,
            "grace_seconds": GRACE_SECONDS,
            "reason": self.reason,
            "notes": list(self.notes),
            # Restated on every read so a consumer that only looks here cannot
            # mistake an absent entry for an absent catalyst.
            "missing_is_not_no_catalyst": True,
            "unattributed_is_a_computed_answer_not_a_miss": True,
            "computed_at": (self.entry.computed_at.isoformat() if self.entry else None),
            "last_success_at": (
                self.entry.last_success_at.isoformat()
                if self.entry and self.entry.last_success_at
                else None
            ),
            "last_error": self.entry.last_error if self.entry else None,
            "last_error_at": (
                self.entry.last_error_at.isoformat()
                if self.entry and self.entry.last_error_at
                else None
            ),
            "payload": self.entry.payload if self.entry else None,
        }


def classify_state(
    *,
    entry: CacheEntry | None,
    now: dt.datetime,
    mode: str | None = None,
) -> tuple[str, float | None, int | None]:
    """Pure freshness ruling. Returns ``(state, age_seconds, expected)``."""

    if entry is None:
        return STATE_MISSING, None, None
    expected = expected_refresh_seconds(mode or entry.mode)
    age = (now - entry.computed_at).total_seconds()
    if age <= expected + GRACE_SECONDS:
        return STATE_FRESH, age, expected
    return STATE_STALE, age, expected


def _entry_path(root: Path, *, market: str, session_date: dt.date, symbol: str) -> Path:
    safe_symbol = symbol.replace("/", "_").replace("..", "_")
    return root / market / session_date.isoformat() / f"{safe_symbol}.json"


def write_entry(entry: CacheEntry, *, root: Path | None = None) -> Path:
    """Atomically persist one entry (tmp file + rename)."""

    base = root or cache_dir()
    path = _entry_path(
        base, market=entry.market, session_date=entry.session_date, symbol=entry.symbol
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entry.as_dict(), handle, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read_entry(
    *, market: str, session_date: dt.date, symbol: str, root: Path | None = None
) -> CacheEntry | None:
    """Return the stored entry, or None. A miss is not an error."""

    path = _entry_path(
        root or cache_dir(), market=market, session_date=session_date, symbol=symbol
    )
    if not path.exists():
        return None
    try:
        return CacheEntry.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        # A corrupt entry is not a cache hit and is not silently a miss either:
        # it is surfaced as missing *with a reason*, so it can be noticed.
        raise CacheError(f"corrupt cache entry at {path}: {exc}") from exc


def lookup(
    *,
    market: str,
    session_date: dt.date,
    symbol: str,
    now: dt.datetime,
    root: Path | None = None,
) -> CacheRead:
    """Cache-first read. Always returns a state; never raises on a miss."""

    notes: list[str] = []
    try:
        entry = read_entry(
            market=market, session_date=session_date, symbol=symbol, root=root
        )
    except CacheError as exc:
        # Corrupt → treated as missing so the caller falls back to a live
        # compute, but the reason travels with it rather than vanishing.
        return CacheRead(
            state=STATE_MISSING,
            market=market,
            session_date=session_date,
            symbol=symbol,
            reason=f"unreadable_entry: {exc}",
            notes=["fall back to live computation"],
        )

    state, age, expected = classify_state(entry=entry, now=now)
    if state == STATE_MISSING:
        reason = "no cache entry was ever written for this symbol/session"
        notes.append("fall back to live computation")
        notes.append("this is NOT evidence that the symbol had no catalyst")
    elif state == STATE_STALE:
        reason = (
            f"entry is {age:.0f}s old, past its {expected}s cadence "
            f"+ {GRACE_SECONDS}s grace"
        )
        notes.append("payload returned with its age; caller decides whether to reuse")
        if entry is not None and entry.last_error:
            notes.append(
                "the last refresh FAILED — the payload below is the last good one"
            )
    else:
        reason = None
        if entry is not None and entry.last_error:
            notes.append(
                "a refresh error is recorded on this entry even though it is "
                "within cadence"
            )

    return CacheRead(
        state=state,
        market=market,
        session_date=session_date,
        symbol=symbol,
        entry=entry,
        age_seconds=age,
        expected_refresh_seconds=expected,
        reason=reason,
        notes=notes,
    )


__all__ = [
    "EXPECTED_REFRESH_SECONDS_BY_MODE",
    "GRACE_SECONDS",
    "MODE_INTRADAY",
    "MODE_PREOPEN",
    "STATE_FRESH",
    "STATE_MISSING",
    "STATE_STALE",
    "CacheEntry",
    "CacheError",
    "CacheRead",
    "cache_dir",
    "classify_state",
    "expected_refresh_seconds",
    "lookup",
    "read_entry",
    "write_entry",
]
