"""Holdout hard refusal — dual path + date gate (T3-grade).

Failure of this guard is irreversible: once holdout is observed, OOS value
of the corpus is permanently burned. Therefore:

1. **Path block**: any resolved path under ``HOLDOUT_DIR`` is refused.
2. **Date block**: any session date inside ``HOLDOUT_WINDOW`` is refused.
3. Refusal is an **exception**, never a silent filter (filtered-to-zero
   looks like a successful empty result).
4. Stage A code must never produce holdout metrics in any artifact.

``HOLDOUT_ACCESS_LOG`` is **not** read by this module. Presence of access
there is an external operator failure signal, not an input we consume.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from windows import HOLDOUT_WINDOW, parse_iso_date

__all__ = [
    "HOLDOUT_DIR",
    "HOLDOUT_END",
    "HOLDOUT_START",
    "HoldoutAccessError",
    "HoldoutDateBlocked",
    "HoldoutPathBlocked",
    "assert_date_not_holdout",
    "assert_path_not_holdout",
    "assert_range_not_holdout",
    "holdout_root_resolved",
]

# §1 literal — absolute path only; cwd-relative interpretation forbidden.
HOLDOUT_DIR = Path("/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/holdout/")
HOLDOUT_START = parse_iso_date(HOLDOUT_WINDOW.start)  # 2025-01-01
HOLDOUT_END = parse_iso_date(HOLDOUT_WINDOW.end)  # 2026-07-31


class HoldoutAccessError(RuntimeError):
    """Base: holdout was requested; harness refuses."""


class HoldoutPathBlocked(HoldoutAccessError):
    """Resolved path is HOLDOUT_DIR or a descendant."""


class HoldoutDateBlocked(HoldoutAccessError):
    """Session date or range intersects HOLDOUT_WINDOW."""


def holdout_root_resolved() -> Path:
    """Return the canonical holdout root (resolved, not necessarily existing)."""
    # resolve(strict=False) normalizes .. and symlinks when parents exist.
    return HOLDOUT_DIR.expanduser().resolve()


def assert_path_not_holdout(path: Path | str) -> Path:
    """Resolve ``path`` and refuse if it is under HOLDOUT_DIR.

    Symlinks, relative segments (``..``), and absolute paths are all
    normalized before the check. This is an **exception**, not a filter.
    """
    if isinstance(path, str):
        candidate = Path(path)
    elif isinstance(path, Path):
        candidate = path
    else:
        raise TypeError(f"path must be Path|str, got {type(path)!r}")

    resolved = candidate.expanduser().resolve()
    root = holdout_root_resolved()
    if resolved == root or root in resolved.parents:
        raise HoldoutPathBlocked(
            f"holdout path blocked: {resolved} is under HOLDOUT_DIR={root}"
        )
    return resolved


def assert_date_not_holdout(d: date | str) -> date:
    """Refuse if ``d`` falls inside HOLDOUT_WINDOW (inclusive)."""
    if isinstance(d, str):
        session = parse_iso_date(d)
    elif isinstance(d, date):
        session = d
    else:
        raise TypeError(f"date must be date|str, got {type(d)!r}")

    if HOLDOUT_START <= session <= HOLDOUT_END:
        raise HoldoutDateBlocked(
            f"holdout date blocked: {session.isoformat()} is inside "
            f"HOLDOUT_WINDOW={HOLDOUT_WINDOW.start}..{HOLDOUT_WINDOW.end}"
        )
    return session


def assert_range_not_holdout(start: date | str, end: date | str) -> tuple[date, date]:
    """Refuse if the closed range ``[start, end]`` intersects HOLDOUT_WINDOW."""
    s = _as_date(start)
    e = _as_date(end)
    if s > e:
        raise ValueError(f"invalid range: start {s} > end {e}")
    # Intersect closed intervals [s, e] and [HOLDOUT_START, HOLDOUT_END].
    if s <= HOLDOUT_END and e >= HOLDOUT_START:
        raise HoldoutDateBlocked(
            f"holdout range blocked: requested {s.isoformat()}..{e.isoformat()} "
            f"intersects HOLDOUT_WINDOW={HOLDOUT_WINDOW.start}..{HOLDOUT_WINDOW.end}"
        )
    return s, e


def _as_date(value: date | str) -> date:
    if isinstance(value, str):
        return parse_iso_date(value)
    if isinstance(value, date):
        return value
    raise TypeError(f"date must be date|str, got {type(value)!r}")
