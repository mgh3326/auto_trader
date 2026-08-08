"""Load + validate the ``policy_table.v1`` artifact B0-X derives orders from.

Contract §2-2: **표가 없거나 ``STALE`` 이면 그 사이클은 주문 0** (조용한 재사용·
재계산 금지, 사유 기록). This module is where that rule lives, and it is the
only door through which a table reaches derivation.

The table generator (``scripts/build_policy_table.py``) is read-only to B0-X:
this module never invokes it, never writes into its output directory, and
never recomputes a row it could not read.

Five independent ways a cycle ends at zero orders, each with its own recorded
reason code:

  ``table_missing``        — no ``latest-<market>.json``.
  ``stale_marker_present`` — the generator wrote ``latest-<market>.STALE``.
  ``schema_mismatch``      — not a ``policy_table.v1`` payload, or wrong market.
  ``hash_mismatch``        — recomputed ``policy_table_hash`` != the stamped one
                             (the artifact was edited after generation).
  ``stale_by_age``         — ``generated_at`` older than the lane's max age.

``stale_by_age`` is B0-X's own addition, not the generator's: a table whose
build failed *silently* (process killed before the STALE marker was written)
would otherwise be replayed forever. It can only ever *reduce* the number of
orders a cycle emits, never increase it.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from scripts.policy_table.core.schema import (
    SCHEMA_VERSION,
    compute_policy_table_hash,
    sha256_of_bytes,
)

#: Where ``scripts/build_policy_table.py`` writes by default. Read-only here.
DEFAULT_TABLE_DIR: Final[Path] = (
    Path.home() / "services" / "auto_trader-operator" / "policy-tables"
)

#: Contract §5: crypto tables are rebuilt on a 4h cadence. Two missed builds is
#: the point at which a table stops describing "now" — locked, no CLI flag.
MAX_TABLE_AGE: Final[dict[str, dt.timedelta]] = {
    "crypto": dt.timedelta(hours=8),
}


class TableReason:
    OK = "ok"
    TABLE_MISSING = "table_missing"
    STALE_MARKER_PRESENT = "stale_marker_present"
    SCHEMA_MISMATCH = "schema_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    STALE_BY_AGE = "stale_by_age"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class PolicyTable:
    """A validated table. Existence of this object == derivation may proceed."""

    market: str
    path: Path
    payload: dict[str, Any]
    policy_table_hash: str
    artifact_sha256: str
    generated_at: dt.datetime
    age: dt.timedelta

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self.payload.get("rows") or [])

    @property
    def config(self) -> dict[str, Any]:
        return dict(self.payload.get("config") or {})

    @property
    def sizing(self) -> dict[str, Any]:
        return dict(self.payload.get("sizing") or {})


@dataclass(frozen=True, slots=True)
class TableUnavailable:
    """No usable table. The caller MUST emit zero orders and record ``reason``."""

    market: str
    reason: str
    detail: str
    path: Path | None = None


def _parse_generated_at(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"generated_at {value!r} is not timezone-aware")
    return parsed.astimezone(dt.UTC)


def load_policy_table(
    *,
    market: str,
    now: dt.datetime,
    table_dir: Path = DEFAULT_TABLE_DIR,
) -> PolicyTable | TableUnavailable:
    """Return a validated :class:`PolicyTable` or a :class:`TableUnavailable`.

    Never raises for an absent/broken table — an unusable table is a normal,
    recordable cycle outcome (zero orders), not an error condition.
    """

    table_dir = Path(table_dir).expanduser()
    stale_marker = table_dir / f"latest-{market}.STALE"
    latest = table_dir / f"latest-{market}.json"

    if stale_marker.exists():
        try:
            detail = stale_marker.read_text().strip()
        except OSError as exc:  # pragma: no cover - marker unreadable
            detail = f"marker unreadable: {exc}"
        return TableUnavailable(
            market=market,
            reason=TableReason.STALE_MARKER_PRESENT,
            detail=detail,
            path=stale_marker,
        )

    if not latest.exists():
        return TableUnavailable(
            market=market,
            reason=TableReason.TABLE_MISSING,
            detail=f"no {latest.name} in {table_dir}",
            path=latest,
        )

    try:
        raw = latest.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return TableUnavailable(
            market=market,
            reason=TableReason.UNREADABLE,
            detail=f"{type(exc).__name__}: {exc}",
            path=latest,
        )

    if not isinstance(payload, dict):
        return TableUnavailable(
            market=market,
            reason=TableReason.SCHEMA_MISMATCH,
            detail=f"top-level JSON is {type(payload).__name__}, expected object",
            path=latest,
        )
    if payload.get("schema") != SCHEMA_VERSION:
        return TableUnavailable(
            market=market,
            reason=TableReason.SCHEMA_MISMATCH,
            detail=f"schema={payload.get('schema')!r}, expected {SCHEMA_VERSION!r}",
            path=latest,
        )
    if payload.get("market") != market:
        return TableUnavailable(
            market=market,
            reason=TableReason.SCHEMA_MISMATCH,
            detail=f"market={payload.get('market')!r}, expected {market!r}",
            path=latest,
        )

    stamps = payload.get("stamps")
    if not isinstance(stamps, dict) or not stamps.get("policy_table_hash"):
        return TableUnavailable(
            market=market,
            reason=TableReason.SCHEMA_MISMATCH,
            detail="missing stamps.policy_table_hash",
            path=latest,
        )

    stamped_hash = str(stamps["policy_table_hash"])
    # The generator hashes the payload *before* attaching ``stamps``
    # (build_policy_table._build_stamps). Reproduce that exactly.
    without_stamps = {key: value for key, value in payload.items() if key != "stamps"}
    try:
        recomputed = compute_policy_table_hash(without_stamps)
    except Exception as exc:  # noqa: BLE001 — any serialization fault is a mismatch
        return TableUnavailable(
            market=market,
            reason=TableReason.HASH_MISMATCH,
            detail=f"could not recompute hash: {type(exc).__name__}: {exc}",
            path=latest,
        )
    if recomputed != stamped_hash:
        return TableUnavailable(
            market=market,
            reason=TableReason.HASH_MISMATCH,
            detail=f"recomputed={recomputed} stamped={stamped_hash}",
            path=latest,
        )

    try:
        generated_at = _parse_generated_at(str(payload.get("generated_at", "")))
    except ValueError as exc:
        return TableUnavailable(
            market=market,
            reason=TableReason.SCHEMA_MISMATCH,
            detail=f"bad generated_at: {exc}",
            path=latest,
        )

    age = now.astimezone(dt.UTC) - generated_at
    max_age = MAX_TABLE_AGE.get(market)
    if max_age is not None and age > max_age:
        return TableUnavailable(
            market=market,
            reason=TableReason.STALE_BY_AGE,
            detail=f"age={age} > max_age={max_age} (generated_at={generated_at.isoformat()})",
            path=latest,
        )
    if age < -dt.timedelta(minutes=5):
        # A table stamped in the future means a clock or a hand-edit is wrong.
        return TableUnavailable(
            market=market,
            reason=TableReason.STALE_BY_AGE,
            detail=f"generated_at={generated_at.isoformat()} is in the future (age={age})",
            path=latest,
        )

    return PolicyTable(
        market=market,
        path=latest.resolve(),
        payload=payload,
        policy_table_hash=stamped_hash,
        artifact_sha256=sha256_of_bytes(raw),
        generated_at=generated_at,
        age=age,
    )


__all__ = [
    "DEFAULT_TABLE_DIR",
    "MAX_TABLE_AGE",
    "TableReason",
    "PolicyTable",
    "TableUnavailable",
    "load_policy_table",
]
