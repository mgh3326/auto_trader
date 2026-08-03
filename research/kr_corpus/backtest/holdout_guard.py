"""Holdout hard refusal — dual path + date gate (T3-grade).

Failure of this guard is irreversible: once holdout is observed, OOS value
of the corpus is permanently burned. Therefore:

1. **Path block**: any resolved path under ``HOLDOUT_DIR`` is refused.
   Comparison is **case-fold** + ``resolve()`` so macOS case-insensitive FS
   variants (``HOLDOUT`` / ``Holdout`` / ``HoLdOuT``) cannot slip through.
2. **Date block**: any session date inside ``HOLDOUT_WINDOW`` is refused.
3. Refusal is an **exception**, never a silent filter (filtered-to-zero
   looks like a successful empty result).
4. Stage A code must never produce holdout metrics in any artifact.

``HOLDOUT_ACCESS_LOG`` is **not** read by this module. Presence of access
there is an external operator failure signal, not an input we consume.

Bypass categories this module must refuse (regression suite pins all):
root · child · abs · rel · ``..`` · ``.`` segment · symlink-to-dir ·
symlink-to-file · CASE(UPPER/Title/Mixed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from windows import HOLDOUT_WINDOW, parse_iso_date

__all__ = [
    "DEFAULT_HOLDOUT_POLICY",
    "HOLDOUT_DIR",
    "HOLDOUT_END",
    "HOLDOUT_START",
    "HoldoutAccessError",
    "HoldoutDateBlocked",
    "HoldoutPolicy",
    "HoldoutPathBlocked",
    "assert_date_not_holdout",
    "assert_partition_year_not_holdout",
    "assert_path_not_holdout",
    "assert_range_not_holdout",
    "holdout_root_resolved",
    "path_is_under_holdout",
]

# §1 literal — absolute path only; cwd-relative interpretation forbidden.
HOLDOUT_DIR = Path("/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/holdout/")
HOLDOUT_START = parse_iso_date(HOLDOUT_WINDOW.start)  # 2025-01-01
HOLDOUT_END = parse_iso_date(HOLDOUT_WINDOW.end)  # 2026-07-31


@dataclass(frozen=True)
class HoldoutPolicy:
    """A corpus-specific holdout root with the shared closed date window.

    The guard algorithm below is deliberately shared by every market adapter.
    A different corpus may name a different holdout directory, but it must not
    receive a copied / weakened path or date implementation.
    """

    holdout_dir: Path
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(
                "invalid holdout policy: "
                f"start {self.start.isoformat()} > end {self.end.isoformat()}"
            )


# Preserve the original KR policy and public constants as the default. Other
# corpus adapters bind their own root to this same guard implementation.
DEFAULT_HOLDOUT_POLICY = HoldoutPolicy(
    holdout_dir=HOLDOUT_DIR,
    start=HOLDOUT_START,
    end=HOLDOUT_END,
)


class HoldoutAccessError(RuntimeError):
    """Base: holdout was requested; harness refuses."""


class HoldoutPathBlocked(HoldoutAccessError):
    """Resolved path is HOLDOUT_DIR or a descendant."""


class HoldoutDateBlocked(HoldoutAccessError):
    """Session date or range intersects HOLDOUT_WINDOW."""


def holdout_root_resolved(*, policy: HoldoutPolicy = DEFAULT_HOLDOUT_POLICY) -> Path:
    """Return the canonical holdout root (resolved, not necessarily existing)."""
    # resolve(strict=False) normalizes .. and symlinks when parents exist.
    return policy.holdout_dir.expanduser().resolve()


def _casefold_key(path: Path | str) -> str:
    """Stable case-insensitive path key (POSIX separators, no trailing slash)."""
    text = str(path)
    # Normalize separators; casefold for macOS case-insensitive FS.
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1 and text.endswith("/"):
        text = text.rstrip("/")
    return text.casefold()


def path_is_under_holdout(
    path: Path | str,
    *,
    policy: HoldoutPolicy = DEFAULT_HOLDOUT_POLICY,
) -> bool:
    """True if ``path`` is HOLDOUT_DIR or a descendant (case-fold + resolve).

    Does **not** open or read any file under holdout — pure path algebra.
    """
    if isinstance(path, str):
        candidate = Path(path)
    elif isinstance(path, Path):
        candidate = path
    else:
        raise TypeError(f"path must be Path|str, got {type(path)!r}")

    root = holdout_root_resolved(policy=policy)
    resolved = candidate.expanduser().resolve()

    # 1) Structural (case-sensitive) — catches symlink / .. after resolve.
    if resolved == root or root in resolved.parents:
        return True

    # 2) Case-fold compare of resolved paths (macOS case-insensitive FS).
    root_cf = _casefold_key(root)
    resolved_cf = _casefold_key(resolved)
    if resolved_cf == root_cf or resolved_cf.startswith(root_cf + "/"):
        return True

    # 3) Case-fold the *input* absolute form before/without relying on on-disk
    #    case canonicalization (non-existent path components keep typed case).
    input_abs = candidate.expanduser()
    if not input_abs.is_absolute():
        input_abs = (Path.cwd() / input_abs).resolve()
    else:
        # resolve for .. and . even when the leaf does not exist
        input_abs = input_abs.resolve()
    input_cf = _casefold_key(input_abs)
    if input_cf == root_cf or input_cf.startswith(root_cf + "/"):
        return True

    return False


def assert_path_not_holdout(
    path: Path | str,
    *,
    policy: HoldoutPolicy = DEFAULT_HOLDOUT_POLICY,
) -> Path:
    """Resolve ``path`` and refuse if it is under HOLDOUT_DIR.

    Symlinks, relative segments (``..``, ``.``), absolute paths, and
    **case variants** are all normalized before the check. This is an
    **exception**, not a filter.
    """
    if isinstance(path, str):
        candidate = Path(path)
    elif isinstance(path, Path):
        candidate = path
    else:
        raise TypeError(f"path must be Path|str, got {type(path)!r}")

    resolved = candidate.expanduser().resolve()
    if path_is_under_holdout(candidate, policy=policy):
        root = holdout_root_resolved(policy=policy)
        raise HoldoutPathBlocked(
            f"holdout path blocked: {resolved} is under HOLDOUT_DIR={root} "
            f"(case-fold path gate)"
        )
    return resolved


def assert_date_not_holdout(
    d: date | datetime | str,
    *,
    policy: HoldoutPolicy = DEFAULT_HOLDOUT_POLICY,
) -> date:
    """Refuse if ``d`` falls inside HOLDOUT_WINDOW (inclusive).

    ``datetime`` is accepted and converted via ``.date()``; a holdout calendar
    day still raises ``HoldoutDateBlocked`` (never a bare ``TypeError`` that
    an outer handler could swallow as non-holdout noise).
    """
    session = _as_date(d)

    if policy.start <= session <= policy.end:
        raise HoldoutDateBlocked(
            f"holdout date blocked: {session.isoformat()} is inside "
            f"HOLDOUT_WINDOW={policy.start.isoformat()}..{policy.end.isoformat()}"
        )
    return session


def assert_partition_year_not_holdout(
    year: int,
    *,
    policy: HoldoutPolicy = DEFAULT_HOLDOUT_POLICY,
) -> int:
    """Refuse a partition ``year`` whose calendar year intersects HOLDOUT_WINDOW.

    Pre-parse / pre-open gate for ``dataset/market/year`` partitions so holdout
    years are refused before parquet bytes are decoded.
    """
    if type(year) is not int:
        raise HoldoutDateBlocked(
            f"holdout date blocked: partition year has unsupported type {type(year)!r}"
        )
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    if year_start <= policy.end and year_end >= policy.start:
        raise HoldoutDateBlocked(
            f"holdout date blocked: partition year={year} intersects "
            f"HOLDOUT_WINDOW={policy.start.isoformat()}..{policy.end.isoformat()}"
        )
    return year


def assert_range_not_holdout(
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    policy: HoldoutPolicy = DEFAULT_HOLDOUT_POLICY,
) -> tuple[date, date]:
    """Refuse if the closed range ``[start, end]`` intersects HOLDOUT_WINDOW."""
    s = _as_date(start)
    e = _as_date(end)
    if s > e:
        raise ValueError(f"invalid range: start {s} > end {e}")
    # Intersect closed intervals [s, e] and [HOLDOUT_START, HOLDOUT_END].
    if s <= policy.end and e >= policy.start:
        raise HoldoutDateBlocked(
            f"holdout range blocked: requested {s.isoformat()}..{e.isoformat()} "
            f"intersects HOLDOUT_WINDOW={policy.start.isoformat()}..{policy.end.isoformat()}"
        )
    return s, e


def _as_date(value: date | datetime | str) -> date:
    """Normalize to ``date``; unknown types → HoldoutDateBlocked (not TypeError).

    Using ``type is`` checks so ``datetime`` (a ``date`` subclass) is handled
    explicitly via ``.date()`` rather than compared raw against ``date`` bounds
    (which raises TypeError in Python 3).
    """
    if type(value) is str:
        return parse_iso_date(value)
    if type(value) is datetime:
        return value.date()
    if type(value) is date:
        return value
    # datetime subclasses of date other than datetime itself — convert.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise HoldoutDateBlocked(
        f"holdout date blocked: unsupported type {type(value)!r} "
        f"(expected date|datetime|str YYYY-MM-DD)"
    )
